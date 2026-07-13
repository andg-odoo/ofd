"""Test-suite conventions: epochs in how Odoo tests are written.

The 2026-07 test-user mechanism (odoo#273014) runs a test class under a
dedicated non-superuser test user once the class declares
`_test_user_groups`. Every primitive extractor is blind to it: the
mechanism lives in `odoo/addons/base/tests/common.py` (outside all
framework paths) and the attribute is underscore-private. Worse, the
rollout arrived as a 761-file placeholder stamp
(`_test_user_groups = None  # FIXME list needed groups`) that follow-up
branches convert to real group tuples module by module - so a content
matcher counting the token would read the placeholder flood as adoption.

This extractor models the convention as a NEW_TEST_CONVENTION epoch
plus one ROLLOUT per test file that opts in, in either shape:
  - the class gains a *real* (non-None) value, or
  - the None placeholder is deleted with no replacement, so the class
    falls back to the real groups its Common base declares (the shape
    the `master-tests-*-chm` rollout branches use).
Adding a None placeholder never fires, and neither does deleting a real
value (cleanup of an already-opted-in class, not new adoption).
Attribution is by explicit emission, never the content matcher.
"""

from __future__ import annotations

import re

from ofd.events.record import ChangeRecord, Kind

# Curated test-writing conventions worth an epoch entry.
# attribute needle -> watchlist symbol. Add an entry when the test
# framework grows another declarative class-attribute convention.
_TEST_CONVENTIONS: dict[str, str] = {
    "_test_user_groups": "tests._test_user_groups",
}

# An added line declaring a real value: `+    _test_user_groups = (...`.
# `None` (with or without a trailing comment) is the not-migrated-yet
# placeholder and must not count.
_OPT_IN_RES: dict[str, re.Pattern[str]] = {
    needle: re.compile(
        rf"^\+\s*{needle}\s*=\s*(?!None\s*(?:#.*)?$)\S"
    )
    for needle in _TEST_CONVENTIONS
}

# A deleted None placeholder: `-    _test_user_groups = None  # FIXME`.
# With no replacement in the same file this is the inheritance-shaped
# opt-in. Any added declaration line (real or None) in the same file
# disqualifies it: real is already counted by _OPT_IN_RES, None means
# the line merely moved.
_NONE_REMOVED_RES: dict[str, re.Pattern[str]] = {
    needle: re.compile(rf"^-\s*{needle}\s*=\s*None\s*(?:#.*)?$")
    for needle in _TEST_CONVENTIONS
}
_ANY_ADDED_RES: dict[str, re.Pattern[str]] = {
    needle: re.compile(rf"^\+\s*{needle}\s*=")
    for needle in _TEST_CONVENTIONS
}


def is_test_file(path: str) -> bool:
    return path.endswith(".py") and "/tests/" in path


def touches_test_files(files: list[str]) -> bool:
    """Cheap pipeline gate: does the commit touch Python test files?"""
    return any(is_test_file(f) for f in files)


def extract(
    patches: dict[str, str],
    known_symbols,
) -> list[ChangeRecord]:
    """Convention events for this commit's diff.

    `known_symbols` is the current watchlist symbol set: the first
    commit to opt a class in emits the NEW_TEST_CONVENTION definition
    (which joins the watchlist), and every opting-in commit - including
    that first one - emits one ROLLOUT per test file gaining a real
    value, giving the ledger the migration's breadth over time.
    """
    records: list[ChangeRecord] = []
    for needle, symbol in _TEST_CONVENTIONS.items():
        opt_in = _OPT_IN_RES[needle]
        none_removed = _NONE_REMOVED_RES[needle]
        any_added = _ANY_ADDED_RES[needle]
        hit_files = []
        for file, patch in patches.items():
            if not is_test_file(file):
                continue
            if needle not in patch:
                continue
            lines = patch.splitlines()
            added_real = any(opt_in.match(line) for line in lines)
            inherited = any(
                none_removed.match(line) for line in lines
            ) and not any(any_added.match(line) for line in lines)
            if added_real or inherited:
                hit_files.append(file)
        if not hit_files:
            continue
        if symbol not in known_symbols:
            records.append(ChangeRecord(
                kind=Kind.NEW_TEST_CONVENTION,
                file=hit_files[0],
                line=0,
                symbol=symbol,
                signature=f"{needle} = (...) on a test class",
            ))
        records.extend(
            ChangeRecord(
                kind=Kind.ROLLOUT,
                file=file,
                line=0,
                symbol=symbol,
            )
            for file in hit_files
        )
    return records
