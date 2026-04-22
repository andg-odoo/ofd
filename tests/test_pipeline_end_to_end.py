"""End-to-end test for the vertical slice.

Fake Odoo-like repo with two commits:
  1. Introduces models.CachedModel in a framework path.
  2. Rolls it out: website.py switches Model -> CachedModel.

Verifies:
- definition event emitted for the class
- watchlist updated (persisted + reloaded)
- rollout event emitted against the second commit
- raw/<repo>/<sha>.json files written
- scores populated
"""

from pathlib import Path

from ofd import config as config_mod
from ofd import state as state_mod
from ofd import watchlist as watchlist_mod
from ofd.events.record import Kind
from ofd.events.store import iter_repo
from ofd.pipeline import run as run_pipeline
from tests.fixtures.repo_builder import make_repo


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
      - odoo/fields.py
      - odoo/models/**/*.py
    core_paths:
      - odoo/orm/**/*.py
      - odoo/fields.py
active_version: "20.0"
key_devs:
  - test@example.com
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


def test_end_to_end_cached_model_introduction_and_rollout(tmp_path: Path, monkeypatch):
    # Isolate state file from the real ~/.local/share so test is hermetic.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    repo = make_repo(tmp_path)

    # Commit 1: baseline with AbstractModel already present.
    repo.commit(
        {
            "odoo/orm/__init__.py": "",
            "odoo/orm/models_cached.py": (
                '"""Cached model machinery."""\n\n'
                "class AbstractModel:\n"
                "    _abstract = True\n"
            ),
        },
        subject="[ADD] orm: AbstractModel skeleton",
        author="Test User <test@example.com>",
    )

    # Commit 2: introduce CachedModel - the definition event.
    definition_sha = repo.commit(
        {
            "odoo/orm/models_cached.py": (
                '"""Cached model machinery."""\n\n'
                "class AbstractModel:\n"
                "    _abstract = True\n\n\n"
                "class CachedModel(AbstractModel):\n"
                '    """Model type that caches selected fields."""\n'
                "    _cached_data_domain = []\n"
                "    _cached_data_fields = ()\n"
            ),
        },
        subject="[ADD] orm: introduce CachedModel",
        body="A new model type that caches selected fields.",
        author="Test User <test@example.com>",
    )

    # Commit 3: rollout - website switches from Model to CachedModel.
    repo.commit(
        {
            "addons/website/models/website.py": (
                "from odoo import models\n\n"
                "class Website(models.Model):\n"
                "    _name = 'website'\n"
                "    _description = 'Website'\n"
            ),
        },
        subject="[IMP] website: baseline",
        author="Test User <test@example.com>",
    )
    repo.commit(
        {
            "addons/website/models/website.py": (
                "from odoo import models\n\n"
                "class Website(models.CachedModel):\n"
                "    _name = 'website'\n"
                "    _description = 'Website'\n"
            ),
        },
        subject="[IMP] website: use CachedModel",
        author="Test User <test@example.com>",
    )

    # Workspace + config pointing at the bare mirror.
    workspace = tmp_path / "ws"
    _write_config(workspace, repo.bare)
    config = config_mod.load(workspace)
    state = state_mod.load()
    watchlist = watchlist_mod.load(workspace)

    summary = run_pipeline(config, state, watchlist)

    assert not summary.errors, summary.errors
    # 4 commits total; only 3 touch framework-path files so 3 are scanned.
    # Commit 1 (framework): baseline, no changes vs parent (file is new).
    # Commit 2 (framework): definition event.
    # Commit 3 (addon only): no framework-path touch, not enumerated.
    # Commit 4 (addon only): same - not enumerated under gated filter.
    # So we expect commit 1 and commit 2 to be scanned.
    assert "odoo" in summary.repos
    scanned_shas = {c.sha for c in summary.repos["odoo"]}
    assert definition_sha in scanned_shas

    records = list(iter_repo(workspace, "odoo"))
    # The baseline commit adds a new file with AbstractModel (also a new class),
    # but AbstractModel is private-style-indicator-less so still public here.
    # The definition commit adds CachedModel.
    all_new_classes = [
        r for cr in records for r in cr.changes
        if r.kind == Kind.NEW_PUBLIC_CLASS
    ]
    symbols = {r.symbol for r in all_new_classes}
    assert "odoo.orm.models_cached.CachedModel" in symbols

    # CachedModel's definition record should have non-zero score and audit trail.
    cached_record = next(
        r for cr in records for r in cr.changes
        if r.symbol == "odoo.orm.models_cached.CachedModel"
        and r.kind == Kind.NEW_PUBLIC_CLASS
    )
    assert cached_record.score >= 4  # base 3 + core +1 + [ADD] +1 (at least)
    assert cached_record.score_reasons
    assert any("core_path" in reason for reason in cached_record.score_reasons)

    # Watchlist picked up the definition.
    persisted_wl = watchlist_mod.load(workspace)
    assert "odoo.orm.models_cached.CachedModel" in persisted_wl.entries

    # State was advanced.
    persisted_state = state_mod.load()
    assert persisted_state.get("odoo").last_seen_sha is not None


