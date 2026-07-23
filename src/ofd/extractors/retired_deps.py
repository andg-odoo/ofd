"""Retirement of a pervasive front-end dependency.

Dropping a library the whole code-base leaned on - jQuery is the 19.4
example - is a headline framework story, but it is invisible to every
primitive extractor: a removal introduces no new symbol, so there is
nothing to define or adopt. The work is spread across many commits that
strip the library from asset bundles, templates and scripts, each of
which looks like unremarkable churn to the forward-looking extractors.

This extractor tracks a curated set of such dependencies and models a
retirement as a DEPENDENCY_CHANGE epoch - the same "removed" shape
`dependencies.py` uses for requirements.txt packages, so it lands as a
first-class ledger entry - plus one ROLLOUT per follow-up commit, giving
the ledger both the "jQuery is gone" headline and a breadth count of how
much of the code-base the cleanup swept through.

Precision comes from two gates: the commit *subject* must name the
retirement campaign (any of the dep's subject needles - the replacement
system's name counts, because a migration commit may only name what it
migrates TO: the Font Awesome removal ran under "Material Symbols" /
"data-icon" subjects), and the diff must show a *net removal* of
references to the dep's diff needles (more removed than added), so a
refactor that merely moves a reference never fires. Attribution is by
explicit emission, never the content matcher.
"""

from __future__ import annotations

from typing import NamedTuple

from ofd.events.record import ChangeRecord, Kind


class _RetiredDep(NamedTuple):
    slug: str  # ledger symbol is frontend.<slug>
    display: str
    # any-of, matched lower-cased against the commit subject
    subject_needles: tuple[str, ...]
    # references counted (once per diff line) for the net-removal gate
    diff_needles: tuple[str, ...]


# Curated pervasive front-end dependencies whose retirement is an epoch.
# Add an entry when another whole-code-base dependency is retired.
_RETIRED_DEPS: tuple[_RetiredDep, ...] = (
    _RetiredDep(
        slug="jquery",
        display="jQuery",
        subject_needles=("jquery",),
        diff_needles=("jquery",),
    ),
    # Font Awesome -> Material Symbols + reworked OI library (20.0,
    # odoo 3e15a7be6940): migration subjects name the NEW system, not FA.
    _RetiredDep(
        slug="fontawesome",
        display="Font Awesome",
        subject_needles=(
            "fontawesome",
            "font awesome",
            "font-awesome",
            "material symbol",
            "ms icons",
            "data-icon",
        ),
        diff_needles=("fontawesome", "font-awesome", "font awesome", "fa fa-"),
    ),
)

# Front-end / packaging files where a dependency reference lives: JS,
# QWeb/templates, styles, and the manifest asset bundles.
_FRONTEND_EXT = (".js", ".xml", ".scss", ".css", ".py")


def mentions_retired_dep(subject: str | None) -> bool:
    """Cheap pipeline gate: does `subject` name a tracked retirement?
    Avoids building the commit diff for the vast majority of commits."""
    subj = (subject or "").lower()
    return any(
        needle in subj for dep in _RETIRED_DEPS for needle in dep.subject_needles
    )


def _net_removed(needles: tuple[str, ...], patches: dict[str, str]) -> int:
    """removed-minus-added count of diff lines mentioning any needle
    (each line counted once) across front-end files. Positive means the
    commit strips more references than it adds - a genuine retirement
    rather than a move or rename."""
    removed = added = 0
    for file, patch in patches.items():
        if not file.endswith(_FRONTEND_EXT):
            continue
        for line in patch.splitlines():
            if len(line) < 2 or line[0] not in "+-":
                continue
            if line[1] == line[0]:  # diff header (+++/---), not a content line
                continue
            lowered = line.lower()
            if not any(needle in lowered for needle in needles):
                continue
            if line[0] == "-":
                removed += 1
            else:
                added += 1
    return removed - added


def extract(
    subject: str | None,
    patches: dict[str, str],
    known_symbols,
) -> list[ChangeRecord]:
    """Retirement events for this commit's diff.

    `known_symbols` is the current watchlist symbol set: the first
    commit to retire a dependency emits the DEPENDENCY_CHANGE definition
    (which joins the watchlist), and every retiring commit - including
    that first one - emits one ROLLOUT so the breadth count covers the
    whole cleanup.
    """
    subj = (subject or "").lower()
    records: list[ChangeRecord] = []
    for dep in _RETIRED_DEPS:
        if not any(needle in subj for needle in dep.subject_needles):
            continue  # subject must name the retirement campaign
        if _net_removed(dep.diff_needles, patches) <= 0:
            continue  # not a net removal (added / moved / mentioned only)
        sample = next(
            (f for f in patches if f.endswith(_FRONTEND_EXT)),
            "",
        )
        symbol = f"frontend.{dep.slug}"
        if symbol not in known_symbols:
            records.append(ChangeRecord(
                kind=Kind.DEPENDENCY_CHANGE,
                file=sample,
                line=0,
                symbol=symbol,
                symbol_hint="removed",
                signature=f"{dep.display} retired from the front-end",
            ))
        records.append(ChangeRecord(
            kind=Kind.ROLLOUT,
            file=sample,
            line=0,
            symbol=symbol,
            symbol_hint="removed",
        ))
    return records
