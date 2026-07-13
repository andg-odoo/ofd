"""Tests for `ofd reindex --watchlist-changed` (pipeline.replay).

The replay re-runs ONLY the content-matcher rollout scan (rollouts.py,
pipeline stage 3) against the current on-disk watchlist, skipping
definition extraction entirely. The point is the pin-backfill workflow:
after `ofd watchlist add`, a manual pin's historical adoptions get
counted in a fraction of a full reindex.

The fixture builds two repos (odoo processed first, enterprise second)
whose raws exercise every record provenance and every watchlist-state
divergence the replay must reproduce:
  - a content-matcher rollout (website adopts the `frobnicate` helper);
  - an extractor-emitted rollout (a manifest adds `paper_muncher` to its
    depends: a module rollout with a nonzero line and NO hunk_header);
  - a commit adopting a not-yet-tracked symbol (`special_helper`), which
    has no raw at all until the symbol is pinned;
  - two entries sharing the short name `grok` with DIFFERENT first_seen
    dates, and an adopter commit between the two definitions - the
    shared-name first-wins attribution must credit the entry that
    existed at scan time, not the alphabetically-first member of the
    final group (the live-workspace `unsafe_policy` regression);
  - an adopter commit sharing its committer TIMESTAMP with the later
    definition commit of `blorp` - date comparison can't order the two,
    only walk-order activation by first_seen_sha can (the live
    `country_timezones` regression);
  - an odoo commit adopting `zap`, defined only in the LATER-ordered
    enterprise repo - such entries never exist during odoo's walk in a
    full reindex, no matter the dates (the live `active_ids` regression).
"""

from pathlib import Path

from ofd import config as config_mod
from ofd import state as state_mod
from ofd import watchlist as watchlist_mod
from ofd.events.record import Kind
from ofd.events.store import iter_repo, raw_path
from ofd.pipeline import replay as replay_pipeline
from ofd.pipeline import run as run_pipeline
from tests.fixtures.repo_builder import make_repo


def _write_config(workspace: Path, odoo_mirror: Path, ent_mirror: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "config.yaml").write_text(f"""\
repos:
  odoo:
    source: /dev/null
    mirror: {odoo_mirror}
    branch: master
    framework_paths:
      - odoo/orm/**/*.py
    core_paths: []
  enterprise:
    source: /dev/null
    mirror: {ent_mirror}
    branch: master
    framework_paths:
      - ent/**/*.py
    core_paths: []
active_version: "20.0"
since_date: "2025-06-01"
key_devs: []
scoring:
  thresholds: {{surface: 3, ledger_threshold: 4, narrate: 5}}
  breadth_bonuses:
    - {{min_rollouts: 5, bonus: 1}}
  dormant_days: 90
  fresh_days: 30
  intent_keywords: [introduce]
narrate:
  backend: claude_code
""")


def _dated(date: str) -> dict[str, str]:
    stamp = f"{date} +0000"
    return {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}


