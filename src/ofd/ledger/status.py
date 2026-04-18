"""Status transitions for ledger entries."""

from __future__ import annotations

from datetime import UTC, datetime

from ofd.aggregate import Primitive


def compute_status(
    prim: Primitive,
    fresh_days: int = 30,
    dormant_days: int = 90,
    pinned: bool = False,
    now: datetime | None = None,
) -> str:
    """Return one of: fresh | active | awaiting-adoption | dormant | reverted.

    A caller that has detected a revert should override to `reverted`
    after this function returns.
    """
    if pinned:
        return "active"
    now = now or datetime.now(tz=UTC)
    first_seen = prim.first_seen.astimezone(UTC)
    age_days = (now - first_seen).total_seconds() / 86400
    count = prim.rollout_count
    if age_days < fresh_days:
        return "fresh"
    if count >= 5:
        return "active"
    if age_days > dormant_days:
        return "dormant"
    return "awaiting-adoption"
