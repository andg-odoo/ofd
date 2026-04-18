# `ofd` - Odoo Framework Digest

A daily pipeline that reads git commits on master for Odoo community and
enterprise, detects framework-layer primitives (new classes, decorators,
view attributes, view types, deprecations), tracks their rollouts across
the codebase, and maintains a markdown ledger of slide-draft entries for
the annual "What's new in Odoo" engineering talk.

---

## 1. Purpose

- Surface framework-layer changes on master as they land, without the
  noise of addon-level business code.
- Pair each new primitive with concrete before/after code pulled from
  the rollout commits that adopt it.
- Keep a long-lived, hand-editable markdown ledger that, by talk prep
  time, is roughly 80% of each slide already written.
- Be runnable daily by hand; safe to systemd-ify later.

## 2. Non-goals

- Functional/marketing "what's new" content. This is for an engineering
  audience.
- UI for browsing data. CLI and markdown files are the interface;
  optional static frontend is a v2 consideration.
- Querying arbitrary Odoo semantics (inheritance chains, field graphs).
  A structural diff of gated framework paths is the scope.
- Live API or database. Everything is files on disk.

## 3. High-level pipeline

```
[ fetch ]    git fetch on bare partial mirrors (parallel across repos)
[ enumerate ] git log <branch> --since=<last_sha> --no-merges -- <gated_paths>
[ extract ]  per commit, per file: dispatch to handler by extension
[ watchlist] new definition events append to the watchlist mid-commit
[ rollouts ] scan same commit's non-gated diffs for watchlist symbols
[ score ]    apply per-event rubric, store on record
[ persist ]  write raw/<repo>/<sha>.json atomically
[ ledger ]   regenerate marked sections in per-primitive markdown
[ digest ]   render today's daily digest
[ state ]    advance last_seen_sha per repo
```

Narration (LLM) is a separate command, not part of `ofd run`.

## 4. Data model

### 4.1 Raw event file

One JSON per commit with any detected changes:
`<workspace>/raw/<repo>/<sha>.json`. Commits with no detected changes
are skipped entirely.

```json
{
  "schema_version": 1,
  "commit": {
    "sha": "abc123def456",
    "repo": "odoo",
    "branch": "master",
    "active_version": "20.0",
    "author_name": "John Doe",
    "author_email": "jdoe@odoo.com",
    "committed_at": "2026-04-17T10:32:15Z",
    "subject": "[ADD] base: introduce models.Constraint class",
    "body": "..."
  },
  "changes": [
    { "kind": "...", "...": "..." }
  ]
}
```

### 4.2 Change record - common fields

| Field | Type | Notes |
|---|---|---|
| `kind` | enum (see 4.3) | |
| `file` | string | path relative to repo root |
| `line` | int | starting line in child-tree version |
| `score` | int, 0-5 | pre-computed |
| `score_reasons` | `[string]` | rubric hits, for debugging |

### 4.3 Kinds

| Kind | Extra fields | Language |
|---|---|---|
| `new_public_class` | `symbol`, `signature`, `after_snippet` | Python |
| `new_decorator_or_helper` | `symbol`, `signature`, `after_snippet` | Python |
| `new_class_attribute` | `symbol` (`Class.attr`), `after_snippet` | Python |
| `signature_change` | `symbol`, `before_signature`, `after_signature` | Python |
| `deprecation_warning_added` | `symbol_hint`, `warning_text`, `removal_version` | Python |
| `removed_public_symbol` | `symbol`, `before_snippet` | Python |
| `new_endpoint` | `symbol`, `route`, `after_snippet` | Python |
| `new_view_attribute` | `attribute`, `element`, `rng_file` | RNG |
| `new_view_element` | `element`, `rng_file` | RNG |
| `new_view_type` | `type_name`, `registry` | JS |
| `new_view_directive` | `directive`, `rng_file` | RNG |
| `removed_view_attribute` | `attribute`, `element`, `rng_file` | RNG |
| `rollout` | `symbol`, `model` or `xml_path`, `before_snippet`, `after_snippet`, `hunk_header` | any |

Snippets are capped at ~30 lines per side. Longer hunks are truncated
with a `# ... <N lines elided> ...` marker.

