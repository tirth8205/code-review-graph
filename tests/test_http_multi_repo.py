"""One ``serve --http --multi-repo`` server answers each call from the repo it names.

Issue #607. A shared HTTP server used to fall back to ``--repo`` or its own
working directory when a call left out ``repo_root``, so a client could be
answered from a repository it never asked about. With ``--multi-repo`` every
call must name a repository from the registry; a missing, relative or
unregistered ``repo_root`` is refused with a clear message instead.

The first tests pin the routing rule in-process. The last ones start a real
``serve --http --multi-repo`` server and drive it with the ``mcp`` client over
Streamable HTTP, building and querying two repositories at the same time.
stdio and plain ``serve --http`` keep their old fallback.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastmcp.exceptions import ToolError

from code_review_graph import cli
from code_review_graph import main as crg_main
from code_review_graph.registry import Registry

REPO_SOURCE_ROOT = Path(__file__).resolve().parents[1]
CALL_TIMEOUT = 120.0


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.name=CRG Test",
            "-c", "user.email=test@example.invalid",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=CALL_TIMEOUT,
    )


def _make_repo(parent: Path, name: str, symbol: str) -> Path:
    """A small real Git repository whose only function is ``symbol``."""
    repo = parent / name
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / f"{symbol}.py").write_text(
        f"def {symbol}():\n    return {symbol!r}\n",
        encoding="utf-8",
    )
    _git(parent, "init", "-q", "-b", "main", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial commit")
    return repo.resolve()


@pytest.fixture
def multi_repo_mode(monkeypatch, tmp_path):
    """Turn strict routing on with an empty registry under a temp CRG_HOME."""
    home = tmp_path / "crg-home"
    home.mkdir()
    monkeypatch.setenv("CRG_HOME", str(home))
    monkeypatch.setattr(crg_main, "_multi_repo", True)
    monkeypatch.setattr(crg_main, "_default_repo_root", None)
    return Registry(home / "registry.json")


def _project(parent: Path, name: str) -> Path:
    root = parent / name
    (root / ".git").mkdir(parents=True)
    return root.resolve()


class TestStrictRouting:
    def test_a_call_naming_no_repo_is_refused(self, multi_repo_mode, tmp_path):
        multi_repo_mode.register(str(_project(tmp_path, "a")))
        for missing in (None, ""):
            with pytest.raises(ToolError, match="repo_root is required"):
                crg_main._resolve_repo_root(missing)

    def test_no_fallback_to_the_server_directory(
        self, multi_repo_mode, tmp_path, monkeypatch,
    ):
        repo = _project(tmp_path, "a")
        multi_repo_mode.register(str(repo))
        monkeypatch.chdir(repo)
        monkeypatch.setenv("CRG_REPO_ROOT", str(repo))
        with pytest.raises(ToolError, match="repo_root is required"):
            crg_main._resolve_repo_root(None)

    def test_a_registered_repo_is_served(self, multi_repo_mode, tmp_path):
        a = _project(tmp_path, "a")
        b = _project(tmp_path, "b")
        multi_repo_mode.register(str(a))
        multi_repo_mode.register(str(b), alias="bee")
        assert crg_main._resolve_repo_root(str(a)) == str(a)
        assert crg_main._resolve_repo_root(str(b)) == str(b)

    def test_an_unregistered_repo_is_refused(self, multi_repo_mode, tmp_path):
        multi_repo_mode.register(str(_project(tmp_path, "a")))
        stranger = _project(tmp_path, "stranger")
        with pytest.raises(ToolError, match="not a registered repository"):
            crg_main._resolve_repo_root(str(stranger))

    def test_a_relative_path_is_refused(self, multi_repo_mode, tmp_path, monkeypatch):
        repo = _project(tmp_path, "a")
        multi_repo_mode.register(str(repo))
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ToolError, match="absolute path"):
            crg_main._resolve_repo_root("a")

    def test_a_subdirectory_of_a_registered_repo_is_refused(
        self, multi_repo_mode, tmp_path,
    ):
        repo = _project(tmp_path, "a")
        (repo / "pkg" / ".git").mkdir(parents=True)
        multi_repo_mode.register(str(repo))
        with pytest.raises(ToolError, match="not a registered repository"):
            crg_main._resolve_repo_root(str(repo / "pkg"))

    def test_dot_dot_that_lands_on_a_registered_repo_is_served(
        self, multi_repo_mode, tmp_path,
    ):
        repo = _project(tmp_path, "a")
        multi_repo_mode.register(str(repo))
        assert crg_main._resolve_repo_root(str(repo / "pkg" / "..")) == str(repo)

    def test_an_alias_is_not_a_path(self, multi_repo_mode, tmp_path):
        multi_repo_mode.register(str(_project(tmp_path, "a")), alias="api")
        with pytest.raises(ToolError, match="absolute path"):
            crg_main._resolve_repo_root("api")

    def test_a_path_that_cannot_be_resolved_is_refused(self, multi_repo_mode):
        with pytest.raises(ToolError, match="repo_root"):
            crg_main._resolve_repo_root(os.path.abspath("bad\x00path"))

    def test_the_registry_is_read_on_every_call(self, multi_repo_mode, tmp_path):
        repo = _project(tmp_path, "late")
        with pytest.raises(ToolError, match="not a registered repository"):
            crg_main._resolve_repo_root(str(repo))
        Registry(multi_repo_mode._path).register(str(repo))
        assert crg_main._resolve_repo_root(str(repo)) == str(repo)
        Registry(multi_repo_mode._path).unregister(str(repo))
        with pytest.raises(ToolError, match="not a registered repository"):
            crg_main._resolve_repo_root(str(repo))


class TestDefaultModeIsUnchanged:
    """stdio and plain ``serve --http`` keep the ``--repo`` / cwd fallback."""

    def test_missing_repo_root_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setattr(crg_main, "_multi_repo", False)
        monkeypatch.setattr(crg_main, "_default_repo_root", "/srv/flag-repo")
        assert crg_main._resolve_repo_root(None) == "/srv/flag-repo"
        assert crg_main._resolve_repo_root("relative") == "relative"


class TestServeMainOptions:
    def _fake_run(self, monkeypatch) -> list[dict]:
        calls: list[dict] = []

        def fake_run(**kwargs):
            calls.append({
                **kwargs,
                "multi_repo": crg_main._multi_repo,
                "default_root": crg_main._default_repo_root,
                "instructions": crg_main.mcp.instructions,
            })

        monkeypatch.setattr(crg_main.mcp, "run", fake_run)
        monkeypatch.setattr(crg_main, "_default_repo_root", None)
        # main() applies CRG_TOOLS to the shared mcp object for good, and
        # refuses CRG_DATA_DIR in multi_repo mode: keep both out of the test.
        monkeypatch.delenv("CRG_TOOLS", raising=False)
        monkeypatch.delenv("CRG_DATA_DIR", raising=False)
        return calls

    def test_http_multi_repo_runs_with_no_default_repo(self, monkeypatch, tmp_path):
        calls = self._fake_run(monkeypatch)
        monkeypatch.chdir(_project(tmp_path, "cwd-repo"))
        crg_main.main(
            transport="streamable-http", host="127.0.0.1", port=5555,
            multi_repo=True,
        )
        assert len(calls) == 1
        assert calls[0]["transport"] == "streamable-http"
        assert calls[0]["multi_repo"] is True
        assert calls[0]["default_root"] is None
        assert "repo_root" in calls[0]["instructions"]
        assert "list_repos_tool" in calls[0]["instructions"]
        # Claude Code cuts server instructions off past 2,048 characters.
        assert len(calls[0]["instructions"]) <= 2048
        # Process-wide state is put back for whatever runs next.
        assert crg_main._multi_repo is False
        assert crg_main.mcp.instructions == crg_main.SERVER_INSTRUCTIONS

    @pytest.mark.parametrize(
        "kwargs, message",
        [
            pytest.param({"transport": "stdio"}, "--http", id="stdio"),
            pytest.param(
                {"transport": "streamable-http", "host": "127.0.0.1", "port": 5555,
                 "repo_root": "/srv/a"},
                "registry", id="repo-root",
            ),
            pytest.param(
                {"transport": "streamable-http", "host": "127.0.0.1", "port": 5555,
                 "auto_watch": True},
                "daemon", id="auto-watch",
            ),
        ],
    )
    def test_conflicting_options_are_refused(self, monkeypatch, kwargs, message):
        calls = self._fake_run(monkeypatch)
        with pytest.raises(ValueError, match=message):
            crg_main.main(multi_repo=True, **kwargs)
        assert calls == []
        assert crg_main._multi_repo is False

    def test_a_shared_data_dir_is_refused(self, monkeypatch, tmp_path):
        """CRG_DATA_DIR would put every registered repo in one graph.db."""
        calls = self._fake_run(monkeypatch)
        monkeypatch.setenv("CRG_DATA_DIR", str(tmp_path / "shared"))
        with pytest.raises(ValueError, match="CRG_DATA_DIR"):
            crg_main.main(
                transport="streamable-http", host="127.0.0.1", port=5555,
                multi_repo=True,
            )
        assert calls == []
        assert crg_main._multi_repo is False


class TestServeCommand:
    def test_http_multi_repo_is_passed_through(self, monkeypatch):
        monkeypatch.delenv("CRG_DATA_DIR", raising=False)
        argv = ["code-review-graph", "serve", "--http", "--multi-repo", "--port", "6001"]
        with patch.object(sys, "argv", argv):
            with patch("code_review_graph.main.main") as serve:
                cli.main()
        serve.assert_called_once_with(
            repo_root=None,
            auto_watch=False,
            transport="streamable-http",
            host="127.0.0.1",
            port=6001,
            tools=None,
            multi_repo=True,
        )

    @pytest.mark.parametrize(
        "extra, message",
        [
            pytest.param([], "--multi-repo requires --http", id="no-http"),
            pytest.param(["--http", "--repo", "x"], "registry", id="repo"),
            pytest.param(
                ["--http", "--auto-watch"], "combined with --auto-watch", id="auto-watch",
            ),
            pytest.param(["--http", "--multi-worktree"], "--multi-worktree", id="worktree"),
        ],
    )
    def test_conflicting_flags_are_one_clear_error(
        self, monkeypatch, capsys, extra, message,
    ):
        monkeypatch.delenv("CRG_DATA_DIR", raising=False)
        argv = ["code-review-graph", "serve", "--multi-repo", *extra]
        with patch.object(sys, "argv", argv):
            with patch("code_review_graph.main.main") as serve:
                with pytest.raises(SystemExit) as stopped:
                    cli.main()
        assert stopped.value.code == 2
        assert message in capsys.readouterr().err
        serve.assert_not_called()

    def test_a_global_data_dir_is_refused(self, monkeypatch, capsys):
        monkeypatch.setenv("CRG_DATA_DIR", "/tmp/shared-graph")
        argv = ["code-review-graph", "serve", "--http", "--multi-repo"]
        with patch.object(sys, "argv", argv):
            with patch("code_review_graph.main.main") as serve:
                with pytest.raises(SystemExit) as stopped:
                    cli.main()
        assert stopped.value.code == 2
        assert "CRG_DATA_DIR" in capsys.readouterr().err
        serve.assert_not_called()


# ---------------------------------------------------------------------------
# A real server over Streamable HTTP
# ---------------------------------------------------------------------------

try:
    from mcp import ClientSession
except ImportError:  # pragma: no cover - ``mcp`` is a hard runtime dependency
    ClientSession = None  # type: ignore[assignment,misc]
try:
    from mcp.client.streamable_http import streamable_http_client
except ImportError:  # pragma: no cover - older ``mcp`` releases spell it this way
    from mcp.client.streamable_http import (  # type: ignore[no-redef]
        streamablehttp_client as streamable_http_client,
    )


def _read_timeout() -> Any:
    """mcp 1.x takes the read timeout as a timedelta; mcp 2.x takes seconds."""
    if ClientSession is None:
        return None
    param = inspect.signature(ClientSession.__init__).parameters["read_timeout_seconds"]
    if "timedelta" in str(param.annotation):
        return timedelta(seconds=CALL_TIMEOUT)
    return CALL_TIMEOUT


_READ_TIMEOUT = _read_timeout()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _server_env(crg_home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["CRG_HOME"] = str(crg_home)
    env["CRG_PARSE_WORKERS"] = "2"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    for key in ("CRG_TOOLS", "CRG_DATA_DIR", "CRG_REPO_ROOT", "CRG_TOOL_TIMEOUT"):
        env.pop(key, None)
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(REPO_SOURCE_ROOT), env.get("PYTHONPATH")) if value
    )
    return env


@pytest.fixture(scope="module")
def http_server(tmp_path_factory):
    """``serve --http --multi-repo`` with two registered repos, run from repo A."""
    if ClientSession is None or shutil.which("git") is None:
        pytest.skip("needs the mcp client library and git")
    base = tmp_path_factory.mktemp("multi-repo-http")
    repo_a = _make_repo(base, "repo_a", "only_in_a")
    repo_b = _make_repo(base, "repo_b", "only_in_b")
    stranger = _make_repo(base, "stranger", "only_in_stranger")
    crg_home = base / "crg-home"
    crg_home.mkdir()
    registry = Registry(crg_home / "registry.json")
    registry.register(str(repo_a), alias="a")
    registry.register(str(repo_b), alias="b")

    port = _free_port()
    log_path = base / "server.log"
    with open(log_path, "wb") as log:
        process = subprocess.Popen(
            [
                sys.executable, "-m", "code_review_graph", "serve",
                "--http", "--multi-repo", "--port", str(port),
            ],
            # Started inside repo A on purpose: the old fallback would have
            # answered an unnamed call from here.
            cwd=str(repo_a),
            env=_server_env(crg_home),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + 60
        while True:
            if process.poll() is not None:
                pytest.fail(
                    "server exited early:\n" + log_path.read_text(errors="replace")
                )
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    pytest.fail("server did not start listening within 60s")
                time.sleep(0.2)
        yield {
            "url": f"http://127.0.0.1:{port}/mcp",
            "a": repo_a,
            "b": repo_b,
            "stranger": stranger,
        }
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)


async def _session_call(server: dict, calls: list[tuple[str, dict]]) -> list[Any]:
    async with streamable_http_client(server["url"]) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(
            read, write, read_timeout_seconds=_READ_TIMEOUT,
        ) as session:
            await asyncio.wait_for(session.initialize(), timeout=CALL_TIMEOUT)
            return list(await asyncio.gather(*(
                asyncio.wait_for(session.call_tool(name, args), timeout=CALL_TIMEOUT)
                for name, args in calls
            )))


def _text(result: Any) -> str:
    return " ".join(
        block.text for block in result.content if getattr(block, "type", "") == "text"
    )


def _is_error(result: Any) -> bool:
    """mcp 2.x renamed ``isError`` to ``is_error``."""
    is_error = getattr(result, "is_error", None)
    return bool(result.isError if is_error is None else is_error)


def _payload(result: Any) -> dict:
    assert not _is_error(result), _text(result)
    return json.loads(result.content[0].text)


def _names(payload: dict) -> set[str]:
    return {row["name"] for row in payload.get("results", [])}


@pytest.mark.e2e
async def test_each_call_answers_from_the_repo_it_names(http_server) -> None:
    a, b = str(http_server["a"]), str(http_server["b"])
    builds = await _session_call(http_server, [
        ("build_or_update_graph_tool",
         {"full_rebuild": True, "postprocess": "minimal", "repo_root": a}),
        ("build_or_update_graph_tool",
         {"full_rebuild": True, "postprocess": "minimal", "repo_root": b}),
    ])
    assert [_payload(r)["status"] for r in builds] == ["ok", "ok"]

    # Two client sessions at once, each asking both repos the same questions.
    first, second = await asyncio.gather(*(
        _session_call(http_server, [
            ("semantic_search_nodes_tool", {"query": "only_in_a", "repo_root": a}),
            ("semantic_search_nodes_tool", {"query": "only_in_a", "repo_root": b}),
            ("semantic_search_nodes_tool", {"query": "only_in_b", "repo_root": b}),
            ("semantic_search_nodes_tool", {"query": "only_in_b", "repo_root": a}),
            ("query_graph_tool",
             {"pattern": "file_summary", "target": "pkg/only_in_a.py", "repo_root": a}),
            ("query_graph_tool",
             {"pattern": "file_summary", "target": "pkg/only_in_a.py", "repo_root": b}),
            ("list_graph_stats_tool", {"repo_root": a}),
            ("list_graph_stats_tool", {"repo_root": b}),
        ])
        for _ in range(2)
    ))
    for results in (first, second):
        (
            a_for_a, a_for_b, b_for_b, b_for_a,
            file_in_a, file_in_b, stats_a, stats_b,
        ) = map(_payload, results)
        assert "only_in_a" in _names(a_for_a)
        assert "only_in_a" not in _names(a_for_b)
        assert "only_in_b" in _names(b_for_b)
        assert "only_in_b" not in _names(b_for_a)
        assert "only_in_a" in _names(file_in_a)
        assert _names(file_in_b) == set()
        assert "Graph statistics for repo_a" in stats_a["summary"]
        assert "Graph statistics for repo_b" in stats_b["summary"]


@pytest.mark.e2e
async def test_a_call_naming_no_repo_or_an_unregistered_one_is_refused(
    http_server,
) -> None:
    stranger = http_server["stranger"]
    missing, unregistered, relative = await _session_call(http_server, [
        ("list_graph_stats_tool", {}),
        ("build_or_update_graph_tool", {"repo_root": str(stranger)}),
        ("list_graph_stats_tool", {"repo_root": "repo_a"}),
    ])
    assert _is_error(missing)
    assert "repo_root is required" in _text(missing)
    assert _is_error(unregistered)
    assert "not a registered repository" in _text(unregistered)
    assert _is_error(relative)
    assert "absolute path" in _text(relative)
    # Refused before anything touched the unregistered repo.
    assert not (stranger / ".code-review-graph").exists()

    listed = await _session_call(http_server, [("list_repos_tool", {})])
    paths = {entry["path"] for entry in _payload(listed[0])["repos"]}
    assert paths == {str(http_server["a"]), str(http_server["b"])}


@pytest.mark.e2e
async def test_the_handshake_tells_the_client_to_name_a_repo(http_server) -> None:
    async with streamable_http_client(http_server["url"]) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(
            read, write, read_timeout_seconds=_READ_TIMEOUT,
        ) as session:
            init = await asyncio.wait_for(session.initialize(), timeout=CALL_TIMEOUT)
    assert init.instructions == (
        crg_main.SERVER_INSTRUCTIONS + crg_main.MULTI_REPO_INSTRUCTIONS
    )
