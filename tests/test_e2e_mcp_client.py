"""End-to-end tests that drive the MCP server with a real MCP client.

Everything else in the suite calls the tool functions in-process. These tests
spawn ``python -m code_review_graph serve`` as a subprocess and talk to it over
stdio with the ``mcp`` client library — the exact transport Claude Code, Cursor,
Codex and Zed use. They therefore cover the parts nothing else does: the CLI
argument wiring, the JSON-RPC handshake, tool registration and schema
generation, JSON serialisation of every tool payload, and error propagation
back to the client.

Portability notes (these run on Linux, macOS and Windows in CI):

* Paths are built with ``pathlib``; comparisons normalise ``\\`` to ``/``.
* The child process gets a copy of ``os.environ`` with ``CRG_HOME`` pointed at
  a temp directory, so the developer's real registry/daemon state is untouched
  and ``list_repos_tool`` is deterministic.
* No ``shell=True`` anywhere; every subprocess takes a list of arguments.
* Text assertions normalise CRLF before matching.
* Every client call and subprocess is bounded by a timeout so a hung server
  fails the test instead of hanging CI.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

try:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    from tests.mcp_client_compat import McpError, read_timeout, wire
except ImportError:  # pragma: no cover - ``mcp`` is a hard runtime dependency
    ClientSession = None  # type: ignore[assignment,misc]
    StdioServerParameters = None  # type: ignore[assignment,misc]
    stdio_client = None  # type: ignore[assignment]
    McpError = None  # type: ignore[assignment,misc]
    read_timeout = None  # type: ignore[assignment]
    wire = None  # type: ignore[assignment]

# Generous: a cold interpreter start plus a full tree-sitter build on a loaded
# Windows CI runner is slow, but nothing here should ever take minutes.
CALL_TIMEOUT = 120.0

REPO_SOURCE_ROOT = Path(__file__).resolve().parents[1]

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(ClientSession is None, reason="mcp client library not installed"),
    pytest.mark.skipif(
        shutil.which("git") is None,
        reason="git is required to build the fixture repository",
    ),
]


# ---------------------------------------------------------------------------
# Fixture repository
# ---------------------------------------------------------------------------

CORE_PY = """\
def normalize_amount(value):
    return round(float(value), 2)


def compute_total(items):
    return sum(normalize_amount(item) for item in items)
"""

CORE_PY_MODIFIED = """\
def normalize_amount(value):
    return round(float(value), 2)


def compute_total(items):
    subtotal = sum(normalize_amount(item) for item in items)
    return normalize_amount(subtotal)
"""

SERVICE_PY = """\
from pkg.core import compute_total


def build_invoice(items):
    return {"total": compute_total(items)}
"""

TEST_CORE_PY = """\
from pkg.core import compute_total


def test_compute_total_sums_items():
    assert compute_total([1, 2]) == 3
