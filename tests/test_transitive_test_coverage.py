"""Test-gap coverage reached through a caller (#1047).

The gate on promotion PR #1047 called ``main.py::_offload``, ``_run_off_loop``
and 71 other symbols "test gaps" while the suite executed every one of them.
TESTED_BY is a direct link from a test to a production symbol, so a private
helper that tests only reach by calling the public function above it has no
such edge and was reported as untested.

The fix walks *up* the CALLS graph for a tested caller, and keeps the answer
in its own class. These tests pin both halves: the false alarms stop, and a
symbol nothing reaches at any depth is still reported -- including the shape
that made the naive "any tested ancestor means covered" rule a net loss on
that very delta, a helper sitting under two well-tested callers whose own
body no test executes.
"""

import tempfile
from pathlib import Path

from code_review_graph.changes import analyze_changes
from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo


class _Fixture:
    """Shared graph-building helpers."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _add_func(
        self,
        name: str,
        path: str = "app.py",
        is_test: bool = False,
        line_start: int = 1,
        line_end: int = 10,
    ) -> str:
        node = NodeInfo(
            kind="Test" if is_test else "Function",
            name=name,
            file_path=path,
            line_start=line_start,
            line_end=line_end,
            language="python",
            parent_name=None,
            is_test=is_test,
            extra={},
        )
        self.store.upsert_node(node, file_hash="abc")
        self.store.commit()
        return f"{path}::{name}"

    def _add_call(self, source_qn: str, target_qn: str, extra: dict | None = None) -> None:
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS",
            source=source_qn,
            target=target_qn,
            file_path=source_qn.split("::", 1)[0],
            line=5,
            extra=extra or {},
        ))
        self.store.commit()

    def _add_tested_by(self, production_qn: str, test_qn: str) -> None:
        # TESTED_BY is stored source=production, target=test. See: #515
        self.store.upsert_edge(EdgeInfo(
            kind="TESTED_BY",
            source=production_qn,
            target=test_qn,
            file_path=test_qn.split("::", 1)[0],
            line=1,
        ))
        self.store.commit()

    def _analyze(self, path: str = "app.py", start: int = 1, end: int = 500) -> dict:
        return analyze_changes(
            self.store,
            changed_files=[path],
            changed_ranges={path: [(start, end)]},
        )

    def _gap(self, result: dict, name: str) -> dict:
        matches = [g for g in result["test_gaps"] if g["name"] == name]
        assert len(matches) == 1, (name, result["test_gaps"])
        return matches[0]


class TestGapClassification(_Fixture):
    def _chain(self, length: int) -> str:
        """``public`` (tested) -> mid1 -> ... -> ``helper``; return helper's qn.

        ``length`` is the number of CALLS hops between the tested caller and
        the helper, so length=1 means the tested function calls it directly.
        """
        public = self._add_func("public", line_start=1, line_end=10)
        test = self._add_func("test_public", path="test_app.py", is_test=True)
        self._add_tested_by(public, test)
        previous = public
        for i in range(1, length):
            mid = self._add_func(f"mid{i}", line_start=20 + i * 10, line_end=25 + i * 10)
            self._add_call(previous, mid)
            previous = mid
        helper = self._add_func("helper", line_start=200, line_end=210)
        self._add_call(previous, helper)
        return helper

    def test_direct_coverage_is_not_a_gap_at_all(self):
        """A symbol with its own TESTED_BY edge never reaches the report."""
        public = self._add_func("public")
        test = self._add_func("test_public", path="test_app.py", is_test=True)
        self._add_tested_by(public, test)

        result = self._analyze()
        assert result["test_gaps"] == []
        assert result["test_gaps_uncovered"] == 0
        assert result["test_gaps_indirect"] == 0

    def test_helper_under_a_tested_caller_is_indirect_not_untested(self):
        """The #1047 shape: ``_offload``, reached only via its caller."""
        self._chain(1)

        result = self._analyze()
        gap = self._gap(result, "helper")
        assert gap["coverage"] == "indirect"
        assert gap["covered_via"] == "app.py::public"
        assert gap["covered_depth"] == 1
        assert gap["covered_by"] == ["test_app.py::test_public"]
        assert result["test_gaps_indirect"] == 1

    def test_two_hops_still_counts_as_reached(self):
        """``_run_off_loop`` sits two CALLS hops below its tested caller."""
        self._chain(2)

        gap = self._gap(self._analyze(), "helper")
        assert gap["coverage"] == "indirect"
        assert gap["covered_depth"] == 2
        # ``via`` names the tested caller found at the end of the walk, not
        # the intermediate hop.
        assert gap["covered_via"] == "app.py::public"

    def test_three_hops_is_past_the_limit_and_stays_a_gap(self):
        """The limit has to bite somewhere, or every symbol reads as covered."""
        self._chain(3)

        gap = self._gap(self._analyze(), "helper")
        assert gap["coverage"] == "none"
        assert "covered_via" not in gap

    def test_symbol_with_no_caller_anywhere_is_still_reported(self):
        """The case the fix must never silence: nothing reaches it at all."""
        self._add_func("orphan", line_start=1, line_end=10)

        result = self._analyze()
        gap = self._gap(result, "orphan")
        assert gap["coverage"] == "none"
        assert result["test_gaps_uncovered"] == 1
        assert result["test_gaps_indirect"] == 0

    def test_indirect_coverage_never_removes_the_row(self):
        """The real gap this rule could have hidden.

        ``incremental.py::_get_svn_changed_files`` has two direct callers, both
        heavily tested, and zero of its own 29 statements executed. Suppressing
        a symbol because a tested caller reaches it would have hidden the one
        genuine gap on the delta while fixing six false alarms.
        """
        helper = self._add_func("svn_helper", line_start=200, line_end=229)
        for i, caller in enumerate(("get_changed_files", "get_staged_and_unstaged")):
            caller_qn = self._add_func(caller, line_start=1 + i * 20, line_end=10 + i * 20)
            self._add_call(caller_qn, helper)
            test_qn = self._add_func(f"test_{caller}", path="test_app.py", is_test=True)
            self._add_tested_by(caller_qn, test_qn)

        result = self._analyze()
        gap = self._gap(result, "svn_helper")
        assert gap["coverage"] == "indirect"
        assert len(result["test_gaps"]) == 1
        assert result["test_gaps_indirect"] == 1

    def test_gap_order_does_not_depend_on_the_coverage_class(self):
        """Truncation must not be able to delete one whole class.

        Sorting the reached ones last looks prudent until you notice every
        consumer bounds this list: at ``detect_changes_tool``'s default of 25
        rows a delta with 73 unreached gaps shipped zero reached ones while
        still reporting a count of them. A class announced in the counts and
        withheld from the payload is worse than no class at all.
        """
        # "orphan" sorts after the chain's helper but is declared first, so a
        # class-blind order is node order, not grouped-by-coverage order.
        self._add_func("orphan", line_start=300, line_end=310)
        self._chain(1)

        gaps = self._analyze()["test_gaps"]
        classes = [g["coverage"] for g in gaps]
        assert "indirect" in classes and "none" in classes
        # Node order, so the two classes interleave rather than grouping.
        assert classes == sorted(
            classes, key=lambda c: [g["coverage"] for g in gaps].index(c)
        )
        assert gaps[0]["name"] == "orphan"

    def test_an_untested_caller_does_not_launder_coverage_onto_its_callees(self):
        """A caller with no tests of its own passes nothing down."""
        untested_caller = self._add_func("caller", line_start=1, line_end=10)
        helper = self._add_func("helper", line_start=20, line_end=30)
        self._add_call(untested_caller, helper)

        result = self._analyze()
        assert {g["name"]: g["coverage"] for g in result["test_gaps"]} == {
            "caller": "none",
            "helper": "none",
        }

    def test_a_caller_inside_a_test_file_launders_nothing(self):
        """The larger laundering class: the "caller" is itself test code.

        A fixture in ``tests/`` that calls production code carries TESTED_BY
        edges pointing at tests in its own file -- 20% of this project's
        TESTED_BY edges start at a test-file symbol. Crediting through one
        lets a test fixture vouch for the code it sets up, and the route the
        report prints names a test file as the caller.
        """
        helper = self._add_func("helper", line_start=1, line_end=10)
        # is_test=0 on purpose: this is the real shape in the graph for a
        # module-level helper inside a test file.
        seeder = self._add_func(
            "_seed_callers", path="tests/test_x.py", line_start=1, line_end=10,
        )
        self._add_call(seeder, helper)
        self._add_tested_by(seeder, "tests/test_x.py::test_thing")

        gaps = {g["name"]: g for g in self._analyze()["test_gaps"]}
        assert gaps["helper"]["coverage"] == "none"
        assert "covered_via" not in gaps["helper"]


