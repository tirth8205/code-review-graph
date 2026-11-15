"""Content-addressed vector payloads and their proven Git memberships."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

_PAYLOADS = """
CREATE TABLE IF NOT EXISTS crg_vector_payloads (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    vector_sha TEXT NOT NULL,
    vector BLOB NOT NULL,
    link_count INTEGER NOT NULL DEFAULT 0,
    orphaned_at REAL,
    UNIQUE (provider, text_hash, vector_sha)
)
"""
_SCOPES = """
CREATE TABLE IF NOT EXISTS crg_vector_scopes (
    scope TEXT NOT NULL,
    provider TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    payload_id INTEGER NOT NULL REFERENCES crg_vector_payloads(id),
    PRIMARY KEY (scope, provider, text_hash)
)
"""
_LINKS = """
CREATE TABLE IF NOT EXISTS embeddings (
    qualified_name TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    payload_id INTEGER NOT NULL REFERENCES crg_vector_payloads(id)
)
"""
_ADOPT_PAYLOAD = (
    "UPDATE crg_vector_payloads SET link_count=link_count+1, orphaned_at=NULL "
    "WHERE id=NEW.payload_id;"
)
_RELEASE_PAYLOAD = (
    "UPDATE crg_vector_payloads SET link_count=link_count-1, "
    "orphaned_at=CASE WHEN link_count=1 THEN CAST(strftime('%s','now') AS INTEGER) ELSE NULL END "
    "WHERE id=OLD.payload_id;"
)


def columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(embeddings)")}


@contextmanager
def _write(conn: sqlite3.Connection) -> Iterator[None]:
    # CheckoutConnection maps this to a savepoint inside the native refresh.
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def initialize(conn: sqlite3.Connection) -> None:
    """Migrate one checkout atomically, retaining its legacy vector bindings."""
    if "payload_id" in columns(conn):
        return
    with _write(conn):
        old = columns(conn)
        if "payload_id" in old:
            return
        conn.execute(_PAYLOADS)
        conn.execute(_SCOPES)
        if old:
            conn.execute("ALTER TABLE embeddings RENAME TO embeddings_before_payloads")
        conn.execute(_LINKS)
        conn.execute("CREATE TRIGGER trg_vectors_insert AFTER INSERT ON embeddings "
                     f"BEGIN {_ADOPT_PAYLOAD} END")
        conn.execute("CREATE TRIGGER trg_vectors_delete AFTER DELETE ON embeddings "
                     f"BEGIN {_RELEASE_PAYLOAD} END")
        conn.execute("CREATE TRIGGER trg_vectors_update AFTER UPDATE OF payload_id ON embeddings "
                     "WHEN OLD.payload_id!=NEW.payload_id "
                     f"BEGIN {_RELEASE_PAYLOAD} {_ADOPT_PAYLOAD} END")
        if old:
            provider = "provider" if "provider" in old else "'unknown'"
            for name, vector, key, identity in conn.execute(
                f"SELECT qualified_name, vector, text_hash, {provider} "
                "FROM embeddings_before_payloads ORDER BY qualified_name"
            ):
                payload = _intern(conn, identity, key, vector)
                conn.execute("INSERT INTO embeddings VALUES (?, ?, ?, ?)",
                             (name, identity, key, payload))
            # Legacy rows have no provenance for cross-checkout reuse. The
            # migration keeps their original bindings but publishes no scopes.
            conn.execute("DROP TABLE embeddings_before_payloads")


def _intern(conn: sqlite3.Connection, provider: str, key: str, vector: bytes) -> int:
    digest = hashlib.sha256(vector).hexdigest()
    conn.execute(
        "INSERT OR IGNORE INTO crg_vector_payloads (provider, text_hash, vector_sha, vector) "
        "VALUES (?, ?, ?, ?)",
        (provider, key, digest, vector),
    )
    return int(conn.execute(
        "SELECT id FROM crg_vector_payloads WHERE provider=? AND text_hash=? AND vector_sha=?",
        (provider, key, digest),
    ).fetchone()[0])


def reusable(
    conn: sqlite3.Connection, provider: str, keys: Sequence[str], scopes: Sequence[str],
) -> dict[str, bytes]:
    """Find matching payloads through the current checkout or direct peers."""
    found: dict[str, bytes] = {}
    conflicts: set[str] = set()
    marks = ",".join("?" for _ in keys)

    def collect(rows: sqlite3.Cursor) -> None:
        for key, vector in rows:
            if found.setdefault(key, vector) != vector:
                conflicts.add(key)

    collect(conn.execute(
        "SELECT e.text_hash, p.vector FROM embeddings e "
        "JOIN crg_vector_payloads p ON p.id=e.payload_id "
        f"WHERE e.provider=? AND e.text_hash IN ({marks})",
        (provider, *keys),
    ))
    for scope in scopes:
        collect(conn.execute(
            "SELECT s.text_hash, p.vector FROM crg_vector_scopes s "
            "JOIN crg_vector_payloads p ON p.id=s.payload_id "
            f"WHERE s.scope=? AND s.provider=? AND s.text_hash IN ({marks})",
            (scope, provider, *keys),
        ))
    return {key: vector for key, vector in found.items() if key not in conflicts}


def link(
    conn: sqlite3.Connection,
    rows: Sequence[tuple[str, bytes, str, str]],
    scopes: Sequence[str],
    generated: set[str],
) -> None:
    """Publish symbols and the payloads observed for those symbols together."""
    with _write(conn):
        payloads: dict[tuple[str, str], int] = {}
        for name, vector, key, provider in rows:
            identity = (provider, key)
            if identity not in payloads:
                payloads[identity] = _intern(conn, provider, key, vector)
            conn.execute(
                "INSERT INTO embeddings VALUES (?, ?, ?, ?) "
                "ON CONFLICT(qualified_name) DO UPDATE SET provider=excluded.provider, "
                "text_hash=excluded.text_hash, payload_id=excluded.payload_id",
                (name, provider, key, payloads[identity]),
            )
        # A peer's hit creates a checkout link, not a new origin for the
        # payload. Otherwise a repo with two remotes could bridge two pools.
        conn.executemany(
            "INSERT OR REPLACE INTO crg_vector_scopes VALUES (?, ?, ?, ?)",
            ((scope, provider, key, payload)
             for (provider, key), payload in payloads.items() if key in generated
             for scope in scopes),
        )


def prune(
    conn: sqlite3.Connection, *, days: int, percent: int, max_bytes: int, apply: bool,
) -> tuple[int, int, int]:
    """Report (objects, bytes, budget) for unreachable vector payloads."""
    active, orphan = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN link_count>0 THEN length(vector) END),0), "
        "COALESCE(SUM(CASE WHEN link_count=0 THEN length(vector) END),0) "
        "FROM crg_vector_payloads",
    ).fetchone()
    budget = min(max_bytes, active * percent // 100) if days else 0
    cutoff = time.time() - days * 86400
    victims = []
    reclaimed = 0
    for payload, size, unlinked_at in conn.execute(
        "SELECT id, length(vector), orphaned_at FROM crg_vector_payloads "
        "WHERE link_count=0 ORDER BY orphaned_at, id",
    ):
        if days and unlinked_at is not None and unlinked_at > cutoff and orphan <= budget:
            break
        victims.append((payload,))
        orphan -= size
        reclaimed += size
    if apply and victims:
        conn.executemany("DELETE FROM crg_vector_scopes WHERE payload_id=?", victims)
        conn.executemany("DELETE FROM crg_vector_payloads WHERE id=? AND link_count=0", victims)
    return len(victims), reclaimed, budget
