# JS extractor - design spec

Status: phases 1 and 2 implemented (definitions + import-anchored
adoption matching); phase 3 (QWeb needles, breadth calibration) is
spec only. Companion to [DESIGN.md](DESIGN.md); assumes its vocabulary
(kinds, watchlist, rollout matcher, baselines, bench harness).

## Goal

Make the two `web/static/src/**` entries in `framework_paths` real:
extract JS framework primitives (services, hooks, helpers, registry
entries) and track their adoption across odoo + enterprise. Enterprise
alone had ~2,300 JS commits in the current window - the largest
untapped rollout surface in the tool.

## Decisions (locked 2026-06-04)

1. **Both anchors ship in the first pass** - export-diff and registry -
   they share the parse.
2. **Registry scan is wide-scope from day one** (all addons, both
   repos), needle-gated like `depends_context`.
3. **`web/static/src/core/**` joins `core_paths`** (the +1 scoring
   modifier), mirroring `odoo/orm/**`.
4. **JS primitives get their own kinds.** `_KIND_LANGUAGES` is keyed by
   kind, and kind implicitly encodes *definition language*. Letting
   `NEW_PUBLIC_CLASS` match in JS scope is exactly how the historical
   `PropertiesDefinition.setup` / `Transaction.cache` false-positive
   columns happened. Python kinds never match in JS scope; JS kinds
   never match in Python scope.
5. **JS rollouts match on import lines only** (plus registry-add for
   registry entries). We accept missing dynamic/indirect uses in
   exchange for near-zero false positives. Content matching in JS is
   what failed before; import matching is a different mechanism.

## Anchors

### A. Export diff (framework paths only)

The JS mirror of `python_.py`: parent->child diff of `export class` /
`export function` / `export const` in gated files.

- `NEW_JS_EXPORT` (base 2; consider 3 for `export class`)
- `REMOVED_JS_EXPORT` (base 3 - removals are deprecation stories)
- Hook convention: an exported `function use[A-Z]...` is still
  `NEW_JS_EXPORT`, but stamp `symbol_hint="hook"` for ledger display.

ast-grep (`ast_grep_py`, language `"javascript"`) parses this fine -
verified against the vendored version. Reuse the `SgRoot` +
rule-dict pattern from `context_keys.py`.

### B. Registry (wide-scope, both repos)

`registry.category("services").add("orm", ...)` is the JS analog of
`@api.depends_context(...)`: a typed-string registry where the
framework itself certifies the string as meaningful. No heuristics
needed to decide if the string matters.

- New category string seen in a framework path -> `NEW_REGISTRY_CATEGORY`
  (base 3; a new registry category is a new extension point).
- `.add("name", ...)` in a *framework* path -> `NEW_REGISTRY_ENTRY`
  (base 2; e.g. a new core service).
- `.add("name", ...)` in a *non-framework* path for a watchlisted
  category -> ROLLOUT of the category (adoption of the extension
  point). This is emitted by the extractor itself, like
  `file_conventions` does - the content matcher is not involved.

Pipeline shape: needle-gate on `registry.category` in the patch text
(stage 1.5 pattern), run over all `.js` files in all repos.

Baseline: categories and framework entries existing at the
`since_date` floor are pre-known. Reuse `_load_or_build_baseline`
with a scan that greps the floor tree for `registry.category(` -
literal-string args only, same skip-silently rule as context keys
for computed names.

## Symbol naming

Use the asset alias - it is what adopting code actually writes:

    addons/web/static/src/core/utils/hooks.js
      -> module  @web/core/utils/hooks
      -> symbol  @web/core/utils/hooks.useService
      -> short   useService

Registry symbols: `registry.services` (category),
`registry.services.orm` (entry); short = last segment.

The path->alias transform is `addons/<addon>/static/src/<rest>.js ->
@<addon>/<rest>` (same for enterprise addons).

## Rollout matching (phase 2)

- `Language.JS` added to `rollouts._FILE_LANGUAGES` for `.js`.
- `_KIND_LANGUAGES`: JS kinds -> `{Language.JS}`. All existing kinds
  unchanged (no Python/View primitive ever matches in JS).
