"""Per-section renderers for a ledger entry.

Pure string rendering. The update command composes these with the
frontmatter + format modules to write out a full file. The only I/O is
an optional `git remote get-url origin` lookup per repo (cached via the
`repo_links` map the caller threads through) so a sample commit can
render as a Ctrl+clickable `[repo@sha](github_url)` link instead of a
bare hash that gives no clue which repo it lives in.
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
from ofd.gitio import github_commit_url


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


def render_before_after(
    prim: Primitive,
    key_devs: list[str],
    repo_links: dict[str, str] | None = None,
) -> str:
    chosen = select_canonical_rollout(prim, key_devs)
    if chosen is None:
        if prim.after_snippet:
            lang = _lang_for(prim.file or "")
            def_ref = (
                _commit_ref(
                    prim.definition_commits[0].repo,
                    prim.definition_commits[0].sha,
                    repo_links,
                )
                if prim.definition_commits else ""
            )
            return (
                f"**Definition** (`{prim.file}` at {def_ref}):\n\n"
                f"```{lang}\n{prim.after_snippet}\n```"
            )
        return "_No rollout examples recorded yet._"
    lang = _lang_for(chosen.file)
    lines: list[str] = []
    ref = _commit_ref(chosen.commit.repo, chosen.commit.sha, repo_links)
    header = f"**Before** (`{chosen.file}` at {ref}):"
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


def render_commits(
    prim: Primitive,
    limit: int = 10,
    repo_links: dict[str, str] | None = None,
) -> str:
    lines: list[str] = []
    if prim.definition_commits:
        lines.append("**Definition:**")
        for c in prim.definition_commits:
            ref = _commit_ref(c.repo, c.sha, repo_links)
            lines.append(
                f"- {ref} - {c.subject} ({c.author_name}, {c.committed_at.split('T')[0]})"
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
            commit = rs[0].commit
            mods = sorted({r.file for r in rs})
            mod_display = mods[0] if len(mods) == 1 else f"{len(mods)} files"
            ref = _commit_ref(commit.repo, sha, repo_links)
            lines.append(
                f"- {ref} - {mod_display} ({commit.author_name}, {commit.committed_at.split('T')[0]})"
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


def _commit_ref(repo: str, sha: str, repo_links: dict[str, str] | None) -> str:
    """Format a commit as `[\\`repo@sha\\`](github_url)` when we have a
    GitHub-shaped source for the repo, else the plain `\\`repo@sha\\``.

    `repo_links` maps repo name -> GitHub source URL (the form
    `git@github.com:org/repo.git` or `https://github.com/org/repo.git`),
    typically built once by the caller via `gitio.resolve_github_base`.
    """
    short = _short(sha)
    label = f"{repo}@{short}"
    source = (repo_links or {}).get(repo)
    url = github_commit_url(source, short) if source else None
    return f"[`{label}`]({url})" if url else f"`{label}`"


def _lang_for(file: str) -> str:
    ext = PurePosixPath(file).suffix.lower()
    return {".py": "python", ".xml": "xml", ".rng": "xml", ".js": "javascript"}.get(ext, "")
