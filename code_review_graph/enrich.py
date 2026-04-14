"""PreToolUse search enrichment for Claude Code hooks.

Intercepts Grep/Glob/Bash/Read tool calls and enriches them with
structural context from the code knowledge graph: callers, callees,
execution flows, community membership, and test coverage.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Flags that consume the next token in grep/rg commands
_RG_FLAGS_WITH_VALUES = frozenset({
    "-e", "-f", "-m", "-A", "-B", "-C", "-g", "--glob",
    "-t", "--type", "--include", "--exclude", "--max-count",
    "--max-depth", "--max-filesize", "--color", "--colors",
    "--context-separator", "--field-match-separator",
    "--path-separator", "--replace", "--sort", "--sortr",
})


def extract_pattern(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """Extract a search pattern from a tool call's input.

    Returns None if no meaningful pattern can be extracted.
    """
    if tool_name == "Grep":
        return tool_input.get("pattern")

    if tool_name == "Glob":
        raw = tool_input.get("pattern", "")
        # Extract meaningful name from glob: "**/auth*.ts" -> "auth"
        # Skip pure extension globs like "**/*.ts"
        match = re.search(r"[*/]([a-zA-Z][a-zA-Z0-9_]{2,})", raw)
        return match.group(1) if match else None

    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if not re.search(r"\brg\b|\bgrep\b", cmd):
            return None
        tokens = cmd.split()
        found_cmd = False
        skip_next = False
        for token in tokens:
            if skip_next:
                skip_next = False
                continue
            if not found_cmd:
                if re.search(r"\brg$|\bgrep$", token):
                    found_cmd = True
                continue
            if token.startswith("-"):
                if token in _RG_FLAGS_WITH_VALUES:
                    skip_next = True
                continue
            cleaned = token.strip("'\"")
            return cleaned if len(cleaned) >= 3 else None
        return None

    return None


def _make_relative(file_path: str, repo_root: str) -> str:
    """Make a file path relative to repo_root for display."""
    try:
        return str(Path(file_path).relative_to(repo_root))
    except ValueError:
        return file_path


def _get_community_name(conn: Any, community_id: int) -> str:
    """Fetch a community name by ID."""
    row = conn.execute(
        "SELECT name FROM communities WHERE id = ?", (community_id,)
    ).fetchone()
    return row["name"] if row else ""


def _get_flow_names_for_node(conn: Any, node_id: int) -> list[str]:
    """Fetch execution flow names that a node participates in (max 3)."""
    rows = conn.execute(
        "SELECT f.name FROM flow_memberships fm "
        "JOIN flows f ON fm.flow_id = f.id "
        "WHERE fm.node_id = ? LIMIT 3",
        (node_id,),
    ).fetchall()
    return [r["name"] for r in rows]


def _format_node_context(
    node: Any,
    store: Any,
    conn: Any,
    repo_root: str,
) -> list[str]:
    """Format a single node's structural context as plain text lines."""
    from .graph import GraphNode
    assert isinstance(node, GraphNode)

    qn = node.qualified_name
    loc = _make_relative(node.file_path, repo_root)
    if node.line_start:
        loc = f"{loc}:{node.line_start}"

    header = f"{node.name} ({loc})"

    # Community
    if node.extra.get("community_id"):
        cname = _get_community_name(conn, node.extra["community_id"])
        if cname:
            header += f" [{cname}]"
    else:
        # Check via direct query
        row = conn.execute(
            "SELECT community_id FROM nodes WHERE id = ?", (node.id,)
        ).fetchone()
        if row and row["community_id"]:
            cname = _get_community_name(conn, row["community_id"])
            if cname:
                header += f" [{cname}]"

    lines = [header]

    # Callers (max 5, deduplicated)
    callers: list[str] = []
    seen: set[str] = set()
    for e in store.get_edges_by_target(qn):
        if e.kind == "CALLS" and len(callers) < 5:
            c = store.get_node(e.source_qualified)
            if c and c.name not in seen:
                seen.add(c.name)
                callers.append(c.name)
    if callers:
        lines.append(f"  Called by: {', '.join(callers)}")

    # Callees (max 5, deduplicated)
    callees: list[str] = []
    seen.clear()
    for e in store.get_edges_by_source(qn):
        if e.kind == "CALLS" and len(callees) < 5:
            c = store.get_node(e.target_qualified)
            if c and c.name not in seen:
                seen.add(c.name)
                callees.append(c.name)
    if callees:
        lines.append(f"  Calls: {', '.join(callees)}")

    # Execution flows
    flow_names = _get_flow_names_for_node(conn, node.id)
    if flow_names:
        lines.append(f"  Flows: {', '.join(flow_names)}")

    # Tests
    tests: list[str] = []
    for e in store.get_edges_by_target(qn):
        if e.kind == "TESTED_BY" and len(tests) < 3:
            t = store.get_node(e.source_qualified)
            if t:
                tests.append(t.name)
    if tests:
        lines.append(f"  Tests: {', '.join(tests)}")

    return lines