- The JS matcher recognizes exactly two positions:
  1. `import { NAME } from "..."` / `import NAME from "..."` on an
     added diff line. ~~Bonus precision: when the entry's module alias
     is known, require the from-string to match it.~~ **Disproved by
     Phase 1 data (2026-06-04):** `localeCompare` is defined in
     `@web/core/l10n/utils/collation` but all real adopters (42/42 on
     the bench corpus) import it from the `@web/core/l10n/utils`
     barrel re-export. A strict from-string match counts zero of them.
     **Implemented rule (`_js_from_plausible`):** the from-string must
     be the defining module, an *ancestor barrel* on its path, or a
     relative path. Pure name-anchoring was also disproved by the
     bench: `formatDuration` is exported independently by both
     `@web/views/fields/formatters` (watchlisted) and the pre-floor
     `@web/core/l10n/dates`, and hoot shadows framework helper names
     in test files (`waitUntil`: 11 of 13 raw hits were hoot's) - JS
     test files (`static/tests/`, `*.test.js`) are skipped wholesale.
  2. `registry.category("CAT").add(` for category entries (extractor-
     emitted, see above; not the content matcher).
- JS generic-short-name blocklist, mirroring `_GENERIC_SHORT_NAMES`:
  OWL lifecycle + ubiquitous component vocabulary (`setup`, `props`,
  `state`, `env`, `render`, `mounted`, `onMounted`, `onWillStart`,
  `Component`, `useState`, ...). With import-only anchoring this list
  is belt-and-suspenders, but the historical failures earn it.

## OWL 3 handling

Finding (2026-06-04): `odoo/upgrade_code/owl3-migration.py` is almost
entirely *template-expression* rewriting - etree transforms, JS
expression recompilation, excluded-template lists. It is not a symbol
rename map. Consequence: **the chosen anchors are orthogonal to the
migration churn.** Per-module conversion commits touch template
syntax, not export names or registry entries, so they fire (almost)
nothing. The mid-migration noise problem largely dissolves for this
anchor set.

What remains, and how the refactor still gets marked:

1. **Epoch event.** Extend the `release_detect.py` pattern to sniff
   the vendored lib version in `addons/web/static/lib/owl/owl.js`
   (8 commits in the current window - those are the epoch moments).
   A major-version change emits a high-score event (`NEW_JS_EXPORT`
   on symbol `@odoo/owl` or a dedicated `VENDORED_LIB_BUMP` kind,
   decide at implementation) so "OWL 3 landed" is one loud ledger
   entry instead of scattered noise. The migration tooling itself
   (`owl3-migration.py`, `tools_js_expressions.py`) is already
   surfaced by the broadened `upgrade_code/**` paths.
2. **Rename folding (cheap insurance). DELETED in phase 2:** it never
   fired across the full 19,774-commit reindex, triggering this
   paragraph's own delete rule. A rename is now an honest
   `REMOVED_JS_EXPORT` + `NEW_JS_EXPORT` pair.
3. **No special JS floor.** The standard `since_date` baseline
   applies. If backfill still proves noisy around the merge window,
   fall back to a `js_since_date` per-repo knob - but only with bench
   evidence, not preemptively.

## Touch points (implementation checklist)

- `extractors/js_.py` (new): export diff + registry definitions +
  registry rollouts + lib-version sniff.
- `extractors/dispatcher.py`: route `.js` (odoo has no `.ts`).
- `events/record.py`: `NEW_JS_EXPORT`, `REMOVED_JS_EXPORT`,
  `NEW_REGISTRY_CATEGORY`, `NEW_REGISTRY_ENTRY` (+ DEFINITION_KINDS).
- `scoring.py`: `_BASE` + `_KIND_PRIORITY` rows.
- `rollouts.py`: `Language.JS`, `_FILE_LANGUAGES` `.js` entry,
  `_KIND_LANGUAGES` rows, import-anchored matcher, JS generic list.
- `watchlist.py`: short-name rule for registry symbols (last segment
  is fine; no special case expected).
- `pipeline.py`: wide-scope stage for registry (1.5 pattern) +
  registry baseline via `_load_or_build_baseline`.
- Workspace `config.yaml`: add `web/static/src/core/**` to
  `core_paths`. Consider `search/**` + `webclient/**` in
  `framework_paths` while at it.
- README: pipeline stages + event kinds sections.

## Phasing & bench gates

- **Phase 1 - definitions only. DONE 2026-06-04.** Both anchors, no
  rollout matching (`_KIND_LANGUAGES` rows stay empty). Ledger fills
  with JS primitives at zero rollouts. Zero false-positive risk;
  immediately useful for the talk. Gate: reindex diff eyeballed, unit
  + e2e tests in the `test_file_conventions.py` style.
  Reindex results: 102 NEW_JS_EXPORT, 47 REMOVED_JS_EXPORT, 19
  NEW_REGISTRY_CATEGORY, 18 NEW_REGISTRY_ENTRY, exactly 1
  VENDORED_LIB_BUMP (owl 2.8.2 -> 3.0.0-alpha.31), 51 extractor-emitted
  registry rollouts (pos_payment_providers x18 the star). Two field
  notes: (a) rename folding never fired on the real corpus - candidate
  for deletion per the OWL section's own rule, decide in phase 2;
  (b) NEW_REGISTRY_CATEGORY was implemented wide-scope (addon-invented
  registries count) - that's what surfaced web_studio's
  `editor_fields` from enterprise; tighten only if it gets noisy.
- **Phase 2 - adoption. DONE 2026-06-04.** Import-anchored matcher
  (`rollouts._js_import_pattern`: named/aliased/multi-line/default
  import forms) + `_js_from_plausible` (defining module / ancestor
  barrel / relative path), `Language.JS` rows for NEW_JS_EXPORT and
  REMOVED_JS_EXPORT only, `_JS_GENERIC_SHORT_NAMES` blocklist (OWL
  vocabulary + ubiquitous `@web/core` exports), JS test files skipped
  (hoot name-shadowing). Gate run as `bench/bench_js.py` over the full
  real corpus (7,129 JS-touching commits): the historical false
  positives (`PropertiesDefinition.setup`, `Transaction.cache`) are
  live watchlist entries, so a full-corpus scan asserting zero
  non-JS-kind rollouts in .js files IS the must-not-match regression;
  matched import lines dumped to `bench/js_adoptions.jsonl` and
  hand-verified per symbol. Known accepted misses: a name appended to
  an existing multi-line import list (diff shows a bare `NAME,` line -
  matching it would readmit the object-literal FP class), and barrels
  living off the defining module's ancestor path.
- **Phase 3 - polish.** Generic-list tuning, QWeb `t-` directive
  needles in `static/src/**/*.xml`, breadth-bonus calibration for the
  enterprise volume (2,300 commits may warrant JS-specific
  `breadth_bonuses` tiers).

## Non-goals

- TS support (odoo master is JS-only).
- Tracking `static/lib/owl` internals beyond the version sniff - the
  OWL story enters via the epoch event and the upgrade_code tooling.
- Content-based JS matching of any Python/View primitive. Ever.
