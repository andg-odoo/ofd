"""OWL3-migration and retired-frontend-dependency epoch extractors.

Both close a recall gap: a framework story that leaves no new symbol for
the primitive extractors to define or adopt (an API swap, a dependency
removal), so they emit rollouts explicitly from the commit subject + diff.
"""

from ofd.events.record import Kind
from ofd.extractors import owl_migration, retired_deps, test_conventions

# --- OWL 3 migration --------------------------------------------------------


def test_owl3_migration_subjects_attribute_one_owl_rollout():
    for subject in (
        "[REF] web: Replace useLayoutEffect",
        "[REF] *: Owl3 - replace useState with proxy",
        "[REF] mail: replace onWillUpdateProps with owl3 alternatives",
        "[REF] account: replace t-custom-model with t-model/t-model.proxy",
        "[IMP] mail: use new props syntax OWL 3 (part 2)",
        "[REF] mail: replace useExternalListener by useListener",
        "[REF] *: Owl3 - replace one-arg reactive calls with proxy",
    ):
        records = owl_migration.extract(subject, ["addons/web/static/src/x.js"])
        assert len(records) == 1, subject
        r = records[0]
        assert r.kind is Kind.ROLLOUT
        assert r.symbol == "@odoo/owl"
        assert r.symbol_hint == "owl3-migration"


def test_owl3_migration_needs_frontend_files():
    # Subject matches but the commit only touches Python/docs: not an adoption.
    assert owl_migration.extract(
        "[REF] web: Replace useLayoutEffect", ["odoo/models/foo.py", "README.md"],
    ) == []


def test_owl3_migration_skips_the_lib_bump_commit():
    # The commit bumping owl.js is the VENDORED_LIB_BUMP definition; it must
    # not also count as a migration rollout of itself.
    assert owl_migration.extract(
        "[REF] web: update owl library to owl3",
        ["addons/web/static/lib/owl/owl.js"],
    ) == []


def test_owl3_migration_ignores_unrelated_subjects():
    for subject in (
        "[FIX] web: useEffect cleanup fires twice",   # useEffect != useLayoutEffect
        "[IMP] mail: make the reactive store faster",  # reactive without proxy
        "[FIX] account: fix proxy settings parsing",   # proxy without owl term
        "[ADD] web: new dashboard view",
    ):
        assert owl_migration.extract(
            subject, ["addons/web/static/src/x.js"],
        ) == [], subject


# --- retired front-end dependency (jQuery) ----------------------------------

_JQUERY_REMOVAL_PATCH = {
    "addons/web/static/src/legacy/utils.js": (
        "--- a/addons/web/static/src/legacy/utils.js\n"
        "+++ b/addons/web/static/src/legacy/utils.js\n"
        "@@ -1,3 +1,2 @@\n"
        '-import jQuery from "jquery";\n'
        "-    jQuery(el).hide();\n"
        "     el.classList.add('d-none');\n"
    ),
}


def test_jquery_retirement_emits_definition_then_rollout():
    # First commit: no prior watchlist entry -> definition epoch + rollout.
    records = retired_deps.extract(
        "[REM] web: remove jQuery from codebase", _JQUERY_REMOVAL_PATCH, {},
    )
    kinds = [(r.kind, r.symbol, r.symbol_hint) for r in records]
    assert (Kind.DEPENDENCY_CHANGE, "frontend.jquery", "removed") in kinds
    assert (Kind.ROLLOUT, "frontend.jquery", "removed") in kinds

    # Follow-up commit, symbol already watchlisted -> rollout only (breadth).
    followup = retired_deps.extract(
        "[REM] *: remove jQuery from all assets",
        _JQUERY_REMOVAL_PATCH, {"frontend.jquery": object()},
    )
    assert [r.kind for r in followup] == [Kind.ROLLOUT]


def test_jquery_net_addition_does_not_fire():
    # Subject mentions jQuery but the diff *adds* references (a shim, say).
    patch = {"x.js": (
        "@@ -1 +1,2 @@\n"
        "     const a = 1;\n"
        '+import jQuery from "jquery";\n'
    )}
    assert retired_deps.extract("[IMP] web: jquery compatibility shim", patch, {}) == []


def test_retired_dep_subject_gate():
    assert retired_deps.mentions_retired_dep("[REM] web: remove jQuery")
    assert not retired_deps.mentions_retired_dep("[FIX] web: tidy imports")
    # Unrelated subject short-circuits even with a jquery-shaped diff.
    assert retired_deps.extract("[FIX] web: tidy imports", _JQUERY_REMOVAL_PATCH, {}) == []


