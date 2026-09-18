"""Call-site provenance and CALLS target-resolution certainty.

Two contracts are pinned here.

**Call sites.** Every edge in the graph carries a file and a line, but
``query_graph`` used to return caller nodes in ``results`` and edges in a
separate ``edges`` list with no key joining the two. A reading agent could
not say where a call happens without opening the file. ``callers_of``,
``callees_of`` and ``references_to`` now attach the call site to the row
itself, and a caller that calls the target three times produces three rows
rather than one collapsed row.

**Resolution certainty.** A CALLS edge either points at an indexed node
(``direct``) or carries a bare name that the read path can only match by
name, subject to a receiver-evidence check (``unresolved``). That fact is
now a stored, indexed column on ``edges`` so an answer can be filtered and
split by it.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.migrations import LATEST_VERSION, get_schema_version
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.tools import get_impact_radius, list_graph_stats, query_graph


@pytest.fixture()
def repo(tmp_path: Path) -> dict:
    """A small graph with repeated call sites and one unresolved caller."""
    root = tmp_path
    (root / ".code-review-graph").mkdir()
    db_path = root / ".code-review-graph" / "graph.db"

    app = (root / "app.py").as_posix()
    svc = (root / "svc.py").as_posix()
    other = (root / "other.py").as_posix()

    with GraphStore(db_path) as store:
        for path in (app, svc, other):
            store.upsert_node(NodeInfo(
                kind="File", name=path, file_path=path,
                line_start=1, line_end=80, language="python",
            ))
        store.upsert_node(NodeInfo(
            kind="Function", name="handler", file_path=svc,
            line_start=10, line_end=20, language="python",
        ))
        store.upsert_node(NodeInfo(
            kind="Function", name="caller_one", file_path=app,
            line_start=5, line_end=30, language="python",
        ))
        store.upsert_node(NodeInfo(
            kind="Function", name="caller_two", file_path=other,
            line_start=3, line_end=9, language="python",
        ))
        store.upsert_node(NodeInfo(
            kind="Function", name="bare_caller", file_path=other,
            line_start=40, line_end=48, language="python",
        ))
        store.upsert_node(NodeInfo(
            kind="Function", name="leaf", file_path=svc,
            line_start=60, line_end=66, language="python",
        ))
        # Two hops from the changed file, so it has no single call site.
        store.upsert_node(NodeInfo(
            kind="Function", name="outer", file_path=app,
            line_start=50, line_end=58, language="python",
        ))
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=f"{app}::outer",
            target=f"{other}::caller_two", file_path=app, line=52,
        ))

        # caller_one calls handler three times: three call sites, one caller.
        for line in (11, 19, 27):
            store.upsert_edge(EdgeInfo(
                kind="CALLS", source=f"{app}::caller_one",
                target=f"{svc}::handler", file_path=app, line=line,
            ))
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=f"{other}::caller_two",
            target=f"{svc}::handler", file_path=other, line=6,
        ))
        # A bare target: only a name match, so the answer is not certain.
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=f"{other}::bare_caller",
            target="handler", file_path=other, line=44,
        ))
        # handler -> leaf, twice, for callees_of.
        for line in (13, 17):
            store.upsert_edge(EdgeInfo(
                kind="CALLS", source=f"{svc}::handler",
                target=f"{svc}::leaf", file_path=svc, line=line,
            ))
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=f"{svc}::handler",
            target="missing_helper", file_path=svc, line=18,
        ))
        # References, one resolved and one bare.
        store.upsert_edge(EdgeInfo(
            kind="REFERENCES", source=f"{app}::caller_one",
            target=f"{svc}::handler", file_path=app, line=29,
        ))
        store.commit()
        store.refresh_target_resolution()

    return {"root": str(root), "app": app, "svc": svc, "other": other}


# ---------------------------------------------------------------------------
# Change 1: call sites on the row
# ---------------------------------------------------------------------------


class TestCallSitesOnRows:
    def test_callers_of_returns_one_row_per_call_site_with_file_and_line(self, repo):
        result = query_graph(
            pattern="callers_of",
            target=f"{repo['svc']}::handler",
            repo_root=repo["root"],
        )

        assert result["status"] == "ok"
        sites = [
            (
                r["name"],
                r["call_site"].get("file", r["file_path"]),
                r["call_site"]["line"],
            )
            for r in result["results"]
        ]
        assert ("caller_one", repo["app"], 11) in sites
        assert ("caller_one", repo["app"], 19) in sites
        assert ("caller_one", repo["app"], 27) in sites
        assert ("caller_two", repo["other"], 6) in sites
        assert ("bare_caller", repo["other"], 44) in sites
        # Five call sites, three distinct callers: the repeats are not collapsed.
        assert result["result_count"] == 5
        assert result["distinct_nodes"] == 3

    def test_distinct_callers_come_before_a_caller_second_call_site(self, repo):
        """A caller that calls 3x must not crowd other callers out of the window."""
        result = query_graph(
            pattern="callers_of",
            target=f"{repo['svc']}::handler",
            repo_root=repo["root"],
            max_results=3,
        )
        assert {r["name"] for r in result["results"]} == {
            "caller_one", "caller_two", "bare_caller",
        }
        assert result["result_count"] == 5
        assert result["results_omitted"] == 2

    @pytest.mark.parametrize("detail_level", ["standard", "minimal"])
    def test_a_redundant_call_file_is_omitted(self, repo, detail_level):
        result = query_graph(
            pattern="callers_of",
            target=f"{repo['svc']}::handler",
            repo_root=repo["root"],
            detail_level=detail_level,
        )
        rows = {(r["name"], r["call_site"]["line"]): r for r in result["results"]}
        assert rows[("caller_one", 11)]["file_path"] == repo["app"]
        # The call is written in the caller's own file, which the row already
        # names, so the response does not pay for a second copy of the path.
        assert "file" not in rows[("caller_one", 11)]["call_site"]

    @pytest.mark.parametrize("detail_level", ["standard", "minimal"])
    def test_the_call_file_is_kept_when_the_call_is_elsewhere(
        self, repo, detail_level,
    ):
        """A call written outside the caller's own file must still be locatable."""
        with GraphStore(Path(repo["root"]) / ".code-review-graph" / "graph.db") as store:
            store.upsert_edge(EdgeInfo(
                kind="CALLS", source=f"{repo['other']}::caller_two",
                target=f"{repo['svc']}::handler",
                file_path=repo["svc"], line=71,
            ))
            store.commit()
            store.refresh_target_resolution()

        result = query_graph(
            pattern="callers_of",
            target=f"{repo['svc']}::handler",
            repo_root=repo["root"],
            detail_level=detail_level,
            max_results=100,
        )
        elsewhere = [
            r for r in result["results"] if r["call_site"].get("file") == repo["svc"]
        ]
        assert elsewhere and elsewhere[0]["call_site"]["line"] == 71

    def test_callees_of_returns_call_sites(self, repo):
        result = query_graph(
            pattern="callees_of",
            target=f"{repo['svc']}::handler",
            repo_root=repo["root"],
        )
        sites = sorted(
            (r["name"], r["call_site"]["line"]) for r in result["results"]
        )
        assert sites == [("leaf", 13), ("leaf", 17), ("missing_helper", 18)]
        assert result["distinct_nodes"] == 2

    def test_references_to_returns_call_sites(self, repo):
        result = query_graph(
            pattern="references_to",
            target=f"{repo['svc']}::handler",
            repo_root=repo["root"],
        )
        assert [
            (r["name"], r["call_site"]["line"]) for r in result["results"]
        ] == [("caller_one", 29)]

    def test_patterns_without_a_call_site_are_unchanged(self, repo):
        result = query_graph(
            pattern="children_of", target=repo["svc"], repo_root=repo["root"],
        )
        assert result["status"] == "ok"
        assert all("call_site" not in r for r in result["results"])
        assert "resolution_split" not in result

    def test_impact_radius_attaches_the_call_site_into_changed_code(self, repo):
        result = get_impact_radius(
            changed_files=[repo["svc"]], repo_root=repo["root"], max_depth=2,
        )
        by_name = {n["name"]: n for n in result["impacted_nodes"]}
        assert by_name["caller_one"]["file_path"] == repo["app"]
        # Same omit-the-redundant-path rule as the query patterns.
        assert by_name["caller_one"]["call_site"] == {"line": 11}
        assert by_name["caller_one"]["call_site_count"] == 4
        assert by_name["caller_two"]["call_site"] == {"line": 6}
        # A node reached only through another node has no single call site.
        assert "call_site" not in by_name["outer"]