Fully-qualified symbol names (`odoo.models.Constraint`) are used
throughout so watchlist matching is unambiguous.

## 5. Extractors

One handler per file type, dispatched by extension. Each emits the same
change-record schema.

### 5.1 Python (`extractors/python_.py`)

- Uses stdlib `ast`.
- For each changed `.py` file in a gated path:
  1. `git show <commit>^:<path>` and `git show <commit>:<path>`.
  2. Parse both; walk top level and one level into classes.
  3. Build `{fully_qualified_name: signature_hash}` for each side.
  4. Set-diff → `added`, `removed`, `signature_changed`.
- Regex pass over added lines for `warnings.warn(..., DeprecationWarning)`
  and similar → `deprecation_warning_added`.

### 5.2 XML / RNG (`extractors/xml_.py`, `extractors/rng.py`)

- Uses `lxml.etree`.
- RNG: for each `<rng:define>`, compute a summary of
  (attributes, refs, inline elements, group/choice shapes) and diff.
  Catches:
  - `new_view_attribute` / `removed_view_attribute` - attribute added or
    removed in an element definition.
  - `new_view_element` - a brand-new top-level define.
  - `new_view_directive` - new child ref, inline element, or net-new
    `<rng:group>` / `<rng:choice>` shape. The group-shape path catches
    pure restructuring (e.g. PR 241459 reorganizing `<filter>` into
    choice-of-groups without changing its attribute set).
- Validated against PR 241459 (subfilter) and PR 257101 (filter `<field>`
  child) from master: both produce `new_view_directive` events on the
  `filter` define.
- XML (framework view fixtures): diff `<record>` elements with
  `type="<view_type>"` attributes. (Deferred.)

### 5.3 JavaScript (`extractors/javascript.py`)

- Shells out to `ast-grep` with codified rules in `rules/ast_grep/*.yml`.
- Starter rules:
  - `new_view_type.yml` - matches `registry.category("views").add($NAME, ...)`
  - `new_registry_category.yml` - matches `registry.category($NAME)`
  - `new_owl_component.yml` - matches `export class $NAME extends Component`

Each rule emits JSON; extractor parses and converts to change records.
Rules are checked into the repo so behavior is deterministic and
reviewable.

### 5.4 Gated paths (initial set)

Configurable per repo in `config.yaml`. Starting values:

**Community (`odoo`):**
- `odoo/models/**/*.py`
- `odoo/fields.py`
- `odoo/api.py`
- `odoo/osv/**/*.py`
- `odoo/tools/view_validation.py`
- `odoo/tools/template_inheritance.py`
- `odoo/addons/base/rng/*.rng`
- `odoo/addons/base/data/**/*.xml`
- `odoo/addons/web/static/src/core/**`
- `odoo/addons/web/static/src/views/**`

**Enterprise:**
- `web_enterprise/static/src/**`
- `web_studio/static/src/**`

**Core subset (bonus +1 score modifier):**
- `odoo/models/**/*.py`
- `odoo/fields.py`
- `odoo/api.py`
- `odoo/orm/**/*.py` (if present)

## 6. Watchlist

Auto-populated. Any `new_public_class`, `new_decorator_or_helper`,
`new_endpoint`, or `new_view_type` in a gated framework path enters the
watchlist at ingest time.

Within a single commit, the watchlist is updated before the rollout
pass runs - so a commit that defines a primitive *and* uses it in
another file in the same commit gets both events emitted.

`ofd watchlist remove <symbol>` prunes false positives. The watchlist
persists to `<workspace>/watchlist.json`.

Retroactive scans triggered by:
- Manual watchlist edit
- Extractor rule changes
- Gated-path additions

Handled by `ofd reindex [--watchlist-changed] [--since=<ref>]`.

## 7. Scoring

### 7.1 Per-event base scores

| Kind | Base |
|---|---|
| `new_public_class` | 3 |
| `new_endpoint` | 3 |
| `new_view_type` | 3 |
| `new_view_attribute` | 3 |
| `new_view_element` | 3 |
| `deprecation_warning_added` | 3 |
| `removed_public_symbol` | 3 |
| `removed_view_attribute` | 3 |
| `new_decorator_or_helper` | 2 |
| `new_view_directive` | 2 |
| `signature_change` | 1 |
| `new_class_attribute` | 1 |
| `rollout` | 0 (aggregate matters) |

