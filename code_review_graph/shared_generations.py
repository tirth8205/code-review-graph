"""Immutable node and edge snapshots, with checkout-local Git reference bindings."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from sqlite3 import Connection
from typing import Any

from .constants import normalize_file_path
from .errors import GraphStoreError

_NODE_COLUMNS = (
    "kind", "name", "qualified_name", "file_path", "line_start", "line_end",
    "language", "parent_name", "params", "return_type", "is_test", "file_hash",
    "extra", "modifiers", "symbol", "docstring", "name_tokens", "signature",
)
_EDGE_COLUMNS = (
    "kind", "source_qualified", "target_qualified", "file_path", "line",
    "extra", "confidence", "confidence_tier", "target_resolution",
)
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS crg_generation_payloads (
        id INTEGER PRIMARY KEY, digest BLOB NOT NULL UNIQUE, data TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS crg_graph_generations (
        id INTEGER PRIMARY KEY, digest BLOB NOT NULL UNIQUE
    )""",
    """CREATE TABLE IF NOT EXISTS crg_generation_members (
        generation_id INTEGER NOT NULL REFERENCES crg_graph_generations(id),
        payload_id INTEGER NOT NULL REFERENCES crg_generation_payloads(id),
        multiplicity INTEGER NOT NULL CHECK (multiplicity > 0),
        PRIMARY KEY (generation_id, payload_id)
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS crg_generation_bindings (
        checkout BLOB NOT NULL, ref TEXT NOT NULL,
        head TEXT NOT NULL, built_at TEXT NOT NULL,
        generation_id INTEGER NOT NULL REFERENCES crg_graph_generations(id),
        PRIMARY KEY (checkout, ref)
    ) WITHOUT ROWID""",
)


def _checkout_key(root: Path) -> bytes:
    return hashlib.sha256(os.fsencode(os.path.normcase(str(root.resolve())))).digest()


def git_ref(branch: str, head: str) -> str:
    if branch and branch != "HEAD":
        return f"refs/heads/{branch}"
    return f"detached:{head}" if head else "unborn"


def _path_fields(kind: str, row_kind: str) -> tuple[int, ...]:
    if kind == "edges":
        return (1, 2, 3)
    if row_kind == "File":
        return (1, 2, 3, _NODE_COLUMNS.index("symbol"), _NODE_COLUMNS.index("signature"))
    return (2, 3)


def _payloads(conn: Connection, root: Path) -> tuple[Counter[bytes], dict[bytes, str]]:
    prefix = normalize_file_path(root).rstrip("/") + "/"
    counts: Counter[bytes] = Counter()
    payloads: dict[bytes, str] = {}
    for kind, columns in (("nodes", _NODE_COLUMNS), ("edges", _EDGE_COLUMNS)):
        for row in conn.execute(f"SELECT {', '.join(columns)} FROM {kind}"):
            data: list[Any] = list(row)
            for index in _path_fields(kind, data[0]):
                value = data[index]
                if isinstance(value, str) and value.startswith(prefix):
                    # Strings outside this checkout, including unresolved
                    # names, stay literal. Extra metadata is opaque.
                    data[index] = [value[len(prefix):]]
            encoded = json.dumps((kind, data), ensure_ascii=True, separators=(",", ":"))
            digest = hashlib.sha256(encoded.encode()).digest()
            counts[digest] += 1
            payloads.setdefault(digest, encoded)
    return counts, payloads


def initialize(conn: Connection) -> None:
    """Install the generation schema and age tracking without changing bindings."""
    for statement in _SCHEMA:
        conn.execute(statement)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS crg_generation_unbound (
            generation_id INTEGER PRIMARY KEY, since REAL NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS crg_generation_members_payload "
        "ON crg_generation_members(payload_id)",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS crg_generation_bindings_generation "
        "ON crg_generation_bindings(generation_id)",
    )
    conn.execute(
        "INSERT OR IGNORE INTO crg_generation_unbound "
        "SELECT g.id, ? FROM crg_graph_generations g WHERE NOT EXISTS "
        "(SELECT 1 FROM crg_generation_bindings b WHERE b.generation_id=g.id)",
        (time.time(),),
    )


