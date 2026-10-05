"""Canonical dump and diff of a built graph database.

Not a test module. ``tests/test_determinism.py`` uses it to turn two built
databases into two comparable, path-normalised, fully sorted text dumps, and
to describe exactly where they differ.

Design rules, because a determinism check that quietly compares nothing is
worse than no check at all:

* **Everything is compared by content, never by count.** Every section is a
  sorted list of canonical row strings; the diff reports rows present on one
  side only, with concrete examples.
* **Surrogate keys are resolved, not ignored.** ``flows.entry_point_id``,
  ``flow_memberships.node_id``, ``nodes.community_id`` and friends are
  rewritten to the qualified name or community name they point at, so two
  graphs that are the same modulo autoincrement numbering compare equal --
  while ``node_id_order`` separately pins the numbering itself.
* **Exclusions are enumerated and justified**, see ``IGNORED``.
* **Coverage is asserted, not assumed.** ``uncovered_tables()`` reports any
  table in the database that no section reads, so a derived table added
  later cannot silently escape the gate.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# Columns and metadata keys deliberately excluded from comparison, with the
# reason each one is legitimately allowed to differ between two builds of the
# same input. Anything not listed here is compared.
IGNORED: dict[str, str] = {
    "nodes.id": "autoincrement surrogate key; pinned separately by the node_id_order section",
    "nodes.updated_at": "wall-clock time the row was written",
    "nodes.community_id": "surrogate key; compared as a community name in node_communities",
    "edges.id": "autoincrement surrogate key with no referents",
    "edges.updated_at": "wall-clock time the row was written",
    "flows.id": "autoincrement surrogate key; resolved to the flow's own identity",
    "flows.created_at": "wall-clock time the row was written",
    "flows.updated_at": "wall-clock time the row was written",
    "communities.id": "autoincrement surrogate key; resolved to the community name",
    "communities.created_at": "wall-clock time the row was written",
    "risk_index.last_computed": "wall-clock time the row was written",
    "metadata.last_updated": "wall-clock time of the build",
    "metadata.last_postprocessed_at": "wall-clock time of post-processing",
    "sqlite_sequence": "SQLite's own autoincrement bookkeeping for the ignored id columns",
    "nodes_fts_data/idx/docsize/config": (
        "FTS5 shadow tables; the index is compared through the "
        "fts_index_bytes and fts_query_results sections instead"
    ),
}

# Tables a section reads, for the coverage assertion.
_COVERED_TABLES = {
    "nodes",
    "edges",
    "metadata",
    "flows",
    "flow_memberships",
    "communities",
    "community_summaries",
    "flow_snapshots",
    "risk_index",
    "nodes_fts",
    "nodes_fts_state",
    "embeddings",
}

# Tables that exist but are deliberately not read directly.
_EXEMPT_TABLES = {
    "sqlite_sequence",
    "nodes_fts_data",
    "nodes_fts_idx",
    "nodes_fts_docsize",
    "nodes_fts_config",
}

# Fixed probes for the full-text index. Chosen to hit different code paths in
# the corpus (a class, a snake_case function, a domain word, a bare token) and
# to return a non-empty ranked list, which the volume canary enforces.
FTS_QUERIES = (
    "GraphStore",
    "parse",
    "community",
    "embedding",
    "incremental",
    "sanitize",
)

PLACEHOLDER = "<REPO>"

STUB_PROVIDER_NAME = "determinism-stub"
STUB_DIM = 8


@dataclass(frozen=True)
class SectionDiff:
    """One section's disagreement between two dumps."""

    section: str
    only_left: list[str]
    only_right: list[str]

    @property
    def rows(self) -> int:
        return len(self.only_left) + len(self.only_right)

    def describe(self, left_label: str, right_label: str, examples: int = 3) -> str:
        lines = [
            f"[{self.section}] {self.rows} differing row(s): "
            f"{len(self.only_left)} only in {left_label}, "
            f"{len(self.only_right)} only in {right_label}"
        ]
        for label, rows in ((left_label, self.only_left), (right_label, self.only_right)):
            for row in rows[:examples]:
                lines.append(f"    only in {label}: {row[:400]}")
            if len(rows) > examples:
                lines.append(f"    ... and {len(rows) - examples} more only in {label}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Path normalisation
# ---------------------------------------------------------------------------


def path_variants(root: Path) -> list[str]:
    """Every spelling of *root* that can appear inside a stored value."""
    seen: list[str] = []
    for candidate in (root, root.resolve(), Path("/private") / str(root).lstrip("/")):
        for text in (str(candidate), candidate.as_posix()):
            if text and text not in seen:
                seen.append(text)
    # Longest first so a prefix never shadows a longer spelling.
    return sorted(seen, key=len, reverse=True)


def normalise(value: Any, roots: Iterable[str]) -> Any:
    """Replace every absolute repository path in *value* with ``<REPO>``."""
    if not isinstance(value, str):
        return value
    for root in roots:
        if root in value:
            value = value.replace(root, PLACEHOLDER)
    return value


# ---------------------------------------------------------------------------
# Stub embeddings
# ---------------------------------------------------------------------------


class _StubEmbeddingProvider:
    """Deterministic, offline stand-in for a real embedding provider.

    The gate is checking the *pipeline*: which nodes are selected for
    embedding, what text each one is reduced to, and what ends up in the
    ``embeddings`` table. A real provider would fold model and hardware
    nondeterminism into that answer and would need a network call, so the
    vector here is a pure function of the text. ``text_hash`` -- the column
    that actually records what was sent -- is produced by the real code path
    either way.
    """

    name = STUB_PROVIDER_NAME
    dimension = STUB_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vectors.append([digest[i] / 255.0 for i in range(STUB_DIM)])
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]


