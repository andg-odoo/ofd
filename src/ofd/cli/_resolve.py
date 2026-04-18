"""Shared symbol-resolution helper for CLI commands.

`ofd show CachedModel` should work, but must fail loudly when the short
name is ambiguous (e.g. two modules both define `Query`) or absent.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable

import click


def resolve_symbol(symbols: Iterable[str], query: str) -> str | None:
    """Resolve `query` against `symbols`.

    Returns:
      - the matched fully-qualified symbol on a unique match
      - None after printing a diagnostic to stderr and exiting non-zero
        when the query is ambiguous or unmatched
    """
    all_symbols = list(symbols)

    if query in all_symbols:
        return query

    last_segment = [s for s in all_symbols if s.rsplit(".", 1)[-1] == query]
    if len(last_segment) == 1:
        return last_segment[0]
    if len(last_segment) > 1:
        click.echo(
            f"ambiguous symbol {query!r}; {len(last_segment)} candidates:",
            err=True,
        )
        for s in sorted(last_segment):
            click.echo(f"  {s}", err=True)
        sys.exit(2)

    ql = query.lower()
    suggestions = sorted(
        s for s in all_symbols
        if ql in s.lower() or ql in s.rsplit(".", 1)[-1].lower()
    )
    click.echo(f"no symbol matching {query!r}", err=True)
    if suggestions:
        click.echo("did you mean:", err=True)
        for s in suggestions[:10]:
            click.echo(f"  {s}", err=True)
        if len(suggestions) > 10:
            click.echo(f"  ... {len(suggestions) - 10} more", err=True)
    sys.exit(1)
