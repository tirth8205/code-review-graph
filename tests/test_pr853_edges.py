"""Edge-case tests for get_affected_flows detail_level / max_flows (#849, PR #853).

Covers boundaries and scale beyond the PR's own tests: exact-boundary
truncation, the default max_flows=50 cap on a 60-flow graph, negative and
huge max_flows values, minimal projection combined with truncation,
criticality ordering after projection, unknown detail_level fallback, and
unicode changed-file paths.
"""

import tempfile
from pathlib import Path

from code_review_graph.graph import EdgeInfo, GraphStore, NodeInfo
from code_review_graph.tools.review import get_affected_flows_func

MINIMAL_KEYS = {"id", "name", "criticality", "depth", "node_count", "file_count"}


class _FlowFixture:
    """Seed a repo-shaped temp dir with N entry points flowing through shared.py."""

    def setup_flows(self, n_flows: int):
        self.tmp_dir = tempfile.mkdtemp()
        self.root = Path(self.tmp_dir).resolve()
        (self.root / ".git").mkdir()
        (self.root / ".code-review-graph").mkdir()

        db_path = str(self.root / ".code-review-graph" / "graph.db")
        self.store = GraphStore(db_path)

        shared_py = (self.root / "shared.py").as_posix()
        self.store.upsert_node(NodeInfo(
            kind="File", name="shared.py", file_path=shared_py,
            line_start=1, line_end=50, language="python",
        ))
        self.store.upsert_node(NodeInfo(
            kind="Function", name="shared_helper", file_path=shared_py,
            line_start=5, line_end=20, language="python",
        ))

        for i in range(n_flows):
            entry_file = (self.root / f"entry_{i}.py").as_posix()
            self.store.upsert_node(NodeInfo(
                kind="File", name=f"entry_{i}.py", file_path=entry_file,
                line_start=1, line_end=30, language="python",
            ))
            self.store.upsert_node(NodeInfo(
                kind="Function", name=f"entry_point_{i}", file_path=entry_file,
                line_start=3, line_end=15, language="python",
            ))
            self.store.upsert_edge(EdgeInfo(
                kind="CALLS",
                source=f"{entry_file}::entry_point_{i}",
                target=f"{shared_py}::shared_helper",
                file_path=entry_file, line=7,
            ))
        self.store.commit()

        from code_review_graph.flows import store_flows, trace_flows
        store_flows(self.store, trace_flows(self.store))

    def teardown_method(self):
        self.store.close()
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def affected(self, **kwargs):
        return get_affected_flows_func(
            changed_files=["shared.py"], repo_root=str(self.root), **kwargs
        )


class TestMaxFlowsBoundaries(_FlowFixture):
    """Exact boundary behavior of max_flows on a 5-flow graph."""

    def setup_method(self):
        self.setup_flows(5)

    def test_max_flows_equal_to_total_is_not_truncated(self):
        result = self.affected(max_flows=5)
        assert result["status"] == "ok"
        assert result["total"] == 5
        assert len(result["affected_flows"]) == 5
        assert result["truncated"] is False
        assert "showing" not in result["summary"]

    def test_max_flows_one_below_total_truncates(self):
        result = self.affected(max_flows=4)
        assert result["total"] == 5
        assert len(result["affected_flows"]) == 4
        assert result["truncated"] is True
        assert "showing 4" in result["summary"]

    def test_max_flows_one_returns_single_highest_criticality(self):
        full = self.affected(max_flows=0)
        result = self.affected(max_flows=1)
        assert len(result["affected_flows"]) == 1
        assert result["truncated"] is True
        # Truncation keeps the head of the criticality-sorted list.
        assert result["affected_flows"][0]["id"] == full["affected_flows"][0]["id"]

    def test_negative_max_flows_disables_limit(self):
        # Documented contract is 0 disables; negatives currently behave the
        # same way (no truncation) rather than raising or returning nothing.
        result = self.affected(max_flows=-1)
        assert result["status"] == "ok"
        assert result["truncated"] is False
        assert len(result["affected_flows"]) == result["total"] == 5

    def test_huge_max_flows_returns_everything(self):
        result = self.affected(max_flows=10**9)
        assert result["truncated"] is False
        assert len(result["affected_flows"]) == 5

    def test_truncated_key_present_when_nothing_matches(self):
        result = get_affected_flows_func(
            changed_files=["unrelated.py"], repo_root=str(self.root)
        )
        assert result["status"] == "ok"
        assert result["total"] == 0
        assert result["truncated"] is False


class TestDefaultCapAtScale(_FlowFixture):
    """The default max_flows=50 must bound a 60-flow response (#849)."""

    def setup_method(self):
        self.setup_flows(60)

    def test_default_truncates_sixty_flows_to_fifty(self):
        result = self.affected()
        assert result["status"] == "ok"
        assert result["total"] == 60
        assert len(result["affected_flows"]) == 50
        assert result["truncated"] is True
        assert "showing 50" in result["summary"]

    def test_zero_disables_limit_at_scale(self):
        result = self.affected(max_flows=0)
        assert result["truncated"] is False
        assert len(result["affected_flows"]) == result["total"] == 60

    def test_minimal_with_default_cap_stays_projected(self):
        result = self.affected(detail_level="minimal")
        assert len(result["affected_flows"]) == 50
        assert result["truncated"] is True
        for flow in result["affected_flows"]:
            assert set(flow.keys()) == MINIMAL_KEYS


class TestMinimalProjection(_FlowFixture):
    def setup_method(self):
        self.setup_flows(5)

    def test_minimal_has_exactly_the_documented_keys(self):
        result = self.affected(detail_level="minimal")
        assert result["total"] == 5
        for flow in result["affected_flows"]:
            assert set(flow.keys()) == MINIMAL_KEYS
            assert flow["id"] is not None
            assert flow["name"]
            assert flow["node_count"] >= 1

    def test_minimal_matches_standard_order_and_identity(self):
        standard = self.affected(max_flows=0)
        minimal = self.affected(detail_level="minimal", max_flows=0)
        assert [f["id"] for f in minimal["affected_flows"]] == [
            f["id"] for f in standard["affected_flows"]
        ]
        crits = [f["criticality"] for f in minimal["affected_flows"]]
        assert crits == sorted(crits, reverse=True)

    def test_minimal_truncation_applies_before_projection(self):
        standard = self.affected(max_flows=0)
        result = self.affected(detail_level="minimal", max_flows=2)
        assert len(result["affected_flows"]) == 2
        assert result["total"] == 5
        assert [f["id"] for f in result["affected_flows"]] == [
            f["id"] for f in standard["affected_flows"][:2]
        ]

    def test_unknown_detail_level_falls_back_to_standard(self):
        # Sibling tools treat anything except "minimal" as standard; the new
        # parameter follows the same convention rather than raising.
        for level in ("full", "MINIMAL", "", "detailed"):
            result = self.affected(detail_level=level)
            assert result["status"] == "ok"
            assert all("steps" in f for f in result["affected_flows"])

    def test_unicode_changed_file_is_handled(self):
        result = get_affected_flows_func(
            changed_files=["mödulé/日本語.py"],
            repo_root=str(self.root),
        )
        assert result["status"] == "ok"
        assert result["total"] == 0
        assert result["truncated"] is False