def test_net_removed_ignores_diff_headers():
    # The `--- a/..jquery..` / `+++ b/..jquery..` headers must not count as
    # content removals/additions.
    patch = {"jquery_widget.js": (
        "--- a/jquery_widget.js\n"
        "+++ b/jquery_widget.js\n"
        "@@ -1 +0,0 @@\n"
        "-jQuery.fn.foo = 1;\n"
    )}
    assert retired_deps._net_removed(("jquery",), patch) == 1


# --- retired front-end dependency (Font Awesome) -----------------------------

_FA_MIGRATION_PATCH = {
    "addons/web/static/src/views/fields/badge.xml": (
        "--- a/addons/web/static/src/views/fields/badge.xml\n"
        "+++ b/addons/web/static/src/views/fields/badge.xml\n"
        "@@ -1,2 +1,2 @@\n"
        '-    <i class="fa fa-check"/>\n'
        '+    <i class="oi" data-icon="check"/>\n'
    ),
}


def test_fontawesome_retirement_fires_on_replacement_subject():
    # Migration subjects name the NEW system (Material Symbols), not FA.
    records = retired_deps.extract(
        "[IMP] web, *: add Material Symbols icon system", _FA_MIGRATION_PATCH, {},
    )
    kinds = [(r.kind, r.symbol, r.symbol_hint) for r in records]
    assert (Kind.DEPENDENCY_CHANGE, "frontend.fontawesome", "removed") in kinds
    assert (Kind.ROLLOUT, "frontend.fontawesome", "removed") in kinds

    followup = retired_deps.extract(
        "[IMP] mass_mailing, website, *: apply data-icon in snippets",
        _FA_MIGRATION_PATCH, {"frontend.fontawesome": object()},
    )
    assert [r.kind for r in followup] == [Kind.ROLLOUT]
    assert followup[0].symbol == "frontend.fontawesome"


def test_fontawesome_needles_counted_once_per_line():
    # A line matching two diff needles must count once, and the lib css
    # removal (font-awesome.css paths) counts via the file-content lines.
    patch = {"addons/web/static/src/scss/x.scss": (
        "@@ -1,2 +0,0 @@\n"
        "-@import 'fontawesome/css/font-awesome';\n"
        "-font-family: FontAwesome;\n"
    )}
    assert retired_deps._net_removed(
        ("fontawesome", "font-awesome"), patch,
    ) == 2


def test_fontawesome_subject_gate_covers_campaign_forms():
    for subject in (
        "[IMP] *: remove FontAwesome font-family",
        "[IMP] html_editor, html_builder, *: adapt media dialog for MS icons",
        "[IMP] web, *: add Material Symbols icon system",
    ):
        assert retired_deps.mentions_retired_dep(subject), subject
    # Icon-ish but unrelated subjects stay out.
    assert not retired_deps.mentions_retired_dep(
        "[FIX] website: support favicon for iOS PWA",
    )


# --- test-writing conventions (_test_user_groups) ---------------------------


def _patch(*lines):
    return "\n".join(lines)


def test_test_convention_real_value_counts():
    """A class declaring real groups (odoo#273014 commit 1) emits the
    epoch definition plus one rollout for the file."""
    patches = {
        "addons/product/tests/common.py": _patch(
            "@@ -8,0 +9,4 @@",
            "+    _test_user_groups = (",
            "+        'product.group_product_manager',",
            "+    )",
        ),
    }
    records = test_conventions.extract(patches, known_symbols=set())
    assert [r.kind for r in records] == [Kind.NEW_TEST_CONVENTION, Kind.ROLLOUT]
    assert all(r.symbol == "tests._test_user_groups" for r in records)


def test_test_convention_none_placeholder_ignored():
    """The 761-file FIXME stamp must not count as adoption."""
    patches = {
        "addons/mrp/tests/test_bom.py": _patch(
            "+    _test_user_groups = None  # FIXME list needed groups",
        ),
        "addons/mrp/tests/test_order.py": _patch(
            "+    _test_user_groups = None",
        ),
    }
    assert test_conventions.extract(patches, known_symbols=set()) == []


def test_test_convention_placeholder_conversion_counts():
    """The chm rollout branches convert None -> real tuple; that IS the
    adoption curve."""
    patches = {
        "addons/website/tests/test_views.py": _patch(
            "-    _test_user_groups = None  # FIXME list needed groups",
            "+    _test_user_groups = ('website.group_website_designer',)",
        ),
        "addons/website/tests/test_menu.py": _patch(
            "-    _test_user_groups = None  # FIXME list needed groups",
            "+    _test_user_groups = ('base.group_user',)",
        ),
    }
    records = test_conventions.extract(
        patches, known_symbols={"tests._test_user_groups"},
    )
    assert [r.kind for r in records] == [Kind.ROLLOUT, Kind.ROLLOUT]
    assert {r.file for r in records} == {
        "addons/website/tests/test_views.py",
        "addons/website/tests/test_menu.py",
    }


