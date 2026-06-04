"""JS extractor: export diff, registry scan, vendored-lib version sniff.

DESIGN-js.md anchors:

- **Export diff** (framework paths, via the dispatcher): the JS mirror
  of `python_.py`. Parent->child diff of `export class` / `export
  function` / `export const` names in gated files. New exports are
  NEW_JS_EXPORT; removed ones are REMOVED_JS_EXPORT (removals are
  deprecation stories). Phase 1 shipped exact-body rename folding as
  refactor-churn insurance; it never fired across the full 19,774-commit
  reindex, so it was deleted per the spec's own rule.

- **Registry scan** (wide scope, all repos, pipeline stage): the
  `registry.category("services").add("orm", ...)` call is the JS
  analog of `@api.depends_context(...)` - a typed-string registry
  where the framework itself certifies the string as meaningful. A
  new category string is a new extension point (NEW_REGISTRY_CATEGORY);
  a new `.add` in a framework path is a new core entry
  (NEW_REGISTRY_ENTRY); a `.add` in a non-framework path against a
  watchlisted category is a ROLLOUT of that category, emitted by this
  extractor itself like `file_conventions` does - the content matcher
  is not involved. Literal-string args only; computed names are
  skipped silently, same rule as context keys.

- **Vendored-lib sniff** (release_detect pattern): a major-version
  change of `addons/web/static/lib/owl/owl.js` emits one loud
  VENDORED_LIB_BUMP epoch event ("OWL 3 landed") instead of scattered
  per-module migration noise. The migration tooling itself
  (`owl3-migration.py`) is already surfaced by the `upgrade_code/**`
  framework paths.

Symbol naming uses the asset alias - it is what adopting code actually
writes: `addons/web/static/src/core/utils/hooks.js` exports
`useService` as `@web/core/utils/hooks.useService` (short name
`useService`). Registry symbols are `registry.<category>` and
`registry.<category>.<entry>`; short = last segment.

CRITICAL INVARIANT (from documented false-positive history): all
kinds emitted here are JS-specific. NEW_JS_EXPORT adopts via
import-anchored matching in JS scope only (phase 2,
`rollouts._js_import_pattern`); registry and lib-bump kinds carry
empty rollout-language sets. Never let a Python/View kind match in JS
scope or vice versa - the historical `PropertiesDefinition.setup` /
`Transaction.cache` rollout columns were exactly that failure.
"""

from __future__ import annotations

import re
from collections.abc import Container
from dataclasses import dataclass
from pathlib import Path

from ast_grep_py import SgRoot

from ofd import gitio
from ofd.events.record import ChangeRecord, Kind

# `addons/web/static/src/core/utils/hooks.js` -> `@web/core/utils/hooks`.
# The addon prefix may be nested (`odoo/addons/base`) or bare (enterprise
# repos keep addons at the root: `account_accountant/static/src/...`).
_MODULE_ALIAS = re.compile(
    r"^(?:.*/)?(?P<addon>[^/]+)/static/src/(?P<rest>.+)\.js$"
)

# Vendored third-party libs whose major-version bumps are epoch events.
# Path -> the asset alias adopting code imports from.
_VENDORED_LIBS: dict[str, str] = {
    "addons/web/static/lib/owl/owl.js": "@odoo/owl",
}

# `var version = "3.0.0-alpha.33";` (OWL 3 bundle) or
# `const version = "2.8.0";` (OWL 2 bundle). First literal match wins.
_LIB_VERSION = re.compile(r"""\bversion\s*=\s*["']([^"']+)["']""")

_HOOK_NAME = re.compile(r"^use[A-Z]")

_EXPORT_RULE = {"rule": {"kind": "export_statement"}}

# `registry.category(...)` - member call on the bare `registry`
# identifier. The `registry` name is how every Odoo module imports it
# (`import { registry } from "@web/core/registry"`); a differently-named
# import is skipped silently, same as a computed category string.
_CATEGORY_CALL_RULE = {"rule": {
    "kind": "call_expression",
    "has": {
        "field": "function",
        "kind": "member_expression",
        "all": [
            {"has": {"field": "object", "kind": "identifier",
                     "regex": "^registry$"}},
            {"has": {"field": "property", "regex": "^category$"}},
        ],
    },
}}

