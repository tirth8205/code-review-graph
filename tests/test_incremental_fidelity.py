"""Tests for the incremental-equals-rebuild fidelity benchmark.

The benchmark answers one question: after an incremental update, is the graph
byte-for-byte what a clean rebuild of the same tree would have produced? A
clean rebuild is a free and perfect oracle, so any divergence is drift the
persistent graph accumulated and never reported.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from code_review_graph.eval.benchmarks import incremental_fidelity as fidelity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HELPER_PY = '''"""Helper utilities."""


def greet(name):
    return f"Hello {name}"


def farewell(name):
    return f"Bye {name}"
'''

_SERVICE_PY = '''"""Service layer."""

from helper import farewell, greet


def welcome(name):
    return greet(name)


def dismiss(name):
    return farewell(name)
'''

_MAIN_PY = '''"""Entry point."""

from service import dismiss, welcome


def main():
    print(welcome("world"))
    print(dismiss("world"))


if __name__ == "__main__":
    main()
'''

_TEST_PY = '''"""Tests."""

from service import welcome


def test_welcome():
    assert welcome("a") == "Hello a"
'''


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=bench@example.com",
            "-c",
            "user.name=bench",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )


def _mock_repo(root: Path) -> Path:
    """A tiny but structurally real git repository."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "helper.py").write_text(_HELPER_PY, encoding="utf-8")
    (root / "service.py").write_text(_SERVICE_PY, encoding="utf-8")
    (root / "main.py").write_text(_MAIN_PY, encoding="utf-8")
    (root / "test_service.py").write_text(_TEST_PY, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--no-verify", "-m", "initial")
    return root


def _build_db(tree: Path, db_path: Path) -> None:
    fidelity._clean_build(tree, db_path)


# ---------------------------------------------------------------------------
# Registration and rendering
# ---------------------------------------------------------------------------


def test_incremental_fidelity_registered_in_the_runner():
    """A benchmark nothing dispatches to never runs."""
    yaml = pytest.importorskip("yaml")  # noqa: F841
    from code_review_graph.eval.runner import BENCHMARK_REGISTRY

    assert "incremental_fidelity" in BENCHMARK_REGISTRY
    assert BENCHMARK_REGISTRY["incremental_fidelity"] is fidelity.run


def test_incremental_fidelity_rendered_by_the_full_report(tmp_path):
    """The reporter must emit the benchmark's rows like the existing seven."""
    from code_review_graph.eval.reporter import generate_full_report

    csv_path = tmp_path / "mock_incremental_fidelity_2026-01-01.csv"
    csv_path.write_text(
        "repo,edit_kind,status,total_differing_rows\n"
        "mock,trailing_comment,ok,0\n",
        encoding="utf-8",
    )
    report = generate_full_report(tmp_path)
    assert "Incremental Fidelity" in report
    assert "trailing_comment" in report


def test_incremental_fidelity_rendered_by_the_readme_tables(tmp_path):
    from code_review_graph.eval.reporter import generate_readme_tables

    csv_path = tmp_path / "mock_incremental_fidelity_2026-01-01.csv"
    csv_path.write_text(
        "repo,edit_kind,status,total_differing_rows,seconds\n"
        "mock,delete_file,diverged,7,1.5\n",
        encoding="utf-8",
    )
    tables = generate_readme_tables(tmp_path)
    assert "delete_file" in tables
    assert "7" in tables


# ---------------------------------------------------------------------------
# The comparator itself
# ---------------------------------------------------------------------------


def test_two_clean_builds_of_the_same_tree_compare_equal(tmp_path):
    """The oracle must agree with itself, or every later number is noise."""
    tree = _mock_repo(tmp_path / "repo")
    left = tmp_path / "left.db"
    right = tmp_path / "right.db"
    _build_db(tree, left)
    _build_db(tree, right)

    diff = fidelity.compare_graphs(left, right)
    assert diff["total_differing_rows"] == 0, diff["tables"]


def test_comparator_counts_differing_rows_per_table(tmp_path):
    """An injected divergence is attributed to the table that holds it."""
    tree = _mock_repo(tmp_path / "repo")
    left = tmp_path / "left.db"
    right = tmp_path / "right.db"
    _build_db(tree, left)
    shutil.copy2(left, right)

    import sqlite3

    conn = sqlite3.connect(str(left))
    conn.execute(
        "DELETE FROM edges WHERE kind = 'CALLS' AND rowid IN "
        "(SELECT rowid FROM edges WHERE kind = 'CALLS' LIMIT 2)"
    )
    conn.commit()
    conn.close()

    diff = fidelity.compare_graphs(left, right)
    assert diff["tables"]["edges"]["differing_rows"] == 2
    assert diff["total_differing_rows"] >= 2


def test_comparator_names_a_concrete_example(tmp_path):
    """"Seven rows differ" is unactionable without one of the seven."""
    tree = _mock_repo(tmp_path / "repo")
    left = tmp_path / "left.db"
    right = tmp_path / "right.db"
    _build_db(tree, left)
    shutil.copy2(left, right)

    import sqlite3

    conn = sqlite3.connect(str(left))
    conn.execute(
        "UPDATE nodes SET line_start = line_start + 99 WHERE name = 'greet'"
    )
    conn.commit()
    conn.close()

    diff = fidelity.compare_graphs(left, right)
    nodes = diff["tables"]["nodes"]
    assert nodes["differing_rows"] >= 1
    assert nodes["examples"], "a divergence with no example is not a report"
    assert any("greet" in ex for ex in nodes["examples"])


def test_comparator_covers_every_derived_table(tmp_path):
    """Nodes and edges alone leave the derived tables free to rot."""
    tree = _mock_repo(tmp_path / "repo")
    left = tmp_path / "left.db"
    right = tmp_path / "right.db"
    _build_db(tree, left)
    _build_db(tree, right)

    diff = fidelity.compare_graphs(left, right)
    for table in (
        "nodes",
        "edges",
        "communities",
        "node_community",
        "flows",
        "flow_memberships",
        "nodes_fts",
        "community_summaries",
        "flow_snapshots",
        "risk_index",
        "metadata",
    ):
        assert table in diff["tables"], f"{table} is never compared"


def test_comparator_uses_the_unused_graph_diff_helpers(tmp_path):
    """graph_diff.take_snapshot/diff_snapshots exist and nothing called them."""
    tree = _mock_repo(tmp_path / "repo")
    left = tmp_path / "left.db"
    right = tmp_path / "right.db"
    _build_db(tree, left)
    shutil.copy2(left, right)

    import sqlite3

    conn = sqlite3.connect(str(left))
    conn.execute("DELETE FROM nodes WHERE name = 'farewell'")
    conn.commit()
    conn.close()

    diff = fidelity.compare_graphs(left, right)
    # "added" is relative to the incremental side: the rebuild has a node the
    # incremental graph lost.
    assert diff["snapshot_summary"]["nodes_added"] == 1


# ---------------------------------------------------------------------------
# End-to-end benchmark
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _mock_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("fidelity")
    tree = _mock_repo(root / "repo")
    config = {"name": "mock"}
    from code_review_graph.graph import GraphStore

    store = GraphStore(root / "outer.db")
    try:
        return fidelity.run(tree, store, config)
    finally:
        store.close()


def test_every_edit_kind_gets_exactly_one_row(_mock_run):
    kinds = [r["edit_kind"] for r in _mock_run]
    assert kinds == list(fidelity.EDIT_KINDS)
    assert len(fidelity.EDIT_KINDS) == 7


def test_each_row_reports_wall_time_and_per_table_counts(_mock_run):
    for row in _mock_run:
        assert isinstance(row["seconds"], float)
        assert row["seconds"] >= 0.0
        assert isinstance(row["total_differing_rows"], int)
        for table in fidelity.COMPARED_TABLES:
            assert f"{table}_diff" in row


def test_known_failures_do_not_turn_the_run_red(_mock_run):
    """A currently-broken edit kind is recorded, not raised."""
    for row in _mock_run:
        assert row["status"] in {"ok", "known_failure", "diverged", "skipped", "error"}
        if row["edit_kind"] in fidelity.KNOWN_FAILURES:
            assert row["status"] != "diverged", (
                "a known failure must be reported as known_failure, "
                "not as an unexpected divergence"
            )


def test_edit_kinds_outside_the_known_failure_list_still_hold(_mock_run):
    """Regression guard: a passing edit kind must not start diverging."""
    unexpected = [
        (r["edit_kind"], r["total_differing_rows"], r["worst_table"])
        for r in _mock_run
        if r["status"] == "diverged"
    ]
    assert not unexpected, f"undeclared divergences: {unexpected}"


def test_run_reports_the_benchmark_wall_time(_mock_run):
    assert all("benchmark_seconds" in r for r in _mock_run)
    assert len({r["benchmark_seconds"] for r in _mock_run}) == 1


def test_known_failures_only_names_real_edit_kinds():
    """A stale entry would silence a regression guard for a kind that is gone."""
    assert fidelity.KNOWN_FAILURES <= set(fidelity.EDIT_KINDS)


# ---------------------------------------------------------------------------
# What the benchmark found. These document the defects at the level of the
# defect rather than at the level of the whole-graph diff; they are xfail so
# the pull request that fixes each one turns them green without editing them.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason=(
        "flow_memberships.node_id has no foreign key or cascade, and "
        "GraphStore._replace_file_data re-inserts a re-parsed file's nodes "
        "with fresh ids, so every membership row for that file is orphaned"
    ),
    strict=False,
)
def test_incremental_update_leaves_no_dangling_flow_memberships(tmp_path):
    import sqlite3

    from code_review_graph.eval.benchmarks import incremental_fidelity as f

    tree = _mock_repo(tmp_path / "repo")
    db = tmp_path / "graph.db"
    f._clean_build(tree, db)
    base = f._head(tree)

    helper = tree / "helper.py"
    helper.write_text(helper.read_text(encoding="utf-8") + "# neutral\n",
                      encoding="utf-8")
    f._commit_all(tree, "neutral edit")
    f._incremental_build(tree, db, base=base)

    conn = sqlite3.connect(str(db))
    try:
        dangling = conn.execute(
            "SELECT count(*) FROM flow_memberships fm "
            "LEFT JOIN nodes n ON n.id = fm.node_id WHERE n.id IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    assert dangling == 0


@pytest.mark.xfail(
    reason=(
        "incremental_detect_communities matches nodes.file_path (absolute) "
        "against incremental_update's changed_files (repo-relative), so it "
        "always finds zero affected communities and skips"
    ),
    strict=False,
)
def test_reparsed_nodes_keep_a_community_assignment(tmp_path):
    import sqlite3

    from code_review_graph.eval.benchmarks import incremental_fidelity as f

    tree = _mock_repo(tmp_path / "repo")
    db = tmp_path / "graph.db"
    f._clean_build(tree, db)
    base = f._head(tree)

    def uncommunitied() -> int:
        conn = sqlite3.connect(str(db))
        try:
            return conn.execute(
                "SELECT count(*) FROM nodes WHERE community_id IS NULL"
            ).fetchone()[0]
        finally:
            conn.close()

    before = uncommunitied()
    helper = tree / "helper.py"
    helper.write_text(helper.read_text(encoding="utf-8") + "# neutral\n",
                      encoding="utf-8")
    f._commit_all(tree, "neutral edit")
    f._incremental_build(tree, db, base=base)
    assert uncommunitied() == before
