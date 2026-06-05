"""Per-event and aggregate scoring.

Per-event: deterministic base + modifiers + clamp to [0, 5]. Every
modifier hit is appended to score_reasons so the resulting score is
auditable end-to-end.

Aggregate: definition_score + breadth_bonus, with a recency floor so
newly-introduced primitives aren't penalized for not yet having rollouts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from ofd.config import BreadthBonus, ScoringConfig
from ofd.events.record import ChangeRecord, CommitEnvelope, Kind
from ofd.globs import match_any

_BASE: dict[Kind, int] = {
    # Epoch events ("OWL 3 landed", "weasyprint added to requirements")
    # should make the ledger on their own; 4 clears the ledger threshold
    # without any modifier help.
    Kind.VENDORED_LIB_BUMP: 4,
    Kind.DEPENDENCY_CHANGE: 4,
    Kind.NEW_PUBLIC_CLASS: 3,
    Kind.NEW_ENDPOINT: 3,
    Kind.NEW_VIEW_TYPE: 3,
    Kind.NEW_VIEW_ATTRIBUTE: 3,
    Kind.NEW_VIEW_ELEMENT: 3,
    Kind.DEPRECATION_WARNING_ADDED: 3,
    Kind.REMOVED_PUBLIC_SYMBOL: 3,
    Kind.REMOVED_VIEW_ATTRIBUTE: 3,
    Kind.NEW_FILE_CONVENTION: 3,
    # A new registry category is a new extension point; a removed export
    # is a deprecation story. A new manifest key is a new module-level
    # platform knob.
    Kind.NEW_REGISTRY_CATEGORY: 3,
    Kind.NEW_MANIFEST_KEY: 3,
    Kind.REMOVED_JS_EXPORT: 3,
    # A removed module is a rare, loud deprecation story. A new module
    # starts at 2: [ADD] subject tag (+1) puts a deliberate addition at
    # the surface threshold, the auto_install penalty (-1) sinks bridge
    # glue, and intent keywords / adoption breadth promote the real
    # platform stories (new report engine) toward the ledger.
    Kind.REMOVED_MODULE: 3,
    Kind.NEW_MODULE: 2,
    Kind.NEW_DECORATOR_OR_HELPER: 2,
    Kind.NEW_KWARG: 2,
    Kind.NEW_VIEW_DIRECTIVE: 2,
    Kind.NEW_JS_EXPORT: 2,
    Kind.NEW_REGISTRY_ENTRY: 2,
    Kind.SIGNATURE_CHANGE: 1,
    Kind.NEW_CLASS_ATTRIBUTE: 1,
    Kind.ROLLOUT: 0,
}

_KIND_PRIORITY: dict[Kind, int] = {
    Kind.NEW_PUBLIC_CLASS: 0,
    Kind.DEPRECATION_WARNING_ADDED: 1,
    Kind.REMOVED_PUBLIC_SYMBOL: 2,
    Kind.NEW_ENDPOINT: 3,
    Kind.NEW_VIEW_TYPE: 4,
    Kind.NEW_VIEW_ATTRIBUTE: 5,
    Kind.NEW_VIEW_ELEMENT: 6,
    Kind.REMOVED_VIEW_ATTRIBUTE: 7,
    Kind.NEW_FILE_CONVENTION: 8,
    Kind.NEW_DECORATOR_OR_HELPER: 9,
    Kind.NEW_KWARG: 10,
    Kind.NEW_VIEW_DIRECTIVE: 11,
    Kind.SIGNATURE_CHANGE: 12,
    Kind.NEW_CLASS_ATTRIBUTE: 13,
    Kind.VENDORED_LIB_BUMP: 14,
    Kind.NEW_REGISTRY_CATEGORY: 15,
    Kind.REMOVED_JS_EXPORT: 16,
    Kind.NEW_JS_EXPORT: 17,
    Kind.NEW_REGISTRY_ENTRY: 18,
    Kind.DEPENDENCY_CHANGE: 19,
    Kind.NEW_MANIFEST_KEY: 20,
    Kind.NEW_MODULE: 21,
    Kind.REMOVED_MODULE: 22,
    Kind.ROLLOUT: 99,
}

_TAG_PATTERN = re.compile(r"^\[([A-Z]+)\]")


def _tag(subject: str) -> str | None:
    m = _TAG_PATTERN.match(subject)
    return m.group(1) if m else None


def _matches_any(path: str, globs: list[str]) -> bool:
    return match_any(path, globs)


@dataclass
class ScoreContext:
    """Per-commit context needed by scoring."""
    commit: CommitEnvelope
    core_paths: list[str]
    key_devs: list[str]
    intent_keywords: list[str]


def score_event(record: ChangeRecord, ctx: ScoreContext) -> ChangeRecord:
    """Mutates and returns the record with score + score_reasons populated."""
    base = _BASE.get(record.kind, 0)
    reasons = [f"base:{record.kind.value}:+{base}"]
    delta = 0

    if ctx.core_paths and _matches_any(record.file, ctx.core_paths):
        delta += 1
        reasons.append("core_path:+1")

    tag = _tag(ctx.commit.subject)
    if tag == "ADD":
        delta += 1
        reasons.append("subject_tag:[ADD]:+1")
    elif tag == "FIX":
        delta -= 1
        reasons.append("subject_tag:[FIX]:-1")
    elif tag == "REV":
        delta -= 2
        reasons.append("subject_tag:[REV]:-2")

    if ctx.commit.author_email in ctx.key_devs:
        delta += 1
        reasons.append(f"key_dev_author:{ctx.commit.author_email}:+1")

    # Module commits name their module in the subject by convention
    # (`[ADD] paper_muncher: ...`), so the mention carries no signal
    # for module kinds - every new module would get the bump.
    if record.symbol and record.kind not in (
        Kind.NEW_MODULE, Kind.REMOVED_MODULE,
    ):
        short = record.symbol.rsplit(".", 1)[-1]
        blob = f"{ctx.commit.subject}\n{ctx.commit.body}"
        if short and short in blob:
            delta += 1
            reasons.append(f"symbol_in_message:{short}:+1")

    lowered = f"{ctx.commit.subject}\n{ctx.commit.body}".lower()
    if any(kw.lower() in lowered for kw in ctx.intent_keywords):
        delta += 1
        reasons.append("intent_keyword:+1")

    if "/tests/" in record.file or record.file.startswith("tests/"):
        delta -= 1
        reasons.append("tests_path:-1")

    if record.auto_install:
        delta -= 1
        reasons.append("auto_install_bridge:-1")

    raw = base + delta
    final = max(0, min(5, raw))
    if raw != final:
        reasons.append(f"clamped:{raw}->{final}")

    record.score = final
    record.score_reasons = reasons
    return record


def breadth_bonus(
    rollout_count: int,
    bonuses: list[BreadthBonus],
    first_seen: datetime,
    now: datetime | None = None,
    fresh_days: int = 30,
) -> int:
    """Compute the aggregate breadth bonus with a recency floor.

    Primitives younger than `fresh_days` floor their bonus at 1 so they
    aren't penalized for not having had time to roll out.
    """
    now = now or datetime.now(tz=UTC)
    ranked = sorted(bonuses, key=lambda b: b.min_rollouts)
    bonus = 0
    for b in ranked:
        if rollout_count >= b.min_rollouts:
            bonus = b.bonus
    age_days = (now - first_seen).total_seconds() / 86400
    if age_days < fresh_days:
        bonus = max(bonus, 1)
    return bonus


def aggregate_score(
    definition_score: int,
    rollout_count: int,
    first_seen: datetime,
    config: ScoringConfig,
    now: datetime | None = None,
) -> int:
    bonus = breadth_bonus(
        rollout_count, config.breadth_bonuses, first_seen, now, config.fresh_days
    )
    return min(5, definition_score + bonus)


def sort_records(records: list[ChangeRecord]) -> list[ChangeRecord]:
    """Sort by score desc, then kind priority, then by file/symbol for stability."""
    return sorted(
        records,
        key=lambda r: (
            -r.score,
            _KIND_PRIORITY.get(r.kind, 99),
            r.file,
            r.symbol or "",
        ),
    )
