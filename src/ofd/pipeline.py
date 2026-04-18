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

from dataclasses import dataclass, field
from datetime import UTC, datetime

from ofd import gitio
from ofd import state as state_mod
from ofd import watchlist as watchlist_mod
from ofd.config import Config, RepoConfig
from ofd.events.record import ChangeRecord, CommitEnvelope, CommitRecord
from ofd.events.store import write as write_record
from ofd.extractors.dispatcher import extract_for_file
from ofd.globs import match_any
from ofd.rollouts import detect_rollouts, find_model_name
from ofd.scoring import ScoreContext, score_event
from ofd.state import State
from ofd.watchlist import Watchlist


def _is_gated(path: str, patterns: list[str]) -> bool:
    return match_any(path, patterns)


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
    blob_fetcher: gitio.BlobFetcher | None = None,
) -> CommitRecord | None:
    """Run extract + rollout + score for one commit. Returns a CommitRecord
    if any changes were found, else None. Does not persist - caller writes.

    If `blob_fetcher` is provided, all blob reads go through it (one git
    subprocess for the whole run); otherwise each read spawns its own.
    """
    info = gitio.commit_info(repo.mirror, sha)
    envelope = CommitEnvelope(
        sha=info.sha,
        repo=repo.name,
        branch=repo.branch,
        active_version=config.active_version,
        author_name=info.author_name,
        author_email=info.author_email,
        committed_at=info.committed_at,
        subject=info.subject,
        body=info.body,
    )

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

    # --- stage 2: watchlist update (before rollout scan) ---
    for record in changes:
        watchlist.add_from_definition(
            record,
            repo=repo.name,
            sha=sha,
            committed_at=envelope.committed_at,
            active_version=config.active_version,
        )

    # --- stage 3: rollout scan over all changed files ---
    if watchlist.short_names():
        # One git call to get the whole commit's diff, split client-side.
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
        rollouts = detect_rollouts(patches, watchlist, child_sources)
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


def run_repo(
    repo: RepoConfig,
    config: Config,
    state: State,
    watchlist: Watchlist,
    since_override: str | None = None,
) -> list[CommitSummary]:
    """Process every new commit on this repo's tracked branch."""
    repo_state = state.get(repo.name)
    since_sha = since_override or repo_state.last_seen_sha

    # Bulk-enumerate commits + their file lists in a single git call -
    # orders of magnitude faster than per-commit diff-tree when most
    # commits only touch non-gated paths.
    commits_with_files = gitio.log_commits_with_files(
        repo.mirror, repo.branch, since_sha=since_sha
    )

    summaries: list[CommitSummary] = []
    with gitio.BlobFetcher(repo.mirror) as fetcher:
        for sha, changed in commits_with_files:
            touches_gated = any(_is_gated(f, repo.framework_paths) for f in changed)
            needs_rollout_scan = _any_rollout_candidate(changed, watchlist)
            if not touches_gated and not needs_rollout_scan:
                repo_state.last_seen_sha = sha
                repo_state.last_run_at = datetime.now(tz=UTC).isoformat()
                continue
            record = process_commit(
                repo, sha, config, watchlist,
                preloaded_files=changed, blob_fetcher=fetcher,
            )
            if record:
                write_record(config.workspace, record)
                summaries.append(CommitSummary(sha=sha, changes=len(record.changes), persisted=True))
            else:
                summaries.append(CommitSummary(sha=sha, changes=0, persisted=False))
            repo_state.last_seen_sha = sha
            repo_state.last_run_at = datetime.now(tz=UTC).isoformat()

    return summaries


def run(config: Config, state: State, watchlist: Watchlist) -> RunSummary:
    summary = RunSummary()
    for repo in config.repos:
        if not repo.mirror.exists():
            summary.errors.append(f"{repo.name}: mirror missing at {repo.mirror}")
            continue
        try:
            summary.repos[repo.name] = run_repo(repo, config, state, watchlist)
        except Exception as e:
            summary.errors.append(f"{repo.name}: {e}")

    # Persist state and watchlist here so direct programmatic use doesn't
    # need to remember the save calls.
    state_mod.save(state)
    watchlist_mod.save(watchlist, config.workspace)
    return summary
