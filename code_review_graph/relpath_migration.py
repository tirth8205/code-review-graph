"""One-time conversion of an absolute-path graph to repo-relative storage.

Copy and swap inside the same database file: ``nodes`` and ``edges`` are
copied in id order into ``<table>_relpath`` with the root stripped, one batch
per transaction, then a single transaction drops the old tables, renames the
copies into place and recreates their indexes. A run that is killed resumes
from the highest id already copied; nothing is half converted, because the
old tables stay the live ones until the swap commits.

While a conversion runs, every other opener gets :class:`GraphMigratingError`
("try again"), so no process reads a mode that is about to change and no
writer adds rows the copy would miss.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from typing import Any, Optional

from .errors import GraphStoreError
from .relpaths import PATH_MODE_KEY, PATH_MODE_RELATIVE, PathCodec

logger = logging.getLogger(__name__)

MIGRATION_KEY = "relpath_migration"
MIGRATION_ROOT_KEY = "relpath_migration_root"
BATCH_ROWS = 50_000

#: Columns rewritten per table. Everything else is copied as is.
_PATH_COLUMNS: dict[str, tuple[str, ...]] = {
    "nodes": ("qualified_name", "file_path"),
    "edges": ("source_qualified", "target_qualified", "file_path"),
}


class GraphMigratingError(GraphStoreError):
    """Another process is converting this graph; the caller should retry."""

    def __init__(self, pid: int) -> None:
        super().__init__(
            f"The graph is being converted to repo-relative paths by process {pid}. "
            "This happens once per graph; try again in a moment."
        )
        self.pid = pid


def _meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", (key, value))


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def migration_holder(conn: sqlite3.Connection) -> Optional[int]:
    """PID of a live process converting this graph, other than this one."""
    try:
        value = _meta(conn, MIGRATION_KEY)
    except sqlite3.OperationalError:
        return None
    if not value:
        return None
    try:
        pid = int(value)
    except ValueError:
        return None
    if pid == os.getpid() or not _alive(pid):
        return None
    return pid


def _staging(table: str) -> str:
    return f"{table}_relpath"


def _exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,),
    ).fetchone() is not None


def _claim(conn: sqlite3.Connection, root: str) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        holder = migration_holder(conn)
        if holder is not None:
            raise GraphMigratingError(holder)
        if _meta(conn, MIGRATION_ROOT_KEY) not in (None, root):
            # Resumed against another root: the copies hold the wrong prefix.
            for table in _PATH_COLUMNS:
                conn.execute(f"DROP TABLE IF EXISTS {_staging(table)}")  # nosec B608
        _set_meta(conn, MIGRATION_KEY, str(os.getpid()))
        _set_meta(conn, MIGRATION_ROOT_KEY, root)
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def _ensure_staging(conn: sqlite3.Connection, table: str) -> int:
    """Create *table*'s copy if missing; return the highest id already copied."""
    staging = _staging(table)
    if not _exists(conn, staging):
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,),
        ).fetchone()[0]
        created, count = re.subn(
            rf"^\s*CREATE\s+TABLE\s+(\"?){table}\1", f"CREATE TABLE {staging}", ddl,
            count=1, flags=re.IGNORECASE,
        )
        if count != 1:  # pragma: no cover - our own DDL
            raise sqlite3.OperationalError(f"unexpected DDL for {table}")
        conn.execute(created)
    return conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {staging}").fetchone()[0]  # nosec B608


def _copy_batch(
    conn: sqlite3.Connection, table: str, batch: int, last: int,
) -> tuple[int, int]:
    """Copy up to *batch* source rows after id *last*.

    Returns how many source rows were read and the highest id read. The count
    of rows read, not of rows written, ends the copy: two spellings of one path
    become one key, ``INSERT OR IGNORE`` drops the second, and a short write
    must not be taken for the end of the table.
    """
    staging = _staging(table)
    columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    rewrite = _PATH_COLUMNS[table]
    select = ", ".join(f"crg_relpath({c})" if c in rewrite else c for c in columns)
    names = ", ".join(columns)
    read, upper = conn.execute(
        f"SELECT COUNT(*), MAX(id) FROM (SELECT id FROM {table} "  # nosec B608
        "WHERE id > ? ORDER BY id LIMIT ?)",
        (last, batch),
    ).fetchone()
    if not read:
        return 0, last
    cur = conn.execute(
        f"INSERT OR IGNORE INTO {staging} ({names}) "  # nosec B608
        f"SELECT {select} FROM {table} WHERE id > ? AND id <= ?",
        (last, upper),
    )
    dropped = read - cur.rowcount
    if dropped:
        logger.warning(
            "Relative paths: %d %s row(s) named the same path as another row "
            "(another spelling of the root) and were dropped", dropped, table,
        )
    return read, upper


