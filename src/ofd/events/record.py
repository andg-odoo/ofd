"""Dataclasses for commit envelopes and change records.

All change-record kinds share a common envelope; kind-specific fields are
optional and populated only when relevant. Keeps JSON serialization simple
and schema-evolvable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum


class Kind(StrEnum):
    NEW_PUBLIC_CLASS = "new_public_class"
    NEW_DECORATOR_OR_HELPER = "new_decorator_or_helper"
    NEW_CLASS_ATTRIBUTE = "new_class_attribute"
    NEW_KWARG = "new_kwarg"
    SIGNATURE_CHANGE = "signature_change"
    DEPRECATION_WARNING_ADDED = "deprecation_warning_added"
    REMOVED_PUBLIC_SYMBOL = "removed_public_symbol"
    NEW_ENDPOINT = "new_endpoint"
    NEW_VIEW_ATTRIBUTE = "new_view_attribute"
    NEW_VIEW_ELEMENT = "new_view_element"
    NEW_VIEW_TYPE = "new_view_type"
    NEW_VIEW_DIRECTIVE = "new_view_directive"
    REMOVED_VIEW_ATTRIBUTE = "removed_view_attribute"
    NEW_CONTEXT_KEY = "new_context_key"
    ROLLOUT = "rollout"


DEFINITION_KINDS = frozenset({
    Kind.NEW_PUBLIC_CLASS,
    Kind.NEW_DECORATOR_OR_HELPER,
    Kind.NEW_ENDPOINT,
    Kind.NEW_KWARG,
    Kind.NEW_VIEW_TYPE,
    Kind.NEW_VIEW_ATTRIBUTE,
    Kind.NEW_VIEW_ELEMENT,
    Kind.NEW_VIEW_DIRECTIVE,
    Kind.NEW_CONTEXT_KEY,
})
"""Kinds that introduce a watchlist-able primitive."""


@dataclass
class CommitEnvelope:
    sha: str
    repo: str
    branch: str
    active_version: str
    author_name: str
    author_email: str
    committed_at: str  # ISO-8601 UTC
    subject: str
    body: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ChangeRecord:
    kind: Kind
    file: str
    line: int
    score: int = 0
    score_reasons: list[str] = field(default_factory=list)

    # Optional kind-specific fields. All default to None so every kind can be
    # represented in one dataclass without a visitor hierarchy.
    symbol: str | None = None
    symbol_hint: str | None = None
    signature: str | None = None
    before_signature: str | None = None
    after_signature: str | None = None
    before_snippet: str | None = None
    after_snippet: str | None = None
    warning_text: str | None = None
    removal_version: str | None = None
    route: str | None = None
    attribute: str | None = None
    element: str | None = None
    rng_file: str | None = None
    type_name: str | None = None
    registry: str | None = None
    directive: str | None = None
    model: str | None = None
    xml_path: str | None = None
    hunk_header: str | None = None

    def to_dict(self) -> dict:
        raw = asdict(self)
        raw["kind"] = self.kind.value
        return {k: v for k, v in raw.items() if v is not None and v != []}


@dataclass
class CommitRecord:
    """Full serialization unit - one file per commit on disk."""

    commit: CommitEnvelope
    changes: list[ChangeRecord]
    schema_version: int = 1

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "commit": self.commit.to_dict(),
            "changes": [c.to_dict() for c in self.changes],
        }

    @classmethod
    def from_dict(cls, data: dict) -> CommitRecord:
        envelope = CommitEnvelope(**data["commit"])
        changes = [
            ChangeRecord(
                kind=Kind(c["kind"]),
                file=c["file"],
                line=c["line"],
                score=c.get("score", 0),
                score_reasons=c.get("score_reasons", []),
                **{
                    k: v for k, v in c.items()
                    if k not in {"kind", "file", "line", "score", "score_reasons"}
                },
            )
            for c in data["changes"]
        ]
        return cls(
            commit=envelope,
            changes=changes,
            schema_version=data.get("schema_version", 1),
        )
