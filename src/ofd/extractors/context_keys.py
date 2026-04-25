"""Context-key extractor.

Finds new arguments to `@api.depends_context(...)` decorators in
parent->child file transitions. Each new argument string IS a context
key worth tracking - the decorator declares "this method's compute
result depends on this context value", which is the framework's own
way of certifying the key as meaningful.

Why this kind of decorator-arg diff is worth its own extractor:

- Context keys (`'formatted_display_name'`, `'lang'`, `'company_id'`,
  ...) are framework primitives that don't surface as classes,
  methods, or kwargs - they're string constants. The Python AST walk
  in `python_.py` can't see them; manual `ofd watchlist add` was the
  only way to track them.

- The `@api.depends_context(...)` call is a structural anchor: any
  string passed in is, by definition, a real context key. We don't
  need heuristics to decide "is this string a key or a coincidence" -
  the decorator does that for us.

- The same shape pattern can later catch other typed-string
  registries (`@http.route('/x', ...)`, `@api.depends('a', 'b')`,
  `@api.constrains('a', 'b')`). For now we ship the most common one
  and let the bench harness gate adding more patterns.

The extractor runs on any `.py` file (not just `framework_paths` -
real `@api.depends_context` calls live in addons) which is why it's
gated by patch-text needle (`"depends_context"`) at the pipeline
level rather than by directory.
"""

from __future__ import annotations

from ast_grep_py import SgRoot

from ofd.events.record import ChangeRecord, Kind

# Find a `decorator > call` whose called function name (or
# attribute-access tail, e.g. `api.depends_context`) is exactly
# `depends_context`. The `inside: decorator` clause keeps us off bare
# `depends_context(...)` calls in regular code (those would be a
# helper invocation, not a decorator declaration).
_DECORATOR_CALL_RULE = {"rule": {
    "kind": "call",
    "all": [
        {"has": {
            "field": "function",
            "any": [
                {"kind": "identifier", "regex": "^depends_context$"},
                {"kind": "attribute",
                 "has": {"field": "attribute", "regex": "^depends_context$",
                         "stopBy": "end"}},
            ],
            "stopBy": "end",
        }},
        {"inside": {"kind": "decorator", "stopBy": "end"}},
    ],
}}


def _depends_context_keys(source: str | None) -> dict[str, int]:
    """Return {key_string: line_number} for every literal-string argument
    passed to `@api.depends_context(...)` in this source.

    Variable-arg forms (`@api.depends_context(*KEYS)`, f-strings,
    concatenations) are skipped silently - they're rare in practice and
    we'd over-emit if we tried to follow them.
    """
    if not source:
        return {}
    try:
        root = SgRoot(source, "python").root()
    except Exception:
        return {}
    out: dict[str, int] = {}
    for call in root.find_all(_DECORATOR_CALL_RULE):
        line_no = call.range().start.line + 1  # 1-indexed for humans
        for child in call.children():
            if child.kind() != "argument_list":
                continue
            for arg in child.children():
                if arg.kind() != "string":
                    continue
                # `string` -> `string_start`, `string_content`,
                # `string_end`. Skip f-strings and concatenations
                # (their content nodes carry interpolation children).
                content_nodes = [c for c in arg.children()
                                 if c.kind() == "string_content"]
                if len(content_nodes) != 1:
                    continue
                key = content_nodes[0].text()
                # Empty-string args are noise; skip.
                if not key:
                    continue
                # First-decorator-wins on duplicates within a file.
                out.setdefault(key, line_no)
    return out


def extract(
    parent_source: str | None,
    child_source: str | None,
    file: str,
) -> list[ChangeRecord]:
    """Emit one record per context key newly introduced in this file."""
    parent_keys = _depends_context_keys(parent_source)
    child_keys = _depends_context_keys(child_source)
    new_keys = sorted(child_keys.keys() - parent_keys.keys())
    return [
        ChangeRecord(
            # NEW_DECORATOR_OR_HELPER mirrors the convention
            # `watchlist.add_manual` uses for context-key pins, so
            # the rollout matcher's specific-name path (identifier OR
            # string_content match) handles the adoption shape
            # (`'formatted_display_name'` literal, kwarg form, etc.)
            # without a new schema kind.
            kind=Kind.NEW_DECORATOR_OR_HELPER,
            file=file,
            line=child_keys[key],
            symbol=f"context_key.{key}",
        )
        for key in new_keys
    ]
