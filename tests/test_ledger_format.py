"""Tests for ledger/format.py and ledger/frontmatter.py."""

from ofd.ledger import format as fmt
from ofd.ledger import frontmatter as fm


def test_frontmatter_split_and_join_roundtrip():
    original = """---
symbol: odoo.orm.models_cached.CachedModel
status: active
score: 5
---

# CachedModel

body text
"""
    data, body = fm.split(original)
    assert data["symbol"] == "odoo.orm.models_cached.CachedModel"
    assert data["status"] == "active"
    assert body.startswith("# CachedModel")
    joined = fm.join(data, body)
    # Re-split should give the same values.
    data2, body2 = fm.split(joined)
    assert data2 == data
    assert body2.strip() == body.strip()


def test_frontmatter_missing_returns_empty_dict():
    data, body = fm.split("no frontmatter here")
    assert data == {}
    assert body == "no frontmatter here"


def test_parse_body_extracts_marker_content():
    body = """\
# thing

<!-- ofd:auto:summary -->
Introduced on 2026-01-15.
<!-- /ofd:auto:summary -->

<!-- ofd:narrative -->
This is why it matters.
<!-- /ofd:narrative -->

## Notes

My own thoughts here.
"""
    parsed = fmt.parse_body(body)
    assert parsed.marker_content["auto:summary"] == "Introduced on 2026-01-15."
    assert parsed.marker_content["narrative"] == "This is why it matters."
    # User-owned trailing section should survive in the layout.
    assert "My own thoughts here." in parsed.user_tail()


def test_render_body_preserves_narrative_by_default():
    existing = """\
# thing

<!-- ofd:auto:summary -->
OLD SUMMARY
<!-- /ofd:auto:summary -->

<!-- ofd:narrative -->
Hand-edited narrative.
<!-- /ofd:narrative -->

## Notes
Keep me.
"""
    parsed = fmt.parse_body(existing)
    regenerated = {
        "auto:summary": "NEW SUMMARY",
        "narrative": "LLM regenerated narrative",
    }
    out = fmt.render_body(
        parsed, regenerated, default_layout=[],
        narrative_policy="preserve",
    )
    assert "NEW SUMMARY" in out
    assert "OLD SUMMARY" not in out
    assert "Hand-edited narrative." in out
    assert "LLM regenerated narrative" not in out
    assert "## Notes" in out
    assert "Keep me." in out


def test_render_body_force_narrative_overrides():
    existing = """\
<!-- ofd:narrative -->
Hand-edited.
<!-- /ofd:narrative -->
"""
    parsed = fmt.parse_body(existing)
    out = fmt.render_body(
        parsed, {"narrative": "Fresh narrative"},
        default_layout=[], narrative_policy="force",
    )
    assert "Fresh narrative" in out
    assert "Hand-edited" not in out


def test_render_body_fill_if_empty_only_when_blank():
    # Existing narrative is blank.
    existing = """\
<!-- ofd:narrative -->

<!-- /ofd:narrative -->
"""
    parsed = fmt.parse_body(existing)
    out = fmt.render_body(
        parsed, {"narrative": "Filled in"},
        default_layout=[], narrative_policy="fill_if_empty",
    )
    assert "Filled in" in out

    # Existing narrative has content - should be preserved.
    existing2 = """\
<!-- ofd:narrative -->
Already written.
<!-- /ofd:narrative -->
"""
    parsed2 = fmt.parse_body(existing2)
    out2 = fmt.render_body(
        parsed2, {"narrative": "New text"},
        default_layout=[], narrative_policy="fill_if_empty",
    )
    assert "Already written." in out2
    assert "New text" not in out2


def test_render_body_fresh_file_uses_default_layout():
    """When there's no existing file, the default layout drives output."""
    parsed = fmt.ParsedBody()
    default_layout = [
        ("text", "# Thing\n\n"),
        ("marker", "auto:summary"),
        ("text", "\n"),
        ("marker", "narrative"),
        ("text", "\n## Notes\n"),
    ]
    out = fmt.render_body(
        parsed,
        {"auto:summary": "Summary here", "narrative": "Why it matters"},
        default_layout=default_layout,
        narrative_policy="force",
    )
    assert "# Thing" in out
    assert "Summary here" in out
    assert "Why it matters" in out
    assert "## Notes" in out


def test_parse_body_unterminated_marker_preserves_text():
    body = """\
before
<!-- ofd:auto:summary -->
no closer
"""
    parsed = fmt.parse_body(body)
    # Layout should preserve the text even though the marker was broken.
    assert any(frag[0] == "text" for frag in parsed.layout)
