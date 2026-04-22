"""Daily digest renderer - the morning read.

Three sections:
  1. New primitives landed in the window (every framework-path definition,
     regardless of score - nothing is lost).
  2. Adoption velocity: watchlisted symbols that gained rollouts in the
     window, sorted by number of new rollouts.
  3. Deprecations and removals in the window.

Input: raw event store. Output: one markdown file.
"""

from __future__ import annotations

import os
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from ofd.config import Config
from ofd.events.record import DEFINITION_KINDS, Kind
from ofd.events.store import iter_repo


@dataclass
class DigestSections:
    new_primitives: list[tuple[str, str, str]] = field(default_factory=list)  # (symbol, kind, subject)
    adoption_velocity: list[tuple[str, int, str]] = field(default_factory=list)  # (symbol, count_in_window, sample_commit)
    deprecations: list[tuple[str, str, str]] = field(default_factory=list)  # (symbol, removal_version, warning)


def _parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def build_sections(
    workspace: Path,
    config: Config,
    window_start: datetime,
    window_end: datetime,
) -> DigestSections:
    new_by_symbol: dict[str, tuple[str, str]] = {}
    adoption_counts: dict[str, int] = defaultdict(int)
    adoption_sample: dict[str, str] = {}
    deprecations: list[tuple[str, str, str]] = []

    for repo in config.repos:
        for commit_record in iter_repo(workspace, repo.name):
            ts = _parse_iso(commit_record.commit.committed_at)
            if not (window_start <= ts <= window_end):
                continue
            for change in commit_record.changes:
                if change.kind in DEFINITION_KINDS and change.symbol:
                    new_by_symbol.setdefault(
                        change.symbol,
                        (change.kind.value, commit_record.commit.subject),
                    )
                elif change.kind == Kind.ROLLOUT and change.symbol:
                    adoption_counts[change.symbol] += 1
                    adoption_sample.setdefault(
                        change.symbol, commit_record.commit.sha
                    )
                elif change.kind == Kind.DEPRECATION_WARNING_ADDED:
                    deprecations.append((
                        change.symbol_hint or change.symbol or "?",
                        change.removal_version or "-",
                        change.warning_text or "",
                    ))

    sections = DigestSections()
    for sym, (kind, subject) in sorted(new_by_symbol.items()):
        sections.new_primitives.append((sym, kind, subject))
    for sym, count in sorted(
        adoption_counts.items(), key=lambda kv: (-kv[1], kv[0])
    ):
        sections.adoption_velocity.append((sym, count, adoption_sample[sym][:12]))
    sections.deprecations = deprecations
    return sections


def render(sections: DigestSections, target_date: date) -> str:
    """Render a digest into markdown."""
    total_primitives = len(sections.new_primitives)
    total_rollouts = sum(c for _, c, _ in sections.adoption_velocity)
    total_deprecations = len(sections.deprecations)

    lines: list[str] = [
        f"# Digest - {target_date.isoformat()}",
        "",
        f"_{total_primitives} new primitive(s) · {total_rollouts} new rollout(s) · "
        f"{total_deprecations} deprecation(s)._",
        "",
    ]

    lines.append("## New primitives")
    lines.append("")
    if sections.new_primitives:
        # Group by kind so the reader scans categories, not a flat list.
        by_kind: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for sym, kind, subject in sections.new_primitives:
            by_kind[kind].append((sym, subject))
        for kind in sorted(by_kind):
            lines.append(f"### {kind.replace('_', ' ')}")
            lines.append("")
            for sym, subject in by_kind[kind]:
                lines.append(f"- **{sym}** - {subject}")
            lines.append("")
    else:
        lines.append("_None._")
        lines.append("")

    lines.append("## Adoption velocity")
    lines.append("")
    if sections.adoption_velocity:
        lines.append("| Rollouts | Symbol | Sample commit |")
        lines.append("|---:|---|---|")
        for sym, count, sha in sections.adoption_velocity:
            lines.append(f"| {count} | `{sym}` | `{sha}` |")
    else:
        lines.append("_No new rollouts of watchlisted primitives._")
    lines.append("")

    lines.append("## Deprecations")
    lines.append("")
    if sections.deprecations:
        for hint, removal, warning in sections.deprecations:
            lines.append(f"- **{hint}** - removed in **{removal}**")
            if warning:
                lines.append(f"  > {warning}")
    else:
        lines.append("_None._")
    lines.append("")

    return "\n".join(lines)


def write(workspace: Path, target_date: date, content: str) -> Path:
    path = workspace / "digests" / f"{target_date.isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def build_and_render(
    workspace: Path,
    config: Config,
    target_date: date | None = None,
    window_days: int = 1,
) -> tuple[Path, str]:
    """High-level helper: build sections for a single day and write the file."""
    target_date = target_date or datetime.now(tz=UTC).date()
    end = datetime.combine(target_date, datetime.max.time()).replace(tzinfo=UTC)
    start = end - timedelta(days=window_days)
    sections = build_sections(workspace, config, start, end)
    content = render(sections, target_date)
    path = write(workspace, target_date, content)
    return path, content
