"""Rebuild legacy stored paths without deleting current canonical rows (#911)."""

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update
from code_review_graph.parser import NodeInfo


def test_911_rebuild_removes_legacy_native_path_rows(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    kept = root / "keep.py"
    deleted = root / "gone.py"
    kept.write_text("def keep(): return 1\n")
    deleted.write_text("def gone(): return 2\n")
    with GraphStore(tmp_path / "graph.db") as store:
        first = full_build(root, store)
        assert not first["errors"]
        assert store.get_stats().total_nodes == 4
        store._conn.execute(
            "UPDATE nodes SET file_path = replace(file_path, '/', char(92)), "
            "qualified_name = replace(qualified_name, '/', char(92))"
        )
        store._conn.execute(
            "UPDATE edges SET file_path = replace(file_path, '/', char(92)), "
            "source_qualified = replace(source_qualified, '/', char(92)), "
            "target_qualified = replace(target_qualified, '/', char(92))"
        )
        deleted.unlink()
        result = full_build(root, store)
        assert not result["errors"]
        assert store.get_stats().total_nodes == 2
        assert set(store.get_all_files()) == {kept.as_posix()}
        assert not store._conn.execute(
            "SELECT 1 FROM nodes WHERE instr(file_path, char(92)) > 0"
        ).fetchone()
        assert not store._conn.execute(
            "SELECT 1 FROM edges WHERE instr(file_path, char(92)) > 0"
        ).fetchone()


def test_incremental_cleanup_preserves_canonical_copy(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "keep.py"
    source.write_text("def keep(): return 1\n")
    with GraphStore(tmp_path / "graph.db") as store:
        full_build(root, store)
        store.upsert_node(
            NodeInfo(
                kind="Function",
                name="stale",
                file_path=str(source),
                line_start=1,
                line_end=1,
                language="python",
            )
        )
        store._conn.execute(
            "UPDATE nodes SET file_path = replace(file_path, '/', char(92)) WHERE name = 'stale'"
        )
        result = incremental_update(root, store, changed_files=[])
        assert not result["errors"]
        assert store.get_node(source.as_posix() + "::keep") is not None
        assert store.get_node(source.as_posix() + "::stale") is None
        assert store.get_stats().total_nodes == 2
