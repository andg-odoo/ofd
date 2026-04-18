"""Aggregation across raw events.

Walks every raw event JSON for every configured repo and rolls them up
per primitive symbol. The output of `build_primitives()` is the single
source of truth the ledger renderer, digest renderer, and `show`/`list`
commands all consume.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from ofd.events.record import DEFINITION_KINDS, ChangeRecord, Kind
from ofd.events.store import iter_repo


@dataclass
class CommitRef:
    sha: str
    repo: str
    committed_at: str
    author_name: str
    author_email: str
    subject: str


@dataclass
class RolloutOccurrence:
    commit: CommitRef
    file: str
    model: str | None
    before_snippet: str | None
    after_snippet: str | None
    hunk_header: str | None


@dataclass
class Primitive:
    """Aggregated view of one framework primitive.

    Built from the raw event store; the ledger and digest renderers read
    this and never touch the raw files directly.
    """
    symbol: str
    kind: Kind
    active_version: str
    definition_commits: list[CommitRef] = field(default_factory=list)
    rollouts: list[RolloutOccurrence] = field(default_factory=list)
    definition_record: ChangeRecord | None = None
    # Auxiliary data useful for ledger/digest rendering.
    file: str | None = None
    signature: str | None = None
    after_snippet: str | None = None
    warning_text: str | None = None
    removal_version: str | None = None

    @property
    def first_seen(self) -> datetime:
        dates = [_parse_iso(c.committed_at) for c in self.definition_commits]
        return min(dates) if dates else datetime.fromtimestamp(0)

    @property
    def last_activity(self) -> datetime:
        candidates = [_parse_iso(c.committed_at) for c in self.definition_commits]
        candidates.extend(_parse_iso(r.commit.committed_at) for r in self.rollouts)
        return max(candidates) if candidates else self.first_seen

    @property
    def rollout_count(self) -> int:
        return len(self.rollouts)

    @property
    def adopting_modules(self) -> dict[str, tuple[datetime, int]]:
        """Module name -> (first_rollout_date, count). Module derived
        from the `addons/<name>/...` or `<name>/models/...` prefix of
        the rollout file path.
        """
        by_mod: dict[str, list[datetime]] = defaultdict(list)
        for r in self.rollouts:
            mod = _module_of(r.file)
            if mod is None:
                continue
            by_mod[mod].append(_parse_iso(r.commit.committed_at))
        return {m: (min(dates), len(dates)) for m, dates in by_mod.items()}


def _parse_iso(s: str) -> datetime:
    # Accept both `...Z` and `+00:00` style offsets.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _module_of(file: str) -> str | None:
    """Best-effort module-name extraction from a repo-rooted path.

    addons/<mod>/... -> <mod>
    odoo/addons/<mod>/... -> <mod>
    <mod>/models/... -> <mod> (common enterprise layout)
    Otherwise None.
    """
    parts = PurePosixPath(file).parts
    if not parts:
        return None
    if parts[0] == "addons" and len(parts) > 1:
        return parts[1]
    if len(parts) >= 3 and parts[0] == "odoo" and parts[1] == "addons":
        return parts[2]
    if len(parts) >= 2 and parts[1] in {"models", "static", "views", "report", "wizard", "controllers"}:
        return parts[0]
    return None


def build_primitives(
    workspace: Path,
    repo_names: list[str],
) -> dict[str, Primitive]:
    """Walk the raw event store for each repo and roll events into a
    symbol-keyed primitive map.

    A primitive is created the first time we see a definition event for
    its symbol. Subsequent definition events (polish commits on the same
    symbol) are appended to `definition_commits`. Rollout events target
    the symbol named on the rollout record.
    """
    primitives: dict[str, Primitive] = {}

    for repo in repo_names:
        for commit_record in iter_repo(workspace, repo):
            for change in commit_record.changes:
                if not change.symbol:
                    continue
                ref = CommitRef(
                    sha=commit_record.commit.sha,
                    repo=commit_record.commit.repo,
                    committed_at=commit_record.commit.committed_at,
                    author_name=commit_record.commit.author_name,
                    author_email=commit_record.commit.author_email,
                    subject=commit_record.commit.subject,
                )
                if change.kind in DEFINITION_KINDS:
                    prim = primitives.get(change.symbol)
                    if prim is None:
                        prim = Primitive(
                            symbol=change.symbol,
                            kind=change.kind,
                            active_version=commit_record.commit.active_version,
                            definition_record=change,
                            file=change.file,
                            signature=change.signature,
                            after_snippet=change.after_snippet,
                        )
                        primitives[change.symbol] = prim
                    prim.definition_commits.append(ref)
                elif change.kind == Kind.ROLLOUT:
                    prim = primitives.get(change.symbol)
                    if prim is None:
                        # Rollout for a symbol we haven't seen a definition
                        # for yet (can happen on reindex or manual watchlist
                        # entry). Synthesize a stub primitive so we don't
                        # drop the data.
                        prim = Primitive(
                            symbol=change.symbol,
                            kind=Kind.NEW_PUBLIC_CLASS,  # best guess
                            active_version=commit_record.commit.active_version,
                        )
                        primitives[change.symbol] = prim
                    prim.rollouts.append(RolloutOccurrence(
                        commit=ref,
                        file=change.file,
                        model=change.model,
                        before_snippet=change.before_snippet,
                        after_snippet=change.after_snippet,
                        hunk_header=change.hunk_header,
                    ))
                elif change.kind == Kind.DEPRECATION_WARNING_ADDED:
                    # Tracked under its own ledger category - keyed by
                    # symbol_hint.
                    hint = change.symbol_hint or change.symbol
                    prim = primitives.get(hint)
                    if prim is None:
                        prim = Primitive(
                            symbol=hint,
                            kind=Kind.DEPRECATION_WARNING_ADDED,
                            active_version=commit_record.commit.active_version,
                            definition_record=change,
                            file=change.file,
                            warning_text=change.warning_text,
                            removal_version=change.removal_version,
                        )
                        primitives[hint] = prim
                    prim.definition_commits.append(ref)
                # Other kinds (signature_change, new_class_attribute,
                # new_view_attribute, new_view_directive) are kept in raw/
                # and are surfaced by query/digest but not promoted into
                # standalone ledger entries at v1. They'll be referenced
                # via their owning primitive when relevant.

    return primitives


def select_definition_commit(prim: Primitive) -> CommitRef | None:
    """Pick the earliest definition commit as the canonical one."""
    if not prim.definition_commits:
        return None
    return min(prim.definition_commits, key=lambda c: _parse_iso(c.committed_at))


def select_canonical_rollout(
    prim: Primitive,
    key_devs: list[str],
) -> RolloutOccurrence | None:
    """Pick oldest rollout by a key-dev, falling back to oldest overall."""
    if not prim.rollouts:
        return None
    if key_devs:
        by_keydev = [
            r for r in prim.rollouts if r.commit.author_email in key_devs
        ]
        if by_keydev:
            return min(by_keydev, key=lambda r: _parse_iso(r.commit.committed_at))
    return min(prim.rollouts, key=lambda r: _parse_iso(r.commit.committed_at))
