"""`ofd commits SYMBOL` - list commits relevant to a primitive."""

from __future__ import annotations

import click

from ofd import config as config_mod
from ofd.aggregate import build_primitives
from ofd.cli._resolve import resolve_symbol
from ofd.config import resolve_workspace


@click.command("commits")
@click.argument("symbol")
@click.option("--workspace", "workspace_path", default=None)
@click.option(
    "--kind",
    type=click.Choice(["definition", "rollout", "all"]),
    default="all",
)
def commits(symbol: str, workspace_path: str | None, kind: str):
    """Print commit SHAs and subjects for SYMBOL, ready to feed to `git show`."""
    workspace = resolve_workspace(workspace_path)
    config = config_mod.load(workspace)
    prims = build_primitives(workspace, [r.name for r in config.repos])
    resolved = resolve_symbol(prims.keys(), symbol)
    prim = prims[resolved]

    if kind in ("definition", "all"):
        for c in prim.definition_commits:
            click.echo(f"definition  {c.sha}  {c.committed_at[:10]}  {c.author_name}  {c.subject}")

    if kind in ("rollout", "all"):
        seen: set[str] = set()
        for r in prim.rollouts:
            if r.commit.sha in seen:
                continue
            seen.add(r.commit.sha)
            click.echo(
                f"rollout     {r.commit.sha}  {r.commit.committed_at[:10]}  "
                f"{r.commit.author_name}  {r.commit.subject}"
            )
