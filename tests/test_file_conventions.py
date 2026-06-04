"""File-convention detector: unit tests + an end-to-end pipeline run.

The e2e scenario mirrors the ir.access migration shape:
  - pre-floor: three modules ship the classic `security/ir.model.access.csv`
    (baseline).
  - post-floor commit A: the same basename appears in a NEW module ->
    suppressed by baseline.
  - post-floor commit B: `security/ir.access.csv` lands in three modules
    at once -> one NEW_FILE_CONVENTION definition + two ROLLOUTs.
  - post-floor commit C: a fourth module adopts it -> one ROLLOUT.
"""

from pathlib import Path

from ofd import config as config_mod
from ofd import state as state_mod
from ofd import watchlist as watchlist_mod
from ofd.events.record import ChangeRecord, Kind
from ofd.events.store import iter_repo
from ofd.extractors import file_conventions
from ofd.pipeline import run as run_pipeline
from tests.fixtures.repo_builder import make_repo


def test_candidate_parsing():
    assert file_conventions.candidate(
        "addons/sale/security/ir.access.csv"
    ) == ("addons/sale", "ir.access.csv")
    assert file_conventions.candidate(
        "odoo/addons/base/data/website.xml"
    ) == ("odoo/addons/base", "website.xml")
    # Nested under the bucket: module-internal organization, not a convention.
    assert file_conventions.candidate(
        "addons/sale/data/templates/mail.xml"
    ) is None
    # Wrong bucket / extension / no module prefix.
    assert file_conventions.candidate("addons/sale/views/sale_views.xml") is None
    assert file_conventions.candidate("addons/sale/security/notes.md") is None
    assert file_conventions.candidate("security/ir.access.csv") is None


def test_baseline_basenames():
    paths = [
        "addons/sale/security/ir.model.access.csv",
        "addons/crm/security/ir.model.access.csv",
        "addons/crm/data/crm_data.xml",
        "addons/crm/models/crm_lead.py",
    ]
    assert file_conventions.baseline_basenames(paths) == frozenset(
        {"ir.model.access.csv", "crm_data.xml"}
    )


def _added(*paths: str) -> list[tuple[str, str, str]]:
    out = []
    for p in paths:
        module, basename = file_conventions.candidate(p)
        out.append((p, module, basename))
    return out


def test_extract_definition_plus_rollouts():
    records = file_conventions.extract(
        _added(
            "addons/a/security/ir.access.csv",
            "addons/b/security/ir.access.csv",
            "addons/c/security/ir.access.csv",
        ),
        known_symbols=frozenset(),
    )
    kinds = [r.kind for r in records]
    assert kinds == [Kind.NEW_FILE_CONVENTION, Kind.ROLLOUT, Kind.ROLLOUT]
    definition = records[0]
    assert definition.symbol == "ir.access.csv"
    assert definition.file == "addons/a/security/ir.access.csv"
    assert "3 modules" in definition.symbol_hint
    assert {r.file for r in records[1:]} == {
        "addons/b/security/ir.access.csv",
        "addons/c/security/ir.access.csv",
    }


def test_extract_below_threshold_is_silent():
    records = file_conventions.extract(
        _added(
            "addons/a/security/ir.access.csv",
            "addons/b/security/ir.access.csv",
        ),
        known_symbols=frozenset(),
    )
    assert records == []


def test_extract_known_symbol_yields_rollouts_only():
    records = file_conventions.extract(
        _added("addons/d/security/ir.access.csv"),
        known_symbols=frozenset({"ir.access.csv"}),
    )
    assert [r.kind for r in records] == [Kind.ROLLOUT]
    assert records[0].symbol == "ir.access.csv"


def test_extract_dedupes_within_module():
    # Two candidate files in ONE module is one adopter, not two.
    records = file_conventions.extract(
        _added(
            "addons/a/security/ir.access.csv",
            "addons/a/data/ir.access.csv",
            "addons/b/security/ir.access.csv",
        ),
        known_symbols=frozenset(),
    )
    assert records == []


