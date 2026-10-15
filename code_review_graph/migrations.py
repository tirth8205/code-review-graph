"""Schema migration framework for the code-review-graph SQLite database.

Manages incremental schema changes via versioned migration functions.
Each migration is idempotent (uses IF NOT EXISTS / column existence checks).

Idempotence used to be "check, then act", which is only safe for one process.
A watcher child, a PostToolUse hook and a hand-run ``build`` can all open the
same fresh ``graph.db`` at once, and every one of them runs the pending
migrations from :meth:`GraphStore.__init__`.  Two openers could both see a
column missing, both issue the ``ALTER``, and the loser died with
``duplicate column name: signature`` before its caller had a database at all.

The check is therefore a fast path only.  The act itself is now idempotent at
the SQL level: :func:`_apply` treats "already exists" as success, so whichever
opener loses the race simply adopts the winner's schema.  Contention on the
write lock is retried rather than raised, because a migration is a handful of
statements and the other process is about to finish.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Callable

logger = logging.getLogger(__name__)

# The whole migration set is a few DDL statements; a peer running it holds the
# write lock for milliseconds.  Retrying is cheaper, and far less confusing,
# than handing "database is locked" back out of GraphStore.__init__.
_MAX_ATTEMPTS = 5
_RETRY_BASE_SECONDS = 0.05

# Substrings of the SQLite errors that mean "another process already applied
# this exact change".  They are outcomes, not failures.
_ALREADY_APPLIED = (
    "duplicate column name",
    "already exists",
)

# Substrings that mean "someone else is writing; nothing was applied".
_CONTENDED = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
    "cannot start a transaction within a transaction",
)


def _matches(exc: sqlite3.Error, needles: tuple[str, ...]) -> bool:
    message = str(exc).lower()
    return any(needle in message for needle in needles)


def _apply(conn: sqlite3.Connection, sql: str, *, what: str) -> bool:
    """Execute one DDL statement, tolerating a peer that already applied it.

    Returns True when this process made the change, False when it found the
    change already present.  Any other error is re-raised: a migration that
    fails for a real reason must still stop the opener.
    """
    try:
        conn.execute(sql)
    except sqlite3.OperationalError as exc:
        if _matches(exc, _ALREADY_APPLIED):
            logger.debug("%s already applied by another process", what)
            return False
        raise
    return True


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Read the current schema version from the metadata table.

    Returns:
        int: The schema version (0 if metadata table doesn't exist, 1 if not set).
    """
    try:
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            return 1
        return int(row[0] if isinstance(row, (tuple, list)) else row["value"])
    except sqlite3.OperationalError:
        # metadata table doesn't exist
        return 0


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    """Set the schema version in the metadata table."""
    conn.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES ('schema_version', ?)",
        (str(version),),
    )


_KNOWN_TABLES = frozenset({
    "nodes", "edges", "metadata", "communities", "flows", "flow_memberships", "nodes_fts",
    "community_summaries", "flow_snapshots", "risk_index", "nodes_fts_state",
})


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Check if a column exists in a table."""
    if table not in _KNOWN_TABLES:
        raise ValueError(f"Unknown table: {table}")
    cursor = conn.execute(f"PRAGMA table_info({table})")  # noqa: S608
    columns = [row[1] if isinstance(row, tuple) else row["name"] for row in cursor]
    return column in columns


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Check if a table exists."""
    if table not in _KNOWN_TABLES:
        raise ValueError(f"Unknown table: {table}")
    row = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type IN ('table', 'view') "
        "AND name = ?",
        (table,),
    ).fetchone()
    return row[0] > 0


# The columns ``nodes_fts`` indexes, in table order. Shared with
# ``search`` so the DDL, the BM25 weight vector, the index-state mirror and
# the delta delete/insert statements cannot drift apart. Order is
# load-bearing: ``bm25()`` takes one weight per column positionally, and an
# external-content table can only name columns that exist on ``nodes``.
NODES_FTS_COLUMNS: tuple[str, ...] = (
    "name",
    "qualified_name",
    "file_path",
    "signature",
    "docstring",
    "name_tokens",
)

