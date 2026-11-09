"""Tests for the graph storage and query engine."""

import logging
import os
import sqlite3
import tempfile
import time
from pathlib import Path, PureWindowsPath

import pytest

import code_review_graph.constants as constants_module
import code_review_graph.graph as graph_module
from code_review_graph.analysis import find_bridge_nodes
from code_review_graph.errors import GraphStoreError
from code_review_graph.graph import (
    CorruptGraphDatabaseError,
    GraphStore,
    discard_corrupt_database,
)
from code_review_graph.incremental import full_build
from code_review_graph.parser import EdgeInfo, NodeInfo


def _assert_callers_survive(store, identities, base):
    """Each suffixed duplicate keeps its caller across a re-index. A caller that
    named the base gives no evidence for either definition, so it lists every
    duplicate instead of claiming the first one (#585, #586)."""
    for identity in identities:
        assert store.get_node(identity) is not None
        callers = store.get_edges_by_target(identity)
        if identity == base:
            assert callers == []
        else:
            assert len(callers) == 1
            assert store.get_node(callers[0].source_qualified) is not None
    if base in identities:
        [ambiguous] = [
            edge for edge in store.get_all_edges()
            if edge.kind == "CALLS" and edge.target_qualified == "render"
        ]
        assert set(ambiguous.extra["ambiguous_targets"]) == set(identities)
        assert store.get_node(ambiguous.source_qualified) is not None