# ---------------------------------------------------------------------------
# Change 2: stored target-resolution certainty
# ---------------------------------------------------------------------------


class TestTargetResolutionColumn:
    def test_column_is_created_indexed_and_backfilled_by_migration(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            store = GraphStore(tmp.name)
            conn = store._conn
            conn.execute("DROP INDEX IF EXISTS idx_edges_kind_target_resolution")
            conn.execute("ALTER TABLE edges DROP COLUMN target_resolution")
            conn.execute(
                "INSERT INTO nodes "
                "(kind, name, qualified_name, file_path, language, updated_at) "
                "VALUES ('Function', 'run', 'src/app.py::run', 'src/app.py', "
                "'python', 0)"
            )
            conn.executemany(
                "INSERT INTO edges "
                "(kind, source_qualified, target_qualified, file_path, line, "
                "updated_at) VALUES (?, ?, ?, 'src/app.py', 1, 0)",
                [
                    ("CALLS", "src/app.py::main", "src/app.py::run"),
                    ("CALLS", "src/app.py::main", "run"),
                    ("IMPORTS_FROM", "src/app.py", "src/lib.py"),
                ],
            )
            conn.execute(
                "UPDATE metadata SET value = '10' WHERE key = 'schema_version'"
            )
            store.commit()
            store.close()

            store = GraphStore(tmp.name)
            try:
                assert get_schema_version(store._conn) == LATEST_VERSION
                rows = dict(store._conn.execute(
                    "SELECT target_qualified, target_resolution FROM edges"
                ).fetchall())
                assert rows["src/app.py::run"] == "direct"
                assert rows["run"] == "unresolved"
                # The column means nothing for an import edge; it stays NULL
                # rather than claiming a file path is an unresolved call.
                assert rows["src/lib.py"] is None
                indexes = {
                    row[0] for row in store._conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index' "
                        "AND tbl_name = 'edges'"
                    )
                }
                assert "idx_edges_kind_target_resolution" in indexes
            finally:
                store.close()
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    def test_refresh_reclassifies_a_target_a_resolver_bound(self, repo):
        """A resolver pass rewrites a bare target; the column must follow it."""
        db_path = Path(repo["root"]) / ".code-review-graph" / "graph.db"
        resolved_qn = f"{repo['svc']}::missing_helper"
        with GraphStore(db_path) as store:
            assert store._conn.execute(
                "SELECT target_resolution FROM edges WHERE target_qualified = ?",
                ("missing_helper",),
            ).fetchone()[0] == "unresolved"
            store.upsert_node(NodeInfo(
                kind="Function", name="missing_helper", file_path=repo["svc"],
                line_start=70, line_end=74, language="python",
            ))
            # A node of that name alone proves nothing: the bare target stays
            # a guess until a resolver actually binds it.
            store.commit()
            store.refresh_target_resolution()
            assert store._conn.execute(
                "SELECT target_resolution FROM edges WHERE target_qualified = ?",
                ("missing_helper",),
            ).fetchone()[0] == "unresolved"

            store._conn.execute(
                "UPDATE edges SET target_qualified = ? WHERE target_qualified = ?",
                (resolved_qn, "missing_helper"),
            )
            store.commit()
            store.refresh_target_resolution()
            assert store._conn.execute(
                "SELECT target_resolution FROM edges WHERE target_qualified = ?",
                (resolved_qn,),
            ).fetchone()[0] == "direct"

    def test_counts_are_correct_before_the_column_is_populated(self, repo):
        """A graph built before the column existed must still split correctly,
        and a half-classified one must not double-count or under-count."""
        db_path = Path(repo["root"]) / ".code-review-graph" / "graph.db"
        with GraphStore(db_path) as store:
            classified = store.count_edges_by_resolution("CALLS")
            store._conn.execute("UPDATE edges SET target_resolution = NULL")
            store.commit()
            assert store.count_edges_by_resolution("CALLS") == classified

            # Half classified, half not: edges written since the last refresh.
            store._conn.execute(
                "UPDATE edges SET target_resolution = 'unresolved' "
                "WHERE target_qualified NOT LIKE '%::%'"
            )
            store.commit()
            assert store.count_edges_by_resolution("CALLS") == classified
        assert classified == {"direct": 7, "unresolved": 2}


