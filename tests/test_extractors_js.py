"""JS extractor: unit tests + an end-to-end pipeline run.

The e2e scenario covers all three anchors:
  - pre-floor: a framework JS file already registers `services` and an
    entry in it (baseline), and owl.js is vendored at 2.8.0.
  - post-floor commit A: framework file gains a new export and a new
    registry category + core entry -> NEW_JS_EXPORT,
    NEW_REGISTRY_CATEGORY, NEW_REGISTRY_ENTRY.
  - post-floor commit B: a non-framework addon adds to the new category
    -> ROLLOUT; its add to baseline `services` stays silent.
  - post-floor commit C: owl.js bumps 2.8.0 -> 3.0.0-alpha.1 ->
    VENDORED_LIB_BUMP.
"""

from pathlib import Path

from ofd import config as config_mod
from ofd import state as state_mod
from ofd import watchlist as watchlist_mod
from ofd.events.record import Kind
from ofd.events.store import iter_repo
from ofd.extractors import js_
from ofd.pipeline import run as run_pipeline
from tests.fixtures.repo_builder import make_repo


def test_module_alias():
    assert js_.module_alias(
        "addons/web/static/src/core/utils/hooks.js"
    ) == "@web/core/utils/hooks"
    # Enterprise layout: addon dirs at the repo root.
    assert js_.module_alias(
        "account_accountant/static/src/components/foo.js"
    ) == "@account_accountant/components/foo"
    # Outside static/src: no alias.
    assert js_.module_alias("addons/web/static/tests/utils.js") is None
    assert js_.module_alias("addons/web/static/lib/owl/owl.js") is None


def test_export_diff_new_and_forms():
    child = """
export class Popover extends Component {}
export function useService(name) { return name; }
export function plainHelper() { return 1; }
export const STATUS = 1;
const local = 2;
export { local };
export { reexported } from "./other";
export default class Hidden {}
export const _private = 3;
"""
    records = js_.extract(None, child, "addons/web/static/src/core/x.js")
    by_symbol = {r.symbol: r for r in records}
    assert all(r.kind is Kind.NEW_JS_EXPORT for r in records)
    assert set(by_symbol) == {
        "@web/core/x.Popover",
        "@web/core/x.useService",
        "@web/core/x.plainHelper",
        "@web/core/x.STATUS",
        "@web/core/x.local",
    }
    # Hook convention stamped for ledger display; others carry the form.
    assert by_symbol["@web/core/x.useService"].symbol_hint == "hook"
    assert by_symbol["@web/core/x.plainHelper"].symbol_hint == "function"
    assert by_symbol["@web/core/x.Popover"].symbol_hint == "class"
    assert by_symbol["@web/core/x.STATUS"].symbol_hint == "const"


def test_export_diff_removed():
    parent = "export function helper(a) { return a; }\n"
    records = js_.extract(parent, "", "addons/web/static/src/core/x.js")
    assert [r.kind for r in records] == [Kind.REMOVED_JS_EXPORT]
    assert records[0].symbol == "@web/core/x.helper"


def test_export_rename_is_removal_plus_addition():
    # Rename folding existed in phase 1 but never fired on the real
    # corpus and was deleted per DESIGN-js.md's own rule; a rename is
    # now an honest removal + addition pair.
    parent = "export function oldName(a) { return a + 1; }\n"
    child = "export function newName(a) { return a + 1; }\n"
    records = js_.extract(parent, child, "addons/web/static/src/core/x.js")
    assert sorted((r.kind, r.symbol) for r in records) == [
        (Kind.NEW_JS_EXPORT, "@web/core/x.newName"),
        (Kind.REMOVED_JS_EXPORT, "@web/core/x.oldName"),
    ]


def test_export_diff_unchanged_is_silent():
    src = "export function helper(a) { return a; }\n"
    assert js_.extract(src, src, "addons/web/static/src/core/x.js") == []


def test_registry_uses_chained_variable_and_computed():
    uses = js_._registry_uses("""
import { registry } from "@web/core/registry";
registry.category("services").add("orm", ormService);
const fieldRegistry = registry.category("fields");
fieldRegistry.add("many2one", { component: M2O });
registry.category(DYNAMIC).add("nope", x);
registry.category("services").add(computed, x);
registry.category(`templated`).add("nope", x);
mySet.add("red_herring");
""")
    assert set(uses.categories) == {"services", "fields"}
    assert set(uses.entries) == {("services", "orm"), ("fields", "many2one")}


