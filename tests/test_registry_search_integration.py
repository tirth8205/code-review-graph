"""Real registry/database boundaries for scoped search (#915)."""

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import NodeInfo
from code_review_graph.registry import Registry
from code_review_graph.tools.registry_tools import cross_repo_search_func


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    return Registry()


def indexed_repo(root, registry, *, alias=None, count=1):
    root = root.resolve()
    directory = root / ".code-review-graph"
    directory.mkdir(parents=True)
    with GraphStore(directory / "graph.db") as store:
        store.store_file_nodes_edges(
            str(root / "service.py"),
            [
                NodeInfo(
                    kind="Function",
                    name=f"lookup_{index:03}",
                    file_path=str(root / "service.py"),
                    line_start=index + 1,
                    line_end=index + 1,
                    language="python",
                )
                for index in range(count)
            ],
            [],
        )
    registry.register(str(root), alias=alias)
    return root


def test_alias_and_folder_requests_search_one_real_repo_once(tmp_path, registry):
    chosen = indexed_repo(tmp_path / "service #1 café", registry, alias="chosen")
    indexed_repo(tmp_path / "decoy", registry, alias="decoy")
    result = cross_repo_search_func(
        "lookup",
        repos=["chosen", "service #1 café", "chosen"],
        kind="Function",
    )
    assert result["status"] == "ok"
    assert result["repos_searched"] == ["chosen"]
    assert result["total"] == 1
    assert [(row["repo"], row["file_path"]) for row in result["results"]] == [
        ("chosen", str(chosen / "service.py")),
    ]
    assert result["unknown"] == result["ambiguous"] == []


@pytest.mark.parametrize("names", [None, []])
def test_omitted_and_empty_selection_keep_existing_all_repo_search(tmp_path, registry, names):
    indexed_repo(tmp_path / "one", registry, alias="first")
    indexed_repo(tmp_path / "two", registry, alias="second")
    result = cross_repo_search_func("lookup", repos=names, kind="Function")
    assert result["status"] == "ok"
    assert result["repos_searched"] == ["first", "second"]
    assert [row["repo"] for row in result["results"]] == ["first", "second"]
    assert result["total"] == 2
    assert result["truncated"] is False


def test_explicit_alias_beats_another_repos_folder_in_actual_search(tmp_path, registry):
    indexed_repo(tmp_path / "wanted", registry, alias="incidental")
    intended = indexed_repo(tmp_path / "different", registry, alias="wanted")
    result = cross_repo_search_func("lookup", repos=["wanted"], kind="Function")
    assert result["repos_searched"] == ["wanted"]
    assert {row["repo_path"] for row in result["results"]} == {str(intended)}
    assert result["total"] == 1
    assert result["ambiguous"] == []


def test_shared_folder_selects_both_real_graphs_and_reports_ambiguity(tmp_path, registry):
    first = indexed_repo(tmp_path / "one" / "shared", registry, alias="first")
    second = indexed_repo(tmp_path / "two" / "shared", registry, alias="second")
    result = cross_repo_search_func("lookup", repos=["shared"], kind="Function")
    assert result["repos_searched"] == ["first", "second"]
    assert {row["repo_path"] for row in result["results"]} == {str(first), str(second)}
    assert result["ambiguous"] == ["shared"]
    assert result["ambiguous_total"] == 1
    assert result["total"] == 2


@pytest.mark.parametrize(
    "unknown_count,want_shown,truncated", [(19, 19, False), (20, 20, False), (21, 20, True)]
)
def test_partial_search_echo_bounds_preserve_selected_context(
    tmp_path,
    registry,
    unknown_count,
    want_shown,
    truncated,
):
    indexed_repo(tmp_path / "chosen", registry, alias="chosen")
    unknown = [f"missing-{index}" for index in range(unknown_count)]
    result = cross_repo_search_func("lookup", repos=["chosen", *unknown], kind="Function")
    assert result["total"] == 1
    assert [row["name"] for row in result["results"]] == ["lookup_000"]
    assert result["unknown"] == unknown[:want_shown]
    assert result["unknown_total"] == unknown_count
    assert result["unknown_truncated"] is truncated
    assert result["truncated"] is False


def test_global_result_ceiling_is_honest_with_real_rows(tmp_path, registry):
    indexed_repo(tmp_path / "large", registry, alias="large", count=110)
    result = cross_repo_search_func(
        "lookup",
        repos=["large"],
        kind="Function",
        limit=110,
        max_results=500,
    )
    assert result["status"] == "ok"
    assert result["total"] == 110
    assert len(result["results"]) == 100
    assert len({row["qualified_name"] for row in result["results"]}) == 100
    assert result["truncated"] is True
