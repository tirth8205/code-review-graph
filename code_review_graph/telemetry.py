"""Local aggregate telemetry for token-efficiency dashboards."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    surface TEXT NOT NULL,
    operation TEXT NOT NULL,
    estimated_baseline_tokens INTEGER NOT NULL,
    estimated_payload_tokens INTEGER NOT NULL,
    estimated_saved_tokens INTEGER NOT NULL,
    extra TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_telemetry_events_created_at
    ON telemetry_events(created_at);
CREATE INDEX IF NOT EXISTS idx_telemetry_events_operation
    ON telemetry_events(operation);
"""


def estimate_tokens_from_chars(char_count: int) -> int:
    if char_count <= 0:
        return 0
    return max(1, int(char_count / 4))


def record_event(
    db_path: str | Path,
    *,
    surface: str,
    operation: str,
    baseline_tokens: int,
    payload_tokens: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    saved_tokens = max(baseline_tokens - payload_tokens, 0)
    with _connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO telemetry_events (
                created_at, surface, operation, estimated_baseline_tokens,
                estimated_payload_tokens, estimated_saved_tokens, extra
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                time.time(),
                surface,
                operation,
                baseline_tokens,
                payload_tokens,
                saved_tokens,
                json.dumps(extra or {}, separators=(",", ":"), sort_keys=True),
            ),
        )
        conn.commit()
    return {
        "status": "ok",
        "surface": surface,
        "operation": operation,
        "estimated_baseline_tokens": baseline_tokens,
        "estimated_payload_tokens": payload_tokens,
        "estimated_saved_tokens": saved_tokens,
    }


def summarize_events(db_path: str | Path) -> dict[str, Any]:
    with _connect(db_path) as conn:
        _ensure_schema(conn)
        total = conn.execute(
            """
            SELECT
                COUNT(*) AS request_count,
                COALESCE(SUM(estimated_baseline_tokens), 0) AS baseline_tokens,
                COALESCE(SUM(estimated_payload_tokens), 0) AS payload_tokens,
                COALESCE(SUM(estimated_saved_tokens), 0) AS saved_tokens
            FROM telemetry_events
            """
        ).fetchone()
        by_operation_rows = conn.execute(
            """
            SELECT
                operation,
                COUNT(*) AS request_count,
                COALESCE(SUM(estimated_saved_tokens), 0) AS saved_tokens
            FROM telemetry_events
            GROUP BY operation
            ORDER BY request_count DESC, operation ASC
            """
        ).fetchall()

    request_count = int(total["request_count"])
    saved_tokens = int(total["saved_tokens"])
    return {
        "status": "estimated",
        "method": "chars_div_4",
        "scope": "observed local API operations",
        "total_requests": request_count,
        "estimated_baseline_tokens": int(total["baseline_tokens"]),
        "estimated_payload_tokens": int(total["payload_tokens"]),
        "estimated_saved_tokens": saved_tokens,
        "average_saved_tokens": round(saved_tokens / request_count, 2)
        if request_count else 0.0,
        "by_operation": [
            {
                "operation": row["operation"],
                "request_count": int(row["request_count"]),
                "estimated_saved_tokens": int(row["saved_tokens"]),
            }
            for row in by_operation_rows
        ],
    }


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=1000")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
