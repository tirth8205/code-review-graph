"""SQLite-backed knowledge graph storage and query engine.

Stores code structure as nodes (File, Class, Function, Type, Test) and
edges (CALLS, IMPORTS_FROM, INHERITS, IMPLEMENTS, CONTAINS, TESTED_BY, DEPENDS_ON, REFERENCES).
Supports impact-radius queries and subgraph extraction.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import networkx as nx

from .constants import BFS_ENGINE, MAX_IMPACT_DEPTH, MAX_IMPACT_NODES
from .migrations import get_schema_version, run_migrations
from .parser import EdgeInfo, NodeInfo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,          -- File, Class, Function, Type, Test
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL UNIQUE,
    file_path TEXT NOT NULL,
    line_start INTEGER,
    line_end INTEGER,
    language TEXT,
    parent_name TEXT,
    params TEXT,
    return_type TEXT,
    modifiers TEXT,
    is_test INTEGER DEFAULT 0,
    file_hash TEXT,
    extra TEXT DEFAULT '{}',
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,           -- CALLS, IMPORTS_FROM, INHERITS, REFERENCES, etc.
    source_qualified TEXT NOT NULL,
    target_qualified TEXT NOT NULL,
    file_path TEXT NOT NULL,
    line INTEGER DEFAULT 0,
    extra TEXT DEFAULT '{}',
    confidence REAL DEFAULT 1.0,
    confidence_tier TEXT DEFAULT 'EXTRACTED',
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_path);
CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(kind);
CREATE INDEX IF NOT EXISTS idx_nodes_qualified ON nodes(qualified_name);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_qualified);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_qualified);
CREATE INDEX IF NOT EXISTS idx_edges_kind ON edges(kind);
CREATE INDEX IF NOT EXISTS idx_edges_target_kind ON edges(target_qualified, kind);
CREATE INDEX IF NOT EXISTS idx_edges_source_kind ON edges(source_qualified, kind);
CREATE INDEX IF NOT EXISTS idx_edges_file ON edges(file_path);
"""


@dataclass
class GraphNode:
    id: int
    kind: str
    name: str
    qualified_name: str
    file_path: str
    line_start: int
    line_end: int
    language: str
    parent_name: Optional[str]
    params: Optional[str]
    return_type: Optional[str]
    is_test: bool
    file_hash: Optional[str]
    extra: dict


@dataclass
class GraphEdge:
    id: int
    kind: str
    source_qualified: str
    target_qualified: str
    file_path: str
    line: int
    extra: dict
    confidence: float = 1.0
    confidence_tier: str = "EXTRACTED"


@dataclass
class FlowAdjacency:
    """In-memory adjacency structure for flow tracing.

    Loaded once via :meth:`GraphStore.load_flow_adjacency` and passed to
    ``trace_flows`` / ``compute_criticality`` to avoid per-edge SQLite
    point queries on large graphs.
    """
    calls_out: dict[str, list[str]]
    has_tested_by: set[str]
    nodes_by_qn: dict[str, "GraphNode"]
    nodes_by_id: dict[int, "GraphNode"]


@dataclass
class GraphStats:
    total_nodes: int
    total_edges: int
    nodes_by_kind: dict[str, int]
    edges_by_kind: dict[str, int]
    languages: list[str]
    files_count: int
    last_updated: Optional[str]


# ---------------------------------------------------------------------------
# GraphStore
# ---------------------------------------------------------------------------


