"""End-to-end orchestration for `ofd run`.

Per-repo sequential commit processing:
  1. Enumerate new commits on tracked branch since last_seen_sha, filtered
     to framework paths.
  2. For each commit, run handlers on gated-path files -> definition events.
  3. Update the (mutable) watchlist with newly-seen primitives.
  4. Run rollout detection on *all* changed files for that commit.
  5. Score every record with the commit's ScoreContext.
  6. Persist raw/<repo>/<sha>.json if non-empty.
  7. Advance state.last_seen_sha only on success.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ofd import gitio
from ofd import state as state_mod
from ofd import watchlist as watchlist_mod
from ofd.config import Config, RepoConfig
from ofd.events.record import ChangeRecord, CommitEnvelope, CommitRecord, Kind
from ofd.events.store import raw_path
from ofd.events.store import write as write_record
from ofd.extractors import (
    context_keys,
    dependencies,
    file_conventions,
    js_,
    manifest_keys,
    modules,
)
from ofd.extractors.dispatcher import extract_for_file
from ofd.globs import match_any
from ofd.release_detect import detect_version, is_release_file
from ofd.rollouts import detect_rollouts, find_model_name
from ofd.scoring import ScoreContext, score_event
from ofd.state import State
from ofd.watchlist import Watchlist


def _is_gated(path: str, patterns: list[str]) -> bool:
    return match_any(path, patterns)


# Added line that *looks like* it carries a manifest dict key. Pre-gate
# before the parent/child fetch + ast parse: data-list / depends edits
# add no key-shaped line at all. Over-matching (version bumps, nested
# asset-bucket keys, one-line dicts) is fine - the exact ast top-level
# diff decides.
_MANIFEST_KEY_LINE = re.compile(
    r"""^\+.*['"][A-Za-z_][\w.]*['"]\s*:""", re.MULTILINE,
)


def _dedupe_kwarg_overrides(records: list[ChangeRecord]) -> list[ChangeRecord]:
    """Collapse `Class.method.kwarg` duplicates within a single commit.

    When a commit adds the same kwarg to a method override across
    multiple Field subclasses (e.g. `Field.to_sql.table`,
    `BaseString.to_sql.table`, `Many2one.to_sql.table` all in the same
    PR), the matcher's per-hunk dedupe attributes every adoption to
    just one entry - the others sit in the ledger as zero-rollout
    shadows of the same logical primitive.

    Heuristic: group by `(method_name, kwarg_name)` (last two symbol
    segments). When a group has >1 entry, keep the alphabetically-first
    symbol (which puts `Field` ahead of `BaseString`/`Many2one`/etc.
    because `fields.py` < `fields_textual.py` / `fields_relational.py`
    in module-path sort order). Drop the rest.

    Cross-commit shadows aren't touched - if a subclass override lands
    in a separate PR, its symbol is preserved. This is the in-flight
    PR pattern, not a long-tail concern.
    """
    by_method_kwarg: dict[tuple[str, str], list[ChangeRecord]] = {}
    others: list[ChangeRecord] = []
    for r in records:
        if r.kind is not Kind.NEW_KWARG or not r.symbol:
            others.append(r)
            continue
        parts = r.symbol.split(".")
        if len(parts) < 4:
            others.append(r)
            continue
        by_method_kwarg.setdefault((parts[-2], parts[-1]), []).append(r)
    out: list[ChangeRecord] = list(others)
    for group in by_method_kwarg.values():
        if len(group) == 1:
            out.append(group[0])
            continue
        out.append(min(group, key=lambda r: r.symbol))
    return out


def _rollout_postdates_definition(
    rollout: ChangeRecord,
    watchlist: Watchlist,
    commit_at: str,
) -> bool:
    """True iff `rollout`'s commit isn't earlier than the watchlisted
    primitive's first_seen_at. ISO-8601 strings compare correctly
    lexicographically; manual pins (`first_seen_at = "(manual)"`) sort
    before any real ISO date so they're always kept.
    """
    if not rollout.symbol:
        return True
    entry = watchlist.entries.get(rollout.symbol)
    if entry is None:
        return True
    return entry.first_seen_at <= commit_at