def _registry_files(*specs):
    return [
        (file, is_framework, parent, child)
        for file, is_framework, parent, child in specs
    ]


def test_extract_registry_definitions_and_rollout():
    files = _registry_files(
        (
            "addons/web/static/src/core/tooltip/tooltip_service.js", True,
            None, 'registry.category("tooltips").add("tooltip", svc);',
        ),
        (
            "addons/foo/static/src/foo.js", False,
            None,
            'registry.category("tooltips").add("foo_tip", x);\n'
            'registry.category("services").add("foo_service", y);',
        ),
    )
    records = js_.extract_registry(
        files,
        known_symbols=frozenset(),
        baseline=frozenset({"registry.services"}),
    )
    by_kind = {}
    for r in records:
        by_kind.setdefault(r.kind, []).append(r)
    # New category (framework) + its core entry.
    assert [r.symbol for r in by_kind[Kind.NEW_REGISTRY_CATEGORY]] == [
        "registry.tooltips"
    ]
    assert [r.symbol for r in by_kind[Kind.NEW_REGISTRY_ENTRY]] == [
        "registry.tooltips.tooltip"
    ]
    # Same-commit adoption from the non-framework file; the add to the
    # baseline `services` category stays silent.
    assert [r.symbol for r in by_kind[Kind.ROLLOUT]] == ["registry.tooltips"]


def test_extract_registry_known_category_yields_rollout_only():
    files = _registry_files(
        (
            "addons/bar/static/src/bar.js", False,
            None, 'registry.category("tooltips").add("bar_tip", x);',
        ),
    )
    records = js_.extract_registry(
        files,
        known_symbols=frozenset({"registry.tooltips"}),
        baseline=frozenset(),
    )
    assert [r.kind for r in records] == [Kind.ROLLOUT]
    assert records[0].symbol == "registry.tooltips"


def test_extract_registry_wide_scope_category_in_addon():
    # An addon inventing its own registry is a new extension point too.
    files = _registry_files(
        (
            "addons/foo/static/src/foo.js", False,
            None, 'registry.category("foo.handlers").add("a", x);',
        ),
    )
    records = js_.extract_registry(
        files, known_symbols=frozenset(), baseline=frozenset(),
    )
    kinds = [r.kind for r in records]
    assert kinds == [Kind.NEW_REGISTRY_CATEGORY, Kind.ROLLOUT]
    assert records[0].symbol == "registry.foo.handlers"


def test_extract_registry_parent_diff_suppresses_recitation():
    # A modified file that already used the category fires nothing.
    src = 'registry.category("tooltips").add("tip", x);'
    files = _registry_files(
        ("addons/foo/static/src/foo.js", False, src, src + "\nconst a = 1;"),
    )
    assert js_.extract_registry(
        files, known_symbols=frozenset({"registry.tooltips"}),
        baseline=frozenset(),
    ) == []


def test_lib_bump_major_only():
    file = "addons/web/static/lib/owl/owl.js"
    records = js_.extract_lib_bump(
        'const version = "2.8.0";', 'var version = "3.0.0-alpha.33";', file,
    )
    assert [r.kind for r in records] == [Kind.VENDORED_LIB_BUMP]
    assert records[0].symbol == "@odoo/owl"
    assert records[0].symbol_hint == "2.8.0 -> 3.0.0-alpha.33"
    # Minor / patch updates are routine maintenance.
    assert js_.extract_lib_bump(
        'const version = "2.8.1";', 'const version = "2.8.2";', file,
    ) == []
    # Fresh vendoring has no "before" to compare.
    assert js_.extract_lib_bump(None, 'var version = "3.0.0";', file) == []
    # Untracked path.
    assert js_.extract_lib_bump(
        'version = "1.0"', 'version = "2.0"', "addons/web/static/lib/x.js",
    ) == []


def _write_config(workspace: Path, mirror: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "config.yaml").write_text(f"""\
repos:
  odoo:
    source: /dev/null
    mirror: {mirror}
    branch: master
    framework_paths:
      - addons/web/static/src/core/**
    core_paths:
      - addons/web/static/src/core/**
active_version: "20.0"
since_date: "2025-06-01"
key_devs: []
scoring:
  thresholds: {{surface: 3, ledger_threshold: 4, narrate: 5}}
  breadth_bonuses:
    - {{min_rollouts: 5, bonus: 1}}
  dormant_days: 90
  fresh_days: 30
  intent_keywords: [introduce, replace]
narrate:
  backend: claude_code
""")


