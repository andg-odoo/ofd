from pathlib import Path

from ofd.events.record import ChangeRecord, Kind
from ofd.rollouts import detect_rollouts
from ofd.watchlist import Watchlist, load, save


def _seeded_watchlist() -> Watchlist:
    wl = Watchlist()
    wl.add_from_definition(
        ChangeRecord(
            kind=Kind.NEW_PUBLIC_CLASS,
            file="odoo/orm/models_cached.py",
            line=8,
            symbol="odoo.orm.models_cached.CachedModel",
        ),
        repo="odoo",
        sha="abc",
        committed_at="2026-04-01T00:00:00Z",
        active_version="20.0",
    )
    return wl


def test_watchlist_skips_non_definition_kinds():
    wl = Watchlist()
    entry = wl.add_from_definition(
        ChangeRecord(
            kind=Kind.SIGNATURE_CHANGE,
            file="odoo/fields.py",
            line=1,
            symbol="odoo.fields.Field.__init__",
        ),
        repo="odoo", sha="x", committed_at="2026-04-01T00:00:00Z", active_version="20.0",
    )
    assert entry is None
    assert wl.entries == {}


def test_watchlist_short_name_extraction():
    wl = _seeded_watchlist()
    assert wl.short_names() == {"CachedModel"}


def test_watchlist_lookup_by_short():
    wl = _seeded_watchlist()
    entries = wl.lookup_by_short("CachedModel")
    assert len(entries) == 1
    assert entries[0].symbol == "odoo.orm.models_cached.CachedModel"


def test_watchlist_persist_and_reload(tmp_path: Path):
    wl = _seeded_watchlist()
    save(wl, tmp_path)
    got = load(tmp_path)
    assert got.short_names() == {"CachedModel"}
    entry = got.entries["odoo.orm.models_cached.CachedModel"]
    assert entry.kind == Kind.NEW_PUBLIC_CLASS


def test_watchlist_remove():
    wl = _seeded_watchlist()
    assert wl.remove("odoo.orm.models_cached.CachedModel")
    assert wl.entries == {}


def test_manual_entry_survives_persist(tmp_path: Path):
    wl = Watchlist()
    wl.add_manual(
        symbol="formatted_display_name",
        active_version="19.4",
        note="context key on display_name compute",
    )
    save(wl, tmp_path)
    got = load(tmp_path)
    entry = got.entries["formatted_display_name"]
    assert entry.source == "manual"
    assert entry.note == "context key on display_name compute"
    assert entry.short_name == "formatted_display_name"
    assert got.manual_entries() == [entry]


def test_manual_entry_triggers_rollout_detection():
    """Pinning a context-key magic string lets the normal rollout
    matcher find adoption without any extractor involvement."""
    wl = Watchlist()
    wl.add_manual(symbol="formatted_display_name", active_version="19.4")
    patch = """\
--- a/m.py
+++ b/m.py
@@ -1,1 +1,2 @@
 x = 1
+    @api.depends_context('formatted_display_name')
"""
    records = detect_rollouts({"m.py": patch}, wl, {})
    assert len(records) == 1
    assert records[0].symbol == "formatted_display_name"


def test_detect_rollouts_finds_usage_in_hunk():
    wl = _seeded_watchlist()
    patch = """\
diff --git a/addons/website/models/website.py b/addons/website/models/website.py
--- a/addons/website/models/website.py
+++ b/addons/website/models/website.py
@@ -10,7 +10,7 @@ from odoo import models
-class Website(models.Model):
+class Website(models.CachedModel):
     _name = 'website'
     _description = 'Website'
"""
    child_source = (
        "from odoo import models\n\n"
        "class Website(models.CachedModel):\n"
        "    _name = 'website'\n"
        "    _description = 'Website'\n"
    )
    records = detect_rollouts(
        {"addons/website/models/website.py": patch},
        wl,
        {"addons/website/models/website.py": child_source},
    )
    assert len(records) == 1
    r = records[0]
    assert r.kind == Kind.ROLLOUT
    assert r.symbol == "odoo.orm.models_cached.CachedModel"
    assert r.model == "website"
    assert "CachedModel" in r.after_snippet
    assert "class Website(models.Model)" in r.before_snippet
    assert r.hunk_header.startswith("@@ ")


