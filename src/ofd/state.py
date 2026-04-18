"""Per-repo last-seen SHA state, persisted between runs."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RepoState:
    last_seen_sha: str | None = None
    last_run_at: str | None = None


@dataclass
class State:
    repos: dict[str, RepoState] = field(default_factory=dict)
    schema_version: int = 1

    def get(self, repo: str) -> RepoState:
        return self.repos.setdefault(repo, RepoState())

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "repos": {
                name: {"last_seen_sha": r.last_seen_sha, "last_run_at": r.last_run_at}
                for name, r in self.repos.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> State:
        repos = {
            name: RepoState(
                last_seen_sha=r.get("last_seen_sha"),
                last_run_at=r.get("last_run_at"),
            )
            for name, r in (data.get("repos") or {}).items()
        }
        return cls(repos=repos, schema_version=data.get("schema_version", 1))


def default_path() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "ofd" / "state.json"


def load(path: Path | None = None) -> State:
    path = path or default_path()
    if not path.exists():
        return State()
    with path.open() as f:
        return State.from_dict(json.load(f))


def save(state: State, path: Path | None = None) -> Path:
    path = path or default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".state.", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state.to_dict(), f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path
