"""Shared rich theme + markdown rendering helpers for the CLI.

Rich's default `markdown.code` paints a grey block behind inline
backticks that reads as "selected text" in dark terminals. Override to
foreground-only cyan and give the h1/h2/h3 headers distinct accents so
sections stand out.
"""

from __future__ import annotations

import os
import subprocess
import sys


def markdown_theme():
    from rich.theme import Theme
    return Theme({
        "markdown.code": "cyan",
        "markdown.code_block": "cyan",
        "markdown.h1": "bold",
        "markdown.h2": "bold magenta",
        "markdown.h3": "bold yellow",
    })


def print_markdown(
    content: str,
    stderr: bool = False,
    paginate: bool = False,
) -> None:
    """Render markdown to the terminal using the shared theme.

    When `paginate` is set, pipe the rendered ANSI output through
    `less -RF` so long ledger entries open in less's alt-screen
    (auto-exit on short content via `-F`, ANSI preserved via `-R`).
    Why this matters: under tmux with `mouse on`, scrolling up in the
    main screen buffer enters copy-mode and tmux captures mouse events
    - which masks the terminal emulator's Ctrl+click hyperlink handler.
    less's alt-screen sidesteps copy-mode entirely (tmux's scroll-into-
    copy-mode rule only fires on the main buffer), and less itself
    doesn't grab the mouse, so Ctrl+click on rendered links keeps
    working inside the pager.
    """
    from rich.console import Console
    from rich.markdown import Markdown
    console = Console(theme=markdown_theme(), stderr=stderr)
    md = Markdown(content)
    if not paginate:
        console.print(md)
        return
    with console.capture() as cap:
        console.print(md)
    rendered = cap.get()
    _pipe_through_less(rendered)


def _pipe_through_less(rendered: str) -> None:
    """Pipe pre-rendered ANSI through the user's PAGER (default `less`).

    `LESS=FRX` matches what git uses by default: `-F` quits if the
    content fits one screen (so short entries print inline, no pager
    UI), `-R` lets ANSI color through, `-X` skips the terminal init
    that would otherwise clear short-content output. If PAGER spawn
    fails for any reason, fall back to a plain stdout write so we
    never lose the output.
    """
    pager_cmd = os.environ.get("PAGER") or "less"
    env = {**os.environ}
    env.setdefault("LESS", "FRX")
    try:
        proc = subprocess.Popen(
            pager_cmd, shell=True, stdin=subprocess.PIPE, env=env, text=True,
        )
    except OSError:
        sys.stdout.write(rendered)
        return
    try:
        assert proc.stdin is not None
        proc.stdin.write(rendered)
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            proc.wait()