def test_detect_rollouts_ignores_removed_only():
    """If the watchlisted name appears only in a removal, not addition,
    don't treat it as a rollout."""
    wl = _seeded_watchlist()
    patch = """\
--- a/a.py
+++ b/a.py
@@ -1,3 +1,2 @@
 x = 1
-from .cached import CachedModel
 y = 2
"""
    records = detect_rollouts({"a.py": patch}, wl, {})
    assert records == []


def test_detect_rollouts_returns_empty_when_watchlist_empty():
    records = detect_rollouts({"a.py": "any patch"}, Watchlist(), {})
    assert records == []


def test_detect_rollouts_captures_inherit_model():
    wl = _seeded_watchlist()
    patch = """\
--- a/a.py
+++ b/a.py
@@ -1,2 +1,3 @@
 x = 1
+class Foo(models.CachedModel):
+    pass
 y = 2
"""
    child_source = 'class Foo(models.Model):\n    _inherit = "res.partner"\n'
    records = detect_rollouts({"a.py": patch}, wl, {"a.py": child_source})
    assert records and records[0].model == "res.partner"


# --- Context-aware matching: generic names require explicit import ---


def _watchlist_with(symbol: str) -> Watchlist:
    wl = Watchlist()
    wl.add_from_definition(
        ChangeRecord(
            kind=Kind.NEW_DECORATOR_OR_HELPER,
            file="odoo/orm/fields_relational.py",
            line=1,
            symbol=symbol,
        ),
        repo="odoo", sha="abc", committed_at="2026-01-01T00:00:00Z",
        active_version="20.0",
    )
    return wl


def test_generic_name_join_ignores_string_join_noise():
    """A `.join()` on a list/string must NOT be treated as a rollout of
    `Many2many.join`. Without the generic-name gate, this produced huge
    false positives in the real backfill (481 spurious rollouts)."""
    wl = _watchlist_with("odoo.orm.fields_relational.Many2many.join")
    patch = """\
--- a/x.py
+++ b/x.py
@@ -1,2 +1,3 @@
 a = 1
+    return ",".join(parts)
 b = 2
"""
    records = detect_rollouts({"x.py": patch}, wl, {})
    assert records == []


def test_generic_name_join_matched_on_explicit_import():
    wl = _watchlist_with("odoo.orm.fields_relational.Many2many.join")
    patch = """\
--- a/x.py
+++ b/x.py
@@ -1,2 +1,3 @@
 a = 1
+from odoo.orm.fields_relational import Many2many, join
 b = 2
"""
    records = detect_rollouts({"x.py": patch}, wl, {})
    assert len(records) == 1
    assert records[0].symbol == "odoo.orm.fields_relational.Many2many.join"


def test_context_match_requires_syntactic_position():
    """`CachedModel` in a string literal / comment must not match."""
    wl = _watchlist_with("odoo.orm.models_cached.CachedModel")
    patch = """\
--- a/x.py
+++ b/x.py
@@ -1,1 +1,4 @@
 # describe: CachedModel lets you cache things
+# note: CachedModel, CachedModel, CachedModel
+log.debug("seen CachedModel mentioned somewhere")
 end = True
"""
    records = detect_rollouts({"x.py": patch}, wl, {})
    assert records == []


def test_context_match_accepts_attribute_access():
    wl = _watchlist_with("odoo.orm.models_cached.CachedModel")
    patch = """\
--- a/x.py
+++ b/x.py
@@ -1,2 +1,3 @@
 from odoo import models
+class Site(models.CachedModel):
 pass
"""
    records = detect_rollouts({"x.py": patch}, wl, {})
    assert len(records) == 1