"""


def _git(repo: Path, *args: str) -> None:
    """Run git in ``repo`` with identity forced on the command line.

    ``-c user.name`` / ``-c user.email`` keep the commit from depending on the
    developer's (or the runner's) global git config, which may be absent.
    """
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=CRG E2E",
            "-c",
            "user.email=e2e@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=CALL_TIMEOUT,
    )


def _make_fixture_repo(root: Path) -> Path:
    """Create a small, real git repository with cross-file calls and a test."""
    repo = root / "sample_repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "pkg" / "core.py").write_text(CORE_PY, encoding="utf-8")
    (repo / "pkg" / "service.py").write_text(SERVICE_PY, encoding="utf-8")
    (repo / "tests" / "test_core.py").write_text(TEST_CORE_PY, encoding="utf-8")

    _git(repo.parent, "init", "-q", "-b", "main", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial commit")
    return repo


def _server_env(crg_home: Path) -> dict[str, str]:
    """Environment for the spawned server: isolated state, importable package."""
    env = os.environ.copy()
    # Registry, daemon PID/state and logs all resolve from CRG_HOME.
    env["CRG_HOME"] = str(crg_home)
    # Keep the fixture repo tiny and the build deterministic.
    env["CRG_PARSE_WORKERS"] = "2"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    for key in (
        "CRG_TOOLS",
        "CRG_DATA_DIR",
        "CRG_REPO_ROOT",
        "CRG_PARSE_EXECUTOR",
        "CRG_SERIAL_PARSE",
    ):
        env.pop(key, None)
    # Run the checkout under test, not a version that happens to be installed.
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(REPO_SOURCE_ROOT), env.get("PYTHONPATH")) if value
    )
    return env


def _server_params(
    repo: Path,
    crg_home: Path,
    *,
    multi_worktree: bool = False,
) -> Any:
    """Stdio launch parameters, as an MCP client config file would supply them.

    ``python -m code_review_graph`` is the runnable entry point: ``cli.py`` has
    no ``__main__`` guard, so ``-m code_review_graph.cli`` would exit without
    starting a server. stdio is the default transport (``--http`` opts out), so
    there is no ``--stdio`` flag to pass.
    """
    args = ["-m", "code_review_graph", "serve", "--repo", str(repo)]
    if multi_worktree:
        args.append("--multi-worktree")
    return StdioServerParameters(
        command=sys.executable,
        args=args,
        env=_server_env(crg_home),
        cwd=str(repo),
    )


# ---------------------------------------------------------------------------
# Client helpers
# ---------------------------------------------------------------------------


def _text_blocks(result: Any) -> list[str]:
    return [
        block.text
        for block in result.content
        if getattr(block, "type", None) == "text" and getattr(block, "text", None)
    ]


def _payload(result: Any) -> dict[str, Any]:
    """Decode a successful tool result into the dict the tool returned."""
    assert not wire(result, "isError"), f"tool call failed: {_text_blocks(result)}"
    blocks = _text_blocks(result)
    assert blocks, f"tool returned no text content: {result!r}"
    payload = json.loads(blocks[0])
    assert isinstance(payload, dict), f"expected a JSON object, got {type(payload)!r}"
    return payload


async def _call(session: Any, name: str, arguments: dict[str, Any] | None = None) -> Any:
    return await asyncio.wait_for(
        session.call_tool(name, arguments or {}),
        timeout=CALL_TIMEOUT,
    )


def _posix(path: str) -> str:
    """Normalise a path string so Windows separators compare equal."""
    return path.replace("\\", "/")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_mcp_client_full_review_journey(tmp_path: Path) -> None:
    """Drive the whole review workflow the way a real MCP client would."""
    repo = _make_fixture_repo(tmp_path)
    crg_home = tmp_path / "crg-home"
    crg_home.mkdir()

    async with stdio_client(_server_params(repo, crg_home)) as (read, write):
        async with ClientSession(
            read,
            write,
            read_timeout_seconds=read_timeout(CALL_TIMEOUT),
        ) as session:
            # --- handshake -------------------------------------------------
            init = await asyncio.wait_for(session.initialize(), timeout=CALL_TIMEOUT)
            assert wire(init, "serverInfo").name == "code-review-graph"
            assert init.capabilities.tools is not None

            # --- tool discovery --------------------------------------------
            listed = await asyncio.wait_for(session.list_tools(), timeout=CALL_TIMEOUT)
            names = {tool.name for tool in listed.tools}
            expected = {
                "build_or_update_graph_tool",
                "get_minimal_context_tool",
                "get_impact_radius_tool",
                "query_graph_tool",
                "get_review_context_tool",
                "semantic_search_nodes_tool",
                "detect_changes_tool",
                "get_architecture_overview_tool",
                "refactor_tool",
                "list_repos_tool",
            }
            assert expected <= names, f"missing tools: {sorted(expected - names)}"
            # The server advertises 30 tools; assert the floor, not the exact
            # number, so adding a tool does not break this test.
            assert len(names) >= 30, f"only {len(names)} tools registered"
            build_schema = next(
                tool for tool in listed.tools if tool.name == "build_or_update_graph_tool"
            )
            assert "full_rebuild" in wire(build_schema, "inputSchema")["properties"]

            # --- build ------------------------------------------------------
            built = _payload(
                await _call(
                    session,
                    "build_or_update_graph_tool",
                    {"full_rebuild": True, "repo_root": str(repo)},
                )
            )
            assert built["status"] == "ok"
            assert built["build_type"] == "full"
            assert built["files_parsed"] == 3, built["summary"]
            assert built["total_nodes"] >= 7
            assert built["errors"] == []
            assert (repo / ".code-review-graph" / "graph.db").is_file()

            # --- minimal context (documented first call) --------------------
            context = _payload(
                await _call(
                    session,
                    "get_minimal_context_tool",
                    {"task": "review the invoice change"},
                )
            )
            assert context["status"] == "ok", context
            assert "3 files" in context["summary"], context["summary"]
            assert context["next_tool_suggestions"]

            # --- semantic search -------------------------------------------
            found = _payload(
                await _call(session, "semantic_search_nodes_tool", {"query": "compute_total"})
            )
            assert found["status"] == "ok", found
            hit_names = {row["name"] for row in found["results"]}
            assert "compute_total" in hit_names, hit_names
            assert "test_compute_total_sums_items" in hit_names, hit_names
            compute_total = next(row for row in found["results"] if row["name"] == "compute_total")
            assert compute_total["kind"] == "Function"
            assert _posix(compute_total["file_path"]).endswith("pkg/core.py")
            qualified_name = compute_total["qualified_name"]

            # --- callers_of --------------------------------------------------
            callers = _payload(
                await _call(
                    session,
                    "query_graph_tool",
                    {"pattern": "callers_of", "target": "normalize_amount"},
                )
            )
            assert callers["status"] == "ok", callers
            assert callers["result_count"] == 1, callers["summary"]
            assert [row["name"] for row in callers["results"]] == ["compute_total"]
            assert callers["results"][0]["qualified_name"] == qualified_name
            assert {edge["kind"] for edge in callers["edges"]} == {"CALLS"}

            # A cross-file caller resolves too: service.build_invoice calls
            # core.compute_total through an import.
            cross_file = _payload(
                await _call(
                    session,
                    "query_graph_tool",
                    {"pattern": "callers_of", "target": qualified_name},
                )
            )
            assert cross_file["status"] == "ok", cross_file
            assert "build_invoice" in {row["name"] for row in cross_file["results"]}

            # --- change a file, commit, update, review ----------------------
            (repo / "pkg" / "core.py").write_text(CORE_PY_MODIFIED, encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "round the invoice subtotal")

            updated = _payload(await _call(session, "build_or_update_graph_tool", {}))
            assert updated["status"] == "ok", updated
            assert updated["build_type"] == "incremental", updated["summary"]

            changes = _payload(await _call(session, "detect_changes_tool", {"base": "HEAD~1"}))
            assert changes["status"] == "ok", changes
            assert [_posix(path) for path in changes["changed_files"]] == ["pkg/core.py"]
            assert changes["changed_file_count"] == 1
            changed_names = {row["name"] for row in changes["changed_functions"]}
            assert "compute_total" in changed_names, changed_names
            assert 0.0 < changes["risk_score"] <= 1.0
            assert changes["review_priorities"], "expected prioritised review items"

            # --- registry ----------------------------------------------------
            repos = _payload(await _call(session, "list_repos_tool", {}))
            assert repos["status"] == "ok", repos
            # CRG_HOME is a fresh temp dir, so the registry must be empty —
            # this is what keeps the test independent of the host machine.
            assert repos["repos"] == [], repos
            assert repos["summary"].startswith("0 registered repository")


@pytest.mark.e2e
def test_shared_postprocess_invalidates_previous_refresh(tmp_path):
    repo = _make_fixture_repo(tmp_path)
    env = {**_server_env(tmp_path / "home"), "CRG_SHARED_DB": str(tmp_path / "shared.db")}

    def run(*args):
        return subprocess.check_output(
            [sys.executable, "-m", "code_review_graph", *args, "--repo", str(repo)],
            env=env, text=True, timeout=30,
        )

    run("build")
    mixed = subprocess.run(
        [sys.executable, "-m", "code_review_graph", "status",
         "--repo", str(repo), "--data-dir", str(tmp_path / "other")],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert mixed.returncode == 2
    assert "Select either CRG_SHARED_DB or --data-dir" in mixed.stderr
    run("update")
    assert "Reused the completed refresh" in run("update")
    run("postprocess")
    assert "Reused the completed refresh" not in run("update")
    assert "Reused the completed refresh" in run("update")


@pytest.mark.e2e
def test_shared_cli_reuses_watcher_reconciliation(tmp_path):
    repo = _make_fixture_repo(tmp_path)
    env = {**_server_env(tmp_path / "home"), "CRG_SHARED_DB": str(tmp_path / "shared.db")}

    def cli(*args):
        return [sys.executable, "-m", "code_review_graph", *args, "--repo", str(repo)]

    subprocess.check_call(cli("build"), env=env, timeout=30)
    relative = "pkg/view.py"
    (repo / relative).write_text("def reconciled_by_watch(): return 1\n", encoding="utf-8")
    log = tmp_path / "watch.log"
    with log.open("wb") as output, subprocess.Popen(
        cli("watch"), env={**env, "CRG_WATCH_HEALTH_INTERVAL": "1"},
        stdout=output, stderr=subprocess.STDOUT,
    ) as watcher:
        try:
            deadline = time.monotonic() + 30
            while "Watching " not in log.read_text(encoding="utf-8"):
                assert watcher.poll() is None, log.read_text(encoding="utf-8")
                assert time.monotonic() < deadline, log.read_text(encoding="utf-8")
                time.sleep(0.1)
            assert "Reused the completed refresh" in subprocess.check_output(
                cli("update"), env=env, text=True, timeout=30,
            )
            query = json.loads(subprocess.check_output(
                cli("query", "file_summary", relative), env=env, text=True, timeout=30,
            ))
            assert "reconciled_by_watch" in {row["name"] for row in query["results"]}
        finally:
            watcher.terminate()
            watcher.wait(timeout=15)


@pytest.mark.e2e
def test_shared_cli_crash_rolls_back_written_rows(tmp_path):
    repo = _make_fixture_repo(tmp_path)
    env = {**_server_env(tmp_path / "home"), "CRG_SERIAL_PARSE": "1",
           "CRG_SHARED_DB": str(tmp_path / "shared.db")}
    cli = [sys.executable, "-m", "code_review_graph"]

    def run(*args):
        return subprocess.check_output(
            [*cli, *args, "--repo", str(repo)], env=env, text=True, timeout=20,
        )

    source = repo / "pkg" / "view.py"
    source.write_text("def previous_symbol(): return 1\n", encoding="utf-8")
    run("build")
    source.write_text("def replacement_symbol(): return 2\n", encoding="utf-8")

    def symbols():
        graph = json.loads(run("query", "file_summary", "pkg/view.py"))
        return {node["name"] for node in graph["results"] if node["kind"] == "Function"}

    entered = tmp_path / "entered"
    producer = """
