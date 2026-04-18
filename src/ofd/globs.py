"""Gitignore-style glob matching.

Supports:
- `*` matches any characters except `/`
- `?` matches any single character except `/`
- `**` matches any number of path segments (including zero)
- literal characters otherwise

Paths are POSIX-normalized (forward slashes).
"""

from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=256)
def _compile(pattern: str) -> re.Pattern[str]:
    segments = pattern.split("/")
    parts: list[str] = []
    for seg in segments:
        if seg == "**":
            parts.append("__DOUBLESTAR__")
        else:
            out = ""
            for ch in seg:
                if ch == "*":
                    out += "[^/]*"
                elif ch == "?":
                    out += "[^/]"
                else:
                    out += re.escape(ch)
            parts.append(out)

    # Stitch with "/", collapsing the ** placeholder properly so it can
    # match zero or more path segments.
    regex = ""
    for i, p in enumerate(parts):
        if p == "__DOUBLESTAR__":
            if i == 0:
                regex = "(?:.*/)?"
            else:
                if regex.endswith("/"):
                    regex = regex[:-1]
                regex += "(?:/.*)?"
        else:
            if i > 0 and not regex.endswith("?") and not regex.endswith("/"):
                regex += "/"
            regex += p
    return re.compile(f"^{regex}$")


def match(path: str, pattern: str) -> bool:
    return _compile(pattern).match(path) is not None


def match_any(path: str, patterns: list[str]) -> bool:
    return any(match(path, p) for p in patterns)
