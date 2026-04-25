"""Audit golden-vs-current drops: bucket by (entry.kind, generic?, source)."""
from __future__ import annotations

import json
import pickle
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ofd.rollouts import _GENERIC_SHORT_NAMES, detect_rollouts  # noqa: E402
from ofd.watchlist import Watchlist  # noqa: E402

from capture_corpus import CorpusEntry  # noqa: E402,F401


def key(d):
    return (d.get("repo"), d.get("sha"), d.get("file"), d.get("line"),
            d.get("symbol"), d.get("hunk_header"))


def main():
    bench = Path(__file__).parent
    with (bench / "corpus.pkl").open("rb") as f:
        corpus = pickle.load(f)
    with (bench / "watchlist.json").open() as f:
        wl = Watchlist.from_dict(json.load(f))
    by_sym = {e.symbol: e for e in wl.entries.values()}

    golden = []
    with (bench / "golden.jsonl").open() as f:
        for line in f:
            if line.strip():
                golden.append(json.loads(line))

    current = []
    for entry in corpus:
        for r in detect_rollouts(entry.patches, wl):
            d = r.to_dict()
            d["repo"] = entry.repo
            d["sha"] = entry.sha
            current.append(d)

    gset = {key(r) for r in golden}
    cset = {key(r) for r in current}
    missing = gset - cset
    extra = cset - gset
    print(f"golden={len(golden)} current={len(current)} "
          f"dropped={len(missing)} added={len(extra)}")

    buckets = Counter()
    samples = {}
    for r in golden:
        if key(r) not in missing:
            continue
        sym = r.get("symbol")
        e = by_sym.get(sym)
        if e is None:
            bucket = ("?-no-entry", "?", "?", sym[:30] if sym else "?")
        else:
            bucket = (
                e.kind.value,
                "generic" if e.short_name in _GENERIC_SHORT_NAMES else "specific",
                e.source,
                e.short_name[:20],
            )
        buckets[bucket] += 1
        samples.setdefault(bucket, []).append(r)

    print("\ndropped buckets (kind | generic? | source | short_name):")
    for b, n in buckets.most_common():
        print(f"  {n:4d}  {b}")

    extra_buckets = Counter()
    extra_samples = {}
    for r in current:
        if key(r) not in extra:
            continue
        sym = r.get("symbol")
        e = by_sym.get(sym)
        if e is None:
            bucket = ("?-no-entry", "?", "?", sym[:30] if sym else "?")
        else:
            bucket = (
                e.kind.value,
                "generic" if e.short_name in _GENERIC_SHORT_NAMES else "specific",
                e.source,
                e.short_name[:20],
            )
        extra_buckets[bucket] += 1
        extra_samples.setdefault(bucket, []).append(r)

    print("\nadded buckets (kind | generic? | source | short_name):")
    for b, n in extra_buckets.most_common():
        print(f"  {n:4d}  {b}")

    if "--samples" in sys.argv:
        print("\nsample dropped records per bucket:")
        for b, rs in samples.items():
            r = rs[0]
            print(f"\n--- DROP {b}  ({len(rs)}) ---")
            print(f"  file:   {r.get('file')}:{r.get('line')}")
            print(f"  hunk:   {r.get('hunk_header')}")
            print(f"  added:")
            for ln in (r.get('after_snippet') or '').splitlines()[:8]:
                print(f"    | {ln}")
        print("\nsample added records per bucket:")
        for b, rs in extra_samples.items():
            r = rs[0]
            print(f"\n--- ADD {b}  ({len(rs)}) ---")
            print(f"  file:   {r.get('file')}:{r.get('line')}")
            print(f"  hunk:   {r.get('hunk_header')}")
            print(f"  added:")
            for ln in (r.get('after_snippet') or '').splitlines()[:8]:
                print(f"    | {ln}")


if __name__ == "__main__":
    main()
