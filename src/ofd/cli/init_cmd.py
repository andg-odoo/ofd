"""`ofd init` - bootstrap a workspace."""

from __future__ import annotations

import sys

import click

from ofd.config import DEFAULT_CONFIG_YAML, resolve_workspace


@click.command("init")
@click.option("--workspace", "workspace_path", default=None, help="Workspace directory.")
@click.option("--force", is_flag=True, help="Overwrite an existing config.yaml.")
def init(workspace_path: str | None, force: bool):
    """Create a new workspace skeleton (config.yaml, raw/, ledger/, digests/)."""
    workspace = resolve_workspace(workspace_path)
    workspace.mkdir(parents=True, exist_ok=True)

    config_path = workspace / "config.yaml"
    if config_path.exists() and not force:
        click.echo(f"config already exists at {config_path}; use --force to overwrite", err=True)
        sys.exit(1)
    config_path.write_text(DEFAULT_CONFIG_YAML)

    for sub in ("raw", "ledger", "ledger/new-apis", "ledger/deprecations", "ledger/refactors", "digests"):
        (workspace / sub).mkdir(parents=True, exist_ok=True)

    click.echo(f"initialized workspace at {workspace}")
    click.echo(f"edit {config_path} to adjust repos, key-devs, active version")