def bind_manifest(conn: Connection, root: Path, ref: str, digest: str, paths: int) -> None:
    """Attach verified inputs to the checkout/ref in its publication transaction."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS crg_graph_inputs (
            digest TEXT PRIMARY KEY, path_count INTEGER NOT NULL
        ) WITHOUT ROWID
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS crg_graph_input_bindings (
            checkout BLOB NOT NULL, ref TEXT NOT NULL, digest TEXT NOT NULL,
            PRIMARY KEY (checkout, ref)
        ) WITHOUT ROWID
    """)
    conn.execute("INSERT OR IGNORE INTO crg_graph_inputs VALUES (?, ?)", (digest, paths))
    conn.execute(
        "INSERT INTO crg_graph_input_bindings VALUES (?, ?, ?) "
        "ON CONFLICT(checkout, ref) DO UPDATE SET digest=excluded.digest",
        (_checkout_key(root), ref, digest),
    )
    if conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE name='crg_artifact_unbound'",
    ).fetchone():
        conn.execute(
            "DELETE FROM crg_artifact_unbound WHERE kind='manifest' AND digest=?", (digest,),
        )


def publish(conn: Connection, root: Path, ref: str, head: str, built_at: str) -> str:
    """Intern immutable rows and move only this checkout/reference binding."""
    checkout = _checkout_key(root)
    previous = conn.execute(
        "SELECT generation_id FROM crg_generation_bindings WHERE checkout=? AND ref=?",
        (checkout, ref),
    ).fetchone()
    counts, payloads = _payloads(conn, root)
    digest = hashlib.sha256(b"crg-graph-rows-v1\0")
    for payload, count in sorted(counts.items()):
        digest.update(payload)
        digest.update(count.to_bytes(8, "big"))
    key = digest.digest()
    row = conn.execute(
        "SELECT id FROM crg_graph_generations WHERE digest=?", (key,),
    ).fetchone()
    if row is None:
        generation = conn.execute(
            "INSERT INTO crg_graph_generations (digest) VALUES (?)", (key,),
        ).lastrowid
        conn.executemany(
            "INSERT OR IGNORE INTO crg_generation_payloads (digest, data) VALUES (?, ?)",
            payloads.items(),
        )
        conn.executemany(
            "INSERT INTO crg_generation_members "
            "SELECT ?, id, ? FROM crg_generation_payloads WHERE digest=?",
            ((generation, count, payload) for payload, count in counts.items()),
        )
    else:
        generation = row[0]
    conn.execute(
        "INSERT INTO crg_generation_bindings VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(checkout, ref) DO UPDATE SET "
        "generation_id=excluded.generation_id, head=excluded.head, built_at=excluded.built_at",
        (checkout, ref, head, built_at, generation),
    )
    conn.execute("DELETE FROM crg_generation_unbound WHERE generation_id=?", (generation,))
    if conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE name='crg_artifact_unbound'",
    ).fetchone():
        conn.execute(
            "DELETE FROM crg_artifact_unbound WHERE kind='parser' AND digest IN "
            "(SELECT file_hash FROM nodes WHERE kind='File' AND file_hash IS NOT NULL)",
        )
    if previous is not None and previous[0] != generation:
        if conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='crg_graph_input_bindings'",
        ).fetchone():
            conn.execute(
                "DELETE FROM crg_graph_input_bindings WHERE checkout=? AND ref=?",
                (checkout, ref),
            )
        conn.execute(
            "INSERT OR IGNORE INTO crg_generation_unbound "
            "SELECT ?, ? WHERE NOT EXISTS "
            "(SELECT 1 FROM crg_generation_bindings WHERE generation_id=?)",
            (previous[0], time.time(), previous[0]),
        )
    return key.hex()


def read(
    conn: Connection, root: Path, ref: str,
) -> tuple[str, str, Iterator[tuple[str, dict[str, Any]]]]:
    """Read only a generation bound to this checkout and Git reference."""
    prefix = normalize_file_path(root).rstrip("/") + "/"
    table = conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='crg_generation_bindings'",
    ).fetchone()
    binding = table and conn.execute(
        "SELECT generation_id, head, built_at FROM crg_generation_bindings "
        "WHERE checkout=? AND ref=?",
        (_checkout_key(root), ref),
    ).fetchone()
    if binding is None:
        raise GraphStoreError(f"No published graph generation for {ref} in {root}")
    rows = conn.execute(
        "SELECT p.data, m.multiplicity FROM crg_generation_members m "
        "JOIN crg_generation_payloads p ON p.id=m.payload_id "
        "WHERE m.generation_id=? ORDER BY p.digest",
        (binding[0],),
    )

    def materialize() -> Iterator[tuple[str, dict[str, Any]]]:
        for raw, count in rows:
            kind, data = json.loads(raw)
            for index in _path_fields(kind, data[0]):
                if index < len(data) and isinstance(data[index], list):
                    data[index] = prefix + data[index][0]
            row = dict(zip(_NODE_COLUMNS if kind == "nodes" else _EDGE_COLUMNS, data))
            for _ in range(count):
                yield kind, row.copy()

    return binding[1], binding[2], materialize()