import sys, time
from pathlib import Path
from code_review_graph.cli import main
from code_review_graph.graph import GraphStore

marker, path = Path(sys.argv.pop(1)), Path(sys.argv.pop(1))
real_write = GraphStore.store_file_nodes_edges
def pause(self, file_path, *args, **kwargs):
    result = real_write(self, file_path, *args, **kwargs)
    if Path(file_path) == path:
        marker.touch()
        time.sleep(30)
    return result
GraphStore.store_file_nodes_edges = pause
main()
"""
    with subprocess.Popen(
        [sys.executable, "-c", producer, str(entered), str(source),
         "update", "--repo", str(repo)], env=env,
    ) as child:
        try:
            deadline = time.monotonic() + 15
            while not entered.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            assert entered.exists()
            assert symbols() == {"previous_symbol"}
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
    assert symbols() == {"previous_symbol"}
    run("update")
    assert symbols() == {"replacement_symbol"}
    assert list(tmp_path.rglob("*.db")) == [tmp_path / "shared.db"]


@pytest.mark.parametrize("outcome", ["reused", "crash", "different-profile", "new-edit"])
async def test_cli_and_mcp_complete_a_contended_refresh(tmp_path, outcome):
    repo = _make_fixture_repo(tmp_path)
    crg_home = tmp_path / "refresh-home"
    crg_home.mkdir()
    env = {**_server_env(crg_home), "CRG_SERIAL_PARSE": "1"}
    if outcome == "new-edit":
        env["CRG_SHARED_DB"] = str(tmp_path / "shared.db")
    # Keep the production parser and writer; pause the CLI process after
    # it has taken the kernel lock and started parsing.
    producer = """