def enrich_search(pattern: str, repo_root: str) -> str:
    """Search the graph for pattern and return enriched context."""
    from .graph import GraphStore
    from .search import _fts_search

    db_path = Path(repo_root) / ".code-review-graph" / "graph.db"
    if not db_path.exists():
        return ""

    store = GraphStore(db_path)
    try:
        conn = store._conn

        fts_results = _fts_search(conn, pattern, limit=8)
        if not fts_results:
            return ""

        all_lines: list[str] = []
        count = 0
        for node_id, _score in fts_results:
            if count >= 5:
                break
            node = store.get_node_by_id(node_id)
            if not node or node.is_test:
                continue
            node_lines = _format_node_context(node, store, conn, repo_root)
            all_lines.extend(node_lines)
            all_lines.append("")
            count += 1

        if not all_lines:
            return ""

        header = f'[code-review-graph] {count} symbol(s) matching "{pattern}":\n'
        return header + "\n".join(all_lines)
    finally:
        store.close()


def enrich_file_read(file_path: str, repo_root: str) -> str:
    """Enrich a file read with structural context for functions in that file."""
    from .graph import GraphStore

    db_path = Path(repo_root) / ".code-review-graph" / "graph.db"
    if not db_path.exists():
        return ""

    store = GraphStore(db_path)
    try:
        conn = store._conn
        nodes = store.get_nodes_by_file(file_path)
        if not nodes:
            # Try with resolved path
            try:
                resolved = str(Path(file_path).resolve())
                nodes = store.get_nodes_by_file(resolved)
            except (OSError, ValueError):
                pass
        if not nodes:
            return ""

        # Filter to functions/classes/types (skip File nodes), limit to 10
        interesting = [
            n for n in nodes
            if n.kind in ("Function", "Class", "Type", "Test")
        ][:10]

        if not interesting:
            return ""

        all_lines: list[str] = []
        for node in interesting:
            node_lines = _format_node_context(node, store, conn, repo_root)
            all_lines.extend(node_lines)
            all_lines.append("")

        rel_path = _make_relative(file_path, repo_root)
        header = (
            f"[code-review-graph] {len(interesting)} symbol(s) in {rel_path}:\n"
        )
        return header + "\n".join(all_lines)
    finally:
        store.close()


def run_hook() -> None:
    """Entry point for the enrich CLI subcommand.

    Reads Claude Code hook JSON from stdin, extracts the search pattern,
    queries the graph, and outputs hookSpecificOutput JSON to stdout.
    """
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})
    cwd = hook_input.get("cwd", os.getcwd())

    # Find repo root by walking up from cwd
    from .incremental import find_project_root

    repo_root = str(find_project_root(Path(cwd)))
    db_path = Path(repo_root) / ".code-review-graph" / "graph.db"
    if not db_path.exists():
        return

    # Dispatch
    context = ""
    if tool_name == "Read":
        fp = tool_input.get("file_path", "")
        if fp:
            context = enrich_file_read(fp, repo_root)
    else:
        pattern = extract_pattern(tool_name, tool_input)
        if not pattern or len(pattern) < 3:
            return
        context = enrich_search(pattern, repo_root)

    if not context:
        return

    response = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": context,
        }
    }
    json.dump(response, sys.stdout)