# Any `<expr>.add(...)` call; the object is qualified in Python below
# (chained category call, or an identifier bound to one in this file).
_ADD_CALL_RULE = {"rule": {
    "kind": "call_expression",
    "has": {
        "field": "function",
        "kind": "member_expression",
        "has": {"field": "property", "regex": "^add$"},
    },
}}


def module_alias(path: str) -> str | None:
    """Asset alias for a JS file path, or None when the path isn't under
    a module's `static/src/` tree (tests, libs, tours)."""
    m = _MODULE_ALIAS.match(path)
    if not m:
        return None
    return f"@{m.group('addon')}/{m.group('rest')}"


# --- export diff -----------------------------------------------------------


@dataclass(frozen=True)
class _Export:
    name: str
    line: int
    form: str  # "class" | "function" | "const"


def _string_literal(node) -> str | None:
    """Literal text of a `string` node, or None for template strings,
    interpolations, and empty strings - the skip-silently rule."""
    if node is None or node.kind() != "string":
        return None
    fragments = [c for c in node.children() if c.kind() == "string_fragment"]
    if len(fragments) != 1:
        return None
    return fragments[0].text() or None


def _exports(source: str | None) -> dict[str, _Export]:
    """Public exported names of a JS module, first-definition-wins."""
    if not source:
        return {}
    try:
        root = SgRoot(source, "javascript").root()
    except Exception:
        return {}
    out: dict[str, _Export] = {}

    def _record(name: str, line: int, form: str) -> None:
        if name.startswith("_"):
            return
        out.setdefault(name, _Export(name=name, line=line, form=form))

    for st in root.find_all(_EXPORT_RULE):
        # `export default <anything>` has no stable adopter-side name
        # (importers pick their own); skip.
        if st.text().startswith("export default"):
            continue
        decl = st.field("declaration")
        if decl is not None:
            kind = decl.kind()
            line = decl.range().start.line + 1
            if kind == "class_declaration":
                name_node = decl.field("name")
                if name_node is not None:
                    _record(name_node.text(), line, "class")
            elif kind in ("function_declaration",
                          "generator_function_declaration"):
                name_node = decl.field("name")
                if name_node is not None:
                    _record(name_node.text(), line, "function")
            elif kind in ("lexical_declaration", "variable_declaration"):
                for d in decl.children():
                    if d.kind() != "variable_declarator":
                        continue
                    name_node = d.field("name")
                    if name_node is None or name_node.kind() != "identifier":
                        continue  # destructuring exports: skip silently
                    _record(name_node.text(), d.range().start.line + 1, "const")
            continue
        # `export { a, b as c }` - bare re-export lists. Ones with a
        # `from "..."` source alias another module's symbol; skip those
        # so the defining module keeps sole ownership.
        if st.field("source") is not None:
            continue
        for clause in st.children():
            if clause.kind() != "export_clause":
                continue
            for spec in clause.children():
                if spec.kind() != "export_specifier":
                    continue
                name_node = spec.field("alias") or spec.field("name")
                if name_node is not None:
                    _record(name_node.text(), spec.range().start.line + 1,
                            "const")
    return out


def _hint(exp: _Export) -> str:
    """Ledger display hint. The hook convention (`function use[A-Z]...`)
    is worth surfacing distinctly; otherwise show the export form."""
    if exp.form == "function" and _HOOK_NAME.match(exp.name):
        return "hook"
    return exp.form


def extract(
    parent_source: str | None,
    child_source: str | None,
    file: str,
) -> list[ChangeRecord]:
    """Export-diff records for this file's parent->child transition."""
    parent = _exports(parent_source)
    child = _exports(child_source)
    module = module_alias(file) or file.removesuffix(".js")

    added = sorted(child.keys() - parent.keys())
    removed = sorted(parent.keys() - child.keys())

    records: list[ChangeRecord] = []
    for name in added:
        exp = child[name]
        records.append(ChangeRecord(
            kind=Kind.NEW_JS_EXPORT,
            file=file,
            line=exp.line,
            symbol=f"{module}.{name}",
            symbol_hint=_hint(exp),
            signature=f"export {exp.form} {name}",
        ))
    for name in removed:
        exp = parent[name]
        records.append(ChangeRecord(
            kind=Kind.REMOVED_JS_EXPORT,
            file=file,
            line=exp.line,
            symbol=f"{module}.{name}",
            symbol_hint=_hint(exp),
            signature=f"export {exp.form} {name}",
        ))
    return records


