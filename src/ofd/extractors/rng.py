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
- `new_view_directive` - a child element/ref whose occurrence count rose
  inside a define (expanded content model). This fires both when a child
  is brand new AND when an existing child gains a NEW syntactic position -
  e.g. `<field>` becoming allowable inside a `<filter>` group that
  previously held only `<filter>` refs, even though `<field>` already
  appeared in another `<filter>` branch. A flat set-diff misses the
  latter (the tag is already in the ref set); counting occurrences per
  scope is the group-restructure signal name-based diffing can't see.
- `new_view_element` - a brand-new top-level define
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from lxml import etree

from ofd.events.record import ChangeRecord, Kind

_RNG_NS = "http://relaxng.org/ns/structure/1.0"
_NS = {"rng": _RNG_NS}


@dataclass
class _DefineSummary:
    attributes: set[str] = field(default_factory=set)
    # Count of each child element/ref tag reachable directly in this
    # scope (not crossing nested `<rng:element>` boundaries). A COUNT,
    # not a set: a tag gaining a NEW syntactic position - e.g. `<field>`
    # becoming allowable inside one `<filter>` branch where it wasn't
    # before, even though `<field>` already appears in another branch of
    # the same `<filter>` - shows up as an increase. A flat set-diff
    # misses that (the tag is already in the set); the count diff is the
    # group-restructure signal name-based diffing can't see.
    child_tags: Counter[str] = field(default_factory=Counter)
    line: int = 1


def _parse(source: str) -> etree._Element | None:
    try:
        return etree.fromstring(source.encode("utf-8"))
    except etree.XMLSyntaxError:
        return None


def _iter_scope(scope_root: etree._Element):
    """Yield descendants of `scope_root` in document order, but stop
    descending whenever a child is a nested `<rng:element>` (still
    yielding the element itself so its name is counted as a child tag
    in the parent's summary).

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
    """Direct attributes and child tags (refs + inline elements) visible
    at `scope_root`, not crossing nested `<rng:element>` boundaries.

    Child tags are COUNTED, not just collected: a ref/element that gains
    an extra occurrence (a new syntactic position inside a restructured
    content model) is then detectable as a count increase.
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
        elif tag in ("ref", "element"):
            n = desc.get("name")
            if n:
                summary.child_tags[n] += 1
    return summary


def _collect_summaries(
    root: etree._Element,
) -> tuple[dict[str, _DefineSummary], dict[str, _DefineSummary]]:
    """Build two summary maps:

    - `top_level`: one entry per `<rng:define name="X">`, summarizing
      X's own scope (the canonical `<rng:element name="X">` wrapper if
      present, else the define itself). Direct attributes / child tags
      only - anything inside nested `<rng:element>` tags is NOT rolled up.
    - `summaries`: a wider map keyed by every element name that appears
      either as a top-level define OR as a nested `<rng:element name="Y">`
      inside any define. Same per-scope semantics as `top_level`. Used
      for attribute / child-tag diffs so a new `align` attribute on
      `column` shows up as `column.align`, not `list.align` (the
      misattribution that motivated this restructure).

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
            existing.child_tags += summary.child_tags

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

        for attr_name in sorted(after.attributes - before.attributes):
            records.append(ChangeRecord(
                kind=Kind.NEW_VIEW_ATTRIBUTE,
                file=file,
                line=after.line,
                element=name,
                attribute=attr_name,
                rng_file=file,
                symbol=_module_symbol(file, f"{name}.{attr_name}"),
            ))
        for attr_name in sorted(before.attributes - after.attributes):
            records.append(ChangeRecord(
                kind=Kind.REMOVED_VIEW_ATTRIBUTE,
                file=file,
                line=before.line,
                element=name,
                attribute=attr_name,
                rng_file=file,
                symbol=_module_symbol(file, f"{name}.{attr_name}"),
            ))
        # New or repositioned child tags = expanded content model. A tag
        # whose occurrence count rose either appeared for the first time
        # (brand-new ref / inline element) or gained a new syntactic
        # position (e.g. `<field>` newly allowed inside a `<filter>`
        # group that previously held only `<filter>` refs). Both mean
        # "element X may now contain Y here" - emit as `new_view_directive`
        # so ledger routing treats these separately from attribute adds.
        # A pure restructure that only rewraps existing content leaves
        # every count unchanged, so it emits nothing.
        for child_tag in sorted(after.child_tags):
            if after.child_tags[child_tag] > before.child_tags[child_tag]:
                records.append(ChangeRecord(
                    kind=Kind.NEW_VIEW_DIRECTIVE,
                    file=file,
                    line=after.line,
                    element=name,
                    directive=child_tag,
                    rng_file=file,
                    symbol=_module_symbol(file, f"{name}+{child_tag}"),
                ))

    return records
