"""Thin git subprocess wrappers. All git invocations flow through here so
tests can mock a single interface and the rest of the code stays CLI-free.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


def _run(args: list[str], cwd: Path | None = None, check: bool = True) -> str:
    try:
        result = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            check=check,
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode("utf-8", errors="replace") if e.stderr else ""
        raise GitError(
            f"git {' '.join(args[1:])} failed (exit {e.returncode}): {stderr.strip()}"
        ) from e
    # Decode leniently: binary blobs (e.g. PNGs) go through `git show`
    # too, and we shouldn't die on them.
    return result.stdout.decode("utf-8", errors="replace")


@dataclass
class CommitInfo:
    sha: str
    author_name: str
    author_email: str
    committed_at: str
    subject: str
    body: str


def clone_bare_partial(source: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", "--bare", "--filter=blob:none", source, str(target)])


def remote_url(mirror: Path, remote: str = "origin") -> str | None:
    """Return the configured URL for `<remote>`, or None if absent.

    Used to derive a GitHub commit URL when the configured `source` is
    a local filesystem path (the common dev setup) but the mirror still
    has a real GitHub remote.
    """
    try:
        return _run(
            ["git", "remote", "get-url", remote], cwd=mirror, check=True,
        ).strip() or None
    except GitError:
        return None


# Tolerates the three real source-URL shapes Odoo configs use:
# `git@github.com:org/repo.git`, `https://github.com/org/repo.git`,
# `https://github.com/org/repo`. Anything else (including `/dev/null`
# from test fixtures, or local `~/Dev/src/foo/.git` paths) returns None
# from `github_commit_url` and callers fall back to plain text.
_GITHUB_SOURCE_RE = re.compile(
    r"^(?:git@github\.com:|https?://github\.com/)([^/]+)/([^/]+?)(?:\.git)?/?$"
)


def github_commit_url(source: str, sha: str) -> str | None:
    """GitHub commit page URL derived from a repo source URL, or None
    when `source` isn't a GitHub-shaped URL.
    """
    m = _GITHUB_SOURCE_RE.match(source)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    return f"https://github.com/{owner}/{repo}/commit/{sha}"


def resolve_github_base(source: str, mirror: Path) -> str | None:
    """Best-effort GitHub-shaped source URL for a repo, falling back to
    the mirror's `origin` remote when `source` is a local path (dev
    setups frequently configure `~/Dev/src/odoo/.git` rather than the
    GitHub URL). Returns the *source* URL; pair with `github_commit_url`
    to get the commit page.
    """
    if _GITHUB_SOURCE_RE.match(source):
        return source
    fallback = remote_url(mirror)
    if fallback and _GITHUB_SOURCE_RE.match(fallback):
        return fallback
    return None


def fetch(mirror: Path, branch: str) -> None:
    """Fetch `<branch>` from `origin`, updating only the remote-tracking
    ref `refs/remotes/origin/<branch>`.

    We don't fastforward the local `<branch>` ref because the mirror
    might be a normal working clone with `<branch>` checked out (a
    common dev setup is to point ofd's mirror at `~/Dev/src/<repo>/.git`
    rather than maintain a separate bare clone). Git refuses to update
    a checked-out branch via fetch, which used to break `ofd run`
    entirely. Reading via `origin/<branch>` works in both cases:
    bare mirrors get the remote-tracking ref populated by fetch, and
    working clones already have it.
    """
    _run(
        ["git", "--git-dir", str(mirror), "fetch", "--prune", "origin", branch]
    )


def _resolve_branch_ref(mirror: Path, branch: str) -> str:
    """Pick the ref to read from. Prefers `origin/<branch>` so reads
    are decoupled from whatever the local working copy has checked
    out. Falls back to plain `<branch>` when `origin/<branch>` doesn't
    exist (bare clones built without a configured remote, e.g. test
    fixtures pushed to directly via `git push <bare>`).
    """
    verified = _run(
        ["git", "--git-dir", str(mirror), "rev-parse", "--verify", "-q",
         f"refs/remotes/origin/{branch}"],
        check=False,
    ).strip()
    return f"origin/{branch}" if verified else branch


def head_sha(mirror: Path, branch: str) -> str:
    return _run(
        ["git", "--git-dir", str(mirror), "rev-parse",
         _resolve_branch_ref(mirror, branch)]
    ).strip()


def log_commits(
    mirror: Path,
    branch: str,
    since_sha: str | None = None,
    paths: list[str] | None = None,
) -> list[str]:
    """Return SHAs on `branch`, oldest first, between since_sha (exclusive)
    and branch tip. If since_sha is None, returns all commits touching the
    given paths.
    """
    ref = _resolve_branch_ref(mirror, branch)
    range_spec = f"{since_sha}..{ref}" if since_sha else ref
    args = [
        "git", "--git-dir", str(mirror), "log",
        "--no-merges", "--reverse", "--format=%H",
        range_spec,
    ]
    if paths:
        args.append("--")
        args.extend(paths)
    out = _run(args)
    return [line for line in out.splitlines() if line]


def commit_info(mirror: Path, sha: str) -> CommitInfo:
    # Use NUL-separated format so bodies containing any text are safe.
    fmt = "%H%x00%an%x00%ae%x00%cI%x00%s%x00%b"
    out = _run(
        ["git", "--git-dir", str(mirror), "log", "-1", f"--format={fmt}", sha]
    )
    # %b can contain NULs in theory but git emits none for this combination.
    parts = out.rstrip("\n").split("\x00", 5)
    if len(parts) < 6:
        raise GitError(f"unexpected commit-info output for {sha!r}")
    return CommitInfo(
        sha=parts[0],
        author_name=parts[1],
        author_email=parts[2],
        committed_at=parts[3],
        subject=parts[4],
        body=parts[5].rstrip("\n"),
    )


def changed_files(mirror: Path, sha: str) -> list[str]:
    out = _run(
        [
            "git", "--git-dir", str(mirror),
            "diff-tree", "--no-commit-id", "--name-only", "-r", sha,
        ]
    )
    return [line for line in out.splitlines() if line]


def ls_tree(mirror: Path, sha: str) -> list[str]:
    """Every tracked file path at `sha` (recursive, names only)."""
    out = _run(
        ["git", "--git-dir", str(mirror), "ls-tree", "-r", "--name-only", sha]
    )
    return [line for line in out.splitlines() if line]


def log_commits_with_files(
    mirror: Path,
    branch: str,
    since_sha: str | None = None,
    since_date: str | None = None,
) -> list[tuple[CommitInfo, list[str]]]:
    """Bulk-enumerate (CommitInfo, changed_files) pairs in one git call.

    Uses `git log --name-only` with a sentinel-separated format so parsing
    is unambiguous. Returns commits oldest-first.

    Replaces the per-commit `commit_info` + `changed_files` round-trip
    with one subprocess for the whole branch - `commit_info` showed up
    as ~14.7% of reindex wall time when called per commit.

    `since_date` (e.g. "2025-10-01") bounds the walk to commits whose
    COMMITTER date is on/after that day. git's own `--since` filter
    uses AUTHOR date, which silently drops commits that were authored
    earlier but merged/committed into master afterwards (rebased PRs,
    cherry-picks). We pass git a buffered `--since` (90 days earlier)
    to keep the walk fast, then filter client-side by `%cI`. The
    committer-date semantic matches what we store in `committed_at`
    and what `prune_before` uses, so the walk and the prune agree.
    Combines with `since_sha`: git ANDs the two floors.
    """
    ref = _resolve_branch_ref(mirror, branch)
    range_spec = f"{since_sha}..{ref}" if since_sha else ref
    # \x1e bounds each commit section; git's `%x00` emits a NUL between
    # fields (sent as literal four chars since Python's subprocess
    # rejects real NULs in argv). Both are vanishingly rare in commit
    # messages. Body goes last so embedded newlines don't confuse the
    # file parser - the closing \x1e is our anchor back to structure.
    fmt = "\x1eCOMMIT%x00%H%x00%an%x00%ae%x00%cI%x00%s%x00%b\x1e"
    args = [
        "git", "--git-dir", str(mirror), "log",
        "--no-merges", "--reverse", "--name-only",
        f"--format={fmt}",
    ]
    if since_date:
        # Pass git a 90-day-earlier buffer so rebased/cherry-picked
        # commits (author_date older than committer_date) survive git's
        # author-date-based --since filter. We re-filter below.
        from datetime import date, timedelta
        buffered = (
            date.fromisoformat(since_date) - timedelta(days=90)
        ).isoformat()
        args.append(f"--since={buffered}")
    args.append(range_spec)
    out = _run(args)
    results: list[tuple[CommitInfo, list[str]]] = []
    current_info: CommitInfo | None = None
    current_files: list[str] = []
    for raw in out.split("\x1e"):
        if not raw:
            continue
        if raw.startswith("COMMIT\x00"):
            if current_info:
                results.append((current_info, current_files))
            parts = raw[len("COMMIT\x00"):].split("\x00", 5)
            if len(parts) < 6:
                raise GitError(f"unexpected log entry: {raw[:80]!r}")
            current_info = CommitInfo(
                sha=parts[0],
                author_name=parts[1],
                author_email=parts[2],
                committed_at=parts[3],
                subject=parts[4],
                body=parts[5].rstrip("\n"),
            )
            current_files = []
        else:
            for line in raw.splitlines():
                line = line.strip()
                if line:
                    current_files.append(line)
    if current_info:
        results.append((current_info, current_files))
    if since_date:
        # Committer-date filter: git's --since uses author date, which
        # can leak-out commits whose committer_date >= since_date (the
        # semantic we actually want). Filter here to enforce it.
        results = [
            (info, files) for info, files in results
            if info.committed_at[:10] >= since_date
        ]
    return results


def show_blob(mirror: Path, sha: str, path: str) -> str | None:
    """Return the file's contents at commit sha, or None if not present."""
    try:
        return _run(
            ["git", "--git-dir", str(mirror), "show", f"{sha}:{path}"],
            check=True,
        )
    except GitError:
        return None