def _dated(date: str) -> dict[str, str]:
    stamp = f"{date}T12:00:00 +0000"
    return {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}


def test_end_to_end_js_extraction(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    repo = make_repo(tmp_path)

    # Pre-floor baseline: services registry + one entry + owl 2.8.0.
    repo.commit(
        {
            "addons/web/static/src/core/registry.js":
                'export class Registry {}\n'
                'export const registry = new Registry();\n',
            "addons/web/static/src/core/orm_service.js":
                'registry.category("services").add("orm", ormService);\n',
            "addons/web/static/lib/owl/owl.js":
                'const version = "2.8.0";\n',
        },
        subject="[ADD] web: initial framework",
        env=_dated("2025-01-15"),
    )

    # Commit A: new export + new category with a core entry.
    definition_sha = repo.commit(
        {
            "addons/web/static/src/core/tooltip/tooltip_service.js":
                'export function useTooltip(el) { return el; }\n'
                'registry.category("tooltips").add("tooltip", tooltipService);\n',
        },
        subject="[ADD] web: introduce tooltip registry",
        env=_dated("2025-07-01"),
    )

    # Commit B: a non-framework addon adopts the new category; its add
    # to the baseline `services` category stays silent.
    adopter_sha = repo.commit(
        {
            "addons/foo/static/src/foo.js":
                'registry.category("tooltips").add("foo_tip", fooTip);\n'
                'registry.category("services").add("foo_service", fooSvc);\n',
        },
        subject="[IMP] foo: register tooltip",
        env=_dated("2025-07-02"),
    )

    # Commit C: OWL 3 lands.
    bump_sha = repo.commit(
        {"addons/web/static/lib/owl/owl.js": 'var version = "3.0.0-alpha.1";\n'},
        subject="[REF] web: update owl library to owl3",
        env=_dated("2025-07-03"),
    )

    workspace = tmp_path / "ws"
    _write_config(workspace, repo.bare)
    config = config_mod.load(workspace)
    state = state_mod.load()
    watchlist = watchlist_mod.load(workspace)

    summary = run_pipeline(config, state, watchlist)
    assert not summary.errors, summary.errors

    records = {cr.commit.sha: cr for cr in iter_repo(workspace, "odoo")}
    assert set(records) == {definition_sha, adopter_sha, bump_sha}

    # Commit A: export + category + entry, all from the framework path.
    by_kind: dict[Kind, list] = {}
    for c in records[definition_sha].changes:
        by_kind.setdefault(c.kind, []).append(c)
    exports = by_kind[Kind.NEW_JS_EXPORT]
    assert [e.symbol for e in exports] == [
        "@web/core/tooltip/tooltip_service.useTooltip"
    ]
    assert exports[0].symbol_hint == "hook"
    assert exports[0].score >= 3  # base 2 + core_path + [ADD], clamped reasons
    assert [c.symbol for c in by_kind[Kind.NEW_REGISTRY_CATEGORY]] == [
        "registry.tooltips"
    ]
    assert [c.symbol for c in by_kind[Kind.NEW_REGISTRY_ENTRY]] == [
        "registry.tooltips.tooltip"
    ]

    # Commit B: one rollout of the new category, nothing for `services`.
    adopter_changes = records[adopter_sha].changes
    assert [c.kind for c in adopter_changes] == [Kind.ROLLOUT]
    assert adopter_changes[0].symbol == "registry.tooltips"

    # Commit C: the epoch event, loud enough for the ledger on its own.
    bump_changes = records[bump_sha].changes
    assert [c.kind for c in bump_changes] == [Kind.VENDORED_LIB_BUMP]
    assert bump_changes[0].symbol == "@odoo/owl"
    assert bump_changes[0].score >= 4

    # Watchlist: JS symbols persisted with last-segment short names.
    persisted = watchlist_mod.load(workspace)
    assert (
        persisted.entries[
            "@web/core/tooltip/tooltip_service.useTooltip"
        ].short_name == "useTooltip"
    )
    assert persisted.entries["registry.tooltips"].short_name == "tooltips"
    assert (
        persisted.entries["registry.tooltips.tooltip"].short_name == "tooltip"
    )
    assert "@odoo/owl" in persisted.entries
    # Baseline registry symbols never became primitives.
    assert "registry.services" not in persisted.entries
    assert "registry.services.orm" not in persisted.entries
