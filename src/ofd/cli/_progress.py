"""Rich progress bar for pipeline.run, shared by `ofd run` and `ofd reindex`.

Wraps `pipeline.run` with a per-repo task; falls back gracefully when
stderr isn't a TTY (no bars, plain summary).
"""

from __future__ import annotations

import sys

from ofd.pipeline import run as run_pipeline


def run_pipeline_with_progress(config, state, watchlist):
    """Call `pipeline.run` with a rich progress bar attached.

    Always safe: if stderr isn't a TTY, or the user has NO_COLOR/--quiet
    upstream, the caller should simply bypass this helper. This function
    unconditionally builds the progress UI.
    """
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )

    console = Console(stderr=True)
    tasks: dict[str, int] = {}

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.fields[repo]}"),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TextColumn("[dim]{task.fields[sha]}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
        # rich default is 10 Hz; at that rate the live refresh consumed
        # ~14.6% of reindex wall time in a py-spy profile. 4 Hz is still
        # a smooth user experience and roughly halves the render cost.
        refresh_per_second=4,
    ) as progress:

        def cb(repo_name: str, sha: str, processed: int, total: int) -> None:
            if repo_name not in tasks:
                tasks[repo_name] = progress.add_task(
                    "", total=total, repo=repo_name, sha=sha[:10],
                )
            progress.update(tasks[repo_name], completed=processed, sha=sha[:10])

        return run_pipeline(config, state, watchlist, progress_cb=cb)


def want_progress(quiet: bool = False, explicit_disable: bool = False) -> bool:
    """True when a progress bar should render on stderr."""
    return not quiet and not explicit_disable and sys.stderr.isatty()
