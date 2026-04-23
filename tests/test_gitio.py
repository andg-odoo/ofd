from pathlib import Path

from ofd import gitio
from tests.fixtures.repo_builder import make_repo


def test_log_commits_since(tmp_path: Path):
    repo = make_repo(tmp_path)
    s1 = repo.commit({"a.py": "x = 1\n"}, "[ADD] a")
    s2 = repo.commit({"b.py": "y = 2\n"}, "[ADD] b")
    s3 = repo.commit({"c.py": "z = 3\n"}, "[ADD] c")

    all_shas = gitio.log_commits(repo.bare, "master")
    assert all_shas == [s1, s2, s3]

    since_s1 = gitio.log_commits(repo.bare, "master", since_sha=s1)
    assert since_s1 == [s2, s3]


def test_log_commits_filters_by_path(tmp_path: Path):
    repo = make_repo(tmp_path)
    s1 = repo.commit({"odoo/fields.py": "# framework\n"}, "[ADD] fields")
    s2 = repo.commit({"addons/sale/foo.py": "# addon\n"}, "[ADD] sale")

    only_framework = gitio.log_commits(
        repo.bare, "master", paths=["odoo/fields.py"]
    )
    assert only_framework == [s1]
    assert s2 not in only_framework


def test_commit_info_parses_body(tmp_path: Path):
    repo = make_repo(tmp_path)
    sha = repo.commit(
        {"a.py": "x=1\n"},
        "[ADD] base: do a thing",
        body="Because reasons.\n\nWith a second paragraph.",
        author="Jane <jane@example.com>",
    )
    info = gitio.commit_info(repo.bare, sha)
    assert info.sha == sha
    assert info.author_name == "Jane"
    assert info.author_email == "jane@example.com"
    assert info.subject == "[ADD] base: do a thing"
    assert "second paragraph" in info.body


def test_log_commits_with_files_returns_info_and_files(tmp_path: Path):
    """Bulk enumeration should give us both per-commit metadata and the
    file list in one subprocess, so the pipeline doesn't need to call
    `commit_info` separately for each commit."""
    repo = make_repo(tmp_path)
    s1 = repo.commit(
        {"a.py": "x = 1\n"},
        "[ADD] base: first",
        body="With body.",
        author="Alice <alice@example.com>",
    )
    s2 = repo.commit(
        {"b.py": "y = 2\n", "c.py": "z = 3\n"},
        "[IMP] base: second",
        author="Bob <bob@example.com>",
    )
    rows = gitio.log_commits_with_files(repo.bare, "master")
    assert len(rows) == 2
    first_info, first_files = rows[0]
    assert first_info.sha == s1
    assert first_info.author_name == "Alice"
    assert first_info.subject == "[ADD] base: first"
    assert "With body." in first_info.body
    assert first_files == ["a.py"]

    second_info, second_files = rows[1]
    assert second_info.sha == s2
    assert second_info.author_email == "bob@example.com"
    assert set(second_files) == {"b.py", "c.py"}


def test_changed_files_and_show_blob(tmp_path: Path):
    repo = make_repo(tmp_path)
    repo.commit({"a.py": "x = 1\n"}, "[ADD] a")
    sha = repo.commit(
        {"a.py": "x = 2\n", "b.py": "y = 0\n"},
        "[IMP] a, add b",
    )
    files = gitio.changed_files(repo.bare, sha)
    assert set(files) == {"a.py", "b.py"}

    assert gitio.show_blob(repo.bare, sha, "a.py") == "x = 2\n"
    assert gitio.show_blob(repo.bare, f"{sha}^", "a.py") == "x = 1\n"
    assert gitio.show_blob(repo.bare, f"{sha}^", "b.py") is None


def test_head_sha(tmp_path: Path):
    repo = make_repo(tmp_path)
    sha = repo.commit({"a.py": "x=1\n"}, "[ADD] a")
    assert gitio.head_sha(repo.bare, "master") == sha


def test_blob_fetcher_reads_multiple_blobs(tmp_path: Path):
    repo = make_repo(tmp_path)
    s1 = repo.commit({"a.py": "x = 1\n"}, "[ADD] a")
    s2 = repo.commit({"a.py": "x = 2\n", "b.py": "y = 0\n"}, "[IMP]")
    with gitio.BlobFetcher(repo.bare) as fetcher:
        assert fetcher.fetch(s2, "a.py") == "x = 2\n"
        assert fetcher.fetch(s1, "a.py") == "x = 1\n"
        assert fetcher.fetch(s2, "b.py") == "y = 0\n"
        assert fetcher.fetch(s1, "b.py") is None


def test_blob_fetcher_handles_binary(tmp_path: Path):
    repo = make_repo(tmp_path)
    sha = repo.commit({"a.bin": "\x00\x01binary\x02\x00"}, "[ADD] binary")
    with gitio.BlobFetcher(repo.bare) as fetcher:
        got = fetcher.fetch(sha, "a.bin")
        assert got is not None
        assert "binary" in got
