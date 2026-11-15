"""Immutable node and edge snapshots, with checkout-local Git reference bindings."""

from __future__ import annotations

import hashlib
import json
import os
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
    "extra", "modifiers", "symbol", "docstring", "name_tokens",
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
    return (1, 2, 3, _NODE_COLUMNS.index("symbol")) if row_kind == "File" else (2, 3)


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


def publish(conn: Connection, root: Path, ref: str, head: str, built_at: str) -> str:
    """Intern immutable rows and move only this checkout/reference binding."""
    for statement in _SCHEMA:
        conn.execute(statement)
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
        (_checkout_key(root), ref, head, built_at, generation),
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
                if isinstance(data[index], list):
                    data[index] = prefix + data[index][0]
            row = dict(zip(_NODE_COLUMNS if kind == "nodes" else _EDGE_COLUMNS, data))
            for _ in range(count):
                yield kind, row.copy()

    return binding[1], binding[2], materialize()
