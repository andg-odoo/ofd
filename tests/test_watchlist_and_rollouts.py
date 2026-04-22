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
