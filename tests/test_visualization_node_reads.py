"""Full-export read scaling and file-scope compatibility for PR #1165."""

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.visualization import export_graph_data


@pytest.mark.parametrize("unrelated_files", [100, 200, 400])
@pytest.mark.parametrize("view", ["full", "path"])
def test_full_export_batches_node_reads(tmp_path, unrelated_files, view):
    with GraphStore(tmp_path / "graph.db") as store:
        for index in range(unrelated_files + 1):
            file_path = f"file_{index:04}.py"
            store.upsert_node(NodeInfo("File", file_path, file_path, 1, 2, "python"))
            store.upsert_node(NodeInfo("Function", "run", file_path, 1, 2, "python"))
            store.upsert_edge(EdgeInfo("CONTAINS", file_path, f"{file_path}::run", file_path))
        statements = []
        store._conn.set_trace_callback(statements.append)
        try:
            options = (
                {"path_from": "file_0000.py::run", "path_to": "file_0000.py::run"}
                if view == "path"
                else {}
            )
            data = export_graph_data(store, **options)
        finally:
            store._conn.set_trace_callback(None)

        expected = (
            {"file_0000.py", "file_0000.py::run"}
            if view == "path"
            else {
                name
                for index in range(unrelated_files + 1)
                for name in (f"file_{index:04}.py", f"file_{index:04}.py::run")
            }
        )
        assert {node["qualified_name"] for node in data["nodes"]} == expected
        node_reads = [sql for sql in statements if sql.startswith("SELECT * FROM nodes")]
        assert len(node_reads) == 1, node_reads


def test_export_preserves_file_scope_and_ambiguous_target_order(tmp_path):
    with GraphStore(tmp_path / "graph.db") as store:
        # Insert the later path first: file order, rather than insertion order,
        # determines the first candidate for a cross-directory bare call.
        for file_path in ("b/target.py", "a/target.py", "caller.py"):
            store.upsert_node(NodeInfo("File", file_path, file_path, 1, 2, "python"))
            name = "call" if file_path == "caller.py" else "run"
            store.upsert_node(NodeInfo("Function", name, file_path, 1, 2, "python"))
        store.upsert_node(
            NodeInfo("Function", "hidden", "virtual.py", 1, 2, "python", extra={"virtual": True})
        )
        store.upsert_node(
            NodeInfo("Function", "sibling", "caller.py", 1, 2, "python", extra={"virtual": True})
        )
        store.upsert_node(
            NodeInfo("Function", "edge_owned", "edge.py", 1, 2, "python", extra={"virtual": True})
        )
        store.upsert_edge(EdgeInfo("CALLS", "caller.py::call", "run", "caller.py"))
        store.upsert_edge(EdgeInfo("CALLS", "edge.py::edge_owned", "missing", "edge.py"))

        data = export_graph_data(store)

        assert [node["qualified_name"] for node in data["nodes"]] == [
            "a/target.py",
            "a/target.py::run",
            "b/target.py",
            "b/target.py::run",
            "caller.py",
            "caller.py::call",
            "caller.py::sibling",
            "edge.py::edge_owned",
        ]
        assert [(edge["source"], edge["target"]) for edge in data["edges"]] == [
            ("caller.py::call", "a/target.py::run")
        ]
