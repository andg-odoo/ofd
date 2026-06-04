"""Same-commit kwarg-override dedupe.

When a PR adds the same kwarg to a method override across multiple
Field subclasses, the matcher's per-hunk dedupe attributes adoption
to just one entry; the others sit as zero-rollout shadows of the same
logical primitive. The pipeline collapses these at extraction time so
they never reach the watchlist.
"""

from ofd.events.record import ChangeRecord, Kind
from ofd.pipeline import _dedupe_kwarg_overrides


def _kwarg(symbol: str) -> ChangeRecord:
    return ChangeRecord(
        kind=Kind.NEW_KWARG, file="x.py", line=1, symbol=symbol,
    )


def test_dedupe_keeps_alphabetically_first_symbol():
    """`fields.py` < `fields_textual.py` lexicographically, which keeps
    the root `Field.<method>.<kwarg>` and drops the subclass overrides -
    matching how the rollout matcher attributes hits anyway."""
    records = [
        _kwarg("odoo.orm.fields.Field.to_sql.table"),
        _kwarg("odoo.orm.fields_textual.BaseString.to_sql.table"),
        _kwarg("odoo.orm.fields_relational.Many2one.to_sql.table"),
        _kwarg("odoo.orm.fields_misc.Id.to_sql.table"),
    ]
    out = _dedupe_kwarg_overrides(records)
    assert len(out) == 1
    assert out[0].symbol == "odoo.orm.fields.Field.to_sql.table"


def test_dedupe_groups_independently_by_method_and_kwarg():
    """Different (method, kwarg) pairs are independent - one collapse
    per pair, others untouched."""
    records = [
        _kwarg("odoo.orm.fields.Field.to_sql.table"),
        _kwarg("odoo.orm.fields_relational.Many2one.to_sql.table"),
        _kwarg("odoo.orm.fields.Field.to_sql.query"),
        _kwarg("odoo.orm.fields_relational.Many2one.to_sql.query"),
        _kwarg("odoo.orm.fields.Field.condition_to_sql.table"),
    ]
    out = _dedupe_kwarg_overrides(records)
    symbols = {r.symbol for r in out}
    assert symbols == {
        "odoo.orm.fields.Field.to_sql.table",
        "odoo.orm.fields.Field.to_sql.query",
        "odoo.orm.fields.Field.condition_to_sql.table",
    }


def test_dedupe_leaves_singletons_alone():
    """Most kwargs aren't shared across subclasses - those pass through
    unchanged."""
    records = [
        _kwarg("odoo.orm.fields_relational.Many2one.join.kind"),
        _kwarg("odoo.orm.utils.PrefetchUnion.__init__.ids"),
    ]
    out = _dedupe_kwarg_overrides(records)
    assert {r.symbol for r in out} == {r.symbol for r in records}


def test_dedupe_ignores_non_kwarg_records():
    """NEW_PUBLIC_CLASS / NEW_DECORATOR_OR_HELPER / etc. are passed
    through. The dedupe is scoped to NEW_KWARG because that's where
    the cross-subclass-override pattern lives - other kinds don't
    suffer from the same matcher dedupe collision."""
    other = ChangeRecord(
        kind=Kind.NEW_PUBLIC_CLASS, file="x.py", line=1,
        symbol="odoo.orm.fields.Field",
    )
    records = [
        other,
        _kwarg("odoo.orm.fields.Field.to_sql.table"),
        _kwarg("odoo.orm.fields_textual.BaseString.to_sql.table"),
    ]
    out = _dedupe_kwarg_overrides(records)
    symbols = {r.symbol for r in out}
    assert "odoo.orm.fields.Field" in symbols
    assert "odoo.orm.fields.Field.to_sql.table" in symbols
    assert "odoo.orm.fields_textual.BaseString.to_sql.table" not in symbols


def test_dedupe_empty_input():
    assert _dedupe_kwarg_overrides([]) == []
