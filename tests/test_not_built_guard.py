"""Tests for the graph-not-built guard on read tools.

A read tool run against a repo whose graph was never built must return a
``not_built`` status instead of a misleading empty result. A built graph
(even one with zero matches) must keep returning a normal response.
"""

from __future__ import annotations

import builtins
import sqlite3
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import NodeInfo
from code_review_graph.tools import (
    get_architecture_overview_func,
    get_impact_radius,
    list_communities_func,
    list_flows,
    list_graph_stats,
    query_graph,
    semantic_search_nodes,
)
from code_review_graph.tools._common import (
    _get_store_for_read,
    _graph_is_built,
    _not_built_response,
)


def _make_unbuilt_repo(tmp_path: Path) -> Path:
    """A repo that looks like a project root but has never been built."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".code-review-graph").mkdir()
    return tmp_path


def _make_built_repo(tmp_path: Path, *, with_nodes: bool = True) -> Path:
    """A repo with a real graph.db that has been through a build."""
    (tmp_path / ".git").mkdir()
    db_dir = tmp_path / ".code-review-graph"
    db_dir.mkdir()
    db_path = db_dir / "graph.db"
    with GraphStore(db_path) as store:
        if with_nodes:
            store.upsert_node(
                NodeInfo(
                    kind="File",
                    name="a.py",
                    file_path=str(tmp_path / "a.py"),
                    line_start=1,
                    line_end=10,
                    language="python",
                )
            )
            store.upsert_node(
                NodeInfo(
                    kind="Function",
                    name="hello",
                    file_path=str(tmp_path / "a.py"),
                    line_start=2,
                    line_end=4,
                    language="python",
                )
            )
        else:
            # Simulate a build over an empty repo: a build marker is written
            # even though no nodes were produced.
            store.set_metadata("last_updated", "2026-01-01T00:00:00")
            store.set_metadata("last_build_type", "full")
        store.commit()
    return tmp_path


# ---------------------------------------------------------------------------
# Helper-level unit tests
# ---------------------------------------------------------------------------


def test_not_built_response_shape():
    resp = _not_built_response()
    assert resp["status"] == "not_built"
    assert "build_or_update_graph_tool" in resp["message"]
    assert resp["next_tool_suggestions"] == ["build_or_update_graph_tool"]


def test_graph_is_built_false_when_empty(tmp_path):
    db_path = tmp_path / "graph.db"
    with GraphStore(db_path) as store:
        assert _graph_is_built(store) is False


def test_graph_is_built_true_with_nodes(tmp_path):
    db_path = tmp_path / "graph.db"
    with GraphStore(db_path) as store:
        store.upsert_node(
            NodeInfo(
                kind="File",
                name="a.py",
                file_path="a.py",
                line_start=1,
                line_end=1,
                language="python",
            )
        )
        store.commit()
        assert _graph_is_built(store) is True


def test_graph_is_built_true_with_build_marker_no_nodes(tmp_path):
    db_path = tmp_path / "graph.db"
    with GraphStore(db_path) as store:
        store.set_metadata("last_updated", "2026-01-01T00:00:00")
        store.commit()
        assert _graph_is_built(store) is True


def test_get_store_for_read_missing_db(tmp_path):
    _make_unbuilt_repo(tmp_path)
    store, root, not_built = _get_store_for_read(str(tmp_path))
    assert store is None
    assert root is None
    assert not_built is not None
    assert not_built["status"] == "not_built"
    # No graph.db should have been created by the guard.
    assert not (tmp_path / ".code-review-graph" / "graph.db").exists()


def test_get_store_for_read_built(tmp_path):
    _make_built_repo(tmp_path)
    store, root, not_built = _get_store_for_read(str(tmp_path))
    try:
        assert not_built is None
        assert store is not None
        assert root is not None
    finally:
        if store is not None:
            store.close()


# ---------------------------------------------------------------------------
# Read tools on an unbuilt repo -> not_built
# ---------------------------------------------------------------------------


def test_query_graph_unbuilt_returns_not_built(tmp_path):
    _make_unbuilt_repo(tmp_path)
    result = query_graph(pattern="callers_of", target="anything", repo_root=str(tmp_path))
    assert result["status"] == "not_built"
    assert "build_or_update_graph_tool" in result["next_tool_suggestions"]


def test_semantic_search_unbuilt_returns_not_built(tmp_path):
    _make_unbuilt_repo(tmp_path)
    result = semantic_search_nodes("anything", repo_root=str(tmp_path))
    assert result["status"] == "not_built"


def test_impact_radius_unbuilt_returns_not_built(tmp_path):
    _make_unbuilt_repo(tmp_path)
    result = get_impact_radius(changed_files=["a.py"], repo_root=str(tmp_path))
    assert result["status"] == "not_built"


def test_list_graph_stats_unbuilt_returns_not_built(tmp_path):
    _make_unbuilt_repo(tmp_path)
    result = list_graph_stats(repo_root=str(tmp_path))
    assert result["status"] == "not_built"


def test_list_flows_unbuilt_returns_not_built(tmp_path):
    _make_unbuilt_repo(tmp_path)
    result = list_flows(repo_root=str(tmp_path))
    assert result["status"] == "not_built"


def test_list_communities_unbuilt_returns_not_built(tmp_path):
    _make_unbuilt_repo(tmp_path)
    result = list_communities_func(repo_root=str(tmp_path))
    assert result["status"] == "not_built"


def test_architecture_overview_unbuilt_returns_not_built(tmp_path):
    _make_unbuilt_repo(tmp_path)
    result = get_architecture_overview_func(repo_root=str(tmp_path))
    assert result["status"] == "not_built"


# ---------------------------------------------------------------------------
# Built graph with no match -> normal empty result (NOT the guard)
# ---------------------------------------------------------------------------


def test_query_graph_built_no_match_is_not_guarded(tmp_path):
    _make_built_repo(tmp_path)
    result = query_graph(
        pattern="callers_of",
        target="does_not_exist",
        repo_root=str(tmp_path),
    )
    assert result["status"] != "not_built"
    # Real "no node" path, not the build guard.
    assert result["status"] in ("not_found", "ok")


def test_semantic_search_built_no_match_is_not_guarded(tmp_path):
    _make_built_repo(tmp_path)
    result = semantic_search_nodes("zzz_nonexistent_zzz", repo_root=str(tmp_path))
    assert result["status"] == "ok"
    assert result["results"] == []


def test_semantic_search_without_embeddings_reports_lexical_mode(tmp_path):
    from code_review_graph.search import rebuild_fts_index

    _make_built_repo(tmp_path)
    with GraphStore(tmp_path / ".code-review-graph" / "graph.db") as store:
        rebuild_fts_index(store)
    result = semantic_search_nodes("hello", repo_root=str(tmp_path))
    assert result["status"] == "ok"
    assert result["search_mode"] == "fts"


def test_semantic_search_without_embeddings_does_not_import_provider(tmp_path, monkeypatch):
    _make_built_repo(tmp_path)
    real_import = builtins.__import__

    def fail_embeddings_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "code_review_graph.embeddings" or (
            level and fromlist and "EmbeddingStore" in fromlist
        ):
            raise AssertionError(
                "semantic_search_nodes must not import embedding providers when no vectors exist"
            )
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fail_embeddings_import)

    result = semantic_search_nodes("zzz_nonexistent_zzz", repo_root=str(tmp_path))
    assert result["status"] == "ok"
    assert result["results"] == []


def test_list_graph_stats_built_returns_ok(tmp_path):
    _make_built_repo(tmp_path)
    result = list_graph_stats(repo_root=str(tmp_path))
    assert result["status"] == "ok"
    assert result["total_nodes"] >= 1


def test_list_graph_stats_does_not_initialize_embedding_provider(tmp_path, monkeypatch):
    _make_built_repo(tmp_path)

    def fail_provider(*_args, **_kwargs):
        raise AssertionError("list_graph_stats must not initialize embedding providers")

    monkeypatch.setattr("code_review_graph.embeddings.get_provider", fail_provider)

    result = list_graph_stats(repo_root=str(tmp_path))
    assert result["status"] == "ok"
    assert result["embeddings_count"] == 0


def test_list_graph_stats_does_not_hide_broken_embeddings_schema(tmp_path):
    _make_built_repo(tmp_path)
    db_path = tmp_path / ".code-review-graph" / "graph.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE VIEW embeddings AS SELECT * FROM missing_embedding_table")
        conn.commit()
    finally:
        conn.close()

    try:
        list_graph_stats(repo_root=str(tmp_path))
    except sqlite3.OperationalError as exc:
        assert "missing_embedding_table" in str(exc)
    else:
        raise AssertionError("broken embeddings schema must fail loud")


def test_built_but_empty_graph_is_not_guarded(tmp_path):
    """A build over an empty repo (0 nodes + build marker) is not 'not_built'."""
    _make_built_repo(tmp_path, with_nodes=False)
    result = list_graph_stats(repo_root=str(tmp_path))
    assert result["status"] == "ok"
    assert result["total_nodes"] == 0
