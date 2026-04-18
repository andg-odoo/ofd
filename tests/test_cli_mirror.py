"""Tests for the mirror CLI.

We don't network-clone - we fake a source by turning a local bare repo
into the `source` URL, which git accepts as a filesystem remote.
"""

from pathlib import Path

from click.testing import CliRunner

from ofd.cli.mirror import mirror
from tests.fixtures.repo_builder import make_repo


def _workspace_with_local_source(tmp_path: Path, source_repo) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    mirror_path = tmp_path / "mirror.git"
    (workspace / "config.yaml").write_text(f"""\
repos:
  demo:
    source: {source_repo.bare}
    mirror: {mirror_path}
    branch: master
    framework_paths: ["*.py"]
active_version: "20.0"
""")
    return workspace


def test_mirror_init_clones_and_is_idempotent(tmp_path: Path):
    source = make_repo(tmp_path, name="source")
    source.commit({"a.py": "x = 1\n"}, subject="[ADD] a")

    workspace = _workspace_with_local_source(tmp_path, source)
    runner = CliRunner()

    result = runner.invoke(mirror, ["init", "--workspace", str(workspace)])
    assert result.exit_code == 0, result.output
    assert "cloned demo" in result.output
    # Second call: no-op.
    result2 = runner.invoke(mirror, ["init", "--workspace", str(workspace)])
    assert result2.exit_code == 0
    assert "already present" in result2.output


def test_mirror_status_shows_head(tmp_path: Path):
    source = make_repo(tmp_path, name="source")
    source.commit({"a.py": "x = 1\n"}, subject="[ADD] a")
    workspace = _workspace_with_local_source(tmp_path, source)
    runner = CliRunner()
    runner.invoke(mirror, ["init", "--workspace", str(workspace)])

    result = runner.invoke(mirror, ["status", "--workspace", str(workspace)])
    assert result.exit_code == 0
    assert "demo:" in result.output
    assert "head=" in result.output
    assert "MiB" in result.output


def test_mirror_fetch_pulls_new_commits(tmp_path: Path):
    source = make_repo(tmp_path, name="source")
    source.commit({"a.py": "x = 1\n"}, subject="[ADD] a")
    workspace = _workspace_with_local_source(tmp_path, source)
    runner = CliRunner()
    runner.invoke(mirror, ["init", "--workspace", str(workspace)])

    source.commit({"b.py": "y = 2\n"}, subject="[ADD] b")
    result = runner.invoke(mirror, ["fetch", "--workspace", str(workspace)])
    assert result.exit_code == 0, result.output
    assert "fetched demo" in result.output


def test_mirror_reset_requires_confirmation_or_yes(tmp_path: Path):
    source = make_repo(tmp_path, name="source")
    source.commit({"a.py": "x = 1\n"}, subject="[ADD] a")
    workspace = _workspace_with_local_source(tmp_path, source)
    runner = CliRunner()
    runner.invoke(mirror, ["init", "--workspace", str(workspace)])

    result = runner.invoke(
        mirror, ["reset", "demo", "--workspace", str(workspace), "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "re-cloned demo" in result.output


def test_mirror_reset_unknown_repo_exits_nonzero(tmp_path: Path):
    source = make_repo(tmp_path, name="source")
    source.commit({"a.py": "x = 1\n"}, subject="[ADD] a")
    workspace = _workspace_with_local_source(tmp_path, source)
    runner = CliRunner()
    result = runner.invoke(
        mirror, ["reset", "nonexistent", "--workspace", str(workspace), "--yes"]
    )
    assert result.exit_code != 0
