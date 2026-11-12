"""``get_minimal_context_tool`` answers right after a commit the pre-commit hook updated.

The pre-commit hook runs ``update`` before Git makes the commit, so the graph
records the parent commit while it already holds the committed files. The
tool CLAUDE.md tells agents to call first compared commit ids only, and so
said ``stale_graph`` after every normal commit while every other tool
answered.

It now looks at the files: when each file changed between the recorded commit
and HEAD has the same SHA-256 in the graph as on disk, the graph is current.

Found by the developer-experience review of 2.4.0 (product item 4).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import graph_holds_commits
from code_review_graph.tools.build import build_or_update_graph
from code_review_graph.tools.context import get_minimal_context

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
        timeout=30,
    ).stdout.strip()


def _update(repo: Path) -> None:
    """What the pre-commit hook does: an incremental update before the commit."""
    result = build_or_update_graph(
        full_rebuild=False, repo_root=str(repo), base=None, postprocess="none",
    )
    assert result["status"] == "ok", result


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text(".code-review-graph/\n", encoding="utf-8")
    (repo / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (repo / "b.py").write_text("def beta():\n    return 2\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "first")
    build_or_update_graph(full_rebuild=True, repo_root=str(repo), postprocess="none")
    return repo


def _recorded_sha(repo: Path) -> str:
    with GraphStore(repo / ".code-review-graph" / "graph.db") as store:
        return store.get_metadata("git_head_sha") or ""


def test_hooked_commit_leaves_the_graph_fresh(repo: Path) -> None:
    (repo / "a.py").write_text("def alpha():\n    return 10\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    _update(repo)
    _git(repo, "commit", "-q", "-m", "change alpha")

    assert _recorded_sha(repo) != _git(repo, "rev-parse", "HEAD")
    result = get_minimal_context(task="review", repo_root=str(repo))

    assert result["status"] == "ok", result
    assert result.get("reason") is None


def test_hooked_commit_with_a_file_the_graph_does_not_index_is_fresh(repo: Path) -> None:
    (repo / "a.py").write_text("def alpha():\n    return 10\n", encoding="utf-8")
    (repo / "NOTES.unindexed").write_text("notes\n", encoding="utf-8")
    _git(repo, "add", ".")
    _update(repo)
    _git(repo, "commit", "-q", "-m", "change alpha, add notes")

    assert get_minimal_context(repo_root=str(repo))["status"] == "ok"


def test_commit_without_an_update_is_stale(repo: Path) -> None:
    (repo / "a.py").write_text("def alpha():\n    return 10\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "change alpha, no hook")

    result = get_minimal_context(repo_root=str(repo))

    assert (result["status"], result["reason"]) == ("not_ready", "stale_graph")


def test_committed_new_source_file_the_graph_lacks_is_stale(repo: Path) -> None:
    (repo / "c.py").write_text("def gamma():\n    return 3\n", encoding="utf-8")
    _git(repo, "add", "c.py")
    _git(repo, "commit", "-q", "-m", "add gamma, no hook")

    result = get_minimal_context(repo_root=str(repo))

    assert (result["status"], result["reason"]) == ("not_ready", "stale_graph")


def test_committed_deletion_the_graph_still_holds_is_stale(repo: Path) -> None:
    _git(repo, "rm", "-q", "b.py")
    _git(repo, "commit", "-q", "-m", "drop beta, no hook")

    result = get_minimal_context(repo_root=str(repo))

    assert (result["status"], result["reason"]) == ("not_ready", "stale_graph")


def test_hooked_deletion_is_fresh(repo: Path) -> None:
    _git(repo, "rm", "-q", "b.py")
    _update(repo)
    _git(repo, "commit", "-q", "-m", "drop beta")

    assert get_minimal_context(repo_root=str(repo))["status"] == "ok"


@pytest.mark.parametrize("built_at", ["", "-x", "not a ref", "0" * 40])
def test_unusable_recorded_commit_is_not_fresh(repo: Path, built_at: str) -> None:
    with GraphStore(repo / ".code-review-graph" / "graph.db") as store:
        assert graph_holds_commits(repo, store, built_at) is False
