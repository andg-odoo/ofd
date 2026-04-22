"""Read-side helpers for the ledger directory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ofd.ledger import frontmatter as fm

_DIRS = ("new-apis", "deprecations", "refactors")


@dataclass
class LedgerEntry:
    symbol: str
    path: Path
    frontmatter: dict
    body: str

    @property
    def kind(self) -> str:
        return str(self.frontmatter.get("kind", "?"))

    @property
    def status(self) -> str:
        return str(self.frontmatter.get("status", "?"))

    @property
    def score(self) -> int:
        try:
            return int(self.frontmatter.get("score") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def rollout_count(self) -> int:
        try:
            return int(self.frontmatter.get("rollout_count") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def first_seen(self) -> str:
        return str(self.frontmatter.get("first_seen") or "")

    @property
    def active_version(self) -> str:
        return str(self.frontmatter.get("active_version") or "")


def iter_entries(workspace: Path) -> list[LedgerEntry]:
    entries: list[LedgerEntry] = []
    ledger_dir = workspace / "ledger"
    if not ledger_dir.exists():
        return entries
    for sub in _DIRS:
        d = ledger_dir / sub
        if not d.exists():
            continue
        for path in sorted(d.glob("*.md")):
            try:
                data, body = fm.split(path.read_text())
            except OSError:
                continue
            symbol = str(data.get("symbol") or path.stem)
            entries.append(LedgerEntry(
                symbol=symbol, path=path, frontmatter=data, body=body,
            ))
    return entries


def find(workspace: Path, symbol: str) -> LedgerEntry | None:
    # Exact symbol match first; fall back to suffix match on the last
    # dotted segment so `ofd show CachedModel` works.
    entries = iter_entries(workspace)
    for e in entries:
        if e.symbol == symbol:
            return e
    for e in entries:
        if e.symbol.rsplit(".", 1)[-1] == symbol:
            return e
    return None