### 7.2 Modifiers

| Modifier | Delta | When |
|---|---|---|
| Core path | +1 | File in `core_paths` list |
| Commit tag `[ADD]` | +1 | Subject starts with `[ADD]` |
| Commit tag `[FIX]` | −1 | Subject starts with `[FIX]` |
| Commit tag `[REV]` | −2 | Subject starts with `[REV]` |
| Key-dev author | +1 | Author email in `key_devs` |
| Symbol named in message | +1 | Subject or body contains symbol |
| Intent keyword | +1 | Strict list: introduce, new API, replace |
| Tests-only path | −1 | Path under `tests/` |

Formula: `score = clamp(base + sum(modifiers), 0, 5)`.

Every event stores both `score` and `score_reasons` for auditability.

### 7.3 Aggregate score per primitive

At ledger update:

```
ledger_score = definition_score + breadth_bonus
```

Breadth bonus table:

| Rollout count | Bonus |
|---|---|
| < 5 | 0 |
| 5-19 | 1 |
| 20-49 | 2 |
| ≥ 50 | 3 |

Recency floor: if the primitive's `first_seen` is < 30 days ago, the
breadth bonus has a floor of 1. Prevents penalizing fresh primitives
for having few rollouts yet.

Clamped to 5.

### 7.4 Thresholds

| Score | Behavior |
|---|---|
| ≥ 3 | Appears in daily digest highlights |
| ≥ 4 | Primitives get dedicated ledger entries (but every framework-path definition gets an entry regardless) |
| ≥ 5 | Eligible for `ofd ledger narrate` |
| < 3 | Stored in raw, queryable, not surfaced |

### 7.5 Tie-breaking

`ofd list --sort=score` ties broken by:
1. Kind priority (new_public_class > deprecation > new_endpoint > new_decorator > removed_public_symbol > signature_change > new_class_attribute)
2. Committed date (newer first)
3. Commit SHA (deterministic)

### 7.6 Status transitions

Computed on every ledger update:

| Status | Condition |
|---|---|
| `fresh` | `first_seen` < 30 days, any rollout count |
| `active` | ≥ 5 rollouts in last 90 days |
| `awaiting-adoption` | < 5 rollouts, < 90 days old |
| `dormant` | < 5 rollouts, > 90 days old |
| `reverted` | definition commit reverted |

## 8. Ledger format

One markdown file per primitive:

- `<workspace>/ledger/new-apis/<symbol>.md`
- `<workspace>/ledger/deprecations/<symbol>.md`

### 8.1 Layout

```markdown
---
symbol: odoo.models.Constraint
kind: new_public_class
active_version: "20.0"
status: active
score: 5
rollout_count: 47
first_seen: 2026-01-15
last_updated: 2026-04-17
pinned: false
pin_reason: null
---

# models.Constraint

<!-- ofd:auto:summary -->
Introduced in `odoo/models/constraint.py` by John Doe on 2026-01-15.
Replaces: `_sql_constraints`.
Status: active - 47 rollouts across 23 addons.
<!-- /ofd:auto:summary -->

<!-- ofd:narrative -->
## Why this matters
[LLM-generated paragraph; preserved after user edits]
<!-- /ofd:narrative -->

<!-- ofd:auto:before_after -->
## Before / After
...
<!-- /ofd:auto:before_after -->

<!-- ofd:auto:commits -->
## Commits
...
<!-- /ofd:auto:commits -->

<!-- ofd:auto:adoption -->
## Adoption
...
<!-- /ofd:auto:adoption -->

## Notes
[Freeform user area - never touched]
```

### 8.2 Regeneration rules

| Region | Machine owns? | On update |
|---|---|---|
| Frontmatter | Yes | Overwritten from raw events |
| `<!-- ofd:auto:* -->` blocks | Yes | Overwritten |
| `<!-- ofd:narrative -->` | Shared | LLM fills if empty; preserved if non-empty unless `--force-narrative` |
| Outside markers (incl. `## Notes`) | No | Never touched |