class TestGraphStore:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()  # release the handle before GraphStore reopens it on Windows
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

    def test_upsert_function_overwrites_line_range_for_same_qualified_name(self):
        first = self._make_func_node()
        second = self._make_func_node()
        first.line_start, first.line_end = 2, 4
        second.line_start, second.line_end = 10, 12

        self.store.upsert_node(first)
        self.store.upsert_node(second)
        self.store.commit()

        result = self.store.get_node("/test/file.py::my_func")
        assert result is not None
        assert (result.line_start, result.line_end) == (10, 12)

    def test_store_file_nodes_edges_keeps_each_duplicate_node_range(self):
        """#585: a same-file duplicate keeps its own row and range instead of
        overwriting the first definition's (the pre-#585 collapse #980 pinned).
        """
        first = self._make_func_node(name="helper")
        second = self._make_func_node(name="helper")
        first.parent_name = "outer1"
        second.parent_name = "outer1"
        first.line_start, first.line_end = 2, 3
        second.line_start, second.line_end = 8, 9

        self.store.store_file_nodes_edges(
            "/test/file.py", [first, second], [],
        )

        result = self.store.get_node("/test/file.py::outer1.helper")
        assert result is not None
        assert (result.line_start, result.line_end) == (2, 3)
        ranges = sorted(
            (node.line_start, node.line_end)
            for node in self.store.get_nodes_by_file("/test/file.py")
            if node.name == "helper"
        )
        assert ranges == [(2, 3), (8, 9)]

    def test_same_name_nested_functions_keep_separate_line_ranges(self):
        first = self._make_func_node(name="helper")
        second = self._make_func_node(name="helper")
        first.parent_name = "outer1"
        second.parent_name = "outer2"
        first.line_start, first.line_end = 2, 3
        second.line_start, second.line_end = 8, 9

        self.store.store_file_nodes_edges(
            "/test/file.py", [first, second], [],
        )

        first_result = self.store.get_node("/test/file.py::outer1.helper")
        second_result = self.store.get_node("/test/file.py::outer2.helper")
        assert first_result is not None
        assert second_result is not None
        assert (first_result.line_start, first_result.line_end) == (2, 3)
        assert (second_result.line_start, second_result.line_end) == (8, 9)

    def test_upsert_method_node(self):
        method = self._make_func_node(name="do_thing", parent="MyClass")
        self.store.upsert_node(method)
        self.store.commit()

        result = self.store.get_node("/test/file.py::MyClass.do_thing")
        assert result is not None
        assert result.parent_name == "MyClass"

    def test_get_node_bridges_windows_native_qualified_names(self):
        """A Windows-native path prefix still finds the POSIX-keyed node (#774)."""
        path = "repo/pkg/mod.py"
        self.store.upsert_node(self._make_file_node(path))
        self.store.upsert_node(self._make_func_node("my_func", path))
        self.store.commit()

        native_prefix = str(PureWindowsPath(path))
        assert native_prefix == "repo\\pkg\\mod.py"
        native_qn = f"{native_prefix}::my_func"
        result = self.store.get_node(native_qn)
        assert result is not None
        assert result.qualified_name == "repo/pkg/mod.py::my_func"

        file_node = self.store.get_node(native_prefix)
        assert file_node is not None
        assert file_node.qualified_name == "repo/pkg/mod.py"

        assert self.store.get_node(f"{native_prefix}::missing") is None

    def test_get_node_bridge_keeps_php_backslashes_in_symbol_part(self):
        """Only the path component is normalized; PHP FQN symbols keep ``\\``."""
        node = NodeInfo(
            kind="Class", name="App\\Domain\\Job", file_path="src/App.php",
            line_start=1, line_end=10, language="php",
        )
        self.store.upsert_node(node)
        self.store.commit()

        posix_qn = "src/App.php::App\\Domain\\Job"
        assert self.store.get_node(posix_qn) is not None
        native_qn = "src\\App.php::App\\Domain\\Job"
        bridged = self.store.get_node(native_qn)
        assert bridged is not None
        assert bridged.qualified_name == posix_qn

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

    def test_remove_file_permanently_removes_references_and_same_db_embeddings(self):
        deleted_path = "/test/deleted.py"
        survivor_path = "/test/survivor.py"
        deleted_qn = f"{deleted_path}::removed"
        survivor_qn = f"{survivor_path}::caller"
        self.store.store_file_nodes_edges(
            deleted_path,
            [
                self._make_file_node(deleted_path),
                self._make_func_node("removed", deleted_path),
            ],
            [],
        )
        self.store.store_file_nodes_edges(
            survivor_path,
            [
                self._make_file_node(survivor_path),
                self._make_func_node("caller", survivor_path),
            ],
            [
                EdgeInfo(
                    kind="CALLS",
                    source=survivor_qn,
                    target=deleted_qn,
                    file_path=survivor_path,
                ),
            ],
        )
        self.store._conn.execute(
            "CREATE TABLE embeddings ("
            "qualified_name TEXT PRIMARY KEY, vector BLOB NOT NULL, "
            "text_hash TEXT NOT NULL, provider TEXT NOT NULL)"
        )
        self.store._conn.executemany(
            "INSERT INTO embeddings VALUES (?, ?, ?, ?)",
            [
                (deleted_qn, b"deleted", "deleted", "test"),
                (survivor_qn, b"survivor", "survivor", "test"),
                ("unrelated::orphan", b"orphan", "orphan", "test"),
            ],
        )
        self.store.commit()

        self.store.remove_file_permanently(deleted_path)
        self.store.commit()

        assert self.store.get_nodes_by_file(deleted_path) == []
        assert self.store.get_node(survivor_qn) is not None
        assert self.store.get_edges_by_source(survivor_qn) == []
        embeddings = self.store._conn.execute(
            "SELECT qualified_name FROM embeddings ORDER BY qualified_name"
        ).fetchall()
        assert [row["qualified_name"] for row in embeddings] == [
            survivor_qn,
            "unrelated::orphan",
        ]

    def test_remove_file_permanently_handles_more_than_sqlite_variable_limit(self):
        deleted_path = "/test/large.py"
        rows = [
            (
                "Function",
                f"node_{index}",
                f"{deleted_path}::node_{index}",
                deleted_path,
                index + 1,
                index + 1,
                "python",
                0,
                0.0,
            )
            for index in range(16_384)
        ]
        self.store._conn.executemany(
            "INSERT INTO nodes "
            "(kind, name, qualified_name, file_path, line_start, line_end, language, "
            "is_test, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.store.commit()

        changed = self.store.remove_file_permanently(deleted_path)

        assert changed == 1
        assert self.store.get_nodes_by_file(deleted_path) == []

    def test_remove_files_permanently_rolls_back_every_table_on_failure(self):
        deleted_path = "/test/deleted.py"
        survivor_path = "/test/survivor.py"
        deleted_qn = f"{deleted_path}::removed"
        survivor_qn = f"{survivor_path}::caller"
        self.store.store_file_nodes_edges(
            deleted_path,
            [self._make_file_node(deleted_path), self._make_func_node("removed", deleted_path)],
            [
                EdgeInfo(
                    kind="CONTAINS",
                    source=deleted_path,
                    target=deleted_qn,
                    file_path=deleted_path,
                )
            ],
        )
        self.store.store_file_nodes_edges(
            survivor_path,
            [self._make_file_node(survivor_path), self._make_func_node("caller", survivor_path)],
            [
                EdgeInfo(
                    kind="CALLS",
                    source=survivor_qn,
                    target=deleted_qn,
                    file_path=survivor_path,
                )
            ],
        )
        self.store._conn.execute(
            "CREATE TABLE embeddings (qualified_name TEXT PRIMARY KEY, vector BLOB NOT NULL, "
            "text_hash TEXT NOT NULL, provider TEXT NOT NULL)"
        )
        self.store._conn.execute(
            "INSERT INTO embeddings VALUES (?, ?, ?, ?)",
            (deleted_qn, b"deleted", "deleted", "test"),
        )
        self.store.commit()
        before = {
            "nodes": self.store._conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
            "edges": self.store._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
            "embeddings": self.store._conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0],
        }

        self.store._conn.execute(
            "CREATE TRIGGER fail_deleted_node BEFORE DELETE ON nodes "
            f"WHEN OLD.file_path = '{deleted_path}' "
            "BEGIN SELECT RAISE(ABORT, 'injected deletion failure'); END"
        )
        self.store.commit()

        with pytest.raises(sqlite3.IntegrityError, match="injected deletion failure"):
            self.store.remove_files_permanently([deleted_path])

        after = {
            "nodes": self.store._conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
            "edges": self.store._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
            "embeddings": self.store._conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0],
        }
        assert after == before

    def test_remove_files_permanently_counts_changed_paths_and_commits_once(self):
        paths = ["/test/first.py", "/test/second.py", "/test/missing.py"]
        for path in paths[:2]:
            self.store.store_file_nodes_edges(path, [self._make_file_node(path)], [])

        commits = 0

        def count_commits() -> int:
            nonlocal commits
            commits += 1
            return 0

        self.store._conn.set_trace_callback(
            lambda statement: count_commits() if statement == "COMMIT" else None
        )
        changed = self.store.remove_files_permanently(paths)

        assert changed == 2
        assert commits == 1

    def test_replacement_preserves_incoming_edges_from_other_files(self):
        target_path = "/test/target.py"
        caller_path = "/test/caller.py"
        target_qn = f"{target_path}::target"
        caller_qn = f"{caller_path}::caller"
        self.store.store_file_nodes_edges(
            target_path,
            [
                self._make_file_node(target_path),
                self._make_func_node("target", target_path),
            ],
            [],
        )
        self.store.store_file_nodes_edges(
            caller_path,
            [
                self._make_file_node(caller_path),
                self._make_func_node("caller", caller_path),
            ],
            [
                EdgeInfo(
                    kind="CALLS",
                    source=caller_qn,
                    target=target_qn,
                    file_path=caller_path,
                ),
            ],
        )

        self.store.store_file_nodes_edges(
            target_path,
            [
                self._make_file_node(target_path),
                self._make_func_node("target", target_path),
            ],
            [],
        )

        incoming = self.store.get_edges_by_target(target_qn)
        assert [(edge.source_qualified, edge.file_path) for edge in incoming] == [
            (caller_qn, caller_path),
        ]

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

    def test_store_file_preserves_qualified_name_collisions(self):
        nodes = [
            NodeInfo(kind="Function", name="render", file_path="/test/file.py",
                     line_start=10, line_end=12, language="python"),
            NodeInfo(kind="Function", name="render", file_path="/test/file.py",
                     line_start=20, line_end=24, language="python"),
        ]

        self.store.store_file_nodes_edges("/test/file.py", nodes, [])

        stored = self.store.get_nodes_by_file("/test/file.py")
        assert sorted((node.line_start, node.line_end) for node in stored) == [
            (10, 12),
            (20, 24),
        ]

    def test_reindex_line_shift_preserves_collision_identities_and_callers(self):
        target_file = "/test/target.py"
        caller_file = "/test/caller.py"
        nodes = [
            NodeInfo(kind="Function", name="render", file_path=target_file,
                     line_start=10, line_end=12, language="python"),
            NodeInfo(kind="Function", name="render", file_path=target_file,
                     line_start=20, line_end=24, language="python"),
        ]
        self.store.store_file_nodes_edges(target_file, nodes, [])
        original_qns = {
            node.qualified_name
            for node in self.store.get_nodes_by_file(target_file)
        }
        caller_nodes = [
            NodeInfo(kind="Function", name="call_first", file_path=caller_file,
                     line_start=1, line_end=3, language="python"),
            NodeInfo(kind="Function", name="call_second", file_path=caller_file,
                     line_start=5, line_end=7, language="python"),
        ]
        caller_edges = [
            EdgeInfo(
                kind="CALLS", source=f"{caller_file}::{caller.name}", target=target,
                file_path=caller_file, line=caller.line_start + 1,
            )
            for caller, target in zip(caller_nodes, sorted(original_qns))
        ]
        self.store.store_file_nodes_edges(caller_file, caller_nodes, caller_edges)

        shifted_nodes = [
            NodeInfo(kind="Function", name="render", file_path=target_file,
                     line_start=11, line_end=13, language="python"),
            NodeInfo(kind="Function", name="render", file_path=target_file,
                     line_start=21, line_end=25, language="python"),
        ]
        self.store.store_file_nodes_edges(target_file, shifted_nodes, [])

        shifted_qns = {
            node.qualified_name
            for node in self.store.get_nodes_by_file(target_file)
        }
        assert shifted_qns == original_qns
        _assert_callers_survive(self.store, original_qns, f"{target_file}::render")

    def test_reindex_collision_shrink_preserves_survivor_identity_and_caller(self):
        target_file = "/test/target.py"
        caller_file = "/test/caller.py"
        nodes = [
            NodeInfo(kind="Function", name="render", file_path=target_file,
                     line_start=10, line_end=12, language="python"),
            NodeInfo(kind="Function", name="render", file_path=target_file,
                     line_start=20, line_end=24, language="python"),
        ]
        self.store.store_file_nodes_edges(target_file, nodes, [])
        survivor = next(
            node for node in self.store.get_nodes_by_file(target_file)
            if node.line_start == 20
        )
        caller = NodeInfo(
            kind="Function", name="call_render", file_path=caller_file,
            line_start=1, line_end=3, language="python",
        )
        self.store.store_file_nodes_edges(
            caller_file,
            [caller],
            [EdgeInfo(
                kind="CALLS", source=f"{caller_file}::call_render",
                target=survivor.qualified_name, file_path=caller_file, line=2,
            )],
        )

        self.store.store_file_nodes_edges(
            target_file,
            [NodeInfo(kind="Function", name="render", file_path=target_file,
                      line_start=21, line_end=25, language="python")],
            [],
        )

        stored = self.store.get_nodes_by_file(target_file)
        assert len(stored) == 1
        assert stored[0].qualified_name == survivor.qualified_name
        callers = self.store.get_edges_by_target(survivor.qualified_name)
        assert len(callers) == 1
        assert self.store.get_node(callers[0].target_qualified) == stored[0]

        later_caller_file = "/test/later_caller.py"
        later_caller = NodeInfo(
            kind="Function", name="call_render", file_path=later_caller_file,
            line_start=1, line_end=3, language="python",
        )
        self.store.store_file_nodes_edges(
            later_caller_file,
            [later_caller],
            [EdgeInfo(
                kind="CALLS", source=f"{later_caller_file}::call_render",
                target=f"{target_file}::render", file_path=later_caller_file, line=2,
            )],
        )
        callers = self.store.get_edges_by_target(survivor.qualified_name)
        assert len(callers) == 2

    def test_reindex_collision_growth_preserves_existing_identity_and_caller(self):
        target_file = "/test/target.py"
        caller_file = "/test/caller.py"
        original = NodeInfo(
            kind="Function", name="render", file_path=target_file,
            line_start=20, line_end=24, language="python",
        )
        self.store.store_file_nodes_edges(target_file, [original], [])
        original_qn = self.store.get_nodes_by_file(target_file)[0].qualified_name
        caller = NodeInfo(
            kind="Function", name="call_render", file_path=caller_file,
            line_start=1, line_end=3, language="python",
        )
        self.store.store_file_nodes_edges(
            caller_file,
            [caller],
            [EdgeInfo(
                kind="CALLS", source=f"{caller_file}::call_render",
                target=original_qn, file_path=caller_file, line=2,
            )],
        )

        self.store.store_file_nodes_edges(
            target_file,
            [
                NodeInfo(kind="Function", name="render", file_path=target_file,
                         line_start=10, line_end=12, language="python"),
                NodeInfo(kind="Function", name="render", file_path=target_file,
                         line_start=21, line_end=25, language="python"),
            ],
            [],
        )

        survivor = self.store.get_node(original_qn)
        assert survivor is not None
        assert (survivor.line_start, survivor.line_end) == (21, 25)
        callers = self.store.get_edges_by_target(original_qn)
        assert len(callers) == 1
        assert self.store.get_node(callers[0].target_qualified) == survivor

    def test_reindex_collision_reorder_preserves_identities_and_callers(self):
        target_file = "/test/target.py"
        caller_file = "/test/caller.py"
        nodes = [
            NodeInfo(kind="Function", name="render", file_path=target_file,
                     line_start=10, line_end=12, language="python", params="(first)"),
            NodeInfo(kind="Function", name="render", file_path=target_file,
                     line_start=20, line_end=24, language="python", params="(second)"),
        ]
        self.store.store_file_nodes_edges(target_file, nodes, [])
        original_qns = {
            node.params: node.qualified_name
            for node in self.store.get_nodes_by_file(target_file)
        }
        caller_nodes = [
            NodeInfo(kind="Function", name="call_first", file_path=caller_file,
                     line_start=1, line_end=3, language="python"),
            NodeInfo(kind="Function", name="call_second", file_path=caller_file,
                     line_start=5, line_end=7, language="python"),
        ]
        self.store.store_file_nodes_edges(
            caller_file,
            caller_nodes,
            [
                EdgeInfo(
                    kind="CALLS", source=f"{caller_file}::call_first",
                    target=original_qns["(first)"], file_path=caller_file, line=2,
                ),
                EdgeInfo(
                    kind="CALLS", source=f"{caller_file}::call_second",
                    target=original_qns["(second)"], file_path=caller_file, line=6,
                ),
            ],
        )

        self.store.store_file_nodes_edges(
            target_file,
            [
                NodeInfo(kind="Function", name="render", file_path=target_file,
                         line_start=10, line_end=14, language="python", params="(second)"),
                NodeInfo(kind="Function", name="render", file_path=target_file,
                         line_start=20, line_end=22, language="python", params="(first)"),
            ],
            [],
        )

        reordered = {
            node.params: node.qualified_name
            for node in self.store.get_nodes_by_file(target_file)
        }
        assert reordered == original_qns
        _assert_callers_survive(
            self.store, set(original_qns.values()), f"{target_file}::render",
        )

    def test_reindex_unique_signature_and_line_change_keeps_bare_identity(self):
        target_file = "/test/target.py"
        caller_file = "/test/caller.py"
        base_qualified = f"{target_file}::render"
        original = NodeInfo(
            kind="Function", name="render", file_path=target_file,
            line_start=10, line_end=12, language="python", params="(a)",
        )
        self.store.store_file_nodes_edges(target_file, [original], [])
        caller = NodeInfo(
            kind="Function", name="call_render", file_path=caller_file,
            line_start=1, line_end=3, language="python",
        )
        self.store.store_file_nodes_edges(
            caller_file,
            [caller],
            [EdgeInfo(
                kind="CALLS", source=f"{caller_file}::call_render",
                target=base_qualified, file_path=caller_file, line=2,
            )],
        )

        for line_start, params in [(12, "(a, b)"), (14, "(a, b, c)")]:
            self.store.store_file_nodes_edges(
                target_file,
                [NodeInfo(
                    kind="Function", name="render", file_path=target_file,
                    line_start=line_start, line_end=line_start + 2,
                    language="python", params=params,
                )],
                [],
            )
            stored = self.store.get_node(base_qualified)
            assert stored is not None
            assert stored.params == params
            assert len(self.store.get_nodes_by_file(target_file)) == 1
            assert len(self.store.get_edges_by_target(base_qualified)) == 1

    def test_upsert_same_colliding_definition_updates_in_place(self):
        nodes = [
            NodeInfo(kind="Function", name="render", file_path="/test/file.py",
                     line_start=10, line_end=12, language="python"),
            NodeInfo(kind="Function", name="render", file_path="/test/file.py",
                     line_start=20, line_end=24, language="python"),
        ]
        self.store.store_file_nodes_edges("/test/file.py", nodes, [])
        original = next(
            node for node in self.store.get_nodes_by_file("/test/file.py")
            if node.line_start == 20
        )

        node_id = self.store.upsert_node(nodes[1])

        assert len(self.store.get_nodes_by_file("/test/file.py")) == 2
        assert node_id == original.id

    def test_recursive_edge_resolves_for_disambiguated_node(self):
        base_qualified = "/test/file.py::render"
        nodes = [
            NodeInfo(kind="Function", name="render", file_path="/test/file.py",
                     line_start=10, line_end=12, language="python"),
            NodeInfo(kind="Function", name="render", file_path="/test/file.py",
                     line_start=20, line_end=24, language="python"),
        ]
        edges = [
            EdgeInfo(
                kind="CALLS", source=base_qualified, target=base_qualified,
                file_path="/test/file.py", line=22,
            )
        ]

        self.store.store_file_nodes_edges("/test/file.py", nodes, edges)

        disambiguated = next(
            node for node in self.store.get_nodes_by_file("/test/file.py")
            if node.line_start == 20
        )
        callers = self.store.get_edges_by_target(disambiguated.qualified_name)
        callees = self.store.get_edges_by_source(disambiguated.qualified_name)
        assert disambiguated.qualified_name.startswith(f"{base_qualified}:S")
        assert len(callers) == 1
        assert len(callees) == 1
        assert self.store.get_node(callers[0].source_qualified) == disambiguated
        assert self.store.get_node(callees[0].target_qualified) == disambiguated

    def test_unevidenced_cross_file_call_lists_every_duplicate(self):
        """A caller naming the base cannot say which duplicate it means, so it
        claims neither (#586): plain name, every candidate, AMBIGUOUS 0.4."""
        target_file = "/test/target.py"
        base_qualified = f"{target_file}::render"
        nodes = [
            NodeInfo(kind="Function", name="render", file_path=target_file,
                     line_start=10, line_end=12, language="python"),
            NodeInfo(kind="Function", name="render", file_path=target_file,
                     line_start=20, line_end=24, language="python"),
        ]
        self.store.store_file_nodes_edges(target_file, nodes, [])
        identities = {n.qualified_name for n in self.store.get_nodes_by_file(target_file)}
        caller = NodeInfo(
            kind="Function", name="call_render", file_path="/test/caller.py",
            line_start=1, line_end=3, language="python",
        )
        edge = EdgeInfo(
            kind="CALLS", source="/test/caller.py::call_render",
            target=base_qualified, file_path="/test/caller.py", line=2,
        )

        self.store.store_file_nodes_edges("/test/caller.py", [caller], [edge])

        for identity in identities:
            assert self.store.get_edges_by_target(identity) == []
        [stored] = self.store.get_edges_by_source("/test/caller.py::call_render")
        assert stored.target_qualified == "render"
        assert stored.extra["ambiguous_targets"][0] == base_qualified
        assert set(stored.extra["ambiguous_targets"]) == identities
        assert stored.confidence == 0.4
        assert stored.confidence_tier == "AMBIGUOUS"

    def test_unresolved_identity_lookup_uses_qualified_name_index_range(self):
        statements = []
        self.store._conn.set_trace_callback(statements.append)
        try:
            self.store._resolve_stored_identity("/test/file.py::missing")
        finally:
            self.store._conn.set_trace_callback(None)

        prefix_queries = [
            statement
            for statement in statements
            if statement.startswith("SELECT qualified_name FROM nodes")
        ]
        assert len(prefix_queries) == 1
        assert "qualified_name >=" in prefix_queries[0]
        assert "qualified_name <" in prefix_queries[0]
        assert "substr(" not in prefix_queries[0]

    def test_colon_symbol_is_not_mistaken_for_collision_suffix(self):
        target_file = "/test/target.m"
        caller_file = "/test/caller.m"
        bare_target = f"{target_file}::render"
        selector = NodeInfo(
            kind="Function", name="render:Save:", file_path=target_file,
            line_start=10, line_end=12, language="objc",
        )
        self.store.store_file_nodes_edges(target_file, [selector], [])
        caller = NodeInfo(
            kind="Function", name="callRender", file_path=caller_file,
            line_start=1, line_end=3, language="objc",
        )
        self.store.store_file_nodes_edges(
            caller_file,
            [caller],
            [EdgeInfo(
                kind="CALLS", source=f"{caller_file}::callRender",
                target=bare_target, file_path=caller_file, line=2,
            )],
        )

        assert len(self.store.get_edges_by_target(bare_target)) == 1
        assert self.store.get_edges_by_target(f"{target_file}::render:Save:") == []

    def test_store_file_nodes_edges_collapses_duplicate_call_sites(self):
        """Regression for #721: the batched store must collapse duplicate call
        sites (same kind/source/target/file/line) into a single edge exactly
        like the previous per-row upsert path, and replacing a file must drop
        stale rows instead of merging with them.
        """
        nodes = [self._make_file_node(), self._make_func_node()]
        dup_edge = EdgeInfo(
            kind="CALLS", source="/test/file.py::my_func",
            target="/test/file.py::other", file_path="/test/file.py", line=10,
        )
        self.store.store_file_nodes_edges(
            "/test/file.py", nodes, [dup_edge, dup_edge]
        )
        assert (
            self.store._conn.execute(
                "SELECT COUNT(*) FROM edges WHERE kind='CALLS'"
            ).fetchone()[0]
        ) == 1

        # Re-store the same file with no edges: stale rows must be gone.
        self.store.store_file_nodes_edges("/test/file.py", nodes, [])
        assert (
            self.store._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        ) == 0

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

    def test_store_with_open_transaction_no_error(self):
        """Regression test for #489: store_file_nodes_edges and
        store_file_batch must not raise 'cannot start a transaction
        within a transaction' when the caller has an explicit BEGIN open.
        """
        node_a = self._make_file_node("/test/a.py")
        node_b = self._make_file_node("/test/b.py")

        # Force an open transaction on the shared connection.
        self.store._conn.execute("BEGIN")
        assert self.store._conn.in_transaction

        # Must not raise sqlite3.OperationalError.
        self.store.store_file_nodes_edges("/test/a.py", [node_a], [])
        assert self.store.get_node("/test/a.py") is not None

        # Re-open the transaction and verify the batch path is guarded too.
        self.store._conn.execute("BEGIN")
        assert self.store._conn.in_transaction
        self.store.store_file_batch([("/test/b.py", [node_b], [], "")])
        assert self.store.get_node("/test/b.py") is not None

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

    def test_has_nodes(self):
        assert self.store.has_nodes() is False

        self.store.upsert_node(self._make_file_node())

        assert self.store.has_nodes() is True

    def test_impact_radius(self):
        # func_b depends on the changed func_a, so func_b is impacted.
        self.store.upsert_node(self._make_file_node("/a.py"))
        self.store.upsert_node(self._make_func_node("func_a", "/a.py"))
        self.store.upsert_node(self._make_file_node("/b.py"))
        self.store.upsert_node(self._make_func_node("func_b", "/b.py"))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="/b.py::func_b",
            target="/a.py::func_a", file_path="/b.py", line=10,
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

    def test_get_transitive_tests_follows_direct_tested_by_edge(self):
        """Regression test for #515: get_transitive_tests must follow
        TESTED_BY edges by source_qualified (production) since the parser
        stores source=production, target=test. The test function uses an
        unconventional name so the bare-name fallback cannot mask the bug.
        """
        self.store.upsert_node(self._make_file_node("/src/calc.py"))
        self.store.upsert_node(self._make_func_node("add", "/src/calc.py"))
        self.store.upsert_node(self._make_file_node("/tests/check.py"))
        self.store.upsert_node(self._make_func_node(
            "verify_addition", "/tests/check.py", is_test=True,
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="TESTED_BY",
            source="/src/calc.py::add",
            target="/tests/check.py::verify_addition",
            file_path="/tests/check.py", line=1,
        ))
        self.store.commit()

        results = self.store.get_transitive_tests("/src/calc.py::add")
        qns = {r["qualified_name"] for r in results}
        assert "/tests/check.py::verify_addition" in qns
        assert all(not r["indirect"] for r in results)

    def test_get_transitive_tests_follows_calls_then_tested_by(self):
        """Transitive coverage: caller -> CALLS -> callee -> TESTED_BY -> test.
        Uses an unconventional test name so the bare-name fallback cannot
        match. See: #515.
        """
        self.store.upsert_node(self._make_file_node("/src/svc.py"))
        self.store.upsert_node(self._make_func_node("orchestrate", "/src/svc.py"))
        self.store.upsert_node(self._make_func_node("compute", "/src/svc.py"))
        self.store.upsert_node(self._make_file_node("/tests/check.py"))
        self.store.upsert_node(self._make_func_node(
            "verify_compute", "/tests/check.py", is_test=True,
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="/src/svc.py::orchestrate",
            target="/src/svc.py::compute", file_path="/src/svc.py", line=2,
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="TESTED_BY",
            source="/src/svc.py::compute",
            target="/tests/check.py::verify_compute",
            file_path="/tests/check.py", line=1,
        ))
        self.store.commit()

        results = self.store.get_transitive_tests(
            "/src/svc.py::orchestrate", max_depth=2,
        )
        qns = {r["qualified_name"] for r in results}
        assert "/tests/check.py::verify_compute" in qns
        match = next(
            r for r in results
            if r["qualified_name"] == "/tests/check.py::verify_compute"
        )
        assert match["indirect"] is True

    def test_parse_store_get_transitive_tests_end_to_end(self):
        """End-to-end producer->store->consumer guard for #515.

        Parse a real fixture pair (production + test) through the parser,
        persist the emitted nodes/edges, and confirm get_transitive_tests
        surfaces the test as covering the production code. This couples the
        parser's canonical TESTED_BY direction (source=production,
        target=test) to the consumer query, so a future parser flip would
        break this test even if every hand-seeded fixture test still passed.
        """
        from code_review_graph.parser import CodeParser

        fixtures = Path(__file__).parent / "fixtures"
        parser = CodeParser()
        all_nodes: list[NodeInfo] = []
        all_edges: list[EdgeInfo] = []
        for fixture in ("sample_python.py", "test_sample.py"):
            nodes, edges = parser.parse_file(fixtures / fixture)
            all_nodes.extend(nodes)
            all_edges.extend(edges)

        for n in all_nodes:
            self.store.upsert_node(n)
        for e in all_edges:
            self.store.upsert_edge(e)
        self.store.commit()

        tested_by = [e for e in all_edges if e.kind == "TESTED_BY"]
        assert tested_by, "fixture pair should yield at least one TESTED_BY edge"

        # Producer direction guard: every TESTED_BY target must be a stored
        # Test node, and querying the consumer (get_transitive_tests) by the
        # edge's *source* (production) must surface that test target. If a
        # future parser flip swapped the direction, the target would point at
        # production code and this end-to-end assertion would fail.
        checked = 0
        for edge in tested_by:
            target = self.store.get_node(edge.target)
            assert target is not None, f"missing test node {edge.target}"
            assert target.is_test, (
                f"TESTED_BY target {edge.target!r} should be a test node; "
                f"a flipped parser would put production code here"
            )

            results = self.store.get_transitive_tests(edge.source)
            qns = {r["qualified_name"] for r in results}
            assert edge.target in qns, (
                f"get_transitive_tests({edge.source!r}) should surface test "
                f"{edge.target!r}; got {sorted(qns)}"
            )
            checked += 1
        assert checked >= 1

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
        self.tmp.close()  # release the handle before GraphStore reopens it on Windows
        self.store = GraphStore(self.tmp.name)
        self._build_chain()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _build_chain(self):
        """Build D -> C -> B -> A dependency chain for testing."""
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
            kind="CALLS", source="/b.py::func_b",
            target="/a.py::func_a", file_path="/b.py", line=10,
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="/c.py::func_c",
            target="/b.py::func_b", file_path="/c.py", line=10,
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="/d.py::func_d",
            target="/c.py::func_c", file_path="/d.py", line=10,
        ))
        self.store.commit()

    def test_sql_matches_networkx(self):
        """SQL and NetworkX BFS produce identical impacted node sets."""
        sql_result = self.store.get_impact_radius_sql(["/a.py"], max_depth=2)
        nx_result = self.store._get_impact_radius_networkx(["/a.py"], max_depth=2)

        sql_qns = {n.qualified_name for n in sql_result["impacted_nodes"]}
        nx_qns = {n.qualified_name for n in nx_result["impacted_nodes"]}
        assert sql_qns == {"/b.py::func_b", "/c.py::func_c"}
        assert sql_qns == nx_qns

    def test_max_nodes_truncation(self):
        """Setting max_nodes=2 should truncate results."""
        result = self.store.get_impact_radius_sql(
            ["/a.py"], max_depth=3, max_nodes=2,
        )
        assert result["truncated"] is True
        assert result["total_impacted"] == 3
        assert len(result["impacted_nodes"]) == 2

    def test_empty_changed_files(self):
        result = self.store.get_impact_radius_sql([], max_depth=2)
        assert result["changed_nodes"] == []
        assert result["impacted_nodes"] == []
        assert result["total_impacted"] == 0