class TestSummaryText(_Fixture):
    def test_summary_separates_the_two_claims(self):
        public = self._add_func("public", line_start=1, line_end=10)
        test = self._add_func("test_public", path="test_app.py", is_test=True)
        self._add_tested_by(public, test)
        helper = self._add_func("helper", line_start=20, line_end=30)
        self._add_call(public, helper)
        self._add_func("orphan", line_start=40, line_end=50)

        summary = self._analyze()["summary"]
        # "no tested caller found", not "no test in reach": the graph reads
        # its own edges, not the test suite. A test reaching production code
        # through importlib or a subprocess leaves no edge behind, and two
        # such symbols sit in this list on the delta of #1047.
        assert (
            "  - 2 test gap(s) (1 with no tested caller found, "
            "1 reached only through a caller)"
        ) in summary
        untested_line = next(
            line for line in summary.splitlines() if "Untested:" in line
        )
        assert "orphan" in untested_line
        assert "helper" not in untested_line
        indirect_line = next(
            line for line in summary.splitlines()
            if "Reached only through a caller:" in line
        )
        assert "helper" in indirect_line

    def test_summary_is_unchanged_when_nothing_is_indirect(self):
        """No breakdown clause and no extra line when the split is trivial."""
        self._add_func("orphan", line_start=1, line_end=10)

        summary = self._analyze()["summary"]
        assert "  - 1 test gap(s)\n" in summary + "\n"
        assert "reached only through a caller" not in summary
        assert "Reached only through a caller:" not in summary


