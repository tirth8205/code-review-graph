"""Edge-case tests for the orphan-row purge in reconciliation (PR #861 / issue #812).

Covers boundaries beyond the PR's own regression test: virtual-node exclusion
during reconciliation itself, edge-only orphans in both reconciliation modes,
paths outside the repository root, unicode paths, same-path rows in both
tables counting once, NULL ``extra`` rows, ordering/dedup guarantees of
``get_all_files``, and purge behavior at scale.
"""

import time
from unittest.mock import patch

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import _reconcile_stale_files, full_build
from code_review_graph.parser import EdgeInfo, NodeInfo


def _add_orphan_function(store, path, name="orphan_fn"):
    store.upsert_node(
        NodeInfo(
            kind="Function",
            name=name,
            file_path=path,
            line_start=1,
            line_end=2,
            language="javascript",
        )
    )


def _add_orphan_edge(store, path):
    store.upsert_edge(
        EdgeInfo(
            kind="CALLS",
            source=f"{path}::caller",
            target=f"{path}::callee",
            file_path=path,
        )
    )


def _add_virtual_event_node(store, name="OrderCreated"):
    store.upsert_node(
        NodeInfo(
            kind="Event",
            name=name,
            file_path="event",
            line_start=0,
            line_end=0,
            language="java",
            extra={"event_type": name, "virtual": True},
        )
    )


def _count_edges(store, path):
    return store._conn.execute(
        "SELECT COUNT(*) FROM edges WHERE file_path = ?", (path,)
    ).fetchone()[0]


class TestReconcileVirtualExclusion:
    def test_virtual_event_node_survives_reconcile_while_orphans_purge(self, tmp_path):
        real = tmp_path / "real.py"
        real.write_text("def live():\n    pass\n")
        orphan_node_path = str(tmp_path / "gone" / "a.js")
        orphan_edge_path = str(tmp_path / "gone" / "b.js")
        store = GraphStore(tmp_path / "test.db")
        try:
            _add_virtual_event_node(store)
            _add_orphan_function(store, orphan_node_path)
            _add_orphan_edge(store, orphan_edge_path)
            store.commit()

            stale = _reconcile_stale_files(tmp_path, store, ["real.py"])

            assert sorted(stale) == sorted([orphan_node_path, orphan_edge_path])
            events = store._conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE kind = 'Event' AND file_path = 'event'"
            ).fetchone()[0]
            assert events == 1
            assert store.get_nodes_by_file(orphan_node_path) == []
            assert _count_edges(store, orphan_edge_path) == 0
        finally:
            store.close()

    def test_virtual_event_node_survives_probe_mode_reconcile(self, tmp_path):
        # current_files=None branch: the relative sentinel path "event" raises
        # ValueError in relative_to() and would be purged if not excluded.
        real = tmp_path / "real.py"
        real.write_text("def live():\n    pass\n")
        orphan = str(tmp_path / "missing.js")
        store = GraphStore(tmp_path / "test.db")
        try:
            store.store_file_nodes_edges(
                str(real),
                [
                    NodeInfo(
                        kind="File",
                        name="real.py",
                        file_path=str(real),
                        line_start=1,
                        line_end=2,
                        language="python",
                    )
                ],
                [],
                "hash",
            )
            _add_virtual_event_node(store)
            _add_orphan_function(store, orphan)
            store.commit()

            stale = _reconcile_stale_files(tmp_path, store)

            assert stale == [orphan]
            events = store._conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE kind = 'Event'"
            ).fetchone()[0]
            assert events == 1
            assert store.get_nodes_by_file(str(real)) != []
        finally:
            store.close()


