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

from ofd.config import Config, RepoConfig
from ofd.events.record import DEFINITION_KINDS, Kind
from ofd.events.store import iter_repo
from ofd.gitio import github_commit_url, resolve_github_base


@dataclass
class DeprecationEntry:
    # "module" for a removed module, "warning" for a deprecation warning.
    # The two render differently: a module removal has a name but no
    # removal version (the module is just gone), while a deprecation
    # warning carries the message text and sometimes a target version.
    kind: str
    symbol: str | None  # module symbol; None for bare deprecation warnings
    removal_version: str | None
    text: str  # module description, or the warning message
    # Provenance: a bare warning like "Since 20.0 import ormcache from
    # odoo.api" is opaque on its own, so we surface the commit that
    # introduced it and the file:line it lives at.
    repo: str = ""
    sha: str = ""
    file: str = ""
    line: int = 0


@dataclass
class DigestSections:
    new_primitives: list[tuple[str, str, str]] = field(default_factory=list)  # (symbol, kind, subject)
    adoption_velocity: list[tuple[str, int, str, str]] = field(default_factory=list)  # (symbol, count, repo, sample_sha)
    deprecations: list[DeprecationEntry] = field(default_factory=list)


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
    adoption_sample: dict[str, tuple[str, str]] = {}  # symbol -> (repo, sha)
    deprecations: list[DeprecationEntry] = []
    seen_deprecations: set[tuple[str, str | None, str, str]] = set()

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
                    continue
                if change.kind == Kind.ROLLOUT and change.symbol:
                    adoption_counts[change.symbol] += 1
                    adoption_sample.setdefault(
                        change.symbol,
                        (commit_record.commit.repo, commit_record.commit.sha),
                    )
                    continue

                if change.kind == Kind.DEPRECATION_WARNING_ADDED:
                    # No symbol is attached - the warning message itself is
                    # the payload, so render that rather than a "?" name.
                    entry = DeprecationEntry(
                        kind="warning",
                        symbol=change.symbol_hint or change.symbol,
                        removal_version=change.removal_version,
                        text=change.warning_text or "",
                        repo=commit_record.commit.repo,
                        sha=commit_record.commit.sha,
                        file=change.file,
                        line=change.line,
                    )
                elif change.kind == Kind.REMOVED_MODULE:
                    # Module removals are rare and loud; surface them in
                    # the deprecations section rather than losing them
                    # (REMOVED_MODULE isn't a definition kind). There's no
                    # removal version - the module is simply gone.
                    entry = DeprecationEntry(
                        kind="module",
                        symbol=change.symbol or "?",
                        removal_version=None,
                        text=change.signature or "",
                        repo=commit_record.commit.repo,
                        sha=commit_record.commit.sha,
                        file=change.file,
                        line=change.line,
                    )
                else:
                    continue
                # Dedup on location, not just text: the same warning may be
                # added to several files in one commit, and each site is
                # worth surfacing.
                dedup_key = (entry.kind, entry.symbol, entry.text, entry.file)
                if dedup_key not in seen_deprecations:
                    seen_deprecations.add(dedup_key)
                    deprecations.append(entry)

    sections = DigestSections()
    for sym, (kind, subject) in sorted(new_by_symbol.items()):
        sections.new_primitives.append((sym, kind, subject))
    for sym, count in sorted(
        adoption_counts.items(), key=lambda kv: (-kv[1], kv[0])
    ):
        repo_name, sha = adoption_sample[sym]
        sections.adoption_velocity.append((sym, count, repo_name, sha[:12]))
    sections.deprecations = deprecations
    return sections


def render(
    sections: DigestSections,
    target_date: date,
    repos: list[RepoConfig] | None = None,
) -> str:
    """Render a digest into markdown.

    `repos` is used to derive a GitHub commit URL per sample, so the
    sample-commit cell renders as a clickable `[repo@sha](url)` link.
    Without it (e.g. test fixtures with `source: /dev/null`), the cell
    falls back to plain `repo@sha` text.
    """
    sources_by_repo = {
        r.name: resolve_github_base(r.source, r.mirror) or "" for r in (repos or [])
    }
    total_primitives = len(sections.new_primitives)
    total_rollouts = sum(c for _, c, _, _ in sections.adoption_velocity)
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
        for sym, count, repo, sha in sections.adoption_velocity:
            label = f"{repo}@{sha}"
            url = github_commit_url(sources_by_repo.get(repo, ""), sha)
            cell = f"[`{label}`]({url})" if url else f"`{label}`"
            lines.append(f"| {count} | `{sym}` | {cell} |")
    else:
        lines.append("_No new rollouts of watchlisted primitives._")
    lines.append("")

    lines.append("## Deprecations")
    lines.append("")
    if sections.deprecations:
        def _provenance(entry: DeprecationEntry) -> str:
            # `file:line` plus a clickable commit when the repo source
            # resolves to a GitHub URL (falls back to plain `repo@sha`).
            loc = f"`{entry.file}:{entry.line}`" if entry.file else ""
            sha = entry.sha[:12]
            label = f"{entry.repo}@{sha}"
            url = github_commit_url(sources_by_repo.get(entry.repo, ""), entry.sha)
            commit = f"[`{label}`]({url})" if url else f"`{label}`"
            return " · ".join(part for part in (loc, commit) if part)

        removed_modules = [e for e in sections.deprecations if e.kind == "module"]
        warnings = [e for e in sections.deprecations if e.kind == "warning"]
        if removed_modules:
            lines.append("### Removed modules")
            lines.append("")
            for entry in removed_modules:
                suffix = f" - {entry.text}" if entry.text else ""
                lines.append(f"- **{entry.symbol}**{suffix}")
                lines.append(f"  {_provenance(entry)}")
            lines.append("")
        if warnings:
            lines.append("### Deprecation warnings")
            lines.append("")
            for entry in warnings:
                # The warning text is the headline; prepend the symbol when
                # one is known and a "removed in X" badge when a target
                # version was parsed out of the message. The source commit
                # and file:line follow, since the message alone is opaque.
                head = f"**{entry.symbol}** - " if entry.symbol else ""
                badge = (
                    f"removed in **{entry.removal_version}** - "
                    if entry.removal_version
                    else ""
                )
                lines.append(f"- {head}{badge}{entry.text}")
                lines.append(f"  {_provenance(entry)}")
            lines.append("")
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
    content = render(sections, target_date, repos=config.repos)
    path = write(workspace, target_date, content)
    return path, content
