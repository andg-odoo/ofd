"""`ofd run` - ingest + score. No LLM here."""

from __future__ import annotations

import sys

import click

from ofd import config as config_mod
from ofd import state as state_mod
from ofd import watchlist as watchlist_mod
from ofd.cli._progress import run_pipeline_with_progress, want_progress
from ofd.cli._since import apply_since_overrides as _apply_since_overrides
from ofd.config import resolve_workspace
from ofd.pipeline import run as run_pipeline


@click.command("run")
@click.option("--workspace", "workspace_path", default=None, help="Workspace directory.")
@click.option(
    "--since",
    "since_overrides",
    multiple=True,
    default=(),
    help="Override state. Bare SHA applies to all repos; REPO=SHA scopes to one. Repeatable.",
)
@click.option("--quiet", is_flag=True, help="Only print errors.")
@click.option("--no-fetch", is_flag=True, help="Skip git fetch; use cached mirror state.")
@click.option("--no-progress", is_flag=True, help="Disable progress bar (default: on in TTY).")
def run(
    workspace_path: str | None,
    since_overrides: tuple[str, ...],
    quiet: bool,
    no_fetch: bool,
    no_progress: bool,
):
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

    _apply_since_overrides(state, config, since_overrides)

    if want_progress(quiet=quiet, explicit_disable=no_progress):
        summary = run_pipeline_with_progress(config, state, watchlist)
    else:
        summary = run_pipeline(config, state, watchlist)

    if not quiet:
        _print_summary(summary)
    if summary.errors:
        for err in summary.errors:
            click.echo(err, err=True)
        sys.exit(1)


def _print_summary(summary) -> None:
    """Colored per-repo summary; green check on quiet repos, cyan counts
    on active ones. Falls back to plain on non-TTY."""
    if not sys.stdout.isatty():
        for repo, commits in summary.repos.items():
            persisted = sum(1 for c in commits if c.persisted)
            events = sum(c.changes for c in commits)
            click.echo(
                f"{repo}: {len(commits)} commit(s), {persisted} persisted, {events} events"
            )
        return

    from rich.console import Console
    console = Console()
    name_width = max((len(r) for r in summary.repos), default=0)
    for repo, commits in summary.repos.items():
        persisted = sum(1 for c in commits if c.persisted)
        events = sum(c.changes for c in commits)
        if not commits:
            console.print(
                f"[green]✓[/] [bold]{repo:<{name_width}}[/]  [dim]up to date[/]"
            )
            continue
        console.print(
            f"[cyan]•[/] [bold]{repo:<{name_width}}[/]  "
            f"[bold]{len(commits)}[/] commit{'s' if len(commits) != 1 else ''}  "
            f"[dim]·[/]  [bold]{persisted}[/] persisted  "
            f"[dim]·[/]  [bold cyan]{events}[/] events"
        )