def publish_projections(conn: Connection, root: Path, ref: str) -> None:
    """Record the native search and flow state for the just-published binding."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS crg_generation_projections (
            checkout BLOB NOT NULL, ref TEXT NOT NULL, data TEXT NOT NULL,
            PRIMARY KEY (checkout, ref)
        ) WITHOUT ROWID
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS crg_generation_projection_vectors (
            checkout BLOB NOT NULL, ref TEXT NOT NULL, qualified_name TEXT NOT NULL,
            provider TEXT NOT NULL, text_hash TEXT NOT NULL, payload_id INTEGER NOT NULL,
            PRIMARY KEY (checkout, ref, qualified_name)
        ) WITHOUT ROWID
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS crg_generation_vector_payload "
        "ON crg_generation_projection_vectors(payload_id)",
    )
    checkout = _checkout_key(root)
    conn.execute(
        "DELETE FROM crg_generation_projection_vectors WHERE checkout=? AND ref=?",
        (checkout, ref),
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(embeddings)")}
    if "payload_id" in columns:
        conn.execute(
            "INSERT INTO crg_generation_projection_vectors "
            "SELECT ?, ?, e.qualified_name, e.provider, e.text_hash, e.payload_id "
            "FROM embeddings e JOIN nodes n USING (qualified_name)",
            (checkout, ref),
        )
    names = dict(conn.execute("SELECT id, qualified_name FROM nodes"))
    flows = []
    for row in conn.execute(
        "SELECT id, name, entry_point_id, depth, node_count, file_count, "
        "criticality, path_json, created_at, updated_at FROM flows ORDER BY id",
    ):
        flow = list(row)
        path = json.loads(flow[7])
        if flow[2] not in names or any(node not in names for node in path):
            raise GraphStoreError(f"Flow {flow[0]} references missing graph nodes")
        flow[2] = names[flow[2]]
        flow[7] = [names[node] for node in path]
        flows.append(flow)
    communities = [list(row) for row in conn.execute(
        "SELECT id, name, level, parent_id, cohesion, size, dominant_language, "
        "description, created_at FROM communities ORDER BY id",
    )]
    members = [list(row) for row in conn.execute(
        "SELECT community_id, qualified_name FROM nodes "
        "WHERE community_id IS NOT NULL ORDER BY qualified_name",
    )]
    detector = conn.execute(
        "SELECT value FROM metadata WHERE key='community_detector'",
    ).fetchone()
    fts = conn.execute(
        "SELECT value FROM metadata WHERE key='postprocess_level'",
    ).fetchone()
    conn.execute(
        "INSERT INTO crg_generation_projections VALUES (?, ?, ?) "
        "ON CONFLICT(checkout, ref) DO UPDATE SET data=excluded.data",
        (_checkout_key(root), ref, json.dumps(
            {
                "flows": flows, "fts": bool(fts and fts[0] in ("full", "minimal")),
                "communities": communities, "community_members": members,
                "community_detector": detector[0] if detector else None,
            },
            separators=(",", ":"),
        )),
    )


def load_projections(
    source: Connection, destination: Connection, root: Path, ref: str,
    *, require_communities: bool = False,
) -> bool:
    """Restore stored flows with IDs remapped to the materialized nodes."""
    table = source.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='crg_generation_projections'",
    ).fetchone()
    row = table and source.execute(
        "SELECT data FROM crg_generation_projections WHERE checkout=? AND ref=?",
        (_checkout_key(root), ref),
    ).fetchone()
    if row is None:
        raise GraphStoreError(f"No published search and flow state for {ref}; build that ref")
    state = json.loads(row[0])
    if require_communities:
        if "communities" not in state:
            raise GraphStoreError(f"No published communities for {ref}; build that ref")
        destination.executemany(
            "INSERT INTO communities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            state.get("communities", ()),
        )
        destination.executemany(
            "UPDATE nodes SET community_id=? WHERE qualified_name=?",
            state.get("community_members", ()),
        )
        if state.get("community_detector") is not None:
            destination.execute(
                "INSERT INTO metadata VALUES ('community_detector', ?)",
                (state["community_detector"],),
            )
        return False
    ids = dict(destination.execute("SELECT qualified_name, id FROM nodes"))
    for flow in state["flows"]:
        path = [ids[name] for name in flow[7]]
        flow[2], flow[7] = ids[flow[2]], json.dumps(path)
        destination.execute("INSERT INTO flows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", flow)
        destination.executemany(
            "INSERT OR IGNORE INTO flow_memberships VALUES (?, ?, ?)",
            ((flow[0], node, position) for position, node in enumerate(path)),
        )
    if source.execute(
        "SELECT 1 FROM sqlite_schema WHERE name='crg_generation_projection_vectors'",
    ).fetchone():
        vectors = source.execute(
            "SELECT e.qualified_name, p.vector, e.text_hash, e.provider "
            "FROM crg_generation_projection_vectors e "
            "JOIN crg_vector_payloads p ON p.id=e.payload_id "
            "WHERE e.checkout=? AND e.ref=?",
            (_checkout_key(root), ref),
        ) if source.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='crg_vector_payloads'",
        ).fetchone() else ()
        destination.execute("""
            CREATE TABLE embeddings (
                qualified_name TEXT PRIMARY KEY, vector BLOB NOT NULL,
                text_hash TEXT NOT NULL, provider TEXT NOT NULL
            )
        """)
        destination.executemany("INSERT INTO embeddings VALUES (?, ?, ?, ?)", vectors)
    return bool(state["fts"])


def prune(
    conn: Connection, *, days: int, percent: int, max_bytes: int, apply: bool,
) -> tuple[int, int, int]:
    """Expire unbound generations, charging shared payload bytes only once."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE name='crg_generation_unbound'",
    ).fetchone():
        return 0, 0, 0
    active = conn.execute("""
        SELECT COALESCE(SUM(length(CAST(p.data AS BLOB))), 0)
        FROM crg_generation_payloads p WHERE EXISTS (
            SELECT 1 FROM crg_generation_members m JOIN crg_generation_bindings b
            USING (generation_id) WHERE m.payload_id=p.id
        )
    """).fetchone()[0]
    budget = min(max_bytes, active * percent // 100) if days else 0
    # Count references exclusively in dead generations. A payload shared
    # with any active generation cannot contribute to reclaimed bytes.
    exclusive = {
        payload: [size, count]
        for payload, size, count in conn.execute("""
            SELECT p.id, length(CAST(p.data AS BLOB)), count(*)
            FROM crg_generation_payloads p JOIN crg_generation_members m ON m.payload_id=p.id
            WHERE NOT EXISTS (
                SELECT 1 FROM crg_generation_members live JOIN crg_generation_bindings b
                USING (generation_id) WHERE live.payload_id=p.id
            )
            GROUP BY p.id
        """)
    }
    remaining = sum(value[0] for value in exclusive.values())
    cutoff = time.time() - days * 86400
    victims = []
    payloads = []
    reclaimed = 0
    for generation, since in conn.execute("""
        SELECT u.generation_id, u.since FROM crg_generation_unbound u
        WHERE NOT EXISTS (
            SELECT 1 FROM crg_generation_bindings b WHERE b.generation_id=u.generation_id
        ) ORDER BY u.since, u.generation_id
    """):
        if days and percent and max_bytes and since > cutoff and remaining <= budget:
            break
        victims.append((generation,))
        for (payload,) in conn.execute(
            "SELECT payload_id FROM crg_generation_members WHERE generation_id=?", (generation,),
        ):
            if payload in exclusive:
                size_and_count = exclusive[payload]
                size_and_count[1] -= 1
                if size_and_count[1] == 0:
                    remaining -= size_and_count[0]
                    reclaimed += size_and_count[0]
                    payloads.append((payload,))
    if apply:
        for table in ("crg_generation_members", "crg_generation_unbound"):
            conn.executemany(f"DELETE FROM {table} WHERE generation_id=?", victims)
        conn.executemany("DELETE FROM crg_graph_generations WHERE id=?", victims)
        conn.executemany("DELETE FROM crg_generation_payloads WHERE id=?", payloads)
    return len(victims), reclaimed, budget
