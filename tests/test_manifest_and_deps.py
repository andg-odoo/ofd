"""Manifest-key extractor + requirements.txt dependency epochs."""

from pathlib import Path

from ofd import config as config_mod
from ofd import state as state_mod
from ofd import watchlist as watchlist_mod
from ofd.events.record import Kind
from ofd.events.store import iter_repo
from ofd.extractors import dependencies, manifest_keys, modules
from ofd.pipeline import run as run_pipeline
from tests.fixtures.repo_builder import make_repo

# --- manifest keys: unit ----------------------------------------------------


def test_manifest_keys_parse():
    keys = manifest_keys.manifest_keys(
        "# comment\n{\n    'name': 'X',\n    'depends': ['base'],\n"
        "    'assets': {'web.assets_backend': ['x.js']},\n}\n"
    )
    # Top-level only: nested asset-bucket keys are not manifest keys.
    assert set(keys) == {"name", "depends", "assets"}
    assert keys["depends"] == 4
    assert manifest_keys.manifest_keys("not a manifest {") is None
    assert manifest_keys.manifest_keys(None) is None


def test_manifest_new_key_definition_and_rollout():
    files = [
        ("addons/l10n_de/__manifest__.py", "{'name': 'DE'}",
         "{'name': 'DE', 'countries': ['de']}"),
        ("addons/l10n_fr/__manifest__.py", "{'name': 'FR'}",
         "{'name': 'FR', 'countries': ['fr']}"),
    ]
    records = manifest_keys.extract(
        files, known_symbols=frozenset(),
        baseline=frozenset({"manifest.name"}),
    )
    assert [(r.kind, r.symbol, r.file) for r in records] == [
        (Kind.NEW_MANIFEST_KEY, "manifest.countries",
         "addons/l10n_de/__manifest__.py"),
        (Kind.ROLLOUT, "manifest.countries",
         "addons/l10n_fr/__manifest__.py"),
    ]


def test_manifest_known_key_yields_rollout_only():
    files = [(
        "addons/l10n_be/__manifest__.py", "{'name': 'BE'}",
        "{'name': 'BE', 'countries': ['be']}",
    )]
    records = manifest_keys.extract(
        files, known_symbols=frozenset({"manifest.countries"}),
        baseline=frozenset({"manifest.name"}),
    )
    assert [r.kind for r in records] == [Kind.ROLLOUT]


def test_manifest_baseline_and_existing_keys_silent():
    # Editing values / adding a baseline key never fires; neither does
    # a brand-new module whose manifest only uses baseline keys.
    base = frozenset({"manifest.name", "manifest.depends", "manifest.data"})
    files = [
        ("addons/foo/__manifest__.py",
         "{'name': 'Foo', 'depends': ['base']}",
         "{'name': 'Foo!', 'depends': ['base', 'web'], 'data': []}"),
        ("addons/newmod/__manifest__.py", None,
         "{'name': 'New', 'depends': ['base']}"),
    ]
    assert manifest_keys.extract(files, frozenset(), base) == []


def test_manifest_test_modules_skipped():
    assert manifest_keys.is_test_module_manifest(
        "odoo/addons/test_new_api/__manifest__.py"
    )
    files = [(
        "odoo/addons/test_new_api/__manifest__.py", "{}",
        "{'bogus_loader_key': 1}",
    )]
    assert manifest_keys.extract(files, frozenset(), frozenset()) == []


# --- modules: unit -----------------------------------------------------------


def test_new_and_removed_module():
    new = modules.extract(
        [("addons/base_report_pm/__manifest__.py", None,
          "{'name': 'Paper Muncher', 'category': 'Technical',"
          " 'depends': ['base']}")],
        known_symbols=frozenset(),
    )
    assert [(r.kind, r.symbol, r.signature, r.auto_install) for r in new] == [
        (Kind.NEW_MODULE, "module.base_report_pm",
         "Paper Muncher (Technical)", None),
    ]
    removed = modules.extract(
        [("addons/base_report_pm/__manifest__.py",
          "{'name': 'Paper Muncher'}", None)],
        known_symbols=frozenset(),
    )
    assert [(r.kind, r.symbol) for r in removed] == [
        (Kind.REMOVED_MODULE, "module.base_report_pm"),
    ]


def test_module_bridge_carries_auto_install():
    records = modules.extract(
        [("addons/pos_self_order_qfpay/__manifest__.py", None,
          "{'name': 'Bridge', 'auto_install': True, 'depends': ['pos_qfpay']}")],
        known_symbols=frozenset(),
    )
    assert records[0].kind is Kind.NEW_MODULE
    assert records[0].auto_install is True


def test_module_depends_rollout():
    # An existing manifest adding a tracked module to depends is the
    # adoption surface; untracked additions stay silent.
    records = modules.extract(
        [("addons/mail/__manifest__.py",
          "{'name': 'Mail', 'depends': ['base']}",
          "{'name': 'Mail',\n 'depends': ['base', 'paper_muncher', 'web']}")],
        known_symbols=frozenset({"module.paper_muncher"}),
    )
    assert [(r.kind, r.symbol, r.line) for r in records] == [
        (Kind.ROLLOUT, "module.paper_muncher", 2),
    ]