def test_watchlist_short_name_is_full_basename():
    wl = watchlist_mod.Watchlist()
    record = ChangeRecord(
        kind=Kind.NEW_FILE_CONVENTION,
        file="addons/a/security/ir.access.csv",
        line=0,
        symbol="ir.access.csv",
    )
    entry = wl.add_from_definition(
        record, repo="odoo", sha="deadbeef",
        committed_at="2026-01-01T00:00:00+00:00", active_version="20.0",
    )
    # Not "csv" - the basename is the primitive.
    assert entry.short_name == "ir.access.csv"


def _write_config(workspace: Path, mirror: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "config.yaml").write_text(f"""\
repos:
  odoo:
    source: /dev/null
    mirror: {mirror}
    branch: master
    framework_paths:
      - odoo/orm/**/*.py
    core_paths:
      - odoo/orm/**/*.py
active_version: "20.0"
since_date: "2025-06-01"
key_devs: []
scoring:
  thresholds: {{surface: 3, ledger_threshold: 4, narrate: 5}}
  breadth_bonuses:
    - {{min_rollouts: 5, bonus: 1}}
  dormant_days: 90
  fresh_days: 30
  intent_keywords: [introduce, replace]
narrate:
  backend: claude_code
""")


def _dated(date: str) -> dict[str, str]:
    stamp = f"{date}T12:00:00 +0000"
    return {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}


def test_end_to_end_convention_detection(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    repo = make_repo(tmp_path)
    acl = "id,name,model_id:id,group_id:id\n"

    # Pre-floor baseline: the classic convention exists in 3 modules.
    repo.commit(
        {
            "addons/a/security/ir.model.access.csv": acl,
            "addons/b/security/ir.model.access.csv": acl,
            "addons/c/security/ir.model.access.csv": acl,
        },
        subject="[ADD] a,b,c: initial modules",
        env=_dated("2025-01-15"),
    )

    # Post-floor: a new module ships the BASELINE basename -> no event.
    repo.commit(
        {"addons/d/security/ir.model.access.csv": acl},
        subject="[ADD] d: new module",
        env=_dated("2025-07-01"),
    )

    # Post-floor: the new convention lands in 3 modules at once.
    definition_sha = repo.commit(
        {
            "addons/a/security/ir.access.csv": acl,
            "addons/b/security/ir.access.csv": acl,
            "addons/c/security/ir.access.csv": acl,
        },
        subject="[IMP] *: apply script converting to ir.access",
        env=_dated("2025-07-02"),
    )

    # Post-floor: a straggler module adopts it -> rollout.
    straggler_sha = repo.commit(
        {"addons/d/security/ir.access.csv": acl},
        subject="[IMP] d: convert to ir.access",
        env=_dated("2025-07-03"),
    )

    workspace = tmp_path / "ws"
    _write_config(workspace, repo.bare)
    config = config_mod.load(workspace)
    state = state_mod.load()
    watchlist = watchlist_mod.load(workspace)

    summary = run_pipeline(config, state, watchlist)
    assert not summary.errors, summary.errors

    records = {cr.commit.sha: cr for cr in iter_repo(workspace, "odoo")}

    # The conversion commit: one definition + two same-commit rollouts.
    definition_changes = records[definition_sha].changes
    by_kind: dict[Kind, list] = {}
    for c in definition_changes:
        by_kind.setdefault(c.kind, []).append(c)
    assert len(by_kind[Kind.NEW_FILE_CONVENTION]) == 1
    assert len(by_kind[Kind.ROLLOUT]) == 2
    definition = by_kind[Kind.NEW_FILE_CONVENTION][0]
    assert definition.symbol == "ir.access.csv"
    assert definition.score >= 3
    assert definition.score_reasons

    # The straggler commit: one rollout against the watchlisted symbol.
    straggler_changes = records[straggler_sha].changes
    assert [c.kind for c in straggler_changes] == [Kind.ROLLOUT]
    assert straggler_changes[0].symbol == "ir.access.csv"

    # The baseline-basename commit produced nothing.
    assert all(
        sha in (definition_sha, straggler_sha) for sha in records
    ), f"unexpected raws: {sorted(records)}"

    # Watchlist entry persisted with the full basename as short name.
    persisted = watchlist_mod.load(workspace)
    assert "ir.access.csv" in persisted.entries
    assert persisted.entries["ir.access.csv"].short_name == "ir.access.csv"
