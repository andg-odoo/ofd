"""`ofd reindex` - re-run extraction over stored commits.

Needed after changing extractor rules, adding gated paths, or manually
editing the watchlist. Walks every stored raw event file by SHA and
asks the pipeline to re-extract that commit.
"""

from __future__ import annotations

import sys

import click

from ofd import config as config_mod
from ofd import state as state_mod
from ofd import watchlist as watchlist_mod
from ofd.cli._progress import run_pipeline_with_progress, want_progress
from ofd.cli._since import apply_since_overrides as _apply_since_overrides
from ofd.config import resolve_workspace
from ofd.events.store import prune_before
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
@click.option("--no-progress", is_flag=True, help="Disable progress bar.")
def reindex(
    workspace_path: str | None,
    since_overrides: tuple[str, ...],
    watchlist_changed: bool,
    no_progress: bool,
):
    """Re-run extraction over stored commits."""
    workspace = resolve_workspace(workspace_path)
    config = config_mod.load(workspace)

    # `--watchlist-changed` is a v2 optimization; for now it behaves the
    # same as a full rebuild but with the existing watchlist preserved.
    state = state_mod.State()  # wipe state so we re-walk from scratch
    if watchlist_changed:
        wl = watchlist_mod.load(workspace)
    else:
        # Fresh watchlist - but carry forward manual pins, since their
        # definitions aren't discoverable by the extractors.
        existing = watchlist_mod.load(workspace)
        wl = watchlist_mod.Watchlist()
        for entry in existing.manual_entries():
            wl.entries[entry.symbol] = entry

    _apply_since_overrides(state, config, since_overrides)

    # Prune raws older than the since_date floor so the raw store stays
    # consistent with what we'd enumerate on this run. Without this,
    # leftover files from a previous unbounded walk keep propagating
    # into the ledger. Explicit `--since SHA` overrides skip pruning:
    # the SHA boundary isn't date-comparable and the user may have
    # narrowed the walk intentionally.
    if config.since_date and not since_overrides:
        _prune_stale_raws(config)

    if want_progress(explicit_disable=no_progress):
        summary = run_pipeline_with_progress(config, state, wl)
    else:
        summary = run_pipeline(config, state, wl)
    click.echo(
        f"reindexed {summary.total_commits} commit(s), "
        f"{summary.total_changes} total events"
    )
    for err in summary.errors:
        click.echo(err, err=True)


def _prune_stale_raws(config) -> None:
    """Drop raws older than `config.since_date`, per repo, with a rich
    spinner if we're on a TTY. The scan itself reads every raw's
    frontmatter, which takes real seconds on a big workspace - worth a
    status line so the user knows what's happening."""
    ttyish = sys.stderr.isatty()
    if ttyish:
        from rich.console import Console
        console = Console(stderr=True)
        for repo in config.repos:
            with console.status(
                f"[dim]pruning {repo.name} raws older than "
                f"{config.since_date}...[/]",
                spinner="dots",
            ):
                n = prune_before(config.workspace, repo.name, config.since_date)
            if n:
                console.print(
                    f"[dim]· {repo.name}: pruned {n} raw(s) "
                    f"older than {config.since_date}[/]"
                )
    else:
        for repo in config.repos:
            n = prune_before(config.workspace, repo.name, config.since_date)
            if n:
                click.echo(
                    f"{repo.name}: pruned {n} raw(s) older than "
                    f"{config.since_date}",
                    err=True,
                )