def test_context_match_accepts_kwarg_use():
    """New kwarg like `compute_sql=` - rollout shows up as `name=value`."""
    wl = _watchlist_with("odoo.fields.Field.__init__.compute_sql")
    patch = """\
--- a/m.py
+++ b/m.py
@@ -1,1 +1,2 @@
 x = 1
+is_fav = fields.Boolean(compute_sql='_compute_sql_is_fav')
"""
    records = detect_rollouts({"m.py": patch}, wl, {})
    assert len(records) == 1
    assert records[0].symbol == "odoo.fields.Field.__init__.compute_sql"


def test_context_match_accepts_call():
    wl = _watchlist_with("odoo.orm.query.TableSQL")
    patch = """\
--- a/q.py
+++ b/q.py
@@ -1,1 +1,2 @@
 x = 1
+table = TableSQL(alias='foo')
"""
    records = detect_rollouts({"q.py": patch}, wl, {})
    assert len(records) == 1


def test_context_match_accepts_exact_quoted_string_in_python():
    """`env.context.get('formatted_display_name')` - the field name passed
    as a string is a real adoption signal, not noise."""
    wl = _watchlist_with("odoo.models.BaseModel.formatted_display_name")
    patch = """\
--- a/m.py
+++ b/m.py
@@ -1,1 +1,3 @@
 x = 1
+    @api.depends_context('formatted_display_name')
+    def _compute_name(self): pass
"""
    records = detect_rollouts({"m.py": patch}, wl, {})
    assert len(records) == 1


def test_context_match_accepts_xml_name_attribute():
    """`<field name="formatted_display_name"/>` in an XML view."""
    wl = _watchlist_with("odoo.models.BaseModel.formatted_display_name")
    patch = """\
--- a/v.xml
+++ b/v.xml
@@ -1,1 +1,2 @@
 <tree>
+    <field name="formatted_display_name"/>
"""
    records = detect_rollouts({"v.xml": patch}, wl, {})
    assert len(records) == 1


def test_context_match_rejects_name_embedded_in_longer_string():
    """String that *contains* the name but isn't exactly the name shouldn't
    match - kills log-message / docstring noise."""
    wl = _watchlist_with("odoo.models.BaseModel.formatted_display_name")
    patch = """\
--- a/m.py
+++ b/m.py
@@ -1,1 +1,2 @@
 x = 1
+log.debug("the formatted_display_name thing is broken")
"""
    records = detect_rollouts({"m.py": patch}, wl, {})
    assert records == []


# --- RNG-derived view attributes: scope rollouts to parent element ---


def _watchlist_with_rng_attr(element: str, attribute: str) -> Watchlist:
    """Seed a watchlist with a NEW_VIEW_ATTRIBUTE entry like the RNG
    extractor would emit (`<element>` gained `<attribute>`)."""
    wl = Watchlist()
    wl.add_from_definition(
        ChangeRecord(
            kind=Kind.NEW_VIEW_ATTRIBUTE,
            file="odoo/addons/base/rng/common.rng",
            line=1,
            element=element,
            attribute=attribute,
            symbol=f"odoo.addons.base.rng.common.{element}.{attribute}",
        ),
        repo="odoo", sha="abc", committed_at="2026-03-05T00:00:00Z",
        active_version="20.0",
    )
    return wl


def test_rng_attribute_rollout_requires_parent_element():
    """widget.invisible must match `<widget ... invisible=...>` but NOT
    `<field ... invisible=...>` or `<setting ... invisible=...>`. This is
    the core false-positive fix: ~65% of widget.invisible rollouts were
    actually field/setting usages with the old quoted-string matcher."""
    wl = _watchlist_with_rng_attr("widget", "invisible")

    widget_patch = """\
--- a/v.xml
+++ b/v.xml
@@ -1,1 +1,2 @@
 <list>
+    <widget name="test_widget" invisible="state == 'draft'"/>
"""
    records = detect_rollouts({"v.xml": widget_patch}, wl, {})
    assert len(records) == 1
    assert records[0].symbol == "odoo.addons.base.rng.common.widget.invisible"

    field_patch = """\
--- a/v.xml
+++ b/v.xml
@@ -1,1 +1,2 @@
 <form>
+    <field name="adyen_merchant_account" invisible="use_payment_terminal != 'adyen'"/>
"""
    assert detect_rollouts({"v.xml": field_patch}, wl, {}) == []

    setting_patch = """\
--- a/v.xml
+++ b/v.xml
@@ -1,1 +1,2 @@
 <settings>
+    <setting id="barcode_scanner" invisible="use_kiosk_mode">text</setting>
"""
    assert detect_rollouts({"v.xml": setting_patch}, wl, {}) == []


