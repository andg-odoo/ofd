"""Capture a corpus of (file, parent_src, child_src) tuples for the
extraction bench.

The set of commits we keep is deliberately wider than the rollout
corpus: framework-path commits exercise the existing python_ / rng
extractors, but a context-key extractor wants to see every commit that
touches `@api.depends_context(...)` calls - those decorators almost
always live in addons (outside `framework_paths`), so we gate by
diff-text signal instead.

Output:
  bench/extract_corpus.pkl - list[ExtractEntry]

Run once per extraction-bench cycle. Pickle is gitignored.
"""

from __future__ import annotations

import argparse
import pickle
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ofd import config as config_mod  # noqa: E402
from ofd import gitio  # noqa: E402
from ofd.globs import match_any  # noqa: E402

# Patch-text needles. If any appear in a commit's full diff, we keep
# every changed .py file from that commit (parent + child source) so
# the bench can reproduce the future extractor's input. Cheap to extend
# when we add another extractor (e.g. `@http.route`, `@api.depends`).
_DIFF_NEEDLES = ("depends_context",)


@dataclass
class ExtractEntry:
    repo: str
    sha: str
    committed_at: str
    subject: str
    file: str
    parent_src: str | None  # None = file added in this commit
    child_src: str | None   # None = file deleted in this commit


def _matches_extension_kept(path: str) -> bool:
    return path.endswith((".py", ".rng"))


def capture(
    workspace: Path,
    out_dir: Path,
    limit: int | None = None,
) -> None:
    cfg = config_mod.load(workspace)
    out_entries: list[ExtractEntry] = []
    print(f"[capture] since={cfg.since_date}")

    for repo in cfg.repos:
        print(f"[capture] enumerating {repo.name} ...")
        t0 = time.perf_counter()
        log = gitio.log_commits_with_files(
            mirror=repo.mirror, branch=repo.branch, since_date=cfg.since_date,
        )
        print(f"[capture]   {len(log)} commits in {time.perf_counter() - t0:.1f}s")

        with gitio.BlobFetcher(repo.mirror) as blobs:
            for idx, (info, files) in enumerate(log):
                if limit is not None and idx >= limit:
                    break
                # Cheap path: any framework-gated file in this commit?
                framework_files = [
                    f for f in files
                    if _matches_extension_kept(f)
                    and match_any(f, repo.framework_paths)
                ]
                # Wider path: signal-bearing diff that the context-key
                # extractor will care about (lives mostly in addons).
                needs_widescan = False
                if any(_matches_extension_kept(f) for f in files):
                    patches = gitio.commit_diff_by_file(repo.mirror, info.sha)
                    full_diff = "\n".join(patches.values())
                    needs_widescan = any(n in full_diff for n in _DIFF_NEEDLES)

                if not framework_files and not needs_widescan:
                    continue

                # Pick the file set we'll capture. Framework files always;
                # for widescan, include any .py file whose patch contains
                # a needle - keeps the corpus to a useful subset.
                kept: set[str] = set(framework_files)
                if needs_widescan:
                    for f, patch_text in patches.items():
                        if not _matches_extension_kept(f):
                            continue
                        if any(n in patch_text for n in _DIFF_NEEDLES):
                            kept.add(f)

                for file in sorted(kept):
                    parent_src = blobs.fetch(f"{info.sha}^", file)
                    child_src = blobs.fetch(info.sha, file)
                    if parent_src is None and child_src is None:
                        continue
                    out_entries.append(ExtractEntry(
                        repo=repo.name,
                        sha=info.sha,
                        committed_at=info.committed_at,
                        subject=info.subject,
                        file=file,
                        parent_src=parent_src,
                        child_src=child_src,
                    ))

                if (idx + 1) % 500 == 0:
                    print(
                        f"[capture]   {repo.name}: {idx + 1}/{len(log)} "
                        f"commits, {len(out_entries)} entries"
                    )

    print(f"[capture] done: {len(out_entries)} entries")
    out_dir.mkdir(parents=True, exist_ok=True)
    pkl = out_dir / "extract_corpus.pkl"
    with pkl.open("wb") as f:
        pickle.dump(out_entries, f, protocol=pickle.HIGHEST_PROTOCOL)
    size_mb = pkl.stat().st_size / (1024 * 1024)
    print(f"[capture] wrote {pkl} ({size_mb:.1f} MB)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--out", default="bench")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    workspace = config_mod.resolve_workspace(args.workspace)
    out_dir = Path(args.out).resolve()
    capture(workspace, out_dir, limit=args.limit)


if __name__ == "__main__":
    main()
