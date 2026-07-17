"""Tests for the shared post-processing pipeline."""

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.postprocessing import run_post_processing


def _get_signature(store, qualified_name):
    row = store._conn.execute(
        "SELECT signature FROM nodes WHERE qualified_name = ?",
        (qualified_name,),
    ).fetchone()
    return row["signature"] if row else None


class TestRunPostProcessing:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()  # release the handle before GraphStore reopens it on Windows
        self.store = GraphStore(self.tmp.name)
        self._seed_data()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _seed_data(self):
        self.store.upsert_node(
            NodeInfo(
                kind="File",
                name="/repo/app.py",
                file_path="/repo/app.py",
                line_start=1,
                line_end=50,
                language="python",
            )
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Class",
                name="Service",
                file_path="/repo/app.py",
                line_start=5,
                line_end=40,
                language="python",
            )
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="handle",
                file_path="/repo/app.py",
                line_start=10,
                line_end=20,
                language="python",
                parent_name="Service",
                params="request",
                return_type="Response",
            )
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="process",
                file_path="/repo/app.py",
                line_start=25,
                line_end=35,
                language="python",
            )
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Test",
                name="test_handle",
                file_path="/repo/test_app.py",
                line_start=1,
                line_end=10,
                language="python",
                is_test=True,
            )
        )

        self.store.upsert_edge(
            EdgeInfo(
                kind="CONTAINS",
                source="/repo/app.py",
                target="/repo/app.py::Service",
                file_path="/repo/app.py",
            )
        )
        self.store.upsert_edge(
            EdgeInfo(
                kind="CONTAINS",
                source="/repo/app.py::Service",
                target="/repo/app.py::Service.handle",
                file_path="/repo/app.py",
            )
        )
        self.store.upsert_edge(
            EdgeInfo(
                kind="CALLS",
                source="/repo/app.py::Service.handle",
                target="/repo/app.py::process",
                file_path="/repo/app.py",
                line=15,
            )
        )
        self.store.commit()

    def test_computes_signatures(self):
        unsigned = self.store.get_nodes_without_signature()
        assert len(unsigned) > 0

        result = run_post_processing(self.store)

        assert result["signatures_computed"] > 0
        remaining = self.store.get_nodes_without_signature()
        assert len(remaining) == 0

    def test_function_signature_format(self):
        run_post_processing(self.store)

        sig = _get_signature(self.store, "/repo/app.py::Service.handle")
        assert sig == "def handle(request) -> Response"

    def test_class_signature_format(self):
        run_post_processing(self.store)

        sig = _get_signature(self.store, "/repo/app.py::Service")
        assert sig == "class Service"

    def test_test_signature_format(self):
        run_post_processing(self.store)

        sig = _get_signature(self.store, "/repo/test_app.py::test_handle")
        assert sig is not None
        assert sig.startswith("def test_handle(")

    def test_rebuilds_fts_index(self):
        result = run_post_processing(self.store)

        assert "fts_indexed" in result
        assert result["fts_indexed"] > 0

    def test_fts_search_works_after_post_processing(self):
        run_post_processing(self.store)

        from code_review_graph.search import hybrid_search

        hits = hybrid_search(self.store, "handle")
        names = {h["name"] for h in hits}
        assert "handle" in names

    def test_detects_flows(self):
        result = run_post_processing(self.store)

        assert "flows_detected" in result
        assert result["flows_detected"] >= 0

    def test_detects_communities(self):
        result = run_post_processing(self.store)

        assert "communities_detected" in result
        assert result["communities_detected"] >= 0

    def test_no_warnings_on_healthy_store(self):
        result = run_post_processing(self.store)

        assert "warnings" not in result

    def test_empty_store_no_crash(self):
        empty_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        empty_tmp.close()  # release the handle before GraphStore reopens it on Windows
        empty_store = GraphStore(empty_tmp.name)
        try:
            result = run_post_processing(empty_store)
            assert result["signatures_computed"] == 0
            assert result["fts_indexed"] == 0
        finally:
            empty_store.close()
            Path(empty_tmp.name).unlink(missing_ok=True)

    def test_idempotent(self):
        first = run_post_processing(self.store)
        second = run_post_processing(self.store)

        assert second["fts_indexed"] == first["fts_indexed"]
        assert second["signatures_computed"] == 0

    def test_signature_truncated_at_512(self):
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="f",
                file_path="/repo/big.py",
                line_start=1,
                line_end=2,
                language="python",
                params="a" * 600,
            )
        )
        self.store.commit()

        run_post_processing(self.store)
        sig = _get_signature(self.store, "/repo/big.py::f")
        assert sig is not None
        assert len(sig) <= 512