# The current shape of the FTS5 index over ``nodes``. Shared with
# ``search.rebuild_fts_index`` so the DDL cannot drift between the migration
# that creates the table and the rebuild that recreates it.
NODES_FTS_DDL = """
CREATE VIRTUAL TABLE nodes_fts USING fts5(
    {columns},
    content='nodes', content_rowid='rowid',
    tokenize='porter unicode61'
)
""".format(columns=", ".join(NODES_FTS_COLUMNS))

# ``nodes_fts`` is an external-content table, so removing an entry needs the
# column values that were indexed, and those are gone once the node row is.
# ``nodes_fts_state`` mirrors them. It therefore has to carry exactly the
# columns ``NODES_FTS_COLUMNS`` names; v12 created the first four and v13
# adds the rest, so the shape is reconciled by column name, not by version.
NODES_FTS_STATE_TABLE = "nodes_fts_state"

# Bumped whenever the mirror's shape changes: a mirror written under an
# older value describes an index with fewer columns, so it cannot be used to
# delete from the current one. ``search.update_fts_index`` reads this and
# falls back to one full rebuild, which rewrites both sides together.
FTS_STATE_METADATA_KEY = "fts_state_synced"
FTS_STATE_VERSION = "2"


def ensure_nodes_fts_state(conn: sqlite3.Connection) -> None:
    """Create or widen ``nodes_fts_state`` to mirror every indexed column.

    Idempotent, and safe on a database created by any earlier version: the
    table is created when missing and otherwise gains only the columns it
    does not already have.
    """
    columns = ", ".join(f"{name} TEXT" for name in NODES_FTS_COLUMNS)
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {NODES_FTS_STATE_TABLE} ("  # nosec B608
        f"  node_id INTEGER PRIMARY KEY, {columns})"
    )
    existing = {
        row[1] for row in conn.execute(
            f"PRAGMA table_info({NODES_FTS_STATE_TABLE})"  # nosec B608
        )
    }
    for name in NODES_FTS_COLUMNS:
        if name not in existing:
            conn.execute(
                f"ALTER TABLE {NODES_FTS_STATE_TABLE} "  # nosec B608
                f"ADD COLUMN {name} TEXT"
            )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_nodes_fts_state_file "
        f"ON {NODES_FTS_STATE_TABLE}(file_path)"  # nosec B608
    )


# Rows updated per executemany() batch in the v13 backfill.
_BACKFILL_BATCH = 5_000