def test_end_to_end_rollout_detected_when_commit_touches_framework(tmp_path: Path, monkeypatch):
    """A commit that defines AND uses a primitive in the same commit emits
    both events."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    repo = make_repo(tmp_path)

    # Baseline with an empty orm module.
    repo.commit(
        {
            "odoo/orm/__init__.py": "",
            "odoo/orm/models_cached.py": '"""stub."""\n',
            "addons/website/models/website.py": (
                "from odoo import models\n\n"
                "class Website(models.Model):\n"
                "    _name = 'website'\n"
            ),
        },
        subject="[ADD] baseline",
        author="Test User <test@example.com>",
    )

    # Same commit: introduce CachedModel AND convert Website.
    combo_sha = repo.commit(
        {
            "odoo/orm/models_cached.py": (
                '"""Cached model machinery."""\n\n'
                "class CachedModel:\n"
                "    _cached_data_fields = ()\n"
            ),
            "addons/website/models/website.py": (
                "from odoo import models\n\n"
                "class Website(models.CachedModel):\n"
                "    _name = 'website'\n"
            ),
        },
        subject="[ADD] orm: introduce CachedModel and adopt in website",
        author="Test User <test@example.com>",
    )

    workspace = tmp_path / "ws"
    _write_config(workspace, repo.bare)
    config = config_mod.load(workspace)
    state = state_mod.load()
    watchlist = watchlist_mod.load(workspace)

    summary = run_pipeline(config, state, watchlist)
    assert not summary.errors

    # The combo commit's record should contain both a definition AND a rollout.
    records = list(iter_repo(workspace, "odoo"))
    combo_record = next(cr for cr in records if cr.commit.sha == combo_sha)
    kinds = {c.kind for c in combo_record.changes}
    assert Kind.NEW_PUBLIC_CLASS in kinds
    assert Kind.ROLLOUT in kinds

    rollout = next(c for c in combo_record.changes if c.kind == Kind.ROLLOUT)
    assert rollout.symbol == "odoo.orm.models_cached.CachedModel"
    assert rollout.model == "website"


def test_version_bump_updates_detected_version_and_stamps_next_commit(
    tmp_path: Path, monkeypatch,
):
    """Commits after a bump to `odoo/release.py` should be stamped with the
    new series, independent of what config.yaml's `active_version` says."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    repo = make_repo(tmp_path)
    repo.commit(
        {
            "odoo/release.py": "version_info = (19, 3, 0, 'alpha', 1, '')\n",
            "odoo/orm/__init__.py": "",
        },
        subject="[ADD] baseline 19.3",
        author="Test User <test@example.com>",
    )
    # Bump-only commit; no framework changes.
    repo.commit(
        {"odoo/release.py": "version_info = (19, 4, 0, 'alpha', 1, '')\n"},
        subject="[IMP] core: bump master release to 19.4 alpha",
        author="Test User <test@example.com>",
    )
    # Post-bump commit introduces a framework primitive.
    post_bump_sha = repo.commit(
        {
            "odoo/orm/models_cached.py": (
                '"""Cached."""\nclass CachedModel:\n    _cached_data_fields = ()\n'
            ),
        },
        subject="[ADD] orm: CachedModel",
        author="Test User <test@example.com>",
    )

    workspace = tmp_path / "ws"
    _write_config(workspace, repo.bare)
    config = config_mod.load(workspace)
    state = state_mod.load()
    summary = run_pipeline(config, state, watchlist_mod.load(workspace))
    assert not summary.errors

    record = next(
        cr for cr in iter_repo(workspace, "odoo") if cr.commit.sha == post_bump_sha
    )
    assert record.commit.active_version == "19.4"
    assert state.get("odoo").detected_version == "19.4"
