"""Ledger render unit tests.

Functional pipeline coverage lives in test_ledger_update; here we just
exercise the per-section renderers with hand-built `Primitive` fixtures
so we can spot-check details like the `[repo@sha](url)` link format
without spinning up a fake git repo.
"""

from ofd.aggregate import CommitRef, Primitive, RolloutOccurrence
from ofd.events.record import Kind
from ofd.ledger.render import render_commits


def _commit(sha: str, repo: str = "odoo", subject: str = "[ADD] x") -> CommitRef:
    return CommitRef(
        sha=sha,
        repo=repo,
        committed_at="2026-04-01T00:00:00Z",
        author_name="Some Author",
        author_email="a@example.com",
        subject=subject,
    )


def _rollout(sha: str, file: str, repo: str = "odoo") -> RolloutOccurrence:
    return RolloutOccurrence(
        commit=_commit(sha, repo=repo, subject="[IMP] adopt"),
        file=file,
        model=None,
        before_snippet=None,
        after_snippet=None,
        hunk_header=None,
    )


def test_render_commits_links_when_repo_has_github_url():
    prim = Primitive(
        symbol="odoo.foo.Bar",
        kind=Kind.NEW_PUBLIC_CLASS,
        active_version="20.0",
        definition_commits=[_commit("a" * 40)],
        rollouts=[_rollout("b" * 40, "addons/x/y.py")],
    )
    repo_links = {"odoo": "git@github.com:odoo/odoo.git"}
    out = render_commits(prim, repo_links=repo_links)
    expected_def = "[`odoo@aaaaaaaaaaaa`](https://github.com/odoo/odoo/commit/aaaaaaaaaaaa)"
    expected_rollout = "[`odoo@bbbbbbbbbbbb`](https://github.com/odoo/odoo/commit/bbbbbbbbbbbb)"
    assert expected_def in out
    assert expected_rollout in out


def test_render_commits_falls_back_to_plain_text_without_link():
    """No `repo_links` map -> we still prepend `repo@`, just no link."""
    prim = Primitive(
        symbol="odoo.foo.Bar",
        kind=Kind.NEW_PUBLIC_CLASS,
        active_version="20.0",
        definition_commits=[_commit("c" * 40, repo="enterprise")],
        rollouts=[_rollout("d" * 40, "addons/x/y.py", repo="enterprise")],
    )
    out = render_commits(prim)
    assert "`enterprise@cccccccccccc`" in out
    assert "`enterprise@dddddddddddd`" in out
    # No bracketed-link form should appear.
    assert "](" not in out


def test_render_commits_mixed_repos_render_their_own_repo_prefix():
    """A primitive defined in `odoo` with rollouts in `enterprise` must
    label each commit with the repo it actually lives in - the whole
    point of the prefix is that you don't have to reverse-engineer it."""
    prim = Primitive(
        symbol="odoo.foo.Bar",
        kind=Kind.NEW_PUBLIC_CLASS,
        active_version="20.0",
        definition_commits=[_commit("a" * 40, repo="odoo")],
        rollouts=[_rollout("e" * 40, "x/y.js", repo="enterprise")],
    )
    repo_links = {
        "odoo": "git@github.com:odoo/odoo.git",
        "enterprise": "git@github.com:odoo/enterprise.git",
    }
    out = render_commits(prim, repo_links=repo_links)
    assert "https://github.com/odoo/odoo/commit/aaaaaaaaaaaa" in out
    assert "https://github.com/odoo/enterprise/commit/eeeeeeeeeeee" in out
