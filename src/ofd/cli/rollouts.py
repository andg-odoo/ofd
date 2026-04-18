"""`ofd rollouts SYMBOL` - inspect rollout hunks."""

from __future__ import annotations

from pathlib import PurePosixPath

import click

from ofd import config as config_mod
from ofd.aggregate import build_primitives
from ofd.cli._resolve import resolve_symbol
from ofd.config import resolve_workspace


@click.command("rollouts")
@click.argument("symbol")
@click.option("--workspace", "workspace_path", default=None)
@click.option("--limit", type=int, default=10, help="Max hunks to show.")
@click.option("--diff/--no-diff", "show_diff", default=False, help="Include before/after snippets.")
def rollouts(symbol: str, workspace_path: str | None, limit: int, show_diff: bool):
    """List rollouts for SYMBOL; with --diff, include before/after hunks."""
    workspace = resolve_workspace(workspace_path)
    config = config_mod.load(workspace)
    prims = build_primitives(workspace, [r.name for r in config.repos])
    resolved = resolve_symbol(prims.keys(), symbol)
    prim = prims[resolved]

    if not prim.rollouts:
        click.echo("no rollouts recorded yet")
        return

    for i, r in enumerate(prim.rollouts[:limit]):
        click.echo(
            f"--- {i+1}. {r.file} @ {r.commit.sha[:12]} ({r.commit.committed_at[:10]}) ---"
        )
        if r.model:
            click.echo(f"model: {r.model}")
        if not show_diff:
            continue
        lang = _lang_for(r.file)
        click.echo()
        click.echo(f"Before:\n```{lang}")
        click.echo(r.before_snippet or "(none)")
        click.echo("```")
        click.echo(f"After:\n```{lang}")
        click.echo(r.after_snippet or "(none)")
        click.echo("```")
        click.echo()

    if len(prim.rollouts) > limit:
        click.echo(f"... {len(prim.rollouts) - limit} more")


def _lang_for(file: str) -> str:
    ext = PurePosixPath(file).suffix.lower()
    return {".py": "python", ".xml": "xml", ".rng": "xml", ".js": "javascript"}.get(ext, "")