def test_impact_radius_real_build_includes_importer_not_imported_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real parsed import graph follows impact toward dependents only."""
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    dependency = tmp_path / "dependency.py"
    changed = tmp_path / "changed.py"
    importer = tmp_path / "importer.py"
    dependency.write_text("VALUE = 1\n", encoding="utf-8")
    changed.write_text(
        "from dependency import VALUE\n\n"
        "def changed_value():\n"
        "    return VALUE\n",
        encoding="utf-8",
    )
    importer.write_text(
        "from changed import changed_value\n\n"
        "def consume():\n"
        "    return changed_value()\n",
        encoding="utf-8",
    )

    with GraphStore(tmp_path / "graph.db") as store:
        built = full_build(tmp_path, store)
        assert built["errors"] == []

        sql = store.get_impact_radius_sql([str(changed)], max_depth=1)
        networkx = store._get_impact_radius_networkx(
            [str(changed)],
            max_depth=1,
        )

    expected = {importer.as_posix()}
    assert set(sql["impacted_files"]) == expected
    assert set(networkx["impacted_files"]) == expected
    assert dependency.as_posix() not in sql["impacted_files"]
    assert sql["impact_scores"] == networkx["impact_scores"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0.75", 0.75),
        ("", 0.6),
        ("not-a-number", 0.6),
        ("nan", 0.6),
        ("inf", 0.6),
        ("-0.1", 0.6),
        ("0", 0.6),
        ("1", 0.6),
        ("1.2", 0.6),
    ],
)
def test_impact_float_configuration_is_finite_and_bounded(
    monkeypatch, raw, expected,
):
    monkeypatch.setenv("CRG_TEST_IMPACT_FLOAT", raw)
    assert constants_module._bounded_float_env(
        "CRG_TEST_IMPACT_FLOAT", 0.6, lower=0.0, upper=1.0,
    ) == pytest.approx(expected)


class TestWeightedImpactScoring:
    """Best-path scoring stays ranked, bounded, and engine-independent."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _add_func(self, name: str, path: str) -> str:
        self.store.upsert_node(NodeInfo(
            kind="Function", name=name, file_path=path,
            line_start=1, line_end=10, language="python",
        ))
        return f"{path}::{name}"

    def _add_edge(
        self, kind: str, source: str, target: str, line: int = 1,
    ) -> None:
        self.store.upsert_edge(EdgeInfo(
            kind=kind, source=source, target=target,
            file_path="/seed.py", line=line,
        ))

    @staticmethod
    def _ordered_qns(result) -> list[str]:
        return [node.qualified_name for node in result["impacted_nodes"]]

    @pytest.mark.parametrize(
        "kind",
        [
            "CALLS",
            "IMPORTS_FROM",
            "DEPENDS_ON",
            "REFERENCES",
            "INHERITS",
            "OVERRIDES",
            "IMPLEMENTS",
        ],
    )
    def test_dependency_edges_include_dependents_not_dependencies(self, kind):
        seed = self._add_func("seed", "/seed.py")
        dependent = self._add_func("dependent", "/dependent.py")
        dependency = self._add_func("dependency", "/dependency.py")
        self._add_edge(kind, dependent, seed)
        self._add_edge(kind, seed, dependency, line=2)
        self.store.commit()

        sql = self.store.get_impact_radius_sql(["/seed.py"], max_depth=1)
        nx_result = self.store._get_impact_radius_networkx(
            ["/seed.py"], max_depth=1,
        )

        assert self._ordered_qns(sql) == [dependent]
        assert self._ordered_qns(nx_result) == [dependent]
        assert sql["impact_scores"] == nx_result["impact_scores"]

    def test_tested_by_traverses_from_production_to_test_only(self):
        seed = self._add_func("seed", "/seed.py")
        test = self._add_func("test_seed", "/test_seed.py")
        unrelated_production = self._add_func(
            "unrelated_production", "/unrelated.py",
        )
        self._add_edge("TESTED_BY", seed, test)
        self._add_edge("TESTED_BY", unrelated_production, seed, line=2)
        self.store.commit()

        sql = self.store.get_impact_radius_sql(["/seed.py"], max_depth=1)
        nx_result = self.store._get_impact_radius_networkx(
            ["/seed.py"], max_depth=1,
        )

        assert self._ordered_qns(sql) == [test]
        assert self._ordered_qns(nx_result) == [test]
        assert sql["impact_scores"] == nx_result["impact_scores"]

    def test_contains_edge_cannot_bridge_impact(self):
        seed = self._add_func("seed", "/seed.py")
        stale_container = "stale.py::Container"
        dependent = self._add_func("dependent", "/dependent.py")
        self._add_edge("CONTAINS", stale_container, seed)
        self._add_edge("CALLS", dependent, stale_container, line=2)
        self.store.commit()

        sql = self.store.get_impact_radius_sql(["/seed.py"], max_depth=2)
        nx_result = self.store._get_impact_radius_networkx(
            ["/seed.py"], max_depth=2,
        )

        assert self._ordered_qns(sql) == []
        assert self._ordered_qns(nx_result) == []
        assert sql["impact_scores"] == nx_result["impact_scores"]

    def test_unknown_edge_kind_defaults_to_incoming_dependency_direction(self):
        seed = self._add_func("seed", "/seed.py")
        dependent = self._add_func("dependent", "/dependent.py")
        dependency = self._add_func("dependency", "/dependency.py")
        self._add_edge("UNKNOWN_KIND", dependent, seed)
        self._add_edge("UNKNOWN_KIND", seed, dependency, line=2)
        self.store.commit()

        sql = self.store.get_impact_radius_sql(["/seed.py"], max_depth=1)
        nx_result = self.store._get_impact_radius_networkx(
            ["/seed.py"], max_depth=1,
        )

        assert self._ordered_qns(sql) == [dependent]
        assert sql["impact_scores"][dependent] == pytest.approx(0.3)
        assert self._ordered_qns(nx_result) == [dependent]
        assert sql["impact_scores"] == nx_result["impact_scores"]

    def test_edge_weights_rank_best_path_and_engines_match(self):
        seed = self._add_func("seed", "/seed.py")
        caller = self._add_func("caller", "/caller.py")
        importer = self._add_func("importer", "/importer.py")
        indirect_caller = self._add_func(
            "indirect_caller", "/indirect_caller.py",
        )
        self._add_edge("CALLS", caller, seed)
        self._add_edge("IMPORTS_FROM", importer, seed)
        self._add_edge("CALLS", indirect_caller, caller)
        self.store.commit()

        sql = self.store.get_impact_radius_sql(["/seed.py"], max_depth=2)
        nx_result = self.store._get_impact_radius_networkx(
            ["/seed.py"], max_depth=2,
        )

        assert sql["impact_scores"][caller] == pytest.approx(0.6)
        assert sql["impact_scores"][indirect_caller] == pytest.approx(0.36)
        assert sql["impact_scores"][importer] == pytest.approx(0.3)
        assert self._ordered_qns(sql) == [
            caller, indirect_caller, importer,
        ]
        assert sql["impact_scores"] == nx_result["impact_scores"]
        assert self._ordered_qns(sql) == self._ordered_qns(nx_result)

    def test_deeper_strong_path_beats_shallow_weak_path(self):
        seed = self._add_func("seed", "/seed.py")
        middle = self._add_func("middle", "/middle.py")
        target = self._add_func("target", "/target.py")
        self._add_edge("IMPORTS_FROM", target, seed)
        self._add_edge("CALLS", middle, seed, line=2)
        self._add_edge("CALLS", target, middle, line=3)
        self.store.commit()

        sql = self.store.get_impact_radius_sql(["/seed.py"], max_depth=2)
        nx_result = self.store._get_impact_radius_networkx(
            ["/seed.py"], max_depth=2,
        )

        assert sql["impact_scores"][target] == pytest.approx(0.36)
        assert sql["impact_scores"] == nx_result["impact_scores"]

    def test_score_floor_stops_expansion_in_both_engines(self):
        qns = [
            self._add_func(f"node_{index}", f"/node_{index}.py")
            for index in range(8)
        ]
        for index, (source, target) in enumerate(zip(qns[1:], qns)):
            self._add_edge("CALLS", source, target, line=index + 1)
        self.store.commit()

        sql = self.store.get_impact_radius_sql(
            ["/node_0.py"], max_depth=8,
        )
        nx_result = self.store._get_impact_radius_networkx(
            ["/node_0.py"], max_depth=8,
        )

        assert qns[5] in sql["impact_scores"]
        assert qns[6] not in sql["impact_scores"]
        assert sql["impact_scores"] == nx_result["impact_scores"]

    def test_unknown_edge_kind_uses_default_weight(self):
        seed = self._add_func("seed", "/seed.py")
        target = self._add_func("target", "/target.py")
        self._add_edge("UNKNOWN_KIND", target, seed)
        self.store.commit()

        sql = self.store.get_impact_radius_sql(["/seed.py"], max_depth=1)
        nx_result = self.store._get_impact_radius_networkx(
            ["/seed.py"], max_depth=1,
        )

        assert sql["impact_scores"][target] == pytest.approx(0.3)
        assert sql["impact_scores"] == nx_result["impact_scores"]

    def test_truncation_is_exact_at_boundary_and_uses_sentinel(self):
        seed = self._add_func("seed", "/seed.py")
        targets = [
            self._add_func(f"target_{index}", f"/target_{index}.py")
            for index in range(3)
        ]
        for index, target in enumerate(targets):
            self._add_edge("CALLS", target, seed, line=index + 1)
        self.store.commit()

        exact = self.store.get_impact_radius_sql(
            ["/seed.py"], max_depth=1, max_nodes=3,
        )
        capped = self.store.get_impact_radius_sql(
            ["/seed.py"], max_depth=1, max_nodes=2,
        )

        assert exact["truncated"] is False
        assert exact["total_impacted"] == 3
        assert capped["truncated"] is True
        assert capped["total_impacted"] == 3
        assert len(capped["impacted_nodes"]) == 2

    def test_ghost_endpoint_bridges_without_consuming_limit(self):
        seed = self._add_func("seed", "/seed.py")
        target = self._add_func("target", "/target.py")
        ghost = "external.package::ghost"
        self._add_edge("CALLS", ghost, seed)
        self._add_edge("CALLS", target, ghost, line=2)
        self.store.commit()

        result = self.store.get_impact_radius_sql(
            ["/seed.py"], max_depth=2, max_nodes=1,
        )

        assert self._ordered_qns(result) == [target]
        assert ghost not in result["impact_scores"]
        assert result["truncated"] is False

    def test_parallel_edges_use_strongest_weight_in_both_engines(self):
        seed = self._add_func("seed", "/seed.py")
        target = self._add_func("target", "/target.py")
        self._add_edge("CALLS", target, seed, line=1)
        self._add_edge("IMPORTS_FROM", target, seed, line=2)
        self.store.commit()

        sql = self.store.get_impact_radius_sql(["/seed.py"], max_depth=1)
        nx_result = self.store._get_impact_radius_networkx(
            ["/seed.py"], max_depth=1,
        )
        assert sql["impact_scores"][target] == pytest.approx(0.6)
        assert sql["impact_scores"] == nx_result["impact_scores"]

    def test_parallel_edges_preserve_each_direction_in_both_engines(self):
        source = self._add_func("source", "/source.py")
        target = self._add_func("target", "/target.py")
        self._add_edge("CALLS", source, target, line=1)
        self._add_edge("TESTED_BY", source, target, line=2)
        self.store.commit()

        for path, expected_qn, expected_score in (
            ("/source.py", target, 0.42),
            ("/target.py", source, 0.6),
        ):
            sql = self.store.get_impact_radius_sql([path], max_depth=1)
            nx_result = self.store._get_impact_radius_networkx(
                [path], max_depth=1,
            )

            assert self._ordered_qns(sql) == [expected_qn]
            assert sql["impact_scores"][expected_qn] == pytest.approx(
                expected_score,
            )
            assert sql["impact_scores"] == nx_result["impact_scores"]

    def test_dense_mixed_cycle_is_bounded(self):
        qns = [self._add_func(f"node_{i}", f"/node_{i}.py") for i in range(12)]
        line = 1
        for source_index, source in enumerate(qns):
            for target_index, target in enumerate(qns):
                if source_index == target_index:
                    continue
                kind = "CALLS" if (source_index + target_index) % 2 else "IMPORTS_FROM"
                self._add_edge(kind, source, target, line=line)
                line += 1
        self.store.commit()

        started = time.monotonic()
        result = self.store.get_impact_radius_sql(
            ["/node_0.py"], max_depth=25, max_nodes=20,
        )
        elapsed = time.monotonic() - started

        assert len(result["impacted_nodes"]) == 11
        assert result["truncated"] is False
        assert elapsed < 5.0


