"""Phase 3 (DESIGN-js.md): QWeb adoption surface + NEW_KWARG call-site
validation + surface-only watchlist exclusion.
"""

from pathlib import Path

from ofd import config as config_mod
from ofd import state as state_mod
from ofd import watchlist as watchlist_mod
from ofd.events.record import ChangeRecord, Kind
from ofd.pipeline import run as run_pipeline
from ofd.rollouts import detect_rollouts
from ofd.watchlist import Watchlist
from tests.fixtures.repo_builder import make_repo


def _entry(symbol: str, kind: Kind, file: str = "x.py") -> Watchlist:
    wl = Watchlist()
    wl.add_from_definition(
        ChangeRecord(kind=kind, file=file, line=1, symbol=symbol),
        repo="odoo", sha="abc",
        committed_at="2026-04-01T00:00:00Z", active_version="20.0",
    )
    return wl


def _patch(added: str, file: str) -> dict[str, str]:
    lines = "".join(f"+{line}\n" for line in added.splitlines())
    return {file: (
        f"--- a/{file}\n+++ b/{file}\n@@ -1,1 +1,9 @@\n const x = 1;\n{lines}"
    )}


# --- QWeb component-tag adoption -------------------------------------------


def test_qweb_component_tag_is_rollout():
    wl = _entry(
        "@web/core/tags_list/badge_tag.BadgeTag", Kind.NEW_JS_EXPORT,
        "addons/web/static/src/core/tags_list/badge_tag.js",
    )
    records = detect_rollouts(_patch(
        '<BadgeTag color="tag.color" text="tag.text" onClick="tag.onClick"/>',
        "addons/crm/static/src/views/crm_kanban.xml",
    ), wl, {})
    assert len(records) == 1
    assert records[0].symbol == "@web/core/tags_list/badge_tag.BadgeTag"


def test_qweb_non_tag_mentions_are_silent():
    """Only an opening component tag counts - attribute values, t-esc
    expressions and quoted strings are mentions, not instantiations."""
    wl = _entry(
        "@web/core/tags_list/badge_tag.BadgeTag", Kind.NEW_JS_EXPORT,
        "addons/web/static/src/core/tags_list/badge_tag.js",
    )
    for added in (
        '<t t-esc="BadgeTag"/>',
        '<widget name="BadgeTag"/>',
        '<t t-component="props.BadgeTag"/>',
    ):
        assert detect_rollouts(_patch(
            added, "addons/crm/static/src/views/crm_kanban.xml",
        ), wl, {}) == [], added


def test_qweb_scope_boundaries():
    """Component tags only count in static/src templates: backend views
    can't instantiate OWL components, test templates are skipped, and
    Python/View primitives never match in QWEB scope."""
    js_wl = _entry(
        "@web/core/tags_list/badge_tag.BadgeTag", Kind.NEW_JS_EXPORT,
        "addons/web/static/src/core/tags_list/badge_tag.js",
    )
    tag = '<BadgeTag text="x"/>'
    assert detect_rollouts(
        _patch(tag, "addons/crm/views/crm_views.xml"), js_wl, {},
    ) == []
    assert detect_rollouts(
        _patch(tag, "addons/crm/static/tests/crm_kanban.xml"), js_wl, {},
    ) == []
    # The reverse: a Python helper's name in an OWL template expression
    # (`record.x.raw_value` kanban idiom) is not an adoption.
    py_wl = _entry(
        "odoo.modules.module.Manifest.canary_attr",
        Kind.NEW_DECORATOR_OR_HELPER, "odoo/modules/module.py",
    )
    assert detect_rollouts(_patch(
        '<span t-esc="record.stream_post.canary_attr"/>',
        "addons/social/static/src/views/stream.xml",
    ), py_wl, {}) == []


def test_qweb_attr_needle_manual_pin():
    """The data-available-offline story: a manual NEW_VIEW_ATTRIBUTE
    pin matches plain and t-att- attribute forms in OWL templates."""
    wl = Watchlist()
    wl.add_manual(
        symbol="data-available-offline",
        active_version="19.4",
        kind=Kind.NEW_VIEW_ATTRIBUTE,
    )
    for added in (
        '<button class="btn-close" data-available-offline=""/>',
        '<button t-att-data-available-offline="this.isAvailable"/>',
    ):
        records = detect_rollouts(_patch(
            added, "addons/point_of_sale/static/src/app/dialog.xml",
        ), wl, {})
        assert len(records) == 1, added
    # Not a Python surface.
    py_patch = """\
--- a/m.py
+++ b/m.py
@@ -1,1 +1,2 @@
 x = 1
+key = 'data-available-offline'
"""
    assert detect_rollouts({"m.py": py_patch}, wl, {}) == []


