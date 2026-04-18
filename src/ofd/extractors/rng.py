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


def _summarize_subtree(node: etree._Element) -> _DefineSummary:
    summary = _DefineSummary(line=node.sourceline or 1)
    for attr in node.iter(f"{{{_RNG_NS}}}attribute"):
        name = attr.get("name")
        if name:
            summary.attributes.add(name)
    for ref in node.iter(f"{{{_RNG_NS}}}ref"):
        name = ref.get("name")
        if name:
            summary.refs.add(name)
    for el in node.iter(f"{{{_RNG_NS}}}element"):
        if el is node:
            continue
        name = el.get("name")
        if name:
            summary.inline_elements.add(name)
    for grouping in ("group", "choice"):
        for g in node.iter(f"{{{_RNG_NS}}}{grouping}"):
            summary.group_shapes.add(_group_fingerprint(g, grouping))
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


def _collect_defines(root: etree._Element) -> dict[str, _DefineSummary]:
    result: dict[str, _DefineSummary] = {}
    for define in root.iter(f"{{{_RNG_NS}}}define"):
        name = define.get("name")
        if not name:
            continue
        result[name] = _summarize_subtree(define)
    # Top-level <start>'s element isn't a define but we don't need it
    # for diffing - views reference defines.
    return result


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

    parent_defines = _collect_defines(parent_root) if parent_root is not None else {}
    child_defines = _collect_defines(child_root) if child_root is not None else {}

    records: list[ChangeRecord] = []

    # New top-level defines (entire new element / rule).
    for name in sorted(child_defines.keys() - parent_defines.keys()):
        summary = child_defines[name]
        records.append(ChangeRecord(
            kind=Kind.NEW_VIEW_ELEMENT,
            file=file,
            line=summary.line,
            element=name,
            rng_file=file,
            symbol=_module_symbol(file, name),
        ))

    # Removed defines are rare and treated as view-directive removal.
    for name in sorted(parent_defines.keys() - child_defines.keys()):
        summary = parent_defines[name]
        records.append(ChangeRecord(
            kind=Kind.REMOVED_VIEW_ATTRIBUTE,
            file=file,
            line=summary.line,
            element=name,
            rng_file=file,
            symbol=_module_symbol(file, name),
        ))

    # Content-model changes for defines that exist on both sides.
    for name in sorted(child_defines.keys() & parent_defines.keys()):
        after = child_defines[name]
        before = parent_defines[name]
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

        # Net-new <rng:group>/<rng:choice> shapes = restructured content
        # model, even if attribute and ref sets didn't change. Emit one
        # directive per net-new shape; the shape itself is the directive
        # value (useful for ledger narration even if the name is opaque).
        if not added["attributes"] and not added["refs"] and not added["inline_elements"]:
            for shape in sorted(added["group_shapes"]):
                records.append(ChangeRecord(
                    kind=Kind.NEW_VIEW_DIRECTIVE,
                    file=file,
                    line=after.line,
                    element=name,
                    directive=shape,
                    rng_file=file,
                    symbol=_module_symbol(file, f"{name}+shape"),
                ))

    return records
