"""Tests for community/cluster detection."""

import tempfile
from pathlib import Path

import pytest

import code_review_graph.communities as communities_module
from code_review_graph.communities import (
    IGRAPH_AVAILABLE,
    _compute_cohesion,
    _compute_cohesion_batch,
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


def _community_node(
    node_id: int,
    qualified_name: str,
    *,
    kind: str = "Function",
    is_test: bool = False,
    file_path: str | None = None,
) -> GraphNode:
    """Build a compact GraphNode fixture for community algorithm tests."""
    path, _, name = qualified_name.partition("::")
    return GraphNode(
        id=node_id,
        kind=kind,
        name=name,
        qualified_name=qualified_name,
        file_path=file_path or path,
        line_start=1,
        line_end=2,
        language="python",
        parent_name=None,
        params=None,
        return_type=None,
        is_test=is_test,
        file_hash="fixture",
        extra={},
    )


def _community_edge(
    edge_id: int,
    source: str,
    target: str,
    *,
    kind: str = "CALLS",
) -> GraphEdge:
    """Build a compact GraphEdge fixture for community algorithm tests."""
    return GraphEdge(
        id=edge_id,
        kind=kind,
        source_qualified=source,
        target_qualified=target,
        file_path="fixture.py",
        line=edge_id,
        extra={},
    )


class TestCommunities:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()  # release the handle before GraphStore reopens it on Windows
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

    def test_architecture_overview_excludes_tested_by_coupling(self):
        """TESTED_BY edges do not count toward coupling warnings."""
        self._seed_two_clusters()
        communities = detect_communities(self.store, min_size=2)
        store_communities(self.store, communities)

        # Add many TESTED_BY cross-community edges (well above the threshold of 10)
        for i in range(20):
            self.store.upsert_edge(EdgeInfo(
                kind="TESTED_BY", source=f"auth.py::login",
                target=f"db.py::query", file_path="auth.py", line=i + 100,
            ))
        self.store.commit()

        overview = get_architecture_overview(self.store)
        # Warnings should not include any that are purely from TESTED_BY edges
        for w in overview["warnings"]:
            assert "TESTED_BY" not in w

    def test_architecture_overview_excludes_test_community_warnings(self):
        """Warnings involving test-dominated communities are filtered out."""
        self._seed_two_clusters()
        communities = detect_communities(self.store, min_size=2)
        store_communities(self.store, communities)

        # Manually insert a test-named community with high cross-coupling
        conn = self.store._conn
        cursor = conn.execute(
            "INSERT INTO communities (name, level, cohesion, size, dominant_language, description)"
            " VALUES (?, 0, 0.5, 10, 'typescript', 'Test community')",
            ("handler-it:should",),
        )
        test_comm_id = cursor.lastrowid
        # Assign some nodes to this community (reuse existing node)
        conn.execute(
            "UPDATE nodes SET community_id = ? WHERE name = 'login'",
            (test_comm_id,),
        )
        conn.commit()

        overview = get_architecture_overview(self.store)
        for w in overview["warnings"]:
            assert "it:should" not in w, f"Test community should be filtered: {w}"

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

    def test_compute_cohesion_batch_matches_single(self):
        """Batch cohesion must produce identical results to calling
        _compute_cohesion once per community. Regression guard for the
        O(files * edges) -> O(edges) refactor.
        """
        edges = [
            # Internal to comm_a
            GraphEdge(
                id=1, kind="CALLS", source_qualified="a::f1",
                target_qualified="a::f2", file_path="a.py", line=1, extra={},
            ),
            # Cross-community (a <-> b): external to both
            GraphEdge(
                id=2, kind="CALLS", source_qualified="a::f1",
                target_qualified="b::g1", file_path="a.py", line=2, extra={},
            ),
            # Internal to comm_b
            GraphEdge(
                id=3, kind="CALLS", source_qualified="b::g1",
                target_qualified="b::g2", file_path="b.py", line=3, extra={},
            ),
            # Half-in (b -> c): external to b, ignored by a
            GraphEdge(
                id=4, kind="CALLS", source_qualified="b::g1",
                target_qualified="c::h1", file_path="b.py", line=4, extra={},
            ),
            # Neither endpoint in any tracked community — fully ignored
            GraphEdge(
                id=5, kind="CALLS", source_qualified="c::h1",
                target_qualified="d::k1", file_path="c.py", line=5, extra={},
            ),
        ]
        comm_a = {"a::f1", "a::f2"}
        comm_b = {"b::g1", "b::g2"}

        batch = _compute_cohesion_batch([comm_a, comm_b], edges)
        expected = [
            _compute_cohesion(comm_a, edges),
            _compute_cohesion(comm_b, edges),
        ]
        assert batch == expected
        # Sanity: comm_a has 1 internal + 1 external = 0.5
        # comm_b has 1 internal + 2 external = 1/3
        assert batch[0] == pytest.approx(0.5)
        assert batch[1] == pytest.approx(1 / 3)

    def test_compute_cohesion_batch_empty(self):
        """Batch with empty list returns empty list."""
        assert _compute_cohesion_batch([], []) == []

    def test_compute_cohesion_batch_no_edges(self):
        """Batch with no edges returns 0.0 per community."""
        result = _compute_cohesion_batch([{"a"}, {"b", "c"}], [])
        assert result == [0.0, 0.0]

    def test_detect_file_based_integration(self):
        """End-to-end: _detect_file_based produces correct member sets and
        cohesion values on a hand-built fixture with asymmetric cohesions.

        Guards the batch-cohesion refactor against zip misalignment, wrong
        member_qns passed to the batch helper, and member/cohesion drift.
        Cohesions are deliberately distinct (1.0 vs 0.6667) so a swap would
        fail the assertions.
        """
        def mk_node(nid: int, name: str, fp: str) -> GraphNode:
            return GraphNode(
                id=nid, kind="Function", name=name,
                qualified_name=f"{fp}::{name}",
                file_path=fp, line_start=1, line_end=10, language="python",
                parent_name=None, params=None, return_type=None, is_test=False,
                file_hash="h", extra={},
            )

        def mk_edge(eid: int, src: str, tgt: str, fp: str) -> GraphEdge:
            return GraphEdge(
                id=eid, kind="CALLS", source_qualified=src,
                target_qualified=tgt, file_path=fp, line=1, extra={},
            )

        nodes = [
            mk_node(1, "login", "auth.py"),
            mk_node(2, "logout", "auth.py"),
            mk_node(3, "check_token", "auth.py"),
            mk_node(4, "connect", "db.py"),
            mk_node(5, "query", "db.py"),
            mk_node(6, "close", "db.py"),
        ]
        edges = [
            # auth.py: 2 internal, 0 external  -> cohesion 1.0
            mk_edge(1, "auth.py::login", "auth.py::check_token", "auth.py"),
            mk_edge(2, "auth.py::logout", "auth.py::check_token", "auth.py"),
            # db.py: 2 internal, 1 external  -> cohesion 2/3 ≈ 0.6667
            mk_edge(3, "db.py::query", "db.py::connect", "db.py"),
            mk_edge(4, "db.py::close", "db.py::connect", "db.py"),
            mk_edge(5, "db.py::close", "external.py::log", "db.py"),
        ]

        result = _detect_file_based(nodes, edges, min_size=2)

        assert len(result) == 2
        by_desc = {c["description"]: c for c in result}
        auth = by_desc["Directory-based community: auth"]
        db = by_desc["Directory-based community: db"]

        # Member sets — catches wrong member_qns being passed to batch helper
        assert set(auth["members"]) == {
            "auth.py::login", "auth.py::logout", "auth.py::check_token",
        }
        assert set(db["members"]) == {
            "db.py::connect", "db.py::query", "db.py::close",
        }

        # Cohesions are distinct — zip misalignment would swap these
        assert auth["cohesion"] == pytest.approx(1.0)
        assert db["cohesion"] == pytest.approx(0.6667)

        # Metadata passes through correctly
        assert auth["size"] == 3
        assert db["size"] == 3
        assert auth["dominant_language"] == "python"
        assert db["dominant_language"] == "python"
        assert auth["level"] == 0
        assert db["level"] == 0

    def test_detected_cohesions_match_direct_computation(self):
        """Every stored community cohesion must equal what _compute_cohesion
        produces when called directly on that community's member set and
        the full edge list.

        Algorithm-agnostic: runs against whichever path detect_communities
        takes (Leiden if igraph is available, file-based otherwise). Any
        regression in the batch-cohesion refactor that mis-aligns
        cohesions to communities would fail loudly here with specific
        community names.

        The fixture is deliberately broken out of symmetry (one extra
        internal edge in auth.py) so a swap between auth/db cohesions
        would be visible.
        """
        self._seed_two_clusters()
        # Break cohesion symmetry: add one extra internal edge in auth.py
        # so auth.py cohesion != db.py cohesion. Without this, the seeded
        # fixture has both communities at 2/3 and a zip misalignment
        # would be silent.
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="auth.py::login",
            target="auth.py::logout", file_path="auth.py", line=12,
        ))
        self.store.commit()

        communities = detect_communities(self.store, min_size=2)
        assert len(communities) > 0

        all_edges = self.store.get_all_edges()
        # Collect the distinct cohesion values we see, to guard against
        # the degenerate case where the fixture somehow produces all-equal
        # cohesions (which would make a swap undetectable).
        seen_cohesions: set[float] = set()
        for comm in communities:
            # Sub-communities (level=1) have cohesion computed against
            # a filtered sub-edge set, so skip them. The fixture is tiny
            # enough that no sub-communities are produced in practice.
            if comm.get("level", 0) != 0:
                continue
            member_qns = set(comm["members"])
            direct = round(_compute_cohesion(member_qns, all_edges), 4)
            assert comm["cohesion"] == direct, (
                f"Community {comm['name']!r} stored cohesion "
                f"{comm['cohesion']} but direct computation gives {direct}"
            )
            seen_cohesions.add(comm["cohesion"])

        # Sanity: the fixture produced communities with distinct cohesions,
        # so the equality check above actually guards against swaps.
        assert len(seen_cohesions) >= 2, (
            "Fixture regression: all detected communities have the same "
            "cohesion, which means a zip misalignment bug would not be "
            f"caught here. seen={seen_cohesions}"
        )

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

    def test_leiden_fallback_to_file_based(self):
        """When Leiden produces 0 communities (all < min_size), fall back to file-based."""
        # Seed nodes with only CONTAINS edges (no CALLS/IMPORTS -- sparse graph)
        self.store.upsert_node(
            NodeInfo(
                kind="File", name="a.py", file_path="a.py",
                line_start=1, line_end=100, language="python",
            ), file_hash="a1"
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function", name="f1", file_path="a.py",
                line_start=1, line_end=10, language="python",
                parent_name=None,
            ), file_hash="a1"
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function", name="f2", file_path="a.py",
                line_start=11, line_end=20, language="python",
                parent_name=None,
            ), file_hash="a1"
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function", name="f3", file_path="a.py",
                line_start=21, line_end=30, language="python",
                parent_name=None,
            ), file_hash="a1"
        )
        self.store.upsert_edge(
            EdgeInfo(kind="CONTAINS", source="a.py", target="a.py::f1",
                     file_path="a.py", line=1)
        )
        self.store.upsert_edge(
            EdgeInfo(kind="CONTAINS", source="a.py", target="a.py::f2",
                     file_path="a.py", line=11)
        )
        self.store.upsert_edge(
            EdgeInfo(kind="CONTAINS", source="a.py", target="a.py::f3",
                     file_path="a.py", line=21)
        )
        # With high min_size, Leiden may produce tiny clusters that get dropped.
        # The fallback to file-based should still produce results.
        result = detect_communities(self.store, min_size=2)
        assert isinstance(result, list)
        assert len(result) >= 1

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

    def test_community_capture_remap_survives_node_replacement(self, tmp_path):
        """Community IDs remapped by qualified_name after node ID churn (#569)."""
        from code_review_graph.communities import (
            capture_community_assignments,
            remap_community_assignments,
        )
        from code_review_graph.parser import NodeInfo

        abs_auth = str((tmp_path / "auth.py").resolve())
        login = NodeInfo(
            kind="Function",
            name="login",
            file_path=abs_auth,
            line_start=1,
            line_end=5,
            language="python",
        )
        logout = NodeInfo(
            kind="Function",
            name="logout",
            file_path=abs_auth,
            line_start=6,
            line_end=10,
            language="python",
        )
        old_login_id = self.store.upsert_node(login, file_hash="old")
        self.store.upsert_node(logout, file_hash="old")
        self.store.commit()
        self.store._conn.execute(
            "INSERT INTO communities (name, level, cohesion, size, "
            "dominant_language, description) VALUES (?, 0, 1.0, 2, 'python', '')",
            ("auth",),
        )
        cid = self.store._conn.execute(
            "SELECT id FROM communities WHERE name = 'auth'"
        ).fetchone()[0]
        self.store._conn.execute(
            "UPDATE nodes SET community_id = ? WHERE file_path = ?",
            (cid, abs_auth),
        )
        self.store.commit()

        mapping = capture_community_assignments(self.store, [abs_auth])
        assert mapping[f"{abs_auth}::login"] == cid

        self.store.remove_file_data(abs_auth)
        self.store.commit()
        new_login_id = self.store.upsert_node(login, file_hash="new")
        self.store.upsert_node(logout, file_hash="new")
        self.store.commit()
        assert new_login_id != old_login_id

        updated = remap_community_assignments(self.store, mapping)
        assert updated >= 1
        row = self.store._conn.execute(
            "SELECT community_id FROM nodes WHERE qualified_name = ?",
            (f"{abs_auth}::login",),
        ).fetchone()
        assert row["community_id"] == cid

        # force=True redetects even when relative path alone would miss abs paths
        count = incremental_detect_communities(
            self.store, ["auth.py"], repo_root=tmp_path, force=True,
        )
        assert count >= 0