def _any_rollout_candidate(changed_files: list[str], watchlist: Watchlist) -> bool:
    """Cheap pre-check: should we spend time scanning this commit's
    non-gated diffs for rollouts? Only if the watchlist has entries and
    some changed file looks like it could contain Python/XML/JS code.
    File-extension check beats scanning every .md/.po/.csv commit.
    """
    if not watchlist.short_names():
        return False
    return any(f.endswith((".py", ".xml", ".js")) for f in changed_files)


@dataclass
class CommitSummary:
    sha: str
    changes: int
    persisted: bool


@dataclass
class RunSummary:
    repos: dict[str, list[CommitSummary]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def total_commits(self) -> int:
        return sum(len(v) for v in self.repos.values())

    @property
    def total_changes(self) -> int:
        return sum(
            cs.changes for commits in self.repos.values() for cs in commits
        )


def process_commit(
    repo: RepoConfig,
    sha: str,
    config: Config,
    watchlist: Watchlist,
    preloaded_files: list[str] | None = None,
    preloaded_info: gitio.CommitInfo | None = None,
    blob_fetcher: gitio.BlobFetcher | None = None,
    repo_state=None,
    baseline_context_keys: frozenset[str] | None = None,
    baseline_conventions: frozenset[str] | None = None,
    baseline_registry: frozenset[str] | None = None,
    baseline_manifest_keys: frozenset[str] | None = None,
) -> CommitRecord | None:
    """Run extract + rollout + score for one commit. Returns a CommitRecord
    if any changes were found, else None. Does not persist - caller writes.

    If `blob_fetcher` is provided, all blob reads go through it (one git
    subprocess for the whole run); otherwise each read spawns its own.

    If `preloaded_info` is provided (from a bulk `log_commits_with_files`
    call), the per-commit `commit_info` subprocess is skipped.

    If `repo_state` is provided, version-bump commits (changes to
    `odoo/release.py`) update `repo_state.detected_version`; subsequent
    commits stamp their envelope with that version instead of the config
    default. This lets ledger frontmatter reflect the series each
    primitive landed in.
    """
    info = preloaded_info or gitio.commit_info(repo.mirror, sha)

    all_files = (
        preloaded_files if preloaded_files is not None
        else gitio.changed_files(repo.mirror, sha)
    )
    if not all_files:
        return None

    def _fetch(blob_sha: str, path: str) -> str | None:
        if blob_fetcher is not None:
            return blob_fetcher.fetch(blob_sha, path)
        return gitio.show_blob(repo.mirror, blob_sha, path)

    # Version detection: if this commit touches release.py, re-parse before
    # stamping the envelope so the bump commit itself is credited to the
    # *new* series (useful in the rare case it also changes something
    # framework-adjacent).
    if repo_state is not None:
        for f in all_files:
            if is_release_file(f):
                detected = detect_version(_fetch(sha, f))
                if detected:
                    repo_state.detected_version = detected
                break

    active_version = (
        (repo_state.detected_version if repo_state else None)
        or config.active_version
    )
    envelope = CommitEnvelope(
        sha=info.sha,
        repo=repo.name,
        branch=repo.branch,
        active_version=active_version,
        author_name=info.author_name,
        author_email=info.author_email,
        committed_at=info.committed_at,
        subject=info.subject,
        body=info.body,
    )

    gated_files = [f for f in all_files if match_any(f, repo.framework_paths)]

    changes: list[ChangeRecord] = []

    # --- stage 1: framework-path extraction ---
    child_sources: dict[str, str | None] = {}
    for file in gated_files:
        parent_src = _fetch(f"{sha}^", file)
        child_src = _fetch(sha, file)
        child_sources[file] = child_src
        records = extract_for_file(parent_src, child_src, file)
        changes.extend(records)

    # --- stage 1.5: wide-scope extractors (context keys) ---
    # `@api.depends_context(...)` decorators almost always live in
    # addons (outside framework_paths), so we run a needle-gated wide
    # scan: pull the commit diff once, look for files whose patch
    # mentions `depends_context`, fetch parent/child source for those
    # only. The `all_patches` value is reused by stage 3.
    all_patches: dict[str, str] | None = None
    py_files = [f for f in all_files if f.endswith(".py")]
    if py_files:
        all_patches = gitio.commit_diff_by_file(repo.mirror, sha)
        for file in py_files:
            patch = all_patches.get(file, "")
            if "depends_context" not in patch:
                continue
            if file in child_sources:
                child_src = child_sources[file]
            else:
                child_src = _fetch(sha, file)
                child_sources[file] = child_src
            parent_src = _fetch(f"{sha}^", file)
            changes.extend(context_keys.extract(
                parent_src, child_src, file,
                baseline_keys=baseline_context_keys,
            ))

    # --- stage 1.6: file-convention detection ---
    # New data-file basenames (e.g. `security/ir.access.csv`) appearing
    # across several modules at once. Candidate-gate on basename first -
    # nearly every commit's data files carry baseline basenames - then
    # confirm each survivor is an *added* file by checking its parent
    # blob doesn't exist (the bulk log is --name-only; no status here).
    conv_added: list[tuple[str, str, str]] = []
    for file in all_files:
        cand = file_conventions.candidate(file)
        if cand is None:
            continue
        module, basename = cand
        if baseline_conventions and basename in baseline_conventions:
            continue
        if _fetch(f"{sha}^", file) is None:
            conv_added.append((file, module, basename))
    if conv_added:
        changes.extend(file_conventions.extract(conv_added, watchlist.entries))

    # --- stage 1.65: manifest-key + module detection ---
    # Wide-scope like context keys. Manifests churn constantly (version
    # bumps, depends edits), so gate on cheap patch checks before
    # paying the parent/child fetch + parse:
    #   - an added key-shaped line feeds the manifest-key diff;
    #   - an added/deleted manifest file is a new/removed module;
    #   - an added line quoting a tracked (or same-commit-added) module
    #     name is a candidate depends-rollout.
    # The exact ast diffs downstream keep only genuine events.
    manifest_files = [
        f for f in all_files
        if f.endswith("__manifest__.py")
        and not manifest_keys.is_test_module_manifest(f)
    ]
    if manifest_files:
        if all_patches is None:
            all_patches = gitio.commit_diff_by_file(repo.mirror, sha)
        module_needles = {
            e.short_name for e in watchlist.entries.values()
            if e.kind is Kind.NEW_MODULE
        }
        module_needles.update(
            modules.module_name(f) for f in manifest_files
            if "\nnew file mode " in all_patches.get(f, "")
        )
        gated_manifests: list[tuple[str, str | None, str | None]] = []
        module_manifests: list[tuple[str, str | None, str | None]] = []
        for file in manifest_files:
            patch = all_patches.get(file, "")
            key_shaped = bool(_MANIFEST_KEY_LINE.search(patch))
            added_or_deleted = (
                "\nnew file mode " in patch or "\ndeleted file mode " in patch
            )
            dep_needle = any(
                f"'{n}'" in patch or f'"{n}"' in patch for n in module_needles
            )
            if not (key_shaped or added_or_deleted or dep_needle):
                continue
            pair = (file, _fetch(f"{sha}^", file), _fetch(sha, file))
            if key_shaped:
                gated_manifests.append(pair)
            if added_or_deleted or dep_needle:
                module_manifests.append(pair)
        if gated_manifests:
            changes.extend(manifest_keys.extract(
                gated_manifests, watchlist.entries, baseline_manifest_keys,
            ))
        if module_manifests:
            changes.extend(modules.extract(module_manifests, watchlist.entries))

    # --- stage 1.7: wide-scope JS registry scan ---
    # `registry.category("x").add("y", ...)` lives mostly in addons,
    # outside framework paths - same shape as context keys, so the same
    # needle-gated wide scan: only files whose patch mentions
    # `registry.category` get fetched and parsed. New categories are
    # definitions wherever they appear; new entries are definitions in
    # framework paths and category rollouts elsewhere.
    js_files = [f for f in all_files if f.endswith(".js")]
    if js_files:
        if all_patches is None:
            all_patches = gitio.commit_diff_by_file(repo.mirror, sha)
        registry_files: list[tuple[str, bool, str | None, str | None]] = []
        for file in js_files:
            if "registry.category" not in all_patches.get(file, ""):
                continue
            if file not in child_sources:
                child_sources[file] = _fetch(sha, file)
            registry_files.append((
                file,
                match_any(file, repo.framework_paths),
                _fetch(f"{sha}^", file),
                child_sources[file],
            ))
        if registry_files:
            changes.extend(js_.extract_registry(
                registry_files, watchlist.entries, baseline_registry,
            ))

    # --- stage 1.8: vendored-lib version sniff ---
    # Epoch events ("OWL 3 landed") from major-version changes of
    # tracked vendored bundles. Path-gated dict lookup, so the cost on
    # ordinary commits is nil.
    for file in all_files:
        if js_.vendored_lib_alias(file) is None:
            continue
        changes.extend(js_.extract_lib_bump(
            _fetch(f"{sha}^", file), _fetch(sha, file), file,
        ))

    # --- stage 1.85: external-dependency epochs ---
    # Added/removed package names in the repo-root requirements.txt.
    # Strict path equality: addon-local requirements files aren't
    # platform dependencies.
    if "requirements.txt" in all_files:
        changes.extend(dependencies.extract(
            _fetch(f"{sha}^", "requirements.txt"),
            _fetch(sha, "requirements.txt"),
            "requirements.txt",
        ))

    # Collapse same-commit override duplicates before they reach the
    # watchlist so they never become shadow entries in the first place.
    changes = _dedupe_kwarg_overrides(changes)

    # --- stage 2: watchlist update (before rollout scan) ---
    # Surface-only paths (migration tooling, CLI scripts) emit events
    # but never join the watchlist: their helpers (`change`, `upgrade`,
    # `tokenize`, ...) aren't adoptable APIs, yet their short names
    # match everywhere once watchlisted.
    for record in changes:
        if repo.surface_only_paths and match_any(
            record.file, repo.surface_only_paths,
        ):
            continue
        watchlist.add_from_definition(
            record,
            repo=repo.name,
            sha=sha,
            committed_at=envelope.committed_at,
            active_version=config.active_version,
        )

    # --- stage 3: rollout scan over all changed files ---
    if watchlist.short_names():
        if all_patches is None:
            all_patches = gitio.commit_diff_by_file(repo.mirror, sha)
        non_gated = [f for f in all_files if f not in gated_files]
        patches = {
            file: all_patches[file]
            for file in non_gated
            if file in all_patches
        }
        # Scan once, then back-fill model names only for files that hit.
        # Earlier versions ran the same regex twice - once as a
        # "should we fetch child source?" pre-check and once in
        # detect_rollouts - which profiled as ~85% of runtime.
        rollouts = detect_rollouts(
            patches, watchlist, child_sources,
            fetch_child=lambda f: _fetch(sha, f),
        )
        # Temporal filter: drop rollouts whose commit predates the
        # primitive's first_seen_at. Within a single repo this can't
        # happen (commits are walked oldest-first, so a definition is
        # always added to the watchlist before any later same-repo
        # rollout). It DOES happen across repos: `_ordered_for_watchlist_build`
        # runs the framework repo's full history before the adopter
        # repo, so by the time we reach an early adopter-repo commit,
        # primitives defined years later in the framework repo are
        # already on the watchlist. Any such "rollout" is temporally
        # impossible - the syntax can match (e.g. `<widget invisible=...>`
        # was already legal at runtime before the RNG schema formalized
        # it), but it's not an adoption of *this* primitive. Manual
        # pins carry `first_seen_at = "(manual)"` which sorts lexicographically
        # before any ISO date, so they pass through unchanged.
        rollouts = [
            r for r in rollouts
            if _rollout_postdates_definition(r, watchlist, envelope.committed_at)
        ]
        hit_files = {r.file for r in rollouts if r.file not in child_sources}
        for file in hit_files:
            child_sources[file] = _fetch(sha, file)
        for r in rollouts:
            if r.model is None:
                r.model = find_model_name(child_sources.get(r.file))
        changes.extend(rollouts)

    if not changes:
        return None

    # --- stage 4: scoring ---
    ctx = ScoreContext(
        commit=envelope,
        core_paths=repo.core_paths,
        key_devs=config.key_devs,
        intent_keywords=config.scoring.intent_keywords,
    )
    for record in changes:
        score_event(record, ctx)

    return CommitRecord(commit=envelope, changes=changes)


ProgressCb = Callable[[str, str, int, int], None]
"""progress_cb(repo_name, sha, processed, total)

Called once per commit enumerated. `processed` is the count so far (1-indexed);
`total` is the full commit count for this repo. Used by the CLI to drive a
progress bar; pipeline keeps no dependency on rich.
"""

StatusCb = Callable[[str], None]
"""status_cb(message) - free-form status lines for phases that aren't
per-commit (repo enumeration, pruning, etc). The CLI prints these so the
user knows what the dead time between 'start' and 'first tick' is doing.
"""


def run_repo(
    repo: RepoConfig,
    config: Config,
    state: State,
    watchlist: Watchlist,
    since_override: str | None = None,
    progress_cb: ProgressCb | None = None,
    status_cb: StatusCb | None = None,
) -> list[CommitSummary]:
    """Process every new commit on this repo's tracked branch."""
    repo_state = state.get(repo.name)
    since_sha = since_override or repo_state.last_seen_sha
    # Apply the config date floor only when the walk isn't already
    # bounded by a SHA - explicit SHAs take precedence and implicitly
    # cover a narrower slice.
    since_date = config.since_date if since_sha is None else None

    if status_cb:
        bound = since_sha[:10] if since_sha else since_date or "full history"
        status_cb(f"{repo.name}: enumerating commits (since {bound})...")

    # Pre-existing context keys: anything already declared via
    # `@api.depends_context(...)` at the start of the tracking window
    # is suppressed at extraction time. Computed once per repo, cached
    # to disk by baseline SHA so re-runs skip the rescan.
    baseline_keys = _load_or_build_baseline_keys(repo, config, status_cb)
    # Same idea for data-file basenames under security/ and data/:
    # everything present at the floor is a known convention.
    baseline_convs = _load_or_build_baseline_conventions(repo, config, status_cb)
    # And for JS registry categories/entries: everything registered at
    # the floor is pre-known, so a new addon re-citing `services` or
    # re-adding a floor-era entry never fires.
    baseline_registry = _load_or_build_baseline_registry(repo, config, status_cb)
    # And for manifest keys: every top-level key used by any manifest
    # at the floor is pre-known.
    baseline_manifests = _load_or_build_baseline_manifest_keys(
        repo, config, status_cb,
    )

    # Bulk-enumerate commits + their file lists in a single git call -
    # orders of magnitude faster than per-commit diff-tree when most
    # commits only touch non-gated paths.
    commits_with_files = gitio.log_commits_with_files(
        repo.mirror, repo.branch, since_sha=since_sha, since_date=since_date,
    )
    total = len(commits_with_files)

    if status_cb:
        status_cb(f"{repo.name}: {total} commit(s) to process")

    summaries: list[CommitSummary] = []
    with gitio.BlobFetcher(repo.mirror) as fetcher:
        for i, (info, changed) in enumerate(commits_with_files, start=1):
            sha = info.sha
            touches_gated = any(_is_gated(f, repo.framework_paths) for f in changed)
            needs_rollout_scan = _any_rollout_candidate(changed, watchlist)
            # Convention candidates are .csv/.xml data files - invisible
            # to _any_rollout_candidate's extension check on .csv, so a
            # pure mass-conversion commit needs its own gate.
            needs_convention_scan = any(
                (c := file_conventions.candidate(f)) is not None
                and c[1] not in baseline_convs
                for f in changed
            )
            # JS commits need the registry needle check (and vendored-lib
            # sniff), which requires the diff - can't be decided from
            # file names alone. With a non-empty watchlist these commits
            # already pass `needs_rollout_scan`; this gate only matters
            # for the empty-watchlist window at the start of a reindex.
            touches_js = any(f.endswith(".js") for f in changed)
            # Manifest / requirements commits need their own gate for
            # the empty-watchlist window, like touches_js: manifests
            # are .py (so usually covered by needs_rollout_scan) but
            # requirements.txt is invisible to the extension check.
            touches_platform_meta = any(
                f.endswith("__manifest__.py") or f == "requirements.txt"
                for f in changed
            )
            touches_release = any(is_release_file(f) for f in changed)
            if (
                not touches_gated
                and not needs_rollout_scan
                and not needs_convention_scan
                and not touches_js
                and not touches_platform_meta
            ):
                # Release bumps are commonly one-line changes to release.py
                # with nothing else. Still parse so detected_version
                # advances for subsequent commits.
                if touches_release:
                    for f in changed:
                        if is_release_file(f):
                            v = detect_version(fetcher.fetch(sha, f))
                            if v:
                                repo_state.detected_version = v
                            break
                repo_state.last_seen_sha = sha
                repo_state.last_run_at = datetime.now(tz=UTC).isoformat()
                if progress_cb:
                    progress_cb(repo.name, sha, i, total)
                continue
            record = process_commit(
                repo, sha, config, watchlist,
                preloaded_files=changed, preloaded_info=info,
                blob_fetcher=fetcher, repo_state=repo_state,
                baseline_context_keys=baseline_keys,
                baseline_conventions=baseline_convs,
                baseline_registry=baseline_registry,
                baseline_manifest_keys=baseline_manifests,
            )
            if record:
                write_record(config.workspace, record)
                summaries.append(CommitSummary(sha=sha, changes=len(record.changes), persisted=True))
            else:
                # A previous reindex may have written a raw for this
                # sha; if the watchlist has since shrunk (via remove or
                # rebuild), its events are stale. Drop the file so the
                # ledger pass doesn't resurrect orphaned rollouts.
                stale = raw_path(config.workspace, repo.name, sha)
                if stale.exists():
                    stale.unlink(missing_ok=True)
                summaries.append(CommitSummary(sha=sha, changes=0, persisted=False))
            repo_state.last_seen_sha = sha
            repo_state.last_run_at = datetime.now(tz=UTC).isoformat()
            if progress_cb:
                progress_cb(repo.name, sha, i, total)

    return summaries


def _load_or_build_baseline(
    repo: RepoConfig,
    config: Config,
    status_cb: StatusCb | None,
    *,
    name: str,
    label: str,
    build: Callable[[str], frozenset[str]],
) -> frozenset[str]:
    """Resolve the baseline SHA from `since_date`, build the named
    baseline set there via `build(baseline_sha)`, and cache the result
    by SHA. If the cache hit matches the resolved SHA, skip the scan.

    Returns an empty set when the config has no `since_date` or no
    commit predates it on the tracked branch (fresh repo, or the entire
    branch was authored after the floor) - in either case there's no
    "before" snapshot to compare against, so every emission is
    considered new on its own merits.
    """
    if not config.since_date:
        return frozenset()
    try:
        baseline_sha = gitio.commit_at_or_before(
            repo.mirror, repo.branch, config.since_date,
        )
    except gitio.GitError:
        return frozenset()
    if not baseline_sha:
        return frozenset()
    cache_path = (
        config.workspace / "baselines" / f"{name}.{repo.name}.json"
    )
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text())
            if data.get("baseline_sha") == baseline_sha:
                return frozenset(data.get("keys", []))
        except (OSError, ValueError):
            pass
    if status_cb:
        status_cb(
            f"{repo.name}: scanning baseline {label} at "
            f"{baseline_sha[:10]} (one-time, cached)..."
        )
    keys = build(baseline_sha)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(
        {"baseline_sha": baseline_sha, "keys": sorted(keys)},
        indent=2,
    ) + "\n")
    return frozenset(keys)


