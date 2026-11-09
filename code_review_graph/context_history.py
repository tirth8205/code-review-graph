"""Opt-in local log of context savings, and the reader behind ``context-history``.

Issue #813. Off unless ``CRG_CONTEXT_HISTORY`` is set to ``1``, ``true``,
``yes`` or ``on``. When it is on, every response that carries a
``context_savings`` estimate also appends one JSON line to
``context-history.jsonl`` next to ``graph.db``. Nothing is sent anywhere.

One line holds only numbers and fixed labels::

    {"v":1,"ts":"2026-11-09T18:59:01Z","tool":"detect_changes_tool",
     "baseline_tokens":12932,"returned_tokens":773,"saved_tokens":12159,
     "detail_level":"minimal","files":3,"depth":2}

No file names, paths, symbol names, source text or free text from a client
is ever written: ``tool`` is a constant from the code, ``detail_level`` is
reduced to the two values the tools act on, and the rest are counts. When the
file reaches ``MAX_BYTES`` it is renamed to ``context-history.jsonl.1``
(replacing the older one), so the two files together never pass twice that
by more than one line. Writers take ``context-history.lock`` around the size
check, the roll-over and the append, so two writers that both see a full log
cannot roll it over twice and lose the older file.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .constants import read_only_requested

logger = logging.getLogger(__name__)

HISTORY_ENV = "CRG_CONTEXT_HISTORY"
HISTORY_FILE = "context-history.jsonl"
LOCK_FILE = "context-history.lock"
SCHEMA_VERSION = 1
#: Size at which the log rolls over to ``.1``. Two files at most, so the log
#: never takes much more than twice this on disk (about 10,000 calls in all).
MAX_BYTES = 1024 * 1024

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_TOKEN_KEYS = ("baseline_tokens", "returned_tokens", "saved_tokens")

#: Set by the CLI around a tool call, so a CLI mirror of an MCP tool (for
#: example ``impact``) is logged under the command the person typed.
_label: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "crg_context_history_label", default=None,
)


@dataclass(frozen=True)
class CallInfo:
    """Where to log one call, and the coarse facts about it.

    Only these fields can reach the log, so nothing else about a call can.
    """

    data_dir: Path
    tool: str
    detail_level: str | None = None
    files: int | None = None
    depth: int | None = None


def history_enabled() -> bool:
    """True when the person has turned the log on."""
    value = os.environ.get("CRG_CONTEXT_HISTORY", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


@contextlib.contextmanager
def recorded_as(label: str) -> Iterator[None]:
    """Log calls made inside this block under *label* instead of the tool name."""
    token = _label.set(label)
    try:
        yield
    finally:
        _label.reset(token)


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def record_call(call: CallInfo, *, baseline: int, returned: int, saved: int) -> None:
    """Append one line for *call* when the log is on. Never raises."""
    if not history_enabled() or read_only_requested():
        return
    entry: dict[str, Any] = {
        "v": SCHEMA_VERSION,
        "ts": datetime.now(timezone.utc).strftime(_TS_FORMAT),
        "tool": _label.get() or call.tool,
        "baseline_tokens": int(baseline),
        "returned_tokens": int(returned),
        "saved_tokens": int(saved),
    }
    if call.detail_level is not None:
        entry["detail_level"] = "minimal" if call.detail_level == "minimal" else "standard"
    for key in ("files", "depth"):
        count = _count(getattr(call, key))
        if count is not None:
            entry[key] = count
    line = (json.dumps(entry, separators=(",", ":")) + "\n").encode("utf-8")

    path = Path(call.data_dir) / HISTORY_FILE
    try:
        # The graph's data directory is never created just to hold a log.
        if not path.parent.is_dir():
            return
        with _locked(path.with_name(LOCK_FILE)):
            _roll_over_if_full(path)
            # One write on an O_APPEND descriptor, so lines from the MCP
            # server, the CLI and the daemon do not interleave.
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
            fd = os.open(path, flags, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
    except OSError as exc:
        logger.warning("Could not write context history to %s: %s", path, exc)


@contextlib.contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive ``flock`` on *lock_path* for the block.

    Each call opens its own descriptor, so threads in one process wait for
    each other as well as other processes. Windows has no ``flock``; there
    the block runs unguarded, as the graph write lock does.
    """
    if sys.platform == "win32":  # pragma: no cover - POSIX in CI
        yield
        return
    import fcntl

    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        # Closing the descriptor drops the lock.
        os.close(fd)


