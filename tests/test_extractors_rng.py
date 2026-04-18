"""RNG extractor tests.

Fixtures modeled on real PRs from master:
- PR 241459 (commit 23af974eef79) - inner filter support
- PR 257101 (commit 219d4220d3e3) - lazy-loaded values via <field> child
"""

from ofd.events.record import Kind
from ofd.extractors.rng import extract

_RNG_PREFIX = '<rng:grammar xmlns:rng="http://relaxng.org/ns/structure/1.0">'
_RNG_SUFFIX = "</rng:grammar>"


def _wrap(defines: str) -> str:
    return f"{_RNG_PREFIX}\n{defines}\n{_RNG_SUFFIX}"


def test_pr241459_subfilter_adds_content_expansion():
    """Real-shape diff from PR 241459. The subfilter PR restructures
    <filter> so the element can contain nested <filter> refs inside a
    new group. Our extractor sees this as net additions to content model
    because the new `<rng:ref name="filter"/>` appears on both branches
    of the new choice, but the existing branch already had it.
    """
    parent = _wrap("""
    <rng:define name="filter">
        <rng:element name="filter">
            <rng:attribute name="name"/>
            <rng:optional><rng:attribute name="string"/></rng:optional>
            <rng:zeroOrMore>
                <rng:choice>
                    <rng:ref name="field"/>
                    <rng:ref name="button"/>
                </rng:choice>
            </rng:zeroOrMore>
        </rng:element>
    </rng:define>
    """)
    child = _wrap("""
    <rng:define name="filter">
        <rng:element name="filter">
            <rng:choice>
                <rng:group>
                    <rng:attribute name="name"/>
                    <rng:optional><rng:attribute name="string"/></rng:optional>
                    <rng:zeroOrMore>
                        <rng:choice>
                            <rng:ref name="field"/>
                            <rng:ref name="button"/>
                            <rng:ref name="filter"/>
                        </rng:choice>
                    </rng:zeroOrMore>
                </rng:group>
                <rng:group>
                    <rng:attribute name="string"/>
                    <rng:oneOrMore>
                        <rng:ref name="filter"/>
                    </rng:oneOrMore>
                </rng:group>
            </rng:choice>
        </rng:element>
    </rng:define>
    """)
    records = extract(parent, child, "odoo/addons/base/rng/common.rng")
    directives = [r for r in records if r.kind == Kind.NEW_VIEW_DIRECTIVE]
    assert any(
        r.element == "filter" and r.directive == "filter"
        for r in directives
    ), f"expected filter-can-contain-filter directive; got {records}"


def test_pr257101_filter_values_adds_field_ref():
    """PR 257101 extends the inner-filter group to also accept a <field>
    ref - that's the `values` capability. The diff looks like a new
    `<rng:ref name="field"/>` appearing inside filter's content model."""
    parent = _wrap("""
    <rng:define name="filter">
        <rng:element name="filter">
            <rng:attribute name="string"/>
            <rng:oneOrMore>
                <rng:ref name="filter"/>
            </rng:oneOrMore>
        </rng:element>
    </rng:define>
    """)
    child = _wrap("""
    <rng:define name="filter">
        <rng:element name="filter">
            <rng:attribute name="string"/>
            <rng:choice>
                <rng:ref name="field"/>
                <rng:oneOrMore>
                    <rng:ref name="filter"/>
                </rng:oneOrMore>
            </rng:choice>
        </rng:element>
    </rng:define>
    """)
    records = extract(parent, child, "odoo/addons/base/rng/common.rng")
    directives = [r for r in records if r.kind == Kind.NEW_VIEW_DIRECTIVE]
    assert any(
        r.element == "filter" and r.directive == "field"
        for r in directives
    ), f"expected filter-can-contain-field; got {records}"


def test_new_attribute_on_element():
    parent = _wrap("""
    <rng:define name="label">
        <rng:element name="label">
            <rng:optional><rng:attribute name="for"/></rng:optional>
        </rng:element>
    </rng:define>
    """)
    child = _wrap("""
    <rng:define name="label">
        <rng:element name="label">
            <rng:optional><rng:attribute name="for"/></rng:optional>
            <rng:optional><rng:attribute name="field"/></rng:optional>
        </rng:element>
    </rng:define>
    """)
    records = extract(parent, child, "odoo/addons/base/rng/common.rng")
    new_attrs = [r for r in records if r.kind == Kind.NEW_VIEW_ATTRIBUTE]
    assert len(new_attrs) == 1
    assert new_attrs[0].element == "label"
    assert new_attrs[0].attribute == "field"
    assert new_attrs[0].symbol == "odoo.addons.base.rng.common.label.field"


def test_removed_attribute():
    parent = _wrap("""
    <rng:define name="thing">
        <rng:element name="thing">
            <rng:attribute name="legacy"/>
            <rng:attribute name="keep"/>
        </rng:element>
    </rng:define>
    """)
    child = _wrap("""
    <rng:define name="thing">
        <rng:element name="thing">
            <rng:attribute name="keep"/>
        </rng:element>
    </rng:define>
    """)
    records = extract(parent, child, "odoo/addons/base/rng/common.rng")
    removed = [r for r in records if r.kind == Kind.REMOVED_VIEW_ATTRIBUTE]
    assert len(removed) == 1
    assert removed[0].attribute == "legacy"


def test_new_top_level_define():
    parent = _wrap("""
    <rng:define name="existing">
        <rng:element name="existing"><rng:empty/></rng:element>
    </rng:define>
    """)
    child = _wrap("""
    <rng:define name="existing">
        <rng:element name="existing"><rng:empty/></rng:element>
    </rng:define>
    <rng:define name="cohort">
        <rng:element name="cohort">
            <rng:attribute name="date_start"/>
            <rng:attribute name="date_stop"/>
        </rng:element>
    </rng:define>
    """)
    records = extract(parent, child, "odoo/addons/base/rng/common.rng")
    new_els = [r for r in records if r.kind == Kind.NEW_VIEW_ELEMENT]
    assert any(r.element == "cohort" for r in new_els)


def test_invalid_xml_returns_empty():
    records = extract("<not valid", "<still broken", "odoo/addons/base/rng/common.rng")
    assert records == []


def test_non_rng_file_returns_empty():
    records = extract("<xml/>", "<xml/>", "odoo/addons/base/rng/common.xml")
    assert records == []


def test_nothing_changed_emits_nothing():
    src = _wrap("""
    <rng:define name="thing">
        <rng:element name="thing"><rng:attribute name="a"/></rng:element>
    </rng:define>
    """)
    assert extract(src, src, "odoo/addons/base/rng/common.rng") == []
