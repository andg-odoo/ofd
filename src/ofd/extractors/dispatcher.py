"""Dispatch changed files to the right language handler."""

from __future__ import annotations

from pathlib import PurePosixPath

from ofd.events.record import ChangeRecord
from ofd.extractors import python_, rng


def extract_for_file(
    parent_source: str | None,
    child_source: str | None,
    file: str,
) -> list[ChangeRecord]:
    ext = PurePosixPath(file).suffix.lower()
    if ext == ".py":
        try:
            return python_.extract(parent_source, child_source, file)
        except SyntaxError:
            return []
    if ext == ".rng":
        return rng.extract(parent_source, child_source, file)
    # XML / JS handlers will plug in here.
    return []
