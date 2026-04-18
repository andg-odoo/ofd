"""Thin git subprocess wrappers. All git invocations flow through here so
tests can mock a single interface and the rest of the code stays CLI-free.
"""

from __future__ import annotations

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


def fetch(mirror: Path, branch: str) -> None:
    _run(
        [
            "git", "--git-dir", str(mirror), "fetch", "--prune", "origin",
            f"{branch}:{branch}",
        ]
    )


def head_sha(mirror: Path, branch: str) -> str:
    return _run(
        ["git", "--git-dir", str(mirror), "rev-parse", branch]
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
    range_spec = f"{since_sha}..{branch}" if since_sha else branch
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


def log_commits_with_files(
    mirror: Path,
    branch: str,
    since_sha: str | None = None,
) -> list[tuple[str, list[str]]]:
    """Bulk-enumerate (sha, changed_files) pairs in one git call.

    Uses `git log --name-only -z` with a sentinel format line so parsing
    is unambiguous. Returns commits oldest-first.

    Orders of magnitude faster than calling `log_commits` + `changed_files`
    per commit when you need both: one git process instead of N+1.
    """
    range_spec = f"{since_sha}..{branch}" if since_sha else branch
    fmt = "\x1eCOMMIT\x1f%H\x1e"
    out = _run(
        [
            "git", "--git-dir", str(mirror), "log",
            "--no-merges", "--reverse", "--name-only",
            f"--format={fmt}",
            range_spec,
        ]
    )
    results: list[tuple[str, list[str]]] = []
    current_sha: str | None = None
    current_files: list[str] = []
    for raw in out.split("\x1e"):
        if not raw:
            continue
        if raw.startswith("COMMIT\x1f"):
            if current_sha:
                results.append((current_sha, current_files))
            current_sha = raw[len("COMMIT\x1f"):].strip()
            current_files = []
        else:
            for line in raw.splitlines():
                line = line.strip()
                if line:
                    current_files.append(line)
    if current_sha:
        results.append((current_sha, current_files))
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
