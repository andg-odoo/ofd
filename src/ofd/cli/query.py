"""`ofd query` - ad-hoc filter over raw events."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import click

from ofd import config as config_mod
from ofd.config import resolve_workspace
from ofd.events.record import Kind
from ofd.events.store import iter_repo
from ofd.globs import match_any

_DURATION_UNITS = {"h": 3600, "d": 86400, "w": 604800}


def _parse_duration(s: str) -> timedelta:
    unit = s[-1]
    if unit not in _DURATION_UNITS:
        raise click.BadParameter(f"--since must end with h/d/w, got {s!r}")
    try:
        value = int(s[:-1])
    except ValueError as e:
        raise click.BadParameter(f"--since prefix must be an integer, got {s!r}") from e
    return timedelta(seconds=value * _DURATION_UNITS[unit])


def _parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


@click.command("query")
@click.option("--workspace", "workspace_path", default=None)
@click.option("--author", default=None, help="Match against committer email or name.")
@click.option(
    "--kind",
    "kinds",
    multiple=True,
    help="Only include events of this kind (repeatable).",
)
@click.option(
    "--since",
    default=None,
    help="Window (e.g. 24h, 7d, 2w) counted back from now.",
)
@click.option(
    "--path",
    "path_patterns",
    multiple=True,
    help="Only include events whose `file` matches this glob (repeatable).",
)
@click.option(
    "--symbol",
    default=None,
    help="Match against the change's symbol (substring).",
)
@click.option("--as-json", "output_json", is_flag=True, help="Emit JSON array.")
def query(
    workspace_path: str | None,
    author: str | None,
    kinds: tuple[str, ...],
    since: str | None,
    path_patterns: tuple[str, ...],
    symbol: str | None,
    output_json: bool,
):
    """Filter raw events by author / kind / time / path / symbol."""
    workspace = resolve_workspace(workspace_path)
    config = config_mod.load(workspace)

    kind_set: set[Kind] | None = None
    if kinds:
        try:
            kind_set = {Kind(k) for k in kinds}
        except ValueError as e:
            raise click.BadParameter(f"unknown --kind: {e}") from e

    cutoff: datetime | None = None
    if since:
        cutoff = datetime.now(tz=UTC) - _parse_duration(since)

    emitted: list[dict] = []

    for repo in config.repos:
        for cr in iter_repo(workspace, repo.name):
            if cutoff is not None:
                try:
                    if _parse_iso(cr.commit.committed_at) < cutoff:
                        continue
                except ValueError:
                    pass
            if author and author not in cr.commit.author_email and author not in cr.commit.author_name:
                continue
            for change in cr.changes:
                if kind_set and change.kind not in kind_set:
                    continue
                if path_patterns and not match_any(change.file, list(path_patterns)):
                    continue
                if symbol and (not change.symbol or symbol not in change.symbol):
                    continue
                emitted.append({
                    "sha": cr.commit.sha,
                    "repo": cr.commit.repo,
                    "committed_at": cr.commit.committed_at,
                    "author": cr.commit.author_email,
                    "subject": cr.commit.subject,
                    "kind": change.kind.value,
                    "file": change.file,
                    "symbol": change.symbol,
                    "score": change.score,
                })

    if output_json:
        click.echo(json.dumps(emitted, indent=2))
        return

    if not emitted:
        click.echo("no matches", err=True)
        return

    for e in emitted:
        click.echo(
            f"{e['committed_at'][:10]}  {e['score']}  {e['kind']:30s}  "
            f"{e['symbol'] or '-':50s}  {e['sha'][:12]}  {e['subject']}"
        )
