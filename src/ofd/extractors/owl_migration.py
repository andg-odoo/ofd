"""OWL 3 migration adoption.

The OWL 2 -> OWL 3 migration is one of the loudest framework stories of
the 19.4 cycle, yet it is invisible to every primitive extractor. A
migration commit swaps one already-importable API for another
(`useLayoutEffect` -> `useEffect`, `useState(...)` -> the proxy form,
the `t-custom-model` template directive -> `t-model`) inside files that
already import from `@odoo/owl`: no new import, no new symbol, so the
import-anchored content matcher attributes nothing. The migration
tooling under `odoo/upgrade_code/**` is `surface_only`, so its recipe
primitives never join the watchlist either. Net result: `@odoo/owl`
sits at zero rollouts while ~160 commits migrate every module.

This extractor closes that gap. It recognises a migration commit by its
subject - Odoo's `[REF] web: Replace useLayoutEffect` /
`[REF] *: Owl3 - replace useState with proxy` convention is reliable -
and emits one ROLLOUT of the `@odoo/owl` vendored-lib primitive per
migration commit, so the ledger's breadth number reflects the real
scope of the migration instead of zero.

The token set mirrors the recipes in `odoo/upgrade_code/owl3-migration.py`
(the `replace_usage(old, new)` / `remove_import(old)` old-side tokens);
extend `_OWL3_SUBJECT` when new recipes land. Only OWL 2-only tokens
that appear in a subject solely *because* the commit migrates away from
them are included, to keep the classifier high-precision.

Attribution is by explicit ROLLOUT emission (the `js_.extract_registry`
/ `modules.extract` pattern), NEVER the content matcher: the matcher
must not carry OWL3 tokens or it would fire on every unrelated
`useEffect(` in the code-base.
"""

from __future__ import annotations

import re

from ofd.events.record import ChangeRecord, Kind
from ofd.extractors.js_ import vendored_lib_alias

# The `@odoo/owl` vendored-lib bump primitive these rollouts attribute to
# (kept in sync with `js_._VENDORED_LIBS`).
OWL_LIB_SYMBOL = "@odoo/owl"

# OWL 2-only APIs / migration phrasings. Each token only shows up in a
# commit subject when the commit is migrating away from it, so a plain
# substring/word hit is a reliable classifier. Deliberately excludes
# ambiguous lifecycle names (`onRendered`, `onWillRender`) that also
# appear in bug-fix subjects.
_OWL3_SUBJECT = re.compile(
    r"""
      owl\s?3                                 # "OWL3", "Owl 3"
    | uselayouteffect                         # -> useEffect
    | onwillupdateprops                       # removed in OWL 3
    | useexternallistener                     # -> useListener
    | t-custom-model                          # -> t-model / t-model.proxy
    | new\ props\ syntax                       # OWL 3 props declaration
    | (?:usestate|reactive)\b[^\n]{0,40}\bproxy   # useState/reactive -> proxy
    | proxy\b[^\n]{0,40}\b(?:usestate|reactive)
    """,
    re.IGNORECASE | re.VERBOSE,
)

# The env-dismantling campaign (2026-07, mcm): "This commit is a step
# to remove the env. The env does not exist in owl3 anymore but still
# exists in odoo." Each step swaps an env member for a service
# (`env.isSmall` -> ui service, 174 files) or renames the test-side env
# plumbing (`makeMockEnv` -> `makeTestEnv`) - API swaps with no new
# import, invisible to the content matcher for the same reason the OWL3
# recipes are. Same epoch, distinct hint for curation.
_ENV_REMOVAL_SUBJECT = re.compile(
    r"""
      replace\ env\.\w+                       # "replace env.isSmall by ..."
    | remove\ \w+\ from\ (?:the\ )?env\b(?!\s+\w)  # "remove isDashboard from env"
                                              # (not "... from env cache")
    | makemockenv                             # test-side env plumbing rename
    """,
    re.IGNORECASE | re.VERBOSE,
)

# The service -> plugin conversions: OWL 3 ships a native `Plugin`
# class (+ signals), and the env-coupled service registry is being
# converted to it one service at a time ("convert currency service to
# plugin", "use offline plugin instead of service"). The conversions
# edit odoo/upgrade_code/owl3-migration.py - same recipe file, same
# epoch. Each new XxxPlugin export is separately caught as a
# NEW_JS_EXPORT primitive; this classifier adds the campaign-level
# breadth number.
_SERVICE_TO_PLUGIN_SUBJECT = re.compile(
    r"""
      convert\ [^\n]{0,40}service\ to\ [^\n]{0,30}plugin
    | plugin\ instead\ of\ [^\n]{0,30}service
    """,
    re.IGNORECASE | re.VERBOSE,
)

# A migration must touch front-end code; a doc/test-only commit that
# merely mentions OWL3 in its subject is not an adoption.
_CODE_EXT = (".js", ".xml")


def is_migration_subject(subject: str | None) -> bool:
    """True when `subject` reads like an OWL 2 -> 3 migration commit."""
    return bool(subject and _OWL3_SUBJECT.search(subject))


def _migration_hint(subject: str | None) -> str | None:
    if not subject:
        return None
    if _OWL3_SUBJECT.search(subject):
        return "owl3-migration"
    if _ENV_REMOVAL_SUBJECT.search(subject):
        return "owl3-env-removal"
    if _SERVICE_TO_PLUGIN_SUBJECT.search(subject):
        return "owl3-service-to-plugin"
    return None


def extract(subject: str | None, changed_files: list[str]) -> list[ChangeRecord]:
    """One `@odoo/owl` ROLLOUT when this commit is an OWL3 migration that
    actually touches front-end code, else an empty list.

    One rollout per commit (not per file): the talk-relevant breadth is
    "how many migration commits", and per-file would inflate a single
    sweeping `[REF] *: ...` commit into thousands of adoptions.
    """
    hint = _migration_hint(subject)
    if hint is None:
        return []
    # The commit that bumps the OWL bundle itself (subject often says
    # "update owl to owl3") is the epoch *definition*, not an adopter -
    # skip it so the bump is never double-counted as its own rollout.
    if any(vendored_lib_alias(f) == OWL_LIB_SYMBOL for f in changed_files):
        return []
    code_files = [f for f in changed_files if f.endswith(_CODE_EXT)]
    if not code_files:
        return []
    return [ChangeRecord(
        kind=Kind.ROLLOUT,
        file=code_files[0],
        line=0,
        symbol=OWL_LIB_SYMBOL,
        symbol_hint=hint,
    )]
