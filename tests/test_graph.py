"""Tests for the graph storage and query engine."""

import logging
import sqlite3
import tempfile
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo


class TestGraphStore:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _make_file_node(self, path="/test/file.py"):
        return NodeInfo(
            kind="File", name=path, file_path=path,
            line_start=1, line_end=100, language="python",
        )

    def _make_func_node(self, name="my_func", path="/test/file.py", parent=None, is_test=False):
        return NodeInfo(
            kind="Test" if is_test else "Function",
            name=name, file_path=path,
            line_start=10, line_end=20, language="python",
            parent_name=parent, is_test=is_test,
        )

    def _make_class_node(self, name="MyClass", path="/test/file.py"):
        return NodeInfo(
            kind="Class", name=name, file_path=path,
            line_start=5, line_end=50, language="python",
        )

    def test_upsert_and_get_node(self):
        node = self._make_file_node()
        self.store.upsert_node(node)
        self.store.commit()

        result = self.store.get_node("/test/file.py")
        assert result is not None
        assert result.kind == "File"
        assert result.name == "/test/file.py"

    def test_upsert_function_node(self):
        func = self._make_func_node()
        self.store.upsert_node(func)
        self.store.commit()

        result = self.store.get_node("/test/file.py::my_func")
        assert result is not None
        assert result.kind == "Function"
        assert result.name == "my_func"

    def test_upsert_method_node(self):
        method = self._make_func_node(name="do_thing", parent="MyClass")
        self.store.upsert_node(method)
        self.store.commit()

        result = self.store.get_node("/test/file.py::MyClass.do_thing")
        assert result is not None
        assert result.parent_name == "MyClass"

    def test_upsert_edge(self):
        edge = EdgeInfo(
            kind="CALLS",
            source="/test/file.py::func_a",
            target="/test/file.py::func_b",
            file_path="/test/file.py",
            line=15,
        )
        self.store.upsert_edge(edge)
        self.store.commit()

        edges = self.store.get_edges_by_source("/test/file.py::func_a")
        assert len(edges) == 1
        assert edges[0].kind == "CALLS"
        assert edges[0].target_qualified == "/test/file.py::func_b"

    def test_remove_file_data(self):
        node = self._make_file_node()
        func = self._make_func_node()
        self.store.upsert_node(node)
        self.store.upsert_node(func)
        self.store.commit()

        self.store.remove_file_data("/test/file.py")
        self.store.commit()

        assert self.store.get_node("/test/file.py") is None
        assert self.store.get_node("/test/file.py::my_func") is None

    def test_store_file_nodes_edges(self):
        nodes = [self._make_file_node(), self._make_func_node()]
        edges = [
            EdgeInfo(
                kind="CONTAINS", source="/test/file.py",
                target="/test/file.py::my_func", file_path="/test/file.py",
            )
        ]
        self.store.store_file_nodes_edges("/test/file.py", nodes, edges)

        result = self.store.get_nodes_by_file("/test/file.py")
        assert len(result) == 2

    def test_store_after_remove_no_transaction_error(self):
        """Regression test for #135: store_file_nodes_edges after
        remove_file_data must not raise 'cannot start a transaction
        within a transaction'.
        """
        # Seed initial data for two files
        nodes_a = [self._make_file_node("/test/a.py")]
        nodes_b = [self._make_file_node("/test/b.py")]
        self.store.store_file_nodes_edges("/test/a.py", nodes_a, [])
        self.store.store_file_nodes_edges("/test/b.py", nodes_b, [])

        # Without the isolation_level=None fix, this would leave an
        # implicit transaction open and the next call would crash.
        self.store.remove_file_data("/test/a.py")
        # Must not raise sqlite3.OperationalError
        nodes_c = [self._make_file_node("/test/c.py")]
        self.store.store_file_nodes_edges("/test/c.py", nodes_c, [])

        assert self.store.get_node("/test/a.py") is None
        assert self.store.get_node("/test/c.py") is not None

    def test_store_after_multiple_removes_no_transaction_error(self):
        """Regression test for #181: full_build stale-file purge leaves
        implicit transaction open after multiple remove_file_data calls.
        """
        # Seed data for several files
        for i in range(5):
            path = f"/test/file_{i}.py"
            self.store.store_file_nodes_edges(
                path, [self._make_file_node(path)], [],
            )

        # Simulates full_build's stale-file purge: multiple deletes in a
        # row without explicit commit between them.
        for i in range(3):
            self.store.remove_file_data(f"/test/file_{i}.py")

        # Next store call must succeed regardless of prior connection state.
        new_path = "/test/new_file.py"
        nodes = [self._make_file_node(new_path)]
        self.store.store_file_nodes_edges(new_path, nodes, [])

        assert self.store.get_node(new_path) is not None
        assert self.store.get_node("/test/file_0.py") is None

    def test_search_nodes(self):
        self.store.upsert_node(self._make_func_node("authenticate"))
        self.store.upsert_node(self._make_func_node("authorize"))
        self.store.upsert_node(self._make_func_node("process"))
        self.store.commit()

        results = self.store.search_nodes("auth")
        names = {r.name for r in results}
        assert "authenticate" in names
        assert "authorize" in names
        assert "process" not in names

    def test_get_stats(self):
        self.store.upsert_node(self._make_file_node())
        self.store.upsert_node(self._make_func_node())
        self.store.upsert_node(self._make_class_node())
        self.store.upsert_edge(EdgeInfo(
            kind="CONTAINS", source="/test/file.py",
            target="/test/file.py::my_func", file_path="/test/file.py",
        ))
        self.store.commit()

        stats = self.store.get_stats()
        assert stats.total_nodes == 3
        assert stats.total_edges == 1
        assert stats.nodes_by_kind["File"] == 1
        assert stats.nodes_by_kind["Function"] == 1
        assert stats.nodes_by_kind["Class"] == 1
        assert "python" in stats.languages

    def test_impact_radius(self):
        # Create a chain: file_a -> func_a -> (calls) -> func_b in file_b
        self.store.upsert_node(self._make_file_node("/a.py"))
        self.store.upsert_node(self._make_func_node("func_a", "/a.py"))
        self.store.upsert_node(self._make_file_node("/b.py"))
        self.store.upsert_node(self._make_func_node("func_b", "/b.py"))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="/a.py::func_a",
            target="/b.py::func_b", file_path="/a.py", line=10,
        ))
        self.store.commit()

        result = self.store.get_impact_radius(["/a.py"], max_depth=2)
        assert len(result["changed_nodes"]) > 0
        # func_b in /b.py should be impacted
        impacted_qns = {n.qualified_name for n in result["impacted_nodes"]}
        assert "/b.py::func_b" in impacted_qns or "/b.py" in impacted_qns

    def test_upsert_edge_preserves_multiple_call_sites(self):
        """Multiple CALLS edges to the same target from the same source on different lines."""
        edge1 = EdgeInfo(
            kind="CALLS", source="/test/file.py::caller",
            target="/test/file.py::helper", file_path="/test/file.py", line=10,
        )
        edge2 = EdgeInfo(
            kind="CALLS", source="/test/file.py::caller",
            target="/test/file.py::helper", file_path="/test/file.py", line=20,
        )
        self.store.upsert_edge(edge1)
        self.store.upsert_edge(edge2)
        self.store.commit()

        edges = self.store.get_edges_by_source("/test/file.py::caller")
        assert len(edges) == 2
        lines = {e.line for e in edges}
        assert lines == {10, 20}

    def test_metadata(self):
        self.store.set_metadata("test_key", "test_value")
        assert self.store.get_metadata("test_key") == "test_value"
        assert self.store.get_metadata("nonexistent") is None

    def test_get_all_community_ids_logs_when_column_missing(self, caplog):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE nodes (qualified_name TEXT PRIMARY KEY)"
        )
        store = GraphStore.__new__(GraphStore)
        store._conn = conn

        with caplog.at_level(logging.DEBUG, logger="code_review_graph.graph"):
            result = store.get_all_community_ids()

        assert result == {}
        assert "Community IDs unavailable" in caplog.text
        conn.close()

    def test_get_communities_list_logs_when_table_missing(self, caplog):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        store = GraphStore.__new__(GraphStore)
        store._conn = conn

        with caplog.at_level(logging.DEBUG, logger="code_review_graph.graph"):
            result = store.get_communities_list()

        assert result == []
        assert "Communities list unavailable" in caplog.text
        conn.close()


