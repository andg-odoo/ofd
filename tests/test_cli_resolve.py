"""Tests for CLI symbol resolution (exact, ambiguous, unmatched)."""

from __future__ import annotations

import pytest

from ofd.cli._resolve import resolve_symbol


def test_exact_match_wins():
    got = resolve_symbol(
        ["odoo.orm.models_cached.CachedModel", "odoo.foo.CachedModel"],
        "odoo.foo.CachedModel",
    )
    assert got == "odoo.foo.CachedModel"


def test_unique_last_segment_match():
    got = resolve_symbol(
        ["odoo.orm.models_cached.CachedModel", "odoo.foo.Other"],
        "CachedModel",
    )
    assert got == "odoo.orm.models_cached.CachedModel"


def test_ambiguous_last_segment_exits_nonzero(capsys):
    with pytest.raises(SystemExit) as exc:
        resolve_symbol(
            ["odoo.a.Query", "odoo.b.Query", "odoo.c.Other"],
            "Query",
        )
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "ambiguous" in err
    assert "odoo.a.Query" in err
    assert "odoo.b.Query" in err


def test_no_match_suggests_substring(capsys):
    with pytest.raises(SystemExit) as exc:
        resolve_symbol(
            ["odoo.orm.models_cached.CachedModel", "odoo.tools.cache_helper"],
            "cache",
        )
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "no symbol matching" in err
    assert "did you mean" in err
    assert "CachedModel" in err or "cache_helper" in err


def test_no_match_no_suggestions(capsys):
    with pytest.raises(SystemExit) as exc:
        resolve_symbol(["odoo.foo.Bar"], "zzz_nonsense_zzz")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "no symbol matching" in err
    assert "did you mean" not in err
