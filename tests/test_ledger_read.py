"""Tests for ledger read-side helpers (iter_entries, find)."""

from pathlib import Path

from click.testing import CliRunner

from ofd.cli.list_cmd import list_cmd
from ofd.cli.show import show
from ofd.ledger.read import find, iter_entries


def _write_entry(workspace: Path, subdir: str, symbol: str, body_extra: str = "") -> None:
    path = workspace / "ledger" / subdir / f"{symbol}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""---
symbol: {symbol}
kind: new_public_class
active_version: "20.0"
status: fresh
score: 5
rollout_count: 3
first_seen: "2026-01-15"
---

# {symbol.rsplit('.', 1)[-1]}

{body_extra}
"""
    )


def test_iter_entries_walks_all_subdirs(tmp_path: Path):
    _write_entry(tmp_path, "new-apis", "odoo.orm.Alpha")
    _write_entry(tmp_path, "new-apis", "odoo.orm.Beta")
    _write_entry(tmp_path, "deprecations", "odoo.osv.expression.AND")
    entries = iter_entries(tmp_path)
    symbols = {e.symbol for e in entries}
    assert symbols == {"odoo.orm.Alpha", "odoo.orm.Beta", "odoo.osv.expression.AND"}


def test_find_exact_and_suffix(tmp_path: Path):
    _write_entry(tmp_path, "new-apis", "odoo.orm.CachedModel")
    exact = find(tmp_path, "odoo.orm.CachedModel")
    assert exact is not None
    suffix = find(tmp_path, "CachedModel")
    assert suffix is not None and suffix.symbol == "odoo.orm.CachedModel"
    assert find(tmp_path, "Nope") is None


def test_list_cli_prints_one_line_per_entry(tmp_path: Path):
    _write_entry(tmp_path, "new-apis", "odoo.orm.Alpha")
    _write_entry(tmp_path, "new-apis", "odoo.orm.Beta")
    runner = CliRunner()
    result = runner.invoke(list_cmd, ["--workspace", str(tmp_path)])
    assert result.exit_code == 0
    # Score column first, symbol last.
    assert "odoo.orm.Alpha" in result.output
    assert "odoo.orm.Beta" in result.output


def test_list_cli_symbol_only(tmp_path: Path):
    _write_entry(tmp_path, "new-apis", "odoo.orm.Alpha")
    _write_entry(tmp_path, "new-apis", "odoo.orm.Beta")
    runner = CliRunner()
    result = runner.invoke(list_cmd, ["--workspace", str(tmp_path), "--symbol-only"])
    assert result.exit_code == 0
    lines = [line for line in result.output.strip().splitlines() if line]
    assert lines == ["odoo.orm.Alpha", "odoo.orm.Beta"]


def test_list_cli_filter_by_status(tmp_path: Path):
    # Default-written entries are "fresh".
    path = tmp_path / "ledger" / "new-apis" / "odoo.orm.Dormant.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """---
symbol: odoo.orm.Dormant
kind: new_public_class
active_version: "20.0"
status: dormant
score: 2
rollout_count: 0
first_seen: "2024-01-01"
---
body
"""
    )
    _write_entry(tmp_path, "new-apis", "odoo.orm.Fresh")
    runner = CliRunner()
    result = runner.invoke(
        list_cmd, ["--workspace", str(tmp_path), "--status", "fresh", "--symbol-only"]
    )
    assert result.exit_code == 0
    assert result.output.strip() == "odoo.orm.Fresh"


def test_show_cli_prints_file(tmp_path: Path):
    _write_entry(tmp_path, "new-apis", "odoo.orm.CachedModel", body_extra="Sample body.")
    runner = CliRunner()
    result = runner.invoke(show, ["--workspace", str(tmp_path), "CachedModel"])
    assert result.exit_code == 0
    assert "Sample body." in result.output
    assert "odoo.orm.CachedModel" in result.output


def test_show_cli_missing_exits_nonzero(tmp_path: Path):
    (tmp_path / "ledger" / "new-apis").mkdir(parents=True)
    runner = CliRunner()
    result = runner.invoke(show, ["--workspace", str(tmp_path), "Nope"])
    assert result.exit_code != 0
    combined = result.output + (result.stderr or "")
    assert "no symbol matching" in combined


def test_show_cli_path_flag(tmp_path: Path):
    _write_entry(tmp_path, "new-apis", "odoo.orm.CachedModel")
    runner = CliRunner()
    result = runner.invoke(
        show, ["--workspace", str(tmp_path), "--path", "CachedModel"]
    )
    assert result.exit_code == 0
    assert "odoo.orm.CachedModel.md" in result.output
