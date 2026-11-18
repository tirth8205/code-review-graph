"""Checkout namespaces and refresh transactions for one native SQLite file."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import shared_generations, shared_vectors
from .errors import GraphStoreError
from .migrations import RETIRED_TABLES

SHARED_DB_ENV = "CRG_SHARED_DB"
_SHARED_VERSION = 3


class UnbuiltCheckoutError(GraphStoreError):
    """The shared file exists but this checkout has no graph yet."""

_TABLES = frozenset({
    "nodes", "edges", "metadata", "flows", "flow_memberships", "communities",
    "nodes_fts",
    "nodes_fts_state", "embeddings", "embeddings_before_payloads",
}) | RETIRED_TABLES
_TOKEN = re.compile(
    r"--[^\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\b\w+\b",
)


@lru_cache(maxsize=512)
def _qualify_sql(prefix: str, sql: str) -> str:
    """Reuse bounded SQL translations by checkout and statement."""
    def qualify(match: re.Match[str]) -> str:
        token = match[0]
        if token in _TABLES or token.startswith(("idx_", "trg_")):
            return prefix + token
        if token in {"'nodes'", "'embeddings'"}:
            return "'" + prefix + token[1:-1] + "'"
        return token

    return _TOKEN.sub(qualify, sql)


def shared_database() -> Path | None:
    """Resolve the explicitly selected shared file."""
    if "CRG_SHARED_FILE_SUMMARY_DB" in os.environ:
        raise ValueError(f"CRG_SHARED_FILE_SUMMARY_DB is retired; select {SHARED_DB_ENV}")
    value = os.environ.get(SHARED_DB_ENV)
    return Path(value).expanduser().resolve() if value else None


class CheckoutConnection(sqlite3.Connection):
    """Bind CRG's native schema to one checkout, including its FTS content."""

    _prefix = ""
    _atomic = False
    _step = False

    def bind(self, root: Path) -> None:
        checkout = os.path.normcase(str(root.resolve()))
        self._prefix = "crg_" + hashlib.sha256(os.fsencode(checkout)).hexdigest() + "_"

    def table(self, name: str) -> str:
        return self._prefix + name

    def _sql(self, sql: str) -> str:
        if not self._prefix:
            return sql

        return _qualify_sql(self._prefix, sql)

    @property
    def refresh_active(self) -> bool:
        return self._atomic

    @property
    def in_transaction(self) -> bool:
        return self._step if self._atomic else super().in_transaction

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        if self._atomic and sql.strip().upper().startswith("BEGIN"):
            cursor = super().execute("SAVEPOINT crg_step")
            self._step = True
            return cursor
        if parameters and "FROM sqlite_master" in sql and "AND name = ?" in sql:
            name = parameters[0]
            if name in _TABLES:
                parameters = (self.table(name), *parameters[1:])
        return super().execute(self._sql(sql), parameters)

    def executemany(self, sql: str, parameters: Iterable[Any], /) -> sqlite3.Cursor:
        return super().executemany(self._sql(sql), parameters)

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
        if self._atomic:
            raise RuntimeError("A refresh cannot run a script that implicitly commits")
        return super().executescript(self._sql(sql_script))

    def commit(self) -> None:
        if not self._atomic:
            super().commit()
        elif self._step:
            super().execute("RELEASE crg_step")
            self._step = False

    def rollback(self) -> None:
        if not self._atomic:
            super().rollback()
        elif self._step:
            super().execute("ROLLBACK TO crg_step")
            super().execute("RELEASE crg_step")
            self._step = False
        else:
            raise RuntimeError("No graph write step to roll back")

    @contextmanager
    def refresh(self) -> Iterator[None]:
        """Keep native graph write steps in one SQLite publication."""
        super().execute("BEGIN IMMEDIATE")
        self._atomic = True
        try:
            yield
            if self._step:
                raise RuntimeError("Unfinished graph write step")
            super().commit()
        except BaseException:
            super().rollback()
            raise
        finally:
            self._atomic = self._step = False


