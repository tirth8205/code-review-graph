"""Tables nothing reads and indexes other indexes cover are gone from the graph.

``community_summaries``, ``flow_snapshots`` and ``risk_index`` were written on
every full postprocess and never read, and their rows kept pointing at deleted
nodes. ``idx_nodes_qualified`` repeated the UNIQUE index on
``nodes.qualified_name``; ``idx_edges_source`` and ``idx_edges_target`` are the
leading columns of ``idx_edges_source_kind`` and ``idx_edges_target_kind``.
See: #1059, #1162
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from code_review_graph import migrations
from code_review_graph.graph import GraphStore
from code_review_graph.migrations import LATEST_VERSION, get_schema_version
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.tools import run_postprocess

DROPPED_TABLES = {"community_summaries", "flow_snapshots", "risk_index"}
DROPPED_INDEXES = {
    "idx_nodes_qualified", "idx_edges_source", "idx_edges_target", "idx_risk_index_score",
}
KEPT_INDEXES = {
    "sqlite_autoindex_nodes_1",
    "idx_nodes_file",
    "idx_nodes_kind",
    "idx_edges_kind",
    "idx_edges_file",
    "idx_edges_source_kind",
    "idx_edges_target_kind",
    "idx_edges_composite",
    "idx_edges_kind_target_resolution",
}


def _objects(conn: sqlite3.Connection, kind: str) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = ?", (kind,),
    ).fetchall()
    return {row[0] for row in rows}


def _fill(store: GraphStore) -> None:
    for name in ("first", "second"):
        store.upsert_node(NodeInfo(
            kind="Function", name=name, file_path="/repo/a.py",
            line_start=1, line_end=2, language="python",
        ))
    store.upsert_edge(EdgeInfo(
        kind="CALLS", source="/repo/a.py::first", target="/repo/a.py::second",
        file_path="/repo/a.py", line=1,
    ))
    store.commit()


def _as_v14(db_path: Path) -> None:
    """Put back what a real v14 database holds, as the released code wrote it."""
    conn = sqlite3.connect(db_path)
    migrations._migrate_v6(conn)
    conn.execute("CREATE INDEX idx_nodes_qualified ON nodes(qualified_name)")
    conn.execute("CREATE INDEX idx_edges_source ON edges(source_qualified)")
    conn.execute("CREATE INDEX idx_edges_target ON edges(target_qualified)")
    conn.execute(
        "INSERT INTO risk_index (node_id, qualified_name) VALUES (999, '/gone.py::dangling')"
    )
    conn.execute("UPDATE metadata SET value = '14' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()


def test_a_new_graph_never_has_them(tmp_path):
    with GraphStore(tmp_path / "graph.db") as store:
        assert not _objects(store._conn, "table") & DROPPED_TABLES
        indexes = _objects(store._conn, "index")
        assert not indexes & DROPPED_INDEXES
        assert KEPT_INDEXES <= indexes
        assert get_schema_version(store._conn) == LATEST_VERSION


def test_upgrading_from_v14_drops_them_and_keeps_the_graph(tmp_path):
    db_path = tmp_path / "graph.db"
    with GraphStore(db_path) as store:
        _fill(store)
    _as_v14(db_path)

    with GraphStore(db_path) as store:
        assert not _objects(store._conn, "table") & DROPPED_TABLES
        indexes = _objects(store._conn, "index")
        assert not indexes & DROPPED_INDEXES
        assert KEPT_INDEXES <= indexes
        assert get_schema_version(store._conn) == LATEST_VERSION
        assert store.get_node("/repo/a.py::first") is not None
        callers = store.get_edges_by_target("/repo/a.py::second")
        assert [edge.source_qualified for edge in callers] == ["/repo/a.py::first"]


def test_reopening_does_not_bring_them_back(tmp_path):
    db_path = tmp_path / "graph.db"
    with GraphStore(db_path):
        pass
    with GraphStore(db_path) as store:
        assert not _objects(store._conn, "index") & DROPPED_INDEXES


def test_a_migration_cut_off_half_way_finishes_on_the_next_open(tmp_path):
    db_path = tmp_path / "graph.db"
    with GraphStore(db_path):
        pass
    _as_v14(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE risk_index")
    conn.execute("DROP INDEX idx_edges_source")
    conn.commit()
    conn.close()

    with GraphStore(db_path) as store:
        assert not _objects(store._conn, "table") & DROPPED_TABLES
        assert not _objects(store._conn, "index") & DROPPED_INDEXES
        assert get_schema_version(store._conn) == LATEST_VERSION


@pytest.mark.parametrize(
    ("sql", "index"),
    [
        ("SELECT * FROM edges WHERE source_qualified = 'x'", "idx_edges_source_kind"),
        ("SELECT * FROM edges WHERE target_qualified = 'x'", "idx_edges_target_kind"),
        ("SELECT * FROM nodes WHERE qualified_name = 'x'", "sqlite_autoindex_nodes_1"),
    ],
)
def test_the_covering_index_serves_the_lookup(tmp_path, sql, index):
    with GraphStore(tmp_path / "graph.db") as store:
        plan = store._conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
    assert index in plan[0][3]
    assert plan[0][3].startswith("SEARCH")


def test_edge_readers_keep_insertion_order_across_kinds(tmp_path):
    """The kept indexes list rows by kind first; answers must not depend on that."""
    with GraphStore(tmp_path / "graph.db") as store:
        for kind, target in (("REFERENCES", "b"), ("CALLS", "c"), ("IMPORTS_FROM", "d")):
            store.upsert_edge(EdgeInfo(
                kind=kind, source="a", target=target, file_path="/repo/a.py", line=1,
            ))
        for kind, source in (("REFERENCES", "e"), ("CALLS", "f"), ("IMPORTS_FROM", "g")):
            store.upsert_edge(EdgeInfo(
                kind=kind, source=source, target="a", file_path="/repo/a.py", line=1,
            ))
        store.commit()

        outgoing = store.get_edges_by_source("a")
        incoming = store.get_edges_by_target("a")
        among = store.get_edges_among({"a", "b", "c", "d"})
        # The wiki counts these and keeps the top 15, so ties keep this order.
        targets = store.get_outgoing_targets(["a"])
        sources = store.get_incoming_sources(["a"])

    assert [edge.target_qualified for edge in outgoing] == ["b", "c", "d"]
    assert [edge.source_qualified for edge in incoming] == ["e", "f", "g"]
    assert [edge.target_qualified for edge in among] == ["b", "c", "d"]
    assert targets == ["b", "c", "d"]
    assert sources == ["e", "f", "g"]


def test_postprocess_runs_without_them_and_reports_no_summaries(tmp_path):
    root = tmp_path.resolve()
    (root / ".git").mkdir()
    (root / ".code-review-graph").mkdir()
    with GraphStore(root / ".code-review-graph" / "graph.db") as store:
        _fill(store)

    result = run_postprocess(repo_root=str(root))

    assert result["status"] == "ok"
    assert not any("Summary" in warning for warning in result.get("warnings", []))
    assert "summaries_computed" not in result
    assert "summaries_s" not in result.get("postprocess_timing", {})


def test_only_the_migrations_name_the_dropped_tables():
    """A new writer would bring the stale rows back, with nothing reading them."""
    package = Path(migrations.__file__).parent
    for path in package.rglob("*.py"):
        if path.name == "migrations.py":
            continue
        text = path.read_text(encoding="utf-8")
        for table in DROPPED_TABLES:
            assert table not in text, f"{path} still names {table}"
