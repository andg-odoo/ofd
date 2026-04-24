"""Config loading and workspace resolution."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class RepoConfig:
    name: str
    source: str
    mirror: Path
    branch: str
    framework_paths: list[str]
    core_paths: list[str] = field(default_factory=list)


@dataclass
class BreadthBonus:
    min_rollouts: int
    bonus: int


@dataclass
class ScoringConfig:
    surface: int = 3
    ledger_threshold: int = 4
    narrate: int = 5
    breadth_bonuses: list[BreadthBonus] = field(default_factory=lambda: [
        BreadthBonus(5, 1),
        BreadthBonus(20, 2),
        BreadthBonus(50, 3),
    ])
    dormant_days: int = 90
    fresh_days: int = 30
    intent_keywords: list[str] = field(default_factory=lambda: ["introduce", "new api", "replace"])


@dataclass
class NarrateConfig:
    backend: str = "claude_code"
    model: str = "claude-sonnet-4-6"
    default_status_filter: list[str] = field(default_factory=lambda: ["fresh", "active"])
    min_rollouts: int = 0


@dataclass
class Config:
    workspace: Path
    repos: list[RepoConfig]
    active_version: str
    key_devs: list[str]
    scoring: ScoringConfig
    narrate: NarrateConfig
    # Optional ISO date floor (e.g. "2025-10-01"). When the state is
    # empty (fresh workspace, or reindex wiped state), commit enumeration
    # is capped with `git log --since=<date>` so we don't scan 15 years
    # of pre-19.0 history. `--since` CLI overrides still take precedence.
    since_date: str | None = None

    def repo(self, name: str) -> RepoConfig:
        for r in self.repos:
            if r.name == name:
                return r
        raise KeyError(f"repo {name!r} not configured")


def _expand(p: str | Path) -> Path:
    return Path(os.path.expanduser(str(p))).resolve()


def resolve_workspace(explicit: str | None = None) -> Path:
    """Locate workspace by CLI flag > env > pointer > default."""
    if explicit:
        return _expand(explicit)
    env = os.environ.get("OFD_WORKSPACE")
    if env:
        return _expand(env)
    pointer = Path.home() / ".config" / "ofd" / "workspace"
    if pointer.exists():
        return _expand(pointer.read_text().strip())
    return _expand("~/ofd-workspace")


def load(workspace: Path) -> Config:
    config_path = workspace / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"no config.yaml at {config_path}")

    with config_path.open() as f:
        data = yaml.safe_load(f) or {}

    repos = [
        RepoConfig(
            name=name,
            source=cfg["source"],
            mirror=_expand(cfg["mirror"]),
            branch=cfg.get("branch", "master"),
            framework_paths=cfg.get("framework_paths", []),
            core_paths=cfg.get("core_paths", []),
        )
        for name, cfg in (data.get("repos") or {}).items()
    ]

    scoring_data = data.get("scoring") or {}
    thresholds = scoring_data.get("thresholds") or {}
    breadth = [
        BreadthBonus(**b) for b in scoring_data.get("breadth_bonuses", [])
    ] or None

    scoring = ScoringConfig(
        surface=thresholds.get("surface", 3),
        ledger_threshold=thresholds.get("ledger_threshold", 4),
        narrate=thresholds.get("narrate", 5),
        breadth_bonuses=breadth or [BreadthBonus(5, 1), BreadthBonus(20, 2), BreadthBonus(50, 3)],
        dormant_days=scoring_data.get("dormant_days", 90),
        fresh_days=scoring_data.get("fresh_days", 30),
        intent_keywords=scoring_data.get("intent_keywords", ["introduce", "new api", "replace"]),
    )

    narrate_data = data.get("narrate") or {}
    narrate = NarrateConfig(
        backend=narrate_data.get("backend", "claude_code"),
        model=narrate_data.get("model", "claude-sonnet-4-6"),
        default_status_filter=narrate_data.get("default_status_filter", ["fresh", "active"]),
        min_rollouts=narrate_data.get("min_rollouts", 0),
    )

    return Config(
        workspace=workspace,
        repos=repos,
        active_version=data.get("active_version", "master"),
        key_devs=data.get("key_devs", []),
        scoring=scoring,
        narrate=narrate,
        since_date=data.get("since_date"),
    )


DEFAULT_CONFIG_YAML = """\
repos:
  odoo:
    source: git@github.com:odoo/odoo.git
    mirror: ~/.cache/ofd/odoo.git
    branch: master
    framework_paths:
      - odoo/models/**/*.py
      - odoo/fields.py
      - odoo/api.py
      - odoo/osv/**/*.py
      - odoo/orm/**/*.py
      - odoo/tools/view_validation.py
      - odoo/tools/template_inheritance.py
      - odoo/addons/base/rng/*.rng
      - odoo/addons/web/static/src/core/**
      - odoo/addons/web/static/src/views/**
    core_paths:
      - odoo/models/**/*.py
      - odoo/fields.py
      - odoo/api.py
      - odoo/orm/**/*.py
  enterprise:
    source: git@github.com:odoo/enterprise.git
    mirror: ~/.cache/ofd/enterprise.git
    branch: master
    framework_paths:
      - web_enterprise/static/src/**
      - web_studio/static/src/**

active_version: "20.0"

# Floor for commit enumeration on fresh workspaces. Without this, a
# `reindex` (which wipes state) walks the entire branch back to the
# first commit ever. Pick a date around when the version you care about
# was cut from master.
since_date: "2025-10-01"

key_devs: []

scoring:
  thresholds:
    surface: 3
    ledger_threshold: 4
    narrate: 5
  breadth_bonuses:
    - { min_rollouts: 5, bonus: 1 }
    - { min_rollouts: 20, bonus: 2 }
    - { min_rollouts: 50, bonus: 3 }
  dormant_days: 90
  fresh_days: 30
  intent_keywords: [introduce, "new api", replace]

narrate:
  backend: claude_code
  model: claude-sonnet-4-6
  default_status_filter: [fresh, active]
  min_rollouts: 0
"""
