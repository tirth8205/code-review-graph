"""CLI wrappers for graph tools reconciled from PR #95."""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

import pytest

import code_review_graph.tools  # noqa: F401 - exposes lazy patch targets
from code_review_graph import cli


@pytest.mark.parametrize(
    ("arguments", "tool_name", "expected"),
    [
        (
            ["query", "callers_of", "target"],
            "query_graph",
            {"pattern": "callers_of", "target": "target"},
        ),
        (
            ["impact", "--files", "a.py", "b.py", "--depth", "3", "--max-results", "20"],
            "get_impact_radius",
            {
                "changed_files": ["a.py", "b.py"],
                "max_depth": 3,
                "max_results": 20,
                "base": "HEAD~1",
            },
        ),
        (
            ["search", "login", "--kind", "Function", "--limit", "7"],
            "semantic_search_nodes",
            {"query": "login", "kind": "Function", "limit": 7},
        ),
        (
            ["flows", "--sort", "depth", "--limit", "9", "--kind", "Function"],
            "list_flows",
            {"sort_by": "depth", "limit": 9, "kind": "Function"},
        ),
        (
            ["flow", "--id", "7", "--source"],
            "get_flow",
            {"flow_id": 7, "flow_name": None, "include_source": True},
        ),
        (
            ["communities", "--sort", "cohesion", "--min-size", "3"],
            "list_communities_func",
            {"sort_by": "cohesion", "min_size": 3},
        ),
        (
            ["community", "--name", "parser", "--members"],
            "get_community_func",
            {
                "community_name": "parser",
                "community_id": None,
                "include_members": True,
            },
        ),
        (
            ["architecture", "--detail-level", "standard"],
            "get_architecture_overview_func",
            {"detail_level": "standard"},
        ),
        (
            ["large-functions", "--min-lines", "80", "--kind", "Class", "--limit", "4"],
            "find_large_functions",
            {
                "min_lines": 80,
                "kind": "Class",
                "file_path_pattern": None,
                "limit": 4,
            },
        ),
        (
            ["refactor", "dead_code", "--kind", "Function", "--path", "src/"],
            "refactor_func",
            {
                "mode": "dead_code",
                "old_name": None,
                "new_name": None,
                "kind": "Function",
                "file_pattern": "src/",
            },
        ),
    ],
)
def test_tool_command_forwards_typed_arguments_as_json(
    arguments, tool_name, expected, tmp_path, monkeypatch, capsys,
):
    repo = tmp_path / "repo"
    nested = repo / "src" / "nested"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "graph.db").touch()
    monkeypatch.setenv("CRG_DATA_DIR", str(data_dir))
    argv = ["code-review-graph", *arguments, "--repo", str(nested)]
    result = {"status": "ok", "tool": tool_name}

    with patch.object(sys, "argv", argv):
        with patch(f"code_review_graph.tools.{tool_name}", return_value=result) as tool:
            cli.main()

    assert json.loads(capsys.readouterr().out) == result
    tool.assert_called_once_with(repo_root=str(repo), **expected)


@pytest.mark.parametrize(
    "arguments",
    [
        ["flow"],
        ["flow", "--id", "1", "--name", "duplicate"],
        ["community"],
        ["community", "--id", "1", "--name", "duplicate"],
        ["refactor", "rename", "--old-name", "only-old"],
        ["impact", "--depth", "-1"],
        ["search", "query", "--limit", "0"],
    ],
)
def test_tool_commands_reject_invalid_or_ambiguous_arguments(arguments):
    with patch.object(sys, "argv", ["code-review-graph", *arguments]):
        with pytest.raises(SystemExit) as exc_info:
            cli.main()
    assert exc_info.value.code == 2


def test_tool_command_missing_graph_exits_nonzero(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setenv("CRG_DATA_DIR", str(tmp_path / "missing"))

    with patch.object(
        sys,
        "argv",
        ["code-review-graph", "query", "callers_of", "target", "--repo", str(repo)],
    ):
        with pytest.raises(SystemExit) as exc_info:
            cli.main()

    assert exc_info.value.code == 1
    assert "No graph found" in capsys.readouterr().err


def test_tool_command_honors_repo_flag_over_ancestor_git_in_monorepo(
    tmp_path, monkeypatch, capsys,
):
    """Regression test: a single ``.git`` at a monorepo root must not shadow
    a module subdirectory's own ``.code-review-graph`` graph when ``--repo``
    points directly at that module (e.g. ``llvm-project`` with ``llvm/`` and
    ``clang/`` independently built/registered as CRG projects)."""
    monorepo = tmp_path / "monorepo"
    module = monorepo / "llvm"
    module.mkdir(parents=True)
    (monorepo / ".git").mkdir()

    # A graph exists for the module itself...
    module_data = module / ".code-review-graph"
    module_data.mkdir()
    (module_data / "graph.db").touch()

    # ...but NOT at the monorepo root, so the buggy resolution (walking up
    # to the nearest .git) would report "no graph found" there instead.
    monorepo_data = monorepo / ".code-review-graph"
    monorepo_data.mkdir()

    argv = ["code-review-graph", "search", "raw_ostream", "--repo", str(module)]
    result = {"status": "ok", "tool": "semantic_search_nodes"}

    with patch.object(sys, "argv", argv):
        with patch(
            "code_review_graph.tools.semantic_search_nodes", return_value=result,
        ) as tool:
            cli.main()

    assert json.loads(capsys.readouterr().out) == result
    tool.assert_called_once_with(repo_root=str(module), query="raw_ostream", kind=None, limit=20)
