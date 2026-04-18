"""Minimal fake-repo builder for pipeline tests.

Creates a working repo + a bare clone that tests can point the tool at.
No network, no partial clone filter, just enough to exercise gitio.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


def _run(args: list[str], cwd: Path) -> str:
    r = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)
    return r.stdout


@dataclass
class FakeRepo:
    work: Path       # working tree
    bare: Path       # bare clone (what the tool reads)
    branch: str = "master"

    def commit(
        self,
        files: dict[str, str | None],
        subject: str,
        body: str = "",
        author: str = "Test User <test@example.com>",
    ) -> str:
        """Write/delete the given files and commit. Returns the commit SHA."""
        for rel, content in files.items():
            path = self.work / rel
            if content is None:
                if path.exists():
                    path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        _run(["git", "add", "-A"], self.work)
        name, email = _parse_author(author)
        env_args = [
            "-c", f"user.name={name}",
            "-c", f"user.email={email}",
            "-c", "commit.gpgsign=false",
        ]
        message = subject if not body else f"{subject}\n\n{body}"
        _run(
            ["git", *env_args, "commit", "-m", message, "--allow-empty"],
            self.work,
        )
        sha = _run(["git", "rev-parse", "HEAD"], self.work).strip()
        # Push into the bare clone (plain, non-filtered) so consumers see it.
        _run(["git", "push", str(self.bare), f"HEAD:{self.branch}"], self.work)
        return sha


def _parse_author(s: str) -> tuple[str, str]:
    name, _, rest = s.partition("<")
    email = rest.rstrip(">").strip()
    return name.strip(), email


def make_repo(tmp: Path, name: str = "odoo") -> FakeRepo:
    work = tmp / f"{name}-work"
    bare = tmp / f"{name}.git"
    work.mkdir()
    _run(["git", "init", "-q", "-b", "master"], work)
    _run(["git", "init", "-q", "--bare", str(bare)], tmp)
    return FakeRepo(work=work, bare=bare)
