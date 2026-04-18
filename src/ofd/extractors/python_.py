"""Python AST-diff extractor.

Given parent and child source for the same file, emit change records for
any semantically meaningful additions, removals, or signature changes at
the module and class level.

Detection rules:
- top-level public class / function / module-level assignment is compared
  by name.
- class methods are compared as {Class.method} tuples.
- signature changes compare the arg layout, not the body.
- DeprecationWarning emission is a text-level grep over added lines.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from ofd.events.record import ChangeRecord, Kind


@dataclass(frozen=True)
class _Symbol:
    name: str                      # fully qualified local name (e.g. "Foo.bar")
    kind: str                      # "class" | "func" | "attr"
    signature: str | None          # one-line human signature
    args_hash: str | None          # for signature-change detection
    line: int
    source: str                    # the definition's source text
    decorators: tuple[str, ...] = ()
    arg_names: frozenset[str] = frozenset()  # public arg names, excluding self/cls


def _snippet(lines: list[str], start: int, end: int, max_lines: int = 30) -> str:
    """Extract lines [start, end] (1-indexed, inclusive), capped."""
    start = max(start, 1)
    end = min(end, len(lines))
    chunk = lines[start - 1:end]
    if len(chunk) > max_lines:
        half = max_lines // 2
        elided = len(chunk) - max_lines
        chunk = (
            chunk[:half]
            + [f"# ... <{elided} lines elided> ..."]
            + chunk[-half:]
        )
    return "\n".join(chunk)


def _arg_names(args: ast.arguments) -> frozenset[str]:
    """Collect public arg names from an AST arguments node.

    Excludes the implicit `self`/`cls` of methods and any `_`-prefixed
    private args. Vararg (`*args`) and kwarg (`**kwargs`) names are
    included - a newly-added `*args` is itself a notable API change.
    """
    names: set[str] = set()
    posonly = getattr(args, "posonlyargs", [])
    for a in list(posonly) + list(args.args) + list(args.kwonlyargs):
        if a.arg in ("self", "cls"):
            continue
        if a.arg.startswith("_"):
            continue
        names.add(a.arg)
    if args.vararg and not args.vararg.arg.startswith("_"):
        names.add(args.vararg.arg)
    if args.kwarg and not args.kwarg.arg.startswith("_"):
        names.add(args.kwarg.arg)
    return frozenset(names)


def _render_args(args: ast.arguments) -> str:
    parts = []
    posonly = getattr(args, "posonlyargs", [])
    regular = args.args
    defaults = args.defaults
    n_defaults = len(defaults)
    all_positional = list(posonly) + list(regular)
    n_pos = len(all_positional)

    for i, a in enumerate(all_positional):
        default_idx = i - (n_pos - n_defaults)
        if default_idx >= 0:
            parts.append(f"{a.arg}={ast.unparse(defaults[default_idx])}")
        else:
            parts.append(a.arg)
        if posonly and i == len(posonly) - 1:
            parts.append("/")

    if args.vararg:
        parts.append(f"*{args.vararg.arg}")
    elif args.kwonlyargs:
        parts.append("*")

    for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=False):
        if d is None:
            parts.append(a.arg)
        else:
            parts.append(f"{a.arg}={ast.unparse(d)}")
    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")
    return ", ".join(parts)


def _decorator_names(decorators: list[ast.expr]) -> tuple[str, ...]:
    return tuple(ast.unparse(d) for d in decorators)


def _is_public(name: str) -> bool:
    return not name.startswith("_") or _is_dunder(name)


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _collect(tree: ast.Module, source: str) -> dict[str, _Symbol]:
    """Build the public-symbol table for a module tree.

    Includes:
    - top-level public classes, functions, async functions
    - public methods within public classes (one level deep), keyed "Class.method"
    - module-level name assignments of a Name target (public only)
    - class-body name assignments (keyed "Class.attr", public only)
    """
    lines = source.splitlines()
    table: dict[str, _Symbol] = {}

    def end_of(node: ast.AST) -> int:
        return getattr(node, "end_lineno", node.lineno)

    for node in tree.body:
        if isinstance(node, ast.ClassDef) and _is_public(node.name):
            src = _snippet(lines, node.lineno, end_of(node))
            table[node.name] = _Symbol(
                name=node.name,
                kind="class",
                signature=f"class {node.name}({', '.join(ast.unparse(b) for b in node.bases)})"
                    if node.bases else f"class {node.name}",
                args_hash=None,
                line=node.lineno,
                source=src,
                decorators=_decorator_names(node.decorator_list),
            )
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef) and _is_public(sub.name):
                    qname = f"{node.name}.{sub.name}"
                    args = _render_args(sub.args)
                    sub_src = _snippet(lines, sub.lineno, end_of(sub))
                    table[qname] = _Symbol(
                        name=qname,
                        kind="func",
                        signature=f"def {sub.name}({args})",
                        args_hash=args,
                        line=sub.lineno,
                        source=sub_src,
                        decorators=_decorator_names(sub.decorator_list),
                        arg_names=_arg_names(sub.args),
                    )
                elif isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        if isinstance(target, ast.Name) and _is_public(target.id):
                            qname = f"{node.name}.{target.id}"
                            sub_src = _snippet(lines, sub.lineno, end_of(sub))
                            table[qname] = _Symbol(
                                name=qname,
                                kind="attr",
                                signature=f"{target.id} = {ast.unparse(sub.value)}",
                                args_hash=None,
                                line=sub.lineno,
                                source=sub_src,
                            )
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _is_public(node.name):
            args = _render_args(node.args)
            src = _snippet(lines, node.lineno, end_of(node))
            table[node.name] = _Symbol(
                name=node.name,
                kind="func",
                signature=f"def {node.name}({args})",
                args_hash=args,
                line=node.lineno,
                source=src,
                decorators=_decorator_names(node.decorator_list),
                arg_names=_arg_names(node.args),
            )
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and _is_public(target.id):
                    src = _snippet(lines, node.lineno, end_of(node))
                    table[target.id] = _Symbol(
                        name=target.id,
                        kind="attr",
                        signature=f"{target.id} = {ast.unparse(node.value)}",
                        args_hash=None,
                        line=node.lineno,
                        source=src,
                    )

    return table


_DEPRECATION_PATTERNS = [
    re.compile(r'warnings\.warn\s*\(\s*([rRuUbB]?["\'])(?P<msg>(?:(?!\1).)*)\1[^)]*DeprecationWarning', re.DOTALL),
    re.compile(r'warnings\.warn\s*\(\s*DeprecationWarning\s*\(\s*([rRuUbB]?["\'])(?P<msg>(?:(?!\1).)*)\1', re.DOTALL),
]

_REMOVAL_VERSION = re.compile(r'(?:removed|removal)\s+in\s+(?P<v>\d+\.\d+)', re.I)


def _extract_deprecations(new_source: str, old_source: str, file: str) -> list[ChangeRecord]:
    """Emit a deprecation_warning_added record for each warn() block that
    appears in new_source but not old_source. Naive line-level containment
    is sufficient - only the added text matters."""
    old = old_source
    out: list[ChangeRecord] = []
    for pattern in _DEPRECATION_PATTERNS:
        for match in pattern.finditer(new_source):
            matched_text = match.group(0)
            if matched_text in old:
                continue
            msg = match.group("msg")
            removal = _REMOVAL_VERSION.search(msg)
            out.append(
                ChangeRecord(
                    kind=Kind.DEPRECATION_WARNING_ADDED,
                    file=file,
                    line=new_source.count("\n", 0, match.start()) + 1,
                    warning_text=msg.strip(),
                    removal_version=removal.group("v") if removal else None,
                )
            )
    return out


def _qualify(module_path: str, local_name: str) -> str:
    """Turn a file path + local class/function name into a dotted FQN.

    `odoo/orm/models_cached.py` + `CachedModel`
    → `odoo.orm.models_cached.CachedModel`
    """
    if module_path.endswith(".py"):
        module_path = module_path[:-3]
    parts = module_path.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts + [local_name]) if parts else local_name


def extract(
    parent_source: str | None,
    child_source: str | None,
    file: str,
) -> list[ChangeRecord]:
    """Return change records for this file's parent→child transition.

    If parent_source is None, the file was added in this commit.
    If child_source is None, the file was deleted.
    """
    records: list[ChangeRecord] = []

    parent_tree = ast.parse(parent_source) if parent_source else None
    child_tree = ast.parse(child_source) if child_source else None

    parent_syms = _collect(parent_tree, parent_source or "") if parent_tree else {}
    child_syms = _collect(child_tree, child_source or "") if child_tree else {}

    added = child_syms.keys() - parent_syms.keys()
    removed = parent_syms.keys() - child_syms.keys()
    common = parent_syms.keys() & child_syms.keys()

    # Newly-added classes own their entire body - suppress sub-records
    # (methods, attributes) since they're part of the class's snippet.
    newly_added_classes = {
        name for name in added
        if child_syms[name].kind == "class"
    }

    def _is_inside_new_class(name: str) -> bool:
        if "." not in name:
            return False
        return name.split(".", 1)[0] in newly_added_classes

    for name in sorted(added):
        sym = child_syms[name]
        if _is_inside_new_class(name):
            continue
        fqn = _qualify(file, name)
        if sym.kind == "class":
            records.append(ChangeRecord(
                kind=Kind.NEW_PUBLIC_CLASS,
                file=file,
                line=sym.line,
                symbol=fqn,
                signature=sym.signature,
                after_snippet=sym.source,
            ))
        elif sym.kind == "func":
            records.append(ChangeRecord(
                kind=Kind.NEW_DECORATOR_OR_HELPER,
                file=file,
                line=sym.line,
                symbol=fqn,
                signature=sym.signature,
                after_snippet=sym.source,
            ))
        elif sym.kind == "attr":
            if "." in name:
                records.append(ChangeRecord(
                    kind=Kind.NEW_CLASS_ATTRIBUTE,
                    file=file,
                    line=sym.line,
                    symbol=fqn,
                    after_snippet=sym.source,
                ))
            else:
                # Module-level assignment of a public name - typically
                # sentinel values or re-exports. Skip for now; too noisy.
                continue

    removed_classes = {
        name for name in removed
        if parent_syms[name].kind == "class"
    }

    for name in sorted(removed):
        if "." in name and name.split(".", 1)[0] in removed_classes:
            continue  # class's removal already covers its members
        sym = parent_syms[name]
        fqn = _qualify(file, name)
        records.append(ChangeRecord(
            kind=Kind.REMOVED_PUBLIC_SYMBOL,
            file=file,
            line=sym.line,
            symbol=fqn,
            before_snippet=sym.source,
        ))

    for name in sorted(common):
        before = parent_syms[name]
        after = child_syms[name]
        if (
            before.kind == "func"
            and after.kind == "func"
            and before.args_hash != after.args_hash
        ):
            method_fqn = _qualify(file, name)
            records.append(ChangeRecord(
                kind=Kind.SIGNATURE_CHANGE,
                file=file,
                line=after.line,
                symbol=method_fqn,
                before_signature=before.signature,
                after_signature=after.signature,
            ))
            # Emit a NEW_KWARG sub-record for each newly-added arg. This
            # makes kwargs findable as standalone primitives (watchlist
            # entry, ledger entry, rollout matching on `arg=`).
            new_args = after.arg_names - before.arg_names
            for arg_name in sorted(new_args):
                records.append(ChangeRecord(
                    kind=Kind.NEW_KWARG,
                    file=file,
                    line=after.line,
                    symbol=f"{method_fqn}.{arg_name}",
                    signature=after.signature,
                ))

    records.extend(
        _extract_deprecations(
            child_source or "", parent_source or "", file
        )
    )

    return records
