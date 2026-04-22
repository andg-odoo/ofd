"""`ofd show <symbol>` - print a ledger entry to stdout."""

from __future__ import annotations

import sys

import click

from ofd.cli._resolve import resolve_symbol
from ofd.config import resolve_workspace
from ofd.ledger.read import find, iter_entries


@click.command("show")
@click.argument("symbol")
@click.option("--workspace", "workspace_path", default=None)
@click.option("--path", "show_path", is_flag=True, help="Print the file path only.")
@click.option("--raw", is_flag=True, help="Print raw markdown instead of rendered output.")
def show(symbol: str, workspace_path: str | None, show_path: bool, raw: bool):
    """Print the ledger entry for SYMBOL.

    SYMBOL may be the fully-qualified dotted name or just the last
    segment (e.g. `CachedModel`).
    """
    workspace = resolve_workspace(workspace_path)
    entries = iter_entries(workspace)
    resolved = resolve_symbol((e.symbol for e in entries), symbol)
    entry = find(workspace, resolved)
    if entry is None:
        click.echo(f"no ledger entry for {symbol!r}", err=True)
        sys.exit(1)
    if show_path:
        click.echo(entry.path)
        return
    content = entry.path.read_text()
    if raw or not sys.stdout.isatty():
        click.echo(content, nl=False)
        return
    from ofd.cli._theme import print_markdown
    print_markdown(content)
