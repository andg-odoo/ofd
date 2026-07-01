"""Tests for ledger read-side helpers (iter_entries, find)."""

from pathlib import Path

from click.testing import CliRunner

from ofd.cli.list_cmd import list_cmd
from ofd.cli.show import show
from ofd.ledger.read import find, iter_entries


def _write_entry(
    workspace: Path,
    subdir: str,
    symbol: str,
    body_extra: str = "",
    kind: str = "new_public_class",
    score: int = 5,
    rollout_count: int = 3,
    extra_fm: str = "",
) -> None:
    path = workspace / "ledger" / subdir / f"{symbol}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""---
symbol: {symbol}
kind: {kind}
active_version: "20.0"
status: fresh
score: {score}
rollout_count: {rollout_count}
first_seen: "2026-01-15"
{extra_fm}---

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


def test_moved_frontmatter_read_by_entry(tmp_path: Path):
    _write_entry(
        tmp_path, "new-apis", "odoo.tools.business_data.split_vat",
        kind="new_decorator_or_helper",
        extra_fm="moved: true\nmoved_from: odoo.tools.legacy.split_vat\n",
    )
    entries = iter_entries(tmp_path)
    entry = next(e for e in entries if e.symbol.endswith("split_vat"))
    assert entry.moved is True
    assert entry.moved_from == "odoo.tools.legacy.split_vat"


def test_dev_sort_ranks_extension_points_over_churn_and_moves(tmp_path: Path):
    # A widely-adopted relocation (high score + rollouts) must rank below
    # a fresh, zero-rollout mixin under the dev sort.
    _write_entry(
        tmp_path, "new-apis", "odoo.tools.business_data.split_vat",
        kind="new_decorator_or_helper", score=5, rollout_count=68,
        extra_fm="moved: true\n",
    )
    _write_entry(
        tmp_path, "new-apis", "odoo.addons.bus.models.BusSyncMixin",
        kind="new_public_class", score=3, rollout_count=0,
    )
    _write_entry(
        tmp_path, "new-apis", "odoo.orm.SomeMethod",
        kind="signature_change", score=4, rollout_count=10,
    )
    runner = CliRunner()
    result = runner.invoke(
        list_cmd,
        ["--workspace", str(tmp_path), "--sort", "dev", "--symbol-only"],
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.strip().splitlines() if line]
    # Mixin (tier 0) first, signature_change (tier 3) next, moved last -
    # despite the move having the highest score and rollout count.
    assert lines == [
        "odoo.addons.bus.models.BusSyncMixin",
        "odoo.orm.SomeMethod",
        "odoo.tools.business_data.split_vat",
    ]


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


def test_show_cli_no_pager_flag_accepted(tmp_path: Path):
    """`--no-pager` is the escape hatch for the pager-by-default behavior;
    with stdout piped (the CliRunner case), the pager branch is bypassed
    anyway, so this just verifies the flag is wired and doesn't crash."""
    _write_entry(tmp_path, "new-apis", "odoo.orm.CachedModel", body_extra="x")
    runner = CliRunner()
    result = runner.invoke(
        show, ["--workspace", str(tmp_path), "--no-pager", "CachedModel"]
    )
    assert result.exit_code == 0
    assert "odoo.orm.CachedModel" in result.output
