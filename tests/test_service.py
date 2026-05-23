"""Tests for the shared graph service contract."""

import tempfile
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.service import GraphService


class TestGraphService:
    def setup_method(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.db_path = self.root / ".code-review-graph" / "graph.db"
        self.store = GraphStore(self.db_path)
        self._seed_data()
        self.store.close()

    def teardown_method(self):
        self.tmpdir.cleanup()

    def _seed_data(self):
        (self.root / "auth.py").write_text(
            "def login():\n    return 'ok'\n" + "# auth context\n" * 200,
            encoding="utf-8",
        )
        (self.root / "main.py").write_text(
            "from auth import login\n\ndef run():\n    return login()\n"
            + "# main context\n" * 200,
            encoding="utf-8",
        )
        self.store.upsert_node(NodeInfo(
            kind="File", name=str(self.root / "auth.py"),
            file_path=str(self.root / "auth.py"),
            line_start=1, line_end=30, language="python",
        ))
        self.store.upsert_node(NodeInfo(
            kind="Function", name="login",
            file_path=str(self.root / "auth.py"),
            line_start=5, line_end=10, language="python",
        ))
        self.store.upsert_node(NodeInfo(
            kind="File", name=str(self.root / "main.py"),
            file_path=str(self.root / "main.py"),
            line_start=1, line_end=20, language="python",
        ))
        self.store.upsert_node(NodeInfo(
            kind="Function", name="run",
            file_path=str(self.root / "main.py"),
            line_start=3, line_end=8, language="python",
        ))
        self.store.upsert_node(NodeInfo(
            kind="Function", name="login_view",
            file_path=str(self.root / "main.py"),
            line_start=10, line_end=12, language="python",
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS",
            source=str(self.root / "main.py") + "::run",
            target=str(self.root / "auth.py") + "::login",
            file_path=str(self.root / "main.py"),
            line=5,
        ))
        self.store.commit()

    def test_status_and_search(self):
        with GraphService(self.root) as service:
            status = service.status()
            assert status["status"] == "ok"
            assert status["stats"]["total_nodes"] == 5
            assert status["token_estimate"]["status"] == "estimated"
            assert status["token_estimate"]["source_tokens"] > 0
            assert status["token_estimate"]["graph_tokens"] > 0
            assert status["telemetry"]["status"] == "estimated"
            assert status["telemetry"]["total_requests"] == 0

            results = service.search("login")
            assert results["nodes"][0]["name"] == "login"

    def test_records_local_telemetry(self):
        with GraphService(self.root) as service:
            payload = service.search("login")
            event = service.record_telemetry("search", payload)

            assert event["status"] == "ok"
            summary = service.telemetry_summary()
            assert summary["total_requests"] == 1
            assert summary["estimated_saved_tokens"] >= 0
            assert summary["by_operation"][0]["operation"] == "search"

    def test_callers_and_impact(self):
        with GraphService(self.root) as service:
            callers = service.callers(str(self.root / "auth.py") + "::login")
            assert callers["status"] == "ok"
            assert callers["nodes"][0]["name"] == "run"

            impact = service.impact(["auth.py"])
            names = {n["name"] for n in impact["impacted_nodes"]}
            assert "run" in names

    def test_ambiguous_query_reports_candidates(self):
        with GraphService(self.root) as service:
            result = service.callers("login")
            assert result["status"] == "ambiguous"
            assert len(result["candidates"]) == 2

    def test_node_at_prefers_smallest_span(self):
        with GraphService(self.root) as service:
            node = service.node_at("auth.py", 6)
            assert node is not None
            assert node.name == "login"
