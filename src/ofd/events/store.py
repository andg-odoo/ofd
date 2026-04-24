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


def prune_orphan_rollouts(
    workspace: Path, repo: str, live_symbols: set[str],
) -> tuple[int, int]:
    """Drop ROLLOUT events whose symbol isn't in `live_symbols`.

    Runs in-place on every raw file for the repo. If a raw still has
    non-rollout events (definitions, deprecations) or rollouts for
    live symbols, it's rewritten; if it ends up empty, the file is
    deleted. Returns `(rewritten_count, deleted_count)`.

    Use this after a watchlist rebuild shrinks the set of tracked
    symbols - the alternative is a full reindex that re-runs the
    rollout regex over every in-window commit.
    """
    base = workspace / "raw" / repo
    if not base.exists():
        return (0, 0)
    rewritten = 0
    deleted = 0
    for path in base.glob("*.json"):
        try:
            with path.open() as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        original = data.get("changes") or []
        filtered = [
            c for c in original
            if c.get("kind") != "rollout"
            or (c.get("symbol") in live_symbols)
        ]
        if len(filtered) == len(original):
            continue
        if not filtered:
            path.unlink(missing_ok=True)
            deleted += 1
            continue
        data["changes"] = filtered
        # Atomic rewrite mirroring `write()`.
        fd, tmp = tempfile.mkstemp(
            prefix=f".{path.stem}.", suffix=".json", dir=path.parent,
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, sort_keys=False)
                f.write("\n")
            os.replace(tmp, path)
            rewritten += 1
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise
    return (rewritten, deleted)


def prune_before(workspace: Path, repo: str, since_date: str) -> int:
    """Delete raws whose `commit.committed_at` falls before `since_date`.

    Returns the number of files deleted. `since_date` is an ISO date
    like "2025-09-01"; `committed_at` is a full ISO-8601 timestamp and
    the YYYY-MM-DD prefix sorts lexicographically the same way.

    Skips malformed files silently so a bad raw doesn't break the whole
    prune pass - reindex will overwrite them anyway.
    """
    base = workspace / "raw" / repo
    if not base.exists():
        return 0
    deleted = 0
    for path in base.glob("*.json"):
        try:
            with path.open() as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        committed_at = (data.get("commit") or {}).get("committed_at") or ""
        if committed_at[:10] < since_date:
            path.unlink(missing_ok=True)
            deleted += 1
    return deleted
