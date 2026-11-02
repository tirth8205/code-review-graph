"""File-level impact context should not require a full node/edge response."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from code_review_graph import cli, main
from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.tools import query


@pytest.fixture
def impact_repo(tmp_path):
    root = tmp_path / ("long-checkout-name-" * 5)
    root.mkdir()
    store = GraphStore(root / ".code-review-graph" / "graph.db")
    paths = [root / "src" / name for name in ("core.py", "alpha.py", "beta.py", "gamma.py")]
    for path in paths:
        path.parent.mkdir(exist_ok=True)
        path.write_text("def execute():\n    pass\n")
        store.upsert_node(NodeInfo(
            kind="File", name=str(path), file_path=str(path),
            line_start=1, line_end=2, language="python",
        ))
    store.upsert_node(NodeInfo(
        kind="Function", name="execute", file_path=str(paths[0]),
        line_start=1, line_end=2, language="python",
    ))
    for path in paths[1:]:
        for i in range(12):
            name = f"caller_with_a_descriptive_function_name_{i}"
            store.upsert_node(NodeInfo(
                kind="Function", name=name, file_path=str(path),
                line_start=1, line_end=2, language="python",
            ))
            store.upsert_edge(EdgeInfo(
                kind="CALLS", source=f"{path}::{name}",
                target=f"{paths[0]}::execute", file_path=str(path), line=1,
            ))
    store.commit()
    store.close()
    return root


def test_compact_preserves_files_without_repeating_nodes_and_edges(impact_repo):
    args = {"repo_root": str(impact_repo), "changed_files": ["src/core.py"]}
    standard = query.get_impact_radius(**args)
    compact = query.get_impact_radius(**args, detail_level="compact", max_files=10)
    assert compact["status"] == "ok"
    assert set(compact["impacted_files"]) == {
        Path(p).relative_to(impact_repo).as_posix() for p in standard["impacted_files"]
    }
    assert compact["changed_files"] == ["src/core.py"]
    assert compact["total_impacted"] == standard["total_impacted"]
    assert compact["impacted_file_count"] == 3
    assert compact["impacted_files_omitted"] == 0
    assert compact["truncated"] is False
    assert compact["unresolved_call_sites"] == standard["unresolved_call_sites"]
    assert not {"changed_nodes", "impacted_nodes", "edges"} & compact.keys()
    assert str(impact_repo) not in json.dumps(compact)
    assert len(json.dumps(compact)) < len(json.dumps(standard)) / 4


def test_file_limit_does_not_shrink_the_traversal(impact_repo):
    args = {"repo_root": str(impact_repo), "changed_files": ["src/core.py"]}
    full = query.get_impact_radius(**args)
    compact = query.get_impact_radius(**args, detail_level="compact", max_files=1)
    assert len(compact["impacted_files"]) == 1
    assert compact["impacted_file_count"] == 3
    assert compact["impacted_files_omitted"] == 2
    assert compact["total_impacted"] == full["total_impacted"]
    assert compact["truncated"] is True


def test_compact_skips_full_graph_serialization(impact_repo, monkeypatch):
    def unexpected_serialization(*args, **kwargs):
        pytest.fail("compact mode must not serialize full graph records")

    monkeypatch.setattr(query, "node_to_dict", unexpected_serialization)
    monkeypatch.setattr(query, "edge_to_dict", unexpected_serialization)
    result = query.get_impact_radius(
        changed_files=["src/core.py"], repo_root=str(impact_repo), detail_level="compact",
    )
    assert result["impacted_file_count"] == 3


def test_compact_keeps_the_hard_file_ceiling(impact_repo, monkeypatch):
    monkeypatch.setattr(query, "_MAX_IMPACT_FILES", 2)
    result = query.get_impact_radius(
        changed_files=["src/core.py"], repo_root=str(impact_repo),
        detail_level="compact", max_files=10000,
    )
    assert len(result["impacted_files"]) == 2
    assert result["impacted_files_omitted"] == 1
    assert result["truncated"] is True


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_rejects_invalid_file_limit(impact_repo, limit):
    with pytest.raises(ValueError, match="max_files"):
        query.get_impact_radius(
            changed_files=["src/core.py"], repo_root=str(impact_repo),
            detail_level="compact", max_files=limit,
        )


def test_mcp_exposes_compact_file_context(impact_repo):
    result = asyncio.run(main.get_impact_radius_tool(
        changed_files=["src/core.py"], repo_root=str(impact_repo),
        detail_level="compact", max_files=1,
    ))
    assert result["status"] == "ok"
    assert len(result["impacted_files"]) == 1
    assert result["impacted_files_omitted"] == 2


def test_cli_exposes_compact_file_context(impact_repo, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "code-review-graph", "impact", "--repo", str(impact_repo),
        "--files", "src/core.py", "--detail-level", "compact", "--max-files", "1",
    ])
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert len(result["impacted_files"]) == 1
    assert result["impacted_files_omitted"] == 2


def test_empty_change_list_keeps_compact_shape(impact_repo):
    result = query.get_impact_radius(
        changed_files=[], repo_root=str(impact_repo), detail_level="compact",
    )
    assert result["impacted_files"] == []
    assert result["impacted_file_count"] == 0
    assert result["impacted_files_omitted"] == 0
    assert result["total_impacted"] == 0
    assert result["truncated"] is False
    assert "changed_nodes" not in result


def test_unindexed_file_keeps_uncertainty_marker(impact_repo):
    (impact_repo / "unindexed.py").write_text("def missing(): pass\n")
    result = query.get_impact_radius(
        changed_files=["unindexed.py"], repo_root=str(impact_repo), detail_level="compact",
    )
    assert result["impacted_files"] == []
    standard = query.get_impact_radius(
        changed_files=["unindexed.py"], repo_root=str(impact_repo),
    )
    assert result["confidence"]
    assert result["confidence"] == standard["confidence"]


def test_standard_response_is_unchanged_by_compact_file_limit(impact_repo):
    args = {"repo_root": str(impact_repo), "changed_files": ["src/core.py"]}
    original = query.get_impact_radius(**args)
    small_file_limit = query.get_impact_radius(**args, max_files=1)
    assert original == small_file_limit


def test_compact_files_follow_best_first_node_order(impact_repo):
    args = {"repo_root": str(impact_repo), "changed_files": ["src/core.py"]}
    standard = query.get_impact_radius(**args)
    first = standard["impacted_nodes"][0]["file_path"]
    compact = query.get_impact_radius(**args, detail_level="compact", max_files=1)
    assert compact["impacted_files"] == [Path(first).relative_to(impact_repo).as_posix()]
