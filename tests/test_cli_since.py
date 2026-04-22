"""Tests for the --since override parser shared by run and reindex."""

from dataclasses import dataclass

import pytest

from ofd.cli._since import apply_since_overrides
from ofd.state import State


@dataclass
class _FakeRepo:
    name: str


@dataclass
class _FakeConfig:
    repos: list[_FakeRepo]


def _cfg(*names):
    return _FakeConfig(repos=[_FakeRepo(n) for n in names])


def test_bare_sha_applies_to_every_repo():
    state = State()
    apply_since_overrides(state, _cfg("odoo", "enterprise"), ["abc123"])
    assert state.get("odoo").last_seen_sha == "abc123"
    assert state.get("enterprise").last_seen_sha == "abc123"


def test_scoped_form_only_touches_named_repo():
    state = State()
    apply_since_overrides(state, _cfg("odoo", "enterprise"), ["odoo=abc"])
    assert state.get("odoo").last_seen_sha == "abc"
    assert state.get("enterprise").last_seen_sha is None


def test_scoped_overrides_beat_bare_default():
    state = State()
    apply_since_overrides(
        state,
        _cfg("odoo", "enterprise"),
        ["shared", "enterprise=specific"],
    )
    assert state.get("odoo").last_seen_sha == "shared"
    assert state.get("enterprise").last_seen_sha == "specific"


def test_unknown_repo_exits_nonzero(capsys):
    state = State()
    with pytest.raises(SystemExit) as exc:
        apply_since_overrides(state, _cfg("odoo"), ["typo=abc"])
    assert exc.value.code == 2
    assert "unknown repo" in capsys.readouterr().err


def test_empty_noop():
    state = State()
    apply_since_overrides(state, _cfg("odoo"), [])
    assert state.get("odoo").last_seen_sha is None
