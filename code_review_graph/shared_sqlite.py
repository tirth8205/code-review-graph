"""Checkout namespaces and refresh transactions for one native SQLite file."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from .errors import GraphStoreError
from .migrations import RETIRED_TABLES

SHARED_DB_ENV = "CRG_SHARED_DB"


class UnbuiltCheckoutError(GraphStoreError):
    """The shared file exists but this checkout has no graph yet."""

_TABLES = frozenset({
    "nodes", "edges", "metadata", "flows", "flow_memberships", "communities",
    "nodes_fts",
    "nodes_fts_state", "embeddings",
}) | RETIRED_TABLES
_TOKEN = re.compile(
    r"--[^\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\b\w+\b",
)


@lru_cache(maxsize=512)
def _qualify_sql(prefix: str, sql: str) -> str:
    """Reuse bounded SQL translations by checkout and statement."""
    def qualify(match: re.Match[str]) -> str:
        token = match[0]
        if token in _TABLES or token.startswith("idx_"):
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


def verify_shared_file(conn: CheckoutConnection, path: Path, *, initialize: bool) -> None:
    """Refuse an existing SQLite database that is not a CRG shared store."""
    if initialize:
        conn.execute("BEGIN IMMEDIATE")
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        if "_crg_shared" in tables:
            if [row[0] for row in conn.execute("SELECT version FROM _crg_shared")] != [1]:
                raise GraphStoreError(f"Unsupported CRG shared schema in {path}")
        elif tables or not initialize:
            raise GraphStoreError(f"{path} is not a CRG shared database")
        else:
            conn.execute("CREATE TABLE _crg_shared (version INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO _crg_shared VALUES (1)")
        if initialize:
            conn.commit()
    except BaseException:
        if initialize:
            conn.rollback()
        raise


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