class BlobFetcher:
    """Long-lived `git cat-file --batch` process for bulk blob reads.

    One subprocess for the whole run instead of one per file. Cuts the
    wall-clock cost of the rollout-detection stage by ~10x on big repos
    where stage-3 might touch hundreds of non-gated files per commit.

    Use as a context manager; the process is torn down on exit.
    """

    def __init__(self, mirror: Path) -> None:
        self.mirror = mirror
        self._proc: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> BlobFetcher:
        self._proc = subprocess.Popen(
            ["git", "--git-dir", str(self.mirror), "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._proc:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
            self._proc = None

    def fetch(self, sha: str, path: str) -> str | None:
        """Read `<sha>:<path>` blob contents, or None if missing."""
        proc = self._proc
        if not proc or not proc.stdin or not proc.stdout:
            raise GitError("BlobFetcher used outside `with` block")
        proc.stdin.write(f"{sha}:{path}\n".encode())
        proc.stdin.flush()
        header = proc.stdout.readline()
        if not header:
            raise GitError("git cat-file closed unexpectedly")
        if header.rstrip(b"\n").endswith(b"missing"):
            return None
        # Header: "<oid> <type> <size>\n" ; then `size` bytes of content ; then one "\n".
        parts = header.split()
        if len(parts) != 3:
            return None
        try:
            size = int(parts[2])
        except ValueError:
            return None
        data = _read_exact(proc.stdout, size)
        proc.stdout.read(1)  # trailing newline
        return data.decode("utf-8", errors="replace")


def _read_exact(stream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            raise GitError("git cat-file truncated blob")
        buf.extend(chunk)
    return bytes(buf)


def diff_patch(mirror: Path, sha: str, path: str) -> str:
    """Return the unified diff for one file in one commit."""
    return _run(
        [
            "git", "--git-dir", str(mirror),
            "diff-tree", "-p", "-r", "--no-color", sha, "--", path,
        ]
    )


def grep_files(
    mirror: Path,
    sha: str,
    needle: str,
    *,
    pathspec: str | None = None,
) -> list[str]:
    """Files at <sha> whose contents contain literal <needle>.

    Used by the context-key baseline scan to cheaply prune the tree to
    only `.py` files that mention `depends_context` before we AST-parse
    them. `git grep` is dramatically faster than walking blob-by-blob.
    """
    args = ["git", "grep", "--name-only", "-F", needle, sha]
    if pathspec:
        args += ["--", pathspec]
    out = _run(args, cwd=mirror, check=False)
    paths: list[str] = []
    for line in out.splitlines():
        # `git grep` against a SHA prefixes each match with `<sha>:`.
        if line.startswith(f"{sha}:"):
            paths.append(line[len(sha) + 1 :])
        elif line:
            paths.append(line)
    return paths


def commit_at_or_before(
    mirror: Path,
    branch: str,
    before_iso_date: str,
) -> str | None:
    """Last commit on <branch> committed strictly before <before_iso_date>.

    Used to anchor a "tree state at the start of our tracking window" -
    the baseline against which we decide which primitives are actually
    new vs. just newly cited within the window.
    """
    ref = _resolve_branch_ref(mirror, branch)
    out = _run(
        ["git", "log", "-1", f"--before={before_iso_date}", "--format=%H", ref],
        cwd=mirror, check=False,
    )
    return out.strip() or None


def commit_diff_by_file(mirror: Path, sha: str) -> dict[str, str]:
    """Return {file_path: per-file unified diff} for all files in one
    commit. Uses ONE git call and splits on `diff --git` boundaries.

    Orders of magnitude faster than per-file `diff_patch` when a commit
    touches many files - which is most framework-wide refactor commits.
    """
    raw = _run(
        [
            "git", "--git-dir", str(mirror),
            "diff-tree", "-p", "-r", "--no-color", "--no-renames", sha,
        ]
    )
    out: dict[str, str] = {}
    current_file: str | None = None
    buf: list[str] = []
    for line in raw.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current_file:
                out[current_file] = "".join(buf)
            # The commit SHA line (first line of diff-tree output) might
            # precede the first diff header; ignore anything before.
            buf = [line]
            # Parse the b/<path> side.
            parts = line.split()
            current_file = (
                parts[3][2:]
                if len(parts) >= 4 and parts[3].startswith("b/")
                else None
            )
        else:
            buf.append(line)
    if current_file:
        out[current_file] = "".join(buf)
    return out
