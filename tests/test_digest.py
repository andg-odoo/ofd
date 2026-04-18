"""Tests for the daily digest renderer."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from ofd import config as config_mod
from ofd import state as state_mod
from ofd import watchlist as watchlist_mod
from ofd.digest import build_and_render, build_sections, render
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
    framework_paths: [odoo/orm/**/*.py, odoo/osv/**/*.py]
    core_paths: [odoo/orm/**/*.py]
active_version: "20.0"
key_devs: []
""")


def _seed(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    repo = make_repo(tmp_path)
    repo.commit(
        {
            "odoo/orm/__init__.py": "",
            "odoo/orm/models_cached.py": '"""stub."""\n',
            "odoo/osv/__init__.py": "",
            "odoo/osv/expression.py": "def AND(domains): return domains\n",
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
            "odoo/osv/expression.py": (
                "import warnings\n\n"
                "def AND(domains):\n"
                "    warnings.warn('AND is deprecated, use Domain.AND; removed in 21.0', DeprecationWarning)\n"
                "    return domains\n"
            ),
        },
        subject="[IMP] osv: deprecate AND",
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

    workspace = tmp_path / "ws"
    _write_config(workspace, repo.bare)
    config = config_mod.load(workspace)
    run_pipeline(config, state_mod.load(), watchlist_mod.load(workspace))
    return workspace


def test_digest_collects_all_three_sections(tmp_path: Path, monkeypatch):
    workspace = _seed(tmp_path, monkeypatch)
    config = config_mod.load(workspace)

    start = datetime.now(tz=UTC) - timedelta(days=30)
    end = datetime.now(tz=UTC) + timedelta(days=1)
    sections = build_sections(workspace, config, start, end)

    assert any(
        sym == "odoo.orm.models_cached.CachedModel"
        for sym, _kind, _subj in sections.new_primitives
    )
    assert any(
        sym == "odoo.orm.models_cached.CachedModel"
        for sym, _count, _sha in sections.adoption_velocity
    )
    assert any(
        removal == "21.0" for _hint, removal, _warn in sections.deprecations
    )


def test_digest_renders_markdown_with_headers(tmp_path: Path, monkeypatch):
    workspace = _seed(tmp_path, monkeypatch)
    config = config_mod.load(workspace)

    start = datetime.now(tz=UTC) - timedelta(days=30)
    end = datetime.now(tz=UTC) + timedelta(days=1)
    sections = build_sections(workspace, config, start, end)
    content = render(sections, date(2026, 4, 17))
    assert "# Digest - 2026-04-17" in content
    assert "## New primitives" in content
    assert "## Adoption velocity" in content
    assert "## Deprecations" in content
    assert "CachedModel" in content


def test_digest_empty_window_shows_placeholders(tmp_path: Path, monkeypatch):
    workspace = _seed(tmp_path, monkeypatch)
    config = config_mod.load(workspace)

    # Look at a window in the distant past - no events.
    start = datetime(2000, 1, 1, tzinfo=UTC)
    end = datetime(2000, 1, 2, tzinfo=UTC)
    sections = build_sections(workspace, config, start, end)
    content = render(sections, date(2000, 1, 2))
    assert "_None._" in content or "_No new rollouts" in content


def test_build_and_render_writes_file(tmp_path: Path, monkeypatch):
    workspace = _seed(tmp_path, monkeypatch)
    config = config_mod.load(workspace)

    path, content = build_and_render(
        workspace, config, target_date=date(2026, 4, 17), window_days=30
    )
    assert path.exists()
    assert "# Digest - 2026-04-17" in path.read_text()
    assert path.read_text() == content
