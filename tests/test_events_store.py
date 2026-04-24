from pathlib import Path

from ofd.events.record import ChangeRecord, CommitEnvelope, CommitRecord, Kind
from ofd.events.store import (
    iter_repo,
    prune_before,
    prune_orphan_rollouts,
    read,
    write,
)


def _envelope(sha: str = "abc123") -> CommitEnvelope:
    return CommitEnvelope(
        sha=sha,
        repo="odoo",
        branch="master",
        active_version="20.0",
        author_name="John Doe",
        author_email="jdoe@odoo.com",
        committed_at="2026-04-17T10:32:15Z",
        subject="[ADD] base: introduce models.CachedModel",
        body="Body text",
    )


def test_write_and_read_roundtrip(tmp_path: Path):
    record = CommitRecord(
        commit=_envelope(),
        changes=[
            ChangeRecord(
                kind=Kind.NEW_PUBLIC_CLASS,
                file="odoo/orm/models_cached.py",
                line=8,
                score=5,
                score_reasons=["base:new_public_class:+3", "core_path:+1", "subject_tag:[ADD]:+1"],
                symbol="odoo.orm.models_cached.CachedModel",
                signature="class CachedModel(AbstractModel)",
                after_snippet="class CachedModel(AbstractModel):\n    _cached_data_fields = ()",
            ),
        ],
    )
    write(tmp_path, record)
    got = read(tmp_path, "odoo", "abc123")
    assert got.commit.sha == "abc123"
    assert got.changes[0].kind == Kind.NEW_PUBLIC_CLASS
    assert got.changes[0].symbol == "odoo.orm.models_cached.CachedModel"
    assert got.changes[0].score == 5
    assert got.changes[0].before_snippet is None


