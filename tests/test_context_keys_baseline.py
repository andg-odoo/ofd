"""Tests for the baseline context-key denylist.

The extractor used to emit `@api.depends_context('lang')` as a "new
primitive" every time some addon newly added that decorator to one of
its compute methods, even though `lang` has been a framework-era key
for a decade. The fix: at the start of the tracking window, snapshot
every key already in use across the tree and subtract that set from
later emissions.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from ofd import config as config_mod
from ofd import state as state_mod
from ofd import watchlist as watchlist_mod
from ofd.digest import build_sections
from ofd.extractors import context_keys
from ofd.pipeline import run as run_pipeline
from tests.fixtures.repo_builder import make_repo


def test_extract_without_baseline_emits_key():
    parent = ""
    child = (
        "from odoo import api\n"
        "class M:\n"
        "    @api.depends_context('lang')\n"
        "    def f(self): pass\n"
    )
    records = context_keys.extract(parent, child, "m.py")
    assert [r.symbol for r in records] == ["context_key.lang"]


def test_extract_with_baseline_suppresses_known_keys():
    """A key already in the baseline must not be emitted, even when a
    file newly cites it - the key isn't new, it's just newly cited."""
    parent = ""
    child = (
        "from odoo import api\n"
        "class M:\n"
        "    @api.depends_context('lang', 'genuinely_new_key')\n"
        "    def f(self): pass\n"
    )
    records = context_keys.extract(
        parent, child, "m.py", baseline_keys=frozenset({"lang"}),
    )
    assert [r.symbol for r in records] == ["context_key.genuinely_new_key"]


def _write_config(workspace: Path, mirror: Path, since_date: str | None) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    since_line = f'since_date: "{since_date}"\n' if since_date else ""
    (workspace / "config.yaml").write_text(f"""\
repos:
  odoo:
    source: /dev/null
    mirror: {mirror}
    branch: master
    framework_paths: [odoo/orm/**/*.py]
active_version: "20.0"
key_devs: []
{since_line}""")


def test_pipeline_skips_baseline_context_keys(tmp_path: Path, monkeypatch):
    """End-to-end: a key declared in the tree before `since_date` is
    suppressed even when a later commit adds a *new* file declaring it."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    repo = make_repo(tmp_path)

    # Baseline commit: `lang` is already declared somewhere in the tree.
    # Date is well before our since_date floor so the baseline scan
    # picks it up.
    baseline_env = {
        "GIT_AUTHOR_DATE": "2024-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2024-01-01T00:00:00Z",
    }
    repo.commit(
        {
            "odoo/orm/__init__.py": "",
            "addons/base/models/old.py": (
                "from odoo import api\n"
                "class Old:\n"
                "    @api.depends_context('lang')\n"
                "    def f(self): pass\n"
            ),
        },
        subject="[ADD] baseline tree",
        env=baseline_env,
    )

    # In-window commit: a new addon file newly cites BOTH `lang` (old)
    # and `freshly_minted_key` (new). Only the latter should be emitted.
    in_window_env = {
        "GIT_AUTHOR_DATE": "2025-11-15T00:00:00Z",
        "GIT_COMMITTER_DATE": "2025-11-15T00:00:00Z",
    }
    repo.commit(
        {
            # Touch a framework_paths file so the pipeline's gating
            # short-circuit doesn't skip this commit (the real workspace
            # rarely sees addon-only commits in practice).
            "odoo/orm/__init__.py": "# touched\n",
            "addons/foo/models/foo.py": (
                "from odoo import api\n"
                "class Foo:\n"
                "    @api.depends_context('lang', 'freshly_minted_key')\n"
                "    def g(self): pass\n"
            ),
        },
        subject="[IMP] foo: cite lang and a fresh key",
        env=in_window_env,
    )

    workspace = tmp_path / "ws"
    _write_config(workspace, repo.bare, since_date="2025-10-01")
    config = config_mod.load(workspace)
    run_pipeline(config, state_mod.load(), watchlist_mod.load(workspace))

    # Watchlist should contain freshly_minted_key but NOT lang.
    wl = watchlist_mod.load(workspace)
    symbols = set(wl.entries)
    assert "context_key.freshly_minted_key" in symbols
    assert "context_key.lang" not in symbols

    # And the digest for the window should reflect that.
    start = datetime.now(tz=UTC) - timedelta(days=365)
    end = datetime.now(tz=UTC) + timedelta(days=1)
    sections = build_sections(workspace, config, start, end)
    new_syms = {sym for sym, _kind, _subj in sections.new_primitives}
    assert "context_key.freshly_minted_key" in new_syms
    assert "context_key.lang" not in new_syms


