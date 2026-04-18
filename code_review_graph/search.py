"""Hybrid search engine combining FTS5 (BM25) and vector embeddings.

Uses Reciprocal Rank Fusion (RRF) to merge results from full-text search
and semantic similarity, with query-aware kind boosting and context-file
boosting for relevance tuning.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any, Optional

from .graph import GraphStore, _sanitize_name

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FTS5 index management
# ---------------------------------------------------------------------------


def rebuild_fts_index(store: GraphStore) -> int:
    """Rebuild the FTS5 index from the nodes table.

    Checks whether the ``nodes_fts`` virtual table exists, clears it, then
    repopulates it from every row in ``nodes``.

    Returns:
        Number of rows indexed.
    """
    # NOTE: rebuild_fts_index uses store._conn directly because it manages
    # the FTS5 virtual table DDL, which is tightly coupled to SQLite internals.
    conn = store._conn

    # Wrap the full DROP + CREATE + INSERT sequence in an explicit transaction
    # so a crash mid-rebuild cannot leave the DB without an FTS table at all
    # (DROP succeeded but CREATE/INSERT didn't).  See #259.
    if conn.in_transaction:
        logger.warning("Rolling back uncommitted transaction before BEGIN IMMEDIATE")
        conn.rollback()
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Drop and recreate the FTS table with content sync to match migration v5
        conn.execute("DROP TABLE IF EXISTS nodes_fts")
        conn.execute("""
            CREATE VIRTUAL TABLE nodes_fts USING fts5(
                name, qualified_name, file_path, signature,
                content='nodes', content_rowid='rowid',
                tokenize='porter unicode61'
            )
        """)

        # Rebuild from the content table (nodes) using the FTS5 rebuild command
        conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")


        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    count = conn.execute("SELECT count(*) FROM nodes_fts").fetchone()[0]
    logger.info("FTS index rebuilt: %d rows indexed", count)
    return count


# ---------------------------------------------------------------------------
# Query kind boosting heuristics
# ---------------------------------------------------------------------------


def detect_query_kind_boost(query: str) -> dict[str, float]:
    """Detect query patterns and return kind-specific boost multipliers.

    Heuristics:
    - PascalCase queries (e.g. ``MyClass``) boost Class/Type by 1.5x
    - snake_case queries (e.g. ``get_users``) boost Function by 1.5x
    - Queries containing ``.`` boost qualified name matches by 2.0x

    Returns:
        Dict mapping node kind strings to boost multipliers.
    """
    boosts: dict[str, float] = {}

    if not query or not query.strip():
        return boosts

    q = query.strip()

    # PascalCase: starts with uppercase, has at least one lowercase after
    if re.match(r'^[A-Z][a-z]', q) and not q.isupper():
        boosts["Class"] = 1.5
        boosts["Type"] = 1.5

    # snake_case or SCREAMING_SNAKE_CASE: contains underscore with letters
    if '_' in q and re.search(r'[a-zA-Z]', q):
        boosts["Function"] = 1.5

    # Dotted path: boost qualified name matches
    if '.' in q:
        boosts["_qualified"] = 2.0

    return boosts


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------


def rrf_merge(*result_lists: list[tuple[int, float]], k: int = 60) -> list[tuple[int, float]]:
    """Merge multiple ranked result lists using Reciprocal Rank Fusion.

    Each input list contains ``(id, score)`` tuples, ordered by score
    descending. The RRF score for each item is the sum of
    ``1 / (k + rank + 1)`` across all lists it appears in, where rank is
    the 0-based position.

    Args:
        *result_lists: Variable number of ranked result lists.
        k: RRF constant (default 60). Higher values reduce the impact of
           rank differences.

    Returns:
        Merged list of ``(id, rrf_score)`` tuples sorted by score descending.
    """
    scores: dict[int, float] = {}

    for result_list in result_lists:
        for rank, (item_id, _score) in enumerate(result_list):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank + 1)

    merged = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return merged


# ---------------------------------------------------------------------------
# FTS5 search
# ---------------------------------------------------------------------------


def _fts_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 50,
) -> list[tuple[int, float]]:
    """Run an FTS5 BM25 search against the nodes_fts table.

    Returns list of ``(node_id, bm25_score)`` tuples. The BM25 score is
    negated so higher = better (FTS5 returns negative BM25).
    """
    # Sanitize: wrap in double quotes to prevent FTS5 operator injection
    safe_query = '"' + query.replace('"', '""') + '"'

    try:
        rows = conn.execute(
            "SELECT rowid, rank FROM nodes_fts WHERE nodes_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (safe_query, limit),
        ).fetchall()
        # FTS5 rank is negative BM25 (lower = better), negate for consistency
        return [(row[0], -row[1]) for row in rows]
    except sqlite3.OperationalError as e:
        logger.warning("FTS5 search failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Embedding search (optional)
# ---------------------------------------------------------------------------


def _embedding_search(
    store: GraphStore,
    query: str,
    limit: int = 50,
    model: str | None = None,
    provider: str | None = None,
) -> list[tuple[int, float]]:
    """Run a vector similarity search using the embedding store.

    Returns list of ``(node_id, similarity_score)`` tuples.
    Gracefully returns an empty list if embeddings are not available.
    """
    try:
        from .embeddings import EmbeddingStore
    except ImportError:
        return []

    try:
        emb_store = EmbeddingStore(store.db_path, provider=provider, model=model)
        try:
            if not emb_store.available or emb_store.count() == 0:
                return []

            results = emb_store.search(query, limit=limit)
            # Map qualified names back to node IDs
            id_scores: list[tuple[int, float]] = []
            for qn, score in results:
                node = store.get_node(qn)
                if node:
                    id_scores.append((node.id, score))
            return id_scores
        finally:
            emb_store.close()
    except Exception as e:
        logger.warning("Embedding search failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Keyword LIKE fallback
# ---------------------------------------------------------------------------


def _keyword_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 50,
) -> list[tuple[int, float]]:
    """Fall back to simple LIKE keyword matching.

    Each word in the query must match independently (AND logic).
    Returns ``(node_id, score)`` tuples with a basic relevance score.
    """
    words = query.lower().split()
    if not words:
        return []

    conditions: list[str] = []
    params: list[str | int] = []
    for word in words:
        conditions.append(
            "(LOWER(name) LIKE ? OR LOWER(qualified_name) LIKE ?)"
        )
        params.extend([f"%{word}%", f"%{word}%"])

    where = " AND ".join(conditions)
    params.append(limit)
    sql = f"SELECT id, name, qualified_name FROM nodes WHERE {where} LIMIT ?"  # nosec B608

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []

    # Assign a simple relevance score: exact name match > prefix > contains
    q_lower = query.lower()
    results: list[tuple[int, float]] = []
    for row in rows:
        name_lower = row["name"].lower()
        if name_lower == q_lower:
            score = 3.0
        elif name_lower.startswith(q_lower):
            score = 2.0
        else:
            score = 1.0
        results.append((row["id"], score))

    results.sort(key=lambda x: x[1], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Main hybrid search
# ---------------------------------------------------------------------------


def hybrid_search(
    store: GraphStore,
    query: str,
    kind: Optional[str] = None,
    limit: int = 20,
    context_files: Optional[list[str]] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Hybrid search combining FTS5 BM25 and vector embeddings via RRF.

    Attempts FTS5 + embedding search first, falling back to FTS5-only,
    then keyword LIKE matching if FTS5 is unavailable.

    Args:
        store: The graph store to search.
        query: Search query string.
        kind: Optional node kind filter (e.g. ``"Function"``, ``"Class"``).
        limit: Maximum results to return (default 20).
        context_files: Optional list of file paths. Nodes in these files
            receive a 1.5x score boost.

    Returns:
        List of dicts with node metadata and ``score`` field.
    """
    if not query or not query.strip():
        return []

    # NOTE: hybrid_search uses store._conn for FTS5 and keyword queries
    # because those operate on the FTS virtual table or need raw Row
    # access for batch-fetch performance.  This is documented coupling.
    conn = store._conn
    fetch_limit = limit * 3  # Fetch extra to allow for filtering and boosting

    # ------ Phase 1: Gather ranked lists ------
    fts_results: list[tuple[int, float]] = []
    emb_results: list[tuple[int, float]] = []

    # Try FTS5 search
    try:
        fts_results = _fts_search(conn, query, limit=fetch_limit)
    except Exception as e:
        logger.warning("FTS5 unavailable, will use fallback: %s", e)

    # Try embedding search
    emb_results = _embedding_search(
        store, query, limit=fetch_limit, model=model, provider=provider,
    )

    # ------ Phase 2: Merge via RRF or fallback ------
    if fts_results or emb_results:
        lists_to_merge = []
        if fts_results:
            lists_to_merge.append(fts_results)
        if emb_results:
            lists_to_merge.append(emb_results)
        merged = rrf_merge(*lists_to_merge)
    else:
        # Fallback: keyword LIKE matching
        keyword_results = _keyword_search(conn, query, limit=fetch_limit)
        if not keyword_results:
            return []
        merged = keyword_results

    # ------ Phase 3+4: Batch-fetch nodes, apply boosting and kind filter ------
    kind_boosts = detect_query_kind_boost(query)
    context_set = set(context_files) if context_files else set()

    # Batch-fetch all candidate nodes in one query
    candidate_ids = [node_id for node_id, _ in merged]
    node_rows: dict[int, Any] = {}
    batch_size = 450
    for i in range(0, len(candidate_ids), batch_size):
        batch = candidate_ids[i:i + batch_size]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT * FROM nodes WHERE id IN ({placeholders})",  # nosec B608
            batch,
        ).fetchall()
        for row in rows:
            node_rows[row["id"]] = row

    # Apply boosting
    boosted: list[tuple[int, float]] = []
    for node_id, score in merged:
        row = node_rows.get(node_id)
        if not row:
            continue

        node_kind = row["kind"]
        file_path = row["file_path"]
        qualified_name = row["qualified_name"]

        boost = 1.0
        if node_kind in kind_boosts:
            boost *= kind_boosts[node_kind]
        if "_qualified" in kind_boosts and '.' in query:
            if query.lower() in qualified_name.lower():
                boost *= kind_boosts["_qualified"]
        if context_set and file_path in context_set:
            boost *= 1.5

        boosted.append((node_id, score * boost))

    boosted.sort(key=lambda x: x[1], reverse=True)

    # Build results from the already-fetched rows
    results: list[dict[str, Any]] = []
    for node_id, final_score in boosted:
        if len(results) >= limit:
            break

        row = node_rows.get(node_id)
        if not row:
            continue

        node_kind = row["kind"]
        if kind and node_kind != kind:
            continue

        results.append({
            "name": _sanitize_name(row["name"]),
            "qualified_name": _sanitize_name(row["qualified_name"]),
            "kind": node_kind,
            "file_path": row["file_path"],
            "line_start": row["line_start"],
            "line_end": row["line_end"],
            "language": row["language"] or "",
            "params": row["params"],
            "return_type": row["return_type"],
            "signature": row["signature"] if "signature" in row.keys() else None,
            "score": round(final_score, 6),
        })

    return results
