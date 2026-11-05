"""Failure and repeated-update contracts for batched, indexed graphs (#858, #942)."""

import sqlite3

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.tools.query import query_graph


def function(path, name, *, parent=None, params=""):
    return NodeInfo(
        kind="Function",
        name=name,
        file_path=str(path),
        line_start=1,
        line_end=3,
        language="csharp",
        parent_name=parent,
        params=params,
    )


def graph_path(root):
    directory = root / ".code-review-graph"
    directory.mkdir()
    return directory / "graph.db"


@pytest.mark.parametrize("operation", ["single", "batch", "delete"])
def test_sqlite_failure_preserves_committed_nodes_edges_and_future_writes(tmp_path, operation):
    """Committing a partial transaction loses old context and makes this fail."""
    root = tmp_path.resolve()
    database = graph_path(root)
    first, second = root / "first.cs", root / "second.cs"
    first_qn, second_qn = f"{first}::Before.first", f"{second}::Before.second"
    with GraphStore(database) as store:
        store.store_file_batch(
            [
                (
                    str(first),
                    [function(first, "first", parent="Before")],
                    [
                        EdgeInfo("CALLS", first_qn, second_qn, str(first), 2),
                    ],
                    "first-hash",
                ),
                (str(second), [function(second, "second", parent="Before")], [], "second-hash"),
            ]
        )
        # A real SQLite error occurs after the first batch member was changed,
        # or after the single-file replacement deleted its original rows.
        if operation == "delete":
            store._conn.execute(
                "CREATE TRIGGER reject_delete BEFORE DELETE ON nodes "
                "WHEN OLD.name = 'second' BEGIN "
                "SELECT RAISE(ABORT, 'controlled write failure'); END"
            )
        else:
            store._conn.execute(
                "CREATE TRIGGER reject_insert BEFORE INSERT ON nodes "
                "WHEN NEW.name = 'reject' BEGIN "
                "SELECT RAISE(ABORT, 'controlled write failure'); END"
            )
        store.commit()
        with pytest.raises(sqlite3.IntegrityError, match="controlled write failure"):
            if operation == "single":
                store.store_file_nodes_edges(
                    str(first),
                    [function(first, "reject")],
                    [],
                    "bad-hash",
                )
            elif operation == "batch":
                store.store_file_batch(
                    [
                        (str(first), [function(first, "replacement")], [], "new-hash"),
                        (str(second), [function(second, "reject")], [], "bad-hash"),
                    ]
                )
            else:
                store.remove_files_permanently([str(first), str(second)])
        assert not store._conn.in_transaction
        assert {node.qualified_name for node in store.get_all_nodes()} == {first_qn, second_qn}
        assert store.get_node(first_qn).file_hash == "first-hash"
        assert store.get_node(second_qn).file_hash == "second-hash"
        assert [edge.target_qualified for edge in store.get_edges_by_source(first_qn)] == [
            second_qn
        ]
        assert [
            node.qualified_name for node in store.search_nodes_by_qualified_tail("Before.first")
        ] == [first_qn]
        store._conn.execute(
            f"DROP TRIGGER {'reject_delete' if operation == 'delete' else 'reject_insert'}"
        )
        store.commit()
    # Reopening also checks durable state, rather than only this connection's view.
    with GraphStore(database) as store:
        assert {node.qualified_name for node in store.get_all_nodes()} == {first_qn, second_qn}
        assert [edge.target_qualified for edge in store.get_edges_by_source(first_qn)] == [
            second_qn
        ]
        store.store_file_nodes_edges(str(first), [function(first, "recovered")], [], "good-hash")
        assert store.get_node(f"{first}::recovered").file_hash == "good-hash"
        assert store.get_node(second_qn) is not None


@pytest.mark.parametrize("writer", ["single", "batch"])
def test_indexed_symbol_survives_replacement_reopen_and_permanent_deletion(tmp_path, writer):
    """Missing bulk symbol maintenance or stale deleted tails loses exact addressing."""
    root = tmp_path.resolve()
    database = graph_path(root)
    source = root / "service #1 café.cs"
    with GraphStore(database) as store:
        store.upsert_node(function(source, "run", parent="Old.Service"))
        store.commit()
        for revision in ["one", "two"]:
            nodes = [function(source, "run", parent="New.Service", params=revision)]
            if writer == "single":
                store.store_file_nodes_edges(str(source), nodes, [], revision)
            else:
                store.store_file_batch([(str(source), nodes, [], revision)])
            assert store.search_nodes_by_qualified_tail("Old.Service.run") == []
            current = store.search_nodes_by_qualified_tail("New.Service.run")
            assert len(current) == 1
            assert current[0].qualified_name == f"{source}::New.Service.run"
            assert current[0].params == revision
            assert current[0].file_hash == revision
    result = query_graph("callees_of", "New.Service.run", repo_root=str(root))
    assert result["status"] == "ok"
    assert result["target"] == f"{source}::New.Service.run"
    with GraphStore(database) as store:
        assert store.count_nodes_by_qualified_tail("New.Service.run") == 1
        assert store.remove_files_permanently([str(source), str(source)]) == 1
    with GraphStore(database) as store:
        assert store.count_nodes_by_qualified_tail("New.Service.run") == 0
        assert store.search_nodes_by_qualified_tail("New.Service.run") == []


@pytest.mark.parametrize("detail", ["standard", "minimal"])
def test_dotted_query_rechecks_ambiguity_after_addition_and_deletion(tmp_path, detail):
    """An old cached resolution must not silently select one of two same-tail symbols."""
    root = tmp_path.resolve()
    database = graph_path(root)
    first, second = root / "a.cs", root / "b.cs"
    target = "Outer.Service.run"
    with GraphStore(database) as store:
        store.store_file_nodes_edges(
            str(first), [function(first, "run", parent="Outer.Service")], []
        )
    single = query_graph("callers_of", target, repo_root=str(root), detail_level=detail)
    assert single["status"] == "ok"
    assert single["target"] == f"{first}::{target}"
    with GraphStore(database) as store:
        store.store_file_batch(
            [(str(second), [function(second, "run", parent="Outer.Service")], [], "")]
        )
    ambiguous = query_graph("callers_of", target, repo_root=str(root), detail_level=detail)
    assert ambiguous["status"] == "ambiguous"
    assert ambiguous["candidate_count"] == 2
    assert {node["qualified_name"] for node in ambiguous["candidates"]} == {
        f"{first}::{target}",
        f"{second}::{target}",
    }
    with GraphStore(database) as store:
        store.remove_file_permanently(str(first))
    remaining = query_graph("callers_of", target, repo_root=str(root), detail_level=detail)
    assert remaining["status"] == "ok"
    assert remaining["target"] == f"{second}::{target}"