def _build_repos(tmp_path: Path):
    """Build the shared two-repo fixture; return (odoo, enterprise, shas)."""
    repo = make_repo(tmp_path)
    ent = make_repo(tmp_path, name="enterprise")

    # Pre-floor baselines. All manifest keys used later are present so the
    # manifest-key extractor stays silent. Dated before the config floor
    # -> not enumerated.
    repo.commit(
        {
            "odoo/orm/__init__.py": "",
            "odoo/orm/tools.py": '"""orm tools."""\n',
            "odoo/orm/zulu.py": '"""zulu."""\n',
            "odoo/orm/alpha.py": '"""alpha."""\n',
            "odoo/orm/blorp.py": '"""blorp."""\n',
            "addons/base/__manifest__.py":
                "{'name': 'Base', 'depends': [], 'category': 'Hidden'}\n",
            "addons/website/__manifest__.py":
                "{'name': 'Website', 'depends': ['base'], 'category': 'W'}\n",
            "addons/website/models/website.py":
                "from odoo import models\n\n"
                "class Website(models.Model):\n"
                "    _name = 'website'\n",
            "addons/foo/logic.py":
                "class Foo:\n"
                "    def run(self, record):\n"
                "        return record\n",
        },
        subject="[ADD] baseline",
        env=_dated("2025-01-15T12:00:00"),
    )
    ent.commit(
        {"ent/__init__.py": "", "ent/tools.py": '"""ent tools."""\n'},
        subject="[ADD] baseline",
        env=_dated("2025-01-15T12:00:00"),
    )

    # Enterprise defines `zap` EARLY (2025-06-20). In a full reindex the
    # odoo repo is walked first, so `zap` never exists during odoo's walk -
    # regardless of dates.
    ent.commit(
        {
            "ent/tools.py":
                '"""ent tools."""\n\n\ndef zap(value):\n    return value\n',
        },
        subject="[ADD] ent: introduce zap helper",
        env=_dated("2025-06-20T12:00:00"),
    )

    # Commit A: define the `frobnicate` helper (framework) AND ship a new
    # `paper_muncher` module. Both become watchlist primitives.
    def_sha = repo.commit(
        {
            "odoo/orm/tools.py":
                '"""orm tools."""\n\n\n'
                "def frobnicate(value):\n"
                "    return value\n",
            "addons/paper_muncher/__manifest__.py":
                "{'name': 'Paper Muncher', 'depends': ['base'],"
                " 'category': 'Technical'}\n",
        },
        subject="[ADD] orm: introduce frobnicate helper and paper_muncher",
        env=_dated("2025-07-01T12:00:00"),
    )

    # Commit B: website adopts frobnicate -> content-matcher rollout.
    content_sha = repo.commit(
        {
            "addons/website/models/website.py":
                "from odoo import models\n"
                "from odoo.orm.tools import frobnicate\n\n"
                "class Website(models.Model):\n"
                "    _name = 'website'\n\n"
                "    def act(self, vals):\n"
                "        return frobnicate(vals)\n",
        },
        subject="[IMP] website: use frobnicate",
        env=_dated("2025-07-02T12:00:00"),
    )

    # Commit C: an existing manifest adds paper_muncher to depends ->
    # extractor-emitted module rollout (nonzero line, no hunk_header).
    extractor_sha = repo.commit(
        {
            "addons/website/__manifest__.py":
                "{'name': 'Website', 'depends': ['base', 'paper_muncher'],"
                " 'category': 'W'}\n",
        },
        subject="[IMP] website: render through paper_muncher",
        env=_dated("2025-07-03T12:00:00"),
    )

    # Commit D: foo adopts `special_helper` - a symbol no extractor defines.
    # No rollout (hence no raw) until the symbol is manually pinned.
    pin_target_sha = repo.commit(
        {
            "addons/foo/logic.py":
                "class Foo:\n"
                "    def run(self, record):\n"
                "        return record.special_helper()\n",
        },
        subject="[IMP] foo: adopt special_helper",
        env=_dated("2025-07-04T12:00:00"),
    )

    # Commit E: define `grok` in zulu.py AND adopt it in an addon in the
    # SAME commit - the replay must activate the entry before the commit's
    # own rollout scan (stage 2 runs before stage 3).
    grok_def_sha = repo.commit(
        {
            "odoo/orm/zulu.py":
                '"""zulu."""\n\n\ndef grok(value):\n    return value\n',
            "addons/bar/stuff.py":
                "from odoo.orm.zulu import grok\n\n\n"
                "def process(x):\n"
                "    return grok(x)\n",
        },
        subject="[ADD] orm: introduce grok helper",
        env=_dated("2025-07-05T12:00:00"),
    )
    # Commit F: adopter between the two `grok` definitions. Ground truth
    # attributes this hunk to zulu's grok (the only entry alive at scan
    # time); a replay handed the final watchlist would let alpha's grok
    # (alphabetically first, defined LATER) steal the hunk, then lose the
    # record to the temporal filter.
    grok_adopt_sha = repo.commit(
        {
            "addons/bar/other.py":
                "from odoo.orm.zulu import grok\n\n\n"
                "def handle(x):\n"
                "    return grok(x)\n",
        },
        subject="[IMP] bar: use grok elsewhere",
        env=_dated("2025-07-06T12:00:00"),
    )
    # Commit G: a second `grok` definition whose symbol sorts BEFORE zulu's.
    repo.commit(
        {
            "odoo/orm/alpha.py":
                '"""alpha."""\n\n\ndef grok(value):\n    return value\n',
        },
        subject="[ADD] orm: alpha grok variant",
        env=_dated("2025-07-07T12:00:00"),
    )

    # Commits H1/H2: adopter BEFORE definition in walk order, IDENTICAL
    # committer timestamp. `first_seen_at <= committed_at` can't order the
    # two; only sha-based activation reproduces ground truth (no rollout).
    blorp_early_sha = repo.commit(
        {
            "addons/foo/early.py":
                "from odoo.orm.blorp import blorp\n\n\n"
                "def use(x):\n"
                "    return blorp(x)\n",
        },
        subject="[IMP] foo: premature blorp use",
        env=_dated("2025-07-08T12:00:00"),
    )
    repo.commit(
        {
            "odoo/orm/blorp.py":
                '"""blorp."""\n\n\ndef blorp(value):\n    return value\n',
        },
        subject="[ADD] orm: introduce blorp",
        env=_dated("2025-07-08T12:00:00"),
    )

    # Commit I: odoo adopts `zap`, which only the later-ordered enterprise
    # repo defines. Ground truth: no rollout (entry absent during odoo's
    # walk); a full-watchlist replay would emit one (its date predates I).
    zap_adopt_sha = repo.commit(
        {
            "addons/foo/zap_use.py":
                "from ent.tools import zap\n\n\n"
                "def go(x):\n"
                "    return zap(x)\n",
        },
        subject="[IMP] foo: use zap",
        env=_dated("2025-07-09T12:00:00"),
    )

    return repo, ent, {
        "def": def_sha,
        "content": content_sha,
        "extractor": extractor_sha,
        "pin_target": pin_target_sha,
        "grok_def": grok_def_sha,
        "grok_adopt": grok_adopt_sha,
        "blorp_early": blorp_early_sha,
        "zap_adopt": zap_adopt_sha,
    }


