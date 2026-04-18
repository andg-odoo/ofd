"""Bare partial-mirror lifecycle."""

from __future__ import annotations

from pathlib import Path

from ofd import gitio
from ofd.config import Config


def init(config: Config) -> list[tuple[str, Path]]:
    """Clone any missing mirrors. Returns (repo_name, mirror_path) pairs
    that were newly created."""
    created = []
    for repo in config.repos:
        if repo.mirror.exists():
            continue
        gitio.clone_bare_partial(repo.source, repo.mirror)
        created.append((repo.name, repo.mirror))
    return created


def fetch_all(config: Config) -> None:
    for repo in config.repos:
        gitio.fetch(repo.mirror, repo.branch)


def status(config: Config) -> dict[str, dict]:
    """Summary per repo: existence, disk usage, latest SHA on tracked branch."""
    result = {}
    for repo in config.repos:
        entry = {"mirror": str(repo.mirror), "exists": repo.mirror.exists()}
        if entry["exists"]:
            entry["head"] = gitio.head_sha(repo.mirror, repo.branch)
            entry["size_bytes"] = _du(repo.mirror)
        result[repo.name] = entry
    return result


def _du(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                continue
    return total
