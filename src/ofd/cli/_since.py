"""Shared parser for --since overrides.

Accepts a mix of:
  - bare SHAs: apply to every repo in the config
  - `repo=SHA`: override for just that repo
When both are present, the per-repo form wins.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable

import click


def apply_since_overrides(state, config, since_values: Iterable[str]) -> None:
    """Mutate `state` so each repo's last_seen_sha reflects the overrides.

    Validates that every `repo=SHA` prefix matches a configured repo
    name; otherwise exits non-zero with a clear error (avoids silently
    ignoring typos like `--since odo=abc`).
    """
    values = list(since_values)
    if not values:
        return

    known = {r.name for r in config.repos}
    default: str | None = None
    by_repo: dict[str, str] = {}
    for v in values:
        if "=" in v:
            name, sha = v.split("=", 1)
            if name not in known:
                click.echo(
                    f"--since: unknown repo {name!r}; known: {sorted(known)}",
                    err=True,
                )
                sys.exit(2)
            by_repo[name] = sha
        else:
            default = v

    for repo in config.repos:
        sha = by_repo.get(repo.name, default)
        if sha:
            state.get(repo.name).last_seen_sha = sha
