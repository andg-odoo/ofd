"""Cross-repo temporal-filter regression.

`_ordered_for_watchlist_build` runs the framework repo's full history
before the adopter repo, so by the time we walk an early adopter-repo
commit, primitives defined years later in the framework repo are
already in the watchlist. The contextual regex can still fire on those
early commits (e.g. `<widget invisible=...>` was already legal at
runtime before the RNG schema formally declared it), but those matches
are temporally impossible adoptions of *this* primitive.

The pipeline drops any rollout whose commit predates the watchlisted
primitive's `first_seen_at`. This test pins that behavior.
"""

from pathlib import Path

from ofd import config as config_mod
from ofd import state as state_mod
from ofd import watchlist as watchlist_mod
from ofd.events.record import Kind
from ofd.events.store import iter_repo
from ofd.pipeline import run as run_pipeline
from tests.fixtures.repo_builder import make_repo


def _write_config(workspace: Path, odoo_mirror: Path, ent_mirror: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "config.yaml").write_text(f"""\
repos:
  odoo:
    source: /dev/null
    mirror: {odoo_mirror}
    branch: master
    framework_paths: [odoo/addons/base/rng/*.rng]
  enterprise:
    source: /dev/null
    mirror: {ent_mirror}
    branch: master
    framework_paths: []
active_version: "20.0"
key_devs: []
""")


def test_cross_repo_rollout_predating_definition_is_dropped(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    # Enterprise: long history with a 2025-09 commit that already uses
    # `<widget ... invisible=...>`. Predates the framework definition.
    enterprise = make_repo(tmp_path, name="enterprise")
    enterprise.commit(
        {"some_addon/views/foo.xml": "<list><field name='x'/></list>"},
        subject="[ADD] some_addon: baseline",
        env={
            "GIT_AUTHOR_DATE": "2025-08-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2025-08-01T00:00:00Z",
        },
    )
    early_sha = enterprise.commit(
        {
            "some_addon/views/foo.xml": (
                "<list>"
                "  <widget name='web_ribbon' invisible=\"state == 'draft'\"/>"
                "  <field name='x'/>"
                "</list>"
            ),
        },
        subject="[IMP] some_addon: pre-definition widget invisible",
        env={
            "GIT_AUTHOR_DATE": "2025-09-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2025-09-01T00:00:00Z",
        },
    )

    # Odoo: framework repo. The RNG file at first declares no `invisible`
    # attr on `widget`; the second commit adds it. That second commit
    # IS the definition event - dated 2026-03-05.
    odoo = make_repo(tmp_path, name="odoo")
    odoo.commit(
        {
            "odoo/addons/base/rng/common.rng": (
                "<grammar xmlns='http://relaxng.org/ns/structure/1.0'>"
                "<define name='widget'>"
                "<element name='widget'><attribute name='name'/></element>"
                "</define>"
                "</grammar>"
            ),
        },
        subject="[ADD] base: rng baseline (no invisible on widget)",
        env={
            "GIT_AUTHOR_DATE": "2025-08-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2025-08-01T00:00:00Z",
        },
    )
    odoo.commit(
        {
            "odoo/addons/base/rng/common.rng": (
                "<grammar xmlns='http://relaxng.org/ns/structure/1.0'>"
                "<define name='widget'>"
                "<element name='widget'>"
                "<attribute name='name'/>"
                "<optional><attribute name='invisible'/></optional>"
                "</element>"
                "</define>"
                "</grammar>"
            ),
        },
        subject="[IMP] base: allow invisible on widget in rng",
        env={
            "GIT_AUTHOR_DATE": "2026-03-05T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-03-05T00:00:00Z",
        },
    )
    # Enterprise: a post-definition commit that legitimately uses the
    # new widget invisible attr. Should be detected as a rollout.
    late_sha = enterprise.commit(
        {
            "another_addon/views/bar.xml": (
                "<form>"
                "  <widget name='web_ribbon' invisible=\"foo == 'bar'\"/>"
                "</form>"
            ),
        },
        subject="[IMP] another_addon: post-definition adoption",
        env={
            "GIT_AUTHOR_DATE": "2026-04-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-04-01T00:00:00Z",
        },
    )

    workspace = tmp_path / "ws"
    _write_config(workspace, odoo.bare, enterprise.bare)
    config = config_mod.load(workspace)
    run_pipeline(config, state_mod.load(), watchlist_mod.load(workspace))

    # Pre-definition adopter commit must NOT have a widget.invisible rollout.
    early_record = next(
        (r for r in iter_repo(workspace, "enterprise") if r.commit.sha == early_sha),
        None,
    )
    if early_record is not None:
        rollout_symbols = {
            c.symbol for c in early_record.changes if c.kind == Kind.ROLLOUT
        }
        assert not any(s and "widget.invisible" in s for s in rollout_symbols), (
            f"early adopter commit got temporally-impossible rollouts: "
            f"{rollout_symbols}"
        )

    # Post-definition commit should have the rollout - confirms the
    # filter isn't dropping legitimate adoptions too.
    late_record = next(
        (r for r in iter_repo(workspace, "enterprise") if r.commit.sha == late_sha),
        None,
    )
    assert late_record is not None
    assert any(
        c.kind == Kind.ROLLOUT and c.symbol and "widget.invisible" in c.symbol
        for c in late_record.changes
    ), "post-definition adoption was dropped"