def test_rng_attribute_rollout_does_not_leak_across_tags():
    """The scoped regex must not match when `invisible` lives on a child
    element that happens to sit inside a widget tag."""
    wl = _watchlist_with_rng_attr("widget", "invisible")
    patch = """\
--- a/v.xml
+++ b/v.xml
@@ -1,1 +1,4 @@
 <list>
+    <widget name="outer">
+        <field name="foo" invisible="1"/>
+    </widget>
"""
    assert detect_rollouts({"v.xml": patch}, wl, {}) == []


def test_rng_attribute_rollout_multiline_tag():
    """Widget opening tag split across multiple lines - the scoped regex
    should still find the attribute when the tag is multi-line."""
    wl = _watchlist_with_rng_attr("widget", "invisible")
    patch = """\
--- a/v.xml
+++ b/v.xml
@@ -1,1 +1,4 @@
 <list>
+    <widget
+        name="test_widget"
+        invisible="state == 'draft'"/>
"""
    records = detect_rollouts({"v.xml": patch}, wl, {})
    assert len(records) == 1


def test_rng_attribute_rollout_string_literal_no_longer_matches():
    """Old behavior: `'invisible'` in Python code would trigger a rollout
    via the quoted-string branch. New behavior: RNG-derived entries only
    match `<element ... attr=...>`, so this is no longer a match."""
    wl = _watchlist_with_rng_attr("widget", "invisible")
    patch = """\
--- a/m.py
+++ b/m.py
@@ -1,1 +1,2 @@
 x = 1
+    return attrs.get('invisible', False)
"""
    assert detect_rollouts({"m.py": patch}, wl, {}) == []


def test_watchlist_stores_element_for_rng_attr_entries():
    wl = _watchlist_with_rng_attr("widget", "invisible")
    entry = wl.entries["odoo.addons.base.rng.common.widget.invisible"]
    assert entry.element == "widget"


def test_watchlist_does_not_store_element_for_python_entries():
    """Python primitives should leave `element` as None and keep the
    legacy broad matcher (attribute access, kwargs, string literals)."""
    wl = _watchlist_with("odoo.orm.models_cached.CachedModel")
    entry = wl.entries["odoo.orm.models_cached.CachedModel"]
    assert entry.element is None


def test_watchlist_roundtrip_preserves_element(tmp_path: Path):
    wl = _watchlist_with_rng_attr("widget", "invisible")
    save(wl, tmp_path)
    got = load(tmp_path)
    entry = got.entries["odoo.addons.base.rng.common.widget.invisible"]
    assert entry.element == "widget"


def test_xml_file_uses_slim_contextual_pattern():
    """XML files should still match quoted-string, attribute-assignment,
    and attribute-access forms of a watchlisted name. The slimmed
    per-scope pattern drops Python-only forms but must keep these three
    to match real QWeb/view adoption."""
    wl = _watchlist_with("odoo.models.BaseModel.formatted_display_name")

    # 1. <field name="formatted_display_name"/> (quoted-string form).
    quoted_patch = """\
--- a/v.xml
+++ b/v.xml
@@ -1,1 +1,2 @@
 <tree>
+    <field name="formatted_display_name"/>
"""
    assert len(detect_rollouts({"v.xml": quoted_patch}, wl, {})) == 1

    # 2. attribute-access inside a QWeb expression string.
    qweb_patch = """\
--- a/v.xml
+++ b/v.xml
@@ -1,1 +1,2 @@
 <t t-name="foo">
+    <span t-esc="record.formatted_display_name"/>
"""
    assert len(detect_rollouts({"v.xml": qweb_patch}, wl, {})) == 1


