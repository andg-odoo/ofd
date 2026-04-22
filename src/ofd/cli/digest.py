"""`ofd digest` - render and print the daily digest."""

from __future__ import annotations

import sys
from datetime import date

import click

from ofd import config as config_mod
from ofd import digest as digest_mod
from ofd.config import resolve_workspace


@click.command("digest")
@click.option("--workspace", "workspace_path", default=None)
@click.option("--date", "date_str", default=None, help="YYYY-MM-DD; defaults to today.")
@click.option("--window-days", type=int, default=1, help="Window size (default 1).")
@click.option("--print/--no-print", "do_print", default=True)
@click.option("--raw", is_flag=True, help="Print raw markdown instead of rendered output.")
def digest(
    workspace_path: str | None,
    date_str: str | None,
    window_days: int,
    do_print: bool,
    raw: bool,
):
    """Render the daily digest markdown."""
    workspace = resolve_workspace(workspace_path)
    config = config_mod.load(workspace)
    target = date.fromisoformat(date_str) if date_str else None
    path, content = digest_mod.build_and_render(
        workspace, config, target_date=target, window_days=window_days
    )
    click.echo(f"wrote {path}", err=True)
    if not do_print:
        return

    # Pretty-render the markdown for the terminal; keep the file copy plain.
    if raw or not sys.stdout.isatty():
        click.echo(content)
        return
    from ofd.cli._theme import print_markdown
    print_markdown(content)
