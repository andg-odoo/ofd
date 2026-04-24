"""Parity + wall-time bench for the rollout matcher.

Loads the stratified corpus captured by `capture_corpus.py`, runs
`detect_rollouts` against it, and:

- Parity mode (default): sorts/canonicalizes output, diffs against
  `bench/golden.jsonl`. Pass `--update-golden` to rewrite.
- Timing mode: runs the matcher at N ∈ {25, 50, 100, full} watchlist
  sizes, three runs each, reports median wall-time per stratum. Clears
  the matcher cache between size steps so build cost is visible.

Usage:
  python bench/bench_matcher.py --update-golden        # first run
  python bench/bench_matcher.py                        # parity + timing
  python bench/bench_matcher.py --timing-only
  python bench/bench_matcher.py --parity-only
"""

from __future__ import annotations

import argparse
import json
import pickle
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ofd import rollouts as rollouts_mod  # noqa: E402
from ofd.rollouts import detect_rollouts  # noqa: E402
from ofd.watchlist import Watchlist  # noqa: E402

from capture_corpus import CorpusEntry  # noqa: E402,F401  (needed for unpickle)


TIMING_NS = (25, 50, 100, None)  # None = full
TIMING_RUNS = 3


@dataclass
class MatcherRun:
    total_records: int
    by_stratum: dict[str, int]
    build_ms: float
    match_ms_per_run: list[float]
    match_ms_per_stratum: dict[str, list[float]]


def load_corpus(bench_dir: Path) -> tuple[list, dict]:
    pkl = bench_dir / "corpus.pkl"
    meta_path = bench_dir / "corpus_meta.json"
    if not pkl.exists():
        raise SystemExit(
            f"no corpus at {pkl}; run `python bench/capture_corpus.py` first"
        )
    with pkl.open("rb") as f:
        corpus = pickle.load(f)
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    return corpus, meta


def load_watchlist(bench_dir: Path) -> Watchlist:
    wl_path = bench_dir / "watchlist.json"
    if not wl_path.exists():
        raise SystemExit(
            f"no watchlist snapshot at {wl_path}; re-run capture_corpus"
        )
    with wl_path.open() as f:
        return Watchlist.from_dict(json.load(f))


def subset_watchlist(wl: Watchlist, n: int | None) -> Watchlist:
    """Deterministic prefix by symbol sort. None = full."""
    items = sorted(wl.entries.items(), key=lambda kv: kv[0])
    if n is not None:
        items = items[:n]
    return Watchlist(entries=dict(items))


def canonical_records(corpus: list[CorpusEntry], wl: Watchlist) -> list[dict]:
    """Run the matcher over the corpus and return a sorted list of
    JSON-serializable record dicts keyed uniquely per (repo, sha, ...).
    """
    out: list[dict] = []
    for entry in corpus:
        records = detect_rollouts(entry.patches, wl)
        for r in records:
            d = r.to_dict()
            d["repo"] = entry.repo
            d["sha"] = entry.sha
            out.append(d)
    out.sort(key=lambda d: (
        d["repo"], d["sha"], d["file"], d["line"],
        d.get("symbol", ""), d.get("hunk_header", ""),
        d.get("before_snippet", ""), d.get("after_snippet", ""),
    ))
    return out


def run_parity(
    corpus: list[CorpusEntry],
    wl: Watchlist,
    golden_path: Path,
    update: bool,
) -> bool:
    print(f"[parity] corpus={len(corpus)} watchlist={len(wl.entries)}")
    t0 = time.perf_counter()
    records = canonical_records(corpus, wl)
    print(
        f"[parity] {len(records)} records in "
        f"{time.perf_counter() - t0:.1f}s"
    )

    if update:
        with golden_path.open("w") as f:
            for r in records:
                f.write(json.dumps(r, sort_keys=True) + "\n")
        print(f"[parity] wrote golden: {golden_path} ({len(records)} records)")
        return True

    if not golden_path.exists():
        raise SystemExit(
            f"no golden at {golden_path}; run with --update-golden first"
        )
    with golden_path.open() as f:
        golden = [json.loads(line) for line in f if line.strip()]

    if records == golden:
        print(f"[parity] OK - {len(records)} records match golden")
        return True

    print(
        f"[parity] MISMATCH - current={len(records)} golden={len(golden)}"
    )
    cur_keys = {_key(r) for r in records}
    gld_keys = {_key(r) for r in golden}
    missing = gld_keys - cur_keys
    extra = cur_keys - gld_keys
    print(f"[parity]   missing from current: {len(missing)}")
    print(f"[parity]   extra in current:    {len(extra)}")
    for k in list(missing)[:5]:
        print(f"[parity]     -  {k}")
    for k in list(extra)[:5]:
        print(f"[parity]     +  {k}")
    return False


def _key(r: dict) -> tuple:
    return (
        r.get("repo"), r.get("sha"), r.get("file"), r.get("line"),
        r.get("symbol"), r.get("hunk_header"),
    )