class TestGetTransitiveTestsFrontierCap:
    """Regression tests for O(N*M) query explosion in get_transitive_tests."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()  # release the handle before GraphStore reopens it on Windows
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _add_func(self, name: str, path: str) -> str:
        node = NodeInfo(
            kind="Function", name=name, file_path=path,
            line_start=1, line_end=5, language="python",
        )
        self.store.upsert_node(node)
        return f"{path}::{name}"

    def _add_calls_edge(self, source_qn: str, target_qn: str) -> None:
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source=source_qn, target=target_qn,
            file_path=source_qn.split("::")[0], line=1,
        ))

    def test_frontier_capped_limits_sql_queries(self):
        """Hub function with 200 callees must not issue 200 TESTED_BY queries."""
        hub_qn = self._add_func("hub", "/t/hub.py")
        for i in range(200):
            callee_qn = self._add_func(f"callee_{i}", "/t/callee.py")
            self._add_calls_edge(hub_qn, callee_qn)
        self.store.commit()

        query_count = 0

        def _trace(stmt: str) -> None:
            nonlocal query_count
            query_count += 1

        self.store._conn.set_trace_callback(_trace)
        self.store.get_transitive_tests(hub_qn, max_frontier=50)
        self.store._conn.set_trace_callback(None)

        # Without cap: 200 callee TESTED_BY queries + overhead = ~204
        # With cap of 50: ~54 queries max
        assert query_count <= 60, (
            f"Expected <=60 queries with frontier cap, got {query_count}"
        )

    def test_uncapped_small_frontier_unchanged(self):
        """Small fan-out (< cap) returns same results regardless of cap."""
        hub_qn = self._add_func("hub", "/t/hub.py")
        test_qn = self._add_func("test_hub", "/t/test_hub.py")
        for i in range(5):
            callee_qn = self._add_func(f"callee_{i}", "/t/callee.py")
            self._add_calls_edge(hub_qn, callee_qn)
            # Only callee_2 has a test
            if i == 2:
                self.store.upsert_edge(EdgeInfo(
                    kind="TESTED_BY", source=callee_qn, target=test_qn,
                    file_path="/t/test_hub.py", line=1,
                ))
        self.store.commit()

        results_default = self.store.get_transitive_tests(hub_qn)
        results_capped = self.store.get_transitive_tests(hub_qn, max_frontier=50)

        indirect_default = [r for r in results_default if r["indirect"]]
        indirect_capped = [r for r in results_capped if r["indirect"]]
        assert len(indirect_default) == 1
        assert len(indirect_capped) == 1
        assert indirect_default[0]["name"] == indirect_capped[0]["name"]


class TestResolveBareEndpoints:
    """Only graph evidence may turn a bare call/test endpoint into a node."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _func(self, name: str, path: str, *, is_test: bool = False) -> str:
        self.store.upsert_node(NodeInfo(
            kind="Test" if is_test else "Function",
            name=name,
            file_path=path,
            line_start=1,
            line_end=5,
            language="python",
            is_test=is_test,
        ))
        return f"{path}::{name}"

    def _edge(
        self, kind: str, source: str, target: str, file_path: str,
    ) -> None:
        self.store.upsert_edge(EdgeInfo(
            kind=kind,
            source=source,
            target=target,
            file_path=file_path,
            line=1,
        ))

    def _endpoints(self, kind: str) -> list[tuple[str, str]]:
        rows = self.store._conn.execute(
            "SELECT source_qualified, target_qualified FROM edges "
            "WHERE kind = ? ORDER BY id",
            (kind,),
        ).fetchall()
        return [
            (row["source_qualified"], row["target_qualified"])
            for row in rows
        ]

    def test_unique_tested_by_source_without_evidence_stays_bare(self):
        """A globally unique name in an unrelated file is still not evidence."""
        self._func("parse", "/repo/src/app.py")
        test_qn = self._func(
            "test_parse", "/repo/tests/test_other.py", is_test=True,
        )
        self._edge("TESTED_BY", "parse", test_qn, "/repo/tests/test_other.py")
        self.store.commit()

        assert self.store.resolve_bare_tested_by_sources() == 0
        assert self._endpoints("TESTED_BY") == [("parse", test_qn)]

    def test_unique_tested_by_source_resolves_with_import_evidence(self):
        source_qn = self._func("parse", "/repo/src/app.py")
        test_file = "/repo/tests/test_app.py"
        test_qn = self._func("test_parse", test_file, is_test=True)
        self._edge("IMPORTS_FROM", test_file, "/repo/src/app.py", test_file)
        self._edge("TESTED_BY", "parse", test_qn, test_file)
        self.store.commit()

        assert self.store.resolve_bare_tested_by_sources() == 1
        assert self._endpoints("TESTED_BY") == [(source_qn, test_qn)]

    def test_ambiguous_tested_by_source_uses_one_imported_candidate(self):
        source_qn = self._func("parse", "/repo/src/app.py")
        self._func("parse", "/repo/vendor/app.py")
        test_file = "/repo/tests/test_app.py"
        test_qn = self._func("test_parse", test_file, is_test=True)
        self._edge("IMPORTS_FROM", test_file, "/repo/src/app.py", test_file)
        self._edge("TESTED_BY", "parse", test_qn, test_file)
        self.store.commit()

        assert self.store.resolve_bare_tested_by_sources() == 1
        assert self._endpoints("TESTED_BY") == [(source_qn, test_qn)]

    def test_same_file_call_target_is_strong_evidence(self):
        file_path = "/repo/src/app.py"
        caller_qn = self._func("caller", file_path)
        helper_qn = self._func("helper", file_path)
        self._edge("CALLS", caller_qn, "helper", file_path)
        self.store.commit()

        assert self.store.resolve_bare_call_targets() == 1
        assert self._endpoints("CALLS") == [(caller_qn, helper_qn)]

    def test_unique_unrelated_call_target_stays_bare(self):
        caller_file = "/repo/src/app.py"
        caller_qn = self._func("caller", caller_file)
        self._func("helper", "/repo/unrelated/util.py")
        self._edge("CALLS", caller_qn, "helper", caller_file)
        self.store.commit()

        assert self.store.resolve_bare_call_targets() == 0
        assert self._endpoints("CALLS") == [(caller_qn, "helper")]

    def test_tests_for_does_not_guess_unrelated_bare_source(self):
        source_qn = self._func("parse", "/repo/src/app.py")
        test_file = "/repo/tests/test_other.py"
        test_qn = self._func("test_parse", test_file, is_test=True)
        self._edge("TESTED_BY", "parse", test_qn, test_file)
        self.store.commit()

        assert self.store.get_transitive_tests(source_qn, max_depth=0) == []

    def test_tests_for_accepts_unique_import_backed_bare_source(self):
        source_qn = self._func("parse", "/repo/src/app.py")
        test_file = "/repo/tests/test_app.py"
        test_qn = self._func("test_parse", test_file, is_test=True)
        self._edge("IMPORTS_FROM", test_file, "/repo/src/app.py", test_file)
        self._edge("TESTED_BY", "parse", test_qn, test_file)
        self.store.commit()

        results = self.store.get_transitive_tests(source_qn, max_depth=0)
        assert [result["qualified_name"] for result in results] == [test_qn]

    def test_tests_for_rejects_bare_source_with_two_imported_candidates(self):
        first_qn = self._func("parse", "/repo/src/app.py")
        second_qn = self._func("parse", "/repo/vendor/app.py")
        test_file = "/repo/tests/test_app.py"
        test_qn = self._func("test_parse", test_file, is_test=True)
        self._edge("IMPORTS_FROM", test_file, "/repo/src/app.py", test_file)
        self._edge("IMPORTS_FROM", test_file, "/repo/vendor/app.py", test_file)
        self._edge("TESTED_BY", "parse", test_qn, test_file)
        self.store.commit()

        assert self.store.get_transitive_tests(first_qn, max_depth=0) == []
        assert self.store.get_transitive_tests(second_qn, max_depth=0) == []

    def test_transitive_tests_do_not_follow_unresolved_bare_callee(self):
        hub_qn = self._func("hub", "/repo/src/hub.py")
        self._func("parse", "/repo/unrelated/app.py")
        test_file = "/repo/tests/test_app.py"
        test_qn = self._func("test_parse", test_file, is_test=True)
        self._edge("CALLS", hub_qn, "parse", "/repo/src/hub.py")
        self._edge("TESTED_BY", "parse", test_qn, test_file)
        self.store.commit()

        assert self.store.get_transitive_tests(hub_qn, max_depth=1) == []


