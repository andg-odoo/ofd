from pathlib import Path

from ofd.state import RepoState, State, load, save


def test_state_defaults_and_get(tmp_path: Path):
    s = State()
    assert s.get("odoo").last_seen_sha is None
    s.get("odoo").last_seen_sha = "abc"
    assert s.get("odoo").last_seen_sha == "abc"


def test_save_and_load_roundtrip(tmp_path: Path):
    target = tmp_path / "state.json"
    s = State(repos={"odoo": RepoState(last_seen_sha="abc", last_run_at="2026-04-17T00:00:00Z")})
    save(s, target)
    got = load(target)
    assert got.repos["odoo"].last_seen_sha == "abc"
    assert got.repos["odoo"].last_run_at == "2026-04-17T00:00:00Z"


def test_load_missing_returns_empty(tmp_path: Path):
    got = load(tmp_path / "does-not-exist.json")
    assert got.repos == {}