class TestImpactRadiusSql:
    """Tests for get_impact_radius_sql vs NetworkX BFS."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)
        self._build_chain()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _build_chain(self):
        """Build A -> B -> C -> D chain for testing."""
        for name, path in [
            ("func_a", "/a.py"), ("func_b", "/b.py"),
            ("func_c", "/c.py"), ("func_d", "/d.py"),
        ]:
            self.store.upsert_node(NodeInfo(
                kind="File", name=path, file_path=path,
                line_start=1, line_end=50, language="python",
            ))
            self.store.upsert_node(NodeInfo(
                kind="Function", name=name, file_path=path,
                line_start=5, line_end=20, language="python",
            ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="/a.py::func_a",
            target="/b.py::func_b", file_path="/a.py", line=10,
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="/b.py::func_b",
            target="/c.py::func_c", file_path="/b.py", line=10,
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="/c.py::func_c",
            target="/d.py::func_d", file_path="/c.py", line=10,
        ))
        self.store.commit()

    def test_sql_matches_networkx(self):
        """SQL and NetworkX BFS produce identical impacted node sets."""
        sql_result = self.store.get_impact_radius_sql(["/a.py"], max_depth=2)
        nx_result = self.store._get_impact_radius_networkx(["/a.py"], max_depth=2)

        sql_qns = {n.qualified_name for n in sql_result["impacted_nodes"]}
        nx_qns = {n.qualified_name for n in nx_result["impacted_nodes"]}
        assert sql_qns == nx_qns

    def test_max_nodes_truncation(self):
        """Setting max_nodes=2 should truncate results."""
        result = self.store.get_impact_radius_sql(
            ["/a.py"], max_depth=3, max_nodes=2,
        )
        # With 4 files in chain + file nodes, max_nodes=2 should limit
        assert result["total_impacted"] <= 2 or result["truncated"]

    def test_empty_changed_files(self):
        result = self.store.get_impact_radius_sql([], max_depth=2)
        assert result["changed_nodes"] == []
        assert result["impacted_nodes"] == []
        assert result["total_impacted"] == 0
