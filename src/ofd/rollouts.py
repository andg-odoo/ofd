"""Rollout detection.

Given a commit's unified diffs for files outside framework paths, scan
each file's hunks for usage of watchlisted short names. For each hit,
capture the surrounding before/after snippet so stage-3 gets a ready
slide example.

A naive `short_name in line` match produces huge false-positive rates
for generic names like `join`, `default`, `help`. To compensate:

- Each short name is compiled into a *context-aware* regex that only
  matches when the identifier appears in a syntactic position that
  implies it's being *used* (attribute access, call, kwarg, import),
  not embedded in a string literal or comment.

- Names in `_GENERIC_SHORT_NAMES` require an explicit `from ... import`
  of the name; they're too ambiguous otherwise (e.g. a new `.join()`
  method on a relational field would collide with every `",".join(...)`
  in the codebase).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from ofd.events.record import ChangeRecord, Kind
from ofd.watchlist import Watchlist

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@.*$")
_FILE_HEADER = re.compile(r"^\+\+\+ b/(.+)$")
_CLASS_LINE = re.compile(r"^\s*class\s+(\w+)")
_MODEL_ATTR = re.compile(r"""_name\s*=\s*['"]([^'"]+)['"]""")
_INHERIT_ATTR = re.compile(r"""_inherit\s*=\s*['"]([^'"]+)['"]""")

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
def _contextual_pattern(name: str, module_path: str | None) -> re.Pattern[str]:
    """Build a regex matching `name` only in meaningful contexts.

    Contexts accepted:
      - attribute access:        `.name`  (with word boundary after)
      - call:                    `name(`
      - assignment / kwarg:      `name =` or `name=`
      - import:                  `import name` / `from ... import name`
      - class/function def:      `class name` / `def name`
      - type annotation:         `: name` (hand-wavy; helps class names)
      - decorator:               `@name` / `@...name`

    For generic names (`_GENERIC_SHORT_NAMES`), restrict to import only -
    anything else is too noisy.
    """
    escaped = re.escape(name)
    if name in _GENERIC_SHORT_NAMES:
        # Only match if the watchlisted name shows up in an explicit
        # import. If we know the defining module, prefer its own import.
        if module_path:
            mod_escaped = re.escape(module_path)
            return re.compile(
                rf"(?:from\s+{mod_escaped}\s+import\s+[^#\n]*\b{escaped}\b)"
                rf"|(?:^\s*import\s+[^#\n]*\b{escaped}\b)",
                re.MULTILINE,
            )
        return re.compile(
            rf"(?:from\s+\S+\s+import\s+[^#\n]*\b{escaped}\b)"
            rf"|(?:^\s*import\s+[^#\n]*\b{escaped}\b)",
            re.MULTILINE,
        )
    return re.compile(
        rf"(?:\.{escaped}\b)"
        rf"|(?:\b{escaped}\s*\()"
        rf"|(?:\b{escaped}\s*=(?!=))"
        rf"|(?:\bimport\s+[^#\n]*\b{escaped}\b)"
        rf"|(?:\bfrom\s+\S+\s+import\s+[^#\n]*\b{escaped}\b)"
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
        rf"|(?:\"{escaped}\")"
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
    short_names = watchlist.short_names()
    if not short_names:
        return records

    # For each name, pick the most specific defining module we know; if
    # multiple entries share a short name we settle on the first one's
    # module path (rare, and it only tightens the generic-name matcher).
    compiled: dict[str, re.Pattern[str]] = {}
    for short in short_names:
        entry = watchlist.lookup_by_short(short)[0]
        module = _module_path_of(entry.symbol)
        compiled[short] = _contextual_pattern(short, module)

    # Cheap pre-filter: if a patch/hunk contains NONE of the short names
    # as a raw substring, no contextual pattern can match - skip it and
    # save ~50 regex scans. Single combined alternation is O(text) in C.
    combined = re.compile(
        r"\b(?:" + "|".join(re.escape(n) for n in short_names) + r")\b"
    )

    for file, patch in patches.items():
        if not combined.search(patch):
            continue
        for hunk in _parse_patch(patch):
            added_blob = _strip_comments("\n".join(hunk.raw_added))
            if not added_blob.strip():
                continue
            # Scope per-pattern scans to names that actually appear in
            # this hunk's added text. `<substring> in <text>` is C-fast.
            candidates = [n for n in short_names if n in added_blob]
            if not candidates:
                continue
            hit_names: set[str] = set()
            for short in candidates:
                if compiled[short].search(added_blob):
                    hit_names.add(short)
            for short in sorted(hit_names):
                entries = watchlist.lookup_by_short(short)
                if not entries:
                    continue
                entry = entries[0]
                records.append(ChangeRecord(
                    kind=Kind.ROLLOUT,
                    file=file,
                    line=hunk.line_in_child,
                    symbol=entry.symbol,
                    model=find_model_name(
                        (child_sources or {}).get(file)
                    ),
                    before_snippet=_truncate("\n".join(hunk.raw_removed)),
                    after_snippet=_truncate("\n".join(hunk.raw_added)),
                    hunk_header=hunk.header,
                ))
    return records


def _truncate(text: str, max_lines: int = 30) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    half = max_lines // 2
    elided = len(lines) - max_lines
    return "\n".join(lines[:half] + [f"# ... <{elided} lines elided> ..."] + lines[-half:])
