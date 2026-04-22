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
.venv/bin/ofd init --workspace ~/ofd-workspace

# 2. Edit ~/ofd-workspace/config.yaml. Default template has odoo +
#    enterprise set up; tweak framework_paths / core_paths to taste.

# 3. Clone the mirrors (bare + --filter=blob:none, so they're tiny).
.venv/bin/ofd mirror clone

# 4. Initial backfill from your chosen cutoff (e.g. 19.0 branch-off).
ODOO_SINCE=$(git -C ~/Dev/src/odoo       merge-base 19.0 master)
ENT_SINCE=$( git -C ~/Dev/src/enterprise merge-base 19.0 master)
.venv/bin/ofd reindex \
  --since odoo=$ODOO_SINCE \
  --since enterprise=$ENT_SINCE

# 5. Render the ledger.
.venv/bin/ofd ledger update

# 6. Browse.
.venv/bin/ofd list --sort weighted --limit 20
.venv/bin/ofd show CachedModel
.venv/bin/ofd rollouts CachedModel --diff
```

Daily cadence once the backfill exists: `ofd run && ofd ledger update`.
`ofd run` fetches new commits, advances state, stamps each primitive
with the series `release.py` said master was tracking at the time.

## CLI

| Command                 | What it does                                                         |
|-------------------------|----------------------------------------------------------------------|
| `init`                  | Scaffold a workspace (config, raw/, ledger/).                        |
| `mirror clone`          | Bare + `--filter=blob:none` clone of each configured repo.           |
| `mirror fetch`          | Update mirrors.                                                      |
| `run`                   | Extract events, update watchlist, scan rollouts, score.              |
| `reindex`               | Re-run extractors over stored commits (wipes state, keeps manual pins). |
| `list`                  | Ledger entries in a color-coded table, filterable and sortable.      |
| `show SYMBOL`           | Render the ledger markdown for `SYMBOL` inline.                      |
| `commits SYMBOL`        | Definition + rollout commits with subjects, ready for `git show`.    |
| `rollouts SYMBOL`       | Rollout hunks; `--diff` adds syntax-highlighted before/after panels. |
| `query`                 | Filter raw events by kind / author / path / symbol / time.           |
| `watchlist add SYMBOL`  | Pin a magic string / context key (e.g. `formatted_display_name`).    |
| `watchlist remove SYMBOL` | Drop a watchlist entry.                                            |
| `watchlist list`        | Show watchlist entries; `--manual-only` filters to pins.             |
| `ledger update`         | Refresh `<!-- ofd:auto:* -->` sections in ledger files.              |
| `digest`                | Render the daily digest markdown (saved + pretty-printed).           |

`SYMBOL` accepts either the fully-qualified dotted name or the last
segment (`CachedModel`); the resolver prints candidates on ambiguity
and exits non-zero.

### Useful flags

- **`--since`** on `run` / `reindex`: bare SHA applies to every repo;
  `REPO=SHA` scopes to one. Repeatable, so mixed is fine. Unknown repo
  names exit non-zero.
- **`--sort`** on `list`:
  - `score` (default) - base score only
  - `weighted` - score + recency boost (favors late-cycle primitives)
  - `velocity` - rollouts per week since first_seen
  - `breadth` - raw rollout count
  - `date`, `symbol`
- **`--raw`** on `show` / `digest`: emit plain markdown instead of the
  rendered terminal output.
- **`--plain`** on `list` / `rollouts`: pipe-friendly text, no colors
  or tables.
- **`--no-progress`** on `run` / `reindex`: kill the spinner.

## Pipeline stages

Per commit on the tracked branch:

1. **Version detection.** If the commit touches `odoo/release.py`, parse
   the new `version_info` tuple and cache it on `RepoState`. All
   subsequent envelopes stamp `active_version` from the cache, so each
   primitive records the series it landed in (e.g. 19.2 vs 19.4).
2. **Extract** framework-path files with the Python / RNG extractors.
   Emits definition events (new class, new kwarg, signature change,
   deprecation, etc.).
3. **Watchlist update.** Every new definition adds its short name to
   the watchlist so later commits can be scanned for adoption.
4. **Rollout scan.** Every non-gated changed file's diff is scanned for
   watchlisted short names using context-aware regex patterns. Generic
   names (`join`, `default`, `ids`, ...) require an explicit import to
   count; ambiguous string matches don't.
5. **Score** each event against commit metadata (core path, tag, key
   devs, intent keywords). Aggregate = definition score + rollout
   breadth bonus + recency floor.

Repos run framework-first, adopter-last: repos with non-empty
`framework_paths` are promoted ahead of rollout-only repos (like
`enterprise` with `framework_paths: []`) so the watchlist is populated
before adopters scan their diffs.

## Workspace layout

```
<workspace>/
├── config.yaml               # repos, framework/core paths, scoring knobs
├── raw/<repo>/<sha>.json     # per-commit events (immutable log)
├── watchlist.json            # symbols tracked for rollout detection
├── ledger/
│   ├── new-apis/<symbol>.md
│   └── deprecations/<symbol>.md
└── digests/YYYY-MM-DD.md
```

State (last-seen SHA per repo, detected release series) lives outside
the workspace at `$XDG_DATA_HOME/ofd/state.json` - so multiple
workspaces can share one cursor, and a wipe of the workspace doesn't
replay all of history.

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

## Manual watchlist pins

Some primitives (context keys like `formatted_display_name`, registry
category names, magic strings) have no declarative "definition site"
the extractor can find. Pin them by hand:

```sh
ofd watchlist add formatted_display_name \
  --version 19.2 \
  --note "display_name compute context flag"
ofd reindex --watchlist-changed    # replay rollout detection with the pin
ofd rollouts formatted_display_name --diff
```

Manual pins carry `source: "manual"` on disk and survive a regular
`ofd reindex` (which otherwise wipes the watchlist). The rollout
matcher doesn't care how a name got into the watchlist.

## Tests

```sh
.venv/bin/pytest
```

156 tests cover the extractors, pipeline stages, rollout matcher,
release-version detection, ledger reader/writer, scoring, CLI
resolver/`--since` parser, watchlist persistence, and end-to-end flows
against a disposable git fixture.