class TestPythonDottedModuleImportEvidence:
    """Python imports that stay dotted must still back bare endpoints (#903).

    ``_resolve_python_module_in_repo`` gives up when a module is not reachable
    by walking up from the importing file — the standard ``src`` layout, and
    any framework-mediated layout such as Odoo's ``odoo.addons.*``. The parser
    then stores the raw dotted module as the ``IMPORTS_FROM`` target. Import
    evidence is keyed by file path, so comparing a path against a dotted module
    can never match: the bare-name fallback is skipped and ``tests_for``
    silently reports covered code as uncovered.
    """

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _func(self, name: str, path: str, *, is_test: bool = False) -> str:
        self.store.upsert_node(NodeInfo(
            kind="Test" if is_test else "Function",
            name=name,
            file_path=path,
            line_start=1,
            line_end=5,
            language="python",
            is_test=is_test,
        ))
        return f"{path}::{name}"

    def _edge(self, kind: str, source: str, target: str, file_path: str) -> None:
        self.store.upsert_edge(EdgeInfo(
            kind=kind,
            source=source,
            target=target,
            file_path=file_path,
            line=1,
        ))

    def _endpoints(self, kind: str) -> list[tuple[str, str]]:
        rows = self.store._conn.execute(
            "SELECT source_qualified, target_qualified FROM edges "
            "WHERE kind = ? ORDER BY id",
            (kind,),
        ).fetchall()
        return [
            (row["source_qualified"], row["target_qualified"]) for row in rows
        ]

    def test_dotted_module_import_backs_bare_tested_by_source(self):
        """`from mypkg.core import ...` under a src layout is import evidence."""
        source_qn = self._func("compute_total", "/repo/src/mypkg/core.py")
        test_file = "/repo/tests/test_core.py"
        test_qn = self._func("test_totals", test_file, is_test=True)
        self._edge("IMPORTS_FROM", test_file, "mypkg.core", test_file)
        self._edge("TESTED_BY", "compute_total", test_qn, test_file)
        self.store.commit()

        assert self.store.resolve_bare_tested_by_sources() == 1
        assert self._endpoints("TESTED_BY") == [(source_qn, test_qn)]

    def test_tests_for_finds_test_behind_dotted_module_import(self):
        """The query path must see the coverage, not just the resolver."""
        source_qn = self._func("compute_total", "/repo/src/mypkg/core.py")
        test_file = "/repo/tests/test_core.py"
        test_qn = self._func("test_totals", test_file, is_test=True)
        self._edge("IMPORTS_FROM", test_file, "mypkg.core", test_file)
        self._edge("TESTED_BY", "compute_total", test_qn, test_file)
        self.store.commit()

        results = self.store.get_transitive_tests(source_qn, max_depth=0)
        assert [result["qualified_name"] for result in results] == [test_qn]

    def test_package_import_maps_to_package_init(self):
        """`import mypkg` resolves to `mypkg/__init__.py`, not `mypkg.__init__`."""
        source_qn = self._func("boot", "/repo/src/mypkg/__init__.py")
        test_file = "/repo/tests/test_boot.py"
        test_qn = self._func("test_boot", test_file, is_test=True)
        self._edge("IMPORTS_FROM", test_file, "mypkg", test_file)
        self._edge("TESTED_BY", "boot", test_qn, test_file)
        self.store.commit()

        assert self.store.resolve_bare_tested_by_sources() == 1
        assert self._endpoints("TESTED_BY") == [(source_qn, test_qn)]

    def test_ambiguous_dotted_module_stays_bare(self):
        """Two files answering to one dotted module is not evidence.

        Guards the failure mode the reporter measured: naive suffix matching
        fabricated 20 of 326 resolutions on a real codebase.
        """
        self._func("compute_total", "/repo/src/mypkg/core.py")
        self._func("compute_total", "/repo/vendor/mypkg/core.py")
        test_file = "/repo/tests/test_core.py"
        test_qn = self._func("test_totals", test_file, is_test=True)
        self._edge("IMPORTS_FROM", test_file, "mypkg.core", test_file)
        self._edge("TESTED_BY", "compute_total", test_qn, test_file)
        self.store.commit()

        assert self.store.resolve_bare_tested_by_sources() == 0
        assert self._endpoints("TESTED_BY") == [("compute_total", test_qn)]

    def test_unrelated_dotted_module_is_not_evidence(self):
        """A dotted module that matches no indexed file proves nothing."""
        self._func("compute_total", "/repo/src/mypkg/core.py")
        test_file = "/repo/tests/test_core.py"
        test_qn = self._func("test_totals", test_file, is_test=True)
        self._edge("IMPORTS_FROM", test_file, "thirdparty.helpers", test_file)
        self._edge("TESTED_BY", "compute_total", test_qn, test_file)
        self.store.commit()

        assert self.store.resolve_bare_tested_by_sources() == 0
        assert self._endpoints("TESTED_BY") == [("compute_total", test_qn)]

    def test_partial_segment_match_is_not_evidence(self):
        """`pkg.core` must not match `.../otherpkg/core.py` on a suffix.

        Matching must be on whole path segments; a substring match would
        resolve `mypkg.core` against any file whose directory merely ends in
        `mypkg`.
        """
        self._func("compute_total", "/repo/src/notmypkg/core.py")
        test_file = "/repo/tests/test_core.py"
        test_qn = self._func("test_totals", test_file, is_test=True)
        self._edge("IMPORTS_FROM", test_file, "mypkg.core", test_file)
        self._edge("TESTED_BY", "compute_total", test_qn, test_file)
        self.store.commit()

        assert self.store.resolve_bare_tested_by_sources() == 0
        assert self._endpoints("TESTED_BY") == [("compute_total", test_qn)]


