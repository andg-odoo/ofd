"""Parity bench for the extraction layer.

Loads the corpus captured by `capture_extract_corpus.py`, runs every
extractor (existing `extract_for_file` plus any new ones registered
below) over each (parent, child) pair, canonicalizes the records, and
diffs against `bench/extract_golden.jsonl`.

Modes:
- Default: parity-only (the extraction surface is small enough that
  timing isn't the interesting axis yet).
- `--update-golden`: rewrites the golden after a verified change.
- `--audit`: bucket adds/drops by (extractor, kind, file dir) for
  fast review of a parity break.

Usage:
  python bench/bench_extract.py --update-golden     # first run
  python bench/bench_extract.py                     # parity check
  python bench/bench_extract.py --audit             # explain the diff
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ofd.extractors.dispatcher import extract_for_file  # noqa: E402

from capture_extract_corpus import ExtractEntry  # noqa: E402,F401  (unpickle)


def _canonical_records(corpus: list[ExtractEntry]) -> list[dict]:
    """Run every extractor over the corpus and return a sorted list of
    JSON-serializable record dicts, keyed uniquely per (repo, sha, file,
    line, symbol, kind)."""
    out: list[dict] = []
    for entry in corpus:
        records = extract_for_file(entry.parent_src, entry.child_src, entry.file)
        try:
            from ofd.extractors import context_keys  # type: ignore
            records.extend(
                context_keys.extract(entry.parent_src, entry.child_src, entry.file)
            )
        except ImportError:
            pass
        for r in records:
            d = r.to_dict()
            d["repo"] = entry.repo
            d["sha"] = entry.sha
            out.append(d)
    out.sort(key=lambda d: (
        d.get("repo", ""), d.get("sha", ""), d.get("file", ""),
        d.get("line", 0), d.get("symbol", ""), d.get("kind", ""),
    ))
    return out


def _key(r: dict) -> tuple:
    return (
        r.get("repo"), r.get("sha"), r.get("file"),
        r.get("line"), r.get("symbol"), r.get("kind"),
    )


def run_parity(
    corpus: list[ExtractEntry],
    golden_path: Path,
    update: bool,
    audit: bool,
) -> bool:
    print(f"[parity] corpus={len(corpus)} entries")
    t0 = time.perf_counter()
    records = _canonical_records(corpus)
    print(f"[parity] {len(records)} records in {time.perf_counter() - t0:.1f}s")

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

    cset = {_key(r): r for r in records}
    gset = {_key(r): r for r in golden}
    missing = [gset[k] for k in gset if k not in cset]
    extra = [cset[k] for k in cset if k not in gset]

    if not missing and not extra:
        print(f"[parity] OK - {len(records)} records match golden")
        return True

    print(f"[parity] MISMATCH - current={len(records)} golden={len(golden)} "
          f"dropped={len(missing)} added={len(extra)}")
    if audit:
        _audit(missing, extra)
    else:
        for r in missing[:5]:
            print(f"[parity]   - {_key(r)}")
        for r in extra[:5]:
            print(f"[parity]   + {_key(r)}")
        print("[parity]   (--audit for the full breakdown)")
    return False


def _audit(missing: list[dict], extra: list[dict]) -> None:
    """Bucket missing/extra by (kind, file-prefix) for fast review."""
    def bucket(r: dict) -> tuple:
        path = r.get("file", "")
        head = "/".join(path.split("/")[:3]) if path else "?"
        return (r.get("kind", "?"), head)

    print("\ndropped buckets (kind | file-prefix):")
    for b, n in Counter(bucket(r) for r in missing).most_common():
        print(f"  {n:5d}  {b}")
    print("\nadded buckets (kind | file-prefix):")
    for b, n in Counter(bucket(r) for r in extra).most_common():
        print(f"  {n:5d}  {b}")

    print("\nsample dropped:")
    for r in missing[:6]:
        print(f"  - {r.get('kind')}  {r.get('symbol')}  ({r.get('file')}:{r.get('line')})")
    print("\nsample added:")
    for r in extra[:6]:
        print(f"  + {r.get('kind')}  {r.get('symbol')}  ({r.get('file')}:{r.get('line')})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bench-dir", default="bench")
    parser.add_argument("--golden", default=None)
    parser.add_argument("--update-golden", action="store_true")
    parser.add_argument("--audit", action="store_true")
    args = parser.parse_args()

    bench_dir = Path(args.bench_dir).resolve()
    golden_path = Path(args.golden) if args.golden else bench_dir / "extract_golden.jsonl"

    pkl = bench_dir / "extract_corpus.pkl"
    if not pkl.exists():
        raise SystemExit(
            f"no corpus at {pkl}; run `python bench/capture_extract_corpus.py` first"
        )
    with pkl.open("rb") as f:
        corpus = pickle.load(f)

    ok = run_parity(corpus, golden_path, args.update_golden, args.audit)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
