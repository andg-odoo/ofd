"""Parse Odoo's `odoo/release.py` to learn the series master is tracking.

master's version_info tuple bumps once per release cycle (e.g. 19.3 -> 19.4
after 19.3 is forked off). Detecting this lets us stamp each ChangeRecord
with the version it actually landed in, so adoption analysis can compare
primitives that had different amounts of time to accumulate rollouts.
"""

from __future__ import annotations

import re

# `version_info = (19, 4, 0, ALPHA, 1, '')` -> captures 19, 4.
# Tolerates SAAS-flavored majors (`'saas~17'`) by keeping the capture
# loose on the left side; leaves odd formats to fall through to None.
_VERSION_TUPLE = re.compile(
    r"""^\s*version_info\s*=\s*\(
        \s*(?P<major>['"]?[\w.~-]+['"]?|\d+)\s*,
        \s*(?P<minor>\d+)\s*,""",
    re.VERBOSE | re.MULTILINE,
)

_RELEASE_PATHS = ("odoo/release.py",)


def is_release_file(path: str) -> bool:
    """True if `path` holds a version constant we know how to parse."""
    return path in _RELEASE_PATHS


def detect_version(src: str | None) -> str | None:
    """Return the `major.minor` series declared in a release.py source.

    Strips quotes around SAAS-style majors (`'saas~17'.3` stays `'saas~17'.3`).
    Returns None on None/empty input, or when the file doesn't match the
    expected tuple shape (private forks, moved schema, etc.).
    """
    if not src:
        return None
    m = _VERSION_TUPLE.search(src)
    if not m:
        return None
    major = m.group("major").strip("'\"")
    minor = m.group("minor")
    return f"{major}.{minor}"