Regeneration: parse file → split on marker lines → replace marked
regions → concat → atomic write (tempfile + rename).

### 8.3 Canonical before/after selection

For `ofd:auto:before_after`:
- Pick oldest rollout commit authored by a key-dev.
- Fall back to oldest rollout commit overall.

Rationale: the person introducing the primitive usually writes the
cleanest first migration.

Alternate examples are browseable via `ofd rollouts <symbol> --diff`.

### 8.4 `Replaces:` derivation

Deterministic. Scan all rollout events for the primitive; extract the
common leading pattern of each `-` side (e.g., `_sql_constraints`,
`_auto_init`). If a single pattern dominates, use it. No LLM.

### 8.5 Deprecation entries

Same layout with:
- `before_after` swapped for `migration` (old → new usage)
- Frontmatter gains `removal_version`

## 9. CLI

Binary: `ofd`.

### 9.1 Pipeline

| Command | Purpose |
|---|---|
| `ofd run [--since=...] [--quiet] [--dry-run]` | ingest + ledger update; no LLM |
| `ofd ingest [--since=...] [--force]` | extractor pass only |
| `ofd ledger update [--symbol=X]` | regenerate auto sections |
| `ofd ledger narrate [--symbol=X] [--status=...] [--min-rollouts=N] [--force] [--dry-run]` | LLM pass, manual cadence |
| `ofd reindex [--watchlist-changed] [--since=...]` | retroactive re-extraction |

### 9.2 Reading

| Command | Purpose |
|---|---|
| `ofd digest [--date=YYYY-MM-DD]` | today's digest markdown |
| `ofd show <symbol>` | print ledger entry |
| `ofd list [--kind=...] [--status=...] [--version=...] [--sort=score\|breadth\|date]` | indexed list |
| `ofd query [--author=X] [--kind=...] [--since=7d] [--path=...] [--json]` | ad-hoc filter |
| `ofd commits <symbol> [--kind=definition\|rollout]` | commit SHAs + subjects |
| `ofd rollouts <symbol> [--limit=5] [--diff]` | rollout hunks |

### 9.3 Config / state

| Command | Purpose |
|---|---|
| `ofd config show` | show config |
| `ofd config add-path <path>` / `add-dev <email>` / `set-version <ver>` | edits config.yaml |
| `ofd watchlist` | show watchlist |
| `ofd watchlist remove <symbol>` | prune |

### 9.4 Mirrors

| Command | Purpose |
|---|---|
| `ofd mirror init` | first-time bare partial clones |
| `ofd mirror fetch` | explicit fetch |
| `ofd mirror status` | disk, last-fetch, latest SHA |
| `ofd mirror reset` | nuke + re-clone |

### 9.5 Bootstrap

| Command | Purpose |
|---|---|
| `ofd init [--workspace=<path>]` | create workspace skeleton, write default config, initialize git repo inside it |

### 9.6 Terminal curation

Before a frontend, the primary curation UX is:

```bash
ofd list --sort=score | fzf --preview 'ofd show {}' \
  --bind 'enter:execute($EDITOR ledger/new-apis/{}.md)'
```

## 10. Execution (`ofd run`)

### 10.1 Concurrency

- Across repos: parallel (`ThreadPoolExecutor(max_workers=len(repos))`).
- Within a repo: sequential commit processing (deterministic watchlist
  propagation).
- Across handlers within a commit: sequential.

### 10.2 Per-commit sequence

1. Scan gated-path files → detect definitions → append to watchlist.
2. Scan *all* changed files in that commit → match against updated
   watchlist → emit rollout records.
3. Score every change record.
4. If non-empty: write `raw/<repo>/<sha>.json` atomically.
5. Advance `state.last_seen_sha` only on full success.

### 10.3 Failure semantics

| Stage | On failure |
|---|---|
| Fetch | Retry once; skip repo on second failure |
| Extract (per commit) | Log, skip commit, do not advance `last_seen_sha` past the failure |
| Raw write | Atomic (tempfile + rename) |
| Ledger section update | Atomic per file |
| Narrate (LLM) | Log, leave block empty, retry on next `narrate` invocation |