class TestUnusableDatabaseRecovery:
    """A restored CI cache that cannot be opened must still be recoverable.

    Two shapes reach ``GraphStore.__init__``: a file SQLite cannot read at
    all, and a perfectly valid SQLite file whose tables are the wrong shape.
    ``_init_schema`` uses ``CREATE TABLE IF NOT EXISTS``, so the second is
    never repaired in place -- without a classification the command dies with
    a traceback and every run stays red until someone clears the cache by
    hand, which is the failure the recovery path exists to end.
    """

    def test_not_a_database_is_classified(self, tmp_path):
        db = tmp_path / "graph.db"
        db.write_bytes(b"this is not a sqlite database")
        with pytest.raises(CorruptGraphDatabaseError):
            GraphStore(db)

    def test_valid_sqlite_with_the_wrong_tables_is_classified(self, tmp_path):
        """The file is readable SQLite; only its ``nodes`` table is foreign."""
        db = tmp_path / "graph.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, junk TEXT)")
        conn.commit()
        conn.close()
        assert db.read_bytes()[:16] == b"SQLite format 3\x00", "fixture must be SQLite"
        with pytest.raises(CorruptGraphDatabaseError):
            GraphStore(db)

    def test_discarding_it_makes_the_next_open_succeed(self, tmp_path):
        """Teeth for the check above: recovery, not merely a nicer error."""
        db = tmp_path / "graph.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, junk TEXT)")
        conn.commit()
        conn.close()
        with pytest.raises(CorruptGraphDatabaseError):
            GraphStore(db)
        assert discard_corrupt_database(db) is True
        store = GraphStore(db)
        try:
            assert store.get_stats().total_nodes == 0
        finally:
            store.close()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root ignores the read-only bits this test relies on",
    )
    def test_a_readonly_database_is_not_classified(self, tmp_path):
        """Never discard a healthy graph over an environment problem.

        A read-only checkout raises ``sqlite3.OperationalError`` from the
        same ``__init__`` block as a mis-shaped schema. Classifying it as
        unusable would send ``build`` to ``discard_corrupt_database`` and
        destroy a graph that was never broken.

        It is still reported rather than raised raw: the permission failure
        is one the tool understands about itself, so it arrives as a
        ``GraphStoreError`` naming the directory to fix. What must never
        happen is the *corrupt* classification, which is what the assertion
        below pins.
        """
        db = tmp_path / "graph.db"
        GraphStore(db).close()
        before = db.read_bytes()
        db.chmod(0o444)
        tmp_path.chmod(0o555)
        try:
            with pytest.raises(GraphStoreError) as caught:
                GraphStore(db)
            assert not isinstance(caught.value, CorruptGraphDatabaseError), caught.value
            assert str(tmp_path) in str(caught.value)
        finally:
            tmp_path.chmod(0o755)
            db.chmod(0o644)
        assert db.read_bytes() == before, "the database was touched"

    def test_classifiers_reject_an_environment_error_message(self, tmp_path):
        """The narrow half of the classification, at the unit it lives in.

        A healthy graph under load, on a read-only checkout, on a failing
        disk, or migrated by two processes at once produces an
        ``OperationalError`` from the same block as a mis-shaped schema.
        None of them may reach ``discard_corrupt_database``.
        """
        from code_review_graph.graph import (
            _has_incompatible_schema,
            _is_unreadable_database,
        )

        db = tmp_path / "graph.db"
        GraphStore(db).close()
        messages = (
            "database is locked",
            "attempt to write a readonly database",
            "disk I/O error",
            # Both of these mean two processes migrated the same healthy
            # database at once: every migration checks before it alters, so
            # neither can be produced by a database that is actually wrong.
            "duplicate column name: signature",
            "table communities already exists",
        )
        for message in messages:
            exc = sqlite3.OperationalError(message)
            assert not _is_unreadable_database(db, exc), message
            assert not _has_incompatible_schema(db, exc), message

    def test_a_schema_message_about_an_unreadable_file_is_not_a_schema_problem(
        self, tmp_path
    ):
        """Both halves of the schema check are load-bearing.

        The message alone must not be enough: a file that cannot answer a
        read of ``sqlite_master`` is a corrupt file, and is reported as one
        by the other classifier rather than as a mis-shaped schema.
        """
        from code_review_graph.graph import _has_incompatible_schema

        db = tmp_path / "graph.db"
        db.write_bytes(b"this is not a sqlite database")
        assert not _has_incompatible_schema(
            db, sqlite3.OperationalError("no such column: file_path")
        )


class TestGraphDirectHelpersAndBatching:
    def test_graph_store_batch_memory_and_resolution_direct(self, tmp_path: Path):
        assert constants_module.normalize_file_path(r"a\b\c.py") == "a/b/c.py"
        constants_module.release_os_memory()

        assert graph_module.__getattr__("NodeInfo") is NodeInfo
        assert hasattr(graph_module.__getattr__("nx"), "DiGraph")
        with pytest.raises(AttributeError):
            graph_module.__getattr__("nonexistent_symbol")

        store = GraphStore(tmp_path / "direct.db")
        try:
            f_a = NodeInfo(
                kind="File", name="/test/a.py", file_path="/test/a.py",
                line_start=1, line_end=20, language="python",
            )
            fn_a = NodeInfo(
                kind="Function", name="fn_a", file_path="/test/a.py",
                line_start=2, line_end=10, language="python",
            )
            f_b = NodeInfo(
                kind="File", name="/test/b.py", file_path="/test/b.py",
                line_start=1, line_end=20, language="python",
            )
            fn_b = NodeInfo(
                kind="Function", name="fn_b", file_path="/test/b.py",
                line_start=2, line_end=10, language="python",
            )
            f_t = NodeInfo(
                kind="File", name="/test/test_b.py", file_path="/test/test_b.py",
                line_start=1, line_end=20, language="python",
            )
            test_b = NodeInfo(
                kind="Test", name="test_fn_b", file_path="/test/test_b.py",
                line_start=2, line_end=10, language="python", is_test=True,
            )
            edges_b = [
                EdgeInfo(
                    kind="IMPORTS_FROM",
                    source="/test/b.py",
                    target="/test/a.py",
                    file_path="/test/b.py",
                    line=1,
                ),
                EdgeInfo(
                    kind="CALLS",
                    source="/test/b.py::fn_b",
                    target="fn_a",
                    file_path="/test/b.py",
                    line=5,
                ),
            ]
            edges_t = [
                EdgeInfo(
                    kind="TESTED_BY",
                    source="/test/b.py::fn_b",
                    target="/test/test_b.py::test_fn_b",
                    file_path="/test/test_b.py",
                    line=4,
                ),
            ]
            store.store_file_batch([
                ("/test/a.py", [f_a, fn_a], [], "hash_a"),
                ("/test/b.py", [f_b, fn_b], edges_b, "hash_b"),
                ("/test/test_b.py", [f_t, test_b], edges_t, "hash_t"),
            ])

            hashes = store.get_file_hashes()
            assert hashes["/test/a.py"] == "hash_a"
            assert hashes["/test/b.py"] == "hash_b"

            ctx = store._get_bare_endpoint_context(store._conn)
            assert "fn_a" in ctx[0]

            resolved_count = store._resolve_bare_endpoints("CALLS", "target_qualified")
            assert resolved_count == 1

            refreshed = store.refresh_target_resolution()
            assert refreshed >= 1

            impact = store.get_impact_radius_sql(["/test/a.py"], max_depth=2)
            assert any(n.name == "fn_b" for n in impact["impacted_nodes"])

            routes = store.get_caller_test_routes(["/test/a.py::fn_a"])
            assert "/test/a.py::fn_a" in routes

            unresolved = store.count_unresolved_call_sites({"fn_a"})
            assert unresolved == 0

            nxg = store._build_networkx_graph()
            assert nxg.number_of_nodes() >= 3

            bridges = find_bridge_nodes(store, top_n=5)
            assert isinstance(bridges, list)

            node_row = store._conn.execute("SELECT * FROM nodes LIMIT 1").fetchone()
            edge_row = store._conn.execute("SELECT * FROM edges LIMIT 1").fetchone()
            assert store._row_to_node(node_row).name
            assert store._row_to_edge(edge_row).kind

            store._invalidate_cache()
            store.shrink_memory(clear_nx_cache=True)
            assert store._nxg_cache is None
        finally:
            store.close()


