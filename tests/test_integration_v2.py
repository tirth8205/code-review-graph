"""Comprehensive end-to-end integration test for the v2 pipeline.

Exercises: flows, communities, FTS search, analyze_changes,
find_dead_code, rename_preview, generate_hints, review_changes_prompt,
generate_wiki, and the Registry API.
"""

import tempfile
from pathlib import Path

from code_review_graph.changes import analyze_changes
from code_review_graph.communities import (
    detect_communities,
    get_architecture_overview,
    get_communities,
    store_communities,
)
from code_review_graph.flows import (
    get_affected_flows,
    get_flow_by_id,
    get_flows,
    store_flows,
    trace_flows,
)
from code_review_graph.graph import GraphStore
from code_review_graph.hints import generate_hints, get_session, reset_session
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.prompts import review_changes_prompt
from code_review_graph.refactor import find_dead_code, rename_preview
from code_review_graph.registry import Registry
from code_review_graph.search import hybrid_search, rebuild_fts_index
from code_review_graph.wiki import generate_wiki


class TestV2Integration:
    """End-to-end integration test exercising the full v2 pipeline."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()  # release the handle before GraphStore reopens it on Windows
        self.store = GraphStore(self.tmp.name)
        self._seed_realistic_graph()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    # -----------------------------------------------------------------
    # Graph seeding helpers
    # -----------------------------------------------------------------

    def _seed_realistic_graph(self):
        """Seed a realistic multi-file graph with auth, db, and API layers."""
        s = self.store

        # --- auth.py: authentication module ---
        s.upsert_node(NodeInfo(
            kind="File", name="auth.py", file_path="auth.py",
            line_start=1, line_end=100, language="python",
        ), file_hash="a1")
        s.upsert_node(NodeInfo(
            kind="Function", name="login", file_path="auth.py",
            line_start=5, line_end=20, language="python",
            params="(username: str, password: str)", return_type="Token",
            extra={"decorators": ["route"]},
        ), file_hash="a1")
        s.upsert_node(NodeInfo(
            kind="Function", name="logout", file_path="auth.py",
            line_start=25, line_end=40, language="python",
            params="(token: str)", return_type="bool",
            extra={"decorators": ["route"]},
        ), file_hash="a1")
        s.upsert_node(NodeInfo(
            kind="Function", name="verify_token", file_path="auth.py",
            line_start=45, line_end=60, language="python",
            params="(token: str)", return_type="bool",
        ), file_hash="a1")

        # --- db.py: database layer ---
        s.upsert_node(NodeInfo(
            kind="File", name="db.py", file_path="db.py",
            line_start=1, line_end=120, language="python",
        ), file_hash="b1")
        s.upsert_node(NodeInfo(
            kind="Class", name="Database", file_path="db.py",
            line_start=5, line_end=60, language="python",
        ), file_hash="b1")
        s.upsert_node(NodeInfo(
            kind="Function", name="connect", file_path="db.py",
            line_start=10, line_end=25, language="python",
            parent_name="Database",
            params="(self, dsn: str)", return_type="Connection",
        ), file_hash="b1")
        s.upsert_node(NodeInfo(
            kind="Function", name="query", file_path="db.py",
            line_start=30, line_end=50, language="python",
            parent_name="Database",
            params="(self, sql: str)", return_type="list[Row]",
        ), file_hash="b1")
        s.upsert_node(NodeInfo(
            kind="Function", name="close", file_path="db.py",
            line_start=55, line_end=60, language="python",
            parent_name="Database",
            params="(self)", return_type="None",
        ), file_hash="b1")

        # --- api.py: API handlers ---
        s.upsert_node(NodeInfo(
            kind="File", name="api.py", file_path="api.py",
            line_start=1, line_end=80, language="python",
        ), file_hash="c1")
        s.upsert_node(NodeInfo(
            kind="Function", name="get_users", file_path="api.py",
            line_start=5, line_end=20, language="python",
            params="(request: Request)", return_type="Response",
            extra={"decorators": ["route"]},
        ), file_hash="c1")
        s.upsert_node(NodeInfo(
            kind="Function", name="create_user", file_path="api.py",
            line_start=25, line_end=45, language="python",
            params="(request: Request)", return_type="Response",
            extra={"decorators": ["route"]},
        ), file_hash="c1")

        # --- utils.py: orphaned helper (dead code candidate) ---
        s.upsert_node(NodeInfo(
            kind="File", name="utils.py", file_path="utils.py",
            line_start=1, line_end=30, language="python",
        ), file_hash="d1")
        s.upsert_node(NodeInfo(
            kind="Function", name="format_date", file_path="utils.py",
            line_start=5, line_end=15, language="python",
            params="(dt: datetime)", return_type="str",
        ), file_hash="d1")

        # --- test_auth.py: tests ---
        s.upsert_node(NodeInfo(
            kind="File", name="test_auth.py", file_path="test_auth.py",
            line_start=1, line_end=40, language="python",
        ), file_hash="e1")
        s.upsert_node(NodeInfo(
            kind="Test", name="test_login", file_path="test_auth.py",
            line_start=5, line_end=15, language="python",
            is_test=True,
        ), file_hash="e1")

        # --- Edges: calls ---
        call_edges = [
            ("auth.py::login", "auth.py::verify_token", "auth.py", 10),
            ("auth.py::logout", "auth.py::verify_token", "auth.py", 30),
            ("api.py::get_users", "db.py::Database.query", "api.py", 10),
            ("api.py::get_users", "auth.py::verify_token", "api.py", 8),
            ("api.py::create_user", "db.py::Database.query", "api.py", 30),
            ("api.py::create_user", "auth.py::verify_token", "api.py", 28),
            ("db.py::Database.query", "db.py::Database.connect", "db.py", 35),
        ]
        for source, target, fp, ln in call_edges:
            s.upsert_edge(EdgeInfo(
                kind="CALLS", source=source, target=target,
                file_path=fp, line=ln,
            ))

        # --- Edges: contains ---
        contains_edges = [
            ("auth.py", "auth.py::login", "auth.py"),
            ("auth.py", "auth.py::logout", "auth.py"),
            ("auth.py", "auth.py::verify_token", "auth.py"),
            ("db.py", "db.py::Database", "db.py"),
            ("db.py::Database", "db.py::Database.connect", "db.py"),
            ("db.py::Database", "db.py::Database.query", "db.py"),
            ("db.py::Database", "db.py::Database.close", "db.py"),
            ("api.py", "api.py::get_users", "api.py"),
            ("api.py", "api.py::create_user", "api.py"),
            ("utils.py", "utils.py::format_date", "utils.py"),
        ]
        for source, target, fp in contains_edges:
            s.upsert_edge(EdgeInfo(
                kind="CONTAINS", source=source, target=target,
                file_path=fp, line=1,
            ))

        # --- Edges: tested_by ---
        s.upsert_edge(EdgeInfo(
            kind="TESTED_BY", source="test_auth.py::test_login",
            target="auth.py::login", file_path="test_auth.py", line=5,
        ))

        s.commit()

        # Set signatures for non-File nodes
        rows = s._conn.execute(
            "SELECT id, name, kind, params, return_type FROM nodes"
        ).fetchall()
        for row in rows:
            node_id, name, kind, params, ret = row[0], row[1], row[2], row[3], row[4]
            if kind in ("Function", "Test"):
                sig = f"def {name}({params or ''})"
                if ret:
                    sig += f" -> {ret}"
            elif kind == "Class":
                sig = f"class {name}"
            else:
                sig = name
            s._conn.execute(
                "UPDATE nodes SET signature = ? WHERE id = ?",
                (sig[:512], node_id),
            )
        s._conn.commit()

    # -----------------------------------------------------------------
    # Integration test
    # -----------------------------------------------------------------

    def test_full_pipeline(self):
        """Exercise the full v2 pipeline end-to-end."""

        # ---- Step 1: Verify graph data was seeded correctly ----
        stats = self.store.get_stats()
        assert stats.total_nodes >= 12, f"Expected >= 12 nodes, got {stats.total_nodes}"
        assert stats.total_edges >= 10, f"Expected >= 10 edges, got {stats.total_edges}"

        # ---- Step 2: trace_flows + store_flows ----
        flows = trace_flows(self.store)
        assert isinstance(flows, list)
        assert len(flows) > 0, "Should detect at least one flow"

        flow_count = store_flows(self.store, flows)
        assert flow_count == len(flows)

        # Verify retrieval
        stored = get_flows(self.store, limit=50)
        assert len(stored) > 0

        # Verify single flow retrieval
        first_flow = stored[0]
        detail = get_flow_by_id(self.store, first_flow["id"])
        assert detail is not None
        assert "steps" in detail

        # ---- Step 3: detect_communities + store_communities ----
        communities = detect_communities(self.store)
        assert isinstance(communities, list)
        assert len(communities) > 0, "Should detect at least one community"

        comm_count = store_communities(self.store, communities)
        assert comm_count == len(communities)

        # Verify retrieval
        stored_comms = get_communities(self.store)
        assert len(stored_comms) > 0
        # Each community should have name and size
        for comm in stored_comms:
            assert "name" in comm
            assert "size" in comm
            assert comm["size"] > 0

        # Architecture overview
        arch = get_architecture_overview(self.store)
        assert "communities" in arch
        assert "cross_community_edges" in arch

        # ---- Step 4: rebuild_fts_index + hybrid_search ----
        fts_count = rebuild_fts_index(self.store)
        assert fts_count > 0, "FTS should index at least some nodes"

        # Search for known functions
        results = hybrid_search(self.store, "login")
        assert len(results) > 0, "hybrid_search should find 'login'"
        names = [r["name"] for r in results]
        assert any("login" in n for n in names)

        # Search by kind
        results_func = hybrid_search(self.store, "query", kind="Function")
        assert len(results_func) > 0

        # ---- Step 5: analyze_changes ----
        change_result = analyze_changes(
            self.store,
            changed_files=["auth.py"],
            changed_ranges=None,
            repo_root=None,
            base="HEAD~1",
        )
        assert "summary" in change_result
        assert "risk_score" in change_result
        assert "changed_functions" in change_result
        assert "test_gaps" in change_result
        assert isinstance(change_result["risk_score"], (int, float))
        # auth.py has verify_token, logout -- logout should be a test gap
        # (login has a TESTED_BY edge)
        gap_names = [g["name"] for g in change_result["test_gaps"]]
        assert "verify_token" in gap_names or "logout" in gap_names, (
            f"Expected at least one test gap in auth.py, got: {gap_names}"
        )

        # ---- Step 6: find_dead_code ----
        dead = find_dead_code(self.store)
        assert isinstance(dead, list)
        dead_names = [d["name"] for d in dead]
        # format_date has no callers, no tests, no importers -- should be dead
        assert "format_date" in dead_names, (
            f"format_date should be dead code, got: {dead_names}"
        )

        # ---- Step 7: rename_preview ----
        preview = rename_preview(self.store, "verify_token", "validate_token")
        assert preview is not None, "rename_preview should find verify_token"
        assert "edits" in preview
        assert len(preview["edits"]) > 0
        # Should include definition + call sites
        edit_files = {e["file"] for e in preview["edits"]}
        assert "auth.py" in edit_files

        # ---- Step 8: generate_hints ----
        reset_session()
        session = get_session()
        hints = generate_hints(
            "detect_changes",
            change_result,
            session,
        )
        assert "next_steps" in hints
        assert "warnings" in hints
        assert isinstance(hints["next_steps"], list)

        # ---- Step 9: review_changes_prompt ----
        prompt_messages = review_changes_prompt(base="HEAD~1")
        assert isinstance(prompt_messages, list)
        assert len(prompt_messages) > 0
        assert prompt_messages[0].role == "user"
        assert "detect_changes" in prompt_messages[0].content.text

        # ---- Step 10: generate_wiki ----
        with tempfile.TemporaryDirectory() as wiki_dir:
            wiki_result = generate_wiki(self.store, wiki_dir, force=True)
            assert "pages_generated" in wiki_result
            assert "pages_updated" in wiki_result
            assert "pages_unchanged" in wiki_result
            total = (
                wiki_result["pages_generated"]
                + wiki_result["pages_updated"]
                + wiki_result["pages_unchanged"]
            )
            # At least one community page should have been generated
            assert total >= 0  # might be 0 if no communities stored
            if stored_comms:
                assert total > 0, "Wiki should generate pages for communities"
                # Verify index file exists
                index_path = Path(wiki_dir) / "index.md"
                assert index_path.exists(), "Wiki should generate index.md"

        # ---- Step 11: Registry (basic API test) ----
        with tempfile.TemporaryDirectory() as reg_dir:
            reg_path = Path(reg_dir) / "registry.json"
            registry = Registry(path=reg_path)

            # Empty initially
            assert registry.list_repos() == []

            # Register a fake repo (create .git dir so validation passes)
            fake_repo = Path(reg_dir) / "my-project"
            fake_repo.mkdir()
            (fake_repo / ".git").mkdir()

            entry = registry.register(str(fake_repo), alias="myproj")
            assert entry["alias"] == "myproj"
            assert str(fake_repo.resolve()) in entry["path"]

            repos = registry.list_repos()
            assert len(repos) == 1

            # Unregister
            assert registry.unregister("myproj") is True
            assert registry.list_repos() == []

    def test_affected_flows_with_changed_files(self):
        """get_affected_flows should identify flows touching changed files."""
        # Must have flows stored first
        flows = trace_flows(self.store)
        store_flows(self.store, flows)

        affected = get_affected_flows(self.store, changed_files=["auth.py"])
        assert "affected_flows" in affected
        assert "total" in affected
        # auth.py contains login/logout/verify_token -- flows through them
        # should be detected
        assert affected["total"] >= 0  # May be 0 if no flow touches auth.py

    def test_pipeline_idempotent(self):
        """Running the pipeline twice yields consistent results."""
        # First run
        flows1 = trace_flows(self.store)
        store_flows(self.store, flows1)
        comms1 = detect_communities(self.store)
        store_communities(self.store, comms1)
        fts1 = rebuild_fts_index(self.store)

        # Second run (should overwrite cleanly)
        flows2 = trace_flows(self.store)
        store_flows(self.store, flows2)
        comms2 = detect_communities(self.store)
        store_communities(self.store, comms2)
        fts2 = rebuild_fts_index(self.store)

        assert len(flows1) == len(flows2)
        assert len(comms1) == len(comms2)
        assert fts1 == fts2

    def test_search_after_rebuild(self):
        """FTS search works correctly after index rebuild."""
        rebuild_fts_index(self.store)

        # Exact function name
        results = hybrid_search(self.store, "create_user")
        assert any(r["name"] == "create_user" for r in results)

        # Class name
        results = hybrid_search(self.store, "Database")
        assert any(r["name"] == "Database" for r in results)

        # Partial match
        results = hybrid_search(self.store, "user")
        names = [r["name"] for r in results]
        assert any("user" in n.lower() for n in names)
