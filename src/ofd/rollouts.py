"""Rollout detection.

Given a commit's unified diffs for files outside framework paths, scan
each file's hunks for usage of watchlisted short names. For each hit,
capture the surrounding before/after snippet so stage-3 gets a ready
slide example.

A naive `short_name in line` match produces huge false-positive rates
for generic names like `join`, `default`, `help`. The pipeline applies
three filtering stages, in order:

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
from dataclasses import dataclass
from functools import lru_cache

import ahocorasick
from ast_grep_py import SgRoot

from ofd.events.record import ChangeRecord, Kind
from ofd.watchlist import Watchlist

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

# Kinds whose adoption shape is Python-only - matching them in XML/RNG
# files is a pure error mode (a NEW_KWARG entry like `Many2one.join.kind`
# is not an XML attribute, even if a `<button kind="primary"/>` happens
# to share the short name).
_PYTHON_ONLY_KINDS = frozenset({
    Kind.NEW_PUBLIC_CLASS,
    Kind.NEW_DECORATOR_OR_HELPER,
    Kind.NEW_CLASS_ATTRIBUTE,
    Kind.NEW_KWARG,
    Kind.SIGNATURE_CHANGE,
})

# Kinds skipped in XML scope for *every* short name, generic or specific.
# Most context keys (`employee_id`, `partner_id`, `company`, `lang`, ...)
# share names with Odoo model fields, and XML views reference fields by
# name (`<field name="employee_id"/>`). The rollout regex's
# quoted-string alternative can't distinguish "context-key reference"
# from "field name", so the only safe XML behavior is to skip entirely.
_XML_BLOCKLIST_KINDS = frozenset({
    Kind.NEW_CONTEXT_KEY,
})

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

    Same pattern across all file scopes - .xml entries are dropped at
    the matcher level by `_XML_BLOCKLIST_KINDS`, .py and .py_other use
    this pattern; .py also runs the ast-grep qualifier which rejects
    bare-attribute and identifier matches structurally.
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
      - "py"  (default): 11 Python/JS-compatible forms (attribute access,
        call, kwarg, import, class, def, decorator, annotation, quoted
        string) - handles most adoption shapes across .py / .js.
      - "xml": 3 forms (quoted strings + `name="value"` attribute form).
        Skips the Python-specific alternatives entirely, cutting regex
        cost ~6x per call on XML blobs in benchmarks.

    Generic names (`_GENERIC_SHORT_NAMES`) used to be restricted to
    *import* statements only - the regex on its own can't tell `kind=lazy`
    (a real kwarg adoption) from `kind = self._compute_kind()` (random
    local var). With `file_scope == "py"`, the structural ast-grep
    qualifier in `_ast_qualifies` handles that disambiguation, so the
    regex runs the full pattern and the qualifier filters.

    For `file_scope == "py_other"` (.js, .po, .csv, .html, ...) the
    qualifier can't help (we only parse Python), so we keep the
    import-only gate. Without it, a .po file containing English text
    like `msgid "kind of weird"` matches every generic name.

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

    Accepts the kwarg form (`with_context(NAME=value)`) and any quoted
    string with the key text (`env.context['NAME']`, `('NAME',)` in
    `_depends_context`, `@api.depends_context('NAME')` redeclarations).
    Rejects attribute access and bare-identifier reads, which are the
    model-field-name and local-var collisions that drove the
    `context_key.employee_id`/`context_key.company` FPs.
    """
    name_re = f"^{re.escape(name)}$"
    return {"rule": {"any": [
        {"kind": "keyword_argument",
         "has": {"field": "name", "regex": name_re, "stopBy": "end"}},
        {"kind": "string_content", "regex": name_re},
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
        if root.find_all(_context_key_rule(name)):
            return True
        return _has_truncated_identifier(root, name)
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


@dataclass(frozen=True)
class _Matcher:
    """Pre-built rollout matcher for a given watchlist snapshot.

    Building this costs O(N) regex compiles and was previously done per
    commit, which showed up as ~4% of reindex wall time. Cache keyed by
    a frozenset of (symbol, element) so it invalidates when the watchlist
    grows mid-run.

    `compiled_by_scope` holds per-(symbol, file_scope) patterns so an
    XML rollout pays a ~6x cheaper regex than the full Python-shaped
    pattern would charge.

    `automaton` is an Aho-Corasick automaton over the short names. One
    O(|text|) pass reports which watchlisted short names are present,
    replacing both the file-level `\\b(a|b|...)\\b` prefilter and the
    per-entry `short in added_blob` inner loop. Both used to scale
    linearly with watchlist size; AC's single-pass scan is flat.
    """
    by_short: dict[str, list]
    compiled_by_scope: dict[str, dict[str, re.Pattern[str]]]
    automaton: ahocorasick.Automaton


def _file_scope(path: str) -> str:
    """Pick the narrowest contextual-pattern scope that still covers
    the adoption shapes we care about in this file type.

    `.py` files get the relaxed pattern (qualifier filters generic-name
    noise downstream). Other py-shaped files (.js, .po, .csv, .html,
    ...) get `py_other` which keeps the import-only generic-name gate -
    the qualifier only parses Python and can't help here.
    """
    if path.endswith((".xml", ".rng")):
        return "xml"
    if path.endswith(".py"):
        return "py"
    return "py_other"


def _build_matcher(watchlist: Watchlist) -> _Matcher:
    by_short: dict[str, list] = {}
    for entry in sorted(watchlist.entries.values(), key=lambda e: e.symbol):
        by_short.setdefault(entry.short_name, []).append(entry)
    compiled_by_scope: dict[str, dict[str, re.Pattern[str]]] = {
        "py": {}, "py_other": {}, "xml": {},
    }
    for entry in watchlist.entries.values():
        # Context keys get a dedicated tight pattern across all scopes
        # to reject the `.attribute` form on shared-name model fields
        # (`obj.employee_id`, `self.env.company`). XML scope entries
        # are also dropped at match time by `_XML_BLOCKLIST_KINDS`.
        if entry.kind is Kind.NEW_CONTEXT_KEY:
            ck_pattern = _context_key_pattern(entry.short_name)
            for scope in ("py", "py_other", "xml"):
                compiled_by_scope[scope][entry.symbol] = ck_pattern
            continue
        module = _module_path_of(entry.symbol)
        # Generic-named entries whose kind has no discriminating qualifier
        # rule (e.g. NEW_DECORATOR_OR_HELPER `join`) keep the import-only
        # gate even on .py files - the qualifier would let every
        # `",".join(items)` through.
        keep_strict_on_py = (
            entry.short_name in _GENERIC_SHORT_NAMES
            and entry.kind not in _RELAX_GENERIC_KINDS
        )
        for scope in ("py", "py_other", "xml"):
            effective = "py_other" if (scope == "py" and keep_strict_on_py) else scope
            compiled_by_scope[scope][entry.symbol] = _contextual_pattern(
                entry.short_name, module, entry.element, effective,
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
    signature. Cached keys are monotonic in practice (watchlist only
    grows during a run), so the cache doesn't need bounds.
    """
    key = frozenset(
        (e.symbol, e.element) for e in watchlist.entries.values()
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
) -> list[ChangeRecord]:
    """Scan patches for rollouts of watchlisted short names.

    Args:
      patches: file -> unified diff patch for that file.
      watchlist: current watchlist (short_name -> symbol).
      child_sources: optional map file -> full child source, used to pull
        _name / _inherit for rollouts on Odoo model files.
    """
    records: list[ChangeRecord] = []
    if not watchlist.entries:
        return records

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
        # File-level early exit: short-circuit AC iter on the first hit.
        # Replaces a `\b(a|b|...)\b` regex whose cost grew with watchlist
        # size; the iter stops at the first match, so the no-match case
        # pays one full O(|patch|) scan either way but the alternation
        # cost is gone.
        if next(automaton.iter(patch), None) is None:
            continue
        scope = _file_scope(file)
        compiled = matcher.compiled_by_scope[scope]
        is_py = file.endswith(".py")
        is_xml = scope == "xml"
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
                # On XML/RNG files, drop:
                #   - Python-only kinds with generic short names
                #     (NEW_KWARG `kind` was firing on
                #     `<button kind="primary"/>`)
                #   - any kind in the XML blocklist (currently
                #     NEW_CONTEXT_KEY: most context keys share names
                #     with model fields, so `<field name="employee_id"/>`
                #     is a model-field reference, not a context-key
                #     adoption - the regex can't tell them apart)
                # Specific names in other Python-only kinds
                # (`formatted_display_name` magic strings,
                # `CachedModel` class refs) legitimately appear as XML
                # attribute values / template references, so they stay.
                if is_xml:
                    group = [
                        e for e in group
                        if e.kind not in _XML_BLOCKLIST_KINDS
                        and not (
                            e.kind in _PYTHON_ONLY_KINDS
                            and e.short_name in _GENERIC_SHORT_NAMES
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
                    records.append(_make_record(file, hunk, first))
    return records


def _truncate(text: str, max_lines: int = 30) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    half = max_lines // 2
    elided = len(lines) - max_lines
    return "\n".join(lines[:half] + [f"# ... <{elided} lines elided> ..."] + lines[-half:])