class TestResolutionInAnswers:
    def test_callers_of_reports_the_split(self, repo):
        result = query_graph(
            pattern="callers_of",
            target=f"{repo['svc']}::handler",
            repo_root=repo["root"],
        )
        assert result["resolution_split"] == {"direct": 4, "unresolved": 1}

    def test_callers_of_can_return_only_certain_rows(self, repo):
        result = query_graph(
            pattern="callers_of",
            target=f"{repo['svc']}::handler",
            repo_root=repo["root"],
            resolution="direct",
        )
        assert {r["name"] for r in result["results"]} == {"caller_one", "caller_two"}
        assert result["result_count"] == 4
        assert result["resolution_split"] == {"direct": 4, "unresolved": 0}

    def test_callers_of_can_return_only_guessed_rows(self, repo):
        result = query_graph(
            pattern="callers_of",
            target=f"{repo['svc']}::handler",
            repo_root=repo["root"],
            resolution="unresolved",
        )
        assert [r["name"] for r in result["results"]] == ["bare_caller"]
        assert all(r["target_resolution"] == "unresolved" for r in result["results"])

    def test_minimal_mode_reports_the_split(self, repo):
        result = query_graph(
            pattern="callers_of",
            target=f"{repo['svc']}::handler",
            repo_root=repo["root"],
            detail_level="minimal",
        )
        assert result["resolution_split"] == {"direct": 4, "unresolved": 1}

    def test_an_unknown_resolution_is_rejected(self, repo):
        with pytest.raises(ValueError):
            query_graph(
                pattern="callers_of",
                target=f"{repo['svc']}::handler",
                repo_root=repo["root"],
                resolution="probably",
            )

    def test_impact_radius_counts_the_call_sites_it_cannot_reach(self, repo):
        """A bare target cannot join a qualified seed, so the traversal never
        reaches ``bare_caller``. Silence there reads as proof of absence; the
        count is what says the radius has a blind spot."""
        result = get_impact_radius(
            changed_files=[repo["svc"]], repo_root=repo["root"],
        )
        assert "bare_caller" not in {n["name"] for n in result["impacted_nodes"]}
        assert result["unresolved_call_sites"] == 1

    def test_impact_radius_direct_mode_leaves_the_answer_intact(self, repo):
        """The guard must drop no proven hop: an unresolved edge was already
        unreachable, so ``direct`` is a guarantee, not a narrowing."""
        every = get_impact_radius(
            changed_files=[repo["svc"]], repo_root=repo["root"],
        )
        direct = get_impact_radius(
            changed_files=[repo["svc"]], repo_root=repo["root"],
            resolution="direct",
        )
        assert direct["resolution"] == "direct"
        assert (
            [n["qualified_name"] for n in direct["impacted_nodes"]]
            == [n["qualified_name"] for n in every["impacted_nodes"]]
        )
        assert all(
            e.get("target_resolution") != "unresolved" for e in direct["edges"]
        )

    def test_impact_radius_minimal_mode_reports_the_blind_spot(self, repo):
        minimal = get_impact_radius(
            changed_files=[repo["svc"]], repo_root=repo["root"],
            detail_level="minimal",
        )
        assert minimal["unresolved_call_sites"] == 1

    def test_an_unknown_impact_resolution_is_rejected(self, repo):
        with pytest.raises(ValueError):
            get_impact_radius(
                changed_files=[repo["svc"]], repo_root=repo["root"],
                resolution="unresolved",
            )

    def test_graph_stats_reports_the_repository_wide_split(self, repo):
        stats = list_graph_stats(repo_root=repo["root"])
        assert stats["calls_by_resolution"] == {"direct": 7, "unresolved": 2}