# ---------------------------------------------------------------------------
# Migration functions
# ---------------------------------------------------------------------------


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """v2: Add signature column to nodes table."""
    if not _has_column(conn, "nodes", "signature"):
        if _apply(
            conn,
            "ALTER TABLE nodes ADD COLUMN signature TEXT",
            what="nodes.signature",
        ):
            logger.info("Migration v2: added 'signature' column to nodes")


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """v3: Create flows and flow_memberships tables."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS flows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            entry_point_id INTEGER NOT NULL,
            depth INTEGER NOT NULL,
            node_count INTEGER NOT NULL,
            file_count INTEGER NOT NULL,
            criticality REAL NOT NULL DEFAULT 0.0,
            path_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS flow_memberships (
            flow_id INTEGER NOT NULL,
            node_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            PRIMARY KEY (flow_id, node_id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flows_criticality ON flows(criticality DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flows_entry ON flows(entry_point_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flow_memberships_node ON flow_memberships(node_id)"
    )
    logger.info("Migration v3: created flows and flow_memberships tables")


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """v4: Create communities table, add community_id to nodes."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS communities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            level INTEGER NOT NULL DEFAULT 0,
            parent_id INTEGER,
            cohesion REAL NOT NULL DEFAULT 0.0,
            size INTEGER NOT NULL DEFAULT 0,
            dominant_language TEXT,
            description TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    if not _has_column(conn, "nodes", "community_id"):
        if _apply(
            conn,
            "ALTER TABLE nodes ADD COLUMN community_id INTEGER",
            what="nodes.community_id",
        ):
            logger.info("Migration v4: added 'community_id' column to nodes")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_nodes_community ON nodes(community_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_communities_parent ON communities(parent_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_communities_cohesion ON communities(cohesion DESC)"
    )
    logger.info("Migration v4: created communities table")


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """v5: Create FTS5 virtual table for nodes."""
    if not _table_exists(conn, "nodes_fts"):
        if _apply(
            conn,
            """
            CREATE VIRTUAL TABLE nodes_fts USING fts5(
                name, qualified_name, file_path, signature,
                content='nodes', content_rowid='rowid',
                tokenize='porter unicode61'
            )
            """,
            what="nodes_fts",
        ):
            logger.info("Migration v5: created nodes_fts FTS5 virtual table")


def _migrate_v6(conn: sqlite3.Connection) -> None:
    """v6: Add pre-computed summary tables for token-efficient queries."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS community_summaries (
            community_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            purpose TEXT DEFAULT '',
            key_symbols TEXT DEFAULT '[]',
            risk TEXT DEFAULT 'unknown',
            size INTEGER DEFAULT 0,
            dominant_language TEXT DEFAULT '',
            FOREIGN KEY (community_id) REFERENCES communities(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS flow_snapshots (
            flow_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            entry_point TEXT NOT NULL,
            critical_path TEXT DEFAULT '[]',
            criticality REAL DEFAULT 0.0,
            node_count INTEGER DEFAULT 0,
            file_count INTEGER DEFAULT 0,
            FOREIGN KEY (flow_id) REFERENCES flows(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS risk_index (
            node_id INTEGER PRIMARY KEY,
            qualified_name TEXT NOT NULL,
            risk_score REAL DEFAULT 0.0,
            caller_count INTEGER DEFAULT 0,
            test_coverage TEXT DEFAULT 'unknown',
            security_relevant INTEGER DEFAULT 0,
            last_computed TEXT DEFAULT '',
            FOREIGN KEY (node_id) REFERENCES nodes(id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_risk_index_score "
        "ON risk_index(risk_score DESC)"
    )
    logger.info("Migration v6: created summary tables "
                "(community_summaries, flow_snapshots, risk_index)")


def _migrate_v7(conn: sqlite3.Connection) -> None:
    """v7: Add compound edge indexes for summary and risk queries."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_edges_target_kind "
        "ON edges(target_qualified, kind)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_edges_source_kind "
        "ON edges(source_qualified, kind)"
    )
    logger.info("Migration v7: added compound edge indexes")


