"""`ofd run` - ingest + score. No LLM here."""

from __future__ import annotations

import sys

import click

from ofd import config as config_mod
from ofd import state as state_mod
from ofd import watchlist as watchlist_mod
from ofd.config import resolve_workspace
from ofd.pipeline import run as run_pipeline


@click.command("run")
@click.option("--workspace", "workspace_path", default=None, help="Workspace directory.")
@click.option("--since", "since_override", default=None, help="Override state: process from this commit (exclusive).")
@click.option("--quiet", is_flag=True, help="Only print errors.")
@click.option("--no-fetch", is_flag=True, help="Skip git fetch; use cached mirror state.")
def run(workspace_path: str | None, since_override: str | None, quiet: bool, no_fetch: bool):
    """Ingest new commits, extract events, update ledger structural sections."""
    workspace = resolve_workspace(workspace_path)
    config = config_mod.load(workspace)
    state = state_mod.load()
    watchlist = watchlist_mod.load(workspace)

    if not no_fetch:
        from ofd.mirrors import fetch_all
        try:
            fetch_all(config)
        except Exception as e:
            click.echo(f"fetch failed: {e}", err=True)

    # Since-override routes through state.RepoState for symmetry.
    if since_override:
        for repo in config.repos:
            state.get(repo.name).last_seen_sha = since_override

    summary = run_pipeline(config, state, watchlist)

    if not quiet:
        for repo, commits in summary.repos.items():
            persisted = sum(1 for c in commits if c.persisted)
            click.echo(
                f"{repo}: {len(commits)} commit(s) scanned, "
                f"{persisted} with changes, {summary.total_changes} total events"
            )
    if summary.errors:
        for err in summary.errors:
            click.echo(err, err=True)
        sys.exit(1)
