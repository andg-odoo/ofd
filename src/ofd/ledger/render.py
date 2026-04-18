"""Per-section renderers for a ledger entry.

Pure string rendering. No I/O. The update command composes these with
the frontmatter + format modules to write out a full file.
"""

from __future__ import annotations

from collections import Counter
from pathlib import PurePosixPath

from ofd.aggregate import (
    Primitive,
    select_canonical_rollout,
    select_definition_commit,
)
from ofd.events.record import Kind


def derive_replaces(prim: Primitive) -> str | None:
    """Scan rollouts' `-` hunks for the dominant leading identifier.

    For `models.Constraint` rollouts, virtually every `before_snippet`
    starts with `_sql_constraints = [...]` - that dominant identifier
    becomes the `Replaces:` line. If no clear winner, returns None.
    """
    names: list[str] = []
    for r in prim.rollouts:
        if not r.before_snippet:
            continue
        first = r.before_snippet.lstrip().split("\n", 1)[0].lstrip()
        # Grab the leading assignment name if any.
        if "=" in first:
            lhs = first.split("=", 1)[0].strip()
            if lhs and lhs.replace("_", "").replace(".", "").isalnum():
                names.append(lhs)
    if not names:
        return None
    counts = Counter(names)
    most, freq = counts.most_common(1)[0]
    # Accept only if clearly dominant (>40%).
    if freq / len(names) < 0.4:
        return None
    return most


def render_summary(prim: Primitive, status: str) -> str:
    defining = select_definition_commit(prim)
    parts: list[str] = []
    if defining:
        parts.append(
            f"Introduced in `{prim.file or '?'}` by {defining.author_name} "
            f"on {defining.committed_at.split('T')[0]}."
        )
    replaces = derive_replaces(prim)
    if replaces:
        parts.append(f"Replaces: `{replaces}`.")
    mods = prim.adopting_modules
    if prim.rollout_count:
        parts.append(
            f"Status: {status} - {prim.rollout_count} rollout"
            f"{'s' if prim.rollout_count != 1 else ''} across {len(mods)} addon"
            f"{'s' if len(mods) != 1 else ''}."
        )
    else:
        parts.append(f"Status: {status} - no rollouts yet.")
    if prim.kind == Kind.DEPRECATION_WARNING_ADDED and prim.removal_version:
        parts.append(f"Removed in: {prim.removal_version}.")
    return "\n".join(parts)


def render_before_after(prim: Primitive, key_devs: list[str]) -> str:
    chosen = select_canonical_rollout(prim, key_devs)
    if chosen is None:
        if prim.after_snippet:
            lang = _lang_for(prim.file or "")
            return (
                f"**Definition** (`{prim.file}` at {_short(prim.definition_commits[0].sha) if prim.definition_commits else ''}):\n\n"
                f"```{lang}\n{prim.after_snippet}\n```"
            )
        return "_No rollout examples recorded yet._"
    lang = _lang_for(chosen.file)
    lines: list[str] = []
    header = f"**Before** (`{chosen.file}` at {_short(chosen.commit.sha)}):"
    lines.append(header)
    lines.append("")
    lines.append(f"```{lang}")
    lines.append(chosen.before_snippet or "")
    lines.append("```")
    lines.append("")
    lines.append("**After** (same file, same commit):")
    lines.append("")
    lines.append(f"```{lang}")
    lines.append(chosen.after_snippet or "")
    lines.append("```")
    return "\n".join(lines)


def render_commits(prim: Primitive, limit: int = 10) -> str:
    lines: list[str] = []
    if prim.definition_commits:
        lines.append("**Definition:**")
        for c in prim.definition_commits:
            lines.append(
                f"- `{_short(c.sha)}` - {c.subject} ({c.author_name}, {c.committed_at.split('T')[0]})"
            )
        lines.append("")
    if prim.rollouts:
        rollouts_by_commit: dict[str, list] = {}
        for r in prim.rollouts:
            rollouts_by_commit.setdefault(r.commit.sha, []).append(r)
        ordered = sorted(
            rollouts_by_commit.items(),
            key=lambda kv: kv[1][0].commit.committed_at,
        )
        lines.append("**Rollouts:**")
        for shown, (sha, rs) in enumerate(ordered):
            if shown >= limit:
                break
            ref = rs[0].commit
            mods = sorted({r.file for r in rs})
            mod_display = mods[0] if len(mods) == 1 else f"{len(mods)} files"
            lines.append(
                f"- `{_short(sha)}` - {mod_display} ({ref.author_name}, {ref.committed_at.split('T')[0]})"
            )
        if len(ordered) > limit:
            lines.append(f"- ... {len(ordered) - limit} more")
    return "\n".join(lines).strip() or "_No commits recorded._"


def render_adoption(prim: Primitive) -> str:
    mods = prim.adopting_modules
    if not mods:
        return "_No adoption yet._"
    lines = ["| Addon | First rollout | Count |", "|---|---|---|"]
    for mod in sorted(mods, key=lambda m: mods[m][0]):
        first_dt, count = mods[mod]
        lines.append(f"| {mod} | {first_dt.date().isoformat()} | {count} |")
    return "\n".join(lines)


def _short(sha: str, n: int = 12) -> str:
    return sha[:n]


def _lang_for(file: str) -> str:
    ext = PurePosixPath(file).suffix.lower()
    return {".py": "python", ".xml": "xml", ".rng": "xml", ".js": "javascript"}.get(ext, "")
