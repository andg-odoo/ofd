"""Tests for the narrate layer.

The LLM backend itself is mocked. We verify:
- prompt rendering includes the pieces the LLM needs
- the runner writes the narrative into the correct marker region
- eligibility gates (status, min_rollouts) behave
- --force overrides an existing narrative
- json-envelope parsing handles common shapes
"""

from pathlib import Path

import pytest

from ofd import config as config_mod
from ofd import state as state_mod
from ofd import watchlist as watchlist_mod
from ofd.ledger.update import update as ledger_update
from ofd.narrate.client import (
    NarrateError,
    _extract_text_from_cc_json,
)
from ofd.narrate.prompts import (
    UserPromptInput,
    render_user_prompt,
)
from ofd.narrate.runner import narrate_all
from ofd.pipeline import run as run_pipeline
from tests.fixtures.repo_builder import make_repo


class _MockBackend:
    def __init__(self, response: str):
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def narrate(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.response


def _write_config(workspace: Path, mirror: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "config.yaml").write_text(f"""\
repos:
  odoo:
    source: /dev/null
    mirror: {mirror}
    branch: master
    framework_paths: [odoo/orm/**/*.py]
    core_paths: [odoo/orm/**/*.py]
active_version: "20.0"
key_devs: [alice@odoo.com]
narrate:
  backend: claude_code
  default_status_filter: [fresh, active]
  min_rollouts: 0
""")


def _seed(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    repo = make_repo(tmp_path)
    repo.commit(
        {
            "odoo/orm/__init__.py": "",
            "odoo/orm/models_cached.py": '"""stub."""\n',
            "addons/website/models/website.py": (
                "from odoo import models\nclass Website(models.Model):\n    _name='w'\n"
            ),
        },
        subject="[ADD] baseline",
        author="Alice <alice@odoo.com>",
    )
    repo.commit(
        {
            "odoo/orm/models_cached.py": (
                '"""Cached."""\nclass CachedModel:\n    _cached_data_fields=()\n'
            ),
        },
        subject="[ADD] orm: introduce CachedModel",
        author="Alice <alice@odoo.com>",
    )
    repo.commit(
        {
            "addons/website/models/website.py": (
                "from odoo import models\nclass Website(models.CachedModel):\n    _name='w'\n"
            ),
        },
        subject="[IMP] website: adopt CachedModel",
        author="Alice <alice@odoo.com>",
    )
    workspace = tmp_path / "ws"
    _write_config(workspace, repo.bare)
    config = config_mod.load(workspace)
    run_pipeline(config, state_mod.load(), watchlist_mod.load(workspace))
    ledger_update(workspace, config)
    return workspace


# --- prompt rendering ------------------------------------------------------


def test_user_prompt_contains_symbol_and_examples():
    data = UserPromptInput(
        symbol="odoo.orm.models_cached.CachedModel",
        kind=__import__("ofd.events.record", fromlist=["Kind"]).Kind.NEW_PUBLIC_CLASS,
        active_version="20.0",
        definition_subject="[ADD] orm: introduce CachedModel",
        definition_body="",
        rollout_examples=[
            ("addons/website/models/website.py", "class Website(models.Model):", "class Website(models.CachedModel):"),
        ],
    )
    out = render_user_prompt(data)
    assert "odoo.orm.models_cached.CachedModel" in out
    assert "class Website(models.Model)" in out
    assert "class Website(models.CachedModel)" in out
    assert "Write the paragraph now." in out


# --- runner ----------------------------------------------------------------


def test_runner_writes_narrative_into_existing_file(tmp_path: Path, monkeypatch):
    workspace = _seed(tmp_path, monkeypatch)
    config = config_mod.load(workspace)
    backend = _MockBackend(
        "The old _sql_constraints pattern hard-coded messages. "
        "The new class matches field syntax and supports dynamic messages."
    )
    result = narrate_all(workspace, config, backend=backend)
    assert "odoo.orm.models_cached.CachedModel" in result.written

    path = workspace / "ledger" / "new-apis" / "odoo.orm.models_cached.CachedModel.md"
    content = path.read_text()
    assert "old _sql_constraints pattern hard-coded" in content
    # Stored prompt version marker.
    assert "narrated_prompt_version: 1" in content


def test_runner_skips_when_narrative_already_present(tmp_path: Path, monkeypatch):
    workspace = _seed(tmp_path, monkeypatch)
    config = config_mod.load(workspace)
    backend = _MockBackend("first narration")
    narrate_all(workspace, config, backend=backend)
    backend2 = _MockBackend("second narration")
    result = narrate_all(workspace, config, backend=backend2)
    assert result.written == []
    assert any("already present" in s for s in result.skipped)


def test_runner_force_overrides_existing(tmp_path: Path, monkeypatch):
    workspace = _seed(tmp_path, monkeypatch)
    config = config_mod.load(workspace)
    narrate_all(workspace, config, backend=_MockBackend("NARRATIVE_ONE"))
    narrate_all(workspace, config, backend=_MockBackend("NARRATIVE_TWO"), force=True)
    path = workspace / "ledger" / "new-apis" / "odoo.orm.models_cached.CachedModel.md"
    content = path.read_text()
    assert "NARRATIVE_TWO" in content
    assert "NARRATIVE_ONE" not in content


def test_runner_dry_run_does_not_call_backend(tmp_path: Path, monkeypatch):
    workspace = _seed(tmp_path, monkeypatch)
    config = config_mod.load(workspace)
    backend = _MockBackend("should never be called")
    result = narrate_all(workspace, config, backend=backend, dry_run=True)
    assert backend.calls == []
    assert result.written == []
    assert any("dry-run" in s for s in result.skipped)


def test_runner_symbol_filter(tmp_path: Path, monkeypatch):
    workspace = _seed(tmp_path, monkeypatch)
    config = config_mod.load(workspace)
    backend = _MockBackend("narrated")
    result = narrate_all(
        workspace, config, backend=backend,
        symbol_filter="odoo.orm.models_cached.CachedModel",
    )
    assert result.written == ["odoo.orm.models_cached.CachedModel"]


def test_runner_min_rollouts_gate(tmp_path: Path, monkeypatch):
    workspace = _seed(tmp_path, monkeypatch)
    config = config_mod.load(workspace)
    backend = _MockBackend("unused")
    result = narrate_all(workspace, config, backend=backend, min_rollouts=10)
    assert result.written == []
    assert any("min_rollouts" in s for s in result.skipped)


# --- JSON parsing of `claude -p --output-format json` ---------------------


def test_extract_text_from_result_key():
    out = _extract_text_from_cc_json('{"result": "hello world"}')
    assert out == "hello world"


def test_extract_text_from_content_blocks():
    payload = (
        '{"messages": [{"role": "assistant", '
        '"content": [{"type": "text", "text": "The paragraph."}]}]}'
    )
    assert _extract_text_from_cc_json(payload) == "The paragraph."


def test_extract_text_empty_raises():
    with pytest.raises(NarrateError):
        _extract_text_from_cc_json("")


def test_extract_text_bad_json_raises():
    with pytest.raises(NarrateError):
        _extract_text_from_cc_json("not json")


def test_extract_text_unexpected_shape_raises():
    with pytest.raises(NarrateError):
        _extract_text_from_cc_json('{"unrelated": "thing"}')
