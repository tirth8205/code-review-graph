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
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

try:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
except ImportError:  # pragma: no cover - ``mcp`` is a hard runtime dependency
    ClientSession = None  # type: ignore[assignment,misc]
    StdioServerParameters = None  # type: ignore[assignment,misc]
    stdio_client = None  # type: ignore[assignment]

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


def _server_params(repo: Path, crg_home: Path) -> Any:
    """Stdio launch parameters, as an MCP client config file would supply them.

    ``python -m code_review_graph`` is the runnable entry point: ``cli.py`` has
    no ``__main__`` guard, so ``-m code_review_graph.cli`` would exit without
    starting a server. stdio is the default transport (``--http`` opts out), so
    there is no ``--stdio`` flag to pass.
    """
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "code_review_graph", "serve", "--repo", str(repo)],
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
    assert not result.isError, f"tool call failed: {_text_blocks(result)}"
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
            read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT),
        ) as session:
            # --- handshake -------------------------------------------------
            init = await asyncio.wait_for(session.initialize(), timeout=CALL_TIMEOUT)
            assert init.serverInfo.name == "code-review-graph"
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
            assert "full_rebuild" in build_schema.inputSchema["properties"]

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
            read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT),
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
            assert result.isError, f"traversal was not refused: {_text_blocks(result)}"
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
