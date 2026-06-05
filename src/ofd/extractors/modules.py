"""New/removed module extractor.

A new `__manifest__.py` is a new addon - platform news the primitive
extractors structurally can't see. The 2026-06 audit found the class
of miss this closes: paper-muncher (the wkhtmltopdf replacement)
shipped as a new `base_report_paper_muncher` addon, the reporting
engines were modularized into `base_report_wkhtmltox`, POS was
decoupled from stock via a `pos_stock` split - all headline material,
none of it visible as a class, kwarg, or view attribute. A deleted
manifest is the matching removal story.

Adoption surface: another manifest *adding* the tracked module to its
`depends` is the depends-graph growing around it. Like file
conventions and manifest keys, those rollouts are extractor-emitted -
module names are ordinary snake_case words, so the content matcher
never scans for them (`_KIND_LANGUAGES` rows are empty).

Triage rides on scoring, not extraction: every new module fires (the
digest lists all definitions), but manifests declaring `auto_install`
are bridge glue, flagged on the record so `score_event` can dock them
below the surface threshold.
"""

from __future__ import annotations

import ast
from collections.abc import Container
from pathlib import PurePosixPath

from ofd.events.record import ChangeRecord, Kind
from ofd.extractors.manifest_keys import is_test_module_manifest


def module_name(path: str) -> str:
    """`addons/foo/__manifest__.py` -> `foo`."""
    return PurePosixPath(path).parts[-2]


def _parse(source: str | None) -> tuple[dict | None, dict[str, int]]:
    """Manifest source -> (literal data, depends-entry line numbers).

    Values that aren't pure literals (rare f-string/concat manifests)
    are dropped key-by-key; an unparseable file yields (None, {}) and
    the caller skips silently, same as `manifest_keys`.
    """
    if not source:
        return None, {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None, {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        data: dict = {}
        dep_lines: dict[str, int] = {}
        for key, value in zip(node.keys, node.values, strict=True):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            try:
                data[key.value] = ast.literal_eval(value)
            except (ValueError, TypeError):
                continue
            if key.value == "depends" and isinstance(value, (ast.List, ast.Tuple)):
                for elt in value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        dep_lines.setdefault(elt.value, elt.lineno)
        return data, dep_lines
    return None, {}


def _signature(name: str, data: dict) -> str:
    display = data.get("name") or name
    category = data.get("category")
    return f"{display} ({category})" if category else str(display)


def extract(
    files: list[tuple[str, str | None, str | None]],
    known_symbols: Container[str],
) -> list[ChangeRecord]:
    """Commit-level module events.

    `files` holds (path, parent_source, child_source) tuples for this
    commit's changed manifests. A missing parent blob is a new module,
    a missing child blob is a removal; both present means an edit,
    interesting only for `depends` additions of tracked modules.
    Module renames/splits arrive as delete+add pairs (the diff walk
    runs `--no-renames`) and fire both events - a [MOV] split IS both
    stories.
    """
    records: list[ChangeRecord] = []
    commit_defs: set[str] = set()
    for file, parent_source, child_source in sorted(files):
        if is_test_module_manifest(file):
            continue
        name = module_name(file)
        symbol = f"module.{name}"
        child, child_deps = _parse(child_source)
        parent, parent_deps = _parse(parent_source)

        if parent_source is None and child is not None:
            commit_defs.add(symbol)
            records.append(ChangeRecord(
                kind=Kind.NEW_MODULE,
                file=file,
                line=1,
                symbol=symbol,
                signature=_signature(name, child),
                auto_install=True if child.get("auto_install") else None,
            ))
        elif child_source is None and parent is not None:
            records.append(ChangeRecord(
                kind=Kind.REMOVED_MODULE,
                file=file,
                line=1,
                symbol=symbol,
                signature=_signature(name, parent),
            ))

        # Depends additions onto tracked modules (including modules
        # defined earlier in this same commit: a bridge landing next to
        # its target is the target's first adoption).
        if child is None:
            continue
        added = set(child_deps) - set(parent_deps)
        for dep in sorted(added):
            dep_symbol = f"module.{dep}"
            if dep_symbol == symbol:
                continue
            if dep_symbol in commit_defs or dep_symbol in known_symbols:
                records.append(ChangeRecord(
                    kind=Kind.ROLLOUT,
                    file=file,
                    line=child_deps[dep],
                    symbol=dep_symbol,
                ))
    return records