Result: `ofd run` is resumable. Re-running after a crash picks up from
the last successful commit.

### 10.4 Output

- Default: one line per stage summary on stdout.
- `--quiet`: errors-only to stderr.
- Exit code is meaningful: non-zero if any stage failed.

## 11. Narration

Separate from `ofd run`. Manual cadence (weekly/monthly).

### 11.1 Backends

Abstracted in `narrate/client.py`:

```python
class NarrateBackend(Protocol):
    def narrate(self, system: str, user: str) -> str: ...

class ClaudeCodeCLIBackend:  # default
    # subprocess: claude -p --output-format json
    ...

class AnthropicBackend:  # opt-in
    # requires ANTHROPIC_API_KEY; enables prompt caching
    ...
```

Selected via `narrate.backend` in config. Default `claude_code` -
zero auth setup, covered by Max plan. Swap to `anthropic` if parallel
calls or tighter caching are needed later.

### 11.2 Input to the LLM

Per primitive:
- Symbol, kind, active_version
- Definition commit: SHA, author, subject, body
- 2-3 rollout hunks (before_snippet, after_snippet, file, commit SHA)

Prompt stored in `narrate/prompts.py`, versioned with the code.

### 11.3 Default eligibility

- Narrative block is empty
- `status ∈ {fresh, active}`
- `rollout_count ≥ config.narrate.min_rollouts` (default 0)

### 11.4 `--force`

Regenerates in place. Previous narrative is gone. Preserve good lines
by moving them to `## Notes` before running `--force`.

## 12. Storage layout

| Path | Contents | Managed by |
|---|---|---|
| `~/.cache/ofd/` | `odoo.git/`, `enterprise.git/` (bare partial) | `ofd mirror *` |
| `~/.local/share/ofd/` | `state.json` | `ofd run` |
| `<workspace>` (default `~/ofd-workspace/`) | `config.yaml`, `raw/`, `ledger/`, `digests/`, `watchlist.json` | user + `ofd` |

Workspace path located via (in order):
1. `--workspace` CLI flag
2. `OFD_WORKSPACE` env var
3. `~/.config/ofd/workspace` pointer file
4. Default: `~/ofd-workspace/`

The workspace is intended to be `git init`'d separately - the ledger is
a deliverable worth version-controlling.

### 12.1 Workspace tree

```
<workspace>/
├── config.yaml
├── watchlist.json
├── raw/
│   ├── odoo/
│   │   ├── abc123def.json
│   │   └── ...
│   └── enterprise/
├── ledger/
│   ├── new-apis/
│   │   └── models.Constraint.md
│   ├── deprecations/
│   └── refactors/
└── digests/
    └── 2026-04-17.md
```

## 13. Project layout

```
ofd/
├── pyproject.toml
├── README.md
├── DESIGN.md
├── src/
│   └── ofd/
│       ├── __init__.py
│       ├── __main__.py
│       ├── cli/
│       │   ├── main.py
│       │   ├── run.py
│       │   ├── ingest.py
│       │   ├── ledger_cmd.py
│       │   ├── show.py
│       │   ├── list_cmd.py
│       │   ├── query.py
│       │   ├── commits.py
│       │   ├── rollouts.py
│       │   ├── config_cmd.py
│       │   ├── watchlist.py
│       │   ├── mirror.py
│       │   ├── digest.py
│       │   ├── reindex.py
│       │   └── init_cmd.py
│       ├── config.py
│       ├── state.py
│       ├── gitio.py
│       ├── mirrors.py
│       ├── extractors/
│       │   ├── dispatcher.py
│       │   ├── python_.py
│       │   ├── xml_.py
│       │   ├── rng.py
│       │   └── javascript.py
│       ├── events/
│       │   ├── record.py
│       │   ├── store.py
│       │   └── schema.py
│       ├── watchlist.py
│       ├── rollouts.py
│       ├── scoring.py
│       ├── ledger/
│       │   ├── format.py
│       │   ├── frontmatter.py
│       │   ├── render.py
│       │   └── status.py
│       ├── narrate/
│       │   ├── client.py
│       │   └── prompts.py
│       ├── digest.py
│       └── pipeline.py
├── rules/
│   └── ast_grep/
│       ├── new_view_type.yml
│       ├── new_registry_category.yml
│       └── new_owl_component.yml
└── tests/
    ├── fixtures/
    │   ├── py_snippets/
    │   ├── xml_snippets/
    │   └── commits/
    ├── test_extractors_python.py
    ├── test_extractors_xml.py
    ├── test_extractors_rng.py
    ├── test_scoring.py
    ├── test_ledger_format.py
    ├── test_rollouts.py
    └── test_pipeline.py
```