class TestWalkSafety(_Fixture):
    def test_bare_caller_names_are_not_credited(self):
        """An unresolved caller has no identity, so it lends no coverage."""
        helper = self._add_func("helper", line_start=20, line_end=30)
        self._add_call("helper_caller", helper)
        self._add_tested_by("helper_caller", "test_app.py::test_thing")

        assert self.store.get_caller_test_routes([helper]) == {}

    def test_ambiguous_call_edges_are_not_credited(self):
        """An edge naming several candidates is not a fact about any one."""
        public = self._add_func("public", line_start=1, line_end=10)
        test = self._add_func("test_public", path="test_app.py", is_test=True)
        self._add_tested_by(public, test)
        helper = self._add_func("helper", line_start=20, line_end=30)
        self._add_call(public, helper, extra={"ambiguous_targets": ["a", "b"]})

        assert self.store.get_caller_test_routes([helper]) == {}

    def test_call_cycles_terminate(self):
        """Mutual recursion with no test anywhere must not loop."""
        a = self._add_func("a", line_start=1, line_end=10)
        b = self._add_func("b", line_start=20, line_end=30)
        self._add_call(a, b)
        self._add_call(b, a)

        assert self.store.get_caller_test_routes([a, b]) == {}
        result = self._analyze()
        assert {g["coverage"] for g in result["test_gaps"]} == {"none"}

    def test_depth_zero_walks_nothing(self):
        public = self._add_func("public", line_start=1, line_end=10)
        test = self._add_func("test_public", path="test_app.py", is_test=True)
        self._add_tested_by(public, test)
        helper = self._add_func("helper", line_start=20, line_end=30)
        self._add_call(public, helper)

        assert self.store.get_caller_test_routes([helper], max_depth=0) == {}
        assert self.store.get_caller_test_routes([helper], max_depth=1) != {}

    def test_a_hub_is_not_expanded(self):
        """A symbol with more callers than the limit is skipped, not scanned.

        "One of my 1,800 callers has a test" is no evidence about this symbol,
        and expanding a hub is what makes the walk expensive.
        """
        helper = self._add_func("helper", line_start=1, line_end=5)
        for i in range(10):
            caller = self._add_func(f"c{i:02d}", line_start=10 + i * 10, line_end=15 + i * 10)
            self._add_call(caller, helper)
        self._add_tested_by("app.py::c09", "test_app.py::test_c09")

        assert self.store.get_caller_test_routes(
            [helper], max_callers_per_node=9
        ) == {}
        assert self.store.get_caller_test_routes(
            [helper], max_callers_per_node=10
        ) != {}

    def test_a_hub_in_the_change_set_does_not_erase_other_symbols_routes(self):
        """The limit is per node, never a budget shared across the inputs.

        A shared budget was truncated by sorting caller names and keeping the
        first N, so one hub in a pull request deleted the routes of every
        other changed symbol -- on a real graph, 206 of them -- and which
        symbols survived depended on where in the tree their callers lived.
        That is exactly the false alarm this walk exists to remove.
        """
        hub = self._add_func("hub", line_start=1, line_end=5)
        for i in range(40):
            caller = self._add_func(
                f"h{i:03d}", line_start=100 + i * 10, line_end=105 + i * 10,
            )
            self._add_call(caller, hub)

        ordinary = self._add_func("ordinary", line_start=2000, line_end=2005)
        tested_caller = self._add_func("public", line_start=2100, line_end=2105)
        self._add_call(tested_caller, ordinary)
        self._add_tested_by(tested_caller, "test_app.py::test_public")

        alone = self.store.get_caller_test_routes(
            [ordinary], max_callers_per_node=10,
        )
        with_hub = self.store.get_caller_test_routes(
            [hub, ordinary], max_callers_per_node=10,
        )
        assert ordinary in alone
        assert with_hub == alone

    def test_batching_survives_more_seeds_than_one_sql_batch(self):
        """450 is the per-query bind limit; the walk must chunk past it."""
        public = self._add_func("public", line_start=1, line_end=5)
        self._add_tested_by(public, "test_app.py::test_public")
        seeds = []
        for i in range(600):
            helper = self._add_func(f"h{i:04d}", line_start=10 + i * 10, line_end=15 + i * 10)
            self._add_call(public, helper)
            seeds.append(helper)

        routes = self.store.get_caller_test_routes(seeds)
        assert len(routes) == 600
        assert all(r["depth"] == 1 for r in routes.values())