def _load_or_build_baseline_keys(
    repo: RepoConfig,
    config: Config,
    status_cb: StatusCb | None,
) -> frozenset[str]:
    return _load_or_build_baseline(
        repo, config, status_cb,
        name="context_keys", label="context keys",
        build=lambda sha: frozenset(
            context_keys.scan_baseline_keys(repo.mirror, sha)
        ),
    )


def _load_or_build_baseline_conventions(
    repo: RepoConfig,
    config: Config,
    status_cb: StatusCb | None,
) -> frozenset[str]:
    return _load_or_build_baseline(
        repo, config, status_cb,
        name="file_conventions", label="data-file basenames",
        build=lambda sha: file_conventions.baseline_basenames(
            gitio.ls_tree(repo.mirror, sha)
        ),
    )


def _load_or_build_baseline_registry(
    repo: RepoConfig,
    config: Config,
    status_cb: StatusCb | None,
) -> frozenset[str]:
    return _load_or_build_baseline(
        repo, config, status_cb,
        name="js_registry", label="JS registry symbols",
        build=lambda sha: frozenset(
            js_.scan_baseline_registry(repo.mirror, sha)
        ),
    )


def _load_or_build_baseline_manifest_keys(
    repo: RepoConfig,
    config: Config,
    status_cb: StatusCb | None,
) -> frozenset[str]:
    return _load_or_build_baseline(
        repo, config, status_cb,
        name="manifest_keys", label="manifest keys",
        build=lambda sha: frozenset(
            manifest_keys.scan_baseline_keys(repo.mirror, sha)
        ),
    )


