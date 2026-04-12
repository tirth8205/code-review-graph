"""Tests for change impact analysis (changes.py)."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from code_review_graph.changes import (
    _parse_unified_diff,
    analyze_changes,
    compute_risk_score,
    map_changes_to_nodes,
    parse_git_diff_ranges,
)
from code_review_graph.flows import store_flows, trace_flows
from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo


class TestChanges:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    # -- helpers --

    def _add_func(
        self,
        name: str,
        path: str = "app.py",
        parent: str | None = None,
        is_test: bool = False,
        line_start: int = 1,
        line_end: int = 10,
        extra: dict | None = None,
    ) -> int:
        node = NodeInfo(
            kind="Test" if is_test else "Function",
            name=name,
            file_path=path,
            line_start=line_start,
            line_end=line_end,
            language="python",
            parent_name=parent,
            is_test=is_test,
            extra=extra or {},
        )
        nid = self.store.upsert_node(node, file_hash="abc")
        self.store.commit()
        return nid

    def _add_call(self, source_qn: str, target_qn: str, path: str = "app.py") -> None:
        edge = EdgeInfo(
            kind="CALLS",
            source=source_qn,
            target=target_qn,
            file_path=path,
            line=5,
        )
        self.store.upsert_edge(edge)
        self.store.commit()

    def _add_tested_by(self, test_qn: str, target_qn: str, path: str = "app.py") -> None:
        edge = EdgeInfo(
            kind="TESTED_BY",
            source=test_qn,
            target=target_qn,
            file_path=path,
            line=1,
        )
        self.store.upsert_edge(edge)
        self.store.commit()

    # ---------------------------------------------------------------
    # parse_git_diff_ranges / _parse_unified_diff
    # ---------------------------------------------------------------

    def test_parse_unified_diff_basic(self):
        """Parses a simple unified diff into file -> range mappings."""
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -10,3 +10,5 @@ def foo():\n"
            "+    new line\n"
            "+    another\n"
        )
        result = _parse_unified_diff(diff)
        assert "foo.py" in result
        assert len(result["foo.py"]) == 1
        start, end = result["foo.py"][0]
        assert start == 10
        assert end == 14  # 10 + 5 - 1

    def test_parse_unified_diff_multiple_hunks(self):
        """Parses a diff with multiple hunks in one file."""
        diff = (
            "diff --git a/bar.py b/bar.py\n"
            "--- a/bar.py\n"
            "+++ b/bar.py\n"
            "@@ -5,2 +5,3 @@ class Bar:\n"
            "+    x\n"
            "@@ -20,1 +21,4 @@ def method():\n"
            "+    y\n"
        )
        result = _parse_unified_diff(diff)
        assert "bar.py" in result
        assert len(result["bar.py"]) == 2
        assert result["bar.py"][0] == (5, 7)   # 5 + 3 - 1
        assert result["bar.py"][1] == (21, 24)  # 21 + 4 - 1

    def test_parse_unified_diff_single_line(self):
        """Parses a diff where count is omitted (single line change)."""
        diff = (
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1 +1 @@\n"
            "+changed\n"
        )
        result = _parse_unified_diff(diff)
        assert "x.py" in result
        assert result["x.py"][0] == (1, 1)

    def test_parse_unified_diff_deletion_only(self):
        """Handles pure deletion hunks (+start,0)."""
        diff = (
            "--- a/del.py\n"
            "+++ b/del.py\n"
            "@@ -10,3 +10,0 @@ some context\n"
        )
        result = _parse_unified_diff(diff)
        assert "del.py" in result
        # Count=0 means deletion, start=end
        assert result["del.py"][0] == (10, 10)

    def test_parse_unified_diff_multiple_files(self):
        """Parses a diff spanning two files."""
        diff = (
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,2 +1,3 @@\n"
            "+x\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -5,1 +5,2 @@\n"
            "+y\n"
        )
        result = _parse_unified_diff(diff)
        assert "a.py" in result
        assert "b.py" in result

    def test_parse_git_diff_ranges_error_handling(self):
        """Returns empty dict when git command fails."""
        result = parse_git_diff_ranges("/nonexistent/path", base="HEAD~1")
        assert result == {}

    # ---------------------------------------------------------------
    # map_changes_to_nodes
    # ---------------------------------------------------------------

    def test_map_changes_to_nodes_overlap(self):
        """Finds nodes whose line ranges overlap the changed lines."""
        self._add_func("func_a", path="app.py", line_start=5, line_end=15)
        self._add_func("func_b", path="app.py", line_start=20, line_end=30)
        self._add_func("func_c", path="app.py", line_start=35, line_end=45)

        # Change lines 10-25: overlaps func_a (5-15) and func_b (20-30)
        changed_ranges = {"app.py": [(10, 25)]}
        nodes = map_changes_to_nodes(self.store, changed_ranges)

        names = {n.name for n in nodes}
        assert "func_a" in names
        assert "func_b" in names
        assert "func_c" not in names

    def test_map_changes_to_nodes_no_overlap(self):
        """Returns empty when no nodes overlap the changed lines."""
        self._add_func("func_a", path="app.py", line_start=5, line_end=10)

        changed_ranges = {"app.py": [(50, 60)]}
        nodes = map_changes_to_nodes(self.store, changed_ranges)
        assert len(nodes) == 0

    def test_map_changes_to_nodes_deduplication(self):
        """Deduplicates nodes by qualified name when overlapping multiple ranges."""
        self._add_func("func_a", path="app.py", line_start=5, line_end=20)

        # Two ranges that both overlap func_a.
        changed_ranges = {"app.py": [(6, 8), (15, 18)]}
        nodes = map_changes_to_nodes(self.store, changed_ranges)
        assert len(nodes) == 1
        assert nodes[0].name == "func_a"

    def test_map_changes_to_nodes_different_files(self):
        """Maps changes across different files."""
        self._add_func("func_x", path="x.py", line_start=1, line_end=10)
        self._add_func("func_y", path="y.py", line_start=1, line_end=10)

        changed_ranges = {
            "x.py": [(3, 5)],
            "y.py": [(3, 5)],
        }
        nodes = map_changes_to_nodes(self.store, changed_ranges)
        names = {n.name for n in nodes}
        assert "func_x" in names
        assert "func_y" in names

    # ---------------------------------------------------------------
    # compute_risk_score
    # ---------------------------------------------------------------

    def test_risk_score_range(self):
        """Risk score is always between 0 and 1."""
        self._add_func("simple_func")
        node = self.store.get_node("app.py::simple_func")
        assert node is not None
        score = compute_risk_score(self.store, node)
        assert 0.0 <= score <= 1.0

    def test_risk_score_untested_is_higher(self):
        """Untested functions score higher than tested ones."""
        self._add_func("untested_func", path="a.py", line_start=1, line_end=10)
        self._add_func("tested_func", path="b.py", line_start=1, line_end=10)
        self._add_func("test_tested_func", path="test_b.py", is_test=True)
        self._add_tested_by("test_b.py::test_tested_func", "b.py::tested_func", "test_b.py")

        untested = self.store.get_node("a.py::untested_func")
        tested = self.store.get_node("b.py::tested_func")
        assert untested is not None
        assert tested is not None

        untested_score = compute_risk_score(self.store, untested)
        tested_score = compute_risk_score(self.store, tested)
        # Untested gets 0.30, tested gets 0.05 for test coverage component.
        assert untested_score > tested_score

    def test_risk_score_security_keywords_boost(self):
        """Functions with security keywords score higher."""
        self._add_func("process_data", path="a.py")
        self._add_func("verify_auth_token", path="b.py")

        normal = self.store.get_node("a.py::process_data")
        secure = self.store.get_node("b.py::verify_auth_token")
        assert normal is not None
        assert secure is not None

        normal_score = compute_risk_score(self.store, normal)
        secure_score = compute_risk_score(self.store, secure)
        assert secure_score > normal_score

    def test_risk_score_with_callers(self):
        """Functions with many callers get a caller count bonus."""
        self._add_func("popular_func", path="lib.py")
        for i in range(10):
            caller_name = f"caller_{i}"
            self._add_func(caller_name, path=f"c{i}.py")
            self._add_call(f"c{i}.py::{caller_name}", "lib.py::popular_func", f"c{i}.py")

        self._add_func("lonely_func", path="other.py")

        popular = self.store.get_node("lib.py::popular_func")
        lonely = self.store.get_node("other.py::lonely_func")
        assert popular is not None
        assert lonely is not None

        popular_score = compute_risk_score(self.store, popular)
        lonely_score = compute_risk_score(self.store, lonely)
        assert popular_score > lonely_score

    def test_risk_score_with_flow_membership(self):
        """Nodes participating in flows get a flow participation bonus."""
        # Build a flow: entry -> helper
        self._add_func("entry", path="app.py", line_start=1, line_end=10)
        self._add_func("helper", path="app.py", line_start=15, line_end=25)
        self._add_call("app.py::entry", "app.py::helper")

        flows = trace_flows(self.store)
        store_flows(self.store, flows)

        # helper participates in a flow.
        helper = self.store.get_node("app.py::helper")
        assert helper is not None

        # An isolated node with no flows.
        self._add_func("isolated", path="iso.py")
        isolated = self.store.get_node("iso.py::isolated")
        assert isolated is not None

        helper_score = compute_risk_score(self.store, helper)
        isolated_score = compute_risk_score(self.store, isolated)
        # helper should have flow participation bonus.
        assert helper_score >= isolated_score

    def test_risk_score_weighted_by_flow_criticality(self):
        """Nodes in high-criticality flows score higher than low-criticality."""
        # Build two separate flows with different criticality
        self._add_func("hi_entry", path="hi.py", line_start=1, line_end=5)
        self._add_func("hi_func", path="hi.py", line_start=10, line_end=20)
        self._add_call("hi.py::hi_entry", "hi.py::hi_func")

        self._add_func("lo_entry", path="lo.py", line_start=1, line_end=5)
        self._add_func("lo_func", path="lo.py", line_start=10, line_end=20)
        self._add_call("lo.py::lo_entry", "lo.py::lo_func")

        flows = trace_flows(self.store)
        store_flows(self.store, flows)

        # Manually set different criticality values
        self.store._conn.execute(
            "UPDATE flows SET criticality = 0.9 "
            "WHERE name = 'hi_entry'"
        )
        self.store._conn.execute(
            "UPDATE flows SET criticality = 0.1 "
            "WHERE name = 'lo_entry'"
        )
        self.store.commit()

        hi = self.store.get_node("hi.py::hi_func")
        lo = self.store.get_node("lo.py::lo_func")
        assert hi and lo

        hi_score = compute_risk_score(self.store, hi)
        lo_score = compute_risk_score(self.store, lo)
        assert hi_score > lo_score, (
            f"High-criticality flow node ({hi_score}) should score "
            f"higher than low-criticality ({lo_score})"
        )

    # ---------------------------------------------------------------
    # analyze_changes
    # ---------------------------------------------------------------

    def test_analyze_changes_returns_expected_keys(self):
        """analyze_changes returns all expected top-level keys."""
        self._add_func("changed_func", path="app.py", line_start=1, line_end=10)
        result = analyze_changes(
            self.store,
            changed_files=["app.py"],
            changed_ranges={"app.py": [(1, 10)]},
        )
        assert "summary" in result
        assert "risk_score" in result
        assert "changed_functions" in result
        assert "affected_flows" in result
        assert "test_gaps" in result
        assert "review_priorities" in result

    def test_analyze_changes_risk_score_range(self):
        """Overall risk score is between 0 and 1."""
        self._add_func("func_a", path="app.py", line_start=1, line_end=10)
        result = analyze_changes(
            self.store,
            changed_files=["app.py"],
            changed_ranges={"app.py": [(1, 10)]},
        )
        assert 0.0 <= result["risk_score"] <= 1.0

    def test_analyze_detects_test_gaps(self):
        """Changed functions without TESTED_BY edges are flagged as test gaps."""
        self._add_func("untested_a", path="app.py", line_start=1, line_end=10)
        self._add_func("untested_b", path="app.py", line_start=15, line_end=25)
        self._add_func("tested_c", path="app.py", line_start=30, line_end=40)

        # Only tested_c has a test.
        self._add_func("test_c", path="test_app.py", is_test=True)
        self._add_tested_by("test_app.py::test_c", "app.py::tested_c", "test_app.py")

        result = analyze_changes(
            self.store,
            changed_files=["app.py"],
            changed_ranges={"app.py": [(1, 40)]},
        )
        gap_names = {g["name"] for g in result["test_gaps"]}
        assert "untested_a" in gap_names
        assert "untested_b" in gap_names
        assert "tested_c" not in gap_names

    def test_analyze_changes_with_flows(self):
        """analyze_changes detects affected flows."""
        self._add_func("handler", path="routes.py", line_start=1, line_end=10)
        self._add_func("service", path="services.py", line_start=1, line_end=10)
        self._add_call("routes.py::handler", "services.py::service", "routes.py")

        flows = trace_flows(self.store)
        store_flows(self.store, flows)

        result = analyze_changes(
            self.store,
            changed_files=["services.py"],
            changed_ranges={"services.py": [(1, 10)]},
        )
        assert len(result["affected_flows"]) >= 1

    def test_analyze_changes_review_priorities_ordered(self):
        """Review priorities are ordered by descending risk score."""
        # Create several functions with varying risk levels.
        self._add_func("safe_func", path="app.py", line_start=1, line_end=5)
        self._add_func("auth_handler", path="app.py", line_start=10, line_end=20)

        result = analyze_changes(
            self.store,
            changed_files=["app.py"],
            changed_ranges={"app.py": [(1, 20)]},
        )
        priorities = result["review_priorities"]
        if len(priorities) >= 2:
            for i in range(len(priorities) - 1):
                assert priorities[i]["risk_score"] >= priorities[i + 1]["risk_score"]

    def test_analyze_changes_fallback_no_ranges(self):
        """Falls back to all nodes in files when no ranges provided."""
        self._add_func("func_a", path="app.py", line_start=1, line_end=10)
        self._add_func("func_b", path="app.py", line_start=15, line_end=25)

        result = analyze_changes(
            self.store,
            changed_files=["app.py"],
            changed_ranges=None,
        )
        # Should still find functions even without ranges.
        assert len(result["changed_functions"]) >= 1

    # ---------------------------------------------------------------
    # detect_changes_func (integration)
    # ---------------------------------------------------------------

    def test_detect_changes_tool_no_changes(self):
        """detect_changes_func returns clean result when no changes detected."""
        from code_review_graph.tools import detect_changes_func

        # Patch _get_store to use our test store,
        # and get_changed_files/get_staged_and_unstaged to return empty.
        with (
            patch("code_review_graph.tools.review._get_store") as mock_get_store,
            patch("code_review_graph.tools.review.get_changed_files", return_value=[]),
            patch("code_review_graph.tools.review.get_staged_and_unstaged", return_value=[]),
        ):
            mock_get_store.return_value = (self.store, Path("/fake/repo"))
            # Prevent the store from being closed by the tool
            # (our teardown handles it).
            self.store.close = lambda: None

            result = detect_changes_func(base="HEAD~1", repo_root="/fake/repo")
            assert result["status"] == "ok"
            assert result["risk_score"] == 0.0
            assert result["changed_functions"] == []
            assert result["test_gaps"] == []

    def test_detect_changes_tool_with_changes(self):
        """detect_changes_func returns full analysis for changed files."""
        from code_review_graph.tools import detect_changes_func

        self._add_func("my_func", path="/fake/repo/app.py", line_start=1, line_end=10)

        with (
            patch("code_review_graph.tools.review._get_store") as mock_get_store,
            patch("code_review_graph.tools.review.get_changed_files", return_value=["app.py"]),
            patch(
                "code_review_graph.tools.review.parse_git_diff_ranges",
                return_value={"app.py": [(1, 10)]},
            ),
        ):
            mock_get_store.return_value = (self.store, Path("/fake/repo"))
            self.store.close = lambda: None

            result = detect_changes_func(base="HEAD~1", repo_root="/fake/repo")
            assert result["status"] == "ok"
            assert "changed_functions" in result
            assert "risk_score" in result
            assert "test_gaps" in result
            assert "review_priorities" in result
