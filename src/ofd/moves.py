"""Move detection: flag a "new" symbol that is really a relocation.

A helper or class that Odoo lifts from one file into another shows up to
the forward-looking extractors as a brand-new definition - and, because
every caller updates its import in the same PR, it also collects a burst
of rollouts. The result is a top-of-the-feed entry that reads as a fresh
capability when nothing new was actually built. The anchor case is
`odoo.tools.business_data.split_vat`: a generic `_split_vat` method moved
out of `res_partner` into a new `tools/business_data.py`, 68 rollouts,
zero dev-facing novelty.

Detection is scoped to a single commit and uses two signals:

  1. Removal pairing - the new symbol's short name matches a
     REMOVED_PUBLIC_SYMBOL / REMOVED_JS_EXPORT in the same commit. This
     catches public->public relocations the extractors can pair up.

  2. Commit-message framing - the message uses move language ("[MOV]",
     "move", "relocate", "extract") on the same line that names the
     symbol. This catches moves signal 1 can't see: the old name was
     private (`_split_vat`, skipped by the public-symbol extractor) or
     lived outside the gated framework paths (so no removal event was
     ever emitted). The line-level co-occurrence keeps an ordinary
     [IMP]/[ADD] commit that merely mentions "move" in passing from
     tripping every symbol it adds.

Detection annotates the record (`moved`, `moved_from`) in place; scoring
demotes it and the ledger records the flag so the entry never presents as
new. Attribution is by these explicit signals, never a global guess.
"""

from __future__ import annotations

import re

from ofd.events.record import ChangeRecord, CommitEnvelope, Kind

# Definition kinds that can be a relocation of existing code. A new
# module, manifest key or dependency epoch is genuinely new surface even
# when a commit says "move", so they're excluded.
_MOVABLE_KINDS: frozenset[Kind] = frozenset({
    Kind.NEW_PUBLIC_CLASS,
    Kind.NEW_DECORATOR_OR_HELPER,
    Kind.NEW_JS_EXPORT,
})

# Removal kinds whose short names can pair with a same-commit addition.
_REMOVED_KINDS: frozenset[Kind] = frozenset({
    Kind.REMOVED_PUBLIC_SYMBOL,
    Kind.REMOVED_JS_EXPORT,
})

# Move language. Deliberately tight - a relocation commit says so.
_MOVE_WORDS = re.compile(
    r"\b(mov(e|ed|es|ing)|relocat\w*|extract(ed|s|ing)?)\b", re.IGNORECASE,
)
_MOV_TAG = re.compile(r"^\s*\[MOV\]", re.IGNORECASE)


def _short(symbol: str) -> str:
    """Last dotted segment, with leading underscores stripped so a public
    `split_vat` pairs with a private `_split_vat`."""
    return symbol.rsplit(".", 1)[-1].lstrip("_")


def _name_re(short: str) -> re.Pattern[str]:
    # Word-boundary match tolerating leading underscores, so `split_vat`
    # matches `_split_vat` in the message but `id` never matches `video`.
    return re.compile(r"\b_*" + re.escape(short) + r"\b", re.IGNORECASE)


def detect_moves(
    records: list[ChangeRecord], commit: CommitEnvelope,
) -> list[ChangeRecord]:
    """Annotate movable records in place, returning the same list.

    A record is flagged `moved` when signal 1 or 2 fires; `moved_from`
    is set to the paired removal's FQN when signal 1 supplied one.
    """
    removed_by_short: dict[str, str] = {}
    for r in records:
        if r.kind in _REMOVED_KINDS and r.symbol:
            removed_by_short.setdefault(_short(r.symbol), r.symbol)

    subject = commit.subject or ""
    message = f"{subject}\n{commit.body or ''}"
    mov_tag = bool(_MOV_TAG.match(subject))
    move_lines = [ln for ln in message.splitlines() if _MOVE_WORDS.search(ln)]

    for r in records:
        if r.kind not in _MOVABLE_KINDS or not r.symbol or r.moved:
            continue
        short = _short(r.symbol)
        if not short:
            continue
        # Signal 1: a same-commit public removal with the same short name
        # (a different FQN - the file changed, which is the move).
        old = removed_by_short.get(short)
        if old and old != r.symbol:
            r.moved = True
            r.moved_from = old
            continue
        # Signal 2: the commit message frames this symbol as a move -
        # either a [MOV] commit naming it anywhere, or move language on
        # the same line as the symbol.
        name_re = _name_re(short)
        if (mov_tag and name_re.search(message)) or any(
            name_re.search(ln) for ln in move_lines
        ):
            r.moved = True
    return records
