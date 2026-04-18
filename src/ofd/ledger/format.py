"""Marker-based ledger file format.

Each ledger file body contains:
- `<!-- ofd:auto:NAME -->` / `<!-- /ofd:auto:NAME -->` regions, fully
  regenerated on every update.
- A `<!-- ofd:narrative -->` / `<!-- /ofd:narrative -->` region, shared
  with the user: filled by LLM when empty, preserved thereafter unless
  an explicit --force-narrative flag is passed.
- Everything else outside any marker (including `## Notes`) - never
  touched by the updater.

The parse/render contract is intentionally minimal: one function
extracts the marker regions and user-owned text, a second function
reassembles everything from a fresh auto map + preserved state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_OPEN = re.compile(r"<!--\s*ofd:(?P<name>[a-z0-9_]+(?::[a-z0-9_]+)*)\s*-->")
_CLOSE = re.compile(r"<!--\s*/ofd:(?P<name>[a-z0-9_]+(?::[a-z0-9_]+)*)\s*-->")


@dataclass
class ParsedBody:
    """Result of parsing a ledger file body (after frontmatter)."""
    marker_content: dict[str, str] = field(default_factory=dict)
    # Ordered list of top-level fragments: ("text", str) for user-owned
    # text, ("marker", name) for a marker region's placement.
    layout: list[tuple[str, str]] = field(default_factory=list)

    def narrative(self) -> str:
        return self.marker_content.get("narrative", "").strip()

    def user_tail(self) -> str:
        """Return the trailing user-owned text (including `## Notes`)."""
        if self.layout and self.layout[-1][0] == "text":
            return self.layout[-1][1]
        return ""


def parse_body(body: str) -> ParsedBody:
    """Parse a ledger body into markers + user-owned layout."""
    result = ParsedBody()
    pos = 0
    n = len(body)

    while pos < n:
        open_m = _OPEN.search(body, pos)
        if not open_m:
            result.layout.append(("text", body[pos:]))
            break
        # Preserve whatever comes before the marker.
        if open_m.start() > pos:
            result.layout.append(("text", body[pos:open_m.start()]))
        name = open_m.group("name")
        close_re = re.compile(rf"<!--\s*/ofd:{re.escape(name)}\s*-->")
        close_m = close_re.search(body, open_m.end())
        if not close_m:
            # Unterminated marker - treat the rest as user text.
            result.layout.append(("text", body[open_m.start():]))
            break
        inner = body[open_m.end():close_m.start()]
        result.marker_content[name] = inner.strip("\n")
        result.layout.append(("marker", name))
        pos = close_m.end()
        if pos < n and body[pos] == "\n":
            pos += 1

    return result


def render_body(
    parsed: ParsedBody,
    regenerated: dict[str, str],
    default_layout: list[tuple[str, str]],
    narrative_policy: str = "preserve",
) -> str:
    """Reassemble a body by replacing marker content.

    Args:
      parsed: existing parsed body (from `parse_body` on the prior
        file). Pass a fresh ParsedBody if no prior file exists.
      regenerated: map of marker name -> new content for regeneratable
        auto markers (e.g. "auto:summary", "auto:before_after").
      default_layout: the canonical layout of markers + user-owned
        placeholders to use when the prior layout is empty. Same shape
        as `ParsedBody.layout`.
      narrative_policy: "preserve" keeps the existing narrative,
        "force" replaces it with regenerated["narrative"] if present,
        "fill_if_empty" replaces only when the existing narrative is
        empty.
    """
    existing = dict(parsed.marker_content)  # copy
    new = dict(regenerated)

    layout = parsed.layout if parsed.layout else default_layout

    # Resolve narrative.
    nar_new = new.pop("narrative", None)
    nar_old = existing.get("narrative", "").strip()
    if narrative_policy == "force" and nar_new is not None or narrative_policy == "fill_if_empty" and not nar_old and nar_new:
        final_narrative = nar_new
    else:
        final_narrative = nar_old

    # Apply regenerated auto markers.
    for name, content in new.items():
        existing[name] = content

    # Emit the body in layout order.
    out_parts: list[str] = []
    emitted_markers: set[str] = set()
    for kind, payload in layout:
        if kind == "text":
            out_parts.append(payload)
        else:
            name = payload
            emitted_markers.add(name)
            block = final_narrative if name == "narrative" else existing.get(name, "").strip()
            out_parts.append(
                f"<!-- ofd:{name} -->\n{block}\n<!-- /ofd:{name} -->\n"
            )

    # Any newly-generated auto sections that weren't in the prior layout
    # get appended at the end, just before the user tail (if any).
    pending = [n for n in new if n not in emitted_markers and n != "narrative"]
    if pending:
        tail_idx = None
        for i in range(len(out_parts) - 1, -1, -1):
            if layout[i][0] == "text":
                tail_idx = i
                break
        insertion = "\n".join(
            f"<!-- ofd:{n} -->\n{existing[n]}\n<!-- /ofd:{n} -->\n" for n in pending
        )
        if tail_idx is None:
            out_parts.append(insertion)
        else:
            out_parts.insert(tail_idx, insertion)

    return "".join(out_parts)
