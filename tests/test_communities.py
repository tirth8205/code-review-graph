"""Tests for community/cluster detection."""

import tempfile
from pathlib import Path

import pytest

from code_review_graph.communities import (
    IGRAPH_AVAILABLE,
    _compute_cohesion,
    _detect_file_based,
    _generate_community_name,
    detect_communities,
    get_architecture_overview,
    get_communities,
    incremental_detect_communities,
    store_communities,
)
from code_review_graph.graph import GraphEdge, GraphNode, GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo


class TestCommunities:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _seed_two_clusters(self):
        """Seed two distinct clusters: auth (auth.py) and db (db.py)."""
        # Auth cluster
        self.store.upsert_node(
            NodeInfo(
                kind="File", name="auth.py", file_path="auth.py",
                line_start=1, line_end=100, language="python",
            ), file_hash="a1"
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function", name="login", file_path="auth.py",
                line_start=5, line_end=20, language="python",
            ), file_hash="a1"
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function", name="logout", file_path="auth.py",
                line_start=25, line_end=40, language="python",
            ), file_hash="a1"
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function", name="check_token", file_path="auth.py",
                line_start=45, line_end=60, language="python",
            ), file_hash="a1"
        )
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="auth.py::login",
            target="auth.py::check_token", file_path="auth.py", line=10,
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="auth.py::logout",
            target="auth.py::check_token", file_path="auth.py", line=30,
        ))

        # DB cluster
        self.store.upsert_node(
            NodeInfo(
                kind="File", name="db.py", file_path="db.py",
                line_start=1, line_end=100, language="python",
            ), file_hash="b1"
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function", name="connect", file_path="db.py",
                line_start=5, line_end=20, language="python",
            ), file_hash="b1"
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function", name="query", file_path="db.py",
                line_start=25, line_end=40, language="python",
            ), file_hash="b1"
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function", name="close", file_path="db.py",
                line_start=45, line_end=60, language="python",
            ), file_hash="b1"
        )
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="db.py::query",
            target="db.py::connect", file_path="db.py", line=30,
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="db.py::close",
            target="db.py::connect", file_path="db.py", line=50,
        ))

        # One cross-cluster edge
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="auth.py::login",
            target="db.py::query", file_path="auth.py", line=15,
        ))
        self.store.commit()

    def test_detect_communities_returns_list(self):
        """detect_communities returns a list."""
        self._seed_two_clusters()
        result = detect_communities(self.store, min_size=2)
        assert isinstance(result, list)

    @pytest.mark.skipif(not IGRAPH_AVAILABLE, reason="igraph not installed")
    def test_detect_finds_clusters(self):
        """With clear clusters and igraph, finds >= 2 communities."""
        self._seed_two_clusters()
        result = detect_communities(self.store, min_size=2)
        assert len(result) >= 2

    def test_community_has_required_fields(self):
        """Each community dict has required fields: name, size, cohesion, members."""
        self._seed_two_clusters()
        result = detect_communities(self.store, min_size=2)
        assert len(result) > 0
        for comm in result:
            assert "name" in comm
            assert "size" in comm
            assert "cohesion" in comm
            assert "members" in comm
            assert isinstance(comm["name"], str)
            assert isinstance(comm["size"], int)
            assert isinstance(comm["cohesion"], (int, float))
            assert isinstance(comm["members"], list)

    def test_store_and_retrieve_communities(self):
        """Communities can be stored and retrieved round-trip."""
        self._seed_two_clusters()
        communities = detect_communities(self.store, min_size=2)
        assert len(communities) > 0

        count = store_communities(self.store, communities)
        assert count == len(communities)

        retrieved = get_communities(self.store)
        assert len(retrieved) == len(communities)
        for comm in retrieved:
            assert "id" in comm
            assert "name" in comm
            assert "size" in comm

    def test_architecture_overview(self):
        """Architecture overview has required keys."""
        self._seed_two_clusters()
        communities = detect_communities(self.store, min_size=2)
        store_communities(self.store, communities)

        overview = get_architecture_overview(self.store)
        assert "communities" in overview
        assert "cross_community_edges" in overview
        assert "warnings" in overview
        assert isinstance(overview["communities"], list)
        assert isinstance(overview["cross_community_edges"], list)
        assert isinstance(overview["warnings"], list)

    def test_fallback_file_communities(self):
        """File-based fallback produces communities grouped by file."""
        self._seed_two_clusters()
        # Gather nodes and edges for file-based detection
        all_edges = self.store.get_all_edges()
        nodes = []
        for fp in self.store.get_all_files():
            nodes.extend(self.store.get_nodes_by_file(fp))

        result = _detect_file_based(nodes, all_edges, min_size=2)
        assert isinstance(result, list)
        assert len(result) >= 2
        for comm in result:
            assert "name" in comm
            assert "size" in comm
            assert comm["size"] >= 2

    def test_community_naming(self):
        """Community naming produces non-empty names."""
        self._seed_two_clusters()
        result = detect_communities(self.store, min_size=2)
        for comm in result:
            assert comm["name"]
            assert len(comm["name"]) > 0

    def test_community_naming_with_dominant_class(self):
        """When a class dominates (>40%), it appears in the name."""
        nodes = [
            GraphNode(
                id=1, kind="Class", name="AuthService", qualified_name="auth.py::AuthService",
                file_path="auth.py", line_start=1, line_end=100, language="python",
                parent_name=None, params=None, return_type=None, is_test=False,
                file_hash="x", extra={},
            ),
            GraphNode(
                id=2, kind="Function", name="login", qualified_name="auth.py::AuthService.login",
                file_path="auth.py", line_start=10, line_end=20, language="python",
                parent_name="AuthService", params=None, return_type=None, is_test=False,
                file_hash="x", extra={},
            ),
        ]
        name = _generate_community_name(nodes)
        assert name  # non-empty
        assert "authservice" in name.lower() or "auth" in name.lower()

    def test_community_naming_empty(self):
        """Empty member list produces 'empty' name."""
        name = _generate_community_name([])
        assert name == "empty"

    def test_cohesion_computation(self):
        """Cohesion is correctly computed as internal/(internal+external)."""
        member_qns = {"a", "b"}
        edges = [
            GraphEdge(
                id=1, kind="CALLS", source_qualified="a",
                target_qualified="b", file_path="f.py", line=1, extra={},
            ),
            GraphEdge(
                id=2, kind="CALLS", source_qualified="a",
                target_qualified="c", file_path="f.py", line=2, extra={},
            ),
        ]
        cohesion = _compute_cohesion(member_qns, edges)
        # 1 internal (a->b), 1 external (a->c) => 0.5
        assert cohesion == pytest.approx(0.5)

    def test_cohesion_all_internal(self):
        """All edges internal => cohesion = 1.0."""
        member_qns = {"a", "b"}
        edges = [
            GraphEdge(
                id=1, kind="CALLS", source_qualified="a",
                target_qualified="b", file_path="f.py", line=1, extra={},
            ),
        ]
        cohesion = _compute_cohesion(member_qns, edges)
        assert cohesion == pytest.approx(1.0)

    def test_cohesion_no_edges(self):
        """No edges => cohesion = 0.0."""
        member_qns = {"a", "b"}
        cohesion = _compute_cohesion(member_qns, [])
        assert cohesion == pytest.approx(0.0)

    def test_get_communities_sort_by(self):
        """get_communities respects sort_by parameter."""
        self._seed_two_clusters()
        communities = detect_communities(self.store, min_size=2)
        store_communities(self.store, communities)

        by_size = get_communities(self.store, sort_by="size")
        assert len(by_size) > 0
        # Sizes should be in descending order
        sizes = [c["size"] for c in by_size]
        assert sizes == sorted(sizes, reverse=True)

        by_name = get_communities(self.store, sort_by="name")
        names = [c["name"] for c in by_name]
        assert names == sorted(names)

    def test_get_communities_min_size_filter(self):
        """get_communities with min_size filters small communities."""
        self._seed_two_clusters()
        communities = detect_communities(self.store, min_size=1)
        store_communities(self.store, communities)

        # With very high min_size, should get empty
        result = get_communities(self.store, min_size=999)
        assert len(result) == 0

    def test_store_communities_clears_previous(self):
        """Storing communities clears previous community data."""
        self._seed_two_clusters()
        communities = detect_communities(self.store, min_size=2)
        store_communities(self.store, communities)

        first_count = len(get_communities(self.store))
        assert first_count > 0

        # Store again with empty list
        store_communities(self.store, [])
        assert len(get_communities(self.store)) == 0

    def test_detect_communities_empty_graph(self):
        """Detect on empty graph returns empty list."""
        result = detect_communities(self.store, min_size=2)
        assert result == []

    def test_igraph_available_is_bool(self):
        """IGRAPH_AVAILABLE is a boolean."""
        assert isinstance(IGRAPH_AVAILABLE, bool)

    def test_incremental_detect_no_affected_communities(self):
        """incremental_detect_communities returns 0 when no communities are affected."""
        self._seed_two_clusters()
        communities = detect_communities(self.store, min_size=2)
        store_communities(self.store, communities)

        # Pass a file that has no nodes in any community
        result = incremental_detect_communities(self.store, ["nonexistent.py"])
        assert result == 0

    def test_incremental_detect_redetects_affected(self):
        """incremental_detect_communities re-detects when communities ARE affected."""
        self._seed_two_clusters()
        communities = detect_communities(self.store, min_size=2)
        stored = store_communities(self.store, communities)
        assert stored > 0

        # Pass a file that IS part of existing communities
        result = incremental_detect_communities(self.store, ["auth.py"])
        assert result > 0