def _migrate_namespaces(conn: CheckoutConnection, tables: set[str], version: int) -> None:
    """Preserve the native checkout tables and seed confirmed current bindings."""
    prefix = conn._prefix
    conn.execute("""
        CREATE TABLE IF NOT EXISTS crg_shared_migration_issues (
            namespace TEXT PRIMARY KEY, reason TEXT NOT NULL
        ) WITHOUT ROWID
    """)
    try:
        for table in sorted(tables):
            if not re.fullmatch(r"crg_[0-9a-f]{64}_nodes", table):
                continue
            namespace = table.removesuffix("nodes")
            conn._prefix = namespace
            if version == 1 and conn.table("embeddings") in tables:
                shared_vectors.initialize(conn)
            metadata = dict(conn.execute("SELECT key, value FROM metadata"))
            if not metadata.get("last_build_type"):
                continue
            root = Path(metadata.get("repo_root") or ".")
            if root.is_absolute():
                conn.bind(root)
            if not root.is_absolute() or conn._prefix != namespace:
                reason = "unverified checkout root"
            elif metadata.get("build_state") == "in-progress":
                reason = "incomplete checkout graph"
            else:
                ref = shared_generations.git_ref(
                    metadata.get("git_branch", ""), metadata.get("git_head_sha", ""),
                )
                head = metadata.get("git_head_sha", "")
                shared_generations.publish(
                    conn, root, ref, head, metadata.get("last_updated", ""),
                )
                shared_generations.publish_projections(conn, root, ref)
                continue
            conn.execute(
                "INSERT OR REPLACE INTO crg_shared_migration_issues VALUES (?, ?)",
                (namespace, reason),
            )
    finally:
        conn._prefix = prefix


def verify_shared_file(
    conn: CheckoutConnection, path: Path, *, initialize: bool, validate: bool = False,
) -> int:
    """Refuse an existing SQLite database that is not a CRG shared store."""
    if initialize:
        conn.execute("BEGIN IMMEDIATE")
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        if "_crg_shared" in tables:
            versions = [row[0] for row in conn.execute("SELECT version FROM _crg_shared")]
            if versions not in ([1], [2], [_SHARED_VERSION]):
                raise GraphStoreError(f"Unsupported CRG shared schema in {path}")
            version = versions[0]
            if version < _SHARED_VERSION and initialize:
                shared_generations.initialize(conn)
                _migrate_namespaces(conn, tables, version)
                conn.execute("UPDATE _crg_shared SET version=?", (_SHARED_VERSION,))
        elif tables or not initialize:
            raise GraphStoreError(f"{path} is not a CRG shared database")
        else:
            version = _SHARED_VERSION
            shared_generations.initialize(conn)
            conn.execute("CREATE TABLE _crg_shared (version INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO _crg_shared VALUES (?)", (_SHARED_VERSION,))
        if validate or (initialize and version < _SHARED_VERSION):
            check = conn.execute("PRAGMA integrity_check").fetchall()
            orphan = conn.execute("PRAGMA foreign_key_check").fetchone()
            if len(check) != 1 or check[0][0] != "ok" or orphan:
                raise GraphStoreError(f"Shared migration integrity check failed for {path}")
        if initialize:
            conn.commit()
        return version
    except BaseException:
        if initialize:
            conn.rollback()
        raise


def migrate_selected_database() -> tuple[int, int, int]:
    """Migrate the existing selected shared database under the writer lock."""
    from .locking import WriteLock

    path = shared_database()
    if path is None or not path.is_file():
        raise GraphStoreError("migrate-shared requires an existing CRG_SHARED_DB")
    lock = WriteLock(path)
    if not lock.acquire():
        raise GraphStoreError(f"Cannot migrate {path}: shared graph writer holds the lock")
    try:
        with closing(sqlite3.connect(
            path.as_uri() + "?mode=rw", uri=True, isolation_level=None, factory=CheckoutConnection,
        )) as conn:
            verify_shared_file(conn, path, initialize=False)
            previous = verify_shared_file(conn, path, initialize=True, validate=True)
            issues = conn.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='crg_shared_migration_issues'",
            ).fetchone()
            count = conn.execute(
                "SELECT count(*) FROM crg_shared_migration_issues",
            ).fetchone()[0] if issues else 0
            return previous, _SHARED_VERSION, count
    finally:
        lock.release()


def connect_for_checkout(
    uri: str, path: Path, checkout: Path, *, timeout: float,
) -> CheckoutConnection:
    """Open read probes with the same namespace as the native graph."""
    shared = shared_database() == path.resolve()
    if shared:
        uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout, factory=CheckoutConnection)
    if shared:
        try:
            verify_shared_file(conn, path, initialize=False)
            conn.bind(checkout)
        except BaseException:
            conn.close()
            raise
    return conn
