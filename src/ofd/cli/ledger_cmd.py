"""`ofd ledger ...` command group."""

from __future__ import annotations

import click

from ofd import config as config_mod
from ofd.config import resolve_workspace
from ofd.ledger.update import update as ledger_update
from ofd.narrate.runner import narrate_all


@click.group("ledger")
def ledger():
    """Ledger maintenance commands."""


@ledger.command("update")
@click.option("--workspace", "workspace_path", default=None)
@click.option("--symbol", default=None, help="Only update this one primitive.")
@click.option("--force-narrative", is_flag=True, help="Regenerate narrative even if it exists.")
def update_cmd(workspace_path: str | None, symbol: str | None, force_narrative: bool):
    """Refresh per-primitive ledger files from the raw event store."""
    workspace = resolve_workspace(workspace_path)
    config = config_mod.load(workspace)
    summary = ledger_update(
        workspace, config, symbol_filter=symbol, force_narrative=force_narrative
    )
    click.echo(f"wrote {len(summary.written)} ledger file(s)")
    for skipped in summary.skipped:
        click.echo(f"skipped: {skipped}", err=True)


@ledger.command("narrate")
@click.option("--workspace", "workspace_path", default=None)
@click.option("--symbol", default=None, help="Only narrate this one primitive.")
@click.option(
    "--status",
    multiple=True,
    help="Override status filter (e.g. --status fresh --status active).",
)
@click.option("--min-rollouts", type=int, default=None, help="Override rollout threshold.")
@click.option("--force", is_flag=True, help="Regenerate narrative even if it exists.")
@click.option("--dry-run", is_flag=True, help="List primitives that would be narrated.")
def narrate_cmd(
    workspace_path: str | None,
    symbol: str | None,
    status: tuple[str, ...],
    min_rollouts: int | None,
    force: bool,
    dry_run: bool,
):
    """LLM pass - fills the narrative block for eligible primitives."""
    workspace = resolve_workspace(workspace_path)
    config = config_mod.load(workspace)
    status_list = list(status) if status else None
    result = narrate_all(
        workspace,
        config,
        symbol_filter=symbol,
        status_filter=status_list,
        min_rollouts=min_rollouts,
        force=force,
        dry_run=dry_run,
    )
    click.echo(f"narrated: {len(result.written)}")
    for s in result.skipped:
        click.echo(f"skipped: {s}", err=True)
    for f in result.failures:
        click.echo(f"failed: {f}", err=True)
