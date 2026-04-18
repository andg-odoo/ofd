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
        short = record.symbol.rsplit(".", 1)[-1]
        entry = WatchlistEntry(
            symbol=record.symbol,
            short_name=short,
            kind=record.kind,
            repo=repo,
            file=record.file,
            first_seen_sha=sha,
            first_seen_at=committed_at,
            active_version=active_version,
        )
        self.entries[record.symbol] = entry
        return entry

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
