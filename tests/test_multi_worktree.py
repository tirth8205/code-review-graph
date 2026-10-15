"""Unit tests for the opt-in multi-worktree MCP router boundary."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_review_graph.multi_worktree import (
    MultiWorktreeError,
    MultiWorktreeRouter,
    WorktreeAuthorizer,
    _Child,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(cwd),
            "-c",
            "user.name=CRG test",
            "-c",
            "user.email=crg-test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def test_authorizer_accepts_linked_worktree_and_rejects_other_repo(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    worktree = tmp_path / "worktree"
    _git(repo, "worktree", "add", "-q", "-b", "review", str(worktree))
    other = _repository(tmp_path / "other-root")

    authorizer = WorktreeAuthorizer(str(repo))

    assert authorizer.authorize(str(repo)) == repo.resolve()
    assert authorizer.authorize(str(worktree)) == worktree.resolve()
    with pytest.raises(MultiWorktreeError, match="linked worktree"):
        authorizer.authorize(str(other))


def test_authorizer_rejects_relative_and_missing_roots(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    authorizer = WorktreeAuthorizer(str(repo))

    with pytest.raises(MultiWorktreeError, match="absolute"):
        authorizer.authorize("relative-worktree")
    with pytest.raises(MultiWorktreeError, match="not a directory"):
        authorizer.authorize(str(tmp_path / "missing"))


def test_router_rejects_registry_database_aliasing(tmp_path: Path, monkeypatch) -> None:
    repo = _repository(tmp_path)
    worktree = tmp_path / "worktree"
    _git(repo, "worktree", "add", "-q", "-b", "review", str(worktree))
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "crg-home"))

    from code_review_graph.registry import Registry

    shared = tmp_path / "shared-graph"
    registry = Registry()
    registry.set_data_dir(str(repo), str(shared))
    registry.set_data_dir(str(worktree), str(shared))

    router = MultiWorktreeRouter(WorktreeAuthorizer(str(repo)), None, False)
    with patch.object(_Child, "start"):
        router.get_child(repo)
        with pytest.raises(MultiWorktreeError, match="share a graph database"):
            router.get_child(worktree)
    router.close()


def test_router_routes_cancellation_to_request_child(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    router = MultiWorktreeRouter(WorktreeAuthorizer(str(repo)), None, False)
    primary = MagicMock()
    secondary = MagicMock()
    primary.root = repo
    secondary.root = tmp_path / "secondary"
    primary.pending_lock = threading.Lock()
    secondary.pending_lock = threading.Lock()
    primary.pending = {}
    secondary.pending = {}
    router.children = {repo: primary, secondary.root: secondary}
    router.routes["\"worktree-request\""] = (secondary, "worktree-request")

    router.handle(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": "worktree-request"},
        }
    )

    secondary.send.assert_called_once()
    primary.send.assert_not_called()


def test_router_returns_child_request_response_to_origin(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    router = MultiWorktreeRouter(WorktreeAuthorizer(str(repo)), None, False)
    child = MagicMock()
    child.root = repo
    with patch.object(router, "write") as write:
        router.child_message(
            child,
            {"jsonrpc": "2.0", "id": 7, "method": "roots/list", "params": {}},
        )
        parent_message = write.call_args.args[0]

    router.handle(
        {
            "jsonrpc": "2.0",
            "id": parent_message["id"],
            "result": {"roots": []},
        }
    )

    child.send.assert_called_once_with(
        {"jsonrpc": "2.0", "id": 7, "result": {"roots": []}}
    )