def _migrate_v8(conn: sqlite3.Connection) -> None:
    """v8: Add composite index on edges for upsert_edge performance.

    ``edges.line`` comes from the base schema, which ``_init_schema`` applies
    before the migrations run.  A database that predates it (or one built by
    something other than ``GraphStore``) would otherwise fail the whole open
    with ``no such column: line`` — an index is a speed-up, never a reason to
    refuse to open the graph.
    """
    if not _has_column(conn, "edges", "line"):
        logger.info("Migration v8: skipped, edges.line is not present")
        return
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_edges_composite
        ON edges(kind, source_qualified, target_qualified, file_path, line)
    """)
    logger.info("Migration v8: created composite edge index")


def _migrate_v9(conn: sqlite3.Connection) -> None:
    """v9: Add confidence scoring to edges."""
    if not _has_column(conn, "edges", "confidence"):
        _apply(
            conn,
            "ALTER TABLE edges ADD COLUMN confidence REAL DEFAULT 1.0",
            what="edges.confidence",
        )
    if not _has_column(conn, "edges", "confidence_tier"):
        _apply(
            conn,
            "ALTER TABLE edges ADD COLUMN confidence_tier TEXT DEFAULT 'EXTRACTED'",
            what="edges.confidence_tier",
        )
    logger.info("Migration v9: added edge confidence columns")


def _migrate_v10(conn: sqlite3.Connection) -> None:
    """v10: Add the indexed ``nodes.symbol`` column used by tail lookups.

    ``symbol`` holds the portion of ``qualified_name`` after the first ``::``.
    Storing it lets a dotted-target lookup be an indexed equality test; the
    previous ``substr(qualified_name, -n)`` predicate could not use any index
    and scanned the whole table on every dotted symbol query.
    """
    if not _has_column(conn, "nodes", "symbol"):
        _apply(
            conn,
            "ALTER TABLE nodes ADD COLUMN symbol TEXT",
            what="nodes.symbol",
        )
    # instr() returns 0 when "::" is absent, in which case the qualified name
    # is already a bare symbol. Mirrors graph._symbol_of.
    conn.execute(
        "UPDATE nodes SET symbol = CASE "
        "WHEN instr(qualified_name, '::') > 0 "
        "THEN substr(qualified_name, instr(qualified_name, '::') + 2) "
        "ELSE qualified_name END"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_nodes_symbol ON nodes(symbol)"
    )
    logger.info("Migration v10: added indexed nodes.symbol column")


# SQL that classifies one CALLS/REFERENCES edge by whether its target names a
# node the graph actually indexed. Kept here and imported by graph.py so the
# migration backfill, the post-build refresh and the read-path fallback can
# never drift apart.
TARGET_RESOLUTION_KINDS = ("CALLS", "REFERENCES")

_TARGET_RESOLUTION_TEMPLATE = (
    "CASE WHEN EXISTS (SELECT 1 FROM nodes crg_res_n "
    "WHERE crg_res_n.qualified_name = {alias}.target_qualified) "
    "THEN 'direct' ELSE 'unresolved' END"
)

# Only these two spellings of the edges table may be interpolated, so no
# caller-supplied string ever reaches the SQL text.
_ALLOWED_EDGE_ALIASES = frozenset({"edges", "e"})


def target_resolution_expr(alias: str = "edges") -> str:
    """Return the classification expression bound to one ``edges`` alias."""
    if alias not in _ALLOWED_EDGE_ALIASES:
        raise ValueError(f"Unsupported edges alias: {alias!r}")
    return _TARGET_RESOLUTION_TEMPLATE.format(alias=alias)


TARGET_RESOLUTION_EXPR = target_resolution_expr()


def _migrate_v11(conn: sqlite3.Connection) -> None:
    """v11: Add the indexed ``edges.target_resolution`` column.

    A CALLS edge either points at an indexed node or carries a bare name that
    the read path can only match by name, subject to a receiver-evidence
    check. Both kinds lived in one undifferentiated pile, so an answer could
    not say how much of itself was certain. Storing the classification makes
    the split an indexed group-by rather than a scan that re-derives it, and
    lets impact analysis exclude the guessed hops.

    ``IMPORTS_FROM`` and the other kinds keep NULL: their targets are file
    paths and module names, for which "unresolved" would be a false claim.
    """
    if not _has_column(conn, "edges", "target_resolution"):
        conn.execute("ALTER TABLE edges ADD COLUMN target_resolution TEXT")
    placeholders = ", ".join("?" for _ in TARGET_RESOLUTION_KINDS)
    conn.execute(
        f"UPDATE edges SET target_resolution = {TARGET_RESOLUTION_EXPR} "  # noqa: S608
        f"WHERE kind IN ({placeholders})",
        TARGET_RESOLUTION_KINDS,
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_edges_kind_target_resolution "
        "ON edges(kind, target_resolution)"
    )
    logger.info("Migration v11: added indexed edges.target_resolution column")


def _migrate_v12(conn: sqlite3.Connection) -> None:
    """v12: Add ``nodes_fts_state``, the mirror of what ``nodes_fts`` holds.

    ``nodes_fts`` is an external content table, so deleting one of its
    entries requires the column values that were indexed, and those are
    gone once the node row is deleted.  Mirroring the indexed text here lets
    an incremental update rewrite just the rows that changed instead of
    dropping and repopulating the whole index.  The table starts empty; the
    first index sync after the migration fills it with a full rebuild.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS nodes_fts_state ("
        "  node_id INTEGER PRIMARY KEY,"
        "  name TEXT,"
        "  qualified_name TEXT,"
        "  file_path TEXT,"
        "  signature TEXT"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_nodes_fts_state_file "
        "ON nodes_fts_state(file_path)"
    )
    logger.info("Migration v12: added nodes_fts_state index mirror")


