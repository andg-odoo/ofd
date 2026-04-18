from pathlib import Path

from ofd.events.record import ChangeRecord, CommitEnvelope, CommitRecord, Kind
from ofd.events.store import iter_repo, read, write


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
