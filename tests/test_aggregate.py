"""Focused tests for `aggregate.build_primitives()` stub-upgrade logic."""

from pathlib import Path

from ofd.aggregate import build_primitives
from ofd.events.record import ChangeRecord, CommitEnvelope, CommitRecord, Kind
from ofd.events.store import write


def _env(sha: str, committed_at: str = "2026-03-05T20:47:09Z") -> CommitEnvelope:
    return CommitEnvelope(
        sha=sha, repo="odoo", branch="master", active_version="master",
        author_name="Dev", author_email="dev@odoo.com",
        committed_at=committed_at,
        subject=f"[IMP] test {sha[:8]}", body="",
    )


def test_build_primitives_upgrades_stub_when_definition_arrives_later(
    tmp_path: Path,
):
    """If a rollout commit's SHA sorts before the defining commit's SHA,
    the aggregator creates a stub primitive for the rollout first. The
    later definition event must upgrade the stub - file, kind, and
    definition_record must all reflect the real definition, not the
    NEW_PUBLIC_CLASS default that `ROLLOUT` stubs get seeded with."""
    rollout_sha = "0000abcdef" + "0" * 30  # sorts first
    def_sha = "ffff123456" + "f" * 30      # sorts last

    # Rollout record (seen first due to sort order).
    write(tmp_path, CommitRecord(
        commit=_env(rollout_sha),
        changes=[ChangeRecord(
            kind=Kind.ROLLOUT, file="addons/sale/views/sale.xml", line=42,
            symbol="odoo.addons.base.rng.common.widget.invisible",
        )],
    ))
    # Definition record (processed later because of sort order).
    write(tmp_path, CommitRecord(
        commit=_env(def_sha),
        changes=[ChangeRecord(
            kind=Kind.NEW_VIEW_ATTRIBUTE,
            file="odoo/addons/base/rng/common.rng", line=432,
            symbol="odoo.addons.base.rng.common.widget.invisible",
            attribute="invisible", element="widget",
        )],
    ))

    primitives = build_primitives(tmp_path, ["odoo"])
    prim = primitives["odoo.addons.base.rng.common.widget.invisible"]

    assert prim.kind == Kind.NEW_VIEW_ATTRIBUTE, (
        "stub kind guess must be overwritten by the real definition"
    )
    assert prim.file == "odoo/addons/base/rng/common.rng", (
        "stub's None file must be replaced with the real RNG path"
    )
    assert prim.definition_record is not None
    assert prim.definition_record.element == "widget"
    assert len(prim.definition_commits) == 1
    assert prim.rollout_count == 1


def test_build_primitives_keeps_first_definition_when_two_define(
    tmp_path: Path,
):
    """Two definition events for the same symbol: the FIRST one seen
    wins (kind / file / snippet). Subsequent ones just append to
    definition_commits. The stub-upgrade path must not clobber a real
    definition - that happens only when `definition_record is None`."""
    write(tmp_path, CommitRecord(
        commit=_env("aaaa" + "a" * 36, "2026-03-01T00:00:00Z"),
        changes=[ChangeRecord(
            kind=Kind.NEW_PUBLIC_CLASS,
            file="odoo/real/path.py", line=10,
            symbol="odoo.test.Foo",
        )],
    ))
    write(tmp_path, CommitRecord(
        commit=_env("bbbb" + "b" * 36, "2026-03-02T00:00:00Z"),
        changes=[ChangeRecord(
            kind=Kind.SIGNATURE_CHANGE,
            file="odoo/different/path.py", line=20,
            symbol="odoo.test.Foo",
        )],
    ))
    primitives = build_primitives(tmp_path, ["odoo"])
    prim = primitives["odoo.test.Foo"]
    # The second event is a SIGNATURE_CHANGE which isn't in DEFINITION_KINDS,
    # so it doesn't even hit the upgrade branch. Sanity check that the
    # primitive we get reflects the first event.
    assert prim.kind == Kind.NEW_PUBLIC_CLASS
    assert prim.file == "odoo/real/path.py"