def _migrate_v13(conn: sqlite3.Connection) -> None:
    """v13: index node docstrings and identifier word splits in FTS5.

    ``nodes.extra['docstring']`` was already extracted by the parser and fed
    to the embedding text builder, but the FTS5 index could not see it: an
    external-content table can only index real columns of its content table.
    Two columns are added and backfilled, then ``nodes_fts`` is recreated
    with both so existing graphs gain the prose without a full reparse.

    ``name_tokens`` carries the camelCase/PascalCase splits that the
    ``unicode61`` tokenizer cannot produce (``hashPassword`` is one token to
    it, so ``password`` never matched).

    The v12 index-state mirror is widened to the same two columns and
    refilled from the rebuild this migration just ran, so the incremental
    delta path keeps the values it needs to delete an entry with.
    """
    # Imported here, not at module scope: graph imports migrations, so the
    # reverse edge can only be taken at call time. run_migrations is invoked
    # from GraphStore.__init__, by which point graph is fully imported.
    from .graph import node_index_tokens

    if not _has_column(conn, "nodes", "docstring"):
        conn.execute("ALTER TABLE nodes ADD COLUMN docstring TEXT")
    if not _has_column(conn, "nodes", "name_tokens"):
        conn.execute("ALTER TABLE nodes ADD COLUMN name_tokens TEXT")

    # The docstring backfill is pure SQL; json_extract returns NULL for rows
    # whose extra holds no docstring, which is exactly the wanted value.
    conn.execute(
        "UPDATE nodes SET docstring = "
        "substr(trim(json_extract(extra, '$.docstring')), 1, 400) "
        "WHERE docstring IS NULL AND extra IS NOT NULL AND json_valid(extra) "
        "AND json_type(extra, '$.docstring') = 'text'"
    )

    # The camelCase split has no SQL equivalent, so it runs in Python once.
    pending: list[tuple[str, int]] = []
    cursor = conn.execute(
        "SELECT id, kind, name, parent_name, file_path FROM nodes"
    )
    for row in cursor.fetchall():
        node_id, kind, name, parent, path = (row[0], row[1], row[2], row[3], row[4])
        pending.append((node_index_tokens(kind, name, parent, path), node_id))
        if len(pending) >= _BACKFILL_BATCH:
            conn.executemany(
                "UPDATE nodes SET name_tokens = ? WHERE id = ?", pending
            )
            pending.clear()
    if pending:
        conn.executemany("UPDATE nodes SET name_tokens = ? WHERE id = ?", pending)

    conn.execute("DROP TABLE IF EXISTS nodes_fts")
    conn.execute(NODES_FTS_DDL)
    conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")

    # The index now carries two columns the v12 mirror has no room for, and
    # an entry can only be deleted by replaying every indexed value. Widen
    # the mirror and refill it from the rebuild that just ran.
    ensure_nodes_fts_state(conn)
    columns = ", ".join(NODES_FTS_COLUMNS)
    conn.execute(f"DELETE FROM {NODES_FTS_STATE_TABLE}")  # nosec B608
    conn.execute(
        f"INSERT INTO {NODES_FTS_STATE_TABLE} (node_id, {columns}) "  # nosec B608
        f"SELECT id, {columns} FROM nodes"
    )
    conn.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
        (FTS_STATE_METADATA_KEY, FTS_STATE_VERSION),
    )
    logger.info("Migration v13: indexed nodes.docstring and nodes.name_tokens")