def test_test_convention_known_symbol_skips_definition():
    patches = {
        "addons/sale/tests/common.py": _patch(
            "+    _test_user_groups = ('sales_team.group_sale_manager',)",
        ),
    }
    records = test_conventions.extract(
        patches, known_symbols={"tests._test_user_groups"},
    )
    assert [r.kind for r in records] == [Kind.ROLLOUT]


def test_test_convention_only_python_test_files():
    """Non-test files never fire, even with the needle in the diff (docs,
    the mechanism's own file is odoo/addons/base/tests/ so it IS a test
    file - the definition correctly lands there)."""
    patches = {
        "addons/sale/models/sale_order.py": _patch(
            "+    _test_user_groups = ('base.group_user',)",
        ),
        "odoo/tools/misc.py": _patch(
            "+    _test_user_groups = ('base.group_user',)",
        ),
    }
    assert test_conventions.extract(patches, known_symbols=set()) == []


def test_test_convention_pipeline_gate():
    assert test_conventions.touches_test_files(
        ["addons/sale/tests/test_sale_order.py"],
    )
    assert not test_conventions.touches_test_files(
        ["addons/sale/models/sale_order.py", "addons/web/static/tests/x.js"],
    )


def test_test_convention_none_removal_is_inheritance_opt_in():
    """The master-tests-*-chm branches delete the placeholder so the class
    inherits its Common base's real groups - that IS the opt-in."""
    patches = {
        "addons/mrp/tests/test_bom.py": _patch(
            "-    _test_user_groups = None  # FIXME list needed groups",
        ),
    }
    records = test_conventions.extract(
        patches, known_symbols={"tests._test_user_groups"},
    )
    assert [r.kind for r in records] == [Kind.ROLLOUT]


def test_test_convention_real_value_removal_not_counted():
    """Dropping an explicit real value (class now inherits) is cleanup of
    an already-opted-in class, not new adoption."""
    patches = {
        "addons/sale/tests/test_sale.py": _patch(
            "-    _test_user_groups = ('sales_team.group_sale_salesman',)",
        ),
    }
    assert test_conventions.extract(
        patches, known_symbols={"tests._test_user_groups"},
    ) == []


def test_test_convention_none_line_move_not_counted():
    patches = {
        "addons/stock/tests/test_move.py": _patch(
            "-    _test_user_groups = None  # FIXME list needed groups",
            "+    _test_user_groups = None  # FIXME list needed groups",
        ),
    }
    assert test_conventions.extract(
        patches, known_symbols={"tests._test_user_groups"},
    ) == []


# --- OWL env-removal campaign ------------------------------------------------


def test_env_removal_subjects_emit_owl_rollout():
    for subject in (
        "[REF] *: replace env.isSmall by ui service",
        "[REF] *: replace makeMockEnv by makeTestEnv",
        "[IMP] spreadsheet: remove isDashboard from env",
        "[IMP] spreadsheet: remove isDashboard from the env",
    ):
        records = owl_migration.extract(subject, ["addons/web/static/src/x.js"])
        assert len(records) == 1, subject
        assert records[0].symbol == "@odoo/owl"
        assert records[0].symbol_hint == "owl3-env-removal"


def test_env_removal_ignores_unrelated_env_subjects():
    for subject in (
        "[FIX] web: env vars not passed to subprocess",   # os env, not owl
        "[IMP] mail: replace environment banner",          # no env member
        "[FIX] orm: remove record from env cache",         # cache, not env member
    ):
        assert owl_migration.extract(
            subject, ["addons/web/static/src/x.js"],
        ) == [], subject


def test_service_to_plugin_subjects_emit_owl_rollout():
    for subject in (
        "[REF] web: convert title service to plugin",
        "[REF] web,upgrade_code: convert currency service to plugin",
        "[REF] mail: convert discuss.upgrade service to plugin",
        "convert offline service to offline plugin",
        "[REF] web: use offline plugin instead of service",
    ):
        records = owl_migration.extract(subject, ["addons/web/static/src/x.js"])
        assert len(records) == 1, subject
        assert records[0].symbol == "@odoo/owl"
        assert records[0].symbol_hint == "owl3-service-to-plugin"


def test_service_to_plugin_recipe_only_commit_not_counted():
    """The upgrade_code recipe addition (convert mobile useService to
    plugin) is tooling, not front-end adoption."""
    assert owl_migration.extract(
        "[REF] upgrade_code: convert mobile useService to plugin",
        ["odoo/upgrade_code/owl3-migration.py"],
    ) == []


def test_service_to_plugin_ignores_unrelated_subjects():
    for subject in (
        "[FIX] web: tooltip_service: avoid nested double tooltip",
        "[IMP] html_editor: new plugin for tables",
        "[FIX] mail: service worker cache invalidation",
    ):
        assert owl_migration.extract(
            subject, ["addons/web/static/src/x.js"],
        ) == [], subject
