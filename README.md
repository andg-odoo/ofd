# ofd - Odoo Framework Digest

A pipeline that watches Odoo master, extracts framework-layer primitives
(new classes, decorators, view attributes, deprecations, new kwargs),
tracks their rollouts across the codebase, and keeps a markdown ledger
of slide-draft entries for the annual **"What's new in Odoo"** engineering
talk.

See [DESIGN.md](DESIGN.md) for the full architectural spec.

## Install

```sh
git clone <this repo>
cd ofd
python3 -m venv .venv
.venv/bin/pip install -e .[dev]
```

## Quickstart

```sh
# 1. Scaffold a workspace (creates config.yaml + empty raw/, ledger/).
.venv/bin/ofd init ~/ofd-workspace

# 2. Edit ~/ofd-workspace/config.yaml to point at your git mirror(s):
#    repos:
#      odoo:
#        source: /path/to/odoo.git
#        mirror: /path/to/mirror.git       # bare partial clone
#        branch: master
#        framework_paths: [odoo/orm/**/*.py, odoo/fields.py, ...]
#        core_paths:      [odoo/orm/**/*.py]

# 3. Clone the mirror (bare + --filter=blob:none, so it's tiny).
.venv/bin/ofd mirror clone

# 4. Run ingestion. Repeatable; advances last_seen_sha on success.
.venv/bin/ofd run

# 5. Browse.
.venv/bin/ofd list
.venv/bin/ofd show CachedModel
.venv/bin/ofd commits CachedModel
.venv/bin/ofd rollouts CachedModel --diff
```

## CLI

| Command            | What it does                                                    |
|--------------------|-----------------------------------------------------------------|
| `init`             | Scaffold a workspace (config, raw/, ledger/).                   |
| `mirror clone`     | Bare + `--filter=blob:none` clone of each configured repo.      |
| `mirror fetch`     | Update mirrors.                                                 |
| `run`              | Extract events, update watchlist, scan rollouts, score.         |
| `list`             | One line per ledger entry, sortable.                            |
| `show SYMBOL`      | Print the ledger markdown for `SYMBOL`.                         |
| `commits SYMBOL`   | Definition + rollout commits with subjects, ready for `git show`. |
| `rollouts SYMBOL`  | Rollout hunks; `--diff` to include before/after.                |
| `query`            | Filter raw events by kind / author / path / symbol / time.      |
| `reindex`          | Re-run extractors over stored commits (no git re-fetch).        |
| `ledger update`    | Refresh `<!-- ofd:auto:* -->` sections in ledger files.         |
| `digest`           | Render the daily digest markdown.                               |

`SYMBOL` accepts either the fully-qualified dotted name or the last
segment (`CachedModel`); the resolver prints candidates on ambiguity.

## Pipeline stages

Per commit on tracked branch:

1. **Extract** framework-path files with the Python / RNG (view-schema)
   extractors. Emits primitive events (new class, new kwarg, signature
   change, deprecation, etc.).
2. **Watchlist update** - every new definition gets a short name added
   to the watchlist so later commits can be scanned for adoption.
3. **Rollout scan** - every non-gated changed file's diff is scanned
   for watchlisted short names using context-aware regex patterns.
   Generic names (`join`, `default`, `ids`, ...) require an explicit
   import to count; ambiguous string matches don't.
4. **Score** each event against commit metadata (core path, tag, key
   devs, intent keywords, test coverage). Aggregate score = definition
   score + rollout breadth bonus + recency floor.

## Workspace layout

```
<workspace>/
├── config.yaml               # repos, framework/core paths, scoring knobs
├── raw/<repo>/<sha>.json     # per-commit events (immutable log)
├── watchlist.json            # symbols tracked for rollout detection
├── ledger/
│   ├── new-apis/<symbol>.md  # one markdown file per primitive
│   └── deprecations/<symbol>.md
└── digests/YYYY-MM-DD.md     # generated daily digests
```

State (last-seen SHA per repo) lives outside the workspace, at
`$XDG_DATA_HOME/ofd/state.json` - so multiple workspaces can share
one cursor, and a wipe of the workspace doesn't replay all of history.

## Event kinds

Python extractor: `new_public_class`, `new_decorator_or_helper`,
`new_kwarg`, `new_class_attribute`, `signature_change`,
`deprecation_warning_added`, `removed_public_symbol`.

View-schema (RNG) extractor: `new_view_attribute`, `new_view_element`,
`new_view_type`, `new_view_directive`, `removed_view_attribute`.

Rollout detector: `rollout` (one per hit per hunk; carries before/after
snippets for slide content).

## Rollout matcher - what counts, what doesn't

For a watchlisted short name, a rollout is recorded when the name
appears in an *added* diff line in a syntactic position that implies
use, not mention:

- attribute access `.name`, call `name(`, kwarg `name=`
- `import name` / `from … import name`
- `class name` / `def name` / `@name`
- type annotation (preceded by a word/paren/bracket, to kill
  `# foo: Bar` comment noise)
- exact-content quoted string: `'name'`, `"name"` - catches dict keys,
  XML `<field name="name"/>`, `@api.depends_context('name')`, etc.

Comments are stripped before matching. Strings that merely *contain*
the name (`"see name here"`) don't match.

Names in `_GENERIC_SHORT_NAMES` (`join`, `default`, `ids`, `query`,
`cache`, dunders, ...) only count when imported explicitly - they'd
otherwise collide with every `.join()` / `.ids` / `.get()` in the
codebase.

## Tests

```sh
.venv/bin/pytest
```

143 tests cover the extractors, pipeline stages, rollout matcher,
ledger reader/writer, scoring, CLI resolver, and end-to-end flows
against a disposable git fixture.