# --- registry scan ---------------------------------------------------------


@dataclass(frozen=True)
class _RegistryUses:
    categories: dict[str, int]            # category -> first line
    entries: dict[tuple[str, str], int]   # (category, entry) -> first line


def _category_of_call(node) -> str | None:
    """Literal category string when `node` is a `registry.category("...")`
    call, else None."""
    if node is None or node.kind() != "call_expression":
        return None
    if not node.matches(**_CATEGORY_CALL_RULE["rule"]):
        return None
    args = node.field("arguments")
    if args is None:
        return None
    for arg in args.children():
        if arg.kind() == "string":
            return _string_literal(arg)
        if arg.kind() not in ("(", ")", ","):
            return None  # computed name: skip silently
    return None


def _registry_uses(source: str | None) -> _RegistryUses:
    """Every literal-string registry category and `.add` entry in this
    source. Handles the chained form (`registry.category("x").add("y")`)
    and the single-file variable form (`const r = registry.category("x");
    r.add("y", ...)`)."""
    if not source:
        return _RegistryUses({}, {})
    try:
        root = SgRoot(source, "javascript").root()
    except Exception:
        return _RegistryUses({}, {})

    categories: dict[str, int] = {}
    var_categories: dict[str, str] = {}
    for call in root.find_all(_CATEGORY_CALL_RULE):
        cat = _category_of_call(call)
        if cat is None:
            continue
        categories.setdefault(cat, call.range().start.line + 1)
        # `const fieldRegistry = registry.category("fields")` binds the
        # category to a name `.add` calls can reference later.
        parent = call.parent()
        if parent is not None and parent.kind() == "variable_declarator":
            name_node = parent.field("name")
            if name_node is not None and name_node.kind() == "identifier":
                var_categories[name_node.text()] = cat

    entries: dict[tuple[str, str], int] = {}
    for call in root.find_all(_ADD_CALL_RULE):
        fn = call.field("function")
        obj = fn.field("object") if fn is not None else None
        if obj is None:
            continue
        cat = _category_of_call(obj)
        if cat is None and obj.kind() == "identifier":
            cat = var_categories.get(obj.text())
        if cat is None:
            continue  # `.add` on something that isn't a registry category
        args = call.field("arguments")
        if args is None:
            continue
        first_arg = next(
            (a for a in args.children() if a.kind() not in ("(", ")", ",")),
            None,
        )
        name = _string_literal(first_arg)
        if name is None:
            continue  # computed entry name: skip silently
        entries.setdefault((cat, name), call.range().start.line + 1)
    return _RegistryUses(categories=categories, entries=entries)


