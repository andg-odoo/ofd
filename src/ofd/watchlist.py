"""In-memory watchlist of primitives introduced in framework paths.

Populated during ingestion from definition events. Persisted between
runs to `<workspace>/watchlist.json`.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ofd.events.record import DEFINITION_KINDS, ChangeRecord, Kind

# Kinds whose rollouts must be scoped to a parent XML element. Carrying
# `element` past the watchlist turns the quoted-string regex hit on
# `"invisible"` into a proper `<widget ... invisible=...>` match.
_ELEMENT_SCOPED_KINDS = frozenset({
    Kind.NEW_VIEW_ATTRIBUTE,
    Kind.NEW_VIEW_DIRECTIVE,
})

# Kinds where a definition sharing (kind, parent segment, short name)
# with an existing entry is an *override* of the same primitive, not a
# new one. When base introduced the typed config-param API, mail's
# `_inherit` override of `set_str` became a second watchlist entry -
# and since `addons.mail...` sorts before `odoo.addons.base...`, the
# shared-short-name matcher attributed all 189 set_str adoptions to
# the override. Fold overrides onto the first-seen entry instead.
_OVERRIDE_FOLD_KINDS = frozenset({
    Kind.NEW_DECORATOR_OR_HELPER,
    Kind.NEW_CLASS_ATTRIBUTE,
    Kind.NEW_PUBLIC_CLASS,
})


@dataclass
class WatchlistEntry:
    symbol: str                 # fully-qualified, e.g. odoo.orm.models_cached.CachedModel
    short_name: str             # last segment, used for rollout matching: CachedModel
    kind: Kind                  # definition kind that introduced it
    repo: str
    file: str                   # file where it was introduced
    first_seen_sha: str
    first_seen_at: str          # ISO-8601
    active_version: str
    # "extracted" when the pipeline found a definition automatically;
    # "manual" for entries pinned via `ofd watchlist add`. Manual entries
    # are preserved across reindex since their definition isn't
    # discoverable from any gated path (magic strings, context keys,
    # registry entries).
    source: str = "extracted"
    note: str | None = None     # free-form user note (manual entries only)
    # Parent XML element for RNG-derived primitives. Scopes rollout
    # matching so a `widget.invisible` entry only matches
    # `<widget ... invisible=...>`, not any `<field invisible=...>`.
    element: str | None = None
    # User annotation: structurally restrict rollout matches to those
    # whose XML element has at least one ancestor tag in this list.
    # Set via `ofd watchlist annotate <symbol> --ancestor list,tree`.
    # Used to disambiguate cases the RNG schema can't express - e.g.
    # `<widget invisible=>` is only a real adoption of the new
    # "list-view widget invisibility" feature when the widget sits
    # under a <list>/<tree>; under <form>/<sheet> it's pre-existing.
    # None / empty means no structural restriction.
    required_ancestor: list[str] | None = None


@dataclass
class Watchlist:
    entries: dict[str, WatchlistEntry] = field(default_factory=dict)

    def add_from_definition(
        self,
        record: ChangeRecord,
        repo: str,
        sha: str,
        committed_at: str,
        active_version: str,
    ) -> WatchlistEntry | None:
        if record.kind not in DEFINITION_KINDS:
            return None
        if not record.symbol:
            return None
        if record.symbol in self.entries:
            return self.entries[record.symbol]
        # Override folding: `addons.mail...IrConfig_Parameter.set_str`
        # is the same primitive as the already-tracked
        # `odoo.addons.base...IrConfig_Parameter.set_str` (an
        # `_inherit` override), not a new API. The (kind, class/file
        # segment, short name) tail identifies it.
        if record.kind in _OVERRIDE_FOLD_KINDS:
            parts = record.symbol.split(".")
            if len(parts) >= 2:
                tail = (record.kind, parts[-2], parts[-1])
                for existing in self.entries.values():
                    ep = existing.symbol.split(".")
                    if (
                        len(ep) >= 2
                        and (existing.kind, ep[-2], ep[-1]) == tail
                    ):
                        return existing
        short = record.symbol.rsplit(".", 1)[-1]
        # Directive symbols look like `<file>.<parent>+<child>` so the
        # naive last-segment short name comes out as `parent+child` -
        # an opaque literal the matcher can't usefully scan for. For
        # element/ref directives we prefer `record.directive`, the bare
        # tag name, so the rollout matcher can search for `<child\b` in
        # XML diffs. Shape directives (record.symbol ends in `+shape`)
        # carry a structural fingerprint, not a tag name; leave their
        # short alone so they stay effectively unmatchable rather than
        # flooding the Aho-Corasick automaton with `shape`.
        if (
            record.kind is Kind.NEW_VIEW_DIRECTIVE
            and record.directive
            and not record.symbol.endswith("+shape")
        ):
            short = record.directive
        # Convention symbols are dotted basenames (`ir.access.csv`), so
        # the naive last segment is the bare extension - a short name
        # that would match everywhere if its kind ever gained a rollout
        # language. The basename IS the primitive; keep it whole.
        if record.kind is Kind.NEW_FILE_CONVENTION:
            short = record.symbol
        element = record.element if record.kind in _ELEMENT_SCOPED_KINDS else None
        entry = WatchlistEntry(
            symbol=record.symbol,
            short_name=short,
            kind=record.kind,
            repo=repo,
            file=record.file,
            first_seen_sha=sha,
            first_seen_at=committed_at,
            active_version=active_version,
            element=element,
        )
        self.entries[record.symbol] = entry
        return entry

    def add_manual(
        self,
        symbol: str,
        active_version: str,
        note: str | None = None,
        short_name: str | None = None,
        kind: Kind = Kind.NEW_DECORATOR_OR_HELPER,
    ) -> WatchlistEntry:
        """Pin a symbol for rollout tracking without requiring an extractor hit.

        Use for context keys (`'formatted_display_name'`), registry names,
        magic strings - anything whose definition isn't reachable via the
        Python/RNG extractors but whose adoption pattern (string literal,
        attribute access) is still catchable by the rollout matcher.
        """
        short = short_name or symbol.rsplit(".", 1)[-1]
        entry = WatchlistEntry(
            symbol=symbol,
            short_name=short,
            kind=kind,
            repo="(manual)",
            file="(manual)",
            first_seen_sha="(manual)",
            first_seen_at="(manual)",
            active_version=active_version,
            source="manual",
            note=note,
        )
        self.entries[symbol] = entry
        return entry

    def manual_entries(self) -> list[WatchlistEntry]:
        return [e for e in self.entries.values() if e.source == "manual"]

    def annotated_entries(self) -> list[WatchlistEntry]:
        """Entries carrying user-supplied annotations (e.g. required_ancestor).
        Reindex carries these forward alongside manual pins so the user's
        intent isn't wiped by a watchlist rebuild."""
        return [e for e in self.entries.values() if e.required_ancestor]

    def short_names(self) -> set[str]:
        return {e.short_name for e in self.entries.values()}

    def lookup_by_short(self, short: str) -> list[WatchlistEntry]:
        return [e for e in self.entries.values() if e.short_name == short]

    def remove(self, symbol: str) -> bool:
        return self.entries.pop(symbol, None) is not None

    def to_dict(self) -> dict:
        return {"entries": {s: _entry_to_dict(e) for s, e in self.entries.items()}}

    @classmethod
    def from_dict(cls, data: dict) -> Watchlist:
        raw_entries = (data or {}).get("entries") or {}
        entries = {
            s: WatchlistEntry(
                symbol=v["symbol"],
                short_name=v["short_name"],
                kind=Kind(v["kind"]),
                repo=v["repo"],
                file=v["file"],
                first_seen_sha=v["first_seen_sha"],
                first_seen_at=v["first_seen_at"],
                active_version=v["active_version"],
                source=v.get("source", "extracted"),
                note=v.get("note"),
                element=v.get("element"),
                required_ancestor=v.get("required_ancestor") or None,
            )
            for s, v in raw_entries.items()
        }
        return cls(entries=entries)


def _entry_to_dict(e: WatchlistEntry) -> dict:
    d = asdict(e)
    d["kind"] = e.kind.value
    return d


def path_for(workspace: Path) -> Path:
    return workspace / "watchlist.json"


def load(workspace: Path) -> Watchlist:
    p = path_for(workspace)
    if not p.exists():
        return Watchlist()
    with p.open() as f:
        return Watchlist.from_dict(json.load(f))


def save(watchlist: Watchlist, workspace: Path) -> Path:
    p = path_for(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".watchlist.", suffix=".json", dir=p.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(watchlist.to_dict(), f, indent=2)
            f.write("\n")
        os.replace(tmp, p)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return p
