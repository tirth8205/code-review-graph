"""Read-only raw SQL query against the graph database."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any
from urllib.parse import quote

import sqlglot
from sqlglot import expressions as exp

_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000
_DEFAULT_TIMEOUT = 10


class QueryError(Exception):
    """Structured error raised when a query violates safety rules or fails at runtime."""

    def __init__(self, message: str, error_type: str) -> None:
        super().__init__(message)
        self.error_type = error_type

    def to_dict(self) -> dict[str, str]:
        return {"error": str(self), "type": self.error_type}


def _validate_sql(sql: str) -> None:
    """Raise QueryError if the SQL statement violates safety constraints.

    Uses sqlglot to parse into an AST so string literals, double-quoted
    identifiers, and comments can never fool the keyword check.
    """
    try:
        statements = sqlglot.parse(sql, dialect="sqlite",
                                   error_level=sqlglot.ErrorLevel.RAISE)
    except sqlglot.errors.ParseError as exc:
        raise QueryError(str(exc), "sql_syntax_error") from exc

    if not statements:
        raise QueryError("empty query", "disallowed_statement")

    if len(statements) > 1:
        raise QueryError("only a single SQL statement is permitted",
                         "disallowed_statement")

    stmt = statements[0]

    if isinstance(stmt, exp.Pragma):
        raise QueryError("PRAGMA statements are not permitted", "disallowed_pragma")

    # exp.Select covers both plain SELECT and CTEs (WITH ... SELECT).
    if not isinstance(stmt, exp.Select):
        raise QueryError("only SELECT statements are permitted", "disallowed_statement")


def run_query(
    db_path: Path,
    sql: str,
    params: dict[str, Any] | None = None,
    limit: int = _DEFAULT_LIMIT,
    timeout: int = _DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Execute a read-only SELECT against the graph database.

    Opens the database in read-only URI mode so writes are rejected at the
    connection level, not just by statement inspection.

    Raises:
        QueryError: on safety violations, syntax errors, timeout, or missing DB.
    """
    limit = min(max(1, limit), _MAX_LIMIT)

    _validate_sql(sql)

    db_uri = f"file:{quote(str(db_path))}?mode=ro"
    try:
        conn = sqlite3.connect(db_uri, uri=True, check_same_thread=False)
    except sqlite3.OperationalError as exc:
        raise QueryError(str(exc), "db_error") from exc

    conn.row_factory = sqlite3.Row
    timed_out = threading.Event()

    def _on_timeout() -> None:
        timed_out.set()
        conn.interrupt()

    timer = threading.Timer(timeout, _on_timeout)
    timer.start()
    try:
        cur = conn.execute(sql, params or {})
        # Pull one extra row to detect truncation. We cap at the driver level
        # rather than appending "LIMIT N" to the SQL so the user's query runs
        # unmodified — appending LIMIT would break queries that already contain one.
        rows = cur.fetchmany(limit + 1)
        return [dict(row) for row in rows[:limit]]
    except sqlite3.OperationalError as exc:
        if timed_out.is_set():
            raise QueryError(f"query exceeded timeout of {timeout}s", "timeout") from exc
        raise QueryError(str(exc), "sql_error") from exc
    except sqlite3.DatabaseError as exc:
        raise QueryError(str(exc), "sql_syntax_error") from exc
    finally:
        timer.cancel()
        conn.close()


def format_table(rows: list[dict[str, Any]]) -> str:
    """Format a list of row dicts as a pipe-delimited ASCII table."""
    if not rows:
        return "(no results)"

    columns = list(rows[0].keys())

    def _str(v: Any) -> str:
        return "NULL" if v is None else str(v)

    str_rows = [{col: _str(row.get(col)) for col in columns} for row in rows]
    col_widths = {
        col: max(len(col), max(len(r[col]) for r in str_rows))
        for col in columns
    }

    header = " | ".join(col.ljust(col_widths[col]) for col in columns)
    separator = "-+-".join("-" * col_widths[col] for col in columns)
    data_lines = [
        " | ".join(r[col].ljust(col_widths[col]) for col in columns)
        for r in str_rows
    ]

    return "\n".join([header, separator, *data_lines])