def extract_registry(
    files: list[tuple[str, bool, str | None, str | None]],
    known_symbols: Container[str],
    baseline: frozenset[str] | set[str] | None = None,
) -> list[ChangeRecord]:
    """Commit-level registry events across this commit's needle-gated
    JS files.

    `files` holds (path, is_framework_path, parent_source, child_source)
    tuples. `known_symbols` is the watchlist symbol set; `baseline` is
    the full-tree registry symbol set at the tracking-window floor
    (categories AND entries, as `registry.<cat>` / `registry.<cat>.<name>`).

    Per file we diff parent->child so re-citations of an old category in
    a modified file never fire. Categories are wide-scope (an addon
    inventing its own registry is a new extension point too); entries
    are definitions only in framework paths, and rollouts of the parent
    category elsewhere - including same-commit adoption of a category
    defined in another file of this commit.
    """
    base = baseline or frozenset()
    per_file: list[tuple[str, bool, dict, dict]] = []
    for file, is_framework, parent_source, child_source in files:
        p = _registry_uses(parent_source)
        c = _registry_uses(child_source)
        new_cats = {
            cat: line for cat, line in c.categories.items()
            if cat not in p.categories
        }
        new_entries = {
            key: line for key, line in c.entries.items()
            if key not in p.entries
        }
        if new_cats or new_entries:
            per_file.append((file, is_framework, new_cats, new_entries))

    records: list[ChangeRecord] = []
    commit_cats: set[str] = set()
    for file, _is_framework, new_cats, _new_entries in per_file:
        for cat, line in sorted(new_cats.items()):
            sym = f"registry.{cat}"
            if sym in base or sym in known_symbols or sym in commit_cats:
                continue
            commit_cats.add(sym)
            records.append(ChangeRecord(
                kind=Kind.NEW_REGISTRY_CATEGORY,
                file=file,
                line=line,
                symbol=sym,
                registry=cat,
            ))

    commit_entries: set[str] = set()
    for file, is_framework, _new_cats, new_entries in per_file:
        for (cat, name), line in sorted(new_entries.items()):
            cat_sym = f"registry.{cat}"
            if is_framework:
                entry_sym = f"registry.{cat}.{name}"
                if (
                    entry_sym in base
                    or entry_sym in known_symbols
                    or entry_sym in commit_entries
                ):
                    continue
                commit_entries.add(entry_sym)
                records.append(ChangeRecord(
                    kind=Kind.NEW_REGISTRY_ENTRY,
                    file=file,
                    line=line,
                    symbol=entry_sym,
                    registry=cat,
                ))
            elif cat_sym in known_symbols or cat_sym in commit_cats:
                records.append(ChangeRecord(
                    kind=Kind.ROLLOUT,
                    file=file,
                    line=line,
                    symbol=cat_sym,
                    registry=cat,
                ))
    return records


def scan_baseline_registry(mirror: Path, baseline_sha: str) -> set[str]:
    """Registry symbols (categories and entries) already present at
    <baseline_sha>. Cheap `git grep -F` pre-filter, then the same
    literal-string parse `extract_registry` uses."""
    candidates = gitio.grep_files(
        mirror, baseline_sha, "registry.category", pathspec="*.js",
    )
    symbols: set[str] = set()
    for path in candidates:
        uses = _registry_uses(gitio.show_blob(mirror, baseline_sha, path))
        symbols.update(f"registry.{cat}" for cat in uses.categories)
        symbols.update(
            f"registry.{cat}.{name}" for cat, name in uses.entries
        )
    return symbols


# --- vendored-lib version sniff --------------------------------------------


def vendored_lib_alias(path: str) -> str | None:
    """Asset alias when `path` is a tracked vendored lib bundle."""
    return _VENDORED_LIBS.get(path)


def _lib_version(source: str | None) -> tuple[str, int] | None:
    """(version, line) of the first version constant in a lib bundle."""
    if not source:
        return None
    m = _LIB_VERSION.search(source)
    if not m:
        return None
    return m.group(1), source.count("\n", 0, m.start()) + 1


def _major(version: str) -> str:
    return version.split(".", 1)[0]


def extract_lib_bump(
    parent_source: str | None,
    child_source: str | None,
    file: str,
) -> list[ChangeRecord]:
    """One epoch event when a vendored lib's major version changes.

    Minor/patch updates are routine maintenance and stay silent; a file
    addition (no parent) predating the floor is handled by the walk
    window, and a fresh vendoring mid-window has no "before" to compare,
    so both sides must parse for the event to fire.
    """
    alias = vendored_lib_alias(file)
    if alias is None:
        return []
    old = _lib_version(parent_source)
    new = _lib_version(child_source)
    if old is None or new is None:
        return []
    old_version, _ = old
    new_version, new_line = new
    if _major(old_version) == _major(new_version):
        return []
    return [ChangeRecord(
        kind=Kind.VENDORED_LIB_BUMP,
        file=file,
        line=new_line,
        symbol=alias,
        symbol_hint=f"{old_version} -> {new_version}",
        before_signature=old_version,
        after_signature=new_version,
    )]
