"""Every tool a response suggests next is a name the server registers.

``next_tool_suggestions`` and the ``_hints`` workflow tell an agent what to
call next. A client can only call a tool by the exact name the server lists,
and every registered name ends in ``_tool``. ``get_minimal_context_tool``, the
tool agents are told to call first, suggested ``build_or_update_graph`` and
``detect_changes``, which no client can call; four analysis tools and the
compact impact answer did the same.

This reads the source rather than calling each tool, so a suggestion list on
a path no test reaches is still checked.

Found by the gstack developer-experience review of 2.4.0 (product item 5).
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from code_review_graph import hints, main

PACKAGE = Path(main.__file__).resolve().parent


@pytest.fixture(scope="module")
def registered() -> set[str]:
    names = {tool.name for tool in asyncio.run(main.mcp.list_tools())}
    assert names and all(name.endswith("_tool") for name in names)
    return names


def _strings(node: ast.AST) -> list[str]:
    """The string literals of a list or tuple literal."""
    if isinstance(node, (ast.List, ast.Tuple)):
        return [
            item.value for item in node.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        ]
    return []


def _suggestion_lists(tree: ast.Module) -> list[tuple[int, list[str]]]:
    """Every list given as ``next_tool_suggestions``, by key, keyword or name."""
    assigned: dict[str, list[tuple[int, list[str]]]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned.setdefault(target.id, []).append(
                        (node.lineno, _strings(node.value))
                    )

    def resolve(value: ast.AST, line: int) -> list[tuple[int, list[str]]]:
        if isinstance(value, ast.Name):
            return assigned.get(value.id, [])
        return [(line, _strings(value))]

    found: list[tuple[int, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "next_tool_suggestions":
                    found.extend(resolve(value, value.lineno))
        elif isinstance(node, ast.keyword) and node.arg == "next_tool_suggestions":
            found.extend(resolve(node.value, node.value.lineno))
    return found


def _all_suggestions() -> list[tuple[str, int, str]]:
    rows = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line, entries in _suggestion_lists(tree):
            for entry in entries:
                rows.append((path.relative_to(PACKAGE).as_posix(), line, entry))
    return rows


def test_every_next_tool_suggestion_is_registered(registered: set[str]) -> None:
    rows = _all_suggestions()
    # The scan must keep finding the lists it was written for.
    files = {path for path, _, _ in rows}
    assert {
        "tools/context.py", "tools/review.py", "tools/analysis_tools.py", "tools/query.py",
    } <= files, files
    assert len(rows) >= 25, rows

    # An entry may add words after the name: "query_graph_tool callers_of -- ...".
    wrong = [
        f"{path}:{line}: {entry!r}" for path, line, entry in rows
        if entry.split()[0] not in registered
    ]
    assert not wrong, "suggested tools that are not registered:\n" + "\n".join(wrong)


def test_every_hint_names_a_registered_tool(registered: set[str]) -> None:
    named = set(hints._WORKFLOW)
    for steps in hints._WORKFLOW.values():
        named.update(step["tool"] for step in steps)
    for tools in hints._INTENT_TOOLS.values():
        named.update(tools)

    assert named, "the hints no longer name any tool"
    assert named <= registered, f"not registered: {sorted(named - registered)}"


def test_minimal_context_without_a_graph_suggests_the_build_tool(
    tmp_path: Path, registered: set[str],
) -> None:
    from code_review_graph.tools.context import get_minimal_context

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    result = get_minimal_context(repo_root=str(repo))

    assert result["status"] == "not_ready"
    assert result["next_tool_suggestions"] == ["build_or_update_graph_tool"]
    assert set(result["next_tool_suggestions"]) <= registered


@pytest.mark.parametrize(
    "task", ["review PR #1", "debug a bug", "refactor auth", "explore the architecture", ""],
)
def test_minimal_context_suggests_registered_tools(
    tmp_path: Path, registered: set[str], task: str,
) -> None:
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build
    from code_review_graph.tools.context import get_minimal_context

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    with GraphStore(repo / ".code-review-graph" / "graph.db") as store:
        full_build(repo, store)

    result = get_minimal_context(task=task, changed_files=[], repo_root=str(repo))

    assert result["next_tool_suggestions"]
    assert set(result["next_tool_suggestions"]) <= registered
