"""`ofd mirror ...` - manage the bare partial clones."""

from __future__ import annotations

import shutil
import sys

import click

from ofd import config as config_mod
from ofd import gitio
from ofd import mirrors as mirrors_mod
from ofd.config import resolve_workspace


@click.group("mirror")
def mirror():
    """Bare partial-clone mirror lifecycle."""


@mirror.command("init")
@click.option("--workspace", "workspace_path", default=None)
def init_cmd(workspace_path: str | None):
    """Create any missing mirrors (idempotent)."""
    workspace = resolve_workspace(workspace_path)
    config = config_mod.load(workspace)
    try:
        created = mirrors_mod.init(config)
    except gitio.GitError as e:
        click.echo(f"git clone failed: {e}", err=True)
        sys.exit(1)
    if not created:
        click.echo("all mirrors already present")
        return
    for name, path in created:
        click.echo(f"cloned {name} → {path}")


@mirror.command("fetch")
@click.option("--workspace", "workspace_path", default=None)
def fetch_cmd(workspace_path: str | None):
    """Fetch each configured repo's tracked branch."""
    workspace = resolve_workspace(workspace_path)
    config = config_mod.load(workspace)
    failures: list[str] = []
    for repo in config.repos:
        try:
            gitio.fetch(repo.mirror, repo.branch)
            click.echo(f"fetched {repo.name} ({repo.branch})")
        except gitio.GitError as e:
            failures.append(f"{repo.name}: {e}")
    for f in failures:
        click.echo(f, err=True)
    if failures:
        sys.exit(1)


@mirror.command("status")
@click.option("--workspace", "workspace_path", default=None)
def status_cmd(workspace_path: str | None):
    """Print mirror presence, disk usage, and latest SHA on tracked branch."""
    workspace = resolve_workspace(workspace_path)
    config = config_mod.load(workspace)
    data = mirrors_mod.status(config)
    for name, entry in data.items():
        if not entry["exists"]:
            click.echo(f"{name}: missing at {entry['mirror']}")
            continue
        size_mib = entry.get("size_bytes", 0) / (1024 * 1024)
        head = entry.get("head", "?")[:12]
        click.echo(f"{name}: head={head} size={size_mib:.1f} MiB path={entry['mirror']}")


@mirror.command("reset")
@click.argument("repo_name")
@click.option("--workspace", "workspace_path", default=None)
@click.option("--yes", is_flag=True, help="Skip confirmation.")
def reset_cmd(repo_name: str, workspace_path: str | None, yes: bool):
    """Delete and re-clone a single repo's mirror."""
    workspace = resolve_workspace(workspace_path)
    config = config_mod.load(workspace)
    try:
        repo = config.repo(repo_name)
    except KeyError:
        click.echo(f"unknown repo: {repo_name}", err=True)
        sys.exit(1)

    if not yes:
        click.confirm(
            f"delete {repo.mirror} and re-clone from {repo.source}?",
            abort=True,
        )
    if repo.mirror.exists():
        shutil.rmtree(repo.mirror)
    try:
        gitio.clone_bare_partial(repo.source, repo.mirror)
    except gitio.GitError as e:
        click.echo(f"clone failed: {e}", err=True)
        sys.exit(1)
    click.echo(f"re-cloned {repo.name} → {repo.mirror}")