class TestFilteringUsesTheIndex:
    def test_the_split_query_can_be_served_by_the_index(self, repo):
        """``INDEXED BY`` fails to prepare unless the index really covers the
        predicate, so this holds whatever plan SQLite picks for a table this
        small."""
        db_path = Path(repo["root"]) / ".code-review-graph" / "graph.db"
        with GraphStore(db_path) as store:
            count = store._conn.execute(
                "SELECT COUNT(*) FROM edges "
                "INDEXED BY idx_edges_kind_target_resolution "
                "WHERE kind = 'CALLS' AND target_resolution = 'direct'"
            ).fetchone()[0]
        assert count == 7


def test_build_populates_the_column(tmp_path: Path):
    """A real build must leave no CALLS edge unclassified."""
    from code_review_graph.incremental import full_build

    root = tmp_path
    (root / ".code-review-graph").mkdir()
    (root / "lib.py").write_text(
        "def helper(value):\n    return value + 1\n", encoding="utf-8",
    )
    (root / "app.py").write_text(
        "from lib import helper\n\n\n"
        "def run(value):\n"
        "    return helper(helper(value))\n",
        encoding="utf-8",
    )
    db_path = root / ".code-review-graph" / "graph.db"
    with GraphStore(db_path) as store:
        full_build(root, store)

    with sqlite3.connect(db_path) as conn:
        unclassified = conn.execute(
            "SELECT COUNT(*) FROM edges "
            "WHERE kind IN ('CALLS', 'REFERENCES') AND target_resolution IS NULL"
        ).fetchone()[0]
    assert unclassified == 0
