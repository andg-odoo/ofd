"""Tests for `ofd query`, `ofd commits`, `ofd rollouts`, `ofd reindex`."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from ofd import config as config_mod
from ofd import state as state_mod
from ofd import watchlist as watchlist_mod
from ofd.cli.commits import commits as commits_cmd
from ofd.cli.query import query
from ofd.cli.reindex import reindex
from ofd.cli.rollouts import rollouts as rollouts_cmd
from ofd.pipeline import run as run_pipeline
from tests.fixtures.repo_builder import make_repo


def _workspace(tmp_path: Path, mirror: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.yaml").write_text(f"""\
repos:
  odoo:
    source: /dev/null
    mirror: {mirror}
    branch: master
    framework_paths: [odoo/orm/**/*.py]
    core_paths: [odoo/orm/**/*.py]
active_version: "20.0"
key_devs: []
""")
    return ws


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
        author="A <a@example.com>",
    )
    repo.commit(
        {
            "odoo/orm/models_cached.py": (
                '"""Cached."""\nclass CachedModel:\n    _cached_data_fields=()\n'
            ),
        },
        subject="[ADD] orm: introduce CachedModel",
        author="A <a@example.com>",
    )
    repo.commit(
        {
            "addons/website/models/website.py": (
                "from odoo import models\nclass Website(models.CachedModel):\n    _name='w'\n"
            ),
        },
        subject="[IMP] website: adopt CachedModel",
        author="A <a@example.com>",
    )

    ws = _workspace(tmp_path, repo.bare)
    config = config_mod.load(ws)
    run_pipeline(config, state_mod.load(), watchlist_mod.load(ws))
    return ws


def test_query_outputs_table_by_default(tmp_path: Path, monkeypatch):
    ws = _seed(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(query, ["--workspace", str(ws)])
    assert result.exit_code == 0, result.output
    assert "CachedModel" in result.output


def test_query_json_output(tmp_path: Path, monkeypatch):
    ws = _seed(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(query, ["--workspace", str(ws), "--as-json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert any("CachedModel" in (e.get("symbol") or "") for e in data)


def test_query_filter_by_kind(tmp_path: Path, monkeypatch):
    ws = _seed(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        query, ["--workspace", str(ws), "--kind", "rollout", "--as-json"]
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data
    assert all(e["kind"] == "rollout" for e in data)


def test_query_filter_by_symbol_substring(tmp_path: Path, monkeypatch):
    ws = _seed(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        query, ["--workspace", str(ws), "--symbol", "CachedModel", "--as-json"]
    )
    data = json.loads(result.output)
    assert data
    assert all("CachedModel" in (e["symbol"] or "") for e in data)


def test_commits_lists_definition_and_rollouts(tmp_path: Path, monkeypatch):
    ws = _seed(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(commits_cmd, ["--workspace", str(ws), "CachedModel"])
    assert result.exit_code == 0
    assert "definition" in result.output
    assert "rollout" in result.output


def test_commits_kind_filter(tmp_path: Path, monkeypatch):
    ws = _seed(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        commits_cmd, ["--workspace", str(ws), "--kind", "definition", "CachedModel"]
    )
    assert result.exit_code == 0
    assert "definition" in result.output
    assert "rollout" not in result.output


def test_rollouts_shows_files(tmp_path: Path, monkeypatch):
    ws = _seed(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(rollouts_cmd, ["--workspace", str(ws), "CachedModel"])
    assert result.exit_code == 0
    assert "addons/website/models/website.py" in result.output


def test_rollouts_diff_includes_hunks(tmp_path: Path, monkeypatch):
    ws = _seed(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        rollouts_cmd, ["--workspace", str(ws), "--diff", "CachedModel"]
    )
    assert result.exit_code == 0
    assert "Before:" in result.output
    assert "After:" in result.output


def test_reindex_rewrites_raw_events(tmp_path: Path, monkeypatch):
    ws = _seed(tmp_path, monkeypatch)
    # Delete raw/ to simulate corruption; reindex should repopulate.
    raw_dir = ws / "raw" / "odoo"
    for p in raw_dir.glob("*.json"):
        p.unlink()
    assert not list(raw_dir.glob("*.json"))
    runner = CliRunner()
    result = runner.invoke(reindex, ["--workspace", str(ws)])
    assert result.exit_code == 0, result.output
    assert list(raw_dir.glob("*.json"))