def test_shared_short_name_emits_one_rollout_per_hunk():
    """When multiple watchlist entries share a short name (e.g. a new
    kwarg added to several Field subclasses), a hunk using that name
    should still emit ONE rollout, not N. Per-entry matching without
    dedup would balloon rollout counts on shared-name primitives."""
    wl = Watchlist()
    # `compute_sql` is non-generic so the rollout pattern actually fires
    # on `compute_sql=`. Four entries, same short name.
    for subclass in ("Field", "Binary", "Many2one", "BaseString"):
        wl.add_from_definition(
            ChangeRecord(
                kind=Kind.NEW_KWARG,
                file=f"odoo/orm/fields_{subclass.lower()}.py",
                line=1,
                symbol=f"odoo.orm.fields.{subclass}.__init__.compute_sql",
            ),
            repo="odoo", sha="abc", committed_at="2026-04-01T00:00:00Z",
            active_version="20.0",
        )
    patch = """\
--- a/x.py
+++ b/x.py
@@ -1,1 +1,2 @@
 x = 1
+    is_fav = fields.Boolean(compute_sql="_compute_sql_is_fav")
"""
    records = detect_rollouts({"x.py": patch}, wl, {})
    assert len(records) == 1  # NOT 4


# --- Language gate: cross-language false positives ---


def test_python_primitive_not_matched_in_js_file():
    """Regression: `PropertiesDefinition.setup` (Python helper) must not
    match `setup() { super.setup(...) }` in an OWL component patch. Every
    OWL component has a `setup()` lifecycle method; without the language
    gate, every patched component looks like a rollout."""
    wl = _watchlist_with("odoo.orm.fields_properties.PropertiesDefinition.setup")
    patch = """\
--- /dev/null
+++ b/documents_account/static/src/components/x.js
@@ -0,0 +1,5 @@
+patch(MailAttachments.prototype, {
+    setup() {
+        super.setup(...arguments);
+    },
+});
"""
    records = detect_rollouts(
        {"documents_account/static/src/components/x.js": patch}, wl, {},
    )
    assert records == []


def test_context_key_not_matched_in_js_file():
    """Regression: `partner_id` context key must not match `partner_id =
    fields.One(...)` in a JS Record class - it's a JS field declaration,
    not a context-key adoption."""
    wl = Watchlist()
    wl.add_manual(symbol="partner_id", active_version="19.4")
    patch = """\
--- /dev/null
+++ b/ai/static/src/discuss/core/common/ai_agent_model.js
@@ -0,0 +1,4 @@
+export class AiAgent extends Record {
+    static _name = "ai.agent";
+    partner_id = fields.One("res.partner");
+}
"""
    records = detect_rollouts(
        {"ai/static/src/discuss/core/common/ai_agent_model.js": patch}, wl, {},
    )
    assert records == []


def test_view_kind_does_not_match_in_python_file():
    """A NEW_VIEW_ATTRIBUTE entry has language=VIEW; a `.py` diff that
    happens to contain the attribute name as a string must not fire."""
    wl = _watchlist_with_rng_attr("widget", "invisible")
    patch = """\
--- a/m.py
+++ b/m.py
@@ -1,1 +1,2 @@
 x = 1
+    return attrs.get('invisible', False)
"""
    assert detect_rollouts({"m.py": patch}, wl, {}) == []


def test_new_kwarg_not_matched_in_xml_file():
    """NEW_KWARG entries are PY-only - a `<button kind="primary"/>`
    isn't an adoption of a `kind` kwarg even if the names collide."""
    wl = Watchlist()
    wl.add_from_definition(
        ChangeRecord(
            kind=Kind.NEW_KWARG,
            file="odoo/fields.py",
            line=1,
            symbol="odoo.fields.Many2one.__init__.kind",
        ),
        repo="odoo", sha="abc", committed_at="2026-04-01T00:00:00Z",
        active_version="20.0",
    )
    patch = """\
--- a/v.xml
+++ b/v.xml
@@ -1,1 +1,2 @@
 <list>
+    <button kind="primary"/>
"""
    assert detect_rollouts({"v.xml": patch}, wl, {}) == []