def _ordered_for_watchlist_build(repos: list[RepoConfig]) -> list[RepoConfig]:
    """Reorder so framework repos run before rollout-only repos.

    Enterprise has empty `framework_paths` and only contributes rollouts.
    If it ran first, its commits would scan against an empty watchlist
    and find nothing. Promote framework-path-bearing repos to the front
    while preserving relative config order within each group.
    """
    framework, adopter = [], []
    for r in repos:
        (framework if r.framework_paths else adopter).append(r)
    return framework + adopter


def run(
    config: Config,
    state: State,
    watchlist: Watchlist,
    progress_cb: ProgressCb | None = None,
    status_cb: StatusCb | None = None,
) -> RunSummary:
    summary = RunSummary()
    for repo in _ordered_for_watchlist_build(list(config.repos)):
        if not repo.mirror.exists():
            summary.errors.append(f"{repo.name}: mirror missing at {repo.mirror}")
            continue
        try:
            summary.repos[repo.name] = run_repo(
                repo, config, state, watchlist,
                progress_cb=progress_cb, status_cb=status_cb,
            )
        except Exception as e:
            summary.errors.append(f"{repo.name}: {e}")

    # Persist state and watchlist here so direct programmatic use doesn't
    # need to remember the save calls.
    state_mod.save(state)
    watchlist_mod.save(watchlist, config.workspace)
    return summary