# --- NEW_KWARG call-site validation -----------------------------------------


def _kwarg_patch(added: str) -> dict[str, str]:
    return {"x.py": (
        "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,9 @@\n a = 1\n"
        + "".join(f"+{line}\n" for line in added.splitlines())
    )}


def test_kwarg_requires_call_site():
    """`('model', '=', ...)` domain tuples and `'binary'` type strings
    are not adoptions of `Query.__init__.model` /
    `AssetsBundle.__init__.binary` - the kwarg must appear in a
    constructor-shaped call (119 + 127 bogus rollouts on the
    2026-06-04 corpus)."""
    wl = _entry("odoo.tools.query.Query.__init__.model", Kind.NEW_KWARG)
    for added in (
        "records = self.env['ir.model.data'].search([('model', '=', 'x')])",
        "model = self._get_model()",
        "def f(env, model_name: str): pass",
        "res = lookup(model='res.partner')",  # lowercase callee
    ):
        assert detect_rollouts(_kwarg_patch(added), wl, {}) == [], added
    assert len(detect_rollouts(_kwarg_patch(
        "q = Query(env, model='res.partner')",
    ), wl, {})) == 1
    # Subclass constructors adopt __init__ kwargs too.
    assert len(detect_rollouts(_kwarg_patch(
        "q = tools.SubQuery(env, model='res.partner')",
    ), wl, {})) == 1


def test_kwarg_named_method_call_site():
    wl = _entry("odoo.orm.fields.Field.to_sql.query", Kind.NEW_KWARG)
    assert detect_rollouts(_kwarg_patch(
        "sql = field.to_sql(alias, query=query)",
    ), wl, {})
    for added in (
        "query = self.env.execute_query(sql)",
        "rows = run(query=q)",
    ):
        assert detect_rollouts(_kwarg_patch(added), wl, {}) == [], added


def test_kwarg_multiline_fragment_still_counts():
    """A hunk adding only `kwarg=value,` inside an existing call has no
    call node to validate; the expression_list fingerprint keeps it."""
    wl = _entry("odoo.orm.fields.Field.to_sql.flush_fields", Kind.NEW_KWARG)
    assert len(detect_rollouts(_kwarg_patch(
        "    flush_fields=fnames,",
    ), wl, {})) == 1


# --- surface-only paths ------------------------------------------------------


def _write_config(workspace: Path, mirror: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "config.yaml").write_text(f"""\
repos:
  odoo:
    source: /dev/null
    mirror: {mirror}
    branch: master
    framework_paths:
      - odoo/orm/**/*.py
      - odoo/upgrade_code/**
    surface_only_paths:
      - odoo/upgrade_code/**
    core_paths: []
active_version: "20.0"
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


def test_surface_only_paths_emit_events_but_never_watchlist(
    tmp_path: Path, monkeypatch,
):
    """Migration-tooling helpers surface as scored events (the OWL3
    story) but never join the watchlist - `change` from a t-call
    rewrite script must not become a tracked primitive that matches
    every `change=` in the tree."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    repo = make_repo(tmp_path)
    sha = repo.commit(
        {
            "odoo/upgrade_code/19.1-00-t-call.py":
                "def change(path):\n    return path\n",
            "odoo/orm/helpers.py":
                "def real_api(x):\n    return x\n",
        },
        subject="[ADD] upgrade_code: t-call rewrite",
    )

    workspace = tmp_path / "ws"
    _write_config(workspace, repo.bare)
    config = config_mod.load(workspace)
    state = state_mod.load()
    watchlist = watchlist_mod.load(workspace)

    summary = run_pipeline(config, state, watchlist)
    assert not summary.errors, summary.errors

    from ofd.events.store import iter_repo
    records = {cr.commit.sha: cr for cr in iter_repo(workspace, "odoo")}
    symbols = {c.symbol for c in records[sha].changes}
    # Both definitions surfaced as events...
    assert "odoo.upgrade_code.19.1-00-t-call.change" in symbols
    assert "odoo.orm.helpers.real_api" in symbols
    # ...but only the real framework path joined the watchlist.
    persisted = watchlist_mod.load(workspace)
    assert "odoo.orm.helpers.real_api" in persisted.entries
    assert "odoo.upgrade_code.19.1-00-t-call.change" not in persisted.entries
