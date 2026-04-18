from datetime import UTC, datetime, timedelta

from ofd.config import BreadthBonus, ScoringConfig
from ofd.events.record import ChangeRecord, CommitEnvelope, Kind
from ofd.scoring import (
    ScoreContext,
    aggregate_score,
    breadth_bonus,
    score_event,
    sort_records,
)

DEFAULT_BONUSES = [BreadthBonus(5, 1), BreadthBonus(20, 2), BreadthBonus(50, 3)]


def _commit(subject="[ADD] base: thing", body="", email="alice@example.com"):
    return CommitEnvelope(
        sha="abc",
        repo="odoo",
        branch="master",
        active_version="20.0",
        author_name="Alice",
        author_email=email,
        committed_at="2026-04-17T00:00:00Z",
        subject=subject,
        body=body,
    )


def _ctx(subject="[ADD] base: thing", body="", email="alice@example.com",
         core_paths=None, key_devs=None, intent_keywords=None):
    return ScoreContext(
        commit=_commit(subject, body, email),
        core_paths=core_paths or [],
        key_devs=key_devs or [],
        intent_keywords=intent_keywords or [],
    )


def test_base_scores_per_kind():
    expected = {
        Kind.NEW_PUBLIC_CLASS: 3,
        Kind.NEW_ENDPOINT: 3,
        Kind.DEPRECATION_WARNING_ADDED: 3,
        Kind.REMOVED_PUBLIC_SYMBOL: 3,
        Kind.NEW_DECORATOR_OR_HELPER: 2,
        Kind.SIGNATURE_CHANGE: 1,
        Kind.NEW_CLASS_ATTRIBUTE: 1,
        Kind.ROLLOUT: 0,
    }
    ctx = _ctx(subject="irrelevant")  # no tag, no modifiers
    for kind, base in expected.items():
        r = ChangeRecord(kind=kind, file="x.py", line=1)
        score_event(r, ctx)
        assert r.score == base, f"{kind}: got {r.score}, expected {base}"


def test_core_path_bonus():
    r = ChangeRecord(kind=Kind.NEW_PUBLIC_CLASS, file="odoo/fields.py", line=1)
    ctx = _ctx(core_paths=["odoo/fields.py", "odoo/api.py"])
    score_event(r, ctx)
    assert r.score == 5  # base 3 + core +1 + [ADD] +1 = 5
    assert any("core_path" in reason for reason in r.score_reasons)


def test_tag_add_plus_fix_minus_rev_minus_two():
    r_add = ChangeRecord(kind=Kind.NEW_PUBLIC_CLASS, file="x.py", line=1)
    score_event(r_add, _ctx(subject="[ADD] thing"))
    assert r_add.score == 4  # 3 + 1

    r_fix = ChangeRecord(kind=Kind.NEW_PUBLIC_CLASS, file="x.py", line=1)
    score_event(r_fix, _ctx(subject="[FIX] thing"))
    assert r_fix.score == 2  # 3 - 1

    r_rev = ChangeRecord(kind=Kind.NEW_PUBLIC_CLASS, file="x.py", line=1)
    score_event(r_rev, _ctx(subject="[REV] thing"))
    assert r_rev.score == 1  # 3 - 2


def test_key_dev_author_bonus():
    r = ChangeRecord(kind=Kind.NEW_DECORATOR_OR_HELPER, file="x.py", line=1)
    ctx = _ctx(subject="irrelevant", email="jdoe@odoo.com", key_devs=["jdoe@odoo.com"])
    score_event(r, ctx)
    assert r.score == 3  # base 2 + key_dev +1
    assert any("key_dev_author" in reason for reason in r.score_reasons)


def test_symbol_in_message_bonus():
    r = ChangeRecord(
        kind=Kind.NEW_PUBLIC_CLASS, file="x.py", line=1,
        symbol="odoo.orm.models_cached.CachedModel",
    )
    ctx = _ctx(subject="[ADD] introduce CachedModel")
    score_event(r, ctx)
    # 3 + [ADD] +1 + symbol_in_message +1 + intent_keyword (if introduce is keyword) - not configured here
    assert "symbol_in_message:CachedModel:+1" in r.score_reasons