class TestCommunityPrReconciliation:
    """Regression coverage retained from overlapping PRs #600/#603/#605."""

    def test_slug_splits_camel_case(self):
        assert communities_module._to_slug("AuthService") == "auth-service"

    def test_slug_truncates_at_a_word_boundary(self):
        assert (
            communities_module._to_slug(
                "SuperLongAuthenticationServiceManager"
            )
            == "super-long-authentication"
        )

    def test_mixed_community_name_uses_production_members(self):
        production = _community_node(
            1,
            "src/auth.py::authenticate_user",
            file_path="src/auth.py",
        )
        tests = [
            _community_node(
                2,
                "tests/test_auth.py::should_return_user",
                kind="Test",
                is_test=True,
                file_path="src/auth.py",
            ),
            _community_node(
                3,
                "tests/test_auth.py::expected_user_when_valid",
                kind="Test",
                is_test=True,
                file_path="src/auth.py",
            ),
        ]

        assert communities_module._generate_community_name(
            [production, *tests]
        ) == communities_module._generate_community_name([production])

    def test_pure_test_name_filters_bdd_noise(self):
        tests = [
            _community_node(
                1,
                "tests/test_auth.py::should_return_token",
                kind="Test",
                is_test=True,
            ),
            _community_node(
                2,
                "tests/test_auth.py::should_raise_error",
                kind="Test",
                is_test=True,
            ),
        ]

        name = communities_module._generate_community_name(tests)

        assert all(word not in name for word in ("should", "return", "raise"))

    def test_test_reassignment_counts_unique_subjects(self):
        nodes = {
            0: _community_node(1, "a.py::alpha"),
            1: _community_node(2, "b.py::bravo"),
            2: _community_node(3, "b.py::charlie"),
            3: _community_node(
                4,
                "tests/test_feature.py::test_feature",
                kind="Test",
                is_test=True,
            ),
        }
        qn_to_idx = {node.qualified_name: idx for idx, node in nodes.items()}
        test_qn = nodes[3].qualified_name
        edges = [
            _community_edge(1, nodes[0].qualified_name, test_qn, kind="TESTED_BY"),
            _community_edge(2, nodes[0].qualified_name, test_qn, kind="TESTED_BY"),
            _community_edge(3, test_qn, nodes[1].qualified_name, kind="TESTED_BY"),
            _community_edge(4, nodes[2].qualified_name, test_qn, kind="TESTED_BY"),
        ]

        reassigned = communities_module._reassign_test_nodes(
            [[0, 3], [1, 2]], nodes, qn_to_idx, edges
        )

        assert reassigned == [[0], [3, 1, 2]]

    def test_test_reassignment_keeps_current_cluster_on_a_tie(self):
        nodes = {
            0: _community_node(1, "a.py::alpha"),
            1: _community_node(2, "b.py::bravo"),
            2: _community_node(
                3,
                "tests/test_feature.py::test_feature",
                kind="Test",
                is_test=True,
            ),
        }
        qn_to_idx = {node.qualified_name: idx for idx, node in nodes.items()}
        edges = [
            _community_edge(
                1,
                nodes[0].qualified_name,
                nodes[2].qualified_name,
                kind="TESTED_BY",
            ),
            _community_edge(
                2,
                nodes[2].qualified_name,
                nodes[1].qualified_name,
                kind="TESTED_BY",
            ),
        ]

        reassigned = communities_module._reassign_test_nodes(
            [[0, 2], [1]], nodes, qn_to_idx, edges
        )

        assert reassigned == [[0, 2], [1]]

    def test_test_reassignment_is_independent_of_edge_order(self):
        nodes = {
            0: _community_node(
                1,
                "tests/test_feature.py::test_alpha",
                kind="Test",
                is_test=True,
            ),
            1: _community_node(
                2,
                "tests/test_feature.py::test_bravo",
                kind="Test",
                is_test=True,
            ),
            2: _community_node(3, "src/feature.py::subject"),
        }
        qn_to_idx = {node.qualified_name: idx for idx, node in nodes.items()}
        edges = [
            _community_edge(
                1,
                nodes[0].qualified_name,
                nodes[2].qualified_name,
                kind="TESTED_BY",
            ),
            _community_edge(
                2,
                nodes[1].qualified_name,
                nodes[2].qualified_name,
                kind="TESTED_BY",
            ),
        ]

        forward = communities_module._reassign_test_nodes(
            [[0, 1], [2]], nodes, qn_to_idx, edges
        )
        reverse = communities_module._reassign_test_nodes(
            [[0, 1], [2]], nodes, qn_to_idx, list(reversed(edges))
        )

        assert forward == reverse == [[], [0, 1, 2]]

    def test_duplicate_names_keep_largest_name_and_make_others_unique(self):
        communities = [
            {
                "id": 10,
                "name": "services-auth",
                "size": 3,
                "members": ["auth.py::login", "auth.py::logout", "auth.py::token"],
            },
            {
                "id": 11,
                "name": "services-auth",
                "size": 2,
                "members": ["billing.py::invoice", "billing.py::charge"],
            },
        ]
        nodes = [
            _community_node(1, "auth.py::login"),
            _community_node(2, "auth.py::logout"),
            _community_node(3, "auth.py::token"),
            _community_node(4, "billing.py::invoice"),
            _community_node(5, "billing.py::charge"),
        ]

        communities_module._dedupe_community_names(communities, nodes)

        assert communities[0]["name"] == "services-auth"
        assert communities[1]["name"].startswith("services-auth-")
        assert len({community["name"] for community in communities}) == 2

    def test_duplicate_name_suffix_ignores_test_vocabulary(self):
        communities = [
            {
                "id": 10,
                "name": "services",
                "size": 5,
                "members": [f"auth.py::auth_{index}" for index in range(5)],
            },
            {
                "id": 11,
                "name": "services",
                "size": 4,
                "members": [
                    "billing.py::invoice",
                    "tests/test_billing.py::mock_gateway_one",
                    "tests/test_billing.py::mock_gateway_two",
                    "tests/test_billing.py::mock_gateway_three",
                ],
            },
        ]
        nodes = [
            *[
                _community_node(index, f"auth.py::auth_{index}")
                for index in range(5)
            ],
            _community_node(6, "billing.py::invoice"),
            *[
                _community_node(
                    index + 7,
                    f"tests/test_billing.py::mock_gateway_{name}",
                    kind="Test",
                    is_test=True,
                )
                for index, name in enumerate(("one", "two", "three"))
            ],
        ]

        communities_module._dedupe_community_names(communities, nodes)

        assert communities[1]["name"] == "services-invoice"

    @pytest.mark.skipif(not IGRAPH_AVAILABLE, reason="igraph not installed")
    def test_oversized_split_uses_member_names_and_real_cohesion(self):
        nodes = [
            *[
                _community_node(i, f"services/auth.py::auth_{name}")
                for i, name in enumerate(("load", "save", "delete"), start=1)
            ],
            *[
                _community_node(i, f"services/billing.py::billing_{name}")
                for i, name in enumerate(("load", "save", "delete"), start=4)
            ],
        ]
        left = [node.qualified_name for node in nodes[:3]]
        right = [node.qualified_name for node in nodes[3:]]
        edges = [
            _community_edge(1, left[0], left[1]),
            _community_edge(2, left[0], left[2]),
            _community_edge(3, left[1], left[2]),
            _community_edge(4, right[0], right[1]),
            _community_edge(5, right[0], right[2]),
            _community_edge(6, right[1], right[2]),
            _community_edge(7, left[0], right[0]),
        ]
        parent = {
            "id": 7,
            "name": "services-parent",
            "level": 0,
            "size": 6,
            "members": [node.qualified_name for node in nodes],
            "dominant_language": "python",
        }

        split = communities_module._split_oversized(
            [parent], nodes, edges, threshold_pct=0.1, min_split_size=2
        )

        assert len(split) == 2
        assert any("auth" in community["name"] for community in split)
        assert any("billing" in community["name"] for community in split)
        assert {community["cohesion"] for community in split} == {0.75}

        duplicate_bridge_edges = [
            *edges,
            *[
                _community_edge(edge_id, left[0], right[0])
                for edge_id in range(8, 48)
            ],
        ]
        duplicate_split = communities_module._split_oversized(
            [parent],
            nodes,
            duplicate_bridge_edges,
            threshold_pct=0.1,
            min_split_size=2,
        )
        expected_partition = {
            frozenset(community["members"])
            for community in split
        }
        duplicate_partition = {
            frozenset(community["members"])
            for community in duplicate_split
        }

        assert duplicate_partition == expected_partition

    @pytest.mark.parametrize("bare_subjects", [False, True])
    @pytest.mark.skipif(not IGRAPH_AVAILABLE, reason="igraph not installed")
    def test_oversized_split_keeps_tests_with_their_subjects(
        self, bare_subjects: bool
    ):
        production = [
            _community_node(i, f"src/feature.py::feature_{i}")
            for i in range(1, 5)
        ]
        tests = [
            _community_node(
                i + 4,
                f"tests/test_feature.py::test_feature_{i}",
                kind="Test",
                is_test=True,
            )
            for i in range(1, 5)
        ]
        nodes = [*production, *tests]
        edges: list[GraphEdge] = []
        edge_id = 1
        for group in (production, tests):
            for left_idx, left_node in enumerate(group):
                for right_node in group[left_idx + 1:]:
                    edges.append(
                        _community_edge(
                            edge_id,
                            left_node.qualified_name,
                            right_node.qualified_name,
                        )
                    )
                    edge_id += 1
        for production_node, test_node in zip(production, tests):
            edges.append(
                _community_edge(
                    edge_id,
                    (
                        production_node.name
                        if bare_subjects
                        else production_node.qualified_name
                    ),
                    test_node.qualified_name,
                    kind="TESTED_BY",
                )
            )
            edge_id += 1
        parent = {
            "id": 8,
            "name": "feature-parent",
            "level": 0,
            "size": len(nodes),
            "members": [node.qualified_name for node in nodes],
            "dominant_language": "python",
        }

        split = communities_module._split_oversized(
            [parent], nodes, edges, threshold_pct=0.1, min_split_size=2
        )
        member_to_community = {
            member: index
            for index, community in enumerate(split)
            for member in community["members"]
        }

        for production_node, test_node in zip(production, tests):
            assert (
                member_to_community[production_node.qualified_name]
                == member_to_community[test_node.qualified_name]
            )
