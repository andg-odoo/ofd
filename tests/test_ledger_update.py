"""End-to-end test of the ledger update pipeline.

Builds a tiny fake Odoo repo with an introduction + a rollout commit,
runs the full pipeline to produce raw events, then calls
`ledger.update()` and asserts the resulting markdown file.
"""

from pathlib import Path

from ofd import config as config_mod
from ofd import state as state_mod
from ofd import watchlist as watchlist_mod
from ofd.ledger.update import update as ledger_update
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
    core_paths:
      - odoo/orm/**/*.py
active_version: "20.0"
key_devs:
  - alice@odoo.com
scoring:
  thresholds: {{surface: 3, ledger_threshold: 4, narrate: 5}}
  breadth_bonuses:
    - {{min_rollouts: 5, bonus: 1}}
  dormant_days: 90
  fresh_days: 30
  intent_keywords: [introduce]
narrate:
  backend: claude_code
""")


def _seed(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    repo = make_repo(tmp_path)
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
        author="Alice <alice@odoo.com>",
    )
    repo.commit(
        {
            "odoo/orm/models_cached.py": (
                '"""Cached model machinery."""\n\n'
                "class CachedModel:\n"
                "    _cached_data_fields = ()\n"
            ),
        },
        subject="[ADD] orm: introduce CachedModel",
        body="Introduce a caching model type.",
        author="Alice <alice@odoo.com>",
    )
    repo.commit(
        {
            "addons/website/models/website.py": (
                "from odoo import models\n\n"
                "class Website(models.CachedModel):\n"
                "    _name = 'website'\n"
            ),
        },
        subject="[IMP] website: adopt CachedModel",
        author="Alice <alice@odoo.com>",
    )

    workspace = tmp_path / "ws"
    _write_config(workspace, repo.bare)
    config = config_mod.load(workspace)
    state = state_mod.load()
    wl = watchlist_mod.load(workspace)
    summary = run_pipeline(config, state, wl)
    assert not summary.errors, summary.errors
    return workspace, repo.bare


def test_ledger_update_writes_cached_model_file(tmp_path: Path, monkeypatch):
    workspace, _ = _seed(tmp_path, monkeypatch)
    config = config_mod.load(workspace)
    summary = ledger_update(workspace, config)
    assert summary.written, summary.skipped

    path = workspace / "ledger" / "new-apis" / "odoo.orm.models_cached.CachedModel.md"
    assert path.exists()
    content = path.read_text()

    # Frontmatter populated.
    assert "symbol: odoo.orm.models_cached.CachedModel" in content
    assert "active_version: '20.0'" in content or 'active_version: "20.0"' in content
    assert "status:" in content
    assert "rollout_count: 1" in content

    # All auto sections present.
    assert "<!-- ofd:auto:summary -->" in content
    assert "<!-- ofd:auto:before_after -->" in content
    assert "<!-- ofd:auto:commits -->" in content
    assert "<!-- ofd:auto:adoption -->" in content

    # Narrative placeholder present and empty.
    assert "<!-- ofd:narrative -->" in content

    # The before/after should show the rollout's hunk pair.
    assert "class Website(models.Model)" in content
    assert "class Website(models.CachedModel)" in content

    # Adoption table mentions the website addon.
    assert "website" in content

    # ## Notes anchor is preserved by the default layout.
    assert "## Notes" in content


def test_ledger_update_preserves_human_notes_and_narrative(tmp_path: Path, monkeypatch):
    workspace, _ = _seed(tmp_path, monkeypatch)
    config = config_mod.load(workspace)

    # First pass to create the file.
    ledger_update(workspace, config)
    path = workspace / "ledger" / "new-apis" / "odoo.orm.models_cached.CachedModel.md"

    # Simulate the user hand-editing narrative + notes.
    original = path.read_text()
    edited = (
        original
        .replace(
            "<!-- ofd:narrative -->\n\n<!-- /ofd:narrative -->",
            "<!-- ofd:narrative -->\nHand-written narrative.\n<!-- /ofd:narrative -->",
        )
        .replace("## Notes\n\n", "## Notes\n\nUser-written note.\n")
    )
    path.write_text(edited)

    # Second update should preserve the narrative and the notes.
    ledger_update(workspace, config)
    again = path.read_text()
    assert "Hand-written narrative." in again
    assert "User-written note." in again


def test_ledger_update_force_narrative_regenerates_not_yet_because_no_llm(
    tmp_path: Path, monkeypatch
):
    """With no narrative backend wired in yet, --force-narrative has
    nothing to inject, so the narrative stays empty. Once the narrate
    task plugs in, this test will need updating."""
    workspace, _ = _seed(tmp_path, monkeypatch)
    config = config_mod.load(workspace)
    summary = ledger_update(workspace, config, force_narrative=True)
    assert summary.written


def test_ledger_update_symbol_filter(tmp_path: Path, monkeypatch):
    workspace, _ = _seed(tmp_path, monkeypatch)
    config = config_mod.load(workspace)
    summary = ledger_update(
        workspace, config, symbol_filter="odoo.orm.models_cached.CachedModel"
    )
    assert len(summary.written) == 1
    assert summary.written[0].name == "odoo.orm.models_cached.CachedModel.md"


def test_ledger_update_summary_shows_file_when_stub_upgraded(
    tmp_path: Path, monkeypatch,
):
    """Regression: if a rollout for a symbol was seen before its
    defining commit (SHA-sort order), the resulting ledger entry used
    to render `Introduced in '?'` because `prim.file` was stuck at None.
    After the aggregate fix, the summary must carry the real file path."""
    from ofd.events.record import (
        ChangeRecord, CommitEnvelope, CommitRecord, Kind,
    )
    from ofd.events.store import write

    workspace, _ = _seed(tmp_path, monkeypatch)
    config = config_mod.load(workspace)

    # Inject a rollout-first-then-definition pair for a fresh symbol.
    rollout = CommitRecord(
        commit=CommitEnvelope(
            sha="0" * 40, repo="odoo", branch="master",
            active_version="master",
            author_name="Rollout Dev", author_email="ro@odoo.com",
            committed_at="2026-01-01T00:00:00Z",
            subject="[IMP] adopt", body="",
        ),
        changes=[ChangeRecord(
            kind=Kind.ROLLOUT, file="addons/x/y.xml", line=1,
            symbol="odoo.test.NewThing",
        )],
    )
    definition = CommitRecord(
        commit=CommitEnvelope(
            sha="f" * 40, repo="odoo", branch="master",
            active_version="master",
            author_name="Def Dev", author_email="def@odoo.com",
            committed_at="2026-02-01T00:00:00Z",
            subject="[ADD] test: introduce NewThing", body="",
        ),
        changes=[ChangeRecord(
            kind=Kind.NEW_PUBLIC_CLASS,
            file="odoo/test/new_thing.py", line=10,
            symbol="odoo.test.NewThing",
            signature="class NewThing",
        )],
    )
    write(workspace, rollout)
    write(workspace, definition)

    ledger_update(workspace, config)
    entry = workspace / "ledger" / "new-apis" / "odoo.test.NewThing.md"
    assert entry.exists()
    content = entry.read_text()
    assert "Introduced in `odoo/test/new_thing.py`" in content
    assert "Introduced in `?`" not in content