import sys, time
from pathlib import Path
from code_review_graph.cli import main
from code_review_graph.parser import CodeParser

entered, resume = map(Path, sys.argv[1:3])
del sys.argv[1:3]
real_parse = CodeParser.parse_bytes
def pause(self, path, source):
    result = real_parse(self, path, source)
    if Path(path).name == "core.py" and not entered.exists():
        entered.touch()
        until = time.monotonic() + 60
        while not resume.exists() and time.monotonic() < until:
            time.sleep(0.02)
        if not resume.exists():
            raise TimeoutError("fixture never resumed the parser")
    return result
CodeParser.parse_bytes = pause
main()
"""
    entered, resume = (tmp_path / name for name in ("entered", "resume"))
    with subprocess.Popen(
        [sys.executable, "-c", producer, str(entered), str(resume),
         "build", "--wait", "--skip-postprocess", "--repo", str(repo)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) as child:
        try:
            deadline = time.monotonic() + 60
            while not entered.exists() and child.poll() is None and time.monotonic() < deadline:
                await asyncio.sleep(.02)
            assert entered.exists()
            if outcome == "new-edit":
                status = subprocess.run(
                    [sys.executable, "-m", "code_review_graph", "status", "--json",
                     "--repo", str(repo)],
                    env=env, capture_output=True, text=True, timeout=15, check=False,
                )
                assert status.returncode == 1 and "No committed graph" in status.stderr
            params = _server_params(repo, crg_home)
            params.env = {**env, "TEST_REFRESH_WAITER": str(tmp_path / "waiting")}
            waiter = """