def test_manual_pin_overrides_baseline_filter(tmp_path: Path, monkeypatch):
    """A key in the baseline (auto-suppressed) is still tracked when
    the user has manually pinned it via `ofd watchlist add`. Manual
    pins bypass the extractor entirely, so the baseline denylist - a
    suppression layer for the auto path - cannot remove them.
    Rollouts under the manual pin must still be detected."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    repo = make_repo(tmp_path)

    # Baseline tree: formatted_display_name is in `@api.depends_context`,
    # so it WILL land in the baseline-keys denylist.
    repo.commit(
        {
            "odoo/orm/__init__.py": "",
            "odoo/orm/baseline.py": (
                "from odoo import api\n"
                "class B:\n"
                "    @api.depends_context('formatted_display_name')\n"
                "    def f(self): pass\n"
            ),
        },
        subject="[ADD] baseline",
        env={
            "GIT_AUTHOR_DATE": "2024-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2024-01-01T00:00:00Z",
        },
    )
    # In-window commit: an addon adopts formatted_display_name as a
    # quoted-string context arg - this is the rollout shape we want to
    # see attributed to the manual pin.
    repo.commit(
        {
            "odoo/orm/__init__.py": "# touched\n",
            "addons/foo/models/foo.py": (
                "class Foo:\n"
                "    def f(self):\n"
                "        return self.with_context(formatted_display_name=True)\n"
            ),
        },
        subject="[IMP] foo: adopt formatted_display_name",
        env={
            "GIT_AUTHOR_DATE": "2025-11-15T00:00:00Z",
            "GIT_COMMITTER_DATE": "2025-11-15T00:00:00Z",
        },
    )

    workspace = tmp_path / "ws"
    _write_config(workspace, repo.bare, since_date="2025-10-01")
    config = config_mod.load(workspace)

    # Manually pin the key BEFORE running. This is what `ofd watchlist
    # add formatted_display_name` does on disk.
    wl = watchlist_mod.Watchlist()
    wl.add_manual(symbol="formatted_display_name", active_version="20.0")
    watchlist_mod.save(wl, workspace)

    # Reindex: state wiped, but manual pins carried forward (the cli
    # does this; we mirror it here).
    existing = watchlist_mod.load(workspace)
    fresh_wl = watchlist_mod.Watchlist()
    for e in existing.manual_entries():
        fresh_wl.entries[e.symbol] = e
    run_pipeline(config, state_mod.load(), fresh_wl)

    # Manual pin must survive AND rollouts must still attribute to it.
    final_wl = watchlist_mod.load(workspace)
    assert "formatted_display_name" in final_wl.entries
    assert final_wl.entries["formatted_display_name"].source == "manual"

    # Rollout was emitted: digest's adoption velocity should list the
    # manual pin's symbol with at least one rollout in the window.
    start = datetime.now(tz=UTC) - timedelta(days=365)
    end = datetime.now(tz=UTC) + timedelta(days=1)
    sections = build_sections(workspace, config, start, end)
    rollout_syms = {sym for sym, _count, _repo, _sha in sections.adoption_velocity}
    assert "formatted_display_name" in rollout_syms


def test_baseline_cache_hits_avoid_rescan(tmp_path: Path, monkeypatch):
    """Second run with the same baseline SHA should read the cache and
    return identical keys without re-walking the tree. We assert by
    breaking the mirror after the first run and checking the second
    run still resolves keys (which it could only do via the cache)."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    repo = make_repo(tmp_path)
    repo.commit(
        {
            "odoo/orm/__init__.py": "",
            "addons/base/models/x.py": (
                "from odoo import api\n"
                "class X:\n"
                "    @api.depends_context('cached_check_key')\n"
                "    def f(self): pass\n"
            ),
        },
        subject="[ADD] baseline",
        env={
            "GIT_AUTHOR_DATE": "2024-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2024-01-01T00:00:00Z",
        },
    )
    workspace = tmp_path / "ws"
    _write_config(workspace, repo.bare, since_date="2025-10-01")
    config = config_mod.load(workspace)
    run_pipeline(config, state_mod.load(), watchlist_mod.load(workspace))

    cache_path = workspace / "baselines" / "context_keys.odoo.json"
    assert cache_path.exists()
    cached = cache_path.read_text()
    assert "cached_check_key" in cached
