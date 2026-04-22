"""`ofd watchlist add|remove|list` - manual watchlist pins.

For symbols whose definitions the Python/RNG extractors can't see:
context keys (`formatted_display_name`), registry names, magic strings,
convention flags. Once pinned, the existing rollout matcher will still
pick them up because it works off raw diff content, not a discovered
definition site.
"""

from __future__ import annotations

import sys

import click

from ofd import config as config_mod
from ofd import watchlist as watchlist_mod
from ofd.config import resolve_workspace


@click.group("watchlist")
def watchlist_cli():
    """Manual watchlist pins (for context keys / magic strings)."""


@watchlist_cli.command("add")
@click.argument("symbol")
@click.option("--workspace", "workspace_path", default=None)
@click.option("--short", "short_name", default=None, help="Override short_name (default: last segment).")
@click.option("--version", "active_version", default=None, help="Series to stamp (default: config active_version).")
@click.option("--note", default=None, help="Free-form reason / context.")
def add(
    symbol: str,
    workspace_path: str | None,
    short_name: str | None,
    active_version: str | None,
    note: str | None,
):
    """Pin SYMBOL to the watchlist.

    SYMBOL is the fully-qualified or bare name to track (e.g.
    `formatted_display_name`, `odoo.api.depends_context.formatted_display_name`).
    """
    workspace = resolve_workspace(workspace_path)
    config = config_mod.load(workspace)
    version = active_version or config.active_version

    wl = watchlist_mod.load(workspace)
    if symbol in wl.entries:
        click.echo(f"already in watchlist: {symbol}", err=True)
        sys.exit(1)

    entry = wl.add_manual(
        symbol=symbol,
        active_version=version,
        note=note,
        short_name=short_name,
    )
    watchlist_mod.save(wl, workspace)
    click.echo(
        f"pinned {entry.symbol}  short_name={entry.short_name}  version={entry.active_version}"
    )
    click.echo("run 'ofd reindex --watchlist-changed' to replay rollout detection.", err=True)


@watchlist_cli.command("remove")
@click.argument("symbol")
@click.option("--workspace", "workspace_path", default=None)
def remove(symbol: str, workspace_path: str | None):
    """Remove SYMBOL from the watchlist."""
    workspace = resolve_workspace(workspace_path)
    wl = watchlist_mod.load(workspace)
    if symbol not in wl.entries and symbol not in {e.short_name for e in wl.entries.values()}:
        click.echo(f"not in watchlist: {symbol}", err=True)
        sys.exit(1)
    # Accept short-name too, remove the first match.
    target = symbol if symbol in wl.entries else next(
        e.symbol for e in wl.entries.values() if e.short_name == symbol
    )
    wl.remove(target)
    watchlist_mod.save(wl, workspace)
    click.echo(f"removed {target}")


@watchlist_cli.command("list")
@click.option("--workspace", "workspace_path", default=None)
@click.option("--manual-only", is_flag=True, help="Only show manual pins.")
@click.option("--plain", is_flag=True, help="Disable colors/table.")
def list_cmd(workspace_path: str | None, manual_only: bool, plain: bool):
    """List watchlist entries."""
    workspace = resolve_workspace(workspace_path)
    wl = watchlist_mod.load(workspace)
    entries = sorted(wl.entries.values(), key=lambda e: (e.source != "manual", e.symbol))
    if manual_only:
        entries = [e for e in entries if e.source == "manual"]
    if not entries:
        click.echo("watchlist is empty")
        return
    if plain or not sys.stdout.isatty():
        for e in entries:
            click.echo(f"{e.source:<10s}  {e.active_version:<8s}  {e.symbol}")
        return
    from rich.console import Console
    from rich.table import Table
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("Source")
    table.add_column("Version", style="green")
    table.add_column("Short", style="dim")
    table.add_column("Symbol")
    table.add_column("Note", style="dim italic")
    for e in entries:
        src_style = "bold magenta" if e.source == "manual" else "dim"
        table.add_row(
            f"[{src_style}]{e.source}[/]",
            e.active_version or "-",
            e.short_name,
            e.symbol,
            e.note or "",
        )
    Console().print(table)
