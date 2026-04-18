"""Tests for SQLite transaction robustness and nesting scenarios."""

import sqlite3
import tempfile
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import NodeInfo, EdgeInfo
from code_review_graph.communities import store_communities
from code_review_graph.flows import store_flows

@pytest.fixture
def store():
    """Create a temporary GraphStore for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    store = GraphStore(db_path)
    yield store
    store.close()
    Path(db_path).unlink(missing_ok=True)

class TestTransactionRobustness:
    def test_nested_transaction_guard_in_store_file(self, store, caplog):
        """Test that store_file_nodes_edges handles an already open transaction."""
        # Manually open a transaction
        store._conn.execute("BEGIN")
        store._conn.execute("INSERT INTO metadata (key, value) VALUES (?, ?)", ("test", "val"))
        assert store._conn.in_transaction
        
        # This should trigger the guard, rollback the uncommitted insert, and start a new transaction
        with caplog.at_level(logging.WARNING):
            store.store_file_nodes_edges("test.py", [], [])
            
        assert "Rolling back uncommitted transaction before BEGIN IMMEDIATE" in caplog.text
        assert not store._conn.in_transaction
        
        # Verify the "val" was rolled back
        assert store.get_metadata("test") is None

    def test_atomic_community_storage(self, store):
        """Test that store_communities is atomic and handles existing transactions."""
        communities = [
            {"name": "comm1", "size": 1, "members": ["node1"]}
        ]
        
        # Leave a transaction open
        store._conn.execute("BEGIN")
        store._conn.execute("INSERT INTO metadata (key, value) VALUES ('leak', 'stale')")
        
        # Should rollback the 'leak' and successfully store communities
        store_communities(store, communities)
        
        assert store.get_metadata("leak") is None
        
        # Verify communities table
        count = store._conn.execute("SELECT count(*) FROM communities").fetchone()[0]
        assert count == 1

    def test_atomic_flow_storage(self, store):
        """Test that store_flows is atomic and handles existing transactions."""
        flows = [
            {
                "name": "flow1", "entry_point_id": 1, "depth": 1, 
                "node_count": 1, "file_count": 1, "criticality": 0.5, 
                "path": [1]
            }
        ]
        
        # Leave a transaction open
        store._conn.execute("BEGIN")
        store._conn.execute("INSERT INTO metadata (key, value) VALUES ('leak', 'stale')")
        
        # Should rollback and store flows
        store_flows(store, flows)
        
        assert store.get_metadata("leak") is None
        count = store._conn.execute("SELECT count(*) FROM flows").fetchone()[0]
        assert count == 1

    def test_rollback_on_failure_in_batch_ops(self, store):
        """Verify that store_file_nodes_edges rolls back if an operation fails inside."""
        # Pre-seed some data
        node_keep = NodeInfo(
            kind="File", name="keep", file_path="keep.py", 
            line_start=1, line_end=10, language="python"
        )
        store.store_file_nodes_edges("keep.py", [node_keep], [])
        
        # Attempt to store new file but force a failure
        node_fail = NodeInfo(
            kind="File", name="fail", file_path="fail.py", 
            line_start=1, line_end=10, language="python"
        )
        
        with patch.object(store, 'upsert_node', side_effect=Exception("Simulated failure")):
            with pytest.raises(Exception, match="Simulated failure"):
                store.store_file_nodes_edges("fail.py", [node_fail], [])
        
        # Verify 'fail.py' data is NOT present
        assert len(store.get_nodes_by_file("fail.py")) == 0
        # Verify 'keep.py' data IS still present
        assert len(store.get_nodes_by_file("keep.py")) == 1

    def test_public_rollback_api(self, store):
        """Verify the new GraphStore.rollback() public method works."""
        store._conn.execute("BEGIN")
        store._conn.execute("INSERT INTO metadata (key, value) VALUES ('rollback', 'me')")
        assert store._conn.in_transaction
        
        store.rollback()
        assert not store._conn.in_transaction
        assert store.get_metadata("rollback") is None
