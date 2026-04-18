"""`ofd list` - one line per ledger entry."""

from __future__ import annotations

import click

from ofd.config import resolve_workspace
from ofd.ledger.read import iter_entries

_SORT_KEYS = {
    "score": lambda e: (-e.score, e.symbol),
    "breadth": lambda e: (-e.rollout_count, -e.score, e.symbol),
    "date": lambda e: (e.first_seen, e.symbol),
    "symbol": lambda e: (e.symbol,),
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
@click.option("--symbol-only", is_flag=True, help="Print only the symbol column.")
def list_cmd(
    workspace_path: str | None,
    kind_filter: str | None,
    status_filter: str | None,
    version_filter: str | None,
    sort: str,
    symbol_only: bool,
):
    """List ledger entries (one per line)."""
    workspace = resolve_workspace(workspace_path)
    entries = iter_entries(workspace)
    if kind_filter:
        entries = [e for e in entries if e.kind == kind_filter]
    if status_filter:
        entries = [e for e in entries if e.status == status_filter]
    if version_filter:
        entries = [e for e in entries if str(e.frontmatter.get("active_version")) == version_filter]

    entries.sort(key=_SORT_KEYS[sort])

    for e in entries:
        if symbol_only:
            click.echo(e.symbol)
        else:
            click.echo(
                f"{e.score}  {e.status:<20s}  {e.rollout_count:>4d}  {e.symbol}"
            )
