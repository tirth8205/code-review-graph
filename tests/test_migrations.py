"""Tests for the schema migration framework."""

import sqlite3
import tempfile
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    get_schema_version,
    run_migrations,
)


class TestMigrations:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()  # release the handle before GraphStore reopens it on Windows
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_v10_upgrades_a_database_created_before_the_symbol_column(self):
        """A real pre-v10 database has no ``symbol`` column at all.

        ``_init_schema`` runs before ``run_migrations`` and ``CREATE TABLE IF
        NOT EXISTS`` cannot add a column to an existing table, so if the schema
        script indexed ``symbol`` directly every pre-existing graph would fail
        to open. Reopening a stripped database must migrate it cleanly.
        """
        path = self.tmp.name
        conn = self.store._conn
        conn.execute("DROP INDEX IF EXISTS idx_nodes_symbol")
        conn.execute("ALTER TABLE nodes DROP COLUMN symbol")
        conn.execute(
            "INSERT INTO nodes "
            "(kind, name, qualified_name, file_path, language, updated_at) "
            "VALUES ('Function', 'process', ?, 'src/app.py', 'python', 0)",
            ("src/app.py::Handler.process",),
        )
        conn.execute("UPDATE metadata SET value = '9' WHERE key = 'schema_version'")
        self.store.commit()
        self.store.close()

        # Reopening runs _init_schema and then the migrations.
        self.store = GraphStore(path)

        assert get_schema_version(self.store._conn) == LATEST_VERSION
        assert [
            node.qualified_name
            for node in self.store.search_nodes_by_qualified_tail("Handler.process")
        ] == ["src/app.py::Handler.process"]

    def test_v10_backfills_and_indexes_the_symbol_column(self):
        """v10 must populate ``symbol`` for pre-existing rows and index it, so
        dotted-tail lookups are indexed rather than scanning every node."""
        conn = self.store._conn
        conn.execute("DROP INDEX IF EXISTS idx_nodes_symbol")
        conn.execute(
            "INSERT INTO nodes "
            "(kind, name, qualified_name, file_path, language, symbol, updated_at) "
            "VALUES ('Function', 'process', ?, 'src/app.py', 'python', NULL, 0)",
            ("src/app.py::Handler.process",),
        )
        # A bare qualified name with no path component stays its own symbol.
        conn.execute(
            "INSERT INTO nodes "
            "(kind, name, qualified_name, file_path, language, symbol, updated_at) "
            "VALUES ('File', 'loose', 'loose', 'loose', 'python', NULL, 0)"
        )
        conn.execute("UPDATE metadata SET value = '9' WHERE key = 'schema_version'")
        self.store.commit()

        run_migrations(conn)

        rows = dict(conn.execute("SELECT qualified_name, symbol FROM nodes").fetchall())
        assert rows["src/app.py::Handler.process"] == "Handler.process"
        assert rows["loose"] == "loose"

        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM nodes WHERE symbol = ?", ("x",)
        ).fetchall()
        assert any("idx_nodes_symbol" in str(step[-1]) for step in plan)

    def test_fresh_db_gets_latest_version(self):
        """A newly created DB should be at the latest schema version."""
        version = get_schema_version(self.store._conn)
        assert version == LATEST_VERSION

    def test_v1_db_migrates_to_latest(self):
        """A v1 database should migrate to latest when GraphStore is opened."""
        # Close the store that was already migrated
        self.store.close()

        # Manually create a v1 database (base schema only, version=1)
        conn = sqlite3.connect(str(self.tmp.name))
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES ('schema_version', '1')"
        )
        conn.commit()
        # Drop migration artifacts to simulate v1
        conn.execute("DROP TABLE IF EXISTS flows")
        conn.execute("DROP TABLE IF EXISTS flow_memberships")
        conn.execute("DROP TABLE IF EXISTS communities")
        conn.execute("DROP TABLE IF EXISTS nodes_fts")
        conn.execute("DROP TABLE IF EXISTS community_summaries")
        conn.execute("DROP TABLE IF EXISTS flow_snapshots")
        conn.execute("DROP TABLE IF EXISTS risk_index")
        conn.commit()
        conn.close()

        # Re-open with GraphStore — should trigger migrations
        self.store = GraphStore(self.tmp.name)
        assert get_schema_version(self.store._conn) == LATEST_VERSION

    def test_migration_is_idempotent(self):
        """Opening GraphStore twice should leave schema at latest version."""
        self.store.close()
        self.store = GraphStore(self.tmp.name)
        assert get_schema_version(self.store._conn) == LATEST_VERSION

        self.store.close()
        self.store = GraphStore(self.tmp.name)
        assert get_schema_version(self.store._conn) == LATEST_VERSION

    def test_signature_column_exists_after_migration(self):
        """The nodes table should have a 'signature' column after migration."""
        cursor = self.store._conn.execute("PRAGMA table_info(nodes)")
        columns = [row[1] if isinstance(row, tuple) else row["name"] for row in cursor]
        assert "signature" in columns

    def test_flows_table_exists_after_migration(self):
        """The flows and flow_memberships tables should exist after migration."""
        tables = _get_table_names(self.store._conn)
        assert "flows" in tables
        assert "flow_memberships" in tables

    def test_communities_table_exists_after_migration(self):
        """The communities table should exist and nodes should have community_id."""
        tables = _get_table_names(self.store._conn)
        assert "communities" in tables

        cursor = self.store._conn.execute("PRAGMA table_info(nodes)")
        columns = [row[1] if isinstance(row, tuple) else row["name"] for row in cursor]
        assert "community_id" in columns

    def test_fts5_table_exists_after_migration(self):
        """The nodes_fts FTS5 virtual table should exist after migration."""
        tables = _get_table_names(self.store._conn)
        assert "nodes_fts" in tables

    def test_get_schema_version_no_metadata_table(self):
        """get_schema_version returns 0 when metadata table doesn't exist."""
        conn = sqlite3.connect(":memory:")
        assert get_schema_version(conn) == 0
        conn.close()

    def test_get_schema_version_no_key(self):
        """get_schema_version returns 1 when metadata exists but key is missing."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.commit()
        assert get_schema_version(conn) == 1
        conn.close()

    def test_migrations_dict_covers_all_versions(self):
        """MIGRATIONS should have entries from 2 to LATEST_VERSION."""
        expected = set(range(2, LATEST_VERSION + 1))
        assert set(MIGRATIONS.keys()) == expected

    def test_run_migrations_on_already_current_db(self):
        """run_migrations should be a no-op on an already-current database."""
        version_before = get_schema_version(self.store._conn)
        run_migrations(self.store._conn)
        version_after = get_schema_version(self.store._conn)
        assert version_before == version_after == LATEST_VERSION


    def test_v6_summary_tables_exist(self):
        """v6 summary tables should exist after migration."""
        tables = _get_table_names(self.store._conn)
        assert "community_summaries" in tables
        assert "flow_snapshots" in tables
        assert "risk_index" in tables

    def test_v6_migration_idempotent(self):
        """Running v6 migration twice should not fail."""
        from code_review_graph.migrations import _migrate_v6

        _migrate_v6(self.store._conn)
        _migrate_v6(self.store._conn)
        tables = _get_table_names(self.store._conn)
        assert "community_summaries" in tables

    def test_v7_compound_edge_indexes_exist(self):
        """v7 compound edge indexes should exist after migration."""
        rows = self.store._conn.execute("PRAGMA index_list(edges)").fetchall()
        indexes = {row[1] if isinstance(row, tuple) else row["name"] for row in rows}

        assert "idx_edges_target_kind" in indexes
        assert "idx_edges_source_kind" in indexes


def _get_table_names(conn: sqlite3.Connection) -> set[str]:
    """Helper: return all table/view names in the database."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
    ).fetchall()
    return {row[0] if isinstance(row, (tuple, list)) else row["name"] for row in rows}
