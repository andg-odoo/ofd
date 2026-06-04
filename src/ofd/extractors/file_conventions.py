"""File-convention detector.

Flags the emergence of a new data-file naming convention: a
previously-unseen basename added under module `security/` or `data/`
directories across several modules in one commit (e.g.
`security/ir.access.csv` superseding `ir.model.access.csv` +
`ir_rules.xml` when ACLs and record rules merged into one model).

Why file *paths* and not file contents:

- The framework-level half of such a change (new model, new loader) is
  Python and already covered by the AST extractor. The convention half
  - the new file format every module must adopt - exists only as a
  naming pattern on disk. There is no AST to walk.

- Parsing security CSV / data XML content would be nearly all noise:
  ACL rows and data records change routinely every day. The structural
  anchor here is the basename itself. N modules adding the same
  never-before-seen data filename in a single commit is, by
  definition, a new convention - no content heuristics required.

- Baseline suppression mirrors the context-keys extractor: every
  basename present at the tracking-window floor is pre-known, so a new
  module shipping a routine `ir.model.access.csv` never fires.

Gradual adoptions (a new basename reaching one module per commit) stay
below `_MIN_MODULES` and are dropped silently; the mass-migration shape
is the one worth a slide. Revisit if the bench surfaces a slow-burn
convention worth catching.
"""

from __future__ import annotations

import re
from collections.abc import Container, Iterable

from ofd.events.record import ChangeRecord, Kind

# `<module>/security/<basename>` or `<module>/data/<basename>`, where
# the basename sits directly under the bucket (nested template dirs are
# module-internal organization, not a convention) and carries a data
# extension. The module prefix may itself be nested (`addons/website`,
# `odoo/addons/base`).
_CANDIDATE = re.compile(
    r"^(?P<module>.+)/(?P<bucket>security|data)/(?P<basename>[^/]+\.(?:csv|xml))$"
)

# Distinct modules that must adopt a new basename within one commit
# before it counts as a convention. 2 is a copy-paste; 3 is a pattern.
_MIN_MODULES = 3


def candidate(path: str) -> tuple[str, str] | None:
    """Return (module, basename) when `path` is a data file directly
    under a module's security/ or data/ directory, else None."""
    m = _CANDIDATE.match(path)
    if not m:
        return None
    return m.group("module"), m.group("basename")


def baseline_basenames(paths: Iterable[str]) -> frozenset[str]:
    """Collect every candidate basename present in a full tree listing."""
    out: set[str] = set()
    for p in paths:
        c = candidate(p)
        if c:
            out.add(c[1])
    return frozenset(out)


def extract(
    added: list[tuple[str, str, str]],
    known_symbols: Container[str],
    min_modules: int = _MIN_MODULES,
) -> list[ChangeRecord]:
    """Turn this commit's added candidate files into events.

    `added` holds (path, module, basename) triples, already filtered to
    *added* files whose basename is absent from the baseline. A
    basename already on the watchlist yields ROLLOUT records (later
    modules adopting the convention); an unseen basename reaching
    `min_modules` distinct modules yields one NEW_FILE_CONVENTION
    definition plus a ROLLOUT per extra module.
    """
    records: list[ChangeRecord] = []
    by_basename: dict[str, dict[str, str]] = {}
    for path, module, basename in added:
        by_basename.setdefault(basename, {}).setdefault(module, path)
    for basename, modules in sorted(by_basename.items()):
        paths = sorted(modules.values())
        if basename in known_symbols:
            records.extend(
                ChangeRecord(kind=Kind.ROLLOUT, file=p, line=0, symbol=basename)
                for p in paths
            )
            continue
        if len(modules) < min_modules:
            continue
        first, *rest = paths
        records.append(ChangeRecord(
            kind=Kind.NEW_FILE_CONVENTION,
            file=first,
            line=0,
            symbol=basename,
            symbol_hint=f"adopted by {len(modules)} modules in one commit",
        ))
        records.extend(
            ChangeRecord(kind=Kind.ROLLOUT, file=p, line=0, symbol=basename)
            for p in rest
        )
    return records