def test_module_same_commit_bridge_adoption():
    # A module landing together with its bridge: the bridge's depends
    # is the new module's first rollout, same-commit.
    records = modules.extract(
        [
            ("addons/pm/__manifest__.py", None, "{'name': 'PM'}"),
            ("addons/pm_bridge/__manifest__.py", None,
             "{'name': 'PM Bridge', 'auto_install': True, 'depends': ['pm']}"),
        ],
        known_symbols=frozenset(),
    )
    assert [(r.kind, r.symbol) for r in records] == [
        (Kind.NEW_MODULE, "module.pm"),
        (Kind.NEW_MODULE, "module.pm_bridge"),
        (Kind.ROLLOUT, "module.pm"),
    ]


def test_module_test_modules_and_unparseable_skipped():
    files = [
        ("odoo/addons/test_new_api/__manifest__.py", None, "{'name': 'T'}"),
        ("addons/broken/__manifest__.py", None, "not a manifest {"),
    ]
    assert modules.extract(files, known_symbols=frozenset()) == []


# --- requirements.txt: unit --------------------------------------------------


def test_dependency_added_and_removed():
    parent = "Babel==2.10.3\nlxml==5.2.1 ; python_version >= '3.11'\n"
    child = (
        "Babel==2.10.3\n"
        "lxml==5.2.1 ; python_version >= '3.11'\n"
        "weasyprint==62.3  # new PDF engine\n"
    )
    records = dependencies.extract(parent, child, "requirements.txt")
    assert [(r.kind, r.symbol, r.symbol_hint) for r in records] == [
        (Kind.DEPENDENCY_CHANGE, "requirements.weasyprint", "added"),
    ]
    assert records[0].signature == "weasyprint==62.3"
    removed = dependencies.extract(child, parent, "requirements.txt")
    assert [(r.symbol, r.symbol_hint) for r in removed] == [
        ("requirements.weasyprint", "removed"),
    ]


def test_dependency_version_and_marker_churn_silent():
    parent = "lxml==5.2.1\nBabel==2.10.3 ; python_version < '3.11'\n"
    child = (
        "lxml==5.4.0\n"
        "Babel==2.10.3 ; python_version < '3.11'\n"
        "Babel==2.14.0 ; python_version >= '3.11'\n"
    )
    assert dependencies.extract(parent, child, "requirements.txt") == []
    # Fresh vendoring / deletion: no "before" to compare.
    assert dependencies.extract(None, child, "requirements.txt") == []
    assert dependencies.extract(parent, None, "requirements.txt") == []


# --- end-to-end through the pipeline -----------------------------------------


def _write_config(workspace: Path, mirror: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "config.yaml").write_text(f"""\
repos:
  odoo:
    source: /dev/null
    mirror: {mirror}
    branch: master
    framework_paths:
      - odoo/orm/**/*.py
    core_paths: []
active_version: "20.0"
since_date: "2025-06-01"
key_devs: []
scoring:
  thresholds: {{surface: 3, ledger_threshold: 4, narrate: 5}}
  breadth_bonuses:
    - {{min_rollouts: 5, bonus: 1}}
  dormant_days: 90
  fresh_days: 30
  intent_keywords: [introduce]
narrate:
  backend: claude_code
""")


def _dated(date: str) -> dict[str, str]:
    stamp = f"{date}T12:00:00 +0000"
    return {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}