class GraphStore:
    """SQLite-backed code knowledge graph."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path), timeout=30, check_same_thread=False,
            isolation_level=None,  # Disable implicit transactions (#135)
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()
        # Ensure schema_version is set, then run pending migrations
        if get_schema_version(self._conn) < 1:
            # Fresh DB — metadata table just created by _init_schema
            self._conn.execute(
                "INSERT OR IGNORE INTO metadata (key, value) "
                "VALUES ('schema_version', '1')"
            )
            self._conn.commit()
        run_migrations(self._conn)
        self._nxg_cache: nx.DiGraph | None = None
        self._cache_lock = threading.Lock()

    def __enter__(self) -> "GraphStore":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()

    def _invalidate_cache(self) -> None:
        """Invalidate the cached NetworkX graph after write operations."""
        with self._cache_lock:
            self._nxg_cache = None

    def close(self) -> None:
        self._conn.close()

    # --- Write operations ---

    def upsert_node(self, node: NodeInfo, file_hash: str = "") -> int:
        """Insert or update a node. Returns the node ID."""
        now = time.time()
        qualified = self._make_qualified(node)
        extra = json.dumps(node.extra) if node.extra else "{}"

        self._conn.execute(
            """INSERT INTO nodes
               (kind, name, qualified_name, file_path, line_start, line_end,
                language, parent_name, params, return_type, modifiers, is_test,
                file_hash, extra, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(qualified_name) DO UPDATE SET
                 kind=excluded.kind, name=excluded.name,
                 file_path=excluded.file_path, line_start=excluded.line_start,
                 line_end=excluded.line_end, language=excluded.language,
                 parent_name=excluded.parent_name, params=excluded.params,
                 return_type=excluded.return_type, modifiers=excluded.modifiers,
                 is_test=excluded.is_test, file_hash=excluded.file_hash,
                 extra=excluded.extra, updated_at=excluded.updated_at
            """,
            (
                node.kind, node.name, qualified, node.file_path,
                node.line_start, node.line_end, node.language,
                node.parent_name, node.params, node.return_type,
                node.modifiers, int(node.is_test), file_hash,
                extra, now,
            ),
        )
        row = self._conn.execute(
            "SELECT id FROM nodes WHERE qualified_name = ?", (qualified,)
        ).fetchone()
        return row["id"]

    def upsert_edge(self, edge: EdgeInfo) -> int:
        """Insert or update an edge."""
        now = time.time()
        extra_dict = edge.extra if edge.extra else {}
        confidence = float(extra_dict.get("confidence", 1.0))
        confidence_tier = str(extra_dict.get("confidence_tier", "EXTRACTED"))
        extra = json.dumps(extra_dict)

        # Check for existing edge (include line so multiple call sites are preserved)
        existing = self._conn.execute(
            """SELECT id FROM edges
               WHERE kind=? AND source_qualified=? AND target_qualified=?
                     AND file_path=? AND line=?""",
            (edge.kind, edge.source, edge.target, edge.file_path, edge.line),
        ).fetchone()

        if existing:
            self._conn.execute(
                "UPDATE edges SET line=?, extra=?, confidence=?, confidence_tier=?,"
                " updated_at=? WHERE id=?",
                (edge.line, extra, confidence, confidence_tier, now, existing["id"]),
            )
            return existing["id"]

        self._conn.execute(
            """INSERT INTO edges
               (kind, source_qualified, target_qualified, file_path, line, extra,
                confidence, confidence_tier, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (edge.kind, edge.source, edge.target, edge.file_path, edge.line, extra,
             confidence, confidence_tier, now),
        )
        return self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def remove_file_data(self, file_path: str) -> None:
        """Remove all nodes and edges associated with a file."""
        self._conn.execute("DELETE FROM nodes WHERE file_path = ?", (file_path,))
        self._conn.execute("DELETE FROM edges WHERE file_path = ?", (file_path,))
        self._invalidate_cache()

    def store_file_nodes_edges(
        self, file_path: str, nodes: list[NodeInfo], edges: list[EdgeInfo], fhash: str = ""
    ) -> None:
        """Atomically replace all data for a file."""
        if self._conn.in_transaction:
            logger.warning("Rolling back uncommitted transaction before BEGIN IMMEDIATE")
            self._conn.rollback()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self.remove_file_data(file_path)
            for node in nodes:
                self.upsert_node(node, file_hash=fhash)
            for edge in edges:
                self.upsert_edge(edge)
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        self._invalidate_cache()

    def store_file_batch(
        self, batch: list[tuple[str, list[NodeInfo], list[EdgeInfo], str]]
    ) -> None:
        """Atomically replace data for a batch of files in one transaction."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for file_path, nodes, edges, fhash in batch:
                self.remove_file_data(file_path)
                for node in nodes:
                    self.upsert_node(node, file_hash=fhash)
                for edge in edges:
                    self.upsert_edge(edge)
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        self._invalidate_cache()

    def set_metadata(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", (key, value)
        )
        self._conn.commit()

    def get_metadata(self, key: str) -> Optional[str]:
        row = self._conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        """Rollback the current transaction."""
        self._conn.rollback()

    # --- Read operations ---

    def get_node(self, qualified_name: str) -> Optional[GraphNode]:
        row = self._conn.execute(
            "SELECT * FROM nodes WHERE qualified_name = ?", (qualified_name,)
        ).fetchone()
        return self._row_to_node(row) if row else None

    def get_nodes_by_file(self, file_path: str) -> list[GraphNode]:
        rows = self._conn.execute(
            "SELECT * FROM nodes WHERE file_path = ?", (file_path,)
        ).fetchall()
        return [self._row_to_node(r) for r in rows]

    def get_all_nodes(self, exclude_files: bool = True) -> list[GraphNode]:
        """Return all nodes, optionally excluding File nodes."""
        if exclude_files:
            rows = self._conn.execute(
                "SELECT * FROM nodes WHERE kind != 'File'"
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM nodes").fetchall()
        return [self._row_to_node(r) for r in rows]

    def get_edges_by_source(self, qualified_name: str) -> list[GraphEdge]:
        rows = self._conn.execute(
            "SELECT * FROM edges WHERE source_qualified = ?", (qualified_name,)
        ).fetchall()
        return [self._row_to_edge(r) for r in rows]

    def get_edges_by_target(self, qualified_name: str) -> list[GraphEdge]:
        rows = self._conn.execute(
            "SELECT * FROM edges WHERE target_qualified = ?", (qualified_name,)
        ).fetchall()
        return [self._row_to_edge(r) for r in rows]

    def search_edges_by_target_name(self, name: str, kind: str = "CALLS") -> list[GraphEdge]:
        """Search for edges where target_qualified matches an unqualified name.

        CALLS edges often store unqualified target names (e.g. ``generateTestCode``)
        rather than fully qualified ones (``file.ts::generateTestCode``).  This
        method finds those edges by exact match on the plain function name so that
        reverse call tracing (callers_of) works even when qualified-name lookup
        returns nothing.
        """
        rows = self._conn.execute(
            "SELECT * FROM edges WHERE target_qualified = ? AND kind = ?",
            (name, kind),
        ).fetchall()
        return [self._row_to_edge(r) for r in rows]

    def get_transitive_tests(
        self, qualified_name: str, max_depth: int = 1,
    ) -> list[dict]:
        """Find tests covering a node, including indirect (transitive) coverage.

        1. Direct: TESTED_BY edges targeting this node (+ bare-name fallback).
        2. Indirect: follow outgoing CALLS edges up to *max_depth* hops,
           then collect TESTED_BY edges on each callee.

        Returns a list of dicts with node fields plus ``indirect: bool``.
        """
        conn = self._conn
        seen: set[str] = set()
        results: list[dict] = []

        # If the input is a class, expand to its methods first.
        input_qns = [qualified_name]
        row = conn.execute(
            "SELECT kind FROM nodes WHERE qualified_name = ?",
            (qualified_name,),
        ).fetchone()
        if row and row["kind"] == "Class":
            for mrow in conn.execute(
                "SELECT target_qualified FROM edges "
                "WHERE source_qualified = ? AND kind = 'CONTAINS'",
                (qualified_name,),
            ).fetchall():
                input_qns.append(mrow["target_qualified"])

        def _node_dict(qn: str, indirect: bool) -> dict | None:
            row = conn.execute(
                "SELECT * FROM nodes WHERE qualified_name = ?", (qn,)
            ).fetchone()
            if not row:
                return None
            return {
                "name": row["name"],
                "qualified_name": row["qualified_name"],
                "file_path": row["file_path"],
                "kind": row["kind"],
                "indirect": indirect,
            }

        # Direct TESTED_BY
        for qn in input_qns:
            for row in conn.execute(
                "SELECT source_qualified FROM edges "
                "WHERE target_qualified = ? AND kind = 'TESTED_BY'",
                (qn,),
            ).fetchall():
                src = row["source_qualified"]
                if src not in seen:
                    seen.add(src)
                    d = _node_dict(src, indirect=False)
                    if d:
                        results.append(d)

        # Bare-name fallback for direct
        bare = qualified_name.rsplit("::", 1)[-1] if "::" in qualified_name else qualified_name
        for row in conn.execute(
            "SELECT source_qualified FROM edges "
            "WHERE target_qualified = ? AND kind = 'TESTED_BY'",
            (bare,),
        ).fetchall():
            src = row["source_qualified"]
            if src not in seen:
                seen.add(src)
                d = _node_dict(src, indirect=False)
                if d:
                    results.append(d)

        # Transitive: follow CALLS edges, then collect TESTED_BY on callees
        frontier = set(input_qns)
        for _ in range(max_depth):
            next_frontier: set[str] = set()
            for qn in frontier:
                for row in conn.execute(
                    "SELECT target_qualified FROM edges "
                    "WHERE source_qualified = ? AND kind = 'CALLS'",
                    (qn,),
                ).fetchall():
                    next_frontier.add(row["target_qualified"])
            for callee in next_frontier:
                for row in conn.execute(
                    "SELECT source_qualified FROM edges "
                    "WHERE target_qualified = ? AND kind = 'TESTED_BY'",
                    (callee,),
                ).fetchall():
                    src = row["source_qualified"]
                    if src not in seen:
                        seen.add(src)
                        d = _node_dict(src, indirect=True)
                        if d:
                            results.append(d)
            frontier = next_frontier

        return results

    def resolve_bare_call_targets(self) -> int:
        """Batch-resolve bare-name CALLS targets using the global node table.

        After parsing, some CALLS edges have bare targets (no ``::`` separator)
        because the parser couldn't resolve cross-file.  This method matches
        them against nodes and updates unambiguous matches in-place.

        Disambiguation strategy:
          1. Single node with that name -> resolve directly
          2. Multiple candidates -> prefer one whose file is imported by the
             source file (via IMPORTS_FROM edges)

        Returns the number of resolved edges.
        """
        conn = self._conn

        bare_edges = conn.execute(
            "SELECT id, source_qualified, target_qualified, file_path "
            "FROM edges WHERE kind = 'CALLS' AND target_qualified NOT LIKE '%::%'"
        ).fetchall()
        if not bare_edges:
            return 0

        # bare_name -> list of qualified_names
        node_lookup: dict[str, list[str]] = {}
        for row in conn.execute(
            "SELECT name, qualified_name FROM nodes "
            "WHERE kind IN ('Function', 'Test', 'Class')"
        ).fetchall():
            node_lookup.setdefault(row["name"], []).append(row["qualified_name"])

        # source_file -> set of imported files (for disambiguation)
        import_targets: dict[str, set[str]] = {}
        for row in conn.execute(
            "SELECT DISTINCT file_path, target_qualified FROM edges "
            "WHERE kind = 'IMPORTS_FROM'"
        ).fetchall():
            target = row["target_qualified"]
            target_file = target.split("::", 1)[0] if "::" in target else target
            import_targets.setdefault(row["file_path"], set()).add(target_file)

        resolved = 0
        for edge in bare_edges:
            bare_name = edge["target_qualified"]
            candidates = node_lookup.get(bare_name, [])
            if not candidates:
                continue

            if len(candidates) == 1:
                qualified = candidates[0]
            else:
                # Disambiguate via imports
                src_qn = edge["source_qualified"]
                src_file = (
                    src_qn.split("::", 1)[0] if "::" in src_qn
                    else edge["file_path"]
                )
                imported_files = import_targets.get(src_file, set())
                imported = [
                    c for c in candidates
                    if c.split("::", 1)[0] in imported_files
                ]
                if len(imported) == 1:
                    qualified = imported[0]
                else:
                    continue

            conn.execute(
                "UPDATE edges SET target_qualified = ? WHERE id = ?",
                (qualified, edge["id"]),
            )
            resolved += 1

        if resolved:
            conn.commit()
            logger.info("Resolved %d bare-name CALLS targets", resolved)
        return resolved

    def get_all_files(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE kind = 'File'"
        ).fetchall()
        return [r["file_path"] for r in rows]

    def search_nodes(self, query: str, limit: int = 20) -> list[GraphNode]:
        """Keyword search across node names.

        Tries FTS5 first (fast, tokenized matching), then falls back to
        LIKE-based substring search when FTS5 returns no results.
        """
        words = query.split()
        if not words:
            return []

        # Phase 1: FTS5 search (uses the indexed nodes_fts table)
        try:
            if len(words) == 1:
                fts_query = '"' + query.replace('"', '""') + '"'
            else:
                fts_query = " AND ".join(
                    '"' + w.replace('"', '""') + '"' for w in words
                )
            rows = self._conn.execute(
                "SELECT n.* FROM nodes_fts f "
                "JOIN nodes n ON f.rowid = n.id "
                "WHERE nodes_fts MATCH ? LIMIT ?",
                (fts_query, limit),
            ).fetchall()
            if rows:
                return [self._row_to_node(r) for r in rows]
        except Exception:  # nosec B110 - FTS5 table may not exist on older schemas
            pass

        # Phase 2: LIKE fallback (substring matching)
        conditions: list[str] = []
        params: list[str | int] = []
        for word in words:
            w = word.lower()
            conditions.append(
                "(LOWER(name) LIKE ? OR LOWER(qualified_name) LIKE ?)"
            )
            params.extend([f"%{w}%", f"%{w}%"])

        where = " AND ".join(conditions)
        sql = f"SELECT * FROM nodes WHERE {where} LIMIT ?"  # nosec B608
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_node(r) for r in rows]

    # --- Impact / Graph traversal ---

    def get_impact_radius(
        self,
        changed_files: list[str],
        max_depth: int = MAX_IMPACT_DEPTH,
        max_nodes: int = MAX_IMPACT_NODES,
    ) -> dict[str, Any]:
        """BFS from changed files to find all impacted nodes within depth N.

        Delegates to ``get_impact_radius_sql()`` by default (faster for
        large graphs).  Set ``CRG_BFS_ENGINE=networkx`` to use the legacy
        Python-side BFS via NetworkX.

        Returns dict with:
          - changed_nodes: nodes in changed files
          - impacted_nodes: nodes reachable via edges
          - impacted_files: unique set of affected files
          - edges: connecting edges
        """
        if BFS_ENGINE == "networkx":
            return self._get_impact_radius_networkx(
                changed_files, max_depth=max_depth, max_nodes=max_nodes,
            )
        return self.get_impact_radius_sql(
            changed_files, max_depth=max_depth, max_nodes=max_nodes,
        )

    # -- SQLite recursive CTE version (default) ---------------------------

    def get_impact_radius_sql(
        self,
        changed_files: list[str],
        max_depth: int = MAX_IMPACT_DEPTH,
        max_nodes: int = MAX_IMPACT_NODES,
    ) -> dict[str, Any]:
        """Impact radius via SQLite recursive CTE.

        Faster than NetworkX for large graphs because it avoids
        materialising the full graph in Python.
        """
        if not changed_files:
            return {
                "changed_nodes": [],
                "impacted_nodes": [],
                "impacted_files": [],
                "edges": [],
                "truncated": False,
                "total_impacted": 0,
            }

        # Seed qualified names
        seeds: set[str] = set()
        for f in changed_files:
            nodes = self.get_nodes_by_file(f)
            for n in nodes:
                seeds.add(n.qualified_name)

        if not seeds:
            return {
                "changed_nodes": [],
                "impacted_nodes": [],
                "impacted_files": [],
                "edges": [],
                "truncated": False,
                "total_impacted": 0,
            }

        # Build recursive CTE — use a temp table for the seed set to
        # keep the query plan efficient and stay under variable limits.
        self._conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _impact_seeds "
            "(qn TEXT PRIMARY KEY)"
        )
        self._conn.execute("DELETE FROM _impact_seeds")
        batch_size = 450
        seed_list = list(seeds)
        for i in range(0, len(seed_list), batch_size):
            batch = seed_list[i:i + batch_size]
            placeholders = ",".join("(?)" for _ in batch)
            self._conn.execute(  # nosec B608
                f"INSERT OR IGNORE INTO _impact_seeds (qn) VALUES {placeholders}",
                batch,
            )

        cte_sql = """
        WITH RECURSIVE impacted(node_qn, depth) AS (
            SELECT qn, 0 FROM _impact_seeds
            UNION
            SELECT e.target_qualified, i.depth + 1
            FROM impacted i
            JOIN edges e ON e.source_qualified = i.node_qn
            WHERE i.depth < ?
            UNION
            SELECT e.source_qualified, i.depth + 1
            FROM impacted i
            JOIN edges e ON e.target_qualified = i.node_qn
            WHERE i.depth < ?
        )
        SELECT DISTINCT node_qn, MIN(depth) AS min_depth
        FROM impacted
        GROUP BY node_qn
        LIMIT ?
        """
        rows = self._conn.execute(
            cte_sql, (max_depth, max_depth, max_nodes + len(seeds)),
        ).fetchall()

        # Split into seeds vs impacted
        impacted_qns: set[str] = set()
        for r in rows:
            qn = r[0]
            if qn not in seeds:
                impacted_qns.add(qn)

        # Batch-fetch nodes
        changed_nodes = self._batch_get_nodes(seeds)
        impacted_nodes = self._batch_get_nodes(impacted_qns)

        total_impacted = len(impacted_nodes)
        truncated = total_impacted > max_nodes
        if truncated:
            impacted_nodes = impacted_nodes[:max_nodes]

        impacted_files = list({n.file_path for n in impacted_nodes})

        relevant_edges: list[GraphEdge] = []
        all_qns = seeds | {n.qualified_name for n in impacted_nodes}
        if all_qns:
            relevant_edges = self.get_edges_among(all_qns)

        return {
            "changed_nodes": changed_nodes,
            "impacted_nodes": impacted_nodes,
            "impacted_files": impacted_files,
            "edges": relevant_edges,
            "truncated": truncated,
            "total_impacted": total_impacted,
        }

    # -- NetworkX BFS version (legacy) ------------------------------------

    def _get_impact_radius_networkx(
        self,
        changed_files: list[str],
        max_depth: int = MAX_IMPACT_DEPTH,
        max_nodes: int = MAX_IMPACT_NODES,
    ) -> dict[str, Any]:
        """BFS via NetworkX (legacy). Used when CRG_BFS_ENGINE=networkx."""
        nxg = self._build_networkx_graph()

        seeds: set[str] = set()
        for f in changed_files:
            nodes = self.get_nodes_by_file(f)
            for n in nodes:
                seeds.add(n.qualified_name)

        visited: set[str] = set()
        frontier = seeds.copy()
        depth = 0
        impacted: set[str] = set()

        while frontier and depth < max_depth:
            visited.update(frontier)
            next_frontier: set[str] = set()
            for qn in frontier:
                if qn in nxg:
                    for neighbor in nxg.neighbors(qn):
                        if neighbor not in visited:
                            next_frontier.add(neighbor)
                            impacted.add(neighbor)
                if qn in nxg:
                    for pred in nxg.predecessors(qn):
                        if pred not in visited:
                            next_frontier.add(pred)
                            impacted.add(pred)
            next_frontier -= visited
            if len(visited) + len(next_frontier) > max_nodes:
                break
            frontier = next_frontier
            depth += 1

        changed_nodes = self._batch_get_nodes(seeds)
        impacted_qns = impacted - seeds
        impacted_nodes = self._batch_get_nodes(impacted_qns)

        total_impacted = len(impacted_nodes)
        truncated = total_impacted > max_nodes
        if truncated:
            impacted_nodes = impacted_nodes[:max_nodes]

        impacted_files = list({n.file_path for n in impacted_nodes})

        relevant_edges: list[GraphEdge] = []
        all_qns = seeds | {n.qualified_name for n in impacted_nodes}
        if all_qns:
            relevant_edges = self.get_edges_among(all_qns)

        return {
            "changed_nodes": changed_nodes,
            "impacted_nodes": impacted_nodes,
            "impacted_files": impacted_files,
            "edges": relevant_edges,
            "truncated": truncated,
            "total_impacted": total_impacted,
        }

    def get_subgraph(self, qualified_names: list[str]) -> dict[str, Any]:
        """Extract a subgraph containing the specified nodes and their connecting edges."""
        nodes = []
        for qn in qualified_names:
            node = self.get_node(qn)
            if node:
                nodes.append(node)

        edges = []
        qn_set = set(qualified_names)
        for qn in qualified_names:
            for e in self.get_edges_by_source(qn):
                if e.target_qualified in qn_set:
                    edges.append(e)

        return {"nodes": nodes, "edges": edges}

    def get_stats(self) -> GraphStats:
        """Return aggregate statistics about the graph."""
        total_nodes = self._conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        total_edges = self._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

        nodes_by_kind: dict[str, int] = {}
        for row in self._conn.execute("SELECT kind, COUNT(*) as cnt FROM nodes GROUP BY kind"):
            nodes_by_kind[row["kind"]] = row["cnt"]

        edges_by_kind: dict[str, int] = {}
        for row in self._conn.execute("SELECT kind, COUNT(*) as cnt FROM edges GROUP BY kind"):
            edges_by_kind[row["kind"]] = row["cnt"]

        languages = [
            r["language"] for r in self._conn.execute(
                "SELECT DISTINCT language FROM nodes WHERE language IS NOT NULL AND language != ''"
            )
        ]

        files_count = self._conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE kind = 'File'"
        ).fetchone()[0]

        last_updated = self.get_metadata("last_updated")

        return GraphStats(
            total_nodes=total_nodes,
            total_edges=total_edges,
            nodes_by_kind=nodes_by_kind,
            edges_by_kind=edges_by_kind,
            languages=languages,
            files_count=files_count,
            last_updated=last_updated,
        )

    def get_nodes_by_size(
        self,
        min_lines: int = 50,
        max_lines: int | None = None,
        kind: str | None = None,
        file_path_pattern: str | None = None,
        limit: int = 50,
    ) -> list[GraphNode]:
        """Find nodes within a line-count range, ordered largest first.

        Args:
            min_lines: Minimum line count threshold (inclusive).
            max_lines: Maximum line count threshold (inclusive). None = no upper bound.
            kind: Filter by node kind (Function, Class, File, etc.).
            file_path_pattern: SQL LIKE pattern to filter by file path.
            limit: Maximum results to return.

        Returns:
            List of GraphNode objects, ordered by line count descending.
        """
        conditions = [
            "line_start IS NOT NULL",
            "line_end IS NOT NULL",
            "(line_end - line_start + 1) >= ?",
        ]
        params: list = [min_lines]

        if max_lines is not None:
            conditions.append("(line_end - line_start + 1) <= ?")
            params.append(max_lines)
        if kind:
            conditions.append("kind = ?")
            params.append(kind)
        if file_path_pattern:
            conditions.append("file_path LIKE ?")
            params.append(f"%{file_path_pattern}%")

        params.append(limit)
        where = " AND ".join(conditions)
        rows = self._conn.execute(
            f"SELECT * FROM nodes WHERE {where} "  # nosec B608
            "ORDER BY (line_end - line_start + 1) DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._row_to_node(r) for r in rows]

    # --- Public query helpers (used by flows, changes, communities, etc.) ---

    def get_node_by_id(self, node_id: int) -> Optional[GraphNode]:
        """Fetch a single node by its integer primary key."""
        row = self._conn.execute(
            "SELECT * FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        return self._row_to_node(row) if row else None

    def get_nodes_by_kind(
        self,
        kinds: list[str],
        file_pattern: str | None = None,
    ) -> list[GraphNode]:
        """Return nodes matching any of *kinds*, optionally filtered by file.

        Args:
            kinds: List of node kind strings (e.g. ``["Function", "Test"]``).
            file_pattern: If provided, only nodes whose ``file_path``
                contains *file_pattern* (SQL LIKE ``%pattern%``) are
                returned.
        """
        if not kinds:
            return []
        placeholders = ",".join("?" for _ in kinds)
        conditions = [f"kind IN ({placeholders})"]
        params: list[str] = list(kinds)
        if file_pattern:
            conditions.append("file_path LIKE ?")
            params.append(f"%{file_pattern}%")
        where = " AND ".join(conditions)
        rows = self._conn.execute(  # nosec B608
            f"SELECT * FROM nodes WHERE {where}", params,
        ).fetchall()
        return [self._row_to_node(r) for r in rows]

    def count_flow_memberships(self, node_id: int) -> int:
        """Return the number of flows a node participates in."""
        row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM flow_memberships "
            "WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        return row["cnt"] if row else 0

    def get_flow_criticalities_for_node(self, node_id: int) -> list[float]:
        """Return criticality values for all flows a node participates in."""
        rows = self._conn.execute(
            "SELECT f.criticality FROM flows f "
            "JOIN flow_memberships fm ON fm.flow_id = f.id "
            "WHERE fm.node_id = ?",
            (node_id,),
        ).fetchall()
        return [r["criticality"] for r in rows]

    def get_node_community_id(self, node_id: int) -> int | None:
        """Return the ``community_id`` for a node, or ``None``."""
        row = self._conn.execute(
            "SELECT community_id FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        if row and row["community_id"] is not None:
            return row["community_id"]
        return None

    def get_community_ids_by_qualified_names(
        self, qns: list[str],
    ) -> dict[str, int | None]:
        """Batch-fetch ``community_id`` for a list of qualified names.

        Returns a mapping from qualified name to community_id (may be
        ``None`` if the node has no assigned community).
        """
        result: dict[str, int | None] = {}
        batch_size = 450
        for i in range(0, len(qns), batch_size):
            batch = qns[i:i + batch_size]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(  # nosec B608
                "SELECT qualified_name, community_id FROM nodes "
                f"WHERE qualified_name IN ({placeholders})",
                batch,
            ).fetchall()
            for r in rows:
                result[r["qualified_name"]] = r["community_id"]
        return result

    def get_files_matching(self, pattern: str) -> list[str]:
        """Return distinct ``file_path`` values matching a LIKE suffix."""
        rows = self._conn.execute(
            "SELECT DISTINCT file_path FROM nodes "
            "WHERE file_path LIKE ?",
            (f"%{pattern}",),
        ).fetchall()
        return [r["file_path"] for r in rows]

    def get_nodes_without_signature(self) -> list[sqlite3.Row]:
        """Return raw rows for nodes that have no signature yet."""
        return self._conn.execute(
            "SELECT id, name, kind, params, return_type "
            "FROM nodes WHERE signature IS NULL"
        ).fetchall()

    def update_node_signature(
        self, node_id: int, signature: str,
    ) -> None:
        """Set the ``signature`` column for a single node."""
        self._conn.execute(
            "UPDATE nodes SET signature = ? WHERE id = ?",
            (signature, node_id),
        )

    def get_all_community_ids(self) -> dict[str, int | None]:
        """Return a mapping of *all* qualified names to their community_id.

        Used primarily by the visualization exporter.
        """
        try:
            rows = self._conn.execute(
                "SELECT qualified_name, community_id FROM nodes"
            ).fetchall()
            return {
                r["qualified_name"]: r["community_id"]
                for r in rows
            }
        except sqlite3.OperationalError as exc:
            # community_id column may not exist yet on pre-v6 schemas
            logger.debug("Community IDs unavailable (schema not yet migrated): %s", exc)
            return {}

    def get_node_ids_by_files(
        self, file_paths: list[str],
    ) -> set[int]:
        """Return node IDs belonging to the given file paths."""
        if not file_paths:
            return set()
        result: set[int] = set()
        batch_size = 450
        for i in range(0, len(file_paths), batch_size):
            batch = file_paths[i:i + batch_size]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(  # nosec B608
                "SELECT id FROM nodes "
                f"WHERE file_path IN ({placeholders})",
                batch,
            ).fetchall()
            result.update(r["id"] for r in rows)
        return result

    def get_flow_ids_by_node_ids(
        self, node_ids: set[int],
    ) -> list[int]:
        """Return distinct flow IDs that contain any of *node_ids*."""
        if not node_ids:
            return []
        nids = list(node_ids)
        result: list[int] = []
        batch_size = 450
        for i in range(0, len(nids), batch_size):
            batch = nids[i:i + batch_size]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(  # nosec B608
                "SELECT DISTINCT flow_id FROM flow_memberships "
                f"WHERE node_id IN ({placeholders})",
                batch,
            ).fetchall()
            result.extend(r["flow_id"] for r in rows)
        # Deduplicate across batches
        return list(dict.fromkeys(result))

    def get_flow_qualified_names(self, flow_id: int) -> set[str]:
        """Return the set of qualified names for nodes in a flow."""
        rows = self._conn.execute(
            "SELECT n.qualified_name FROM flow_memberships fm "
            "JOIN nodes n ON fm.node_id = n.id WHERE fm.flow_id = ?",
            (flow_id,),
        ).fetchall()
        return {r["qualified_name"] for r in rows}

    def get_node_kind_by_id(self, node_id: int) -> str | None:
        """Return just the ``kind`` column for a node, or ``None``."""
        row = self._conn.execute(
            "SELECT kind FROM nodes WHERE id = ?", (node_id,),
        ).fetchone()
        return row["kind"] if row else None

    def get_all_call_targets(self, include_file_sources: bool = True) -> set[str]:
        """Return the set of all CALLS-edge target qualified names.

        When ``include_file_sources`` is False, CALLS edges whose source is a
        File node (module-scope calls from top-level script glue, CLI
        entrypoints, or notebook cells) are excluded. Callers that treat "has
        an incoming call" as "is not a root" (e.g. entry-point detection)
        should pass ``include_file_sources=False`` — otherwise a script-only
        callee looks called and is hidden from flow analysis.

        The File-node filter joins against ``nodes.kind`` rather than pattern-
        matching ``source_qualified`` so that file paths containing ``::`` or
        any future change to the File-node naming convention cannot silently
        miscategorize edges.
        """
        if include_file_sources:
            rows = self._conn.execute(
                "SELECT DISTINCT target_qualified FROM edges "
                "WHERE kind = 'CALLS'"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT DISTINCT e.target_qualified FROM edges e "
                "LEFT JOIN nodes n ON n.qualified_name = e.source_qualified "
                "WHERE e.kind = 'CALLS' "
                "AND (n.kind IS NULL OR n.kind != 'File')"
            ).fetchall()
        return {r["target_qualified"] for r in rows}

    def get_communities_list(
        self,
    ) -> list[sqlite3.Row]:
        """Return raw rows from the ``communities`` table."""
        try:
            return self._conn.execute(
                "SELECT id, name FROM communities"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            # communities table doesn't exist yet on pre-v4 schemas
            logger.debug("Communities list unavailable (table missing): %s", exc)
            return []

    def get_community_member_qns(
        self, community_id: int,
    ) -> list[str]:
        """Return qualified names of nodes in a community."""
        rows = self._conn.execute(
            "SELECT qualified_name FROM nodes "
            "WHERE community_id = ?",
            (community_id,),
        ).fetchall()
        return [r["qualified_name"] for r in rows]

    def get_nodes_by_community_id(
        self, community_id: int,
    ) -> list[GraphNode]:
        """Return all nodes belonging to a community."""
        rows = self._conn.execute(
            "SELECT * FROM nodes WHERE community_id = ?",
            (community_id,),
        ).fetchall()
        return [self._row_to_node(r) for r in rows]

    def get_outgoing_targets(
        self, source_qns: list[str],
    ) -> list[str]:
        """Return ``target_qualified`` for edges sourced from *source_qns*."""
        results: list[str] = []
        batch_size = 450
        for i in range(0, len(source_qns), batch_size):
            batch = source_qns[i:i + batch_size]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(  # nosec B608
                "SELECT target_qualified FROM edges "
                f"WHERE source_qualified IN ({placeholders})",
                batch,
            ).fetchall()
            results.extend(r["target_qualified"] for r in rows)
        return results

    def get_incoming_sources(
        self, target_qns: list[str],
    ) -> list[str]:
        """Return ``source_qualified`` for edges targeting *target_qns*."""
        results: list[str] = []
        batch_size = 450
        for i in range(0, len(target_qns), batch_size):
            batch = target_qns[i:i + batch_size]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(  # nosec B608
                "SELECT source_qualified FROM edges "
                f"WHERE target_qualified IN ({placeholders})",
                batch,
            ).fetchall()
            results.extend(r["source_qualified"] for r in rows)
        return results

    # --- Public edge access (for visualization etc.) ---

    def get_all_edges(self) -> list[GraphEdge]:
        """Return all edges in the graph."""
        rows = self._conn.execute("SELECT * FROM edges").fetchall()
        return [self._row_to_edge(r) for r in rows]

    def get_edges_among(self, qualified_names: set[str]) -> list[GraphEdge]:
        """Return edges where both source and target are in the given set.

        Batches the source-side IN clause to stay under SQLite's default
        SQLITE_MAX_VARIABLE_NUMBER limit, then filters targets in Python.
        """
        if not qualified_names:
            return []
        qns = list(qualified_names)
        results: list[GraphEdge] = []
        batch_size = 450  # Stay well under SQLite's default 999 limit
        for i in range(0, len(qns), batch_size):
            batch = qns[i:i + batch_size]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(  # nosec B608
                f"SELECT * FROM edges WHERE source_qualified IN ({placeholders})",
                batch,
            ).fetchall()
            for r in rows:
                edge = self._row_to_edge(r)
                if edge.target_qualified in qualified_names:
                    results.append(edge)
        return results

    def _batch_get_nodes(self, qualified_names: set[str]) -> list[GraphNode]:
        """Batch-fetch nodes by qualified name, staying under SQLite variable limits."""
        if not qualified_names:
            return []
        qns = list(qualified_names)
        results: list[GraphNode] = []
        batch_size = 450
        for i in range(0, len(qns), batch_size):
            batch = qns[i:i + batch_size]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(  # nosec B608
                f"SELECT * FROM nodes WHERE qualified_name IN ({placeholders})",
                batch,
            ).fetchall()
            results.extend(self._row_to_node(r) for r in rows)
        return results

    def load_flow_adjacency(self) -> "FlowAdjacency":
        """Load all nodes and CALLS/TESTED_BY edges into memory for fast traversal.

        Reads the entire ``nodes`` and ``edges`` tables in two streaming
        queries and returns an in-memory adjacency structure suitable for
        flow tracing and criticality scoring.  At ~500k nodes / 3M edges
        this fits in a few hundred MB and eliminates tens of millions of
        single-row SQLite point queries that otherwise dominate
        ``trace_flows`` / ``compute_criticality`` runtime.
        """
        nodes_by_qn: dict[str, GraphNode] = {}
        nodes_by_id: dict[int, GraphNode] = {}
        for row in self._conn.execute("SELECT * FROM nodes"):
            node = self._row_to_node(row)
            nodes_by_qn[node.qualified_name] = node
            nodes_by_id[node.id] = node

        calls_out: dict[str, list[str]] = {}
        has_tested_by: set[str] = set()
        for row in self._conn.execute(
            "SELECT kind, source_qualified, target_qualified FROM edges "
            "WHERE kind IN ('CALLS', 'TESTED_BY')"
        ):
            kind, src, tgt = row["kind"], row["source_qualified"], row["target_qualified"]
            if kind == "CALLS":
                calls_out.setdefault(src, []).append(tgt)
            else:  # TESTED_BY
                has_tested_by.add(tgt)

        return FlowAdjacency(
            calls_out=calls_out,
            has_tested_by=has_tested_by,
            nodes_by_qn=nodes_by_qn,
            nodes_by_id=nodes_by_id,
        )

    # --- Internal helpers ---

    def _build_networkx_graph(self) -> nx.DiGraph:
        """Build (or return cached) in-memory NetworkX directed graph from all edges."""
        with self._cache_lock:
            if self._nxg_cache is not None:
                return self._nxg_cache
            g: nx.DiGraph = nx.DiGraph()
            rows = self._conn.execute("SELECT * FROM edges").fetchall()
            for r in rows:
                g.add_edge(r["source_qualified"], r["target_qualified"], kind=r["kind"])
            self._nxg_cache = g
            return g

    def _make_qualified(self, node: NodeInfo) -> str:
        if node.kind == "File":
            return node.file_path
        if node.parent_name:
            return f"{node.file_path}::{node.parent_name}.{node.name}"
        return f"{node.file_path}::{node.name}"

    def _row_to_node(self, row: sqlite3.Row) -> GraphNode:
        return GraphNode(
            id=row["id"],
            kind=row["kind"],
            name=row["name"],
            qualified_name=row["qualified_name"],
            file_path=row["file_path"],
            line_start=row["line_start"],
            line_end=row["line_end"],
            language=row["language"] or "",
            parent_name=row["parent_name"],
            params=row["params"],
            return_type=row["return_type"],
            is_test=bool(row["is_test"]),
            file_hash=row["file_hash"],
            extra=json.loads(row["extra"]) if row["extra"] else {},
        )

    def _row_to_edge(self, row: sqlite3.Row) -> GraphEdge:
        extra = json.loads(row["extra"]) if row["extra"] else {}
        confidence = row["confidence"] if "confidence" in row.keys() else 1.0
        confidence_tier = row["confidence_tier"] if "confidence_tier" in row.keys() else "EXTRACTED"
        return GraphEdge(
            id=row["id"],
            kind=row["kind"],
            source_qualified=row["source_qualified"],
            target_qualified=row["target_qualified"],
            file_path=row["file_path"],
            line=row["line"],
            extra=extra,
            confidence=confidence,
            confidence_tier=confidence_tier,
        )


def _sanitize_name(s: str, max_len: int = 256) -> str:
    """Strip ASCII control characters and truncate to prevent prompt injection.

    Node names extracted from source code could contain adversarial strings
    (e.g. ``IGNORE_ALL_PREVIOUS_INSTRUCTIONS``).  This function removes control
    characters (0x00-0x1F except tab and newline) and enforces a length limit so
    that names flowing through MCP tool responses cannot easily influence AI
    agent behaviour.
    """
    # Strip control chars 0x00-0x1F except \t (0x09) and \n (0x0A)
    cleaned = "".join(
        ch for ch in s
        if ch in ("\t", "\n") or ord(ch) >= 0x20
    )
    return cleaned[:max_len]


def node_to_dict(n: GraphNode) -> dict:
    return {
        "id": n.id, "kind": n.kind, "name": _sanitize_name(n.name),
        "qualified_name": _sanitize_name(n.qualified_name), "file_path": n.file_path,
        "line_start": n.line_start, "line_end": n.line_end,
        "language": n.language,
        "parent_name": _sanitize_name(n.parent_name) if n.parent_name else n.parent_name,
        "is_test": n.is_test,
    }


def edge_to_dict(e: GraphEdge) -> dict:
    return {
        "id": e.id, "kind": e.kind,
        "source": _sanitize_name(e.source_qualified),
        "target": _sanitize_name(e.target_qualified),
        "file_path": e.file_path, "line": e.line,
        "confidence": e.confidence, "confidence_tier": e.confidence_tier,
    }