### 13.1 Module dependency flow

```
cli/*      → pipeline, config, state, events/store, ledger/*
pipeline   → mirrors, gitio, extractors/*, watchlist, rollouts,
             scoring, events/*, ledger/*, narrate/*
extractors → gitio, events/record
ledger/*   → events/store, scoring
narrate/*  → ledger/* (read), backend
events/record ← everything (shared types)
```

Strict: `extractors/` never imports `ledger/`; `ledger/` never imports
`extractors/`. The pipeline is the only orchestrator.

## 14. Config schema

`<workspace>/config.yaml`:

```yaml
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
      - odoo/tools/view_validation.py
      - odoo/tools/template_inheritance.py
      - odoo/addons/base/rng/*.rng
      - odoo/addons/web/static/src/core/**
      - odoo/addons/web/static/src/views/**
    core_paths:
      - odoo/models/**/*.py
      - odoo/fields.py
      - odoo/api.py
  enterprise:
    source: git@github.com:odoo/enterprise.git
    mirror: ~/.cache/ofd/enterprise.git
    branch: master
    framework_paths:
      - web_enterprise/static/src/**
      - web_studio/static/src/**
    core_paths: []

active_version: "20.0"

key_devs:
  - jdoe@odoo.com

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

digest:
  sections:
    - new_primitives
    - adoption_velocity
    - deprecations
```

## 15. Dependencies

### 15.1 Python

- `click >= 8.1` - CLI
- `lxml >= 5.0` - XML/RNG parsing
- `pyyaml >= 6.0` - config

Optional for `AnthropicBackend`:
- `anthropic >= 0.40`

### 15.2 External

- `ast-grep` - for JS extraction. Install via distro or
  `cargo install ast-grep`. Checked at startup; fail gracefully if
  missing. Only required when JS extractor runs.
- `git` - assumed present.
- `claude` - Claude Code CLI, only required for default narrate
  backend.

## 16. Testing strategy

- **Handler tests** - fixtures of before/after snippets as strings.
  Each handler tested in isolation.
- **Scoring tests** - pure function tests, trivial to exhaustively
  cover the rubric.
- **Ledger format tests** - parse a file, regenerate, verify that user
  sections are preserved and auto sections are refreshed.
- **Pipeline tests** - tiny fake git repos built in `tests/fixtures/commits/`,
  run `pipeline.run()` against them, assert raw events and ledger
  state.
- **Narrate tests** - mock the backend; verify prompt shape and output
  insertion.

## 17. Open questions / v2 ideas

- **OWL component framework tracking** - currently skipped; would need
  a proper JS AST. ast-grep rules catch registrations, not prop/hook
  changes.
- **Cross-commit narrative** - a primitive introduced Monday then
  polished across four commits by Friday. Currently each commit is
  seen in isolation; the ledger merges them but narrative input uses
  the earliest definition commit.
- **Move/rename detection** - AST-diff sees a moved class as
  remove+add. V1 emits both; stage-3 dedupes heuristically. V2 could
  add a pre-pass that hashes class bodies.
- **`odoo-ls` integration** - for disambiguating symbol references in
  ambiguous rollout matches. Not needed until false-positive rollouts
  become noisy.
- **Static frontend** - mkdocs/Astro over the ledger directory.
  Signals to reach for: frequently diffing rollout hunks, wanting
  shortlist UI, charting adoption curves.
- **Parser-grammar extensions** - domain token additions (`'today'`,
  `'=5d'`, `'=monday'`) live in string constant tables, not AST
  symbols. Separate detector needed: diff membership of
  constant-table dicts/sets in gated files.
- **Versioning of raw events** - `schema_version: 1` now; migration
  path for future schema changes TBD.