def test_intent_keyword_bonus():
    r = ChangeRecord(kind=Kind.NEW_DECORATOR_OR_HELPER, file="x.py", line=1)
    ctx = _ctx(subject="something", body="This is introduced here", intent_keywords=["introduce"])
    score_event(r, ctx)
    assert any("intent_keyword" in reason for reason in r.score_reasons)


def test_tests_path_penalty():
    r = ChangeRecord(kind=Kind.NEW_PUBLIC_CLASS, file="odoo/tests/common.py", line=1)
    ctx = _ctx(subject="irrelevant")
    score_event(r, ctx)
    assert r.score == 2  # 3 - 1 for tests path
    assert any("tests_path" in reason for reason in r.score_reasons)


def test_clamp_upper():
    r = ChangeRecord(
        kind=Kind.NEW_PUBLIC_CLASS, file="odoo/fields.py", line=1,
        symbol="odoo.fields.NewThing",
    )
    ctx = _ctx(
        subject="[ADD] introduce NewThing",
        body="introduce a new API",
        email="jdoe@odoo.com",
        core_paths=["odoo/fields.py"],
        key_devs=["jdoe@odoo.com"],
        intent_keywords=["introduce"],
    )
    score_event(r, ctx)
    # 3 + core +1 + [ADD] +1 + keydev +1 + symbol +1 + intent +1 = 8 → clamp 5
    assert r.score == 5
    assert any("clamped" in reason for reason in r.score_reasons)


def test_clamp_lower():
    r = ChangeRecord(kind=Kind.SIGNATURE_CHANGE, file="tests/foo.py", line=1)
    ctx = _ctx(subject="[REV] revert thing")
    score_event(r, ctx)
    # 1 + [REV] -2 + tests -1 = -2 → clamp 0
    assert r.score == 0
    assert any("clamped" in reason for reason in r.score_reasons)


def test_breadth_bonus_thresholds():
    now = datetime(2026, 6, 1, tzinfo=UTC)
    old = now - timedelta(days=120)
    assert breadth_bonus(0, DEFAULT_BONUSES, old, now) == 0
    assert breadth_bonus(4, DEFAULT_BONUSES, old, now) == 0
    assert breadth_bonus(5, DEFAULT_BONUSES, old, now) == 1
    assert breadth_bonus(19, DEFAULT_BONUSES, old, now) == 1
    assert breadth_bonus(20, DEFAULT_BONUSES, old, now) == 2
    assert breadth_bonus(49, DEFAULT_BONUSES, old, now) == 2
    assert breadth_bonus(50, DEFAULT_BONUSES, old, now) == 3
    assert breadth_bonus(999, DEFAULT_BONUSES, old, now) == 3


def test_breadth_recency_floor_for_fresh_primitives():
    now = datetime(2026, 6, 1, tzinfo=UTC)
    fresh = now - timedelta(days=10)
    # Zero rollouts but <30 days old → floor at 1.
    assert breadth_bonus(0, DEFAULT_BONUSES, fresh, now, fresh_days=30) == 1
    # Already past threshold → keep actual bonus.
    assert breadth_bonus(50, DEFAULT_BONUSES, fresh, now, fresh_days=30) == 3


def test_aggregate_score_clamped():
    now = datetime(2026, 6, 1, tzinfo=UTC)
    old = now - timedelta(days=120)
    config = ScoringConfig()
    # definition 4 + breadth 3 (≥50 rollouts) = 7 → clamp 5
    assert aggregate_score(4, 60, old, config, now) == 5


def test_sort_records_by_score_then_kind():
    a = ChangeRecord(kind=Kind.NEW_PUBLIC_CLASS, file="a.py", line=1, score=5, symbol="A")
    b = ChangeRecord(kind=Kind.SIGNATURE_CHANGE, file="b.py", line=1, score=5, symbol="B")
    c = ChangeRecord(kind=Kind.NEW_DECORATOR_OR_HELPER, file="c.py", line=1, score=3, symbol="C")
    d = ChangeRecord(kind=Kind.ROLLOUT, file="d.py", line=1, score=0, symbol="D")
    got = sort_records([d, b, a, c])
    # Score desc, then kind priority (NEW_PUBLIC_CLASS before SIGNATURE_CHANGE).
    assert [r.symbol for r in got] == ["A", "B", "C", "D"]