def _migrate_v14(conn: sqlite3.Connection) -> None:
    """v14: invalidate indexed Ruby files so the next update re-keys them.

    Ruby class identities changed. A class declared inside any namespace now
    carries that namespace in ``parent_name``, and its methods qualify against
    the class's own key, so ``Auth::User#to_s`` moves from ``user.rb::User.to_s``
    to ``user.rb::Auth.User.to_s``. Both the compact spelling
    (``class Auth::User``) and the nested one are affected.

    Without this, an incremental ``update`` keeps the old rows for every Ruby
    file whose content has not changed and writes the new shape only for files
    that happen to be touched, leaving one database holding both. Nothing
    reports an error in that state: ``children_of`` simply answers from
    whichever shape a file was last indexed under, so the damage is silent.

    The fix is to clear ``file_hash`` on the Ruby File nodes rather than to
    delete any row. ``get_file_hashes`` reads that column to decide what has
    changed, so an empty hash can never match the file on disk and the next
    update re-parses it and replaces its rows through the normal path.

    Deleting the rows here would work too, and is the obvious reading of
    "force a reparse", but it breaks the invariant that opening a database
    only migrates it: ``test_opening_loses_no_nodes_or_edges`` requires the
    node and edge counts to be identical after open, because nothing has been
    re-parsed yet. Invalidating the hash defers the change to the update that
    does the parsing, which is where the upgrade path already tolerates churn.

    Rewriting the identities in place is not an option either: the namespace
    the new key needs was never recorded, so a rewrite would have to guess it.
    """
    updated = conn.execute(
        "UPDATE nodes SET file_hash = '' "
        "WHERE kind = 'File' AND language = 'ruby' "
        "AND file_hash IS NOT NULL AND file_hash != ''",
    ).rowcount
    logger.info(
        "Migration v14: invalidated %d Ruby file(s) for re-keying on next update",
        updated,
    )



# ---------------------------------------------------------------------------
# Migration registry
# ---------------------------------------------------------------------------

MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _migrate_v2,
    3: _migrate_v3,
    4: _migrate_v4,
    5: _migrate_v5,
    6: _migrate_v6,
    7: _migrate_v7,
    8: _migrate_v8,
    9: _migrate_v9,
    10: _migrate_v10,
    11: _migrate_v11,
    12: _migrate_v12,
    13: _migrate_v13,
    14: _migrate_v14,
}

LATEST_VERSION = max(MIGRATIONS.keys())


def _run_one(conn: sqlite3.Connection, version: int) -> None:
    """Apply a single migration, surviving a peer doing the same thing.

    Two kinds of failure are not failures here:

    * the change is already present, because another opener won the race —
      :func:`_apply` absorbs those at the statement that hits them, so the
      rest of the migration still runs and nothing is half-applied;
    * the write lock is held, because another opener is mid-migration — the
      attempt is retried, by which time the peer has committed and every
      statement takes the "already present" path.

    Anything else is re-raised.
    """
    last: sqlite3.Error | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            MIGRATIONS[version](conn)
            _set_schema_version(conn, version)
            conn.commit()
            return
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if not _matches(exc, _CONTENDED):
                logger.error("Migration v%d failed", version, exc_info=True)
                raise
            last = exc
            # Backoff jittered by PID, so two openers that started together do
            # not keep retrying in lockstep. Derived from the PID rather than
            # a random source because this needs spread, not unpredictability.
            jitter = 0.5 + (os.getpid() % 101) / 100.0
            time.sleep(_RETRY_BASE_SECONDS * (2**attempt) * jitter)
        except sqlite3.Error:
            conn.rollback()
            logger.error("Migration v%d failed, rolling back", version, exc_info=True)
            raise
    logger.error("Migration v%d could not get the write lock", version)
    raise last if last is not None else sqlite3.OperationalError("database is locked")


def run_migrations(conn: sqlite3.Connection) -> None:
    """Run all pending migrations in order.

    Safe to run from several processes at once: each statement tolerates a
    peer having already applied it, and lock contention is retried rather
    than raised.  The schema_version metadata entry is updated after each
    successful migration.
    """
    current = get_schema_version(conn)
    if current >= LATEST_VERSION:
        return

    logger.info("Schema version %d -> %d: running migrations", current, LATEST_VERSION)

    for version in sorted(MIGRATIONS.keys()):
        if version <= current:
            continue
        logger.info("Running migration v%d", version)
        _run_one(conn, version)

    logger.info("Migrations complete, now at schema version %d", LATEST_VERSION)
