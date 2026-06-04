"""Manifest-key extractor.

A new top-level `__manifest__.py` key is a module-level platform knob:
`countries` scoping l10n modules, new asset buckets, license/category
semantics. The framework half (the loader interpreting the key) lives
in `odoo/modules/**` and is covered by the Python extractor; the key
itself only exists as data in several thousand manifests, so it gets
the wide-scope treatment of context keys / registry categories:

- parse is exact (the manifest is one Python dict literal - `ast`),
  needle-gated by the pipeline on added key-shaped lines;
- a key string present anywhere at the tracking-window floor is
  baseline and never fires;
- the first manifest carrying an unseen key is the NEW_MANIFEST_KEY
  definition; subsequent manifests adding it are extractor-emitted
  ROLLOUTs, like file conventions - the content matcher never scans
  for manifest keys (`_KIND_LANGUAGES` row is empty: key names are
  ordinary words).

Manifests of `test_*` addons are skipped: loader test fixtures carry
intentionally bogus keys.
"""

from __future__ import annotations

import ast
from collections.abc import Container
from pathlib import Path, PurePosixPath

from ofd import gitio
from ofd.events.record import ChangeRecord, Kind


def is_test_module_manifest(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return len(parts) >= 2 and parts[-2].startswith("test")


def manifest_keys(source: str | None) -> dict[str, int] | None:
    """Top-level string keys of a manifest dict -> line number, or None
    when the file doesn't parse to a dict (skip silently)."""
    if not source:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            return {
                k.value: k.lineno
                for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
    return None


def extract(
    files: list[tuple[str, str | None, str | None]],
    known_symbols: Container[str],
    baseline: frozenset[str] | set[str] | None = None,
) -> list[ChangeRecord]:
    """Commit-level manifest-key events.

    `files` holds (path, parent_source, child_source) tuples for this
    commit's changed manifests. Keys are diffed parent->child per file
    so editing a manifest never re-fires its existing keys; a brand-new
    module's manifest diffs against the (almost always baseline) empty
    set.
    """
    base = baseline or frozenset()
    records: list[ChangeRecord] = []
    commit_defs: set[str] = set()
    for file, parent_source, child_source in sorted(files):
        if is_test_module_manifest(file):
            continue
        child = manifest_keys(child_source)
        if child is None:
            continue
        parent = manifest_keys(parent_source) or {}
        for key in sorted(child.keys() - parent.keys()):
            sym = f"manifest.{key}"
            if sym in base:
                continue
            if sym in known_symbols or sym in commit_defs:
                records.append(ChangeRecord(
                    kind=Kind.ROLLOUT,
                    file=file,
                    line=child[key],
                    symbol=sym,
                ))
                continue
            commit_defs.add(sym)
            records.append(ChangeRecord(
                kind=Kind.NEW_MANIFEST_KEY,
                file=file,
                line=child[key],
                symbol=sym,
                signature=f"'{key}': ...",
            ))
    return records


def scan_baseline_keys(mirror: Path, baseline_sha: str) -> set[str]:
    """Manifest-key symbols present anywhere at <baseline_sha>.

    One `cat-file --batch` process streams every manifest blob; exact
    `ast` parse per file, same skip-silently rule as live extraction.
    """
    paths = [
        p for p in gitio.ls_tree(mirror, baseline_sha)
        if p.endswith("__manifest__.py")
    ]
    keys: set[str] = set()
    with gitio.BlobFetcher(mirror) as fetcher:
        for path in paths:
            parsed = manifest_keys(fetcher.fetch(baseline_sha, path))
            if parsed:
                keys.update(parsed)
    return {f"manifest.{k}" for k in keys}
