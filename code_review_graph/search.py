"""Hybrid search engine combining FTS5 (BM25) and vector embeddings.

Uses Reciprocal Rank Fusion (RRF) to merge results from full-text search
and semantic similarity, with query-aware kind boosting and context-file
boosting for relevance tuning.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Sequence
from typing import Any, NamedTuple, Optional

from .graph import GraphStore, _sanitize_name, identifier_words
from .migrations import (
    FTS_STATE_METADATA_KEY,
    FTS_STATE_VERSION,
    NODES_FTS_COLUMNS,
    NODES_FTS_DDL,
    NODES_FTS_STATE_TABLE,
    ensure_nodes_fts_state,
)
from .parser import normalize_file_path

logger = logging.getLogger(__name__)

# Per-column BM25 weights for the FTS5 index, looked up by column name so the
# weight vector follows the table's real shape (see NODES_FTS_DDL). A name hit
# is the strongest evidence; a docstring hit is the weakest, which is what
# keeps a symbol ahead of the prose that merely mentions it.
_FTS_COLUMN_WEIGHTS: dict[str, float] = {
    "name": 10.0,
    "qualified_name": 6.0,
    "file_path": 1.0,
    "signature": 2.0,
    "docstring": 1.5,
    "name_tokens": 4.0,
}
_FTS_DEFAULT_WEIGHT = 1.0

# Score multipliers for a query that is exactly one identifier. The first
# applies when the node's own name is that identifier, the second when the
# identifier is the tail of its qualified name (a method named by its class).
_EXACT_NAME_BOOST = 4.0
_EXACT_SYMBOL_BOOST = 3.0

# A symbol named after the query is almost always the wanted one. BM25 cannot
# say so here: every row carries its whole file path in two columns, so path
# tokens dominate the length normalization and a long test name that repeats
# the query words outscores the short function the query describes. These
# multiply a hit by how much of the query its own symbol name accounts for.
_NAME_COVERAGE_BOOST = 1.0
_NAME_EXACT_COVERAGE_BOOST = 2.0


# ---------------------------------------------------------------------------
# FTS5 index management
# ---------------------------------------------------------------------------

# ``nodes_fts`` is an *external content* table: it stores the inverted index
# but reads column values from ``nodes``.  Removing an entry therefore needs
# the values that were indexed, and once the node row is gone those values
# are unrecoverable, leaving an entry that still matches a query but no
# longer resolves to a node.  ``nodes_fts_state`` mirrors what the index
# currently holds so a delta always has the old values to delete with.
# The table name, the columns it carries and the flag that says it is
# current all live in migrations, next to the index DDL they mirror.
_FTS_STATE_TABLE = NODES_FTS_STATE_TABLE
_FTS_STATE_METADATA_KEY = FTS_STATE_METADATA_KEY
_FTS_STATE_VERSION = FTS_STATE_VERSION

# Column lists built once from the single source of truth, so the mirror,
# the delta delete and the delta insert always name the same columns in the
# same order as the index itself.
_FTS_COLUMN_SQL = ", ".join(NODES_FTS_COLUMNS)
_FTS_COLUMN_PLACEHOLDERS = ", ".join("?" * len(NODES_FTS_COLUMNS))

# Above this share of the graph a delta stops paying for itself: the per-row
# delete/insert work approaches a rebuild while keeping tombstones around.
# The floor keeps small graphs on the delta path, where a few hundred rows
# are cheaper to rewrite than the whole index is to drop and repopulate.
_FTS_DELTA_MAX_RATIO = 0.35
_FTS_DELTA_MIN_ROWS = 500

# SQLite caps host parameters per statement; mirrors communities._SQL_BATCH.
_FTS_SQL_BATCH = 450


def _ensure_fts_state_table(conn: sqlite3.Connection) -> None:
    """Create the index-state mirror, or widen one that predates a column."""
    ensure_nodes_fts_state(conn)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type IN ('table', 'view') "
        "AND name = ?",
        (name,),
    ).fetchone()
    return bool(row and row[0])


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
        # Drop and recreate the FTS table with content sync. The DDL lives in
        # migrations so this cannot drift from the shape migration v11 creates.
        conn.execute("DROP TABLE IF EXISTS nodes_fts")
        conn.execute(NODES_FTS_DDL)

        # Rebuild from the content table (nodes) using the FTS5 rebuild command
        conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")

        # Record what the index now holds so the next delta can delete with
        # the same values 'rebuild' just indexed.
        _ensure_fts_state_table(conn)
        conn.execute(f"DELETE FROM {_FTS_STATE_TABLE}")  # nosec B608
        conn.execute(
            f"INSERT INTO {_FTS_STATE_TABLE} "  # nosec B608
            f"(node_id, {_FTS_COLUMN_SQL}) "
            f"SELECT id, {_FTS_COLUMN_SQL} FROM nodes"
        )
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            (_FTS_STATE_METADATA_KEY, _FTS_STATE_VERSION),
        )

        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    count = conn.execute("SELECT count(*) FROM nodes_fts").fetchone()[0]
    logger.info("FTS index rebuilt: %d rows indexed", count)
    return count


def _stored_path_hints(file_paths: Sequence[str] | None) -> list[str]:
    """Normalise caller-supplied paths to the spelling stored in ``nodes``."""
    if not file_paths:
        return []
    seen: dict[str, None] = {}
    for raw in file_paths:
        if not raw:
            continue
        seen.setdefault(normalize_file_path(raw), None)
    return list(seen)


def _batched(values: list[Any]) -> list[list[Any]]:
    return [
        values[i:i + _FTS_SQL_BATCH]
        for i in range(0, len(values), _FTS_SQL_BATCH)
    ]


def _collect_delta_rowids(
    conn: sqlite3.Connection,
    paths: list[str],
) -> set[int]:
    """Return every rowid whose index entry has to be rewritten.

    Two of the three sources run whatever the caller passed, because they
    are what keeps the index honest: a node with no index entry would be
    unfindable, and an index entry with no node is a phantom hit.  The
    third narrows the rewrite to the files the caller named; without a
    hint it compares the indexed text against the nodes table instead.
    """
    ids: set[int] = set()

    # Nodes in the graph that the index has never seen.
    ids.update(
        row[0]
        for row in conn.execute(
            f"SELECT n.id FROM nodes n "  # nosec B608
            f"LEFT JOIN {_FTS_STATE_TABLE} s ON s.node_id = n.id "
            "WHERE s.node_id IS NULL"
        )
    )
    # Index entries whose node is gone: the phantom-hit case.
    ids.update(
        row[0]
        for row in conn.execute(
            f"SELECT s.node_id FROM {_FTS_STATE_TABLE} s "  # nosec B608
            "LEFT JOIN nodes n ON n.id = s.node_id "
            "WHERE n.id IS NULL"
        )
    )

    if paths:
        for batch in _batched(paths):
            placeholders = ",".join("?" * len(batch))
            ids.update(
                row[0]
                for row in conn.execute(
                    f"SELECT id FROM nodes WHERE file_path IN ({placeholders})",  # nosec B608
                    batch,
                )
            )
            ids.update(
                row[0]
                for row in conn.execute(
                    f"SELECT node_id FROM {_FTS_STATE_TABLE} "  # nosec B608
                    f"WHERE file_path IN ({placeholders})",
                    batch,
                )
            )
    else:
        # No hint: find rows whose indexed text drifted from the node.
        drift = " OR ".join(
            f"n.{column} IS NOT s.{column}" for column in NODES_FTS_COLUMNS
        )
        ids.update(
            row[0]
            for row in conn.execute(
                f"SELECT n.id FROM nodes n "  # nosec B608
                f"JOIN {_FTS_STATE_TABLE} s ON s.node_id = n.id "
                f"WHERE {drift}"
            )
        )

    return ids


def _apply_fts_delta(conn: sqlite3.Connection, rowids: list[int]) -> tuple[int, int]:
    """Re-sync *rowids* in the index, one batch at a time.

    Deletes come first within a batch: a rowid whose text changed is present
    on both sides, and inserting before deleting would index it twice.
    """
    removed = 0
    added = 0
    delete_sql = (  # nosec B608
        f"INSERT INTO nodes_fts(nodes_fts, rowid, {_FTS_COLUMN_SQL}) "
        f"VALUES('delete', ?, {_FTS_COLUMN_PLACEHOLDERS})"
    )
    insert_sql = (  # nosec B608
        f"INSERT INTO nodes_fts(rowid, {_FTS_COLUMN_SQL}) "
        f"VALUES(?, {_FTS_COLUMN_PLACEHOLDERS})"
    )
    state_sql = (  # nosec B608
        f"INSERT INTO {_FTS_STATE_TABLE} (node_id, {_FTS_COLUMN_SQL}) "
        f"VALUES(?, {_FTS_COLUMN_PLACEHOLDERS})"
    )

    for batch in _batched(rowids):
        placeholders = ",".join("?" * len(batch))
        old_rows = [
            tuple(row)
            for row in conn.execute(
                f"SELECT node_id, {_FTS_COLUMN_SQL} "  # nosec B608
                f"FROM {_FTS_STATE_TABLE} WHERE node_id IN ({placeholders})",
                batch,
            )
        ]
        if old_rows:
            conn.executemany(delete_sql, old_rows)
            conn.execute(
                f"DELETE FROM {_FTS_STATE_TABLE} "  # nosec B608
                f"WHERE node_id IN ({placeholders})",
                batch,
            )
            removed += len(old_rows)

        new_rows = [
            tuple(row)
            for row in conn.execute(
                f"SELECT id, {_FTS_COLUMN_SQL} "  # nosec B608
                f"FROM nodes WHERE id IN ({placeholders})",
                batch,
            )
        ]
        if new_rows:
            conn.executemany(insert_sql, new_rows)
            conn.executemany(state_sql, new_rows)
            added += len(new_rows)

    return removed, added


def update_fts_index(
    store: GraphStore,
    file_paths: Sequence[str] | None = None,
    *,
    _out_mode: Optional[list[str]] = None,
) -> int:
    """Bring the FTS5 index in step with ``nodes`` without a full rebuild.

    Rewrites only the entries that changed: the rows of the files in
    *file_paths* plus any row that drifted out of sync.  Falls back to
    :func:`rebuild_fts_index` when the index state is unusable (a database
    that predates it, or one whose state was cleared) or when so much of the
    graph moved that a rebuild is the cheaper way to get there.

    Args:
        store: An open GraphStore.
        file_paths: Files whose rows changed, in any path spelling. Omitting
            them is safe: the drift comparison then covers the whole table.
        _out_mode: Optional output list; a single ``"delta"`` or
            ``"rebuild"`` is appended to say which path ran.

    Returns:
        Number of rows the index now covers.
    """
    conn = store._conn

    def _rebuild() -> int:
        if _out_mode is not None:
            _out_mode.append("rebuild")
        return rebuild_fts_index(store)

    if not _table_exists(conn, "nodes_fts") or not _table_exists(
        conn, _FTS_STATE_TABLE
    ):
        return _rebuild()

    if store.get_metadata(_FTS_STATE_METADATA_KEY) != _FTS_STATE_VERSION:
        # This index was last written without the mirror, so nothing here
        # describes what it holds. One rebuild re-establishes both.
        return _rebuild()

    total_nodes = conn.execute("SELECT count(*) FROM nodes").fetchone()[0]
    state_rows = conn.execute(
        f"SELECT count(*) FROM {_FTS_STATE_TABLE}"  # nosec B608
    ).fetchone()[0]
    if total_nodes and not state_rows:
        # The mirror was emptied without the index being emptied with it;
        # inserting every row again would index each one twice.
        return _rebuild()

    delta_ids = _collect_delta_rowids(conn, _stored_path_hints(file_paths))
    rebuild_threshold = max(
        _FTS_DELTA_MIN_ROWS, int(total_nodes * _FTS_DELTA_MAX_RATIO)
    )
    if len(delta_ids) >= rebuild_threshold:
        return _rebuild()

    if _out_mode is not None:
        _out_mode.append("delta")

    if delta_ids:
        if conn.in_transaction:
            logger.warning(
                "Rolling back uncommitted transaction before BEGIN IMMEDIATE"
            )
            conn.rollback()
        conn.execute("BEGIN IMMEDIATE")
        try:
            removed, added = _apply_fts_delta(conn, sorted(delta_ids))
            conn.commit()
        except sqlite3.DatabaseError as exc:
            # The index state did not describe the index after all (a
            # database written by a version without the mirror, say). A
            # rebuild is the one operation that needs no old values.
            conn.rollback()
            logger.warning("FTS delta failed, rebuilding index: %s", exc)
            return _rebuild()
        except BaseException:
            conn.rollback()
            raise
        logger.info(
            "FTS index delta: %d entries removed, %d indexed", removed, added
        )

    # ``SELECT count(*) FROM nodes_fts`` scans the external content table to
    # answer exactly this; every node now has one index entry, so read the
    # count straight off ``nodes`` instead.
    return total_nodes


# ---------------------------------------------------------------------------
# Query kind boosting heuristics
# ---------------------------------------------------------------------------


# A query that is one identifier and nothing else: the user is naming a
# symbol, not describing behaviour.
_EXACT_IDENT_QUERY_RE = re.compile(r'^[A-Za-z_][\w.]*$')
_DOTTED_IDENT_RE = re.compile(r'\b[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+\b')
_SNAKE_IDENT_RE = re.compile(r'\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b')
_PASCAL_IDENT_RE = re.compile(r'\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b')


def extract_query_identifiers(query: str) -> list[str]:
    """Pull out identifier-shaped tokens from anywhere in a query.

    Catches dotted forms (``Context.Next``), snake_case (``get_dependant``),
    and CamelCase (``APIRoute``) even when they're embedded in a natural-
    language sentence. Used to boost search hits whose qualified_name
    contains any of these tokens, so an LLM asking "Who advances the gin
    middleware chain via Context.Next" lands on ``Context.Next`` instead of
    the bare ``Context`` class.
    """
    found: list[str] = []
    seen: set[str] = set()
    for pat in (_DOTTED_IDENT_RE, _SNAKE_IDENT_RE, _PASCAL_IDENT_RE):
        for match in pat.findall(query):
            lo = match.lower()
            if lo not in seen and len(lo) >= 3:
                seen.add(lo)
                found.append(lo)
    return found


def detect_query_kind_boost(query: str) -> dict[str, Any]:
    """Detect query patterns and return per-node boost multipliers.

    Heuristics:
    - PascalCase queries (e.g. ``MyClass``) boost Class/Type by 1.5x
    - snake_case queries (e.g. ``get_users``) boost Function by 1.5x
    - Queries containing ``.`` boost qualified name matches by 2.0x
    - Identifier-shaped tokens *anywhere* in the query (dotted, snake_case,
      CamelCase) boost results whose qualified_name contains them by 2.0x.
      See ``extract_query_identifiers``.
    - A query that is *only* an identifier names one symbol, so the node
      whose name is exactly that identifier is boosted hardest. This is what
      keeps ``_sanitize_name`` first now that prefix matching widens the
      candidate set and docstrings that merely mention a symbol are indexed.

    Returns:
        Dict whose keys are either node kind strings (mapped to float
        multipliers) or one of the special keys ``_qualified``,
        ``_qualified_identifiers``, ``_exact_name``.
    """
    boosts: dict[str, Any] = {}

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

    # Identifiers extracted from anywhere in the query
    idents = extract_query_identifiers(q)
    if idents:
        boosts["_qualified_identifiers"] = idents

    # The whole query is one identifier: an exact symbol match outranks
    # everything that merely contains or documents it.
    if _EXACT_IDENT_QUERY_RE.match(q):
        boosts["_exact_name"] = q.lower()

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


_EXPLICIT_PHRASE_RE = re.compile(r'"([^"]*)"')
_HAS_ALNUM_RE = re.compile(r"[0-9A-Za-z]")
# A prefix term is only useful once it carries some signal. "a*" matches
# nearly every document in the index, so short tokens stay exact.
_MIN_PREFIX_ALNUM = 3

# Most terms one MATCH expression may carry.
#
# FTS5 reads one doclist per term and adds one BM25 contribution per term
# per matching row, and the OR widening grows the matching set with the term
# count as well, so an uncapped question asked in prose gets superlinearly
# slower. Measured on a 120k-node graph the widened expression took 13 ms at
# 5 terms, 30 ms at 10, 99 ms at 25 and 173 ms at 50; on this repository's
# own graph, 0.6 / 1.5 / 5.1 / 9.9 ms.
#
# Twelve because no query in any shipped benchmark config reaches it -- the
# longest of the 47 is eleven terms -- so the cap cannot change a measured
# result, it only bounds the tail. On a set of 18 deliberately long
# questions (14 to 22 terms each) it measured no worse than no cap at all:
# MRR 0.44 against 0.40, the same 14 of 18 found, and the 50-word query time
# flat from the cap onwards instead of still climbing.
_MAX_FTS_TERMS = 12


def _quote_term(text: str) -> str:
    """Return *text* as a single FTS5 string, with inner quotes doubled.

    Quoting is what keeps the term a term: it neutralizes the operators
    (``AND``/``OR``/``NOT``/``NEAR``), the column filter ``col:``, the
    initial-token ``^``, parentheses and ``*`` that a user may legitimately
    have typed as part of what they are looking for.
    """
    return '"' + text.replace('"', '""') + '"'


def _alnum_count(text: str) -> int:
    return sum(1 for ch in text if ch.isalnum())


class _QueryTerm(NamedTuple):
    """One term of a MATCH expression, with what is needed to rank it."""

    key: str      # lowercased source text, for deduplication and rarity
    expression: str  # the term exactly as it appears in the MATCH expression
    is_phrase: bool  # the user typed it inside double quotes


def _split_query_terms(query: str) -> list[_QueryTerm]:
    """Split *query* into the distinct terms a MATCH expression may carry.

    Explicit phrases come first, then the loose tokens, each in the order
    typed. Terms that repeat are dropped after their first occurrence:
    ``bm25()`` already weights a term by its frequency in the *document*, so
    repeating it in the query buys no ranking signal and costs one more
    doclist read per matching row. Comparison is case-insensitive because
    the index is.
    """
    phrases: list[str] = []
    loose: list[str] = []
    cursor = 0
    for match in _EXPLICIT_PHRASE_RE.finditer(query):
        loose.extend(query[cursor:match.start()].split())
        phrases.append(match.group(1))
        cursor = match.end()
    loose.extend(query[cursor:].split())

    terms: list[_QueryTerm] = []
    seen: set[str] = set()

    def _add(text: str, expression: str, is_phrase: bool) -> None:
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        terms.append(_QueryTerm(key, expression, is_phrase))

    for phrase in phrases:
        if _HAS_ALNUM_RE.search(phrase):
            _add(phrase, _quote_term(phrase), True)
    for token in loose:
        if not _HAS_ALNUM_RE.search(token):
            continue
        prefix = "*" if _alnum_count(token) >= _MIN_PREFIX_ALNUM else ""
        _add(token, _quote_term(token) + prefix, False)
    return terms


def _rarity_key(term: _QueryTerm, position: int) -> tuple[int, int, int]:
    """Sort key putting the term most worth keeping first.

    A phrase the user quoted is always kept: it is both the most selective
    kind of term and an explicit request. Loose tokens are ordered by length,
    descending. Across natural language the commonest words are the shortest
    ones, so the long token is the rarer one and the one that carries what
    the question is about; the short ones are the scaffolding ("how does the
    ... so that it can ...") that a prose question is mostly made of.

    Rarity measured against the index itself was tried instead and measured
    worse -- see the module docstring of ``tests/test_search_quality.py``.
    An index over a repository holds every node's file path and qualified
    name, so this project's own words ("graph", "search", "parser") are the
    commonest terms in it while "today" and "whether" are rare. Index rarity
    therefore drops exactly the words that name the answer.

    Position breaks ties, so the choice never depends on set ordering.
    """
    if term.is_phrase:
        return (0, 0, position)
    return (1, -len(term.key), position)


def _cap_terms(terms: list[_QueryTerm]) -> list[_QueryTerm]:
    """Reduce *terms* to at most :data:`_MAX_FTS_TERMS`, keeping the rarest.

    Truncating to the first N would keep "how does the" and throw away the
    words that say what is being asked about, so the terms are ranked by
    :func:`_rarity_key` and the survivors are put back in the order they
    were typed.
    """
    if len(terms) <= _MAX_FTS_TERMS:
        return terms
    ranked = sorted(range(len(terms)), key=lambda i: _rarity_key(terms[i], i))
    return [terms[i] for i in sorted(ranked[:_MAX_FTS_TERMS])]


def build_fts_queries(query: str) -> list[str]:
    """Build the FTS5 MATCH expressions to try for *query*, in order.

    The previous implementation wrapped the entire query in one pair of
    double quotes, which made every search an exact-adjacency phrase match:
    ``password hashing`` found nothing unless some node carried those two
    tokens side by side. Instead the query is split into tokens, each token
    is quoted (and given a prefix ``*`` when long enough), and the tokens are
    combined:

    * the first expression ``AND``s the tokens, which is the precise reading
      of a multi-word query;
    * the second ``OR``s them, and is only reached when the ``AND`` finds
      nothing, so recall is widened without ever costing precision.

    A phrase the user typed in double quotes is kept as a phrase, takes no
    prefix, and suppresses the ``OR`` widening: the quotes are a request for
    exactness and are honoured.

    Repeated terms are dropped and the count is capped at
    :data:`_MAX_FTS_TERMS`, because the cost of an expression grows with the
    number of terms in it and a question asked in prose -- exactly what
    token-based matching invites -- would otherwise grow one term per word.
    When the cap bites, the terms kept are the rarest ones rather than the
    first ones typed: see :func:`_rarity_key`.

    Degenerate inputs collapse to an empty list, which the caller treats as
    "FTS has nothing to say" and falls through to the LIKE keyword path:
    an empty query, whitespace, and text with no alphanumeric character at
    all (``...``).

    Returns:
        Zero, one or two FTS5 MATCH expressions.
    """
    if not query or not query.strip():
        return []

    terms = _cap_terms(_split_query_terms(query))
    if not terms:
        return []
    expressions = [term.expression for term in terms]
    if len(expressions) == 1:
        return [expressions[0]]

    conjunction = " AND ".join(expressions)
    if any(term.is_phrase for term in terms):
        return [conjunction]
    return [conjunction, " OR ".join(expressions)]


def _covered_word_count(query_words: set[str], symbol_words: set[str]) -> int:
    """Count query words this symbol's own name accounts for.

    A query word counts when a symbol word equals it or starts with it, which
    is the same rule the prefix terms in :func:`build_fts_queries` apply, so
    ``parse`` is accounted for by ``CodeParser``. Words shorter than the
    prefix threshold must match exactly, for the same reason a one or two
    character prefix term is not issued.
    """
    covered = 0
    for word in query_words:
        if word in symbol_words:
            covered += 1
        elif len(word) >= _MIN_PREFIX_ALNUM and any(
            other.startswith(word) for other in symbol_words
        ):
            covered += 1
    return covered


def _fts_column_weights(conn: sqlite3.Connection) -> str:
    """Return the BM25 weight arguments matching the live nodes_fts shape.

    Read from the table itself rather than hard-coded so an index created by
    an older schema version (no ``docstring``/``name_tokens`` columns) still
    gets a well-formed ``bm25()`` call instead of a wrong-arity error.
    """
    columns = [row[1] for row in conn.execute("PRAGMA table_info(nodes_fts)")]
    weights = [
        _FTS_COLUMN_WEIGHTS.get(str(column), _FTS_DEFAULT_WEIGHT)
        for column in columns
    ]
    return ", ".join(f"{weight:.1f}" for weight in weights)


def _fts_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 50,
) -> list[tuple[int, float]]:
    """Run an FTS5 BM25 search against the nodes_fts table.

    Tries each expression from :func:`build_fts_queries` in turn and returns
    the first non-empty result, so the precise reading of a query wins and
    the widened one is only a fallback.

    Returns list of ``(node_id, bm25_score)`` tuples. The BM25 score is
    negated so higher = better (FTS5 returns negative BM25).
    """
    expressions = build_fts_queries(query)
    if not expressions:
        return []

    try:
        weights = _fts_column_weights(conn)
    except sqlite3.OperationalError as exc:
        logger.warning("FTS5 index unavailable: %s", exc)
        return []

    # The weights are floats formatted from a module constant, never from the
    # query, so there is no user-controlled text in this statement. The MATCH
    # expression itself stays a bound parameter.
    sql = (  # nosec B608
        f"SELECT rowid, bm25(nodes_fts, {weights}) AS score FROM nodes_fts "
        "WHERE nodes_fts MATCH ? ORDER BY score LIMIT ?"
    )
    for expression in expressions:
        try:
            rows = conn.execute(sql, (expression, limit)).fetchall()
        except sqlite3.OperationalError as e:
            logger.warning("FTS5 search failed for %r: %s", expression, e)
            return []
        if rows:
            # bm25() is negative (lower = better); negate for consistency.
            return [(row[0], -row[1]) for row in rows]
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
    _out_mode: Optional[list[str]] = None,
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
        _out_mode: Optional output list. If provided, a single string is
            appended indicating which search path(s) contributed:
            ``"hybrid"`` (FTS + embeddings), ``"fts"`` (FTS only),
            ``"semantic"`` (embeddings only), ``"keyword"`` (LIKE fallback),
            or ``"none"`` (empty query, or all search paths returned 0 results).

    Returns:
        List of dicts with node metadata and ``score`` field.
    """
    if not query or not query.strip():
        if _out_mode is not None:
            _out_mode.append("none")
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
        if _out_mode is not None:
            if fts_results and emb_results:
                _out_mode.append("hybrid")
            elif fts_results:
                _out_mode.append("fts")
            else:
                _out_mode.append("semantic")
    else:
        # Fallback: keyword LIKE matching
        keyword_results = _keyword_search(conn, query, limit=fetch_limit)
        if not keyword_results:
            if _out_mode is not None:
                _out_mode.append("none")
            return []
        if _out_mode is not None:
            _out_mode.append("keyword")
        merged = keyword_results

    # ------ Phase 3+4: Batch-fetch nodes, apply boosting and kind filter ------
    kind_boosts = detect_query_kind_boost(query)
    # Stored file paths use POSIX separators (#774); bridge native spellings.
    context_set = (
        {normalize_file_path(p) for p in context_files} if context_files else set()
    )

    query_words = identifier_words(query)

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
        idents = kind_boosts.get("_qualified_identifiers")
        if idents:
            qn_lo = qualified_name.lower()
            if any(ident in qn_lo for ident in idents):
                boost *= 2.0
        if query_words:
            symbol = row["symbol"] if "symbol" in row.keys() else None
            symbol_words = identifier_words(symbol or row["name"])
            covered = _covered_word_count(query_words, symbol_words)
            if covered:
                boost *= 1.0 + _NAME_COVERAGE_BOOST * covered / len(query_words)
                if symbol_words == query_words:
                    boost *= _NAME_EXACT_COVERAGE_BOOST

        exact = kind_boosts.get("_exact_name")
        if exact:
            if row["name"].lower() == exact:
                boost *= _EXACT_NAME_BOOST
            elif qualified_name.lower().endswith("::" + exact):
                boost *= _EXACT_SYMBOL_BOOST
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
