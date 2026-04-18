"""Iterate over eligible primitives, call the LLM backend, write
narratives into existing ledger files.

The narrate step is deliberately separate from `ofd run` so the daily
path stays deterministic and free. Invoke it manually (weekly/monthly)
or via a cron / systemd timer of your own.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ofd.aggregate import build_primitives
from ofd.config import Config
from ofd.events.record import Kind
from ofd.ledger import format as fmt
from ofd.ledger import frontmatter as fm
from ofd.ledger.update import _atomic_write, _category_dir, _slugify
from ofd.narrate.client import NarrateBackend, NarrateError, build_backend
from ofd.narrate.prompts import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_user_prompt_from,
    render_user_prompt,
)

_NEW_API_KINDS = {
    Kind.NEW_PUBLIC_CLASS,
    Kind.NEW_DECORATOR_OR_HELPER,
    Kind.NEW_ENDPOINT,
    Kind.NEW_VIEW_TYPE,
}
_DEPRECATION_KINDS = {Kind.DEPRECATION_WARNING_ADDED}


@dataclass
class NarrateResult:
    written: list[str]   # symbols narrated this run
    skipped: list[str]   # "<symbol>: reason" strings
    failures: list[str]  # "<symbol>: error" strings


def _is_eligible(
    ledger_path: Path,
    status: str,
    rollout_count: int,
    min_rollouts: int,
    allowed_statuses: set[str],
    force: bool,
) -> tuple[bool, str]:
    if not ledger_path.exists():
        return False, "no ledger file (run `ofd ledger update` first)"
    if status not in allowed_statuses and not force:
        return False, f"status={status} not in {sorted(allowed_statuses)}"
    if rollout_count < min_rollouts and not force:
        return False, f"rollout_count={rollout_count} < min_rollouts={min_rollouts}"
    return True, ""


def _write_narrative(ledger_path: Path, new_narrative: str, force: bool) -> bool:
    """Write narrative into the ledger file's narrative block. Returns
    True if the block was actually updated."""
    data, body = fm.split(ledger_path.read_text())
    parsed = fmt.parse_body(body)
    existing_narrative = parsed.marker_content.get("narrative", "").strip()
    if existing_narrative and not force:
        return False

    policy = "force" if force else "fill_if_empty"
    new_body = fmt.render_body(
        parsed,
        regenerated={"narrative": new_narrative.strip() + "\n"},
        default_layout=[],
        narrative_policy=policy,
    )
    data["narrated_prompt_version"] = PROMPT_VERSION
    _atomic_write(ledger_path, fm.join(data, new_body))
    return True


def narrate_all(
    workspace: Path,
    config: Config,
    *,
    backend: NarrateBackend | None = None,
    symbol_filter: str | None = None,
    status_filter: list[str] | None = None,
    min_rollouts: int | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> NarrateResult:
    if backend is None:
        backend = build_backend(config.narrate.backend, config.narrate.model)

    allowed_statuses = set(status_filter or config.narrate.default_status_filter)
    min_rollouts = (
        min_rollouts if min_rollouts is not None else config.narrate.min_rollouts
    )

    result = NarrateResult(written=[], skipped=[], failures=[])

    primitives = build_primitives(workspace, [r.name for r in config.repos])
    for symbol, prim in primitives.items():
        if symbol_filter and symbol != symbol_filter:
            continue
        if prim.kind not in _NEW_API_KINDS | _DEPRECATION_KINDS:
            continue
        category = _category_dir(prim.kind)
        ledger_path = workspace / "ledger" / category / f"{_slugify(symbol)}.md"

        data, _body = (fm.split(ledger_path.read_text()) if ledger_path.exists() else ({}, ""))
        status = str(data.get("status", "?"))
        rollout_count = int(data.get("rollout_count") or 0)

        ok, reason = _is_eligible(
            ledger_path, status, rollout_count, min_rollouts, allowed_statuses, force,
        )
        if not ok:
            result.skipped.append(f"{symbol}: {reason}")
            continue

        # Skip if narrative already filled and not --force.
        body_now = ledger_path.read_text()
        parsed = fmt.parse_body(fm.split(body_now)[1])
        if parsed.marker_content.get("narrative", "").strip() and not force:
            result.skipped.append(f"{symbol}: narrative already present")
            continue

        user_input = build_user_prompt_from(prim, config.key_devs)
        user_prompt = render_user_prompt(user_input)

        if dry_run:
            result.skipped.append(f"{symbol}: dry-run (would narrate)")
            continue

        try:
            text = backend.narrate(SYSTEM_PROMPT, user_prompt)
        except NarrateError as e:
            result.failures.append(f"{symbol}: {e}")
            continue

        if _write_narrative(ledger_path, text, force=force):
            result.written.append(symbol)
        else:
            result.skipped.append(f"{symbol}: narrative already present")

    return result
