"""The MCP server defers its tool modules without losing a name at call time.

``main.py`` loads the tool implementations on first use through a module
``__getattr__`` (PR #1139). That hook only answers ``main.name`` attribute
access, never a bare global lookup inside a function, so a tool body that
reads a deferred name before it is resolved raises ``NameError``. These
tests pin the two ways that can happen, each in a fresh interpreter so no
earlier test has already filled the module's globals.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import code_review_graph.main as crg_main

MAIN_SOURCE = Path(crg_main.__file__).read_text(encoding="utf-8")
LAZY = crg_main._LAZY_TOOL_NAMES | crg_main._LAZY_PROMPT_NAMES


def _run_fresh(code: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".code-review-graph").mkdir()
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code), str(repo)],
        capture_output=True, text=True, timeout=120, env=dict(os.environ),
    )


def test_every_deferred_name_is_read_where_the_worker_resolves_it():
    """A deferred tool name may only be read inside the lambda given to
    ``_offload``: the worker resolves exactly the names that lambda reads."""
    tree = ast.parse(MAIN_SOURCE)
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    misplaced = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)):
            continue
        if node.id not in LAZY:
            continue
        scope = parents.get(node)
        while scope is not None and not isinstance(
            scope, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            scope = parents.get(scope)
        if isinstance(scope, ast.FunctionDef) and scope.name in (
            "__getattr__", "_resolve_attr",
        ):
            continue
        call = parents.get(scope) if isinstance(scope, ast.Lambda) else None
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_offload"
            and len(call.args) > 1
            and call.args[1] is scope
        ):
            continue
        misplaced.append(f"line {node.lineno}: {node.id}")

    assert not misplaced, (
        "read these through _resolve_attr() or inside the _offload lambda: "
        + ", ".join(misplaced)
    )


def test_a_cold_tool_call_resolves_its_implementation(tmp_path):
    result = _run_fresh(
        """
        import asyncio, sys
        import code_review_graph.main as m
        assert "list_graph_stats" not in vars(m)
        out = asyncio.run(m.list_graph_stats_tool(repo_root=sys.argv[1]))
        print("STATUS", out.get("status"))
        """,
        tmp_path,
    )
    assert "NameError" not in result.stderr, result.stderr
    assert "STATUS" in result.stdout, result.stderr


def test_serve_with_auto_watch_opens_the_watch_store(tmp_path):
    """``serve --auto-watch`` opens a GraphStore in ``main()``; it crashed with
    ``NameError: name 'GraphStore' is not defined`` while that name was
    deferred."""
    result = _run_fresh(
        """
        import sys
        from unittest.mock import Mock
        import code_review_graph.main as m
        m.mcp.run = Mock()
        m.start_watch_thread = Mock(return_value=None)
        m.main(repo_root=sys.argv[1], auto_watch=True)
        print("SERVED")
        """,
        tmp_path,
    )
    assert "SERVED" in result.stdout, result.stderr
