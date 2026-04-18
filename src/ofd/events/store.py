"""Atomic read/write for raw/<repo>/<sha>.json files."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

from ofd.events.record import CommitRecord


def raw_path(workspace: Path, repo: str, sha: str) -> Path:
    return workspace / "raw" / repo / f"{sha}.json"


def write(workspace: Path, record: CommitRecord) -> Path:
    """Write a CommitRecord atomically. Returns the written path."""
    target = raw_path(workspace, record.commit.repo, record.commit.sha)
    target.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{record.commit.sha}.", suffix=".json", dir=target.parent
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(record.to_dict(), f, indent=2, sort_keys=False)
            f.write("\n")
        os.replace(tmp_path, target)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise
    return target


def read(workspace: Path, repo: str, sha: str) -> CommitRecord:
    with raw_path(workspace, repo, sha).open() as f:
        return CommitRecord.from_dict(json.load(f))


def iter_repo(workspace: Path, repo: str) -> Iterator[CommitRecord]:
    """Yield all CommitRecords for a repo, in filesystem order."""
    base = workspace / "raw" / repo
    if not base.exists():
        return
    for path in sorted(base.glob("*.json")):
        with path.open() as f:
            yield CommitRecord.from_dict(json.load(f))
