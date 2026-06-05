"""Rollout detection.

Given a commit's unified diffs for files outside framework paths, scan
each file's hunks for usage of watchlisted short names. For each hit,
capture the surrounding before/after snippet so stage-3 gets a ready
slide example.

A naive `short_name in line` match produces huge false-positive rates
for generic names like `join`, `default`, `help`. The pipeline applies
four filtering stages, in order:

0. Language gate (`_file_language` + `_KIND_LANGUAGES`). Files whose
   extension isn't in `_FILE_LANGUAGES` (.py, .xml, .rng, .js) are
   skipped wholesale - we don't extract primitives from .po / .csv /
   .html, so a Python primitive matching `setup()` on every OWL
   component or a context-key name appearing as a JS Record field is a
   pure cross-language false positive. Within a scanned file, entries
   whose source language doesn't include this file's language are
   dropped (NEW_KWARG `compute_sql` doesn't fire in XML; NEW_VIEW_*
   doesn't fire in .py; JS exports match only on import lines in .js,
   never the other way around).

1. Aho-Corasick prefilter (`_Matcher.automaton`). One pass over the
   patch reports which watchlisted short names are present. Replaces
   both the file-level `\\b(a|b|...)\\b` regex screen and the per-entry
   `short in added_blob` substring loop. Single shared automaton across
   the whole watchlist - cost is flat in N.

2. Contextual regex (`_contextual_pattern`). Per-entry pattern that
   only matches when the identifier appears in a syntactic position
   implying *use* (attribute, call, kwarg, import, class, def,
   decorator, annotation, quoted string). Filters out comments and
   string-literal noise the AC pass can't see.

3. ast-grep structural qualifier (`_ast_qualifies`, `.py` files only).
   For specific names: tree-sitter parse confirms an actual identifier
   or quoted-string token of the name exists (kills the residual
   comment bleed-through the regex misses, e.g. `# Query is
   deprecated`). For generic names (`_GENERIC_SHORT_NAMES`) on kinds
   in `_RELAX_GENERIC_KINDS`, applies a stricter kind-shaped rule
   (kwarg position, parameter form, class-body assignment, etc.) -
   precise enough to replace the import-only gate the contextual
   regex used to enforce, unlocking real adoptions previously hidden
   behind it.

Generic names in kinds NOT in `_RELAX_GENERIC_KINDS` (notably
NEW_DECORATOR_OR_HELPER) keep the import-only gate: a generic helper
name like `join` matches `",".join(items)` everywhere, and the
qualifier can't structurally distinguish `Many2many.join(...)` from
`str.join(...)` without runtime type info.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache

import ahocorasick
from ast_grep_py import SgRoot
from lxml import etree

from ofd.events.record import ChangeRecord, Kind
from ofd.watchlist import Watchlist, WatchlistEntry

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@.*$")

# Cap on the added-side text we'll scan per hunk. Above ~100 KB the
# contextual regex degrades sharply even with the anchored-import fix:
# a 90s py-spy saw 51s on a single call on a commit that passed the
# previous 512 KB cap. Any primitive hiding in a single hunk larger
# than 128 KB is a mass refactor / generated file, not slide content.
_MAX_HUNK_CHARS = 128 * 1024
_FILE_HEADER = re.compile(r"^\+\+\+ b/(.+)$")
_CLASS_LINE = re.compile(r"^\s*class\s+(\w+)")
_MODEL_ATTR = re.compile(r"""_name\s*=\s*['"]([^'"]+)['"]""")
_INHERIT_ATTR = re.compile(r"""_inherit\s*=\s*['"]([^'"]+)['"]""")

# Kinds where the ast-grep qualifier provides enough structural
# discrimination to drop the import-only gate for generic short names.
# NEW_DECORATOR_OR_HELPER is excluded: a generic helper name like `join`
# matches `",".join(items)` everywhere, and the qualifier can't tell
# `Many2many.join(...)` from `str.join(...)` without runtime type info.
_RELAX_GENERIC_KINDS = frozenset({
    Kind.NEW_KWARG,
    Kind.SIGNATURE_CHANGE,
    Kind.NEW_CLASS_ATTRIBUTE,
    Kind.NEW_PUBLIC_CLASS,
})

# Source language of a primitive's adoption surface. Drives which file
# extensions we even open looking for a rollout.
class Language(StrEnum):
    PY = "py"      # Python source (.py)
    VIEW = "view"  # XML / RNG view source (.xml, .rng)
    JS = "js"      # JavaScript source (.js)
    QWEB = "qweb"  # OWL component templates (static/src/**/*.xml)


# Languages each primitive kind can legitimately adopt in. A diff file's
# language must be in the kind's set, otherwise the entry is dropped
# before we ever run the contextual regex - the cheapest way to kill
# whole classes of cross-language false positives (an OWL `setup()` is
# not an adoption of `PropertiesDefinition.setup`; a JS Record's
# `partner_id = fields.One(...)` is not a `partner_id` context-key use).
#
# - PY-only kinds: NEW_KWARG/SIGNATURE_CHANGE never appear in XML; XML
#   has no kwargs or call signatures. NEW_CONTEXT_KEY is PY-only because
#   most context keys collide with model field names and `<field
#   name="employee_id"/>` is a field reference, not an adoption.
# - PY+VIEW kinds: a class / helper / class-attribute *name* can leak
#   into XML as a string (`<field name="formatted_display_name"/>`,
#   QWeb `record.formatted_display_name`). Generic short names in this
#   group are blocked from VIEW separately by `_GENERIC_BLOCKED_IN_VIEW`.
# - VIEW-only kinds: pure RNG-derived primitives.
# - PY+VIEW for NEW_VIEW_TYPE: types like 'kanban' / 'list' get
#   referenced both in Python action defs and XML view definitions.
_KIND_LANGUAGES: dict[Kind, frozenset[Language]] = {
    Kind.NEW_KWARG:                 frozenset({Language.PY}),
    Kind.SIGNATURE_CHANGE:          frozenset({Language.PY}),
    Kind.NEW_CONTEXT_KEY:           frozenset({Language.PY}),
    Kind.NEW_ENDPOINT:              frozenset({Language.PY}),
    Kind.DEPRECATION_WARNING_ADDED: frozenset({Language.PY}),
    Kind.REMOVED_PUBLIC_SYMBOL:     frozenset({Language.PY}),
    Kind.NEW_PUBLIC_CLASS:          frozenset({Language.PY, Language.VIEW}),
    Kind.NEW_DECORATOR_OR_HELPER:   frozenset({Language.PY, Language.VIEW}),
    Kind.NEW_CLASS_ATTRIBUTE:       frozenset({Language.PY, Language.VIEW}),
    Kind.NEW_VIEW_TYPE:             frozenset({Language.PY, Language.VIEW}),
    # NEW_VIEW_ATTRIBUTE also matches in QWEB so manual attr-needle
    # pins (`data-available-offline`) can track template adoption;
    # extracted RNG entries are element-scoped (`<widget ... attr=`)
    # and their host elements don't occur in OWL templates, so the
    # extra scope is inert for them.
    Kind.NEW_VIEW_ATTRIBUTE:        frozenset({Language.VIEW, Language.QWEB}),
    Kind.NEW_VIEW_ELEMENT:          frozenset({Language.VIEW}),
    Kind.NEW_VIEW_DIRECTIVE:        frozenset({Language.VIEW}),
    Kind.REMOVED_VIEW_ATTRIBUTE:    frozenset({Language.VIEW}),
    # File conventions are path-shaped, not content-shaped: their
    # adoptions are emitted by the file_conventions extractor itself
    # (a module *adding* a file with the watchlisted basename), so
    # content scanning never applies. Manifest keys are the same
    # (extractor-emitted on later manifests adding the key), and a
    # dependency change has no adoption surface at all.
    Kind.NEW_FILE_CONVENTION:       frozenset(),
    Kind.NEW_MANIFEST_KEY:          frozenset(),
    Kind.DEPENDENCY_CHANGE:         frozenset(),
    # Modules adopt via `depends` additions, emitted by the modules
    # extractor itself; their snake_case names are ordinary words the
    # content matcher must never scan for. REMOVED_MODULE isn't a
    # definition kind; its row is inert symmetry.
    Kind.NEW_MODULE:                frozenset(),
    Kind.REMOVED_MODULE:            frozenset(),
    # JS primitives (DESIGN-js.md, phases 2+3). Exports adopt via
    # import lines in .js (`_js_import_pattern`) and via component
    # tags (`<BadgeTag`) in OWL templates - QWEB scope, phase 3.
    # INVARIANT: never add Language.JS to any Python/View kind above -
    # cross-language content matching is the documented false-positive
    # factory (PropertiesDefinition.setup / Transaction.cache).
    # REMOVED_JS_EXPORT isn't a definition kind so its row is inert;
    # it carries JS for symmetry with REMOVED_PUBLIC_SYMBOL.
    Kind.NEW_JS_EXPORT:             frozenset({Language.JS, Language.QWEB}),
    Kind.REMOVED_JS_EXPORT:         frozenset({Language.JS}),
    # Registry adoptions are emitted by the js_ extractor itself, like
    # file conventions - a category/entry short name ("tooltips") has
    # no import-line adoption shape, so the content matcher never runs.
    Kind.NEW_REGISTRY_CATEGORY:     frozenset(),
    Kind.NEW_REGISTRY_ENTRY:        frozenset(),
    Kind.VENDORED_LIB_BUMP:         frozenset(),
}

# PY+VIEW kinds where a *generic* short name (in `_GENERIC_SHORT_NAMES`)
# is too dangerous to allow in VIEW scope. A NEW_DECORATOR_OR_HELPER
# called `name` would match every `<field name=.../>` in the tree; the
# specific-name case (`formatted_display_name`) is still allowed.
_GENERIC_BLOCKED_IN_VIEW: frozenset[Kind] = frozenset({
    Kind.NEW_PUBLIC_CLASS,
    Kind.NEW_DECORATOR_OR_HELPER,
    Kind.NEW_CLASS_ATTRIBUTE,
})

# File extension -> language. Files outside this map (.po, .csv,
# .html, .scss, ...) are skipped wholesale. We don't extract primitives
# from those formats, and matching Python/View primitives there has
# only produced false positives in the wild (the entire JS rollout
# column of `PropertiesDefinition.setup` and `Transaction.cache` was
# bogus - OWL lifecycle methods and unrelated `cache` properties).
# `.js` maps to a language whose only members are the JS kinds, so
# Python/View primitives still never scan a JS file and vice versa.
_FILE_LANGUAGES: tuple[tuple[tuple[str, ...], Language], ...] = (
    ((".py",), Language.PY),
    ((".xml", ".rng"), Language.VIEW),
    ((".js",), Language.JS),
)

# Names that alias too many unrelated builtins / common idioms to be
# matched outside an explicit import. Hand-curated; extend cautiously.
_GENERIC_SHORT_NAMES: frozenset[str] = frozenset({
    # Python list/string/dict methods.
    "join", "split", "splitlines", "strip", "lstrip", "rstrip",
    "replace", "startswith", "endswith", "format", "encode", "decode",
    "add", "remove", "pop", "push", "discard", "clear", "copy",
    "extend", "insert", "append", "count", "index", "sort", "reverse",
    "update", "items", "keys", "values", "get", "set", "setdefault",
    # Generic english / framework-agnostic.
    "default", "name", "value", "type", "data", "info", "state",
    "cache", "flush", "reset", "init", "close", "open", "read",
    "write", "save", "load", "delete", "create", "find", "match",
    "change", "duplicate", "attributes",
    # Odoo ORM / web vocabulary - ubiquitous on every model or request,
    # so a new helper sharing the name matches every `.unlink()` /
    # `request.env` in the tree. Proven columns (2026-06-04 corpus):
    # ResCountry.unlink 661 bogus rollouts, Dispatcher.request 359,
    # Manifest.raw_value 149 (kanban `record.x.raw_value` template
    # expressions), FormatVatLabelMixin.fields_get 45.
    "unlink", "request", "raw_value", "fields_get",
    # `Environment.website` (2026-06-05) collides with the `website`
    # URL *field* on res.partner/res.company: `partner.website` is an
    # attribute read the ast qualifier can't tell from `env.website`,
    # and `<field name="website"/>` matches in VIEW scope. Its genuine
    # breadth (274 rollouts on conversion day) is already persisted;
    # import-only matching from here on just stops the FP drip.
    "website",
    # Ubiquitous parameter names - NEW_KWARG sub-symbols like
    # `SomeMethod.ids` would else match every `.ids` / `ids=` in Odoo.
    "ids", "id", "query", "table", "kind", "it", "model", "record",
    "records", "env", "context", "ctx", "domain", "field", "fields",
    "key", "arg", "args", "kwargs", "func", "method", "attr", "attrs",
    "path", "view", "views", "obj", "cls", "item", "result", "results",
    # Dunders - always ambiguous.
    "__eq__", "__hash__", "__repr__", "__str__", "__init__",
    "__call__", "__getitem__", "__setitem__", "__delitem__",
    "__enter__", "__exit__", "__iter__", "__next__", "__len__",
    "__contains__",
})

# JS short names blocked from import-anchored matching. Import lines
# are name-anchored with *any* from-string (the localeCompare barrel
# finding, DESIGN-js.md), so a new export colliding with one of these
# would match the existing `import { Component } from "@odoo/owl"` in
# every component file. OWL's public vocabulary plus the handful of
# `@web/core` exports cited from nearly every addon. With import-only
# anchoring this list is belt-and-suspenders, but the historical
# PropertiesDefinition.setup / Transaction.cache failures earn it.
_JS_GENERIC_SHORT_NAMES: frozenset[str] = frozenset({
    # OWL component vocabulary + lifecycle hooks.
    "Component", "App", "EventBus", "xml", "css", "markup", "mount",
    "reactive", "toRaw", "status", "validate", "whenReady", "loadFile",
    "useState", "useRef", "useEffect", "useEnv", "useSubEnv",
    "useChildSubEnv", "useComponent", "useExternalListener",
    "onMounted", "onWillStart", "onWillUnmount", "onWillUpdateProps",
    "onWillRender", "onRendered", "onPatched", "onWillDestroy",
    "onError", "setup", "props", "state", "env", "render", "template",
    # `@web/core` exports imported from nearly every addon file.
    "registry", "useService", "useBus", "browser", "session", "user",
    "rpc", "Domain", "memoize", "debounce", "throttleForAnimation",
})


@lru_cache(maxsize=512)
def _directive_pattern(name: str) -> re.Pattern[str]:
    """Pattern for NEW_VIEW_DIRECTIVE rollouts.

    A directive primitive declares "element X may now contain child
    element Y" (or accept ref Y). Adoption surface is a `<Y>` opening
    tag in a view, not an attribute. Pre-fix, the shared element-scoped
    pattern compiled to `<X\\b[^<]*?\\bY\\s*=` - looking for `Y=` as an
    attribute on `<X>`, which is the wrong shape and fired ~never.

    Now we look for the child opening tag directly. The language gate
    already restricts scanning to `.xml` / `.rng` so the FP surface is
    narrow; if a directive's tag name collides with an unrelated
    element used in other XML contexts (e.g. reports), the future
    `required_ancestor` annotation can tighten this further.
    """
    n = re.escape(name)
    return re.compile(rf"<{n}\b")


@lru_cache(maxsize=512)
def _js_import_pattern(name: str) -> re.Pattern[str]:
    """Import-anchored pattern for JS export adoption (DESIGN-js.md).

    Exactly two recognized positions, both statement-anchored:

      - named import: `import { ..., NAME, ... } from "..."` (covers
        aliased `NAME as x` and fully-added multi-line import lists -
        `[^{}]*` spans newlines but can't cross into a second import
        statement)
      - default import: `import NAME from "..."` / `import NAME, {...}`

    The from-string is captured (group 1 or 2, one per alternative) so
    `_js_from_plausible` can reject cross-module name collisions.

    Known miss, accepted: a single name appended to an *existing*
    multi-line import list shows up in the diff as a bare `NAME,` line
    with no `import` keyword in the added blob. Matching that shape
    would mean matching every object-literal / destructuring line, the
    exact FP class import-anchoring exists to avoid.
    """
    n = re.escape(name)
    return re.compile(
        rf"(?:^\s*import\s*\{{[^{{}}]*\b{n}\b[^{{}}]*\}}\s*from\s*['\"]([^'\"]+)['\"])"
        rf"|(?:^\s*import\s+{n}\s*(?:,[^;\n]*?)?\s*from\s*['\"]([^'\"]+)['\"])",
        re.MULTILINE,
    )


def _js_from_plausible(from_str: str, module: str) -> bool:
    """Could an import from `from_str` resolve to a symbol defined in
    `module` (an asset alias like `@web/core/l10n/utils/collation`)?

    Accepted sources, all proven on the 2026-06-04 bench corpus:

      - the defining module itself;
      - any ancestor barrel on its path: every real `localeCompare`
        adopter (42/42) imports `@web/core/l10n/utils`, not the
        defining `.../utils/collation` - Odoo barrels re-export from
        an index file at a parent path;
      - a relative path (`./badge_tag`): same-addon imports can't be
        resolved cheaply here, and a cross-addon collision can't be
        relative, so accept.

    Everything else is a cross-module name collision - the bench
    counter-case is `formatDuration`, exported independently by both
    `@web/views/fields/formatters` (watchlisted) and the pre-floor
    `@web/core/l10n/dates`; name-only anchoring misattributed every
    import of the latter to the former.
    """
    if from_str.startswith("."):
        return True
    return module == from_str or module.startswith(from_str + "/")


@lru_cache(maxsize=512)
def _context_key_pattern(name: str) -> re.Pattern[str]:
    """Tighter pattern for NEW_CONTEXT_KEY adoption.

    Most context keys share names with Odoo model fields (`employee_id`,
    `partner_id`, `company`, ...) and the broad py-scope contextual
    pattern's `\\.NAME\\b` alternative was matching every `obj.company`,
    `record.employee_id`, `self.env.company` attribute access -
    unrelated model fields, not context-key adoptions. Restrict to
    the canonical context-key shapes:

      - quoted string `'NAME'` / `"NAME"` (covers `env.context['NAME']`,
        `env.context.get('NAME')`, `_depends_context = ('NAME',)`,
        `@api.depends_context('NAME')` re-declarations)
      - kwarg-style `NAME=` (covers `with_context(NAME=value)` plus
        local-var assignments which the .py qualifier filters out)

    Used only in the PY scope - context keys are PY-only by
    `_KIND_LANGUAGES` (XML namespace collision with model field names is
    too high), so we never compile a VIEW pattern for them. The .py
    qualifier still runs on top to reject bare-attribute and identifier
    matches structurally.
    """
    n = re.escape(name)
    return re.compile(
        rf"(?:'{n}')|(?:\"{n}\")|(?:\b{n}\s*=(?!=))",
        re.MULTILINE,
    )


@lru_cache(maxsize=1024)
def _contextual_pattern(
    name: str,
    module_path: str | None,
    element: str | None = None,
    file_scope: str = "py",
) -> re.Pattern[str]:
    """Build a regex matching `name` only in meaningful contexts.

    `file_scope` selects the alternatives we care about:
      - "py"  (default): 11 Python forms (attribute access, call, kwarg,
        import, class, def, decorator, annotation, quoted string) -
        handles most adoption shapes inside .py source.
      - "xml": 3 forms (quoted strings + `name="value"` attribute form).
        Skips the Python-specific alternatives entirely, cutting regex
        cost ~6x per call on XML blobs in benchmarks.
      - "py_other": import-only gate. Used internally as a fallback
        for generic-named entries on .py files where the structural
        qualifier can't disambiguate (NEW_DECORATOR_OR_HELPER `join`
        looks identical to `",".join(items)`); no file path resolves
        to this scope directly anymore.

    Generic names (`_GENERIC_SHORT_NAMES`) used to be restricted to
    *import* statements only - the regex on its own can't tell `kind=lazy`
    (a real kwarg adoption) from `kind = self._compute_kind()` (random
    local var). With `file_scope == "py"`, the structural ast-grep
    qualifier in `_ast_qualifies` handles that disambiguation, so the
    regex runs the full pattern and the qualifier filters.

    If `element` is given (RNG-derived view-attribute entry), restrict
    matches to XML attributes on that specific parent element. Without
    this, short names like `invisible` match every `<field invisible=..>`
    in the tree - inflating `widget.invisible` rollouts ~50x.
    """
    escaped = re.escape(name)
    if element is not None:
        el_escaped = re.escape(element)
        # <element ... attribute=...>. `[^<]*?` bounds the scan to the
        # current opening tag (can't cross into a child element) and
        # naturally covers newlines for multi-line tags.
        return re.compile(rf"<{el_escaped}\b[^<]*?\b{escaped}\s*=")
    if file_scope == "py_other" and name in _GENERIC_SHORT_NAMES:
        # No structural qualifier on non-.py files; preserve the
        # import-only restriction the regex era relied on.
        if module_path:
            mod_escaped = re.escape(module_path)
            return re.compile(
                rf"(?:^\s*from\s+{mod_escaped}\s+import\s+[^#\n]*?\b{escaped}\b)"
                rf"|(?:^\s*import\s+[^#\n]*?\b{escaped}\b)",
                re.MULTILINE,
            )
        return re.compile(
            rf"(?:^\s*from\s+\S+\s+import\s+[^#\n]*?\b{escaped}\b)"
            rf"|(?:^\s*import\s+[^#\n]*?\b{escaped}\b)",
            re.MULTILINE,
        )
    if file_scope == "xml":
        # XML / RNG / view files skip most Python forms but keep the
        # ones that legitimately appear in QWeb: quoted strings (`<field
        # name="foo"/>`), attribute assignment (`invisible="1"`), and
        # attribute access (`t-att-foo="record.bar"` reads `.bar` as a
        # rollout). ~5x cheaper per call than the full 11-alt pattern.
        return re.compile(
            rf"'{escaped}'|\"{escaped}\"|\b{escaped}\s*=(?!=)|\.{escaped}\b"
        )
    # re.MULTILINE + `^\s*` anchors the import alternatives to actual
    # statement lines. Without it, `import` mentioned inside a string
    # or comment can trigger catastrophic backtracking on the `[^#\n]*`
    # filler (a live reindex wasted 51s of 90s on a single call). Also
    # use non-greedy `*?` so the engine doesn't overshoot then backtrack.
    return re.compile(
        rf"(?:\.{escaped}\b)"
        rf"|(?:\b{escaped}\s*\()"
        rf"|(?:\b{escaped}\s*=(?!=))"
        rf"|(?:^\s*import\s+[^#\n]*?\b{escaped}\b)"
        rf"|(?:^\s*from\s+\S+\s+import\s+[^#\n]*?\b{escaped}\b)"
        rf"|(?:\bclass\s+{escaped}\b)"
        rf"|(?:\bdef\s+{escaped}\b)"
        rf"|(?:@{escaped}\b)"
        # Type annotation: `arg: Type` / `var: Type = ...`. Require the
        # `:` to be preceded by a word character or `)` / `]` so we
        # don't match `# foo: Type` or `"key: Type"`.
        rf"|(?:[\w)\]]\s*:\s*{escaped}\b)"
        # Exact-content quoted string: dict keys, kwarg string values,
        # XML attribute values (`<field name="foo"/>`), `env.context.get('foo')`,
        # `@api.depends_context('foo')`. Two alternatives beat one back-reference
        # - Python's re engine falls off its optimized path on \1 patterns.
        rf"|(?:'{escaped}')"
        rf"|(?:\"{escaped}\")",
        re.MULTILINE,
    )


def _strip_comments(source: str) -> str:
    """Drop anything after `#` on each line (naive - doesn't understand
    `#` inside string literals, but good enough to kill comment noise).
    """
    out: list[str] = []
    for line in source.splitlines():
        hash_pos = line.find("#")
        if hash_pos >= 0:
            out.append(line[:hash_pos])
        else:
            out.append(line)
    return "\n".join(out)


@dataclass
class _Hunk:
    file: str
    header: str            # the @@ line
    before: list[str]      # lines starting with " " or "-"
    after: list[str]       # lines starting with " " or "+"
    raw_added: list[str]   # lines starting with "+"
    raw_removed: list[str] # lines starting with "-"
    line_in_child: int     # starting line number in the new file


def _parse_patch(patch: str) -> list[_Hunk]:
    """Parse a `git diff-tree -p` patch into one _Hunk per @@ block."""
    out: list[_Hunk] = []
    current_file: str | None = None
    hunk: _Hunk | None = None

    for raw_line in patch.splitlines():
        if raw_line.startswith("+++ "):
            m = _FILE_HEADER.match(raw_line)
            if m:
                current_file = m.group(1)
            continue
        if raw_line.startswith("--- ") or raw_line.startswith("diff "):
            continue
        if raw_line.startswith("@@"):
            if hunk:
                out.append(hunk)
            line_in_child = 0
            m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", raw_line)
            if m:
                line_in_child = int(m.group(1))
            hunk = _Hunk(
                file=current_file or "",
                header=raw_line,
                before=[],
                after=[],
                raw_added=[],
                raw_removed=[],
                line_in_child=line_in_child,
            )
            continue
        if hunk is None:
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            hunk.after.append(raw_line[1:])
            hunk.raw_added.append(raw_line[1:])
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            hunk.before.append(raw_line[1:])
            hunk.raw_removed.append(raw_line[1:])
        else:
            # context line
            body = raw_line[1:] if raw_line.startswith(" ") else raw_line
            hunk.before.append(body)
            hunk.after.append(body)

    if hunk:
        out.append(hunk)
    return out


def find_model_name(child_source: str | None) -> str | None:
    """Extract `_name` or `_inherit` from an Odoo model source file."""
    if not child_source:
        return None
    m = _MODEL_ATTR.search(child_source)
    if m:
        return m.group(1)
    m = _INHERIT_ATTR.search(child_source)
    if m:
        return m.group(1)
    return None


@lru_cache(maxsize=512)
def _module_path_of(symbol: str) -> str | None:
    """From `odoo.orm.query.TableSQL`, return `odoo.orm.query` - the
    importable module path. For dotted names without enough segments,
    return None (falls back to generic import matcher).
    """
    parts = symbol.rsplit(".", 2)
    if len(parts) < 2:
        return None
    # Strip trailing class+name (or method+arg) to get the module.
    segments = symbol.split(".")
    if len(segments) < 3:
        return None
    return ".".join(segments[:-1]) if segments[-1][:1].isupper() else ".".join(segments[:-2])


@lru_cache(maxsize=2048)
def _specific_rule(name: str) -> dict:
    """Confirm `name` appears as an actual identifier or quoted-string
    token in the parsed tree.

    For specific (non-generic) names this is sufficient: tree-sitter's
    `comment` nodes don't tokenize their contents into identifiers, so
    a comment like `# Query is deprecated` contributes no `identifier:
    Query` node and won't match. Quoted-string adoptions (context keys,
    magic strings - common for `manual` watchlist entries) show up as
    `string_content` and are accepted here.
    """
    name_re = f"^{re.escape(name)}$"
    return {"rule": {"any": [
        {"kind": "identifier", "regex": name_re},
        {"kind": "string_content", "regex": name_re},
    ]}}


@lru_cache(maxsize=2048)
def _kwarg_in_method_rule(kwarg: str, method: str | None) -> dict:
    """ast-grep rule: a `keyword_argument` named `kwarg` whose enclosing
    call's function is exactly `method` (bare `method(...)` or
    `obj.method(...)`), or the multi-line call fragment shape.

    Two jobs:
      - disambiguate NEW_KWARG entries that share a short name across
        different methods (`Field.to_sql.table` vs
        `Field.condition_to_sql.table`) so adoptions attribute to the
        method actually called;
      - validate single-entry kwargs against their call site at all.
        The bare identifier/string qualifier let `('model', '=', ...)`
        domain tuples count as `Query.__init__.model` adoptions and
        `'binary'` type strings as `AssetsBundle.__init__.binary`.

    `method=None` means "any constructor-shaped call" (capitalized
    callee): an `__init__` kwarg is adopted via every subclass
    constructor (`fields.Boolean(compute_sql=...)` adopts
    `Field.__init__.compute_sql`), so an exact class-name match would
    miss nearly all of them.
    """
    kwarg_re = f"^{re.escape(kwarg)}$"
    method_re = "^[A-Z]" if method is None else f"^{re.escape(method)}$"
    return {"rule": {"any": [
        {"kind": "keyword_argument",
         "all": [
             {"has": {"field": "name", "regex": kwarg_re, "stopBy": "end"}},
             {"inside": {
                 "kind": "call",
                 "has": {"field": "function",
                         "any": [
                             {"kind": "identifier", "regex": method_re},
                             {"kind": "attribute",
                              "has": {"field": "attribute", "regex": method_re,
                                      "stopBy": "end"}},
                         ],
                         "stopBy": "end"},
                 "stopBy": "end",
             }},
         ]},
        # Multi-line call fragment: a hunk adding only `kwarg=value,`
        # inside an existing call has no call node to validate against;
        # the trailing comma makes it parse as an assignment to an
        # expression_list - a shape random local-var assignments and
        # quoted strings never take (same fingerprint as
        # _strict_generic_rule's).
        {"all": [
            {"kind": "assignment"},
            {"has": {"field": "left", "kind": "identifier",
                     "regex": kwarg_re, "stopBy": "end"}},
            {"has": {"field": "right", "kind": "expression_list",
                     "stopBy": "end"}},
        ]},
    ]}}


def _kwarg_method_targets(
    group: list[WatchlistEntry],
) -> list[str | None] | None:
    """Call-site targets for a group of NEW_KWARG entries sharing a
    short name, or None if the group can't be method-validated.

    A `__init__` kwarg's call-site target is None ("any
    constructor-shaped call"): adopters call subclass constructors
    (`fields.Boolean(compute_sql=...)`), never `__init__` by name.

    Single-entry groups are validated too: without the call-site check,
    a specific-named kwarg only needs the bare identifier/string
    qualifier, which let `('model', '=', ...)` domain tuples count as
    `Query.__init__.model` adoptions (119 bogus rollouts) and `'binary'`
    type strings as `AssetsBundle.__init__.binary` (127).

    Returns None when:
      - any entry isn't a NEW_KWARG (mixed groups fall back to legacy)
      - any symbol doesn't have the `<...>.<class>.<method>.<kwarg>`
        shape (need at least 4 segments to extract a method name)
      - several entries share one target (no discrimination possible;
        legacy first-wins dedupe keeps shared-name counts honest)
    """
    methods: list[str | None] = []
    for e in group:
        if e.kind is not Kind.NEW_KWARG or not e.symbol:
            return None
        parts = e.symbol.split(".")
        if len(parts) < 4:
            return None
        method = parts[-2]
        methods.append(None if method == "__init__" else method)
    if len(group) > 1 and len(set(methods)) < 2:
        return None
    return methods


@lru_cache(maxsize=512)
def _strict_generic_rule(kind: Kind, name: str) -> dict | None:
    """Strict structural rule for generic short names (`kind`, `default`,
    `table`, etc.). The contextual regex restricts these to *imports*
    today, which throws away most real adoptions. The structural rule
    is precise enough to replace that gate without flooding FPs.
    """
    name_re = f"^{re.escape(name)}$"
    if kind in (Kind.NEW_KWARG, Kind.SIGNATURE_CHANGE):
        return {"rule": {"any": [
            # Call-site: foo(name=...)
            {"kind": "keyword_argument",
             "has": {"field": "name", "regex": name_re, "stopBy": "end"}},
            # Multi-line call: a `+    name=value,` line by itself parses
            # as an assignment whose right field is an `expression_list`
            # (the trailing comma turns the RHS into a singleton tuple
            # syntactically). A plain `name = SQL(...)` local-var
            # assignment has `right.kind == call/identifier/...`, never
            # `expression_list` - so this fingerprint distinguishes the
            # kwarg-fragment case from random local-var assignments
            # whose LHS happens to be a generic word.
            {"all": [
                {"kind": "assignment"},
                {"has": {"field": "left", "kind": "identifier",
                         "regex": name_re, "stopBy": "end"}},
                {"has": {"field": "right", "kind": "expression_list",
                         "stopBy": "end"}},
            ]},
            # Def-site parameter, four shapes.
            {"kind": "default_parameter",
             "has": {"field": "name", "regex": name_re, "stopBy": "end"}},
            {"kind": "typed_default_parameter",
             "has": {"field": "name", "regex": name_re, "stopBy": "end"}},
            {"kind": "typed_parameter",
             "has": {"kind": "identifier", "regex": name_re, "stopBy": "end"}},
            {"kind": "identifier", "regex": name_re,
             "inside": {"kind": "parameters", "stopBy": "end"}},
        ]}}
    if kind is Kind.NEW_CLASS_ATTRIBUTE:
        return {"rule": {"any": [
            # Subclass-body assignment (also catches the multi-line-call
            # form, same shape).
            {"kind": "assignment",
             "has": {"field": "left", "kind": "identifier",
                     "regex": name_re, "stopBy": "end"}},
            # Attribute access: obj.kind / cls.kind
            {"kind": "attribute",
             "has": {"field": "attribute", "regex": name_re, "stopBy": "end"}},
        ]}}
    if kind is Kind.NEW_PUBLIC_CLASS:
        return {"rule": {"any": [
            {"kind": "call",
             "has": {"field": "function", "regex": name_re, "stopBy": "end"}},
            {"kind": "attribute",
             "has": {"field": "attribute", "regex": name_re, "stopBy": "end"}},
            {"kind": "identifier", "regex": name_re,
             "inside": {"kind": "argument_list",
                        "inside": {"kind": "class_definition", "stopBy": "end"},
                        "stopBy": "end"}},
            {"kind": "dotted_name", "regex": name_re,
             "inside": {"kind": "import_from_statement", "stopBy": "end"}},
        ]}}
    if kind is Kind.NEW_DECORATOR_OR_HELPER:
        return {"rule": {"any": [
            {"kind": "decorator", "any": [
                {"has": {"kind": "identifier", "regex": name_re, "stopBy": "end"}},
                {"has": {"kind": "call",
                         "has": {"field": "function", "regex": name_re, "stopBy": "end"},
                         "stopBy": "end"}},
            ]},
            {"kind": "call",
             "has": {"field": "function", "regex": name_re, "stopBy": "end"}},
            {"kind": "attribute",
             "has": {"field": "attribute", "regex": name_re, "stopBy": "end"}},
            {"kind": "dotted_name", "regex": name_re,
             "inside": {"kind": "import_from_statement", "stopBy": "end"}},
        ]}}
    return None


def _has_truncated_identifier(root, name: str) -> bool:
    """When tree-sitter wraps a partial construct in ERROR, find_all skips
    the children. If the broken span contains an identifier matching `name`,
    we can't structurally qualify - accept conservatively rather than emit
    a false negative the regex era didn't have.
    """
    for child in root.children():
        if child.kind() != "ERROR":
            continue
        for sub in child.children():
            if sub.kind() == "identifier" and sub.text() == name:
                return True
    return False


@lru_cache(maxsize=1024)
def _context_key_rule(name: str) -> dict:
    """Strict structural rule for NEW_CONTEXT_KEY adoption.

    Many context keys collide with model-field names (`employee_id`,
    `partner_id`, `company`, ...). The previous "any kwarg or any
    string_content" rule still misfired on `_create(partner_id=...)`
    method-call kwargs and `{'partner_id': line.x}` dict-literal keys
    that happen to share the name. This rule restricts to five
    canonical adoption shapes that have a structural anchor tying
    the string/kwarg to context machinery:

      1. `obj.with_context(NAME=val)` / `with_company(NAME=val)` -
         kwarg in a `with_*` call (the standard env-mutation idiom).
      2. `env.context['NAME']` - subscript on `.context` attribute.
      3. `*.context.get/pop/setdefault('NAME', ...)` - dict-method
         lookup on `.context`.
      4. `_depends_context = ('NAME', ...)` - the class-attr tuple
         declaring context dependencies.
      5. `@api.depends_context('NAME', ...)` - decorator-call form
         (covers redeclarations of the same key in subclasses).

    Everything else - dict pairs, domain tuples, list elements,
    plain method-call kwargs - is rejected. False negatives are
    bounded: an unusual idiom isn't caught, but a watchlist entry
    that legitimately fires here is also rare.
    """
    name_re = f"^{re.escape(name)}$"
    return {"rule": {"any": [
        # 1. with_*(NAME=...)
        {"kind": "keyword_argument",
         "all": [
             {"has": {"field": "name", "regex": name_re, "stopBy": "end"}},
             {"inside": {
                 "kind": "call",
                 "has": {"field": "function",
                         "any": [
                             {"kind": "identifier", "regex": "^with_"},
                             {"kind": "attribute",
                              "has": {"field": "attribute", "regex": "^with_",
                                      "stopBy": "end"}},
                         ],
                         "stopBy": "end"},
                 "stopBy": "end",
             }},
         ]},
        # 2. env.context['NAME']
        {"kind": "string_content", "regex": name_re,
         "inside": {
             "kind": "subscript",
             "has": {"field": "value",
                     "any": [
                         {"kind": "attribute",
                          "has": {"field": "attribute", "regex": "^context$",
                                  "stopBy": "end"}},
                         {"kind": "identifier", "regex": "^context$"},
                     ],
                     "stopBy": "end"},
             "stopBy": "end",
         }},
        # 3. *.context.get/pop/setdefault('NAME', ...)
        {"kind": "string_content", "regex": name_re,
         "inside": {
             "kind": "call",
             "has": {"field": "function",
                     "kind": "attribute",
                     "all": [
                         {"has": {"field": "attribute",
                                  "regex": "^(get|pop|setdefault)$",
                                  "stopBy": "end"}},
                         {"has": {"field": "object",
                                  "any": [
                                      {"kind": "attribute",
                                       "has": {"field": "attribute",
                                               "regex": "^context$",
                                               "stopBy": "end"}},
                                      {"kind": "identifier",
                                       "regex": "^context$"},
                                  ],
                                  "stopBy": "end"}},
                     ],
                     "stopBy": "end"},
             "stopBy": "end",
         }},
        # 4. _depends_context = (...)
        {"kind": "string_content", "regex": name_re,
         "inside": {
             "kind": "assignment",
             "has": {"field": "left", "kind": "identifier",
                     "regex": "^_(depends_context|context_keys|context_dependent)$",
                     "stopBy": "end"},
             "stopBy": "end",
         }},
        # 5. @*depends_context('NAME', ...)
        {"kind": "string_content", "regex": name_re,
         "inside": {
             "kind": "call",
             "all": [
                 {"has": {"field": "function",
                          "any": [
                              {"kind": "identifier",
                               "regex": "^depends_context$"},
                              {"kind": "attribute",
                               "has": {"field": "attribute",
                                       "regex": "^depends_context$",
                                       "stopBy": "end"}},
                          ],
                          "stopBy": "end"}},
                 {"inside": {"kind": "decorator", "stopBy": "end"}},
             ],
             "stopBy": "end",
         }},
    ]}}


def _ast_qualifies(root, kind: Kind, name: str) -> bool:
    """Stage-2 structural confirmation for a regex-detected rollout.

    NEW_CONTEXT_KEY entries get the strictest rule (kwarg or
    string-content only) regardless of whether the name is generic.
    Specific names in other kinds need only a comment-safe sanity
    check (any identifier or quoted-string match). Generic names
    (`_GENERIC_SHORT_NAMES`) get a kind-specific structural rule.
    """
    if kind is Kind.NEW_CONTEXT_KEY:
        # No truncation fallback: the ERROR-node identifier check
        # accepts any bare-identifier presence, which is exactly the
        # FP class we're guarding against (model-field-name collisions).
        # A truncated hunk that genuinely contains a context-key adoption
        # will reparse cleanly under one of the five accepted shapes once
        # the surrounding code lands; we'd rather miss the partial-hunk
        # case than let `company = self.env['res.company']` through.
        return bool(root.find_all(_context_key_rule(name)))
    if name in _GENERIC_SHORT_NAMES:
        rule = _strict_generic_rule(kind, name)
        if rule is None:
            return True  # no structural shape known: accept regex hit
        if root.find_all(rule):
            return True
        return _has_truncated_identifier(root, name)
    if root.find_all(_specific_rule(name)):
        return True
    return _has_truncated_identifier(root, name)


def _ancestor_qualifies(
    xml_root: etree._Element | None,
    entry: WatchlistEntry,
) -> bool:
    """Confirm at least one matching XML element in the file has an
    ancestor whose tag is in `entry.required_ancestor`.

    Caller passes the parsed child-source root (or None on parse
    failure). On None we reject conservatively - the user opted into
    structural matching, so "can't verify" should mean "don't emit"
    rather than letting through a possible false positive.

    The element to look for depends on kind:
      - NEW_VIEW_DIRECTIVE: `entry.short_name` is the child tag itself
        (e.g. `column`); we look for any `<column>` element.
      - NEW_VIEW_ATTRIBUTE: `entry.element` is the host tag (e.g.
        `widget`) and `entry.short_name` is the attribute (e.g.
        `invisible`); we look for `<widget>` elements that carry the
        `invisible` attribute.
    Other kinds aren't VIEW-scoped so they shouldn't reach this path,
    but we conservatively reject if invoked.
    """
    if not entry.required_ancestor:
        return True
    if xml_root is None:
        return False
    if entry.kind is Kind.NEW_VIEW_DIRECTIVE:
        target_tag = entry.short_name
        require_attr: str | None = None
    elif entry.kind is Kind.NEW_VIEW_ATTRIBUTE:
        target_tag = entry.element or entry.short_name
        require_attr = entry.short_name
    else:
        return False
    allowed = {a for a in entry.required_ancestor}
    for el in xml_root.iter():
        if not isinstance(el.tag, str):
            continue
        if etree.QName(el).localname != target_tag:
            continue
        if require_attr is not None and require_attr not in el.attrib:
            continue
        for ancestor in el.iterancestors():
            if not isinstance(ancestor.tag, str):
                continue
            if etree.QName(ancestor).localname in allowed:
                return True
    return False


@dataclass(frozen=True)
class _Matcher:
    """Pre-built rollout matcher for a given watchlist snapshot.

    Building this costs O(N) regex compiles and was previously done per
    commit, which showed up as ~4% of reindex wall time. Cache keyed by
    a frozenset of (symbol, element) so it invalidates when the watchlist
    grows mid-run.

    `compiled_by_scope` holds per-(symbol, file_scope) patterns so an
    XML rollout pays a ~6x cheaper regex than the full Python-shaped
    pattern would charge. Scope keys mirror `Language` values.

    `automaton` is an Aho-Corasick automaton over the short names. One
    O(|text|) pass reports which watchlisted short names are present,
    replacing both the file-level `\\b(a|b|...)\\b` prefilter and the
    per-entry `short in added_blob` inner loop. Both used to scale
    linearly with watchlist size; AC's single-pass scan is flat.
    """
    by_short: dict[str, list]
    compiled_by_scope: dict[str, dict[str, re.Pattern[str]]]
    automaton: ahocorasick.Automaton


def _file_language(path: str) -> Language | None:
    """Map a file path to the language we'll interpret it as, or None
    to skip the file entirely.

    Skipping (returning None) is what kills the cross-language FP class:
    we don't run any regexes against `.po` / `.css` files at all, and
    `.js` files only ever see the import-anchored patterns of JS kinds
    (the kind-language gate drops Python/View entries before lookup).
    The contextual regex's `py_other` scope is preserved
    internally for the strict-on-py fallback (generic-named decorator
    helpers like `join` keep the import-only gate even on .py files),
    but no file path resolves to it any more.

    JS test files are skipped outright: hoot (the JS test framework)
    deliberately shadows framework helper names (`waitUntil`,
    `waitFor`, `click`, ...), so import lines in test files
    systematically attribute hoot imports to same-named framework
    exports (bench 2026-06-04: 11 of 13 `macro.waitUntil` hits were
    hoot's). A test-file import isn't an adoption story even when
    genuine, so the whole surface goes - templates under static/tests
    included.

    XML under `static/src/` is QWEB, not VIEW: OWL component templates
    are a different namespace from backend view schemas, and every
    VIEW-kind rollout ever recorded there was a cross-namespace FP
    (194 on the 2026-06-04 corpus - `record.x.raw_value` expressions
    matching `Manifest.raw_value`, t-attrs matching helper names).
    QWEB's adoption surface is component tags (`<BadgeTag`) for JS
    exports plus manual attr needles (`data-available-offline`).
    """
    if "/static/tests/" in path or path.endswith(".test.js"):
        return None
    if path.endswith(".xml") and "/static/src/" in path:
        return Language.QWEB
    for exts, lang in _FILE_LANGUAGES:
        if path.endswith(exts):
            return lang
    return None


def _build_matcher(watchlist: Watchlist) -> _Matcher:
    by_short: dict[str, list] = {}
    for entry in sorted(watchlist.entries.values(), key=lambda e: e.symbol):
        by_short.setdefault(entry.short_name, []).append(entry)
    # Non-source files are dropped before we look up a pattern (see
    # `_file_language`).
    compiled_by_scope: dict[str, dict[str, re.Pattern[str]]] = {
        Language.PY: {}, Language.VIEW: {}, Language.JS: {},
        Language.QWEB: {},
    }
    for entry in watchlist.entries.values():
        kind_langs = _KIND_LANGUAGES.get(entry.kind, frozenset())
        # JS kinds adopt via import lines in JS scope and (for
        # components) via `<Name` tags in OWL templates; they never
        # need a PY/VIEW pattern, and no Python/View kind ever gets a
        # JS one - the language gate in detect_rollouts drops them
        # before pattern lookup.
        if Language.JS in kind_langs:
            compiled_by_scope[Language.JS][entry.symbol] = _js_import_pattern(
                entry.short_name,
            )
            if Language.QWEB in kind_langs:
                compiled_by_scope[Language.QWEB][entry.symbol] = (
                    _directive_pattern(entry.short_name)
                )
            continue
        # Context keys get a dedicated tight pattern (rejects the bare
        # `.attribute` form on shared-name model fields). They're PY-only
        # by `_KIND_LANGUAGES`, so we don't compile a VIEW pattern.
        if entry.kind is Kind.NEW_CONTEXT_KEY:
            compiled_by_scope[Language.PY][entry.symbol] = _context_key_pattern(
                entry.short_name,
            )
            continue
        # Directive entries declare a new child element under a parent.
        # Adoption is a `<child>` opening tag, not an attribute on the
        # parent - so they get the directive pattern instead of the
        # element-scoped attribute one. VIEW-only by _KIND_LANGUAGES.
        if entry.kind is Kind.NEW_VIEW_DIRECTIVE:
            compiled_by_scope[Language.VIEW][entry.symbol] = _directive_pattern(
                entry.short_name,
            )
            continue
        module = _module_path_of(entry.symbol)
        # Generic-named entries whose kind has no discriminating qualifier
        # rule (e.g. NEW_DECORATOR_OR_HELPER `join`) keep the import-only
        # gate even on .py files - the qualifier would let every
        # `",".join(items)` through. The "py_other" scope of the
        # contextual pattern carries that strict shape.
        keep_strict_on_py = (
            entry.short_name in _GENERIC_SHORT_NAMES
            and entry.kind not in _RELAX_GENERIC_KINDS
        )
        py_scope = "py_other" if keep_strict_on_py else "py"
        compiled_by_scope[Language.PY][entry.symbol] = _contextual_pattern(
            entry.short_name, module, entry.element, py_scope,
        )
        compiled_by_scope[Language.VIEW][entry.symbol] = _contextual_pattern(
            entry.short_name, module, entry.element, "xml",
        )
        # QWEB-capable non-JS kinds (manual attr-needle pins) reuse the
        # xml-shaped pattern - `data-available-offline=` matches via
        # the attribute alternative, `t-att-` prefixed included.
        if Language.QWEB in kind_langs:
            compiled_by_scope[Language.QWEB][entry.symbol] = (
                compiled_by_scope[Language.VIEW][entry.symbol]
            )
    automaton = ahocorasick.Automaton()
    for short in by_short:
        automaton.add_word(short, short)
    automaton.make_automaton()
    return _Matcher(
        by_short=by_short,
        compiled_by_scope=compiled_by_scope,
        automaton=automaton,
    )


_MATCHER_CACHE: dict[frozenset, _Matcher] = {}


def _cached_matcher(watchlist: Watchlist) -> _Matcher:
    """Reuse the compiled matcher when the watchlist hasn't grown.

    Key includes `element` per entry so the fix for RNG-scoped
    primitives isn't silently invalidated by a cache hit on an older
    signature, and `required_ancestor` so an annotation update isn't
    masked by an old `by_short` pointing at the pre-annotation entry
    object. Cached keys are monotonic in practice (watchlist only
    grows during a run), so the cache doesn't need bounds.
    """
    key = frozenset(
        (
            e.symbol,
            e.element,
            tuple(e.required_ancestor) if e.required_ancestor else None,
        )
        for e in watchlist.entries.values()
    )
    cached = _MATCHER_CACHE.get(key)
    if cached is None:
        cached = _build_matcher(watchlist)
        _MATCHER_CACHE[key] = cached
    return cached


def detect_rollouts(
    patches: dict[str, str],
    watchlist: Watchlist,
    child_sources: dict[str, str | None] | None = None,
    fetch_child: Callable[[str], str | None] | None = None,
) -> list[ChangeRecord]:
    """Scan patches for rollouts of watchlisted short names.

    Args:
      patches: file -> unified diff patch for that file.
      watchlist: current watchlist (short_name -> symbol).
      child_sources: optional map file -> full child source, used to pull
        _name / _inherit for rollouts on Odoo model files. Also reused
        for the structural ancestor check on annotated VIEW entries.
      fetch_child: optional callback to lazily load a file's child source
        when an annotated VIEW entry needs it for the ancestor check
        and the file isn't already in `child_sources`. Cheaper than
        pre-fetching every hit candidate; the historical perf wisdom
        was that pre-fetching cost ~85% of runtime.
    """
    records: list[ChangeRecord] = []
    if not watchlist.entries:
        return records
    # Per-call cache of parsed XML roots, keyed by file path. Only built
    # for files where an entry actually requires the structural check.
    parsed_xml: dict[str, etree._Element | None] = {}

    def _xml_root_for(file: str) -> etree._Element | None:
        if file in parsed_xml:
            return parsed_xml[file]
        src = (child_sources or {}).get(file)
        if src is None and fetch_child is not None:
            src = fetch_child(file)
            if child_sources is not None and src is not None:
                child_sources[file] = src
        if not src:
            parsed_xml[file] = None
            return None
        try:
            parsed_xml[file] = etree.fromstring(src.encode("utf-8"))
        except etree.XMLSyntaxError:
            parsed_xml[file] = None
        return parsed_xml[file]

    # Shared-name primitives (e.g. a new kwarg `compute_sql` added to
    # 10 Field subclasses) dedupe to one rollout per hunk, attributed
    # to the first entry - same as the pre-refactor behavior.
    # Element-scoped entries (RNG-derived) are matched per-entry so
    # widget.invisible and field.invisible would stay distinct if both
    # existed. Matcher is cached across commits; rebuilt when the
    # watchlist grows.
    matcher = _cached_matcher(watchlist)
    by_short = matcher.by_short
    automaton = matcher.automaton

    def _make_record(file: str, hunk: _Hunk, entry) -> ChangeRecord:
        return ChangeRecord(
            kind=Kind.ROLLOUT,
            file=file,
            line=hunk.line_in_child,
            symbol=entry.symbol,
            model=find_model_name((child_sources or {}).get(file)),
            before_snippet=_truncate("\n".join(hunk.raw_removed)),
            after_snippet=_truncate("\n".join(hunk.raw_added)),
            hunk_header=hunk.header,
        )

    for file, patch in patches.items():
        # File-level language gate. Files outside `_FILE_LANGUAGES`
        # (.js, .po, .csv, .html, .scss, ...) are skipped wholesale: we
        # don't extract primitives from them, so any match would be a
        # cross-language false positive (the OWL `setup()` lifecycle
        # method is not an adoption of `PropertiesDefinition.setup`).
        file_lang = _file_language(file)
        if file_lang is None:
            continue
        # File-level early exit: short-circuit AC iter on the first hit.
        # Replaces a `\b(a|b|...)\b` regex whose cost grew with watchlist
        # size; the iter stops at the first match, so the no-match case
        # pays one full O(|patch|) scan either way but the alternation
        # cost is gone.
        if next(automaton.iter(patch), None) is None:
            continue
        compiled = matcher.compiled_by_scope[file_lang]
        is_py = file_lang is Language.PY
        is_view = file_lang is Language.VIEW
        is_js = file_lang is Language.JS
        is_qweb = file_lang is Language.QWEB
        for hunk in _parse_patch(patch):
            added_blob = _strip_comments("\n".join(hunk.raw_added))
            if not added_blob.strip():
                continue
            # Huge hunks (mass refactors, generated files, data dumps)
            # can hit catastrophic backtracking on the 11-alternative
            # contextual pattern - a profile captured a single commit
            # spending 33 seconds in one regex call. Any rollout hiding
            # inside a 10k-line hunk isn't meaningful slide material.
            if len(added_blob) > _MAX_HUNK_CHARS:
                continue
            # Single AC pass replaces the per-entry `short in added_blob`
            # loop: one O(|added_blob|) scan reports every watchlisted
            # short name present. Before: N substring searches per hunk
            # (linear in watchlist size, ~46us per extra entry measured
            # on a full reindex). After: one scan per hunk regardless
            # of N, then contextual regex runs only on the (usually
            # small) set of shorts actually present.
            present_shorts = {value for _, value in automaton.iter(added_blob)}
            if not present_shorts:
                continue
            # Parse the added blob once per hunk so the .py qualifier can
            # share a single tree-sitter pass across every candidate entry
            # below. Lazy: only built when a qualifier actually needs it.
            ast_root = None
            for short, group in by_short.items():
                if short not in present_shorts:
                    continue
                # Drop entries whose source language doesn't include
                # this file's language (the bulk of the cross-language
                # FP class). Then, in VIEW scope, additionally drop
                # generic-named entries from the cross-language kinds
                # (a NEW_DECORATOR_OR_HELPER `name` would match every
                # `<field name=.../>` even though the entry can
                # legitimately appear in XML when the name is specific
                # like `formatted_display_name`). In JS scope, drop
                # OWL-vocabulary names: import matching is from-string
                # agnostic, so a colliding export would match the
                # `import { Component } from "@odoo/owl"` line in
                # every component file.
                group = [
                    e for e in group
                    if file_lang in _KIND_LANGUAGES[e.kind]
                    and not (
                        is_view
                        and e.kind in _GENERIC_BLOCKED_IN_VIEW
                        and e.short_name in _GENERIC_SHORT_NAMES
                    )
                    and not (
                        (is_js or is_qweb)
                        and e.short_name in _JS_GENERIC_SHORT_NAMES
                    )
                ]
                if not group:
                    continue
                if any(e.element is not None for e in group):
                    # Per-entry matching: each entry's pattern is
                    # context-specific (parent element differs), so a
                    # match on one entry doesn't imply a match on the
                    # others. Emit per matching entry.
                    for entry in group:
                        if not compiled[entry.symbol].search(added_blob):
                            continue
                        if is_py:
                            if ast_root is None:
                                ast_root = SgRoot(added_blob, "python").root()
                            if not _ast_qualifies(ast_root, entry.kind, entry.short_name):
                                continue
                        if entry.required_ancestor:
                            if not _ancestor_qualifies(_xml_root_for(file), entry):
                                continue
                        records.append(_make_record(file, hunk, entry))
                elif is_js:
                    # Per-entry matching: two JS exports can share a
                    # short name with different defining modules, and
                    # the from-string plausibility check is what tells
                    # them apart (formatDuration: formatters vs dates).
                    for entry in group:
                        module = entry.symbol.rsplit(".", 1)[0]
                        if any(
                            _js_from_plausible(m.group(1) or m.group(2), module)
                            for m in compiled[entry.symbol].finditer(added_blob)
                        ):
                            records.append(_make_record(file, hunk, entry))
                elif is_py and (methods := _kwarg_method_targets(group)) is not None:
                    # NEW_KWARG entries share the same short name across
                    # different methods (e.g. `Field.to_sql.table` vs
                    # `Field.condition_to_sql.table`). The contextual
                    # regex can't tell them apart, so we discriminate
                    # at the AST level: only attribute the rollout to
                    # the entry whose method actually appears at the
                    # call site of the kwarg. Multiple entries can fire
                    # on the same hunk if both methods are called there.
                    if not compiled[group[0].symbol].search(added_blob):
                        continue
                    if ast_root is None:
                        ast_root = SgRoot(added_blob, "python").root()
                    for entry, method in zip(group, methods, strict=True):
                        rule = _kwarg_in_method_rule(entry.short_name, method)
                        if not ast_root.find_all(rule):
                            continue
                        records.append(_make_record(file, hunk, entry))
                else:
                    # Shared short name, no element context -> all
                    # entries use the identical pattern. Legacy dedup:
                    # one rollout per hunk, attributed to the first.
                    first = group[0]
                    if not compiled[first.symbol].search(added_blob):
                        continue
                    if is_py:
                        if ast_root is None:
                            ast_root = SgRoot(added_blob, "python").root()
                        if not _ast_qualifies(ast_root, first.kind, first.short_name):
                            continue
                    if first.required_ancestor:
                        if not _ancestor_qualifies(_xml_root_for(file), first):
                            continue
                    records.append(_make_record(file, hunk, first))
    return records


def _truncate(text: str, max_lines: int = 30) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    half = max_lines // 2
    elided = len(lines) - max_lines
    return "\n".join(lines[:half] + [f"# ... <{elided} lines elided> ..."] + lines[-half:])
