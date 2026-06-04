"""External-dependency epochs from requirements.txt.

A new entry in the repo-root `requirements.txt` is platform news on the
scale of a vendored-lib major bump - a new PDF engine, a new crypto
library - and warrants one loud ledger entry (DEPENDENCY_CHANGE, base
4, clears the ledger threshold alone). Removals are the matching
deprecation story. Version pin changes and per-python-version marker
churn stay silent: package *names* are the signal.

No adoption surface: imports of the new package are ordinary Python
the existing extractors and matcher already judge on their own merits,
so `_KIND_LANGUAGES` carries an empty set.
"""

from __future__ import annotations

import re

from ofd.events.record import ChangeRecord, Kind

# `name==1.2 ; python_version < '3.11'` / `name[extra]>=2` -> `name`.
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _packages(source: str) -> dict[str, tuple[str, int]]:
    """Package name (lowercased) -> (display line, first line number).

    Marker-duplicated entries (one line per python_version range)
    collapse onto the first occurrence.
    """
    out: dict[str, tuple[str, int]] = {}
    for lineno, raw in enumerate(source.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue  # comments, pip options (-r, --hash)
        m = _REQ_NAME.match(line)
        if m:
            out.setdefault(m.group(1).lower(), (line, lineno))
    return out


def extract(
    parent_source: str | None,
    child_source: str | None,
    file: str,
) -> list[ChangeRecord]:
    """Added/removed package names for one requirements.txt transition.

    Both sides must exist: a freshly-created file mid-window has no
    "before" (and the floor-era file predates the walk), mirroring the
    vendored-lib sniff.
    """
    if not parent_source or not child_source:
        return []
    parent = _packages(parent_source)
    child = _packages(child_source)

    records: list[ChangeRecord] = []
    for name in sorted(child.keys() - parent.keys()):
        line_text, lineno = child[name]
        records.append(ChangeRecord(
            kind=Kind.DEPENDENCY_CHANGE,
            file=file,
            line=lineno,
            symbol=f"requirements.{name}",
            symbol_hint="added",
            signature=line_text,
        ))
    for name in sorted(parent.keys() - child.keys()):
        line_text, _ = parent[name]
        records.append(ChangeRecord(
            kind=Kind.DEPENDENCY_CHANGE,
            file=file,
            line=0,
            symbol=f"requirements.{name}",
            symbol_hint="removed",
            signature=line_text,
        ))
    return records
