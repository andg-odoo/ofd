"""Shared rich theme + markdown rendering helpers for the CLI.

Rich's default `markdown.code` paints a grey block behind inline
backticks that reads as "selected text" in dark terminals. Override to
foreground-only cyan and give the h1/h2/h3 headers distinct accents so
sections stand out.
"""

from __future__ import annotations


def markdown_theme():
    from rich.theme import Theme
    return Theme({
        "markdown.code": "cyan",
        "markdown.code_block": "cyan",
        "markdown.h1": "bold",
        "markdown.h2": "bold magenta",
        "markdown.h3": "bold yellow",
    })


def print_markdown(content: str, stderr: bool = False) -> None:
    """Render markdown to the terminal using the shared theme."""
    from rich.console import Console
    from rich.markdown import Markdown
    Console(theme=markdown_theme(), stderr=stderr).print(Markdown(content))
