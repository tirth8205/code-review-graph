"""Unit tests for the opt-in multi-worktree MCP router boundary."""

from __future__ import annotations

import signal
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_review_graph.incremental import get_db_path
from code_review_graph.multi_worktree import (
    _MAX_INFLIGHT_REQUESTS,
    MultiWorktreeError,
    MultiWorktreeRouter,
    WorktreeAuthorizer,
    _Child,
    run_multi_worktree_server,
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


def _worktree_pair(tmp_path: Path) -> tuple[Path, Path]:
    repo = _repository(tmp_path)
    worktree = tmp_path / "worktree"
    _git(repo, "worktree", "add", "-q", "-b", "review", str(worktree))
    return repo.resolve(), worktree.resolve()


def _child_env(root: Path, router: MultiWorktreeRouter) -> dict[str, str]:
    child = _Child(router, root)
    with (
        patch.object(router.authorizer, "authorize", return_value=root),
        patch("code_review_graph.multi_worktree.subprocess.Popen") as popen,
    ):
        popen.return_value.stdout = MagicMock(read1=MagicMock(return_value=b""))
        popen.return_value.stderr = MagicMock(readline=MagicMock(return_value=b""))
        child.start(initialize=False)
    return popen.call_args.kwargs["env"]


def test_child_keeps_the_settings_plain_serve_would_see(tmp_path: Path, monkeypatch) -> None:
    repo = _repository(tmp_path)
    router = MultiWorktreeRouter(WorktreeAuthorizer(str(repo)), None, False)
    kept = {
        "CRG_OPENAI_API_KEY": "key",
        "CRG_OPENAI_BASE_URL": "http://localhost:1",
        "CRG_OPENAI_MODEL": "model",
        "CRG_TOOL_TIMEOUT": "9",
        "CRG_MAX_IMPACT_NODES": "7",
        "VOYAGE_API_KEY": "key",
        "GOOGLE_API_KEY": "key",
        "MINIMAX_API_KEY": "key",
        "USERPROFILE": "C:/Users/me",
        "HOMEDRIVE": "C:",
        "HOMEPATH": "/Users/me",
        "HTTPS_PROXY": "http://proxy",
    }
    for key, value in kept.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CRG_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("GIT_DIR", str(tmp_path))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))

    env = _child_env(repo.resolve(), router)

    assert {key: env.get(key) for key in kept} == kept
    assert "CRG_DATA_DIR" not in env
    assert "CRG_REPO_ROOT" not in env
    assert "GIT_DIR" not in env
    assert "GIT_WORK_TREE" not in env


def test_live_child_is_refused_when_its_database_moves(tmp_path: Path, monkeypatch) -> None:
    repo, worktree = _worktree_pair(tmp_path)
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "crg-home"))
    from code_review_graph.registry import Registry

    router = MultiWorktreeRouter(WorktreeAuthorizer(str(repo)), None, False)

    def start_a_live_child(child: _Child, initialize: bool = True) -> None:
        child.process = MagicMock(poll=MagicMock(return_value=None))

    with patch.object(_Child, "start", start_a_live_child):
        child = router.get_child(worktree)
        assert router.get_child(worktree) is child

        Registry().set_data_dir(str(worktree), str(tmp_path / "moved"))

        with pytest.raises(MultiWorktreeError, match="restart this MCP session"):
            router.get_child(worktree)
    router.children.clear()


def test_cancelled_requests_do_not_use_up_the_in_flight_limit(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    router = MultiWorktreeRouter(WorktreeAuthorizer(str(repo)), None, False)
    child = MagicMock()
    child.root = repo.resolve()
    for index in range(_MAX_INFLIGHT_REQUESTS + 5):
        router._forward(child, {"jsonrpc": "2.0", "id": index, "method": "tools/call"})
        router.handle(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": index},
            }
        )
    assert router.routes == {}
    assert child.send.call_count == 2 * (_MAX_INFLIGHT_REQUESTS + 5)

    with patch.object(router, "write") as write:
        router.child_message(child, {"jsonrpc": "2.0", "id": 0, "result": {}})
    write.assert_not_called()


@pytest.mark.parametrize("bad_root", ["/tmp/\x00evil", "relative", "", 7])
def test_bad_repo_root_is_refused_and_the_session_survives(tmp_path: Path, bad_root) -> None:
    repo = _repository(tmp_path)
    router = MultiWorktreeRouter(WorktreeAuthorizer(str(repo)), None, False)
    with patch.object(router, "write") as write:
        router.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "x", "arguments": {"repo_root": bad_root}},
            }
        )
    assert write.call_args.args[0]["error"]["code"] == -32602


def test_slow_child_start_does_not_block_other_worktrees(tmp_path: Path) -> None:
    repo, worktree = _worktree_pair(tmp_path)
    router = MultiWorktreeRouter(WorktreeAuthorizer(str(repo)), None, False)
    primary = MagicMock()
    primary.root = repo
    primary.starting = False
    primary.process.poll.return_value = None
    router.children[repo] = primary
    router.db_paths[repo] = get_db_path(repo, read_only=True).resolve()
    release = threading.Event()
    started = threading.Event()
    forwarded = threading.Event()

    def slow_start(child: _Child, initialize: bool = True) -> None:
        started.set()
        assert release.wait(10)
        child.process = MagicMock(poll=MagicMock(return_value=None))

    def call(request_id: int, root: Path) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": "x", "arguments": {"repo_root": str(root)}},
        }

    real_send = _Child.send

    def record_send(child: _Child, message: dict) -> None:
        forwarded.set()

    try:
        _Child.send = record_send
        with patch.object(_Child, "start", slow_start):
            router.handle(call(1, worktree))
            assert started.wait(10)

            router.handle(call(2, repo))
            primary.send.assert_called_once()
            assert not forwarded.is_set()

            release.set()
            assert forwarded.wait(10)
    finally:
        _Child.send = real_send
        release.set()
    assert '1' in router.routes
    router.children.clear()


def test_signal_handler_does_not_take_a_lock(tmp_path: Path, monkeypatch) -> None:
    """It can interrupt the main thread while that thread holds children_lock."""
    repo = _repository(tmp_path)
    handlers = {}

    def fake_run(router: MultiWorktreeRouter) -> None:
        router.children_lock.acquire()
        try:
            handlers[signal.SIGTERM](signal.SIGTERM, None)
        finally:
            router.children_lock.release()

    monkeypatch.setattr(
        signal, "signal", lambda number, handler: handlers.update({number: handler}),
    )
    monkeypatch.setattr(MultiWorktreeRouter, "run", fake_run)
    monkeypatch.setattr("code_review_graph.multi_worktree.atexit.register", lambda *_: None)

    with pytest.raises(SystemExit) as stopped:
        run_multi_worktree_server(str(repo))
    assert stopped.value.code == 128 + signal.SIGTERM
