"""RNG schema diff extractor.

Odoo's view schemas live under `odoo/addons/base/rng/*.rng`. Each file
uses a mix of `<rng:define name="X">` blocks (reusable rule groups) and
`<rng:element name="Y">` declarations inside them. Downstream views
reference definitions via `<rng:ref name="X"/>`.

For each `<rng:define>`, we summarize:
- attribute names it accepts (rng:attribute name="..."),
- refs it pulls in (rng:ref name="..."),
- nested element names it introduces (rng:element name="...").

Diffing summaries between two file revisions yields:
- `new_view_attribute` - attribute added to a define
- `removed_view_attribute` - attribute removed from a define
- `new_view_directive` - new `<rng:ref>` or `<rng:element>` inside a define
  (expanded content model - e.g. filter can now contain filter/field)
- `new_view_element` - a brand-new top-level define
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from lxml import etree

from ofd.events.record import ChangeRecord, Kind

_RNG_NS = "http://relaxng.org/ns/structure/1.0"
_NS = {"rng": _RNG_NS}


@dataclass
class _DefineSummary:
    attributes: set[str] = field(default_factory=set)
    refs: set[str] = field(default_factory=set)
    inline_elements: set[str] = field(default_factory=set)
    # Structural fingerprints for rng:group / rng:choice subtrees so we
    # can detect restructuring (new syntactic options) even when the
    # attribute/ref sets don't change.
    group_shapes: set[str] = field(default_factory=set)
    line: int = 1

    def added_vs(self, other: _DefineSummary) -> dict[str, set[str]]:
        return {
            "attributes": self.attributes - other.attributes,
            "refs": self.refs - other.refs,
            "inline_elements": self.inline_elements - other.inline_elements,
            "group_shapes": self.group_shapes - other.group_shapes,
        }

    def removed_vs(self, other: _DefineSummary) -> dict[str, set[str]]:
        return {
            "attributes": other.attributes - self.attributes,
            "refs": other.refs - self.refs,
            "inline_elements": other.inline_elements - self.inline_elements,
            "group_shapes": other.group_shapes - self.group_shapes,
        }


def _parse(source: str) -> etree._Element | None:
    try:
        return etree.fromstring(source.encode("utf-8"))
    except etree.XMLSyntaxError:
        return None


def _iter_scope(scope_root: etree._Element):
    """Yield descendants of `scope_root` in document order, but stop
    descending whenever a child is a nested `<rng:element>` (still
    yielding the element itself so its name is captured as an
    inline_element in the parent's summary).

    This is the boundary that fixes attribute misattribution: an
    `<rng:attribute>` deep inside `<rng:element name="column">` belongs
    to `column`, not to its enclosing `<rng:define name="list">`.
    """
    for child in scope_root:
        if not isinstance(child.tag, str):
            continue
        yield child
        if child.tag == f"{{{_RNG_NS}}}element":
            continue
        yield from _iter_scope(child)


def _summarize_scope(scope_root: etree._Element) -> _DefineSummary:
    """Direct attributes/refs/inline-elements visible at `scope_root`,
    not crossing nested `<rng:element>` boundaries.
    """
    summary = _DefineSummary(line=scope_root.sourceline or 1)
    for desc in _iter_scope(scope_root):
        if not isinstance(desc.tag, str):
            continue
        tag = etree.QName(desc).localname
        if tag == "attribute":
            n = desc.get("name")
            if n:
                summary.attributes.add(n)
        elif tag == "ref":
            n = desc.get("name")
            if n:
                summary.refs.add(n)
        elif tag == "element":
            n = desc.get("name")
            if n:
                summary.inline_elements.add(n)
        elif tag in ("group", "choice"):
            summary.group_shapes.add(_group_fingerprint(desc, tag))
    return summary


def _group_fingerprint(node: etree._Element, kind: str) -> str:
    """Deterministic signature of a <rng:group> or <rng:choice>.

    Represents the node as `kind(child_tag:value, ...)` sorted, so
    permutations don't count as different and context (attribute /
    ref / element names) is preserved.
    """
    parts: list[str] = []
    for child in node:
        # Skip comments / PIs: their .tag is a function, not a string.
        if not isinstance(child.tag, str):
            continue
        tag = etree.QName(child).localname
        if tag == "attribute":
            parts.append(f"attr:{child.get('name') or ''}")
        elif tag == "ref":
            parts.append(f"ref:{child.get('name') or ''}")
        elif tag == "element":
            parts.append(f"el:{child.get('name') or ''}")
        elif tag in {"group", "choice", "oneOrMore", "zeroOrMore", "optional"}:
            nested = ",".join(
                f"{etree.QName(gc).localname}:"
                f"{gc.get('name') or etree.QName(gc).localname}"
                for gc in child
                if isinstance(gc.tag, str)
            )
            parts.append(f"{tag}({nested})")
        else:
            parts.append(tag)
    return f"{kind}(" + ",".join(sorted(parts)) + ")"


def _collect_summaries(
    root: etree._Element,
) -> tuple[dict[str, _DefineSummary], dict[str, _DefineSummary]]:
    """Build two summary maps:

    - `top_level`: one entry per `<rng:define name="X">`, summarizing
      X's own scope (the canonical `<rng:element name="X">` wrapper if
      present, else the define itself). Direct attributes/refs only -
      attributes inside nested `<rng:element>` tags are NOT rolled up.
    - `summaries`: a wider map keyed by every element name that appears
      either as a top-level define OR as a nested `<rng:element name="Y">`
      inside any define. Same per-scope semantics as `top_level`. Used
      for attribute/ref diffs so a new `align` attribute on `column`
      shows up as `column.align`, not `list.align` (the misattribution
      that motivated this restructure).

    Same-name collisions (an inline `<rng:element name="Y">` plus a
    top-level `<rng:define name="Y">`) union into one entry - both
    describe the same element.
    """
    top_level: dict[str, _DefineSummary] = {}
    summaries: dict[str, _DefineSummary] = {}

    def _absorb(name: str, summary: _DefineSummary) -> None:
        existing = summaries.get(name)
        if existing is None:
            summaries[name] = summary
        else:
            existing.attributes |= summary.attributes
            existing.refs |= summary.refs
            existing.inline_elements |= summary.inline_elements
            existing.group_shapes |= summary.group_shapes

    for define in root.iter(f"{{{_RNG_NS}}}define"):
        name = define.get("name")
        if not name:
            continue
        # Locate the canonical self-element wrapper (`<rng:element
        # name="X">` directly inside `<rng:define name="X">`). Most
        # Odoo defines follow this convention; the few that don't get
        # the define itself as the scope root.
        self_el = next(
            (c for c in define
             if isinstance(c.tag, str)
             and c.tag == f"{{{_RNG_NS}}}element"
             and c.get("name") == name),
            None,
        )
        scope = self_el if self_el is not None else define
        top_summary = _summarize_scope(scope)
        top_level[name] = top_summary
        _absorb(name, top_summary)

        # Every nested element (any depth) inside this define gets its
        # own scope summary. `iter()` walks the entire subtree; we
        # filter to elements with names other than scope itself.
        for inner in scope.iter(f"{{{_RNG_NS}}}element"):
            if inner is scope:
                continue
            inner_name = inner.get("name")
            if not inner_name:
                continue
            _absorb(inner_name, _summarize_scope(inner))

    return top_level, summaries


def _module_symbol(file: str, define_name: str) -> str:
    """Build a stable fully-qualified identifier like
    `odoo.addons.base.rng.common.filter`.
    """
    base = file
    if base.endswith(".rng"):
        base = base[:-4]
    parts = base.split("/")
    return ".".join(parts + [define_name])


def extract(
    parent_source: str | None,
    child_source: str | None,
    file: str,
) -> list[ChangeRecord]:
    """Diff two RNG revisions of the same file."""
    if PurePosixPath(file).suffix.lower() != ".rng":
        return []

    parent_root = _parse(parent_source) if parent_source else None
    child_root = _parse(child_source) if child_source else None

    parent_top, parent_summaries = (
        _collect_summaries(parent_root) if parent_root is not None else ({}, {})
    )
    child_top, child_summaries = (
        _collect_summaries(child_root) if child_root is not None else ({}, {})
    )

    records: list[ChangeRecord] = []

    # New top-level defines (entire new element / rule). Brand-new
    # nested elements aren't separately announced here - they show up
    # as a NEW_VIEW_DIRECTIVE on the enclosing define instead, same as
    # the legacy contract.
    for name in sorted(child_top.keys() - parent_top.keys()):
        summary = child_top[name]
        records.append(ChangeRecord(
            kind=Kind.NEW_VIEW_ELEMENT,
            file=file,
            line=summary.line,
            element=name,
            rng_file=file,
            symbol=_module_symbol(file, name),
        ))

    # Removed top-level defines.
    for name in sorted(parent_top.keys() - child_top.keys()):
        summary = parent_top[name]
        records.append(ChangeRecord(
            kind=Kind.REMOVED_VIEW_ATTRIBUTE,
            file=file,
            line=summary.line,
            element=name,
            rng_file=file,
            symbol=_module_symbol(file, name),
        ))

    # Content-model changes for elements present on BOTH sides. The
    # union summaries cover top-level defines AND nested elements with
    # the same fidelity, so an attribute added to a previously-existing
    # nested `<rng:element name="column">` correctly emits as
    # `column.column_invisible` rather than rolling up to `list`.
    for name in sorted(child_summaries.keys() & parent_summaries.keys()):
        after = child_summaries[name]
        before = parent_summaries[name]
        added = after.added_vs(before)
        removed = after.removed_vs(before)

        for attr_name in sorted(added["attributes"]):
            records.append(ChangeRecord(
                kind=Kind.NEW_VIEW_ATTRIBUTE,
                file=file,
                line=after.line,
                element=name,
                attribute=attr_name,
                rng_file=file,
                symbol=_module_symbol(file, f"{name}.{attr_name}"),
            ))
        for attr_name in sorted(removed["attributes"]):
            records.append(ChangeRecord(
                kind=Kind.REMOVED_VIEW_ATTRIBUTE,
                file=file,
                line=before.line,
                element=name,
                attribute=attr_name,
                rng_file=file,
                symbol=_module_symbol(file, f"{name}.{attr_name}"),
            ))
        # New refs or inline elements = expanded content model. Emit as
        # `new_view_directive` so ledger routing treats these separately
        # from attribute additions.
        for ref_name in sorted(added["refs"] | added["inline_elements"]):
            records.append(ChangeRecord(
                kind=Kind.NEW_VIEW_DIRECTIVE,
                file=file,
                line=after.line,
                element=name,
                directive=ref_name,
                rng_file=file,
                symbol=_module_symbol(file, f"{name}+{ref_name}"),
            ))

        # Net-new <rng:group>/<rng:choice> shapes (restructured content
        # model with no new attributes/refs/elements) are intentionally
        # NOT emitted: their directive value is a structural fingerprint
        # (`group(attr:foo,ref:bar)`), not a tag name, so the rollout
        # matcher has no shape to match against and the entry sits at
        # zero rollouts forever. The `group_shapes` summary still
        # exists - it's used to compare content models within elements
        # that DO gain other things - we just don't promote it to its
        # own primitive.

    return records