import os
from pathlib import Path
from code_review_graph.cli import main
from code_review_graph.tools import build

previous = build._previous_refresh
def mark(*args):
    result = previous(*args)
    Path(os.environ["TEST_REFRESH_WAITER"]).touch()
    return result
build._previous_refresh = mark
main()
"""
            params.args = ["-c", waiter, *params.args[2:]]
            async with stdio_client(params) as (read, write):
                async with ClientSession(
                    read, write, read_timeout_seconds=read_timeout(CALL_TIMEOUT),
                ) as session:
                    await asyncio.wait_for(session.initialize(), timeout=CALL_TIMEOUT)
                    join = asyncio.create_task(_call(session, "build_or_update_graph_tool", {
                        "full_rebuild": True,
                        "postprocess": "minimal" if outcome == "different-profile" else "none",
                        "wait_for_lock": True,
                    }))
                    deadline = time.monotonic() + 45
                    while not (tmp_path / "waiting").exists():
                        assert not join.done() and child.poll() is None
                        assert time.monotonic() < deadline
                        await asyncio.sleep(.02)
                    if outcome == "crash":
                        child.kill()
                    else:
                        if outcome == "new-edit":
                            (repo / "pkg" / "core.py").write_text(
                                CORE_PY + "\ndef arrived_during_refresh(): return 1\n",
                                encoding="utf-8",
                            )
                        resume.touch()
                    result = _payload(await join)
                    assert result["status"] == "ok", result
                    if outcome == "reused":
                        assert result["refresh_reused"] is True, result
                        assert result["files_parsed"] == 0
                    else:
                        assert "refresh_reused" not in result, result
                        assert result["files_parsed"] > 0
                    query = _payload(await _call(session, "query_graph_tool", {
                        "pattern": "file_summary", "target": "pkg/core.py",
                    }))
                    names = {node["name"] for node in query["results"]}
                    assert "compute_total" in names
                    if outcome == "new-edit":
                        assert "arrived_during_refresh" in names
        finally:
            resume.touch()
            stdout, stderr = child.communicate(timeout=20)
        if outcome != "crash":
            assert child.returncode == 0, stdout + stderr
        else:
            assert child.returncode != 0


async def test_mcp_shared_sqlite_isolates_sibling_worktrees(tmp_path, monkeypatch):
    repo = _make_fixture_repo(tmp_path)
    sibling = tmp_path / "sibling"
    _git(repo, "worktree", "add", "-q", "--detach", str(sibling))
    db = tmp_path / "shared.db"
    relative_path = "pkg/view.py"
    for root, name in ((repo, "left_symbol"), (sibling, "right_symbol")):
        (root / relative_path).write_text(f"def {name}(): return 1\n", encoding="utf-8")
        _git(root, "add", relative_path)

    monkeypatch.setenv("CRG_SHARED_DB", str(db))
    crg_home = tmp_path / "crg-home"
    crg_home.mkdir()
    async with stdio_client(
        _server_params(repo, crg_home, multi_worktree=True)
    ) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=read_timeout(CALL_TIMEOUT),
        ) as session:
            await asyncio.wait_for(session.initialize(), timeout=CALL_TIMEOUT)

            async def query(root: Path, name: str, excluded: str) -> None:
                result = _payload(await _call(session, "query_graph_tool", {
                    "pattern": "file_summary", "target": relative_path, "repo_root": str(root),
                }))
                assert result["status"] == "ok", result
                assert [row["name"] for row in result["results"]
                        if row["kind"] == "Function"] == [name]
                assert all(_posix(row["file_path"]) == (root / relative_path).as_posix()
                           for row in result["results"])
                search = _payload(await _call(session, "semantic_search_nodes_tool", {
                    "query": name, "repo_root": str(root),
                }))
                assert name in {node["name"] for node in search["results"]}, search
                other = _payload(await _call(session, "semantic_search_nodes_tool", {
                    "query": excluded, "repo_root": str(root),
                }))
                assert excluded not in {node["name"] for node in other["results"]}, other

            async def refresh(root: Path, *, full: bool) -> dict:
                result = _payload(await _call(session, "build_or_update_graph_tool", {
                    "full_rebuild": full, "repo_root": str(root), "wait_for_lock": True,
                }))
                assert result["status"] == "ok", result
                return result

            await asyncio.gather(refresh(repo, full=True), refresh(sibling, full=True))
            await query(repo, "left_symbol", "right_symbol")
            await query(sibling, "right_symbol", "left_symbol")
            assert list(tmp_path.rglob("*.db")) == [db]

            source = sibling / relative_path
            source.write_text("def right_wip(): return 42\n", encoding="utf-8")
            await refresh(sibling, full=False)
            await query(sibling, "right_wip", "right_symbol")
            await query(repo, "left_symbol", "right_wip")
            assert list(tmp_path.rglob("*.db")) == [db]

            await asyncio.to_thread(
                subprocess.check_call,
                [sys.executable, "-m", "code_review_graph", "forget", relative_path,
                 "--repo", str(sibling)],
                env=_server_env(crg_home), timeout=30,
            )
            forgotten = _payload(await _call(session, "semantic_search_nodes_tool", {
                "query": "right_wip", "repo_root": str(sibling),
            }))
            assert "right_wip" not in {row["name"] for row in forgotten["results"]}, forgotten
            await query(repo, "left_symbol", "right_wip")
            assert list(tmp_path.rglob("*.db")) == [db]


async def test_mcp_multi_worktree_routes_parallel_graphs(tmp_path: Path) -> None:
    """One stdio session can build and query separate linked-worktree graphs."""
    repo = _make_fixture_repo(tmp_path)
    worktree = tmp_path / "review_worktree"
    _git(repo, "worktree", "add", "-q", "-b", "review", str(worktree))
    (worktree / "pkg" / "worktree_only.py").write_text(
        "def worktree_only():\n    return 'review'\n",
        encoding="utf-8",
    )
    _git(worktree, "add", "pkg/worktree_only.py")
    _git(worktree, "commit", "-q", "-m", "add worktree-only symbol")
    other_repo = _make_fixture_repo(tmp_path / "other")
    crg_home = tmp_path / "crg-home"
    crg_home.mkdir()

    async with stdio_client(
        _server_params(repo, crg_home, multi_worktree=True)
    ) as (read, write):
        async with ClientSession(
            read,
            write,
            read_timeout_seconds=read_timeout(CALL_TIMEOUT),
        ) as session:
            await asyncio.wait_for(session.initialize(), timeout=CALL_TIMEOUT)

            main_build, worktree_build = await asyncio.gather(
                _call(
                    session,
                    "build_or_update_graph_tool",
                    {
                        "full_rebuild": True,
                        "postprocess": "none",
                        "repo_root": str(repo),
                    },
                ),
                _call(
                    session,
                    "build_or_update_graph_tool",
                    {
                        "full_rebuild": True,
                        "postprocess": "none",
                        "repo_root": str(worktree),
                    },
                ),
            )
            assert _payload(main_build)["status"] == "ok"
            assert _payload(worktree_build)["status"] == "ok"
            assert (repo / ".code-review-graph" / "graph.db").is_file()
            assert (worktree / ".code-review-graph" / "graph.db").is_file()

            main_query, worktree_query = await asyncio.gather(
                _call(
                    session,
                    "query_graph_tool",
                    {
                        "pattern": "file_summary",
                        "target": "pkg/worktree_only.py",
                        "repo_root": str(repo),
                    },
                ),
                _call(
                    session,
                    "query_graph_tool",
                    {
                        "pattern": "file_summary",
                        "target": "pkg/worktree_only.py",
                        "repo_root": str(worktree),
                    },
                ),
            )
            main_payload = _payload(main_query)
            worktree_payload = _payload(worktree_query)
            assert main_payload["result_count"] == 0, main_payload
            assert worktree_payload["status"] == "ok", worktree_payload
            assert "worktree_only" in {
                row["name"] for row in worktree_payload["results"]
            }

            with pytest.raises(McpError, match="linked worktree"):
                await _call(
                    session,
                    "list_graph_stats_tool",
                    {"repo_root": str(other_repo)},
                )


async def test_mcp_client_rejects_repo_root_outside_a_project(tmp_path: Path) -> None:
    """A repo_root that is not a project root is refused, not silently served."""
    repo = _make_fixture_repo(tmp_path)
    crg_home = tmp_path / "crg-home"
    crg_home.mkdir()
    # Exists, readable, and deliberately has no .git / .svn / .code-review-graph.
    outsider = tmp_path / "outside"
    outsider.mkdir()
    (outsider / "secrets.txt").write_text("not yours\n", encoding="utf-8")

    async with stdio_client(_server_params(repo, crg_home)) as (read, write):
        async with ClientSession(
            read,
            write,
            read_timeout_seconds=read_timeout(CALL_TIMEOUT),
        ) as session:
            await asyncio.wait_for(session.initialize(), timeout=CALL_TIMEOUT)

            result = await _call(
                session,
                "query_graph_tool",
                {
                    "pattern": "callers_of",
                    "target": "compute_total",
                    "repo_root": str(outsider),
                },
            )
            assert wire(result, "isError"), f"traversal was not refused: {_text_blocks(result)}"
            message = " ".join(_text_blocks(result))
            assert "repo_root" in message, message
            assert "project root" in message, message


def test_cli_daemon_status_reports_no_daemon(tmp_path: Path) -> None:
    """``daemon status`` runs as a plain subprocess and reports a clean slate."""
    crg_home = tmp_path / "crg-home"
    crg_home.mkdir()

    completed = subprocess.run(
        [sys.executable, "-m", "code_review_graph", "daemon", "status"],
        cwd=str(tmp_path),
        env=_server_env(crg_home),
        capture_output=True,
        text=True,
        timeout=CALL_TIMEOUT,
    )

    assert completed.returncode == 0, completed.stderr
    stdout = completed.stdout.replace("\r\n", "\n")
    daemon_lines = [line for line in stdout.split("\n") if line.startswith("Daemon:")]
    assert daemon_lines, stdout
    assert "not running" in daemon_lines[0], daemon_lines[0]
    assert "No repositories configured." in stdout, stdout
    # The isolated home is what the status came from, not the real one.
    assert _posix(str(crg_home)) in _posix(stdout), stdout
