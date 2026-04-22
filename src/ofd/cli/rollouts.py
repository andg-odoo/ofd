"""`ofd rollouts SYMBOL` - inspect rollout hunks."""

from __future__ import annotations

import sys
from pathlib import PurePosixPath

import click

from ofd import config as config_mod
from ofd.aggregate import build_primitives
from ofd.cli._resolve import resolve_symbol
from ofd.config import resolve_workspace


@click.command("rollouts")
@click.argument("symbol")
@click.option("--workspace", "workspace_path", default=None)
@click.option("--limit", type=int, default=10, help="Max hunks to show.")
@click.option("--diff/--no-diff", "show_diff", default=False, help="Include before/after snippets.")
@click.option("--plain", is_flag=True, help="Disable syntax highlighting / colors.")
def rollouts(
    symbol: str,
    workspace_path: str | None,
    limit: int,
    show_diff: bool,
    plain: bool,
):
    """List rollouts for SYMBOL; with --diff, include before/after hunks."""
    workspace = resolve_workspace(workspace_path)
    config = config_mod.load(workspace)
    prims = build_primitives(workspace, [r.name for r in config.repos])
    resolved = resolve_symbol(prims.keys(), symbol)
    prim = prims[resolved]

    if not prim.rollouts:
        click.echo("no rollouts recorded yet")
        return

    use_rich = not plain and sys.stdout.isatty()
    shown = prim.rollouts[:limit]
    if use_rich:
        _render_rich(shown, show_diff)
    else:
        _render_plain(shown, show_diff)

    if len(prim.rollouts) > limit:
        click.echo(f"... {len(prim.rollouts) - limit} more")


def _render_plain(rollouts, show_diff):
    for i, r in enumerate(rollouts):
        click.echo(
            f"--- {i+1}. {r.file} @ {r.commit.sha[:12]} ({r.commit.committed_at[:10]}) ---"
        )
        if r.model:
            click.echo(f"model: {r.model}")
        if not show_diff:
            continue
        lang = _lang_for(r.file)
        click.echo()
        click.echo(f"Before:\n```{lang}")
        click.echo(r.before_snippet or "(none)")
        click.echo("```")
        click.echo(f"After:\n```{lang}")
        click.echo(r.after_snippet or "(none)")
        click.echo("```")
        click.echo()


def _render_rich(rollouts, show_diff):
    from rich.console import Console
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.text import Text

    console = Console()
    for i, r in enumerate(rollouts):
        header = Text()
        header.append(f"{i+1}. ", style="dim")
        header.append(r.file, style="bold cyan")
        header.append(f"  @ {r.commit.sha[:12]}", style="yellow")
        header.append(f"  ({r.commit.committed_at[:10]})", style="dim")
        if r.model:
            header.append("  model=", style="dim")
            header.append(r.model, style="green")
        header.append(f"\n{r.commit.subject}", style="dim italic")
        console.print(header)

        if show_diff:
            lang = _lang_for(r.file)
            before = r.before_snippet or ""
            after = r.after_snippet or ""
            if before:
                console.print(Panel(
                    Syntax(before, lang or "text", theme="ansi_dark", word_wrap=True, background_color="default"),
                    title="before", title_align="left", border_style="red", padding=(0, 1),
                ))
            console.print(Panel(
                Syntax(after, lang or "text", theme="ansi_dark", word_wrap=True, background_color="default"),
                title="after", title_align="left", border_style="green", padding=(0, 1),
            ))
        console.print()


def _lang_for(file: str) -> str:
    ext = PurePosixPath(file).suffix.lower()
    return {".py": "python", ".xml": "xml", ".rng": "xml", ".js": "javascript"}.get(ext, "")
