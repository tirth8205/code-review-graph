"""A review base Git does not know is an error; the default one in a new repository is not.

``detect-changes --base nope`` used to fall back to ``git status``, review the
working tree and exit 0, with Git's own ``fatal: bad revision`` in a log line.
A CI gate keyed on the exit code passed a review of the wrong files. A base
the user chose now stops the command with exit 1 and a line saying what to
do.

The default base, ``HEAD~1``, cannot resolve in a repository with one commit
or none. That is not the user's mistake, so the review covers uncommitted and
new files and a plain note says so, with no Git text.

New files are reviewed whole: ``git diff`` lists no lines for a file Git does
not track, so a new file used to drop out of the review whenever another file
had changed lines.

Found by the gstack developer-experience review of 2.4.0 (product item 6).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from code_review_graph.errors import ChangeDiscoveryError
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import (
    NO_COMMIT_NOTE,
    ONE_COMMIT_NOTE,
    discover_review_changes,
    full_build,
)
from code_review_graph.tools.review import detect_changes_func

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

EMPTY_TREE_SHA1 = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
        timeout=30,
    ).stdout.strip()


def _init(repo: Path) -> Path:
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text(".code-review-graph/\n", encoding="utf-8")
    return repo


@pytest.fixture
def one_commit(tmp_path: Path) -> Path:
    """One commit, then a tracked edit and a new file."""
    repo = _init(tmp_path / "one")
    (repo / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "first")
    (repo / "app.py").write_text("def handler():\n    return 2\n", encoding="utf-8")
    (repo / "fresh.py").write_text("def brand_new():\n    return 3\n", encoding="utf-8")
    return repo


@pytest.fixture
def two_commits(tmp_path: Path) -> Path:
    repo = _init(tmp_path / "two")
    (repo / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "first")
    (repo / "app.py").write_text("def handler():\n    return 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "second")
    return repo


def _cli(repo: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in ("CRG_DATA_DIR", "CRG_REPO_ROOT")}
    return subprocess.run(
        [sys.executable, "-m", "code_review_graph", *argv],
        cwd=str(repo), capture_output=True, text=True, timeout=120, env=env,
        stdin=subprocess.DEVNULL,
    )


# --- a base the user chose ---------------------------------------------------


@pytest.mark.parametrize(
    "base", ["nope", "origin/missing", "HEAD~5", "nope..HEAD", "--output=x", "", "has space"],
)
def test_unknown_base_is_an_error(two_commits: Path, base: str) -> None:
    notes: list[str] = []
    with pytest.raises(ChangeDiscoveryError) as raised:
        discover_review_changes(two_commits, base, notes=notes)

    message = str(raised.value)
    assert f"base {base!r} is not a known revision" in message
    assert "git branch -a" in message
    assert "fatal" not in message
    assert notes == []


@pytest.mark.parametrize("base", ["HEAD~1", "HEAD", "HEAD~1..HEAD", "HEAD~1...HEAD"])
def test_known_base_is_used(two_commits: Path, base: str) -> None:
    (two_commits / "app.py").write_text("def handler():\n    return 3\n", encoding="utf-8")

    notes: list[str] = []

    changed, _ = discover_review_changes(two_commits, base, notes=notes)

    assert changed == ["app.py"]
    assert notes == []


def test_cli_unknown_base_exits_1_with_one_clear_line(two_commits: Path) -> None:
    assert _cli(two_commits, "build", "-q").returncode == 0

    result = _cli(two_commits, "detect-changes", "--base", "nope")

    assert result.returncode == 1, result
    assert "Error: base 'nope' is not a known revision" in result.stderr
    assert "fatal" not in result.stderr + result.stdout
    assert result.stdout == ""


def test_mcp_unknown_base_is_an_error(two_commits: Path) -> None:
    with GraphStore(two_commits / ".code-review-graph" / "graph.db") as store:
        full_build(two_commits, store)

    result = detect_changes_func(base="nope", repo_root=str(two_commits))

    assert result["status"] == "error"
    assert "not a known revision" in result["error"]


# --- the default base in a new repository ------------------------------------


def test_default_base_in_a_one_commit_repo_reviews_the_working_tree(one_commit: Path) -> None:
    notes: list[str] = []

    changed, base = discover_review_changes(one_commit, notes=notes)

    assert notes == [ONE_COMMIT_NOTE]
    assert base == "HEAD"
    assert set(changed) == {"app.py", "fresh.py"}


def test_default_base_with_no_commits_reviews_the_working_tree(tmp_path: Path) -> None:
    repo = _init(tmp_path / "none")
    (repo / "staged.py").write_text("def staged():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "staged.py")
    (repo / "loose.py").write_text("def loose():\n    return 2\n", encoding="utf-8")
    notes: list[str] = []

    changed, base = discover_review_changes(repo, notes=notes)

    assert notes == [NO_COMMIT_NOTE]
    assert len(base) in (40, 64) and set(base) <= set("0123456789abcdef")
    assert set(changed) >= {"staged.py", "loose.py"}


def test_cli_one_commit_repo_prints_a_plain_note(one_commit: Path) -> None:
    assert _cli(one_commit, "build", "-q").returncode == 0

    result = _cli(one_commit, "detect-changes")

    assert result.returncode == 0, result
    assert (
        "Note: this repository has one commit; reviewing uncommitted and new files instead."
        in result.stderr
    )
    for raw in ("fatal", "bad revision", "git diff failed"):
        assert raw not in result.stderr
    report = json.loads(result.stdout)
    names = {func["name"] for func in report["changed_functions"]}
    assert {"handler", "brand_new"} <= names


def test_mcp_one_commit_repo_says_so_in_the_summary(one_commit: Path) -> None:
    with GraphStore(one_commit / ".code-review-graph" / "graph.db") as store:
        full_build(one_commit, store)

    result = detect_changes_func(repo_root=str(one_commit))

    assert result["status"] == "ok", result
    assert result["summary"].startswith(f"Note: {ONE_COMMIT_NOTE}")


# --- new files are reviewed whole ---------------------------------------------


def test_new_file_is_reviewed_next_to_line_level_changes(two_commits: Path) -> None:
    (two_commits / "fresh.py").write_text("def brand_new():\n    return 3\n", encoding="utf-8")
    with GraphStore(two_commits / ".code-review-graph" / "graph.db") as store:
        full_build(two_commits, store)

    result = detect_changes_func(repo_root=str(two_commits))

    names = {func["name"] for func in result["changed_functions"]}
    assert {"handler", "brand_new"} <= names


def test_unchanged_tracked_file_named_by_the_caller_stays_line_level(two_commits: Path) -> None:
    """Only files Git does not track are taken whole; a tracked file keeps its lines."""
    (two_commits / "other.py").write_text("def other():\n    return 4\n", encoding="utf-8")
    _git(two_commits, "add", "other.py")
    _git(two_commits, "commit", "-q", "-m", "third")
    with GraphStore(two_commits / ".code-review-graph" / "graph.db") as store:
        full_build(two_commits, store)

    result = detect_changes_func(
        base="HEAD~1", changed_files=["other.py", "app.py"], repo_root=str(two_commits),
    )

    names = {func["name"] for func in result["changed_functions"]}
    assert "other" in names
    assert "handler" not in names