class TestRiskScoring(_Fixture):
    def test_being_reached_by_a_tested_caller_buys_no_risk_discount(self):
        """Reachability is not coverage, so it must not move the score.

        A 0.03 credit was tried and was wrong twice. Evidentially: a static
        CALLS path shows the symbol is reachable, not that a test runs it --
        about 6% of the symbols the walk reaches are never executed by this
        project's own suite. Mechanically: every other term moves in steps of
        0.05 and real scores quantize hard, so 0.03 could never leave a symbol
        tied -- it dropped it below its whole tie class, and
        ``review_priorities`` is ``sorted(...)[:10]``. On the delta of #1047
        that evicted the one genuinely untested symbol from the Action's table.
        """
        helper = self._add_func("helper", line_start=1, line_end=10)
        public = self._add_func("public", line_start=20, line_end=30)
        self._add_call(public, helper)
        self._add_tested_by(public, "test_app.py::test_public")

        # Control: identical shape (one caller), except that caller has no
        # tests. The only difference from ``helper`` is the tested caller, so
        # any score gap is the indirect credit and nothing else.
        control = self._add_func("control", line_start=100, line_end=110)
        untested_caller = self._add_func("wrapper", line_start=120, line_end=130)
        self._add_call(untested_caller, control)

        result = self._analyze()
        scores = {n["name"]: n["risk_score"] for n in result["changed_functions"]}
        gaps = {g["name"]: g for g in result["test_gaps"]}

        # Classified as reached, and still scored as fully untested.
        assert gaps["helper"]["coverage"] == "indirect"
        assert gaps["control"]["coverage"] == "none"
        assert scores["helper"] == scores["control"]
        # And it still outranks the symbol that does have a test.
        assert scores["helper"] > scores["public"]

    def test_an_indirect_symbol_keeps_its_place_among_equal_scores(self):
        """The blocker this replaced: a 0.03 nudge is a guaranteed rank drop.

        Scores tie heavily in practice, and ``review_priorities`` keeps the
        top 10. A symbol demoted below its tie class falls off the only list
        the PR comment renders, so a genuine gap disappears from the report a
        maintainer actually reads.
        """
        helper = self._add_func("helper", line_start=1, line_end=10)
        public = self._add_func("public", line_start=20, line_end=30)
        self._add_call(public, helper)
        self._add_tested_by(public, "test_app.py::test_public")
        # Peers share ``helper``'s shape -- one untested caller each -- so they
        # land on the same score and form the tie group the credit used to
        # push ``helper`` out of.
        for i in range(12):
            peer = self._add_func(
                f"peer{i:02d}", line_start=400 + i * 20, line_end=405 + i * 20,
            )
            caller = self._add_func(
                f"peercaller{i:02d}", line_start=410 + i * 20, line_end=415 + i * 20,
            )
            self._add_call(caller, peer)

        result = self._analyze()
        priorities = result["review_priorities"]
        assert "helper" in [p["name"] for p in priorities]
        helper_score = next(p["risk_score"] for p in priorities if p["name"] == "helper")
        peer_score = next(p["risk_score"] for p in priorities if p["name"].startswith("peer"))
        assert helper_score == peer_score


