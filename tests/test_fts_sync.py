"""Tests for FTS5 content sync robustness."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import NodeInfo
from code_review_graph.search import rebuild_fts_index

@pytest.fixture
def store():
    """Create a temporary GraphStore for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    store = GraphStore(db_path)
    yield store
    store.close()
    Path(db_path).unlink(missing_ok=True)

class TestFTSSync:
    def test_fts_rebuild_syncs_with_nodes(self, store):
        """Test that rebuild_fts_index properly populates from nodes table."""
        # 1. Add some nodes
        node1 = NodeInfo(
            kind="Function", name="calculate_total", file_path="app.py", 
            line_start=1, line_end=5, language="python"
        )
        node2 = NodeInfo(
            kind="Class", name="OrderProcessor", file_path="app.py", 
            line_start=10, line_end=50, language="python"
        )
        store.store_file_nodes_edges("app.py", [node1, node2], [])
        
        # 2. Rebuild FTS
        count = rebuild_fts_index(store)
        assert count == 2
        
        # 3. Verify FTS content via search
        # We query the virtual table directly to ensure it has the data
        fts_rows = store._conn.execute(
            "SELECT name FROM nodes_fts WHERE name MATCH 'calculate*'"
        ).fetchall()
        assert len(fts_rows) == 1
        assert fts_rows[0]["name"] == "calculate_total"

    def test_fts_rebuild_clears_old_data(self, store):
        """Test that rebuild_fts_index clears existing FTS data before repopulating."""
        # 1. Add and index one node
        node1 = NodeInfo(
            kind="Function", name="old_func", file_path="old.py", 
            line_start=1, line_end=5, language="python"
        )
        store.store_file_nodes_edges("old.py", [node1], [])
        rebuild_fts_index(store)
        
        # 2. Delete the file/nodes
        store.remove_file_data("old.py")
        store.commit()
        
        # 3. Add a new node
        node2 = NodeInfo(
            kind="Function", name="new_func", file_path="new.py", 
            line_start=1, line_end=5, language="python"
        )
        store.store_file_nodes_edges("new.py", [node2], [])
        
        # 4. Rebuild FTS - should ONLY have new_func
        rebuild_fts_index(store)
        
        fts_rows = store._conn.execute("SELECT name FROM nodes_fts").fetchall()
        assert len(fts_rows) == 1
        assert fts_rows[0]["name"] == "new_func"
