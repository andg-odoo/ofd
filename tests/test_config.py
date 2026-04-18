from pathlib import Path

import pytest

from ofd.config import DEFAULT_CONFIG_YAML, load, resolve_workspace


def test_resolve_workspace_explicit(tmp_path: Path):
    assert resolve_workspace(str(tmp_path)) == tmp_path.resolve()


def test_resolve_workspace_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OFD_WORKSPACE", str(tmp_path))
    assert resolve_workspace() == tmp_path.resolve()


def test_resolve_workspace_pointer(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OFD_WORKSPACE", raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    pointer_dir = fake_home / ".config" / "ofd"
    pointer_dir.mkdir(parents=True)
    workspace_target = tmp_path / "ws"
    workspace_target.mkdir()
    (pointer_dir / "workspace").write_text(str(workspace_target) + "\n")
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    assert resolve_workspace() == workspace_target.resolve()


def test_load_default_yaml(tmp_path: Path):
    (tmp_path / "config.yaml").write_text(DEFAULT_CONFIG_YAML)
    cfg = load(tmp_path)
    assert cfg.active_version == "20.0"
    assert {r.name for r in cfg.repos} == {"odoo", "enterprise"}
    odoo = cfg.repo("odoo")
    assert odoo.branch == "master"
    assert "odoo/fields.py" in odoo.framework_paths
    assert "odoo/fields.py" in odoo.core_paths
    assert cfg.narrate.backend == "claude_code"


def test_load_missing_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load(tmp_path)
