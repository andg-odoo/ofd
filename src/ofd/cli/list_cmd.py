"""`ofd list` - one line per ledger entry."""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime

import click

from ofd.config import resolve_workspace
from ofd.ledger.read import LedgerEntry, iter_entries


def _weeks_since(iso_date: str, now: datetime | None = None) -> float:
    """Weeks elapsed since `iso_date` (a YYYY-MM-DD string); 1.0 min."""
    if not iso_date:
        return 1.0
    try:
        d = date.fromisoformat(iso_date)
    except ValueError:
        return 1.0
    now = now or datetime.now(tz=UTC)
    delta = (now.date() - d).days
    return max(delta / 7.0, 1.0)


def _velocity(e: LedgerEntry, now: datetime | None = None) -> float:
    """Rollouts per week since first_seen. New primitives with modest
    rollout counts can out-rank old ones with high counts.
    """
    return e.rollout_count / _weeks_since(e.first_seen, now)


def _recency_boost(e: LedgerEntry, now: datetime | None = None) -> float:
    """Score adjustment for how recently a primitive landed.

    A primitive introduced in the last 90 days has had less time than
    peers to accumulate rollouts - give it a proportional bump so it
    doesn't fall behind dormant-but-old stuff in the score ranking.
    Caps at +3 so it can't swamp the base score entirely.
    """
    weeks = _weeks_since(e.first_seen, now)
    # 1 week old -> +3; 13 weeks (~quarter) -> 0; older -> 0.
    return max(0.0, min(3.0, (13 - weeks) / 4))


def _sort_keys():
    return {
        "score": lambda e: (-e.score, e.symbol),
        "breadth": lambda e: (-e.rollout_count, -e.score, e.symbol),
        "date": lambda e: (e.first_seen, e.symbol),
        "symbol": lambda e: (e.symbol,),
        # rollouts / weeks-since-first-seen, descending
        "velocity": lambda e: (-_velocity(e), e.symbol),
        # base score + recency boost, for release-proximity weighting
        "weighted": lambda e: (-(e.score + _recency_boost(e)), e.symbol),
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
    type=click.Choice(list(_sort_keys().keys())),
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
    """List ledger entries.

    Sort orders:
      score      - pure base score (default).
      weighted   - base score + recency boost (favors 19.4-era primitives
                   that haven't had full adoption time yet).
      velocity   - rollouts per week since first_seen.
      breadth    - raw rollout count.
      date       - first_seen ascending.
      symbol     - alphabetical.
    """
    workspace = resolve_workspace(workspace_path)
    entries = iter_entries(workspace)
    if kind_filter:
        entries = [e for e in entries if e.kind == kind_filter]
    if status_filter:
        entries = [e for e in entries if e.status == status_filter]
    if version_filter:
        entries = [e for e in entries if str(e.frontmatter.get("active_version")) == version_filter]

    entries.sort(key=_sort_keys()[sort])
    if limit is not None:
        entries = entries[:limit]

    if symbol_only:
        for e in entries:
            click.echo(e.symbol)
        return

    # Fall back to plain output when piped, asked, or no entries.
    if plain or not sys.stdout.isatty() or not entries:
        for e in entries:
            click.echo(
                f"{e.score}  {e.status:<20s}  {e.active_version:<8s}  "
                f"{e.rollout_count:>4d}  {e.symbol}"
            )
        return

    from rich.console import Console
    from rich.table import Table

    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("Score", justify="right", style="bold")
    # Optional columns shown only for the sort that relies on them -
    # avoids visual noise when the user isn't asking for velocity/weighted.
    show_velocity = sort == "velocity"
    show_weighted = sort == "weighted"
    if show_weighted:
        table.add_column("+recency", justify="right", style="yellow")
    if show_velocity:
        table.add_column("v/wk", justify="right", style="cyan")
    table.add_column("Status")
    table.add_column("Version", style="green")
    table.add_column("Rollouts", justify="right")
    table.add_column("Kind", style="dim")
    table.add_column("Symbol")

    for e in entries:
        style = _STATUS_STYLES.get(e.status, "")
        row = [str(e.score)]
        if show_weighted:
            row.append(f"+{_recency_boost(e):.1f}")
        if show_velocity:
            row.append(f"{_velocity(e):.2f}")
        row += [
            f"[{style}]{e.status}[/]" if style else e.status,
            e.active_version or "-",
            str(e.rollout_count),
            e.kind,
            e.symbol,
        ]
        table.add_row(*row)

    Console().print(table)