class TestPostProcessingStepIsolation:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()  # release the handle before GraphStore reopens it on Windows
        self.store = GraphStore(self.tmp.name)
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="fn",
                file_path="/repo/a.py",
                line_start=1,
                line_end=5,
                language="python",
            )
        )
        self.store.commit()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_fts_failure_does_not_block_flows(self):
        with patch(
            "code_review_graph.search.rebuild_fts_index",
            side_effect=ImportError("fts boom"),
        ):
            result = run_post_processing(self.store)

        assert "flows_detected" in result
        assert "communities_detected" in result
        assert "warnings" in result
        assert any("FTS" in w for w in result["warnings"])

    def test_flow_failure_does_not_block_communities(self):
        with patch(
            "code_review_graph.flows.trace_flows",
            side_effect=ImportError("flow boom"),
        ):
            result = run_post_processing(self.store)

        assert "communities_detected" in result
        assert "warnings" in result
        assert any("Flow" in w for w in result["warnings"])

    def test_community_failure_still_has_signatures(self):
        with patch(
            "code_review_graph.communities.detect_communities",
            side_effect=ImportError("comm boom"),
        ):
            result = run_post_processing(self.store)

        assert result["signatures_computed"] > 0
        assert "warnings" in result
        assert any("Community" in w for w in result["warnings"])


class TestToolBuildUsesSharedPipeline:
    def test_build_tool_runs_post_processing(self, tmp_path):
        py_file = tmp_path / "sample.py"
        py_file.write_text("def hello():\n    pass\n")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".code-review-graph").mkdir()

        db_path = tmp_path / ".code-review-graph" / "graph.db"
        store = GraphStore(db_path)
        try:
            mock_target = "code_review_graph.incremental.get_all_tracked_files"
            with patch(mock_target, return_value=["sample.py"]):
                full_build(tmp_path, store)

            unsigned_before_pp = store.get_nodes_without_signature()
            run_post_processing(store)
            unsigned_after_pp = store.get_nodes_without_signature()

            assert len(unsigned_before_pp) > 0
            assert len(unsigned_after_pp) == 0
        finally:
            store.close()


class TestWatchCallbackIntegration:
    def test_watch_accepts_callback_parameter(self):
        import inspect

        from code_review_graph.incremental import watch

        sig = inspect.signature(watch)
        assert "on_files_updated" in sig.parameters

    def test_watch_callback_not_called_without_updates(self, tmp_path):
        import threading

        from code_review_graph.incremental import watch

        (tmp_path / ".git").mkdir()
        db_path = tmp_path / "test.db"
        store = GraphStore(db_path)
        callback = MagicMock()

        try:

            def run_watch():
                try:
                    watch(tmp_path, store, on_files_updated=callback)
                except KeyboardInterrupt:
                    pass

            t = threading.Thread(target=run_watch, daemon=True)
            t.start()

            import time

            time.sleep(0.5)
            callback.assert_not_called()
        finally:
            store.close()


class TestResolveBareEndpointsStep:
    """The shared/watch pipeline resolves evidence-backed bare endpoints."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = GraphStore(self.tmp.name)
        self._seed_bare_edges()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _seed_bare_edges(self):
        app_file = "/repo/src/app.py"
        util_file = "/repo/src/util.py"
        test_file = "/repo/tests/test_app.py"
        for name, path, is_test in [
            ("parse", app_file, False),
            ("helper", util_file, False),
            ("test_parse", test_file, True),
        ]:
            self.store.upsert_node(NodeInfo(
                kind="Test" if is_test else "Function",
                name=name,
                file_path=path,
                line_start=1,
                line_end=5,
                language="python",
                is_test=is_test,
            ))
        for imported in (app_file, util_file):
            self.store.upsert_edge(EdgeInfo(
                kind="IMPORTS_FROM",
                source=test_file,
                target=imported,
                file_path=test_file,
                line=1,
            ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS",
            source=f"{test_file}::test_parse",
            target="helper",
            file_path=test_file,
            line=2,
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="TESTED_BY",
            source="parse",
            target=f"{test_file}::test_parse",
            file_path=test_file,
            line=3,
        ))
        self.store.commit()

    def test_resolves_before_derived_steps_and_reports_count(self):
        result = run_post_processing(self.store)

        assert result["bare_edges_resolved"] == 2
        rows = self.store._conn.execute(
            "SELECT kind, source_qualified, target_qualified FROM edges "
            "WHERE kind IN ('CALLS', 'TESTED_BY') ORDER BY kind"
        ).fetchall()
        by_kind = {
            row["kind"]: (
                row["source_qualified"], row["target_qualified"],
            )
            for row in rows
        }
        assert by_kind["CALLS"] == (
            "/repo/tests/test_app.py::test_parse",
            "/repo/src/util.py::helper",
        )
        assert by_kind["TESTED_BY"] == (
            "/repo/src/app.py::parse",
            "/repo/tests/test_app.py::test_parse",
        )

    def test_resolution_failure_is_a_warning_not_a_pipeline_failure(self):
        with patch.object(
            GraphStore,
            "resolve_bare_call_targets",
            side_effect=sqlite3.OperationalError("boom"),
        ):
            result = run_post_processing(self.store)

        assert "bare_edges_resolved" not in result
        assert any("Bare-endpoint resolution" in w for w in result["warnings"])
        assert "communities_detected" in result