def test_unknown_extension_skipped():
    """Files with no recognized language extension (.po, .scss, .csv, ...)
    are skipped wholesale - they never get scanned for any kind."""
    wl = _watchlist_with("odoo.orm.models_cached.CachedModel")
    po_patch = """\
--- a/m.po
+++ b/m.po
@@ -1,1 +1,2 @@
 msgid "old"
+msgid "use CachedModel for caching"
"""
    scss_patch = """\
--- a/s.scss
+++ b/s.scss
@@ -1,1 +1,2 @@
 .foo { color: red; }
+.CachedModel { color: blue; }
"""
    assert detect_rollouts({"m.po": po_patch, "s.scss": scss_patch}, wl, {}) == []


def test_directive_rollout_matches_child_opening_tag():
    """NEW_VIEW_DIRECTIVE rollouts (e.g. `list+column`) must match the
    child element's opening tag in XML diffs, not look for it as an
    attribute on the parent. Pre-fix, the matcher compiled an
    attribute-shaped regex that essentially never fired."""
    wl = Watchlist()
    wl.add_from_definition(
        ChangeRecord(
            kind=Kind.NEW_VIEW_DIRECTIVE,
            file="odoo/addons/base/rng/list_view.rng",
            line=1,
            element="list",
            directive="column",
            symbol="odoo.addons.base.rng.list_view.list+column",
        ),
        repo="odoo", sha="abc", committed_at="2026-04-09T00:00:00Z",
        active_version="20.0",
    )
    patch = """\
--- a/v.xml
+++ b/v.xml
@@ -1,1 +1,4 @@
 <list>
+    <column string="Total">
+        <field name="amount"/>
+    </column>
"""
    records = detect_rollouts({"v.xml": patch}, wl, {})
    assert len(records) == 1
    assert records[0].symbol == "odoo.addons.base.rng.list_view.list+column"


def test_directive_rollout_does_not_match_attribute_shape():
    """A directive whose name appears as an *attribute* on something
    must not fire (it isn't an adoption of the child-element shape)."""
    wl = Watchlist()
    wl.add_from_definition(
        ChangeRecord(
            kind=Kind.NEW_VIEW_DIRECTIVE,
            file="odoo/addons/base/rng/list_view.rng",
            line=1,
            element="list",
            directive="column",
            symbol="odoo.addons.base.rng.list_view.list+column",
        ),
        repo="odoo", sha="abc", committed_at="2026-04-09T00:00:00Z",
        active_version="20.0",
    )
    patch = """\
--- a/v.xml
+++ b/v.xml
@@ -1,1 +1,2 @@
 <form>
+    <field name="x" column="2"/>
"""
    assert detect_rollouts({"v.xml": patch}, wl, {}) == []


# --- required_ancestor: structural ancestor restriction ---


def _watchlist_with_widget_invisible(
    required_ancestor: list[str] | None = None,
) -> Watchlist:
    wl = _watchlist_with_rng_attr("widget", "invisible")
    entry = wl.entries["odoo.addons.base.rng.common.widget.invisible"]
    entry.required_ancestor = required_ancestor
    return wl


def test_required_ancestor_accepts_widget_inside_list():
    """`<widget invisible=>` directly under `<list>` matches when
    required_ancestor=['list','tree'] - this is the new feature
    `0aa942313fa1` actually shipped."""
    wl = _watchlist_with_widget_invisible(["list", "tree"])
    patch = """\
--- a/v.xml
+++ b/v.xml
@@ -1,1 +1,4 @@
 <list>
+    <widget name="ribbon" invisible="state == 'draft'"/>
+    <field name="x"/>
 </list>
"""
    child = (
        "<list>\n"
        "  <widget name='ribbon' invisible=\"state == 'draft'\"/>\n"
        "  <field name='x'/>\n"
        "</list>\n"
    )
    records = detect_rollouts({"v.xml": patch}, wl, {"v.xml": child})
    assert len(records) == 1