class TestQualifiedNameCollisionLifecycle:
    """Review fixes for same-file qualified-name collisions (#585, PR #715)."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    @staticmethod
    def _render(path, line_start, params=None, line_end=None, is_test=False):
        return NodeInfo(
            kind="Test" if is_test else "Function", name="render", file_path=path,
            line_start=line_start,
            line_end=line_end if line_end is not None else line_start + 2,
            language="python", params=params, is_test=is_test,
        )

    def _call(self, caller_file, name, target, line=2):
        self.store.store_file_nodes_edges(
            caller_file,
            [NodeInfo(kind="Function", name=name, file_path=caller_file,
                      line_start=line - 1, line_end=line + 1, language="python")],
            [EdgeInfo(kind="CALLS", source=f"{caller_file}::{name}", target=target,
                      file_path=caller_file, line=line)],
        )

    def _identity_at(self, path, line_start):
        return next(
            node.qualified_name for node in self.store.get_nodes_by_file(path)
            if node.line_start == line_start
        )

    def test_backslash_spelling_keeps_the_shrink_survivor_identity(self):
        """Windows callers pass ``C:\\t\\target.py``; rows are stored as
        ``C:/t/target.py``. Planning must look old rows up by the stored spelling
        or the survivor re-keys to the bare name and its caller dangles.
        """
        posix = "C:/t/target.py"
        windows = "C:\\t\\target.py"
        self.store.store_file_nodes_edges(
            windows, [self._render(posix, 10, "(a)"), self._render(posix, 20, "(b)")], [],
        )
        survivor = self._identity_at(posix, 20)
        assert survivor != f"{posix}::render"
        self._call("C:/t/caller.py", "go", survivor)

        self.store.store_file_nodes_edges(windows, [self._render(posix, 21, "(b)")], [])

        assert [n.qualified_name for n in self.store.get_nodes_by_file(posix)] == [survivor]
        callers = self.store.get_edges_by_target(survivor)
        assert [e.source_qualified for e in callers] == ["C:/t/caller.py::go"]

    def test_replacement_plan_reads_each_previous_row_once(self, monkeypatch):
        """Planning bucketed old rows per new base, rescanning every old row for
        every name: 6000 unique definitions took seconds. One pass suffices.
        """
        path = "/test/many.py"
        nodes = [
            NodeInfo(kind="Function", name=f"f{i}", file_path=path,
                     line_start=i * 3 + 1, line_end=i * 3 + 2, language="python")
            for i in range(300)
        ]
        self.store.store_file_nodes_edges(path, nodes, [])
        checks = []
        for helper in ("_is_collision_identity", "_collision_base"):
            real = getattr(GraphStore, helper, None)
            if real is None:
                continue
            monkeypatch.setattr(GraphStore, helper, staticmethod(
                lambda *args, _real=real: checks.append(args) or _real(*args)))

        shifted = [
            NodeInfo(kind="Function", name=n.name, file_path=path,
                     line_start=n.line_start + 1, line_end=n.line_end + 1,
                     language="python")
            for n in nodes
        ]
        self.store.store_file_nodes_edges(path, shifted, [])

        assert len(checks) <= 2 * (len(nodes) + len(shifted))
        assert {n.qualified_name for n in self.store.get_nodes_by_file(path)} == {
            f"{path}::f{i}" for i in range(300)
        }

    @pytest.mark.parametrize("shift", [1, 6, 30])
    def test_identical_duplicates_keep_their_order_across_a_large_shift(self, shift):
        """Nearest-line pairing swapped identical twins at 10 and 20 once six
        lines were inserted above both (16 is nearer 20 than 10). Each one must
        keep its own identity, so each caller still names the same definition.
        """
        path = "/test/target.py"
        self.store.store_file_nodes_edges(
            path, [self._render(path, 10), self._render(path, 20)], [],
        )
        first, second = self._identity_at(path, 10), self._identity_at(path, 20)
        assert first != second

        self.store.store_file_nodes_edges(
            path, [self._render(path, 10 + shift), self._render(path, 20 + shift)], [],
        )

        assert self._identity_at(path, 10 + shift) == first
        assert self._identity_at(path, 20 + shift) == second

    @pytest.mark.parametrize(("new_lines", "added"), [
        ([16, 26, 40], 40),   # shifted by six, a third twin appended
        ([10, 15, 20], 15),   # a third twin inserted between, nothing shifted
        ([3, 16, 26], 3),     # a third twin inserted above both, which shift
    ])
    def test_identical_duplicates_keep_order_when_one_is_added(self, new_lines, added):
        path = "/test/target.py"
        self.store.store_file_nodes_edges(
            path, [self._render(path, 10), self._render(path, 20)], [],
        )
        first, second = self._identity_at(path, 10), self._identity_at(path, 20)

        self.store.store_file_nodes_edges(
            path, [self._render(path, line) for line in new_lines], [],
        )

        kept = [line for line in new_lines if line != added]
        assert [self._identity_at(path, line) for line in kept] == [first, second]
        assert self._identity_at(path, added) not in (first, second)

    def test_identical_duplicates_keep_order_when_one_is_removed(self):
        path = "/test/target.py"
        self.store.store_file_nodes_edges(
            path, [self._render(path, line) for line in (10, 20, 30)], [],
        )
        identities = [self._identity_at(path, line) for line in (10, 20, 30)]

        # The middle twin is deleted (three lines) and the rest shift by -3.
        self.store.store_file_nodes_edges(
            path, [self._render(path, 10), self._render(path, 27)], [],
        )

        assert [self._identity_at(path, line) for line in (10, 27)] == [
            identities[0], identities[2],
        ]

    def test_edited_duplicate_keeps_its_identity_beside_an_unchanged_one(self):
        """``render(a)`` at 10 becomes ``render(a, b)`` at 12 while ``render(c)``
        shifts 20 -> 22. The edited definition matches nothing semantically, but
        it is the only old and only new one left, so it keeps its identity
        rather than taking a suffix and stranding its caller on the bare name.
        """
        path = "/test/target.py"
        self.store.store_file_nodes_edges(
            path, [self._render(path, 10, "(a)"), self._render(path, 20, "(c)")], [],
        )
        edited, unchanged = self._identity_at(path, 10), self._identity_at(path, 20)
        assert edited == f"{path}::render"
        self._call("/test/caller_a.py", "go_a", edited)
        self._call("/test/caller_c.py", "go_c", unchanged)

        self.store.store_file_nodes_edges(
            path, [self._render(path, 12, "(a, b)"), self._render(path, 22, "(c)")], [],
        )

        assert self._identity_at(path, 12) == edited
        assert self._identity_at(path, 22) == unchanged
        _assert_callers_survive(self.store, {edited, unchanged}, f"{path}::render")

    def test_every_edited_duplicate_keeps_its_identity_in_order(self):
        path = "/test/target.py"
        self.store.store_file_nodes_edges(
            path, [self._render(path, 10, "(a)"), self._render(path, 20, "(c)")], [],
        )
        before = [self._identity_at(path, 10), self._identity_at(path, 20)]

        self.store.store_file_nodes_edges(
            path, [self._render(path, 14, "(a, x)"), self._render(path, 24, "(c, y)")], [],
        )

        assert [self._identity_at(path, 14), self._identity_at(path, 24)] == before

    def test_upsert_of_a_moved_definition_updates_it_in_place(self):
        """Staging updates a re-upserted definition in place; a moved line must
        not turn the same definition into a second, suffixed row.
        """
        path = "/test/file.py"
        first_id = self.store.upsert_node(self._render(path, 10, "(a)"))
        moved_id = self.store.upsert_node(self._render(path, 11, "(a)"))

        [node] = self.store.get_nodes_by_file(path)
        assert moved_id == first_id
        assert (node.qualified_name, node.line_start) == (f"{path}::render", 11)

    def test_upsert_of_a_moved_duplicate_updates_its_own_row(self):
        path = "/test/file.py"
        self.store.store_file_nodes_edges(
            path, [self._render(path, 10, "(a)"), self._render(path, 20, "(b)")], [],
        )
        second = self._identity_at(path, 20)

        self.store.upsert_node(self._render(path, 25, "(b)"))

        assert sorted(
            (n.qualified_name, n.line_start) for n in self.store.get_nodes_by_file(path)
        ) == sorted([(f"{path}::render", 10), (second, 25)])

    def test_upsert_of_a_different_definition_keeps_both(self):
        """#586's review: direct upserts must not collapse distinct definitions."""
        path = "/test/file.py"
        self.store.upsert_node(self._render(path, 10, "(a)"))
        self.store.upsert_node(self._render(path, 20, "(b)"))

        assert sorted(
            (n.params, n.line_start) for n in self.store.get_nodes_by_file(path)
        ) == [("(a)", 10), ("(b)", 20)]

    def test_tested_by_follows_the_duplicate_test_that_made_the_call(self):
        """The parser mints TESTED_BY as the reverse of a test's CALLS edge on
        the same line. Its target (the test) must be the same duplicate the
        CALLS source was aligned to, or coverage names the wrong test.
        """
        tests = "/test/test_app.py"
        helper = "/test/app.py::helper"
        nodes = [self._render(tests, 10, is_test=True),
                 self._render(tests, 20, is_test=True, line_end=24)]
        base = f"{tests}::render"
        edges = []
        for line in (11, 22):
            edges.append(EdgeInfo(kind="CALLS", source=base, target=helper,
                                  file_path=tests, line=line))
            edges.append(EdgeInfo(kind="TESTED_BY", source=helper, target=base,
                                  file_path=tests, line=line))

        self.store.store_file_nodes_edges(tests, nodes, edges)

        first, second = self._identity_at(tests, 10), self._identity_at(tests, 20)
        calls = {(e.line, e.source_qualified) for e in self.store.get_edges_by_target(helper)
                 if e.kind == "CALLS"}
        tested_by = {(e.line, e.target_qualified) for e in self.store.get_edges_by_source(helper)
                     if e.kind == "TESTED_BY"}
        assert calls == {(11, first), (22, second)}
        assert tested_by == calls

    def test_edges_only_restore_keeps_calls_on_their_duplicate(self):
        """Incremental updates re-store an unchanged file's edges alone
        (``store_file_edges``). Those edges come from a fresh parse, so they
        name the base identity and need the same alignment as a full store.
        """
        path = "/test/target.py"
        base = f"{path}::render"
        nodes = [self._render(path, 10, "(a)"), self._render(path, 20, "(b)")]
        edges = [EdgeInfo(kind="CALLS", source=base, target="/test/lib.py::log",
                          file_path=path, line=line) for line in (11, 21)]
        self.store.store_file_nodes_edges(path, nodes, edges)
        first, second = self._identity_at(path, 10), self._identity_at(path, 20)
        stored = {(e.line, e.source_qualified) for e in self.store.get_edges_by_target(
            "/test/lib.py::log")}
        assert stored == {(11, first), (21, second)}

        self.store.store_file_edges(path, edges)

        assert {(e.line, e.source_qualified) for e in self.store.get_edges_by_target(
            "/test/lib.py::log")} == stored

    def test_edges_only_restore_keeps_a_lone_survivors_suffix(self):
        path = "/test/target.py"
        base = f"{path}::render"
        self.store.store_file_nodes_edges(
            path, [self._render(path, 10, "(a)"), self._render(path, 20, "(b)")], [],
        )
        survivor = self._identity_at(path, 20)
        edge = EdgeInfo(kind="CALLS", source=base, target="/test/lib.py::log",
                        file_path=path, line=22)
        self.store.store_file_nodes_edges(path, [self._render(path, 21, "(b)")], [edge])
        assert [e.source_qualified for e in self.store.get_edges_by_target(
            "/test/lib.py::log")] == [survivor]

        self.store.store_file_edges(path, [edge])

        assert [e.source_qualified for e in self.store.get_edges_by_target(
            "/test/lib.py::log")] == [survivor]

    def test_bulk_store_looks_up_each_other_file_endpoint_once(self):
        """The bulk path (#721) must not seek the index for every endpoint:
        bare names and the file's own symbols need no lookup, and a repeated
        cross-file target is looked up once.
        """
        path = "/test/app.py"
        nodes = [
            NodeInfo(kind="Function", name=f"f{i}", file_path=path,
                     line_start=i * 3 + 1, line_end=i * 3 + 2, language="python")
            for i in range(50)
        ]
        edges = []
        for i, node in enumerate(nodes):
            line = node.line_start
            edges.append(EdgeInfo(kind="CALLS", source=f"{path}::f{i}",
                                  target=f"{path}::f{(i + 1) % 50}", file_path=path, line=line))
            edges.append(EdgeInfo(kind="CALLS", source=f"{path}::f{i}", target="print",
                                  file_path=path, line=line))
            edges.append(EdgeInfo(kind="CALLS", source=f"{path}::f{i}",
                                  target="/test/lib.py::log", file_path=path, line=line))
        statements = []
        self.store._conn.set_trace_callback(statements.append)
        try:
            self.store.store_file_nodes_edges(path, nodes, edges)
        finally:
            self.store._conn.set_trace_callback(None)

        seeks = [s for s in statements if "qualified_name >=" in s]
        assert len(seeks) == 1
        assert "/test/lib.py::log" in seeks[0]

    def test_symbol_tail_lookup_sees_every_duplicate(self):
        """``query_graph`` resolves ``Class.method`` targets through the indexed
        ``symbol`` column. A suffixed duplicate must share its base symbol, or a
        tail lookup reports one confident match and hides the other definition.
        """
        path = "/test/handlers.py"
        nodes = [
            NodeInfo(kind="Function", name="process", file_path=path, parent_name="Handler",
                     line_start=line, line_end=line + 2, language="python")
            for line in (10, 20)
        ]
        self.store.store_file_nodes_edges(path, nodes, [])

        found = self.store.search_nodes_by_qualified_tail("Handler.process")
        assert sorted(n.line_start for n in found) == [10, 20]
        assert self.store.count_nodes_by_qualified_tail("Handler.process") == 2

    def test_a_called_later_overload_is_not_dead_code(self, tmp_path):
        """Staging collapses ``area(int)`` and ``area(int, int)`` into one called
        node. Keeping both rows must not leave the second one without callers:
        ``rect()`` calls it, and nothing at either call picks an overload, so
        both calls list both and neither overload is claimed (#586).
        """
        from code_review_graph.refactor import find_dead_code

        (tmp_path / "Shapes.java").write_text(
            "public class Shapes {\n"
            "    public int area(int side) { return side * side; }\n"
            "    public int area(int w, int h) { return w * h; }\n"
            "    public int perimeter(int side) { return area(side) + 4 * side; }\n"
            "    public int rect() { return area(2, 3); }\n"
            "}\n",
            encoding="utf-8",
        )
        with GraphStore(tmp_path / "graph.db") as store:
            full_build(tmp_path, store)
            overloads = [n for n in store.get_nodes_by_file(
                (tmp_path / "Shapes.java").as_posix()) if n.name == "area"]
            assert len(overloads) == 2
            dead = {d["qualified_name"] for d in find_dead_code(store)}
        assert not {n.qualified_name for n in overloads} & dead


