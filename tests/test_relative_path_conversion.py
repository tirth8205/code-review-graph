"""An absolute-path graph is converted to repo-relative storage once, on open.

Copy and swap in the same file, batched and resumable; other openers are
told to try again while it runs. See relpath_migration.py.
"""

import os
import shutil
import sqlite3

import pytest

from code_review_graph import relpath_migration
from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.relpath_migration import (
    MIGRATION_KEY,
    GraphMigratingError,
    convert_to_relative,
)
from code_review_graph.relpaths import PATH_MODE_KEY, PATH_MODE_RELATIVE


def _absolute_graph(tmp_path, files=3):
    """An old graph: built where no root was known, then put in place."""
    repo = tmp_path / "repo"
    (repo / ".code-review-graph").mkdir(parents=True)
    repo = repo.resolve()
    scratch = tmp_path / "scratch.db"
    store = GraphStore(scratch)
    for i in range(files):
        path = f"{repo}/pkg/m{i}.py"
        store.upsert_node(NodeInfo("File", path, path, 1, 9, "python"))
        store.upsert_node(NodeInfo("Function", f"fn{i}", path, 1, 2, "python"))
        store.upsert_edge(EdgeInfo("CONTAINS", path, f"{path}::fn{i}", path))
        store.upsert_edge(EdgeInfo("CALLS", f"{path}::fn{i}", "print", path, 2))
        if i:
            store.upsert_edge(
                EdgeInfo("CALLS", f"{path}::fn{i}", f"{repo}/pkg/m0.py::fn0", path, 1)
            )
    store.commit()
    store._conn.execute(
        "CREATE TABLE IF NOT EXISTS embeddings (qualified_name TEXT PRIMARY KEY, "
        "vector BLOB NOT NULL, text_hash TEXT NOT NULL, provider TEXT NOT NULL "
        "DEFAULT 'unknown')"
    )
    store._conn.execute(
        "INSERT INTO embeddings VALUES (?, x'00', 'h', 'p')", (f"{repo}/pkg/m0.py::fn0",),
    )
    store.close()
    db = repo / ".code-review-graph" / "graph.db"
    shutil.move(scratch, db)
    return repo, db


def _raw(db, sql, params=()):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _counts(db):
    return tuple(_raw(db, f"SELECT COUNT(*) FROM {t}")[0][0] for t in ("nodes", "edges"))


def test_opening_converts_and_keeps_every_row(tmp_path):
    repo, db = _absolute_graph(tmp_path)
    before = _counts(db)
    store = GraphStore(db)
    try:
        assert store.get_metadata(PATH_MODE_KEY) == PATH_MODE_RELATIVE
        assert store.get_metadata(MIGRATION_KEY) is None
        callers = store.get_edges_by_target(f"{repo}/pkg/m0.py::fn0")
        assert {e.source_qualified for e in callers if e.kind == "CALLS"} == {
            f"{repo}/pkg/m1.py::fn1", f"{repo}/pkg/m2.py::fn2",
        }
        assert [n.name for n in store.search_nodes("fn1")][:1] == ["fn1"]
    finally:
        store.close()
    assert _counts(db) == before
    assert ("./pkg/m0.py::fn0",) in _raw(db, "SELECT qualified_name FROM nodes")
    assert ("print",) in _raw(db, "SELECT target_qualified FROM edges")
    assert _raw(db, "SELECT qualified_name FROM embeddings") == [("./pkg/m0.py::fn0",)]
    indexes = {r[0] for r in _raw(db, "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"idx_nodes_file", "idx_edges_source_kind", "idx_edges_target_kind"} <= indexes
    assert not _raw(db, "SELECT name FROM sqlite_master WHERE name LIKE '%_relpath'")


def test_a_killed_conversion_resumes(tmp_path, monkeypatch):
    repo, db = _absolute_graph(tmp_path, files=7)
    conn = sqlite3.connect(db, isolation_level=None)

    def crash(*_args):
        raise KeyboardInterrupt

    monkeypatch.setattr(relpath_migration, "_rekey_embeddings", crash)
    with pytest.raises(KeyboardInterrupt):
        convert_to_relative(conn, str(repo), batch=4)
    conn.close()
    monkeypatch.undo()
    # The absolute tables are still the live ones, with a partial copy beside them.
    assert _raw(db, "SELECT value FROM metadata WHERE key = ?", (PATH_MODE_KEY,)) == []
    assert _raw(db, "SELECT COUNT(*) FROM nodes_relpath")[0][0] == 14

    store = GraphStore(db)
    try:
        assert store.path_root == str(repo)
        assert len(store.get_nodes_by_file(f"{repo}/pkg/m6.py")) == 2
    finally:
        store.close()
    assert _raw(db, "SELECT COUNT(*) FROM nodes")[0][0] == 14


def test_other_openers_are_told_to_try_again(tmp_path):
    _repo, db = _absolute_graph(tmp_path)
    holder = os.getppid()  # alive, and not this process
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO metadata (key, value) VALUES (?, ?)", (MIGRATION_KEY, str(holder)))
    conn.commit()
    conn.close()
    with pytest.raises(GraphMigratingError, match="try again"):
        GraphStore(db)
    with pytest.raises(GraphMigratingError):
        GraphStore(db, read_only=True)


def test_a_dead_holder_does_not_block(tmp_path):
    repo, db = _absolute_graph(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO metadata (key, value) VALUES (?, ?)", (MIGRATION_KEY, "999999"))
    conn.commit()
    conn.close()
    store = GraphStore(db)
    try:
        assert store.path_root == str(repo)
    finally:
        store.close()


def test_read_only_opener_reads_the_absolute_graph_unchanged(tmp_path):
    repo, db = _absolute_graph(tmp_path)
    store = GraphStore(db, read_only=True)
    try:
        assert store.path_root is None
        assert len(store.get_nodes_by_file(f"{repo}/pkg/m0.py")) == 2
    finally:
        store.close()
    assert (f"{repo}/pkg/m0.py",) in _raw(db, "SELECT DISTINCT file_path FROM nodes")


def test_a_row_that_collides_does_not_end_the_copy(tmp_path):
    """Two spellings of one path become one key; the rows after it still copy.

    ``INSERT OR IGNORE`` drops the second spelling. The batch then copies fewer
    rows than it read, which the copy loop took for the end of the table, so the
    swap dropped every later row with the old table.
    """
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    real = real.resolve()
    db = tmp_path / "graph.db"
    store = GraphStore(db)
    store.upsert_node(NodeInfo("File", f"{link}/a.py", f"{link}/a.py", 1, 9, "python"))
    store.upsert_node(NodeInfo("File", f"{real}/a.py", f"{real}/a.py", 1, 9, "python"))
    for i in range(5):
        path = f"{link}/m{i}.py"
        store.upsert_node(NodeInfo("File", path, path, 1, 9, "python"))
    store.commit()
    store.close()

    conn = sqlite3.connect(db, isolation_level=None)
    try:
        convert_to_relative(conn, str(link), batch=2)
    finally:
        conn.close()
    stored = {r[0] for r in _raw(db, "SELECT qualified_name FROM nodes")}
    assert stored == {"./a.py"} | {f"./m{i}.py" for i in range(5)}
