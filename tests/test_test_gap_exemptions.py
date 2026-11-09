"""Edge-case tests for the test-gap exemption list (#850, PR #854).

Stresses _TEST_GAP_EXEMPT_NAMES beyond the PR's own coverage: that the
exemption cannot fully hide an untested class (the enclosing Class node
still surfaces), that it is exact-match only (no substring or case
leakage), that summary counts stay consistent with the filtered list,
and that risk scoring still sees the exempt nodes.
"""

import tempfile
from pathlib import Path

from code_review_graph.changes import _TEST_GAP_EXEMPT_NAMES, analyze_changes
from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo


class TestExemptListEdges:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _add_node(
        self,
        name: str,
        kind: str = "Function",
        path: str = "app.py",
        parent: str | None = None,
        line_start: int = 1,
        line_end: int = 10,
    ) -> None:
        node = NodeInfo(
            kind=kind,
            name=name,
            file_path=path,
            line_start=line_start,
            line_end=line_end,
            language="python",
            parent_name=parent,
            is_test=False,
            extra={},
        )
        self.store.upsert_node(node, file_hash="abc")
        self.store.commit()

    def _analyze(self, path: str, start: int, end: int) -> dict:
        return analyze_changes(
            self.store,
            changed_files=[path],
            changed_ranges={path: [(start, end)]},
        )

    def test_untested_class_still_surfaces_when_only_init_changes(self):
        """Exempting __init__ must not hide a completely untested class.

        A diff touching only the constructor also overlaps the enclosing
        Class node, which is not exempt, so the class-level gap remains.
        """
        self._add_node("Foo", kind="Class", path="foo.py",
                       line_start=1, line_end=20)
        self._add_node("__init__", parent="Foo", path="foo.py",
                       line_start=2, line_end=5)

        result = self._analyze("foo.py", 3, 4)
        gap_names = {g["name"] for g in result["test_gaps"]}
        assert "__init__" not in gap_names
        assert "Foo" in gap_names

    def test_exemption_is_exact_match_no_substring_or_case_leakage(self):
        """Only the exact names are exempt; near-misses stay flagged."""
        for i, name in enumerate(
            ["setUpX", "mysetUp", "__CONSTRUCT", "Setup_Method",
             "__init__x", "teardown"]
        ):
            self._add_node(name, path="near.py",
                           line_start=i * 10 + 1, line_end=i * 10 + 5)

        result = self._analyze("near.py", 1, 60)
        gap_names = {g["name"] for g in result["test_gaps"]}
        assert gap_names == {
            "setUpX", "mysetUp", "__CONSTRUCT", "Setup_Method",
            "__init__x", "teardown",
        }

    def test_summary_count_matches_filtered_gap_list(self):
        """The 'N test gap(s)' line counts the post-exemption list."""
        exempt = ["setUp", "tearDown", "__construct"]
        real = ["alpha", "beta"]
        for i, name in enumerate(exempt + real):
            self._add_node(name, path="mix.php",
                           line_start=i * 10 + 1, line_end=i * 10 + 5)

        result = self._analyze("mix.php", 1, 50)
        assert len(result["test_gaps"]) == 2
        assert "  - 2 test gap(s)" in result["summary"]
        assert "Untested: " in result["summary"]
        untested_line = next(
            line for line in result["summary"].splitlines()
            if "Untested:" in line
        )
        for name in exempt:
            assert name not in untested_line
        for name in real:
            assert name in untested_line

    def test_all_gaps_exempt_gives_zero_and_no_untested_line(self):
        """A diff of only lifecycle methods reports zero gaps cleanly."""
        for i, name in enumerate(sorted(_TEST_GAP_EXEMPT_NAMES)):
            self._add_node(name, path="life.py",
                           line_start=i * 10 + 1, line_end=i * 10 + 5)

        result = self._analyze("life.py", 1, len(_TEST_GAP_EXEMPT_NAMES) * 10)
        assert result["test_gaps"] == []
        assert "  - 0 test gap(s)" in result["summary"]
        assert "Untested:" not in result["summary"]

    def test_exempt_nodes_still_counted_as_changed_and_risk_scored(self):
        """Exemption only affects the gap list, not changed_functions."""
        self._add_node("setUp", path="only.py", line_start=1, line_end=5)

        result = self._analyze("only.py", 1, 5)
        changed_names = {n["name"] for n in result["changed_functions"]}
        assert "setUp" in changed_names
        assert result["risk_score"] > 0.0
        assert "  - 1 changed function(s)/class(es)" in result["summary"]

    def test_exempt_name_with_existing_coverage_stays_out(self):
        """A covered constructor is not double-reported either way."""
        self._add_node("__construct", path="cov.php", line_start=1, line_end=5)
        # TESTED_BY: source=production, target=test (see #515).
        qn = self.store.get_nodes_by_file("cov.php")[0].qualified_name
        self.store.upsert_edge(EdgeInfo(
            kind="TESTED_BY", source=qn, target="tests::t_construct",
            file_path="cov.php", line=1,
        ))
        self.store.commit()

        result = self._analyze("cov.php", 1, 5)
        assert result["test_gaps"] == []

    def test_unicode_and_whitespace_names_not_exempt(self):
        """Odd names never match the frozenset accidentally."""
        for i, name in enumerate(["初始化", " setUp", "setUp "]):
            self._add_node(name, path="uni.py",
                           line_start=i * 10 + 1, line_end=i * 10 + 5)

        result = self._analyze("uni.py", 1, 30)
        assert len(result["test_gaps"]) == 3