def _full_run(workspace: Path):
    config = config_mod.load(workspace)
    state = state_mod.State()
    watchlist = watchlist_mod.load(workspace)
    summary = run_pipeline(config, state, watchlist)
    assert not summary.errors, summary.errors
    return config


def _snapshot_raws(workspace: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for repo in ("odoo", "enterprise"):
        base = workspace / "raw" / repo
        for p in base.glob("*.json"):
            out[f"{repo}/{p.name}"] = p.read_bytes()
    return out


def _rollout_tuples(workspace: Path):
    """All ROLLOUT records across every raw, as comparable tuples."""
    out = []
    for repo in ("odoo", "enterprise"):
        for cr in iter_repo(workspace, repo):
            for c in cr.changes:
                if c.kind is not Kind.ROLLOUT:
                    continue
                out.append((
                    repo, cr.commit.sha, c.file, c.line, c.symbol, c.score,
                    c.model, c.hunk_header, c.before_snippet, c.after_snippet,
                    c.symbol_hint, c.registry,
                ))
    return sorted(out, key=lambda t: (t[0], t[1], t[4] or "", t[2]))


def test_replay_backfills_manual_pin_and_preserves_everything_else(
    tmp_path: Path, monkeypatch,
):
    """Pinning a symbol and replaying backfills its historical rollouts,
    while every pre-existing raw (definitions, extractor-emitted rollouts,
    envelopes, and re-derived content rollouts) stays byte-identical."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    repo, ent, shas = _build_repos(tmp_path)

    workspace = tmp_path / "ws"
    _write_config(workspace, repo.bare, ent.bare)
    config = _full_run(workspace)

    # Sanity: the fixture produced the raws we expect, and NOT one for the
    # pin target yet.
    assert not raw_path(workspace, "odoo", shas["pin_target"]).exists()
    assert raw_path(workspace, "odoo", shas["content"]).exists()
    assert raw_path(workspace, "odoo", shas["extractor"]).exists()

    # The extractor-emitted module rollout is a rollout with no hunk_header.
    ext_cr = next(
        cr for cr in iter_repo(workspace, "odoo")
        if cr.commit.sha == shas["extractor"]
    )
    ext_rollout = next(c for c in ext_cr.changes if c.kind is Kind.ROLLOUT)
    assert ext_rollout.symbol == "module.paper_muncher"
    assert ext_rollout.hunk_header is None

    # Ground-truth sanity for the watchlist-state fixtures: grok rollouts
    # attributed to zulu's entry (both same-commit and later); no rollout
    # for the pre-definition blorp use or the cross-repo zap use.
    before_all = _snapshot_raws(workspace)
    grok_syms = {
        c.symbol
        for cr in iter_repo(workspace, "odoo")
        if cr.commit.sha in (shas["grok_def"], shas["grok_adopt"])
        for c in cr.changes if c.kind is Kind.ROLLOUT
    }
    assert grok_syms == {"odoo.orm.zulu.grok"}
    assert not raw_path(workspace, "odoo", shas["blorp_early"]).exists()
    assert not raw_path(workspace, "odoo", shas["zap_adopt"]).exists()

    # Pin a symbol the extractors can't see, then replay.
    wl = watchlist_mod.load(workspace)
    wl.add_manual(symbol="odoo.tools.special_helper", active_version="20.0")
    watchlist_mod.save(wl, workspace)

    summary = replay_pipeline(config, state_mod.State(), watchlist_mod.load(workspace))
    assert not summary.errors, summary.errors

    # 1. The pin's historical adoption is now counted: a fresh raw for the
    #    previously-detection-free commit, carrying its content rollout.
    pin_path = raw_path(workspace, "odoo", shas["pin_target"])
    assert pin_path.exists()
    pin_cr = next(
        cr for cr in iter_repo(workspace, "odoo")
        if cr.commit.sha == shas["pin_target"]
    )
    pin_rollouts = [c for c in pin_cr.changes if c.kind is Kind.ROLLOUT]
    assert [c.symbol for c in pin_rollouts] == ["odoo.tools.special_helper"]
    assert pin_rollouts[0].hunk_header is not None  # content-matcher provenance

    # 2. Every other raw is byte-identical: definitions untouched, the
    #    extractor-emitted rollout preserved, content rollouts re-derived
    #    to the same bytes, envelopes unchanged - including the shared-name,
    #    same-timestamp and cross-repo cases.
    after_all = _snapshot_raws(workspace)
    new_files = set(after_all) - set(before_all)
    assert new_files == {f"odoo/{shas['pin_target']}.json"}
    for name, blob in before_all.items():
        assert after_all[name] == blob, name


def test_replay_after_no_change_is_a_noop(tmp_path: Path, monkeypatch):
    """Replaying without touching the watchlist rewrites nothing: content
    rollouts recompute to identical bytes and nothing is created/deleted -
    including for shared-short-name groups whose members have different
    first_seen dates, same-timestamp adopter/definition pairs, and entries
    defined only in a later-ordered repo."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    repo, ent, _shas = _build_repos(tmp_path)

    workspace = tmp_path / "ws"
    _write_config(workspace, repo.bare, ent.bare)
    config = _full_run(workspace)

    before = _snapshot_raws(workspace)
    summary = replay_pipeline(config, state_mod.State(), watchlist_mod.load(workspace))
    assert not summary.errors, summary.errors
    after = _snapshot_raws(workspace)

    assert after == before


def test_full_reindex_and_replay_agree_on_rollouts(tmp_path: Path, monkeypatch):
    """For one watchlist (extracted primitives + a manual pin), a full
    reindex and a replay produce identical ROLLOUT records - including the
    shared-short-name middle commit, whose hunk must stay attributed to the
    entry that existed at scan time."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    # --- Workspace A: full run, pin, then a FULL reindex (ofd reindex). ---
    (tmp_path / "a").mkdir()
    repo_a, ent_a, _ = _build_repos(tmp_path / "a")
    ws_a = tmp_path / "a" / "ws"
    _write_config(ws_a, repo_a.bare, ent_a.bare)
    config_a = _full_run(ws_a)
    wl_a = watchlist_mod.load(ws_a)
    wl_a.add_manual(symbol="odoo.tools.special_helper", active_version="20.0")
    watchlist_mod.save(wl_a, ws_a)
    # Reproduce `ofd reindex` (no flag): fresh state, fresh watchlist that
    # carries manual pins forward, rebuilt from definitions.
    existing = watchlist_mod.load(ws_a)
    rebuilt = watchlist_mod.Watchlist()
    for e in existing.manual_entries():
        rebuilt.entries[e.symbol] = e
    for e in existing.annotated_entries():
        rebuilt.entries[e.symbol] = e
    assert not run_pipeline(config_a, state_mod.State(), rebuilt).errors

    # --- Workspace B: full run, pin, then a REPLAY. ---
    (tmp_path / "b").mkdir()
    repo_b, ent_b, shas = _build_repos(tmp_path / "b")
    ws_b = tmp_path / "b" / "ws"
    _write_config(ws_b, repo_b.bare, ent_b.bare)
    config_b = _full_run(ws_b)
    wl_b = watchlist_mod.load(ws_b)
    wl_b.add_manual(symbol="odoo.tools.special_helper", active_version="20.0")
    watchlist_mod.save(wl_b, ws_b)
    assert not replay_pipeline(
        config_b, state_mod.State(), watchlist_mod.load(ws_b),
    ).errors

    assert _rollout_tuples(ws_a) == _rollout_tuples(ws_b)
    # The pin contributed a rollout in both, alongside all three provenances.
    symbols = {t[4] for t in _rollout_tuples(ws_b)}
    assert "odoo.tools.special_helper" in symbols
    assert "odoo.orm.tools.frobnicate" in symbols
    assert "module.paper_muncher" in symbols
    # Shared-name attribution: the middle commit's hunk stays credited to
    # zulu's grok; alpha's later variant never steals it (and thus never
    # loses it to the temporal filter).
    assert "odoo.orm.zulu.grok" in symbols
    assert "odoo.orm.alpha.grok" not in symbols
    # Same-timestamp and cross-repo adoptions stay absent, as in ground truth.
    grok_adopt = [t for t in _rollout_tuples(ws_b) if t[1] == shas["grok_adopt"]]
    assert [t[4] for t in grok_adopt] == ["odoo.orm.zulu.grok"]
    assert not raw_path(ws_b, "odoo", shas["blorp_early"]).exists()
    assert not raw_path(ws_b, "odoo", shas["zap_adopt"]).exists()