def _time_match(corpus: list[CorpusEntry], wl: Watchlist) -> tuple[float, dict[str, float]]:
    """One full pass; returns (total_ms, per_stratum_ms)."""
    by_stratum: dict[str, float] = {"hit": 0.0, "miss": 0.0}
    t0 = time.perf_counter()
    for entry in corpus:
        t = time.perf_counter()
        detect_rollouts(entry.patches, wl)
        by_stratum[entry.stratum] += (time.perf_counter() - t) * 1000.0
    total = (time.perf_counter() - t0) * 1000.0
    return total, by_stratum


def run_timing(corpus: list[CorpusEntry], wl: Watchlist) -> list[dict]:
    print(f"[timing] corpus={len(corpus)} "
          f"hits={sum(1 for e in corpus if e.stratum == 'hit')} "
          f"misses={sum(1 for e in corpus if e.stratum == 'miss')}")
    results: list[dict] = []
    for n in TIMING_NS:
        sub = subset_watchlist(wl, n)
        n_label = "full" if n is None else str(n)
        # Clear matcher cache so build cost lands on the first run only
        # and isn't smeared across N steps.
        rollouts_mod._MATCHER_CACHE.clear()

        tb = time.perf_counter()
        rollouts_mod._build_matcher(sub)
        build_ms = (time.perf_counter() - tb) * 1000.0

        rollouts_mod._MATCHER_CACHE.clear()  # ensure timed runs pay equal cost
        runs: list[float] = []
        per_stratum: dict[str, list[float]] = {"hit": [], "miss": []}
        total_records = 0
        for _ in range(TIMING_RUNS):
            rollouts_mod._MATCHER_CACHE.clear()
            rec_count = 0
            t0 = time.perf_counter()
            hit_ms = 0.0
            miss_ms = 0.0
            for entry in corpus:
                ts = time.perf_counter()
                rs = detect_rollouts(entry.patches, sub)
                dt = (time.perf_counter() - ts) * 1000.0
                rec_count += len(rs)
                if entry.stratum == "hit":
                    hit_ms += dt
                else:
                    miss_ms += dt
            runs.append((time.perf_counter() - t0) * 1000.0)
            per_stratum["hit"].append(hit_ms)
            per_stratum["miss"].append(miss_ms)
            total_records = rec_count

        median_total = statistics.median(runs)
        median_hit = statistics.median(per_stratum["hit"])
        median_miss = statistics.median(per_stratum["miss"])
        n_hit = sum(1 for e in corpus if e.stratum == "hit")
        n_miss = sum(1 for e in corpus if e.stratum == "miss")
        per_hit_us = (median_hit / n_hit) * 1000.0 if n_hit else 0.0
        per_miss_us = (median_miss / n_miss) * 1000.0 if n_miss else 0.0

        print(
            f"[timing] N={n_label:>4} | build={build_ms:7.1f}ms | "
            f"total={median_total:8.1f}ms (runs {[round(r, 1) for r in runs]}) | "
            f"hit={median_hit:7.1f}ms ({per_hit_us:6.1f}us/commit) | "
            f"miss={median_miss:6.1f}ms ({per_miss_us:6.1f}us/commit) | "
            f"records={total_records}"
        )
        results.append({
            "n": n_label,
            "build_ms": round(build_ms, 2),
            "median_total_ms": round(median_total, 2),
            "median_hit_ms": round(median_hit, 2),
            "median_miss_ms": round(median_miss, 2),
            "per_hit_us": round(per_hit_us, 2),
            "per_miss_us": round(per_miss_us, 2),
            "runs_ms": [round(r, 2) for r in runs],
            "records": total_records,
            "corpus_hit": n_hit,
            "corpus_miss": n_miss,
        })
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bench-dir", default="bench", help="dir with corpus.pkl / watchlist.json")
    parser.add_argument("--golden", default=None, help="golden path (default: <bench-dir>/golden.jsonl)")
    parser.add_argument("--update-golden", action="store_true")
    parser.add_argument("--parity-only", action="store_true")
    parser.add_argument("--timing-only", action="store_true")
    parser.add_argument("--results-out", default=None, help="write timing JSON here")
    args = parser.parse_args()

    bench_dir = Path(args.bench_dir).resolve()
    golden_path = Path(args.golden) if args.golden else bench_dir / "golden.jsonl"

    corpus, meta = load_corpus(bench_dir)
    wl = load_watchlist(bench_dir)
    print(f"[bench] meta={meta}")

    parity_ok = True
    if not args.timing_only:
        parity_ok = run_parity(corpus, wl, golden_path, args.update_golden)

    if not args.parity_only and not args.update_golden:
        results = run_timing(corpus, wl)
        out_path = Path(args.results_out) if args.results_out else bench_dir / "timings.json"
        out_path.write_text(json.dumps({"meta": meta, "results": results}, indent=2) + "\n")
        print(f"[bench] wrote {out_path}")

    if not parity_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