def test_required_ancestor_rejects_widget_inside_form():
    """`<widget invisible=>` inside a `<form>` is rejected when
    required_ancestor=['list','tree'] - form-view widgets have always
    supported invisible at runtime, so this isn't an adoption of the
    new list-view feature."""
    wl = _watchlist_with_widget_invisible(["list", "tree"])
    patch = """\
--- a/v.xml
+++ b/v.xml
@@ -1,1 +1,3 @@
 <form>
+    <widget name="ribbon" invisible="state == 'draft'"/>
 </form>
"""
    child = (
        "<form>\n"
        "  <widget name='ribbon' invisible=\"state == 'draft'\"/>\n"
        "</form>\n"
    )
    assert detect_rollouts({"v.xml": patch}, wl, {"v.xml": child}) == []


def test_required_ancestor_handles_deep_nesting():
    """Ancestor walk is unbounded - a `<widget>` several levels deep
    inside `<list>` (e.g. behind a `<header>`) still qualifies."""
    wl = _watchlist_with_widget_invisible(["list"])
    patch = """\
--- a/v.xml
+++ b/v.xml
@@ -1,1 +1,7 @@
 <list>
+    <header>
+      <group>
+        <widget name="ribbon" invisible="x"/>
+      </group>
+    </header>
 </list>
"""
    child = (
        "<list>"
        "<header><group>"
        "<widget name='ribbon' invisible=\"x\"/>"
        "</group></header>"
        "</list>"
    )
    assert len(detect_rollouts({"v.xml": patch}, wl, {"v.xml": child})) == 1


def test_required_ancestor_uses_lazy_fetcher_when_child_source_absent():
    """If `child_sources` doesn't include the file, the matcher pulls
    it via `fetch_child` lazily - this is how the pipeline avoids
    pre-fetching every potential hit."""
    wl = _watchlist_with_widget_invisible(["list"])
    patch = """\
--- a/v.xml
+++ b/v.xml
@@ -1,1 +1,2 @@
 <list>
+    <widget name="r" invisible="x"/>
"""
    fetched: list[str] = []
    def fake_fetch(file: str):
        fetched.append(file)
        return "<list><widget name='r' invisible=\"x\"/></list>"
    records = detect_rollouts(
        {"v.xml": patch}, wl, child_sources={}, fetch_child=fake_fetch,
    )
    assert fetched == ["v.xml"]
    assert len(records) == 1


def test_required_ancestor_rejects_when_child_source_unavailable():
    """No child source AND no fetcher means we can't structurally
    confirm - reject conservatively (the user opted into strictness
    by setting the annotation)."""
    wl = _watchlist_with_widget_invisible(["list"])
    patch = """\
--- a/v.xml
+++ b/v.xml
@@ -1,1 +1,2 @@
 <list>
+    <widget name="r" invisible="x"/>
"""
    assert detect_rollouts({"v.xml": patch}, wl, {}) == []


def test_required_ancestor_persists_through_serialization(tmp_path: Path):
    wl = _watchlist_with_widget_invisible(["list", "tree"])
    save(wl, tmp_path)
    got = load(tmp_path)
    entry = got.entries["odoo.addons.base.rng.common.widget.invisible"]
    assert entry.required_ancestor == ["list", "tree"]


def test_annotated_entries_helper_returns_only_annotated():
    wl = _watchlist_with_widget_invisible(["list"])
    annotated = wl.annotated_entries()
    assert len(annotated) == 1
    assert annotated[0].symbol == "odoo.addons.base.rng.common.widget.invisible"


# --- shared-short-name kwarg discrimination by call-site method ---


def _seed_kwarg(wl: Watchlist, symbol: str) -> None:
    wl.add_from_definition(
        ChangeRecord(
            kind=Kind.NEW_KWARG, file="x.py", line=1, symbol=symbol,
        ),
        repo="odoo", sha="abc", committed_at="2026-04-01T00:00:00Z",
        active_version="20.0",
    )