class TestUnevidencedCallsToDuplicates:
    """A call that cannot choose between same-file duplicates claims neither
    (#585, as #586 was refused for); one with evidence keeps its binding."""

    SHAPES = (
        "public class Shapes {\n"
        "    public int area(int side) { return side * side; }\n"
        "    public int area(int w, int h) { return w * h; }\n"
        "    public int rect() { return area(2, 3); }\n"
        "}\n"
    )

    def test_same_file_call_lists_both_overloads_and_claims_neither(self, tmp_path):
        from code_review_graph.tools.query import query_graph

        (tmp_path / ".git").mkdir()
        (tmp_path / "Shapes.java").write_text(self.SHAPES, encoding="utf-8")
        with GraphStore(tmp_path / ".code-review-graph" / "graph.db") as store:
            full_build(tmp_path, store)
            overloads = sorted(
                n.qualified_name for n in store.get_nodes_by_file(
                    (tmp_path / "Shapes.java").as_posix(),
                ) if n.name == "area"
            )
            [call] = [
                e for e in store.get_all_edges()
                if e.kind == "CALLS" and e.source_qualified.endswith("Shapes.rect")
            ]
            assert call.target_qualified == "area"
            assert sorted(call.extra["ambiguous_targets"]) == overloads
            assert (call.confidence, call.confidence_tier) == (0.4, "AMBIGUOUS")
            assert call.target_resolution == "unresolved"
        for overload in overloads:
            result = query_graph("callers_of", overload, str(tmp_path))
            assert result["status"] == "ok"
            assert result["results"] == []
        callees = query_graph(
            "callees_of", f"{(tmp_path / 'Shapes.java').as_posix()}::Shapes.rect",
            str(tmp_path),
        )
        [row] = callees["results"]
        assert row["resolution"] == "ambiguous"
        assert len(row["candidates"]) == 2

    def test_a_test_calling_a_duplicate_is_not_claimed_by_either(self, tmp_path):
        target = "/repo/app.py"
        test_file = "/repo/test_app.py"
        with GraphStore(tmp_path / "graph.db") as store:
            store.store_file_nodes_edges(target, [
                NodeInfo(kind="Function", name="render", file_path=target,
                         line_start=1, line_end=2, language="python"),
                NodeInfo(kind="Function", name="render", file_path=target,
                         line_start=4, line_end=5, language="python", params="(x)"),
            ], [])
            identities = {n.qualified_name for n in store.get_nodes_by_file(target)}
            store.store_file_nodes_edges(test_file, [
                NodeInfo(kind="Test", name="test_render", file_path=test_file,
                         line_start=1, line_end=2, language="python", is_test=True),
            ], [
                EdgeInfo(kind="CALLS", source=f"{test_file}::test_render",
                         target=f"{target}::render", file_path=test_file, line=2),
                EdgeInfo(kind="TESTED_BY", source=f"{target}::render",
                         target=f"{test_file}::test_render", file_path=test_file, line=2),
            ])
            for identity in identities:
                assert store.get_edges_by_source(identity) == []
                assert store.get_edges_by_target(identity) == []
            [mirror] = [e for e in store.get_all_edges() if e.kind == "TESTED_BY"]
            assert mirror.source_qualified == "render"
            assert set(mirror.extra["ambiguous_targets"]) == identities

    def test_settle_pass_fixes_store_order_and_restores_a_lone_survivor(self, tmp_path):
        """A caller stored before the duplicates binds to the bare name; the
        settle pass makes it ambiguous. When one definition is left again, the
        edge binds to it, though the caller's file was never re-parsed."""
        target = "/repo/app.py"
        caller = "/repo/main.py"

        def render(line, params):
            return NodeInfo(kind="Function", name="render", file_path=target,
                            line_start=line, line_end=line + 1, language="python",
                            params=params)

        with GraphStore(tmp_path / "graph.db") as store:
            store.store_file_nodes_edges(target, [render(1, "()")], [])
            store.store_file_nodes_edges(caller, [
                NodeInfo(kind="Function", name="main", file_path=caller,
                         line_start=1, line_end=2, language="python"),
            ], [EdgeInfo(kind="CALLS", source=f"{caller}::main",
                         target=f"{target}::render", file_path=caller, line=2)])
            store.refresh_target_resolution()
            assert len(store.get_edges_by_target(f"{target}::render")) == 1

            store.store_file_nodes_edges(target, [render(1, "()"), render(4, "(x)")], [])
            store.refresh_target_resolution()
            assert store.get_edges_by_target(f"{target}::render") == []
            [call] = store.get_edges_by_source(f"{caller}::main")
            assert call.target_qualified == "render"
            assert len(call.extra["ambiguous_targets"]) == 2

            store.store_file_nodes_edges(target, [render(4, "(x)")], [])
            store.refresh_target_resolution()
            [survivor] = store.get_nodes_by_file(target)
            [call] = store.get_edges_by_source(f"{caller}::main")
            assert call.target_qualified == survivor.qualified_name
            assert "ambiguous_targets" not in call.extra
            assert "collision_endpoint" not in call.extra
            assert (call.confidence, call.confidence_tier) == (1.0, "EXTRACTED")
            # Settled once; another pass changes nothing.
            assert store._settle_collision_endpoints() == 0

    def test_recursion_inside_the_first_duplicate_keeps_its_binding(self, tmp_path):
        path = "/repo/app.py"
        base = f"{path}::render"
        with GraphStore(tmp_path / "graph.db") as store:
            store.store_file_nodes_edges(path, [
                NodeInfo(kind="Function", name="render", file_path=path,
                         line_start=1, line_end=3, language="python"),
                NodeInfo(kind="Function", name="render", file_path=path,
                         line_start=5, line_end=7, language="python", params="(x)"),
            ], [EdgeInfo(kind="CALLS", source=base, target=base, file_path=path, line=2)])
            store.refresh_target_resolution()
            [loop] = store.get_edges_by_target(base)
            assert loop.source_qualified == base
            assert "ambiguous_targets" not in loop.extra