def populate_stub_embeddings(db_path: Path) -> int:
    """Run the real embedding refresh path against a deterministic provider."""
    from code_review_graph.embeddings import EmbeddingStore
    from code_review_graph.graph import GraphStore

    store = GraphStore(str(db_path))
    try:
        nodes = store.get_all_nodes()
    finally:
        store.close()

    embed_store = EmbeddingStore(db_path, provider=None)
    try:
        embed_store.provider = _StubEmbeddingProvider()  # type: ignore[assignment]
        embed_store.available = True
        return embed_store.embed_nodes(nodes)
    finally:
        embed_store.close()


# ---------------------------------------------------------------------------
# Dump
# ---------------------------------------------------------------------------


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, params).fetchall())


def _canon(values: Iterable[Any], roots: Iterable[str]) -> str:
    return "\x1f".join(
        "␀" if v is None else str(normalise(v, roots)) for v in values
    )


def _node_names(conn: sqlite3.Connection) -> dict[int, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT id, qualified_name FROM nodes")}


def _community_names(conn: sqlite3.Connection) -> dict[int, str]:
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT id, name FROM communities")}
    except sqlite3.OperationalError:
        return {}


def _flow_keys(conn: sqlite3.Connection, nodes: dict[int, str], roots) -> dict[int, str]:
    """Identify each flow by its content, not by its autoincrement id."""
    keys: dict[int, str] = {}
    try:
        rows = _rows(
            conn,
            "SELECT id, name, entry_point_id, depth, node_count, file_count, "
            "criticality, path_json FROM flows",
        )
    except sqlite3.OperationalError:
        return keys
    for row in rows:
        path = [nodes.get(i, f"<missing:{i}>") for i in json.loads(row["path_json"])]
        keys[row["id"]] = _canon(
            [
                row["name"],
                nodes.get(row["entry_point_id"], f"<missing:{row['entry_point_id']}>"),
                row["depth"],
                row["node_count"],
                row["file_count"],
                f"{row['criticality']:.6f}",
                "␞".join(path),
            ],
            roots,
        )
    return keys


def dump_database(db_path: Path, repo_root: Path) -> dict[str, list[str]]:
    """Return a canonical, path-normalised, sorted dump of every table."""
    roots = path_variants(repo_root)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        nodes = _node_names(conn)
        communities = _community_names(conn)
        flows = _flow_keys(conn, nodes, roots)

        sections: dict[str, list[str]] = {}

        sections["nodes"] = sorted(
            _canon(
                [
                    r["kind"], r["name"], r["qualified_name"], r["file_path"],
                    r["line_start"], r["line_end"], r["language"], r["parent_name"],
                    r["params"], r["return_type"], r["modifiers"], r["is_test"],
                    r["file_hash"], r["extra"], r["symbol"], r["signature"],
                ],
                roots,
            )
            for r in _rows(
                conn,
                "SELECT kind, name, qualified_name, file_path, line_start, "
                "line_end, language, parent_name, params, return_type, modifiers, "
                "is_test, file_hash, extra, symbol, signature FROM nodes",
            )
        )

        # Numbering itself: flows.path_json and flow_memberships persist raw
        # node ids, so identical content under a different id assignment is
        # still a real difference in what the database stores.
        sections["node_id_order"] = [
            _canon([r["id"], r["qualified_name"]], roots)
            for r in _rows(conn, "SELECT id, qualified_name FROM nodes ORDER BY id")
        ]

        sections["edges"] = sorted(
            _canon(
                [
                    r["kind"], r["source_qualified"], r["target_qualified"],
                    r["file_path"], r["line"], r["extra"],
                    f"{r['confidence']:.6f}", r["confidence_tier"],
                ],
                roots,
            )
            for r in _rows(
                conn,
                "SELECT kind, source_qualified, target_qualified, file_path, line, "
                "extra, confidence, confidence_tier FROM edges",
            )
        )

        sections["metadata"] = sorted(
            _canon([r["key"], r["value"]], roots)
            for r in _rows(conn, "SELECT key, value FROM metadata")
            if r["key"] not in ("last_updated", "last_postprocessed_at")
        )

        sections["flows"] = sorted(flows.values())

        sections["flow_memberships"] = sorted(
            _canon(
                [
                    flows.get(r["flow_id"], f"<missing-flow:{r['flow_id']}>"),
                    nodes.get(r["node_id"], f"<missing-node:{r['node_id']}>"),
                    r["position"],
                ],
                roots,
            )
            for r in _rows(
                conn,
                "SELECT flow_id, node_id, position FROM flow_memberships",
            )
        )

        sections["communities"] = sorted(
            _canon(
                [
                    r["name"], r["level"],
                    communities.get(r["parent_id"], "") if r["parent_id"] else "",
                    f"{r['cohesion']:.6f}", r["size"], r["dominant_language"],
                    r["description"],
                ],
                roots,
            )
            for r in _rows(
                conn,
                "SELECT name, level, parent_id, cohesion, size, dominant_language, "
                "description FROM communities",
            )
        )

        sections["node_communities"] = sorted(
            _canon(
                [
                    r["qualified_name"],
                    communities.get(r["community_id"], f"<missing:{r['community_id']}>"),
                ],
                roots,
            )
            for r in _rows(
                conn,
                "SELECT qualified_name, community_id FROM nodes "
                "WHERE community_id IS NOT NULL",
            )
        )

        sections["community_summaries"] = sorted(
            _canon(
                [
                    communities.get(r["community_id"], f"<missing:{r['community_id']}>"),
                    r["name"], r["purpose"], r["key_symbols"], r["risk"], r["size"],
                    r["dominant_language"],
                ],
                roots,
            )
            for r in _rows(
                conn,
                "SELECT community_id, name, purpose, key_symbols, risk, size, "
                "dominant_language FROM community_summaries",
            )
        )

        sections["flow_snapshots"] = sorted(
            _canon(
                [
                    flows.get(r["flow_id"], f"<missing-flow:{r['flow_id']}>"),
                    r["name"], r["entry_point"], r["critical_path"],
                    f"{r['criticality']:.6f}", r["node_count"], r["file_count"],
                ],
                roots,
            )
            for r in _rows(
                conn,
                "SELECT flow_id, name, entry_point, critical_path, criticality, "
                "node_count, file_count FROM flow_snapshots",
            )
        )

        sections["risk_index"] = sorted(
            _canon(
                [
                    nodes.get(r["node_id"], f"<missing-node:{r['node_id']}>"),
                    r["qualified_name"], f"{r['risk_score']:.6f}", r["caller_count"],
                    r["test_coverage"], r["security_relevant"],
                ],
                roots,
            )
            for r in _rows(
                conn,
                "SELECT node_id, qualified_name, risk_score, caller_count, "
                "test_coverage, security_relevant FROM risk_index",
            )
        )

        # The search index, twice over: what it answers, and what it is.
        query_rows: list[str] = []
        for query in FTS_QUERIES:
            hits = _rows(
                conn,
                "SELECT qualified_name, bm25(nodes_fts) AS score FROM nodes_fts "
                "WHERE nodes_fts MATCH ? ORDER BY rank, qualified_name LIMIT 20",
                (query,),
            )
            for rank, hit in enumerate(hits):
                query_rows.append(
                    _canon([query, rank, hit["qualified_name"], f"{hit['score']:.6f}"], roots)
                )
        sections["fts_query_results"] = query_rows

        digest = hashlib.sha256()
        for table in ("nodes_fts_data", "nodes_fts_idx", "nodes_fts_docsize"):
            # Some FTS5 shadow tables are WITHOUT ROWID, so sort in Python
            # rather than relying on a rowid every one of them has.
            raw = [
                tuple(row)
                for row in conn.execute(f"SELECT * FROM {table}")  # nosec B608
            ]
            for row in sorted(raw, key=repr):
                for value in row:
                    digest.update(repr(value).encode("utf-8", "replace"))
        fts_count = conn.execute("SELECT count(*) FROM nodes_fts").fetchone()[0]
        sections["fts_index_bytes"] = [f"rows={fts_count} sha256={digest.hexdigest()}"]

        # ``nodes_fts`` is an external-content table, so deleting an entry needs
        # the column values that were indexed, and those are gone once the node
        # row is. ``nodes_fts_state`` is the mirror kept for that, which makes a
        # disagreement here a disagreement about what the next incremental
        # update will be able to remove -- not cosmetic. Its own ``node_id`` is
        # the surrogate key from ``nodes``, resolved to the node's identity the
        # way ``flow_memberships`` resolves its own; raw numbering is already
        # pinned by the ``node_id_order`` section.
        #
        # Columns come from ``NODES_FTS_COLUMNS`` rather than a literal list, so
        # widening the mirror widens this section with it. Intersected with what
        # the database actually has, because a mirror written by an older
        # release carries fewer columns.
        from code_review_graph.migrations import (
            NODES_FTS_COLUMNS,
            NODES_FTS_STATE_TABLE,
        )

        present = {
            row[1]
            for row in conn.execute(
                f"PRAGMA table_info({NODES_FTS_STATE_TABLE})"  # nosec B608
            )
        }
        mirrored = [name for name in NODES_FTS_COLUMNS if name in present]
        mirror_rows: list[str] = []
        if mirrored:
            for r in _rows(
                conn,
                f"SELECT node_id, {', '.join(mirrored)} "  # nosec B608
                f"FROM {NODES_FTS_STATE_TABLE}",
            ):
                identity = nodes.get(
                    r["node_id"], f"<missing-node:{r['node_id']}>"
                )
                mirror_rows.append(
                    _canon([identity] + [r[name] for name in mirrored], roots)
                )
        sections["nodes_fts_state"] = sorted(mirror_rows)

        # What the embedding pipeline selects and what text it reduces each
        # node to -- computed from the graph, independent of any provider.
        from code_review_graph.embeddings import _node_to_text
        from code_review_graph.graph import GraphNode

        embedding_texts: list[str] = []
        for r in _rows(
            conn,
            "SELECT id, kind, name, qualified_name, file_path, line_start, line_end, "
            "language, parent_name, params, return_type, is_test, file_hash, extra "
            "FROM nodes WHERE kind != 'File'",
        ):
            node = GraphNode(
                id=r["id"], kind=r["kind"], name=r["name"],
                qualified_name=r["qualified_name"], file_path=r["file_path"],
                line_start=r["line_start"], line_end=r["line_end"],
                language=r["language"], parent_name=r["parent_name"],
                params=r["params"], return_type=r["return_type"],
                is_test=bool(r["is_test"]), file_hash=r["file_hash"],
                extra=json.loads(r["extra"] or "{}"),
            )
            text = normalise(_node_to_text(node), roots)
            embedding_texts.append(
                _canon(
                    [r["qualified_name"], hashlib.sha256(text.encode()).hexdigest()],
                    roots,
                )
            )
        sections["embedding_texts"] = sorted(embedding_texts)

        try:
            sections["embeddings"] = sorted(
                _canon(
                    [
                        r["qualified_name"], r["text_hash"], r["provider"],
                        hashlib.sha256(r["vector"]).hexdigest(),
                    ],
                    roots,
                )
                for r in _rows(
                    conn,
                    "SELECT qualified_name, text_hash, provider, vector FROM embeddings",
                )
            )
        except sqlite3.OperationalError:
            sections["embeddings"] = []

        return sections
    finally:
        conn.close()


def uncovered_tables(db_path: Path) -> set[str]:
    """Tables present in the database that no dump section reads."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        present = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        conn.close()
    return present - _COVERED_TABLES - _EXEMPT_TABLES


def compare(
    left: dict[str, list[str]],
    right: dict[str, list[str]],
    *,
    skip: Iterable[str] = (),
) -> list[SectionDiff]:
    """Return one ``SectionDiff`` per section whose contents disagree."""
    skipped = set(skip)
    diffs: list[SectionDiff] = []
    for section in sorted(set(left) | set(right)):
        if section in skipped:
            continue
        lhs = left.get(section, [])
        rhs = right.get(section, [])
        if lhs == rhs:
            continue
        # Multiset difference: identical rows cancel, duplicates survive.
        from collections import Counter

        lc, rc = Counter(lhs), Counter(rhs)
        only_left = sorted((lc - rc).elements())
        only_right = sorted((rc - lc).elements())
        if not only_left and not only_right:
            # Same multiset, different order (only reachable for the ordered
            # sections, which is exactly the difference worth reporting).
            only_left = [f"<same rows, different order: {len(lhs)} rows>"]
            only_right = [f"<same rows, different order: {len(rhs)} rows>"]
        diffs.append(SectionDiff(section, only_left, only_right))
    return diffs


def describe(
    diffs: list[SectionDiff],
    left_label: str,
    right_label: str,
) -> str:
    if not diffs:
        return "no differences"
    total = sum(d.rows for d in diffs)
    head = (
        f"{len(diffs)} section(s) differ between {left_label} and {right_label}, "
        f"{total} differing row(s) in total:"
    )
    return "\n".join([head] + [d.describe(left_label, right_label) for d in diffs])