def test_write_is_atomic(tmp_path: Path):
    record = CommitRecord(commit=_envelope(), changes=[])
    path = write(tmp_path, record)
    assert path.exists()
    # No leftover tempfiles in the target directory.
    leftovers = [p for p in path.parent.iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_iter_repo_yields_all(tmp_path: Path):
    for i in range(3):
        record = CommitRecord(commit=_envelope(sha=f"sha{i:03d}"), changes=[])
        write(tmp_path, record)
    got = list(iter_repo(tmp_path, "odoo"))
    assert len(got) == 3
    assert [r.commit.sha for r in got] == ["sha000", "sha001", "sha002"]


def test_iter_repo_missing_returns_empty(tmp_path: Path):
    assert list(iter_repo(tmp_path, "nonexistent")) == []


def _envelope_at(sha: str, committed_at: str) -> CommitEnvelope:
    env = _envelope(sha=sha)
    env.committed_at = committed_at
    return env


def test_prune_before_drops_old_and_keeps_new(tmp_path: Path):
    """Pruning respects the `committed_at` date, not file mtime."""
    old = CommitRecord(
        commit=_envelope_at("old001", "2015-06-01T00:00:00Z"), changes=[],
    )
    recent = CommitRecord(
        commit=_envelope_at("new001", "2025-10-15T00:00:00Z"), changes=[],
    )
    write(tmp_path, old)
    write(tmp_path, recent)

    deleted = prune_before(tmp_path, "odoo", "2025-09-01")
    assert deleted == 1
    surviving = [r.commit.sha for r in iter_repo(tmp_path, "odoo")]
    assert surviving == ["new001"]


def test_prune_before_missing_dir_is_noop(tmp_path: Path):
    assert prune_before(tmp_path, "never-existed", "2025-01-01") == 0


def test_prune_before_skips_malformed_json(tmp_path: Path):
    """A garbage file shouldn't stop the prune - it'll be overwritten
    by the next reindex anyway."""
    (tmp_path / "raw" / "odoo").mkdir(parents=True)
    (tmp_path / "raw" / "odoo" / "badfile.json").write_text("not json{{")
    recent = CommitRecord(
        commit=_envelope_at("new001", "2025-10-15T00:00:00Z"), changes=[],
    )
    write(tmp_path, recent)
    # No crash; the valid recent file stays, bad file stays (it isn't
    # parseable so we can't tell its date - better to leave it than
    # assume it's stale).
    deleted = prune_before(tmp_path, "odoo", "2025-09-01")
    assert deleted == 0


def test_prune_orphan_rollouts_rewrites_and_deletes(tmp_path: Path):
    """Rollouts for symbols not in `live_symbols` should be dropped.
    If a file has no survivors, it's deleted outright."""
    # Mixed raw: one definition + one live rollout + one orphan rollout.
    keep_def = ChangeRecord(
        kind=Kind.NEW_PUBLIC_CLASS,
        file="odoo/orm/x.py", line=1,
        symbol="odoo.orm.x.CachedModel",
    )
    live_rollout = ChangeRecord(
        kind=Kind.ROLLOUT, file="a.py", line=1,
        symbol="odoo.orm.x.CachedModel",
    )
    orphan = ChangeRecord(
        kind=Kind.ROLLOUT, file="b.py", line=1,
        symbol="odoo.fields.Integer",  # no longer tracked
    )
    write(tmp_path, CommitRecord(
        commit=_envelope("mixed1"), changes=[keep_def, live_rollout, orphan],
    ))
    # Raw whose ONLY event is an orphan rollout - should be deleted.
    write(tmp_path, CommitRecord(
        commit=_envelope("orphan1"), changes=[
            ChangeRecord(
                kind=Kind.ROLLOUT, file="c.py", line=1,
                symbol="odoo.fields.Integer",
            ),
        ],
    ))
    # Raw with only live events - untouched.
    write(tmp_path, CommitRecord(
        commit=_envelope("clean1"), changes=[keep_def],
    ))

    live_symbols = {"odoo.orm.x.CachedModel"}
    rewritten, deleted = prune_orphan_rollouts(tmp_path, "odoo", live_symbols)
    assert rewritten == 1
    assert deleted == 1

    # Mixed file should still exist without the orphan.
    mixed = read(tmp_path, "odoo", "mixed1")
    mixed_symbols = {c.symbol for c in mixed.changes}
    assert mixed_symbols == {"odoo.orm.x.CachedModel"}
    assert len(mixed.changes) == 2

    # Orphan-only file should be gone.
    assert not (tmp_path / "raw" / "odoo" / "orphan1.json").exists()
    # Clean file should be untouched (left-alone returns no count).
    assert (tmp_path / "raw" / "odoo" / "clean1.json").exists()


def test_prune_orphan_rollouts_keeps_definitions(tmp_path: Path):
    """Definitions aren't filtered by `live_symbols` - they're the
    SOURCE of truth, not candidates for rollout-style matching."""
    write(tmp_path, CommitRecord(
        commit=_envelope("defonly"), changes=[
            ChangeRecord(
                kind=Kind.NEW_VIEW_ATTRIBUTE,
                file="odoo/addons/base/rng/common.rng", line=1,
                symbol="some.old.symbol.attr",  # not in live set
                attribute="foo", element="bar",
            ),
        ],
    ))
    rewritten, deleted = prune_orphan_rollouts(tmp_path, "odoo", set())
    assert rewritten == 0
    assert deleted == 0
    assert (tmp_path / "raw" / "odoo" / "defonly.json").exists()


def test_omits_none_fields_in_serialized_output(tmp_path: Path):
    record = CommitRecord(
        commit=_envelope(),
        changes=[
            ChangeRecord(
                kind=Kind.REMOVED_PUBLIC_SYMBOL,
                file="odoo/osv/expression.py",
                line=42,
                symbol="odoo.osv.expression.AND",
                before_snippet="def AND(domains): ...",
            ),
        ],
    )
    path = write(tmp_path, record)
    import json
    data = json.loads(path.read_text())
    change = data["changes"][0]
    assert "after_snippet" not in change
    assert "warning_text" not in change
    assert change["symbol"] == "odoo.osv.expression.AND"