def _copy_rest(conn: sqlite3.Connection, table: str, batch: int, last: int) -> int:
    """Copy every remaining source row of *table* after id *last*; return the new cursor."""
    while True:
        read, last = _copy_batch(conn, table, batch, last)
        if read < batch:
            return last


def _copy_table(conn: sqlite3.Connection, table: str, batch: int) -> None:
    total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # nosec B608
    started = time.monotonic()
    conn.execute("BEGIN IMMEDIATE")
    try:
        last = _ensure_staging(conn, table)
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    while True:
        conn.execute("BEGIN IMMEDIATE")
        try:
            read, last = _copy_batch(conn, table, batch, last)
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
        done = conn.execute(f"SELECT COUNT(*) FROM {_staging(table)}").fetchone()[0]  # nosec B608
        logger.info("Relative paths: %s %d/%d", table, done, total)
        if read < batch:
            break
    logger.debug("Relative paths: copied %s in %.1fs", table, time.monotonic() - started)


def _rekey_embeddings(conn: sqlite3.Connection, batch: int) -> None:
    if not _exists(conn, "embeddings"):
        return
    last = 0
    while True:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT MAX(rowid) FROM (SELECT rowid FROM embeddings "
                "WHERE rowid > ? ORDER BY rowid LIMIT ?)", (last, batch),
            ).fetchone()
            upper = row[0]
            if upper is not None:
                conn.execute(
                    "UPDATE OR IGNORE embeddings "
                    "SET qualified_name = crg_relpath_key(qualified_name) "
                    "WHERE rowid > ? AND rowid <= ?", (last, upper),
                )
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
        if upper is None:
            return
        last = upper


def _swap(conn: sqlite3.Connection, batch: int) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table in _PATH_COLUMNS:
            _copy_rest(conn, table, batch, _ensure_staging(conn, table))
            indexes = [
                row[0] for row in conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' "
                    "AND tbl_name = ? AND sql IS NOT NULL", (table,),
                )
            ]
            conn.execute(f"DROP TABLE {table}")  # nosec B608
            conn.execute(f"ALTER TABLE {_staging(table)} RENAME TO {table}")  # nosec B608
            for ddl in indexes:
                conn.execute(ddl)
        _set_meta(conn, PATH_MODE_KEY, PATH_MODE_RELATIVE)
        conn.execute(
            "DELETE FROM metadata WHERE key IN (?, ?)", (MIGRATION_KEY, MIGRATION_ROOT_KEY),
        )
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def convert_to_relative(
    conn: sqlite3.Connection, root: str, *, batch: int = BATCH_ROWS,
) -> None:
    """Rewrite an absolute graph's paths relative to *root* (see module docstring).

    *conn* must be an untranslated connection (absolute mode). Raises
    :class:`GraphMigratingError` when another live process is already converting.
    """
    codec = PathCodec(root)

    def _strip(value: Any) -> Any:
        return codec.strip(value) if isinstance(value, str) else value

    def _strip_root(value: Any) -> Any:
        return codec.strip_root(value) if isinstance(value, str) else value

    conn.create_function("crg_relpath", 1, _strip, deterministic=True)
    # In place, so it must be idempotent: a resumed run sees ``./`` keys again,
    # and escaping would change them. Embedding keys are node names, which
    # never start with ``./`` of their own.
    conn.create_function("crg_relpath_key", 1, _strip_root, deterministic=True)
    _claim(conn, codec.root)
    started = time.monotonic()
    logger.info("Converting the graph to repo-relative paths (one time)")
    try:
        for table in _PATH_COLUMNS:
            _copy_table(conn, table, batch)
        _rekey_embeddings(conn, batch)
        _swap(conn, batch)
    except BaseException:
        # Leave the copies for the next opener to resume; free the claim so it
        # does not have to wait for this PID to disappear.
        try:
            conn.execute("DELETE FROM metadata WHERE key = ?", (MIGRATION_KEY,))
            conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Could not release the relative-path conversion claim: %s", exc)
        raise
    logger.info(
        "Converted the graph to repo-relative paths in %.1fs", time.monotonic() - started,
    )
