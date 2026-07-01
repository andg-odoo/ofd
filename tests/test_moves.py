"""Tests for move detection (`ofd.moves`)."""

from ofd.events.record import ChangeRecord, CommitEnvelope, Kind
from ofd.moves import detect_moves


def _commit(subject="[IMP] base: thing", body=""):
    return CommitEnvelope(
        sha="abc",
        repo="odoo",
        branch="master",
        active_version="20.0",
        author_name="Alice",
        author_email="alice@example.com",
        committed_at="2026-06-25T00:00:00Z",
        subject=subject,
        body=body,
    )


def _new_helper(symbol, file="odoo/tools/business_data.py"):
    return ChangeRecord(
        kind=Kind.NEW_DECORATOR_OR_HELPER, file=file, line=1, symbol=symbol,
    )


def test_removal_pairing_flags_public_relocation():
    new = _new_helper("odoo.tools.misc.frobnicate", file="odoo/tools/misc.py")
    removed = ChangeRecord(
        kind=Kind.REMOVED_PUBLIC_SYMBOL,
        file="odoo/tools/legacy.py",
        line=1,
        symbol="odoo.tools.legacy.frobnicate",
    )
    detect_moves([new, removed], _commit())
    assert new.moved is True
    assert new.moved_from == "odoo.tools.legacy.frobnicate"


def test_removal_pairing_ignores_same_fqn():
    # A symbol both removed and re-added at the *same* FQN isn't a move
    # (can't happen from the extractor, but guard against it anyway).
    new = _new_helper("odoo.tools.misc.frobnicate", file="odoo/tools/misc.py")
    removed = ChangeRecord(
        kind=Kind.REMOVED_PUBLIC_SYMBOL,
        file="odoo/tools/misc.py",
        line=1,
        symbol="odoo.tools.misc.frobnicate",
    )
    detect_moves([new, removed], _commit())
    assert not new.moved


def test_anchor_case_split_vat_from_commit_message():
    # The real split_vat move: old name private (`_split_vat`), old home a
    # non-gated addon, so no removal event exists - only the message says
    # so. Short name pairs across the leading underscore.
    new = _new_helper("odoo.tools.business_data.split_vat")
    commit = _commit(
        subject="[IMP] account*: add a generic method for VAT number",
        body=(
            "In this commit:\n"
            "   - Move a generic method '_split_vat' from res_partner to tools\n"
        ),
    )
    detect_moves([new], commit)
    assert new.moved is True
    # No removal was paired, so no FQN is recorded.
    assert new.moved_from is None


def test_mov_tag_flags_named_symbol_anywhere_in_message():
    new = ChangeRecord(
        kind=Kind.NEW_PUBLIC_CLASS, file="odoo/orm/foo.py", line=1,
        symbol="odoo.orm.foo.Registry",
    )
    commit = _commit(subject="[MOV] orm: Registry to its own module")
    detect_moves([new], commit)
    assert new.moved is True


def test_move_word_must_share_a_line_with_the_symbol():
    # Move language present, but on a different line from the new symbol -
    # an ordinary feature commit that mentions "move" in passing must not
    # flag the genuinely-new helper.
    new = _new_helper("odoo.tools.misc.brand_new_helper")
    commit = _commit(
        subject="[ADD] tools: brand_new_helper for X",
        body="Also move the call site around.\nAdds brand_new_helper.",
    )
    detect_moves([new], commit)
    assert not new.moved


def test_word_boundary_avoids_substring_false_positive():
    new = _new_helper("odoo.tools.misc.id")
    commit = _commit(body="Move the video handling elsewhere.")
    detect_moves([new], commit)
    assert not new.moved


def test_non_movable_kinds_never_flagged():
    # A new module/manifest key is genuinely new surface even in a commit
    # that says "move".
    mod = ChangeRecord(
        kind=Kind.NEW_MODULE, file="addons/foo/__manifest__.py", line=1,
        symbol="foo",
    )
    commit = _commit(subject="[MOV] foo: move foo into its own module")
    detect_moves([mod], commit)
    assert not mod.moved


def test_plain_commit_leaves_new_symbol_untouched():
    new = _new_helper("odoo.tools.misc.frobnicate")
    detect_moves([new], _commit(subject="[ADD] tools: frobnicate helper"))
    assert not new.moved
    assert new.moved_from is None
