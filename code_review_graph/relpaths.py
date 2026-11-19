"""Repo-relative path storage at the SQLite connection boundary.

A graph in ``path_mode = relative`` stores ``file_path`` values and the path
part of qualified names relative to the repository root. Everything outside
the database keeps seeing absolute paths: parameters that start with the root
are stripped on the way in, and path columns get the root back on the way
out. The root is taken from where the database sits, so a ``graph.db`` copied
into another checkout (a linked worktree) answers with that checkout's paths.

In-repo values are stored as ``./<path>`` (the root itself as ``.``), so they
are told apart from bare names (``Redis::get``, ``helper``) without a lookup.
Values outside the root (followed symlink targets, foreign files) are stored
absolute. A raw value that would read as relative (``./missing``, an
unresolved import specifier) is stored with a leading ``!`` and read back as
written.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

PATH_MODE_KEY = "path_mode"
PATH_MODE_RELATIVE = "relative"

# The repository root itself, as a directory-scoped edge target names it.
ROOT_TOKEN = "."
# Every in-repo value starts with this, so a reader can tell it from a bare
# name without looking anything up: ``./pkg/b.py``, ``./b.py::Helper.ping``.
REL_PREFIX = "./"
# Escapes a raw value that would read as relative, such as the unresolved
# import specifier ``./missing``: stored ``!./missing``, read back unchanged.
ESCAPE = "!"

# Metadata columns: metadata statements are not translated on the way in.
_UNTRANSLATED_COLUMNS = frozenset({"key", "value"})
# Statements on the metadata table, whose values are opaque strings
# (``repo_root`` is the root itself): their parameters are not translated. The
# table name is matched as a table reference, so a graph statement that merely
# mentions the word (a comment, a JSON key) is still translated.
_METADATA_STATEMENT = re.compile(
    r"\b(?:FROM|INTO|UPDATE|JOIN)\s+[\"`\[]?metadata\b", re.IGNORECASE,
)


def _translates(sql: str) -> bool:
    return _METADATA_STATEMENT.search(sql) is None


def is_absolute_graph_path(value: str) -> bool:
    """True for a POSIX absolute path or a Windows drive path."""
    return value.startswith("/") or (len(value) > 1 and value[1] == ":")


def _looks_relative(value: str) -> bool:
    """Would *value*, read back from a relative graph, be taken as in-repo?"""
    body = value.lstrip(ESCAPE)
    return body == ROOT_TOKEN or body.startswith((REL_PREFIX, "../"))


def root_variants(root: str) -> tuple[str, ...]:
    """Spellings of *root* that stored paths may start with, longest first."""
    variants = {root}
    try:
        variants.add(Path(root).resolve().as_posix())
    except OSError:
        pass
    # macOS: /var and /tmp are links into /private.
    for value in list(variants):
        if value.startswith("/private/"):
            variants.add(value[len("/private"):])
    return tuple(sorted(variants, key=len, reverse=True))


def absolute_root(root: str) -> str:
    """*root* made absolute, keeping the spelling it was given (no symlinks)."""
    value = Path(root).absolute().as_posix()
    return value.rstrip("/") or "/"


#: Metadata key: the root spelling the graph was last written with.
ROOT_SPELLING_KEY = "relpath_root"
#: Metadata key: the root of the checkout a copied graph was built in, set when
#: a writer re-anchors it to another directory and cleared by a full build.
#: ``extra`` JSON is not translated and still names that root, so the next
#: update rebuilds instead of trusting it.
MOVED_FROM_KEY = "relpath_moved_from"


def names_another_directory(recorded: Optional[str], root: str) -> bool:
    """True when *recorded* is set and is not *root* under another spelling."""
    if not recorded or recorded == root:
        return False
    try:
        return not os.path.samefile(recorded, root)
    except OSError:
        # The old checkout is gone or unreadable: it is not this one.
        return True


def preferred_root(root: str, recorded: Optional[str]) -> str:
    """*root*, or the *recorded* spelling when both name the same directory.

    Absolute storage kept whatever spelling the writer used (``/var/...`` and
    ``/private/var/...`` on macOS), and ``extra`` JSON still holds it. Reusing
    the recorded spelling keeps paths in agreement with it; a graph copied to
    another checkout names a different directory and takes the new one.
    """
    root = absolute_root(root)
    if not recorded or recorded == root:
        return root
    try:
        if os.path.samefile(recorded, root):
            return recorded
    except OSError:
        pass
    return root


class PathCodec:
    """Strips the root from values going in and restores it coming out."""

    def __init__(self, root: str) -> None:
        self.root = absolute_root(root)
        self._roots = root_variants(self.root)
        self._prefixes = tuple(v + "/" for v in self._roots)
        self._lock = threading.Lock()
        self._specs: dict[int, tuple[Any, tuple[int, ...]]] = {}

    # -- inbound -------------------------------------------------------------

    def strip_root(self, value: str) -> str:
        """``<root>/x`` -> ``./x`` and ``<root>`` -> ``.``; nothing else changes."""
        for prefix in self._prefixes:
            if value.startswith(prefix):
                return REL_PREFIX + value[len(prefix):]
        if value in self._roots:
            return ROOT_TOKEN
        return value

    def strip(self, value: str) -> str:
        """The stored form of *value*: root stripped, or escaped if it needs it."""
        stripped = self.strip_root(value)
        if stripped is value and _looks_relative(value):
            return ESCAPE + value
        return stripped

    def strip_params(self, params: Any) -> Any:
        if isinstance(params, dict):
            return {k: self.strip(v) if isinstance(v, str) else v for k, v in params.items()}
        if isinstance(params, (tuple, list)):
            return tuple(self.strip(v) if isinstance(v, str) else v for v in params)
        return params

    # -- outbound ------------------------------------------------------------

    def restore(self, value: Any) -> Any:
        """The value a caller sees for a stored path or qualified name."""
        if not isinstance(value, str) or not value:
            return value
        first = value[0]
        if first == ".":
            if value == ROOT_TOKEN:
                return self.root
            if value.startswith(REL_PREFIX):
                return self.root + value[1:]
        elif first == ESCAPE and _looks_relative(value):
            return value[1:]
        return value

    # Kept as names for callers that pick by column kind.
    restore_path = restore
    restore_qn = restore

    def _spec(self, cursor: sqlite3.Cursor) -> tuple[int, ...]:
        desc = cursor.description
        key = id(desc)
        entry = self._specs.get(key)
        if entry is not None and entry[0] is desc:
            return entry[1]
        # Every string parameter is stripped or escaped on the way in, so every
        # column is restored on the way out, aliases (``AS endpoint``) included.
        # Metadata statements are not translated, so their columns are skipped.
        result = tuple(
            index for index, column in enumerate(desc or ())
            if column[0].lower() not in _UNTRANSLATED_COLUMNS
        )
        with self._lock:
            if len(self._specs) > 512:
                self._specs.clear()
            self._specs[key] = (desc, result)
        return result

    def row_factory(self, cursor: sqlite3.Cursor, row: tuple) -> sqlite3.Row:
        spec = self._spec(cursor)
        if spec:
            values = None
            for index in spec:
                value = row[index]
                if isinstance(value, str) and value[:1] in (".", ESCAPE):
                    if values is None:
                        values = list(row)
                    values[index] = self.restore(value)
            if values is not None:
                row = tuple(values)
        return sqlite3.Row(cursor, row)


class TranslatingConnection(sqlite3.Connection):
    """``sqlite3.Connection`` that applies a :class:`PathCodec` when bound."""

    codec: Optional[PathCodec] = None

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:  # type: ignore[override]
        codec = self.codec
        if codec is not None and _translates(sql):
            parameters = codec.strip_params(parameters)
        return super().execute(sql, parameters)

    def executemany(  # type: ignore[override]
        self, sql: str, parameters: Iterable[Any], /,
    ) -> sqlite3.Cursor:
        codec = self.codec
        if codec is not None and _translates(sql):
            parameters = (codec.strip_params(p) for p in parameters)
        return super().executemany(sql, parameters)

    def bind_root(self, root: Optional[str]) -> None:
        """Turn translation on for *root*, or off with ``None``."""
        if root:
            self.codec = PathCodec(root)
            self.row_factory = self.codec.row_factory
        else:
            self.codec = None
            self.row_factory = sqlite3.Row


def bind_relative_root(conn: TranslatingConnection, db_path: Path) -> None:
    """Bind *conn* the way :class:`GraphStore` would, for side stores.

    For connections that open ``graph.db`` on their own (embeddings): the root
    is the checkout around ``.code-review-graph``, else the recorded
    ``repo_root``. An absolute graph, or one with no known root, is left alone.
    """
    try:
        rows = dict(sqlite3.Connection.execute(
            conn, "SELECT key, value FROM metadata WHERE key IN (?, ?)",
            (PATH_MODE_KEY, "repo_root"),
        ).fetchall())
        recorded = sqlite3.Connection.execute(
            conn, "SELECT value FROM metadata WHERE key = ?", (ROOT_SPELLING_KEY,),
        ).fetchone()
    except sqlite3.OperationalError:
        return
    if rows.get(PATH_MODE_KEY) != PATH_MODE_RELATIVE:
        return
    parent = Path(db_path).parent
    root = str(parent.parent) if parent.name == ".code-review-graph" else rows.get("repo_root")
    if root:
        conn.bind_root(preferred_root(root, recorded[0] if recorded else None))
