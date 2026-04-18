"""YAML frontmatter read/write for ledger files.

Frontmatter is machine-managed: overwritten on every ledger update.
Callers that want to persist extra keys across regenerations should
use the `## Notes` section or the narrative block, not frontmatter.
"""

from __future__ import annotations

import yaml

_DELIM = "---"


def split(text: str) -> tuple[dict, str]:
    """Split a markdown file into (frontmatter_dict, body).

    If no frontmatter block is present, returns ({}, text).
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != _DELIM:
        return {}, text
    # Find the closing delimiter.
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == _DELIM:
            end = i
            break
    if end is None:
        return {}, text
    fm_text = "".join(lines[1:end])
    body = "".join(lines[end + 1:])
    if body.startswith("\n"):
        body = body[1:]
    try:
        data = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return data, body


def join(frontmatter: dict, body: str) -> str:
    fm = yaml.safe_dump(frontmatter, default_flow_style=False, sort_keys=False).rstrip()
    return f"{_DELIM}\n{fm}\n{_DELIM}\n\n{body.lstrip()}"