def _roll_over_if_full(path: Path) -> None:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    if size >= MAX_BYTES:
        os.replace(path, path.with_name(path.name + ".1"))


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def history_files(data_dir: Path) -> list[Path]:
    """The log files, oldest first."""
    return [data_dir / (HISTORY_FILE + ".1"), data_dir / HISTORY_FILE]


def _parse(line: str) -> dict[str, Any] | None:
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    if not isinstance(entry, dict) or entry.get("v") != SCHEMA_VERSION:
        return None
    ts, tool = entry.get("ts"), entry.get("tool")
    if not isinstance(ts, str) or not isinstance(tool, str):
        return None
    try:
        datetime.strptime(ts, _TS_FORMAT)
    except ValueError:
        return None
    for key in _TOKEN_KEYS:
        value = _count(entry.get(key))
        if value is None or value < 0:
            return None
    return entry


def read_history(
    data_dir: Path, *, since: datetime | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Return ``(entries, skipped_lines)``, oldest first.

    Lines that are not a valid version-1 entry are counted and skipped, so a
    half-written line or a hand edit never stops the report.
    """
    cutoff = since.astimezone(timezone.utc).strftime(_TS_FORMAT) if since else None
    entries: list[dict[str, Any]] = []
    skipped = 0
    for path in history_files(Path(data_dir)):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    entry = _parse(line)
                    if entry is None:
                        skipped += 1
                    elif cutoff is None or entry["ts"] >= cutoff:
                        entries.append(entry)
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("Could not read context history from %s: %s", path, exc)
    return entries, skipped


def _empty(key: str) -> dict[str, Any]:
    return {
        "key": key, "calls": 0, "baseline_tokens": 0,
        "returned_tokens": 0, "saved_tokens": 0, "largest_returned": 0,
    }


def _add(row: dict[str, Any], entry: dict[str, Any]) -> None:
    row["calls"] += 1
    row["baseline_tokens"] += entry["baseline_tokens"]
    row["returned_tokens"] += entry["returned_tokens"]
    # Net, so the columns add up and a call whose reply was bigger than the
    # files it stands for pulls the row down instead of counting as zero.
    row["saved_tokens"] = row["baseline_tokens"] - row["returned_tokens"]
    row["largest_returned"] = max(row["largest_returned"], entry["returned_tokens"])


def summarise(entries: Iterable[dict[str, Any]], *, by: str) -> list[dict[str, Any]]:
    """Group entries per UTC day (date order) or per tool (busiest first).

    ``saved_tokens`` in a row is net: full context minus graph context, which
    is negative when the graph replies were bigger than the files.
    """
    rows: dict[str, dict[str, Any]] = {}
    for entry in entries:
        key = entry["ts"][:10] if by == "day" else entry["tool"]
        _add(rows.setdefault(key, _empty(key)), entry)
    if by == "day":
        return [rows[k] for k in sorted(rows)]
    return sorted(rows.values(), key=lambda r: (-r["calls"], r["key"]))


def total(entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """One row over every entry."""
    row = _empty("Total")
    for entry in entries:
        _add(row, entry)
    return row


def _percent(row: dict[str, Any]) -> int:
    baseline = row["baseline_tokens"]
    return round(row["saved_tokens"] * 100 / baseline) if baseline else 0


def format_table(rows: list[dict[str, Any]], totals: dict[str, Any], *, by: str) -> str:
    """Render the trend as a plain text table."""
    head = "Day (UTC)" if by == "day" else "Tool"
    width = max([len(head), len("Total"), *(len(r["key"]) for r in rows)])
    header = (
        f"{head:<{width}}  {'Calls':>6}  {'Full context':>13}  "
        f"{'Graph context':>13}  {'Saved':>17}  {'Largest reply':>13}"
    )
    out = [header]
    for row in [*rows, totals]:
        saved = f"{row['saved_tokens']:,} ({_percent(row)}%)"
        out.append(
            f"{row['key']:<{width}}  {row['calls']:>6,}  {row['baseline_tokens']:>13,}  "
            f"{row['returned_tokens']:>13,}  {saved:>17}  {row['largest_returned']:>13,}"
        )
    if any(row["saved_tokens"] < 0 for row in rows):
        out.append("")
        out.append(
            "A negative Saved means the graph replies were bigger than the files they stand for."
        )
    return "\n".join(out)
