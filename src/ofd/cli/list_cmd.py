"""`ofd list` - one line per ledger entry."""

from __future__ import annotations

import sys

import click

from ofd.config import resolve_workspace
from ofd.ledger.read import iter_entries

_SORT_KEYS = {
    "score": lambda e: (-e.score, e.symbol),
    "breadth": lambda e: (-e.rollout_count, -e.score, e.symbol),
    "date": lambda e: (e.first_seen, e.symbol),
    "symbol": lambda e: (e.symbol,),
}

_STATUS_STYLES = {
    "fresh": "bold green",
    "active": "bold cyan",
    "awaiting-adoption": "yellow",
    "dormant": "dim",
    "pinned": "bold magenta",
}


@click.command("list")
@click.option("--workspace", "workspace_path", default=None)
@click.option("--kind", "kind_filter", default=None, help="Filter by kind.")
@click.option("--status", "status_filter", default=None, help="Filter by status.")
@click.option("--version", "version_filter", default=None, help="Filter by active_version.")
@click.option(
    "--sort",
    type=click.Choice(list(_SORT_KEYS.keys())),
    default="score",
    help="Sort order (default: score).",
)
@click.option("--limit", type=int, default=None, help="Show at most N rows.")
@click.option("--symbol-only", is_flag=True, help="Print only the symbol column.")
@click.option("--plain", is_flag=True, help="Disable colors/tables (pipe-friendly).")
def list_cmd(
    workspace_path: str | None,
    kind_filter: str | None,
    status_filter: str | None,
    version_filter: str | None,
    sort: str,
    limit: int | None,
    symbol_only: bool,
    plain: bool,
):
    """List ledger entries sorted by score (default)."""
    workspace = resolve_workspace(workspace_path)
    entries = iter_entries(workspace)
    if kind_filter:
        entries = [e for e in entries if e.kind == kind_filter]
    if status_filter:
        entries = [e for e in entries if e.status == status_filter]
    if version_filter:
        entries = [e for e in entries if str(e.frontmatter.get("active_version")) == version_filter]

    entries.sort(key=_SORT_KEYS[sort])
    if limit is not None:
        entries = entries[:limit]

    if symbol_only:
        for e in entries:
            click.echo(e.symbol)
        return

    # Fall back to plain output when piped, asked, or no entries.
    if plain or not sys.stdout.isatty() or not entries:
        for e in entries:
            click.echo(f"{e.score}  {e.status:<20s}  {e.rollout_count:>4d}  {e.symbol}")
        return

    from rich.console import Console
    from rich.table import Table

    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("Score", justify="right", style="bold")
    table.add_column("Status")
    table.add_column("Rollouts", justify="right")
    table.add_column("Kind", style="dim")
    table.add_column("Symbol")

    for e in entries:
        style = _STATUS_STYLES.get(e.status, "")
        table.add_row(
            str(e.score),
            f"[{style}]{e.status}[/]" if style else e.status,
            str(e.rollout_count),
            e.kind,
            e.symbol,
        )

    Console().print(table)
