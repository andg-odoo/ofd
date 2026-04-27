"""Build and persist per-primitive ledger files.

Reads the raw event store, groups events into primitives, then for each
primitive writes (or refreshes) `ledger/<category>/<symbol>.md`. The
machine-owned frontmatter and `<!-- ofd:auto:* -->` sections are
overwritten. The `<!-- ofd:narrative -->` block is preserved unless the
caller passes `force_narrative=True`. Anything outside the markers is
never touched.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ofd.aggregate import Primitive, build_primitives
from ofd.config import Config
from ofd.events.record import Kind
from ofd.ledger import format as fmt
from ofd.ledger import frontmatter as fm
from ofd.ledger.render import (
    render_adoption,
    render_before_after,
    render_commits,
    render_summary,
)
from ofd.ledger.status import compute_status
from ofd.scoring import aggregate_score

_NEW_API_KINDS = {
    Kind.NEW_PUBLIC_CLASS,
    Kind.NEW_DECORATOR_OR_HELPER,
    Kind.NEW_ENDPOINT,
    Kind.NEW_KWARG,
    Kind.NEW_VIEW_TYPE,
    Kind.NEW_VIEW_ATTRIBUTE,
    Kind.NEW_VIEW_ELEMENT,
    Kind.NEW_VIEW_DIRECTIVE,
    Kind.NEW_CONTEXT_KEY,
}
_DEPRECATION_KINDS = {
    Kind.DEPRECATION_WARNING_ADDED,
    Kind.REMOVED_VIEW_ATTRIBUTE,
    Kind.REMOVED_PUBLIC_SYMBOL,
}


@dataclass
class LedgerSummary:
    written: list[Path]
    skipped: list[str]  # symbol -> reason
    deleted: list[Path] = field(default_factory=list)
    preserved: list[Path] = field(default_factory=list)


def _category_dir(kind: Kind) -> str:
    if kind in _DEPRECATION_KINDS:
        return "deprecations"
    return "new-apis"


def _slugify(symbol: str) -> str:
    # File name mirrors the dotted symbol; safe on POSIX FS.
    return symbol.replace("/", "_")


def _default_layout() -> list[tuple[str, str]]:
    return [
        ("text", "# SYMBOL_HEADER_PLACEHOLDER\n\n"),
        ("marker", "auto:summary"),
        ("text", "\n"),
        ("marker", "narrative"),
        ("text", "\n"),
        ("marker", "auto:before_after"),
        ("text", "\n"),
        ("marker", "auto:commits"),
        ("text", "\n"),
        ("marker", "auto:adoption"),
        ("text", "\n## Notes\n\n"),
    ]


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def update_one(
    prim: Primitive,
    workspace: Path,
    config: Config,
    now: datetime | None = None,
    force_narrative: bool = False,
) -> Path:
    """Render and write one ledger entry. Returns the written path."""
    now = now or datetime.now(tz=UTC)
    category = _category_dir(prim.kind)
    path = workspace / "ledger" / category / f"{_slugify(prim.symbol)}.md"

    # Load existing state (if any) so human-owned sections are preserved.
    if path.exists():
        existing_data, existing_body = fm.split(path.read_text())
    else:
        existing_data, existing_body = {}, ""
    parsed = fmt.parse_body(existing_body) if existing_body else fmt.ParsedBody()

    pinned = bool(existing_data.get("pinned"))
    pin_reason = existing_data.get("pin_reason") or None
    status = compute_status(
        prim,
        fresh_days=config.scoring.fresh_days,
        dormant_days=config.scoring.dormant_days,
        pinned=pinned,
        now=now,
    )

    definition_score = (
        prim.definition_record.score
        if prim.definition_record and prim.definition_record.score
        else 0
    )
    total_score = aggregate_score(
        definition_score,
        prim.rollout_count,
        prim.first_seen if prim.definition_commits else now,
        config.scoring,
        now=now,
    )

    frontmatter_data = {
        "symbol": prim.symbol,
        "kind": prim.kind.value,
        "active_version": prim.active_version,
        "status": status,
        "score": total_score,
        "rollout_count": prim.rollout_count,
        "first_seen": (
            prim.first_seen.date().isoformat() if prim.definition_commits else None
        ),
        "last_updated": now.date().isoformat(),
        "pinned": pinned,
        "pin_reason": pin_reason,
    }
    if prim.kind in _DEPRECATION_KINDS and prim.removal_version:
        frontmatter_data["removal_version"] = prim.removal_version

    regenerated = {
        "auto:summary": render_summary(prim, status),
        "auto:before_after": render_before_after(prim, config.key_devs),
        "auto:commits": render_commits(prim),
        "auto:adoption": render_adoption(prim),
    }

    # Build the body. If this is a fresh file, use the default layout
    # (with the symbol header) and pin a narrative placeholder.
    narrative_policy = "force" if force_narrative else "preserve"
    default_layout = _default_layout()
    body = fmt.render_body(parsed, regenerated, default_layout, narrative_policy)
    # Replace the header placeholder on fresh files.
    if "SYMBOL_HEADER_PLACEHOLDER" in body:
        short = prim.symbol.rsplit(".", 1)[-1]
        body = body.replace("SYMBOL_HEADER_PLACEHOLDER", short, 1)

    out = fm.join(frontmatter_data, body)
    _atomic_write(path, out)
    return path


_NARRATIVE_BLOCK = re.compile(
    r"<!-- ofd:narrative -->(.*?)<!-- /ofd:narrative -->", re.DOTALL
)
_PINNED_LINE = re.compile(r"^pinned:\s*true\b", re.MULTILINE)


def _has_manual_edits(path: Path) -> bool:
    """True if the file carries user-added content we shouldn't drop.

    Keeps anything pinned or with a non-empty narrative. Defensive: if we
    can't read the file, treat it as "keep" so we never delete something
    whose state we can't inspect.
    """
    try:
        txt = path.read_text()
    except OSError:
        return True
    if _PINNED_LINE.search(txt):
        return True
    m = _NARRATIVE_BLOCK.search(txt)
    return bool(m and m.group(1).strip())


def _prune_stale_entries(
    workspace: Path, live_slugs: set[str],
) -> tuple[list[Path], list[Path]]:
    """Delete ledger files whose symbols are no longer in `live_slugs`.

    Returns (deleted, preserved). A file is preserved (not deleted) if
    it has manual content - pins or narrative prose. `live_slugs` is
    the set of `_slugify(symbol)` names that the current build knows
    about.
    """
    deleted: list[Path] = []
    preserved: list[Path] = []
    for category in ("new-apis", "deprecations"):
        cat_dir = workspace / "ledger" / category
        if not cat_dir.exists():
            continue
        for path in cat_dir.glob("*.md"):
            if path.stem in live_slugs:
                continue
            if _has_manual_edits(path):
                preserved.append(path)
                continue
            path.unlink(missing_ok=True)
            deleted.append(path)
    return deleted, preserved


def update(
    workspace: Path,
    config: Config,
    symbol_filter: str | None = None,
    force_narrative: bool = False,
) -> LedgerSummary:
    """Refresh every ledger entry (or the one matching `symbol_filter`).

    On a full rebuild (no `symbol_filter`), also deletes stale entries
    whose primitives are no longer in the raw store - this is what
    keeps the ledger in sync with `since_date`-bounded reindexes.
    Entries with pins or narratives are preserved so manual work isn't
    dropped. Scoped rebuilds (`--symbol X`) never prune; they'd risk
    deleting unrelated entries.
    """
    repo_names = [r.name for r in config.repos]
    primitives = build_primitives(workspace, repo_names)

    written: list[Path] = []
    skipped: list[str] = []

    for symbol, prim in primitives.items():
        if symbol_filter and symbol != symbol_filter:
            continue
        if prim.kind not in _NEW_API_KINDS | _DEPRECATION_KINDS:
            skipped.append(f"{symbol}: kind={prim.kind.value} not promoted to ledger")
            continue
        written.append(update_one(prim, workspace, config, force_narrative=force_narrative))

    deleted: list[Path] = []
    preserved: list[Path] = []
    if not symbol_filter:
        live_slugs = {
            _slugify(sym)
            for sym, prim in primitives.items()
            if prim.kind in _NEW_API_KINDS | _DEPRECATION_KINDS
        }
        deleted, preserved = _prune_stale_entries(workspace, live_slugs)

    return LedgerSummary(
        written=written, skipped=skipped, deleted=deleted, preserved=preserved,
    )
