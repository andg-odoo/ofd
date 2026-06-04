"""JS adoption-matcher bench - the DESIGN-js.md phase 2 gate.

Scans the real mirrors for every commit touching .js since the config
floor, runs `detect_rollouts` with the live workspace watchlist over
the .js patches (framework/definition paths excluded, mirroring the
pipeline's non-gated scan), and reports:

- **must-not-match regression**: zero rollouts in .js files attributed
  to a non-JS-kind entry. This is the empirical proof, over the full
  corpus, that the historical `PropertiesDefinition.setup` /
  `Transaction.cache` cross-language FP class cannot recur.
- **recall probes**: the hand-verified adoption stories - localeCompare
  barrel imports (defining module != from-string) and any other
  symbol passed via --probe.
- **labeling output**: every JS rollout written to
  bench/js_adoptions.jsonl (repo, sha, file, symbol, matched import
  line) so true-adoption labels can be assigned by hand.

Usage:
  python bench/bench_js.py --workspace ~/ofd-workspace
  python bench/bench_js.py --workspace ~/ofd-workspace --probe localeCompare
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ofd import config as config_mod  # noqa: E402
from ofd import gitio  # noqa: E402
from ofd import watchlist as watchlist_mod  # noqa: E402
from ofd.globs import match_any  # noqa: E402
from ofd.rollouts import (  # noqa: E402
    _KIND_LANGUAGES,
    Language,
    _js_import_pattern,
    detect_rollouts,
)

# Kinds allowed to roll out on the JS/QWeb surface: JS exports (import
# lines + component tags) and QWEB-capable attr needles. Anything else
# firing in a .js / static/src .xml file is a cross-language regression.
JS_KINDS = frozenset(
    k for k, langs in _KIND_LANGUAGES.items()
    if Language.JS in langs or Language.QWEB in langs
)


def _matched_import_lines(patch: str, short: str, is_xml: bool) -> list[str]:
    """The added lines the matcher anchored on, for the label file."""
    added = "\n".join(
        line[1:] for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    if is_xml:
        # Component tag or attr needle - show the whole line, capped.
        n = re.escape(short)
        pat = re.compile(rf"^.*(?:<{n}\b|\b{n}\s*=).*$", re.MULTILINE)
        return sorted({m.group(0).strip()[:160] for m in pat.finditer(added)})
    pat = _js_import_pattern(short)
    return sorted({m.group(0).strip() for m in pat.finditer(added)})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--probe", action="append", default=[],
                        help="short name whose rollout count to report")
    parser.add_argument("--out", default=None,
                        help="label file (default: <bench>/js_adoptions.jsonl)")
    args = parser.parse_args()

    workspace = Path(args.workspace).expanduser()
    config = config_mod.load(workspace)
    wl = watchlist_mod.load(workspace)
    js_entries = {
        s: e for s, e in wl.entries.items() if e.kind in JS_KINDS
    }
    print(f"[bench-js] watchlist: {len(wl.entries)} total, "
          f"{len(js_entries)} JS-kind")

    out_path = (
        Path(args.out) if args.out
        else Path(__file__).resolve().parent / "js_adoptions.jsonl"
    )

    by_symbol: Counter[str] = Counter()
    cross_language_fps: list[dict] = []
    adoptions: list[dict] = []
    t0 = time.perf_counter()

    for repo in config.repos:
        commits = gitio.log_commits_with_files(
            repo.mirror, repo.branch, since_date=config.since_date,
        )
        scanned = 0
        for info, files in commits:
            js_files = [
                f for f in files
                if (
                    f.endswith(".js")
                    # QWeb templates (phase 3): component-tag and attr
                    # needle adoption in OWL templates.
                    or (f.endswith(".xml") and "/static/src/" in f)
                )
                and not match_any(f, repo.framework_paths)
            ]
            if not js_files:
                continue
            all_patches = gitio.commit_diff_by_file(repo.mirror, info.sha)
            patches = {f: all_patches[f] for f in js_files if f in all_patches}
            if not patches:
                continue
            scanned += 1
            for r in detect_rollouts(patches, wl):
                entry = wl.entries.get(r.symbol)
                row = {
                    "repo": repo.name,
                    "sha": info.sha,
                    "date": info.committed_at,
                    "subject": info.subject,
                    "file": r.file,
                    "symbol": r.symbol,
                    "import_lines": _matched_import_lines(
                        patches[r.file],
                        entry.short_name if entry else r.symbol.rsplit(".", 1)[-1],
                        r.file.endswith(".xml"),
                    ),
                }
                if entry is None or entry.kind not in JS_KINDS:
                    cross_language_fps.append(row)
                    continue
                by_symbol[r.symbol] += 1
                adoptions.append(row)
        print(f"[bench-js] {repo.name}: scanned {scanned} JS-touching "
              f"commits of {len(commits)}")

    elapsed = time.perf_counter() - t0
    print(f"[bench-js] {len(adoptions)} JS rollouts across "
          f"{len(by_symbol)} symbols in {elapsed:.0f}s")

    with out_path.open("w") as f:
        for row in adoptions:
            f.write(json.dumps(row, sort_keys=True) + "\n")
    print(f"[bench-js] wrote {out_path}")

    print("[bench-js] top symbols:")
    for symbol, n in by_symbol.most_common(20):
        print(f"  {n:4d}  {symbol}")

    for probe in args.probe:
        probe_total = sum(
            n for s, n in by_symbol.items()
            if re.search(rf"(?:^|\.){re.escape(probe)}$", s)
        )
        marker = "OK " if probe_total else "MISS"
        print(f"[bench-js] probe {marker} {probe}: {probe_total} rollouts")

    if cross_language_fps:
        print(f"[bench-js] REGRESSION: {len(cross_language_fps)} rollouts "
              f"in .js attributed to non-JS-kind entries:")
        for row in cross_language_fps[:10]:
            print(f"  {row['repo']} {row['sha'][:10]} {row['file']} "
                  f"-> {row['symbol']}")
        sys.exit(1)
    print("[bench-js] must-not-match: OK - zero non-JS-kind rollouts in .js")


if __name__ == "__main__":
    main()
