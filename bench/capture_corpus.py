"""Capture a stratified patch corpus for matcher benchmarks.

Walks the mirrors once, keeps every commit whose diff passes the
combined short-name prefilter (the population AC is targeting), and
reservoir-samples the long tail of prefilter-miss commits to cap size.

Output:
  bench/corpus.pkl   - list[CorpusEntry]
  bench/watchlist.json - copy of the live watchlist used to stratify

Run once per benchmark cycle. The pickle is gitignored.
"""

from __future__ import annotations

import argparse
import json
import pickle
import random
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ofd import config as config_mod  # noqa: E402
from ofd import gitio, watchlist  # noqa: E402
from ofd.globs import match_any  # noqa: E402

MISS_SAMPLE_CAP = 500


@dataclass
class CorpusEntry:
    repo: str
    sha: str
    committed_at: str
    subject: str
    stratum: str  # "hit" or "miss"
    patches: dict[str, str]  # non-gated file -> unified diff


def _build_combined(short_names: list[str]) -> re.Pattern[str]:
    if not short_names:
        return re.compile(r"$^")
    return re.compile(r"\b(?:" + "|".join(re.escape(n) for n in short_names) + r")\b")


def capture(
    workspace: Path,
    out_dir: Path,
    limit: int | None = None,
    seed: int = 17,
) -> None:
    cfg = config_mod.load(workspace)
    wl = watchlist.load(workspace)
    shorts = sorted(wl.short_names())
    combined = _build_combined(shorts)

    print(f"[capture] watchlist: {len(wl.entries)} entries, {len(shorts)} unique short names")

    rng = random.Random(seed)
    miss_reservoir: list[CorpusEntry] = []
    miss_seen = 0
    hits: list[CorpusEntry] = []

    for repo in cfg.repos:
        since_date = cfg.since_date
        print(f"[capture] enumerating {repo.name} since {since_date} ...")
        t0 = time.perf_counter()
        log = gitio.log_commits_with_files(
            mirror=repo.mirror, branch=repo.branch, since_date=since_date,
        )
        print(f"[capture]   {len(log)} commits in {time.perf_counter() - t0:.1f}s")

        for idx, (info, files) in enumerate(log):
            if limit is not None and idx >= limit:
                break
            non_gated = [f for f in files if not match_any(f, repo.framework_paths)]
            if not non_gated:
                continue
            # Pull full commit diff once, filter to non-gated files.
            all_patches = gitio.commit_diff_by_file(repo.mirror, info.sha)
            patches = {f: all_patches[f] for f in non_gated if f in all_patches}
            if not patches:
                continue
            hit = any(combined.search(p) for p in patches.values())
            entry = CorpusEntry(
                repo=repo.name,
                sha=info.sha,
                committed_at=info.committed_at,
                subject=info.subject,
                stratum="hit" if hit else "miss",
                patches=patches,
            )
            if hit:
                hits.append(entry)
            else:
                miss_seen += 1
                # Classic reservoir sampling: first K go in, subsequent
                # items replace a random slot with probability K/n.
                if len(miss_reservoir) < MISS_SAMPLE_CAP:
                    miss_reservoir.append(entry)
                else:
                    j = rng.randrange(miss_seen)
                    if j < MISS_SAMPLE_CAP:
                        miss_reservoir[j] = entry
            if (idx + 1) % 500 == 0:
                print(
                    f"[capture]   {repo.name}: processed {idx + 1}/{len(log)}"
                    f" - hits={len(hits)} miss_seen={miss_seen}"
                )

    corpus = hits + miss_reservoir
    print(
        f"[capture] done: {len(hits)} hit commits + "
        f"{len(miss_reservoir)}/{miss_seen} miss commits = {len(corpus)} entries"
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    pkl_path = out_dir / "corpus.pkl"
    with pkl_path.open("wb") as f:
        pickle.dump(corpus, f, protocol=pickle.HIGHEST_PROTOCOL)
    size_mb = pkl_path.stat().st_size / (1024 * 1024)
    print(f"[capture] wrote {pkl_path} ({size_mb:.1f} MB)")

    # Snapshot the watchlist used - so the bench replays against exactly
    # the set of short names that stratified the capture.
    wl_src = watchlist.path_for(workspace)
    wl_dst = out_dir / "watchlist.json"
    shutil.copy(wl_src, wl_dst)
    print(f"[capture] wrote {wl_dst}")

    # Metadata sidecar: handy for the bench to report what it's running
    # against without re-parsing the pickle header.
    meta = {
        "workspace": str(workspace),
        "since_date": cfg.since_date,
        "repos": [r.name for r in cfg.repos],
        "total": len(corpus),
        "hit": len(hits),
        "miss_kept": len(miss_reservoir),
        "miss_seen": miss_seen,
        "watchlist_entries": len(wl.entries),
        "short_names": len(shorts),
    }
    with (out_dir / "corpus_meta.json").open("w") as f:
        json.dump(meta, f, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace", default=None,
        help="Override workspace (default: standard resolution)",
    )
    parser.add_argument(
        "--out", default="bench", help="Output directory (default: bench/)",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Cap commits per repo (for dev iteration)",
    )
    args = parser.parse_args()
    workspace = config_mod.resolve_workspace(args.workspace)
    out_dir = Path(args.out).resolve()
    capture(workspace, out_dir, limit=args.limit)


if __name__ == "__main__":
    main()