def test_end_to_end_manifest_and_requirements(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    repo = make_repo(tmp_path)

    # Pre-floor: baseline manifests + requirements.
    repo.commit(
        {
            "addons/base/__manifest__.py":
                "{'name': 'Base', 'depends': [], 'version': '1.0'}\n",
            "requirements.txt": "Babel==2.10.3\nlxml==5.2.1\n",
        },
        subject="[ADD] base: initial",
        env=_dated("2025-01-15"),
    )

    # Post-floor A: a new manifest key appears.
    key_sha = repo.commit(
        {"addons/l10n_de/__manifest__.py":
            "{'name': 'DE', 'depends': ['base'], 'countries': ['de']}\n"},
        subject="[ADD] l10n_de: country scoping",
        env=_dated("2025-07-01"),
    )
    # Post-floor B: another module adopts the key.
    adopt_sha = repo.commit(
        {"addons/l10n_fr/__manifest__.py":
            "{'name': 'FR', 'depends': ['base'], 'countries': ['fr']}\n"},
        subject="[ADD] l10n_fr: country scoping",
        env=_dated("2025-07-02"),
    )
    # Post-floor C: new external dependency.
    dep_sha = repo.commit(
        {"requirements.txt":
            "Babel==2.10.3\nlxml==5.2.1\nweasyprint==62.3\n"},
        subject="[IMP] core: add weasyprint engine",
        env=_dated("2025-07-03"),
    )

    workspace = tmp_path / "ws"
    _write_config(workspace, repo.bare)
    config = config_mod.load(workspace)
    state = state_mod.load()
    watchlist = watchlist_mod.load(workspace)

    summary = run_pipeline(config, state, watchlist)
    assert not summary.errors, summary.errors

    records = {cr.commit.sha: cr for cr in iter_repo(workspace, "odoo")}
    assert set(records) == {key_sha, adopt_sha, dep_sha}

    a = records[key_sha].changes
    assert [(c.kind, c.symbol) for c in a] == [
        (Kind.NEW_MANIFEST_KEY, "manifest.countries"),
        (Kind.NEW_MODULE, "module.l10n_de"),
    ]
    b = records[adopt_sha].changes
    assert [(c.kind, c.symbol) for c in b] == [
        (Kind.ROLLOUT, "manifest.countries"),
        (Kind.NEW_MODULE, "module.l10n_fr"),
    ]
    c = records[dep_sha].changes
    assert [(x.kind, x.symbol, x.symbol_hint) for x in c] == [
        (Kind.DEPENDENCY_CHANGE, "requirements.weasyprint", "added"),
    ]
    assert c[0].score >= 4  # epoch: ledger on its own

    persisted = watchlist_mod.load(workspace)
    assert "manifest.countries" in persisted.entries
    assert "module.l10n_de" in persisted.entries
    # Baseline keys never became primitives.
    assert "manifest.version" not in persisted.entries
    # Pre-floor modules never became primitives either.
    assert "module.base" not in persisted.entries


def test_end_to_end_module_lifecycle(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    repo = make_repo(tmp_path)

    # Pre-floor baseline: carries every manifest key used below so the
    # manifest-key extractor stays silent and only module events fire.
    repo.commit(
        {"addons/base/__manifest__.py":
            "{'name': 'Base', 'depends': [], 'version': '1.0',"
            " 'category': 'Hidden', 'auto_install': False}\n"},
        subject="[ADD] base: initial",
        env=_dated("2025-01-15"),
    )

    # A: new module + its auto_install bridge in one commit.
    add_sha = repo.commit(
        {
            "addons/paper_muncher/__manifest__.py":
                "{'name': 'Paper Muncher', 'category': 'Technical',"
                " 'depends': ['base']}\n",
            "addons/pm_bridge/__manifest__.py":
                "{'name': 'PM Bridge', 'auto_install': True,"
                " 'depends': ['paper_muncher']}\n",
        },
        subject="[ADD] paper_muncher: new report engine",
        env=_dated("2025-07-01"),
    )
    # B: an existing module adopts it via depends.
    adopt_sha = repo.commit(
        {"addons/base/__manifest__.py":
            "{'name': 'Base', 'depends': ['paper_muncher'], 'version': '1.0',"
            " 'category': 'Hidden', 'auto_install': False}\n"},
        subject="[IMP] base: render through paper_muncher",
        env=_dated("2025-07-02"),
    )
    # C: the bridge dies.
    rem_sha = repo.commit(
        {"addons/pm_bridge/__manifest__.py": None},
        subject="[REM] pm_bridge: fold into paper_muncher",
        env=_dated("2025-07-03"),
    )

    workspace = tmp_path / "ws"
    _write_config(workspace, repo.bare)
    config = config_mod.load(workspace)
    state = state_mod.load()
    watchlist = watchlist_mod.load(workspace)

    summary = run_pipeline(config, state, watchlist)
    assert not summary.errors, summary.errors

    records = {cr.commit.sha: cr for cr in iter_repo(workspace, "odoo")}
    assert set(records) == {add_sha, adopt_sha, rem_sha}

    a = {(c.kind, c.symbol): c for c in records[add_sha].changes}
    assert set(a) == {
        (Kind.NEW_MODULE, "module.paper_muncher"),
        (Kind.NEW_MODULE, "module.pm_bridge"),
        (Kind.ROLLOUT, "module.paper_muncher"),
    }
    # [ADD] commit: base 2 + tag 1 = 3 (surface); the bridge's
    # auto_install penalty sinks it back to 2.
    assert a[(Kind.NEW_MODULE, "module.paper_muncher")].score == 3
    assert a[(Kind.NEW_MODULE, "module.pm_bridge")].score == 2

    b = records[adopt_sha].changes
    assert [(c.kind, c.symbol, c.file) for c in b] == [
        (Kind.ROLLOUT, "module.paper_muncher", "addons/base/__manifest__.py"),
    ]

    c = records[rem_sha].changes
    assert [(x.kind, x.symbol, x.signature) for x in c] == [
        (Kind.REMOVED_MODULE, "module.pm_bridge", "PM Bridge"),
    ]

    persisted = watchlist_mod.load(workspace)
    assert "module.paper_muncher" in persisted.entries
    assert persisted.entries["module.paper_muncher"].kind is Kind.NEW_MODULE