class TestOrphanPurgeBoundaries:
    def test_same_path_in_nodes_and_edges_counts_once(self, tmp_path):
        current = tmp_path / "sample.py"
        current.write_text("def hello():\n    pass\n")
        orphan = str(tmp_path / "generated" / "both.js")
        store = GraphStore(tmp_path / "test.db")
        try:
            _add_orphan_function(store, orphan)
            _add_orphan_edge(store, orphan)
            store.commit()

            with patch(
                "code_review_graph.incremental.get_all_tracked_files",
                return_value=["sample.py"],
            ):
                result = full_build(tmp_path, store)

            assert result["stale_files_removed"] == 1
            assert store.get_nodes_by_file(orphan) == []
            assert _count_edges(store, orphan) == 0
        finally:
            store.close()

    def test_orphan_outside_repo_root_is_purged_in_both_modes(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "sample.py").write_text("def hello():\n    pass\n")
        outside = str(tmp_path / "elsewhere" / "x.js")

        store = GraphStore(tmp_path / "full.db")
        try:
            _add_orphan_edge(store, outside)
            store.commit()
            with patch(
                "code_review_graph.incremental.get_all_tracked_files",
                return_value=["sample.py"],
            ):
                result = full_build(repo, store)
            assert result["stale_files_removed"] == 1
            assert _count_edges(store, outside) == 0
        finally:
            store.close()

        store = GraphStore(tmp_path / "probe.db")
        try:
            _add_orphan_function(store, outside)
            store.commit()
            stale = _reconcile_stale_files(repo, store)
            assert stale == [outside]
            assert store.get_nodes_by_file(outside) == []
        finally:
            store.close()

    def test_unicode_orphan_path_is_purged(self, tmp_path):
        (tmp_path / "sample.py").write_text("def hello():\n    pass\n")
        orphan = str(tmp_path / "généré" / "文件.js")
        store = GraphStore(tmp_path / "test.db")
        try:
            _add_orphan_function(store, orphan, name="加载")
            store.commit()
            with patch(
                "code_review_graph.incremental.get_all_tracked_files",
                return_value=["sample.py"],
            ):
                result = full_build(tmp_path, store)
            assert result["stale_files_removed"] == 1
            assert store.get_nodes_by_file(orphan) == []
        finally:
            store.close()

    def test_orphan_purge_leaves_unrelated_live_rows_intact(self, tmp_path):
        live = tmp_path / "live.py"
        live.write_text("def keep():\n    pass\n")
        orphan = str(tmp_path / "generated" / "dead.js")
        store = GraphStore(tmp_path / "test.db")
        try:
            _add_orphan_function(store, orphan)
            _add_orphan_edge(store, orphan)
            store.commit()

            with patch(
                "code_review_graph.incremental.get_all_tracked_files",
                return_value=["live.py"],
            ):
                full_build(tmp_path, store)

            live_nodes = store.get_nodes_by_file(str(live))
            assert any(n.kind == "File" for n in live_nodes)
            assert any(n.name == "keep" for n in live_nodes)
            assert store.get_nodes_by_file(orphan) == []
        finally:
            store.close()

    def test_many_orphan_edge_paths_purge_completely(self, tmp_path):
        (tmp_path / "sample.py").write_text("def hello():\n    pass\n")
        store = GraphStore(tmp_path / "test.db")
        try:
            now = time.time()
            rows = []
            for i in range(300):
                path = str(tmp_path / "gen" / f"chunk{i}.js")
                for j in range(5):
                    rows.append(
                        (
                            "CALLS",
                            f"{path}::caller{j}",
                            f"{path}::callee{j}",
                            path,
                            j,
                            "{}",
                            now,
                        )
                    )
            store._conn.executemany(
                "INSERT INTO edges (kind, source_qualified, target_qualified,"
                " file_path, line, extra, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            store.commit()

            with patch(
                "code_review_graph.incremental.get_all_tracked_files",
                return_value=["sample.py"],
            ):
                result = full_build(tmp_path, store)

            assert result["stale_files_removed"] == 300
            remaining = store._conn.execute(
                "SELECT COUNT(*) FROM edges WHERE file_path LIKE ?",
                (str(tmp_path / "gen") + "/%",),
            ).fetchone()[0]
            assert remaining == 0
        finally:
            store.close()


class TestGetAllFilesContract:
    def test_dedupes_across_tables_and_sorts(self, tmp_path):
        a = str(tmp_path / "a.py")
        b = str(tmp_path / "b.py")
        c = str(tmp_path / "c.py")
        store = GraphStore(tmp_path / "test.db")
        try:
            _add_orphan_function(store, b, name="fb")
            _add_orphan_edge(store, b)
            _add_orphan_edge(store, c)
            _add_orphan_function(store, a, name="fa")
            store.commit()
            assert store.get_all_files() == sorted([a, b, c])
        finally:
            store.close()

    def test_null_extra_row_is_included_without_error(self, tmp_path):
        path = str(tmp_path / "legacy.py")
        store = GraphStore(tmp_path / "test.db")
        try:
            store._conn.execute(
                "INSERT INTO nodes (kind, name, qualified_name, file_path,"
                " line_start, line_end, extra, updated_at)"
                " VALUES ('Function', 'old', ?, ?, 1, 2, NULL, ?)",
                (f"{path}::old", path, time.time()),
            )
            store.commit()
            assert store.get_all_files() == [path]
        finally:
            store.close()

    def test_empty_graph_returns_empty_list(self, tmp_path):
        store = GraphStore(tmp_path / "test.db")
        try:
            assert store.get_all_files() == []
        finally:
            store.close()