class TestReviewGuidanceAgrees(_Fixture):
    """The two tools CLAUDE.md sends a reviewer through must not disagree.

    ``get_review_context`` built its untested list from direct TESTED_BY edges
    only and printed "lack test coverage" -- the absolute wording -- about the
    same symbols ``detect_changes`` was, in the same session, reporting as
    reached through a caller. The reviewer got two answers and no way to pick.
    """

    def test_guidance_splits_the_same_way_detect_changes_does(self):
        from code_review_graph.tools.review import _generate_review_guidance

        helper = self._add_func("helper", line_start=1, line_end=10)
        public = self._add_func("public", line_start=20, line_end=30)
        self._add_call(public, helper)
        self._add_tested_by(public, "test_app.py::test_public")
        orphan = self._add_func("orphan", line_start=40, line_end=50)

        impact = {
            "changed_nodes": [
                self.store.get_node(helper),
                self.store.get_node(orphan),
            ],
            "edges": [],
            "impacted_nodes": [],
            "impacted_files": [],
        }
        guidance = _generate_review_guidance(
            impact, ["app.py"], None, self.store,
        )

        assert "lack test coverage" not in guidance
        assert "have no direct test" in guidance
        indirect_line = next(
            line for line in guidance.splitlines()
            if "reached only through a caller" in line
        )
        assert "helper" in indirect_line
        assert "orphan" not in indirect_line
        unreached_line = next(
            line for line in guidance.splitlines()
            if "no tested caller found" in line
        )
        assert "orphan" in unreached_line

    def test_guidance_without_a_store_still_avoids_the_absolute_claim(self):
        from code_review_graph.tools.review import _generate_review_guidance

        orphan = self._add_func("orphan", line_start=40, line_end=50)
        impact = {
            "changed_nodes": [self.store.get_node(orphan)],
            "edges": [],
            "impacted_nodes": [],
            "impacted_files": [],
        }
        guidance = _generate_review_guidance(impact, ["app.py"])
        assert "have no direct test" in guidance
        assert "lack test coverage" not in guidance
