"""`ofd reindex` - re-run extraction over stored commits.

Needed after changing extractor rules, adding gated paths, or manually
editing the watchlist. Walks every stored raw event file by SHA and
asks the pipeline to re-extract that commit.
"""

from __future__ import annotations

import click

from ofd import config as config_mod
from ofd import state as state_mod
from ofd import watchlist as watchlist_mod
from ofd.cli._since import apply_since_overrides as _apply_since_overrides
from ofd.config import resolve_workspace
from ofd.pipeline import run as run_pipeline


@click.command("reindex")
@click.option("--workspace", "workspace_path", default=None)
@click.option(
    "--since",
    "since_overrides",
    multiple=True,
    default=(),
    help="Start point. Bare SHA applies to all repos; REPO=SHA scopes to one. Repeatable.",
)
@click.option(
    "--watchlist-changed",
    is_flag=True,
    help="Cheaper mode: only the rollout pass is re-run. (Future: when implemented.)",
)
def reindex(workspace_path: str | None, since_overrides: tuple[str, ...], watchlist_changed: bool):
    """Re-run extraction over stored commits."""
    workspace = resolve_workspace(workspace_path)
    config = config_mod.load(workspace)

    # `--watchlist-changed` is a v2 optimization; for now it behaves the
    # same as a full rebuild but with the existing watchlist preserved.
    state = state_mod.State()  # wipe state so we re-walk from scratch
    wl = watchlist_mod.load(workspace) if watchlist_changed else watchlist_mod.Watchlist()

    _apply_since_overrides(state, config, since_overrides)

    summary = run_pipeline(config, state, wl)
    click.echo(
        f"reindexed {summary.total_commits} commit(s), "
        f"{summary.total_changes} total events"
    )
    for err in summary.errors:
        click.echo(err, err=True)
