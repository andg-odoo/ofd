"""Tests for the `ofd watchlist` CLI commands."""

from pathlib import Path

from click.testing import CliRunner

from ofd import watchlist as wl_mod
from ofd.cli.watchlist_cmd import watchlist_cli
from ofd.events.record import ChangeRecord, CommitEnvelope, CommitRecord, Kind
from ofd.events.store import write


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.yaml").write_text(
        "repos:\n"
        "  odoo:\n"
        "    source: /dev/null\n"
        "    mirror: /dev/null\n"
        "    branch: master\n"
        "    framework_paths: []\n"
        "active_version: master\n"
    )
    return ws


def _seed_definition_raw(ws: Path) -> None:
    """Drop one raw file with a NEW_VIEW_ATTRIBUTE event carrying element."""
    write(ws, CommitRecord(
        commit=CommitEnvelope(
            sha="a" * 40, repo="odoo", branch="master",
            active_version="master",
            author_name="Dev", author_email="dev@odoo.com",
            committed_at="2026-03-05T20:47:09Z",
            subject="[IMP] widget invisible support", body="",
        ),
        changes=[ChangeRecord(
            kind=Kind.NEW_VIEW_ATTRIBUTE,
            file="odoo/addons/base/rng/common.rng", line=432,
            symbol="odoo.addons.base.rng.common.widget.invisible",
            attribute="invisible", element="widget",
        )],
    ))


def test_watchlist_rebuild_populates_element_from_raws(tmp_path: Path):
    """After changing the WatchlistEntry schema (adding `element`), a
    rebuild picks it up from the existing raw store without needing a
    full reindex."""
    ws = _workspace(tmp_path)
    _seed_definition_raw(ws)

    # Simulate a pre-schema watchlist - entry exists but element is None.
    pre = wl_mod.Watchlist()
    pre.entries["odoo.addons.base.rng.common.widget.invisible"] = wl_mod.WatchlistEntry(
        symbol="odoo.addons.base.rng.common.widget.invisible",
        short_name="invisible", kind=Kind.NEW_VIEW_ATTRIBUTE,
        repo="odoo", file="odoo/addons/base/rng/common.rng",
        first_seen_sha="a"*40, first_seen_at="2026-03-05T20:47:09Z",
        active_version="master", element=None,
    )
    wl_mod.save(pre, ws)

    result = CliRunner().invoke(watchlist_cli, ["rebuild", "--workspace", str(ws)])
    assert result.exit_code == 0, result.output
    assert "rebuilt watchlist" in result.output

    after = wl_mod.load(ws)
    entry = after.entries["odoo.addons.base.rng.common.widget.invisible"]
    assert entry.element == "widget"


def test_watchlist_rebuild_prunes_orphan_rollouts_from_raws(tmp_path: Path):
    """If a symbol drops out of the watchlist (e.g. `odoo.fields.Integer`
    was auto-added by an older unbounded walk), its rollouts linger in
    the raw store and leak into the ledger. `rebuild` must drop them."""
    ws = _workspace(tmp_path)
    _seed_definition_raw(ws)

    # Simulate a stale raw from a previous walk: has a rollout for a
    # symbol whose definition isn't in the raw store (and so won't be
    # carried forward by the rebuild).
    write(ws, CommitRecord(
        commit=CommitEnvelope(
            sha="b" * 40, repo="odoo", branch="master",
            active_version="master",
            author_name="Dev", author_email="dev@odoo.com",
            committed_at="2026-04-01T00:00:00Z",
            subject="[IMP] adopt", body="",
        ),
        changes=[ChangeRecord(
            kind=Kind.ROLLOUT, file="addons/x/y.py", line=1,
            symbol="odoo.fields.Integer",  # orphan post-rebuild
        )],
    ))

    result = CliRunner().invoke(watchlist_cli, ["rebuild", "--workspace", str(ws)])
    assert result.exit_code == 0, result.output
    assert "pruned orphan rollouts" in result.output
    # The orphan-only raw should be gone.
    assert not (ws / "raw" / "odoo" / ("b" * 40 + ".json")).exists()
    # The definition raw should still be there.
    assert (ws / "raw" / "odoo" / ("a" * 40 + ".json")).exists()


def test_watchlist_rebuild_preserves_manual_pins(tmp_path: Path):
    """Manual pins have no backing raw definition - rebuild must keep them."""
    ws = _workspace(tmp_path)
    _seed_definition_raw(ws)

    pre = wl_mod.Watchlist()
    pre.add_manual(
        symbol="formatted_display_name",
        active_version="master",
        note="context key",
    )
    wl_mod.save(pre, ws)

    result = CliRunner().invoke(watchlist_cli, ["rebuild", "--workspace", str(ws)])
    assert result.exit_code == 0, result.output

    after = wl_mod.load(ws)
    assert "formatted_display_name" in after.entries
    assert after.entries["formatted_display_name"].source == "manual"
    # The auto-extracted entry should also be present now.
    assert "odoo.addons.base.rng.common.widget.invisible" in after.entries