def test_method_discriminator_attributes_to_correct_method():
    """Two NEW_KWARG entries share short_name `table` across different
    methods. A single `condition_to_sql(table=...)` call must attribute
    only to the condition_to_sql entry, not the to_sql one."""
    wl = Watchlist()
    _seed_kwarg(wl, "odoo.orm.fields.Field.to_sql.table")
    _seed_kwarg(wl, "odoo.orm.fields.Field.condition_to_sql.table")
    patch = """\
--- a/x.py
+++ b/x.py
@@ -1,1 +1,2 @@
 a = 1
+    return self.condition_to_sql(table=t)
"""
    records = detect_rollouts({"x.py": patch}, wl, {})
    symbols = [r.symbol for r in records]
    assert symbols == ["odoo.orm.fields.Field.condition_to_sql.table"]


def test_method_discriminator_emits_per_method_when_both_present():
    """If a single hunk calls BOTH methods with the same kwarg, both
    entries fire - they're independent adoptions of distinct primitives
    that happen to share a kwarg name."""
    wl = Watchlist()
    _seed_kwarg(wl, "odoo.orm.fields.Field.to_sql.table")
    _seed_kwarg(wl, "odoo.orm.fields.Field.condition_to_sql.table")
    patch = """\
--- a/x.py
+++ b/x.py
@@ -1,1 +1,3 @@
 a = 1
+    self.to_sql(table=t1)
+    self.condition_to_sql(table=t2)
"""
    records = detect_rollouts({"x.py": patch}, wl, {})
    symbols = sorted(r.symbol for r in records)
    assert symbols == [
        "odoo.orm.fields.Field.condition_to_sql.table",
        "odoo.orm.fields.Field.to_sql.table",
    ]


def test_method_discriminator_drops_kwarg_to_unrelated_method():
    """`some_other_method(table=t)` shares the kwarg name but isn't an
    adoption of either watchlisted entry - should fire 0 rollouts."""
    wl = Watchlist()
    _seed_kwarg(wl, "odoo.orm.fields.Field.to_sql.table")
    _seed_kwarg(wl, "odoo.orm.fields.Field.condition_to_sql.table")
    patch = """\
--- a/x.py
+++ b/x.py
@@ -1,1 +1,2 @@
 a = 1
+    self.unrelated_method(table=t)
"""
    assert detect_rollouts({"x.py": patch}, wl, {}) == []


def test_method_discriminator_falls_back_to_legacy_for_single_method_group():
    """When all entries in a shared-short group are for the same method
    (shouldn't happen post-`_dedupe_kwarg_overrides`, but cross-commit
    overrides can produce it), legacy first-wins behavior preserves the
    pre-discriminator semantics."""
    wl = Watchlist()
    _seed_kwarg(wl, "odoo.orm.fields.Field.to_sql.table")
    _seed_kwarg(wl, "odoo.orm.fields_other.Other.to_sql.table")
    patch = """\
--- a/x.py
+++ b/x.py
@@ -1,1 +1,2 @@
 a = 1
+    self.to_sql(table=t)
"""
    records = detect_rollouts({"x.py": patch}, wl, {})
    assert len(records) == 1
    # Alphabetically-first symbol wins, as with the historical dedupe.
    assert records[0].symbol == "odoo.orm.fields.Field.to_sql.table"


def test_watchlist_from_dict_backward_compat_missing_element():
    """Existing watchlist.json files predate the `element` field - they
    should load cleanly with element=None."""
    legacy = {
        "entries": {
            "odoo.orm.models_cached.CachedModel": {
                "symbol": "odoo.orm.models_cached.CachedModel",
                "short_name": "CachedModel",
                "kind": "new_public_class",
                "repo": "odoo",
                "file": "odoo/orm/models_cached.py",
                "first_seen_sha": "abc",
                "first_seen_at": "2026-04-01T00:00:00Z",
                "active_version": "20.0",
                "source": "extracted",
                "note": None,
            }
        }
    }
    wl = Watchlist.from_dict(legacy)
    assert wl.entries["odoo.orm.models_cached.CachedModel"].element is None
