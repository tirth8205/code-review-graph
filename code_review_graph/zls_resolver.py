"""Optional ZLS enrichment for Zig CALLS resolution.

Tree-sitter sees syntax, not types.  ZLS knows aliases, container methods,
package imports and concrete comptime instantiations, so this module asks the
running language server for the definition at each unresolved Zig call site
and rewrites the edge when the definition lands on an indexed project node.

The pass is deliberately opt-in: ZLS startup and per-call requests are much
slower than parsing, so callers run it after a build (or on a schedule), not
on every incremental update.
"""

from __future__ import annotations

import json
import logging
import os
import re
import select
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from .parser import normalize_file_path

logger = logging.getLogger(__name__)

_ZLS_DEFINITION_TIMEOUT = 8.0
_ZLS_START_TIMEOUT = 20.0
_ZLS_MAX_DEFINITION_DEPTH = 2


# ---------------------------------------------------------------------------
# LSP transport
# ---------------------------------------------------------------------------


class ZlsClient:
    """Minimal LSP client for the two methods this resolver needs."""

    def __init__(
        self,
        repo_root: Path,
        request_timeout: float = _ZLS_DEFINITION_TIMEOUT,
    ) -> None:
        self.repo_root = repo_root
        self.request_timeout = request_timeout
        self._proc: Optional[subprocess.Popen] = None
        self._next_id = 1
        self._opened: set[str] = set()
        self._buffer = bytearray()
        self._uri_root = repo_root.as_uri()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        executable = shutil.which("zls")
        if not executable:
            logger.info("ZLS not found on PATH, skipping Zig enrichment")
            return False
        try:
            self._proc = subprocess.Popen(  # noqa: S603 - resolved executable
                [executable],
                cwd=str(self.repo_root),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as exc:
            logger.warning("Could not start ZLS: %s", exc)
            return False
        params = {
            "processId": None,
            "rootUri": self._uri_root,
            "workspaceFolders": [
                {"uri": self._uri_root, "name": self.repo_root.name},
            ],
            "capabilities": {
                "textDocument": {
                    "definition": {"linkSupport": False},
                    "references": {},
                },
                "workspace": {"workspaceFolders": True},
            },
        }
        result = self._request("initialize", params, timeout=_ZLS_START_TIMEOUT)
        if result is None:
            self.stop()
            return False
        self._notify("initialized", {})
        return True

    def stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            self._request("shutdown", None, timeout=2.0)
            self._notify("exit", None)
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        self._proc = None

    # -- documents ---------------------------------------------------------

    def open_document(self, file_path: str, source: str) -> None:
        uri = Path(file_path).as_uri()
        if uri in self._opened:
            return
        self._notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": "zig",
                "version": 1,
                "text": source,
            },
        })
        self._opened.add(uri)

    def definition(
        self, file_path: str, line: int, character: int,
    ) -> Optional[tuple[str, int]]:
        """Return ``(path, zero_based_line)`` for a call-site position."""
        result = self._request("textDocument/definition", {
            "textDocument": {"uri": Path(file_path).as_uri()},
            "position": {"line": line, "character": character},
        })
        return _definition_location(result)

    # -- JSON-RPC ----------------------------------------------------------

    def _request(
        self, method: str, params: Any, timeout: Optional[float] = None,
    ) -> Any:
        if self._proc is None or self._proc.stdin is None:
            return None
        request_id = self._next_id
        self._next_id += 1
        self._write({"jsonrpc": "2.0", "id": request_id,
                     "method": method, "params": params})
        deadline = time.monotonic() + (
            timeout if timeout is not None else self.request_timeout
        )
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.debug("ZLS request %s timed out", method)
                return None
            message = self._read_message(remaining)
            if message is None:
                return None
            if message.get("id") == request_id:
                if "error" in message:
                    logger.debug("ZLS %s error: %s", method, message["error"])
                    return None
                return message.get("result")
            # A server-to-client request (e.g. workspace/configuration) must
            # be answered or ZLS stalls waiting for it.
            if "id" in message and "method" in message:
                self._write({
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": None,
                })
            # Anything else is a notification; ignore it.

    def _notify(self, method: str, params: Any) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, payload: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        proc.stdin.write(
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body,
        )
        proc.stdin.flush()

    def _read_message(self, timeout: float) -> Optional[dict]:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return None
        deadline = time.monotonic() + timeout
        header_end = 0
        length: Optional[int] = None
        while True:
            if length is None:
                header_end = self._buffer.find(b"\r\n\r\n")
                if header_end < 0:
                    header_end = self._buffer.find(b"\n\n")
                if header_end < 0:
                    header_end = -1
                else:
                    separator = (
                        4 if self._buffer[header_end:header_end + 4] == b"\r\n\r\n"
                        else 2
                    )
                    header = bytes(self._buffer[:header_end])
                    for line in header.splitlines():
                        name, _, value = line.partition(b":")
                        if name.strip().lower() == b"content-length":
                            try:
                                length = int(value.strip())
                            except ValueError:
                                return None
                    if length is None:
                        return None
                    del self._buffer[:header_end + separator]
            if length is not None and len(self._buffer) >= length:
                body = bytes(self._buffer[:length])
                del self._buffer[:length]
                try:
                    return json.loads(body)
                except json.JSONDecodeError:
                    return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if not _wait_readable(proc.stdout.fileno(), remaining):
                return None
            try:
                chunk = os.read(proc.stdout.fileno(), 65536)
            except OSError:
                return None
            if not chunk:
                return None
            self._buffer.extend(chunk)


def _wait_readable(fd: int, timeout: float) -> bool:
    try:
        ready, _, _ = select.select([fd], [], [], max(0.0, timeout))
    except (OSError, ValueError):
        return False
    return bool(ready)


def _definition_location(result: Any) -> Optional[tuple[str, int]]:
    if not result:
        return None
    item = result[0] if isinstance(result, list) else result
    if not isinstance(item, dict):
        return None
    uri = item.get("uri") or item.get("targetUri")
    if not isinstance(uri, str):
        return None
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    path = unquote(parsed.path)
    selection = item.get("targetSelectionRange") or item.get("range") or {}
    start = selection.get("start") if isinstance(selection, dict) else None
    if not isinstance(start, dict):
        return None
    line = start.get("line")
    if not isinstance(line, int):
        return None
    return normalize_file_path(Path(path)), line


# ---------------------------------------------------------------------------
# Call-site extraction (tree-sitter, one parse per file)
# ---------------------------------------------------------------------------


def _collect_call_sites(source: bytes) -> dict[tuple[int, str], list[tuple[int, int]]]:
    """Map ``(1-based line, callee name)`` to UTF-16 call positions."""
    try:
        from tree_sitter_language_pack import get_parser
    except ImportError:  # pragma: no cover - bundled dependency
        return {}
    parser = get_parser("zig")
    tree = parser.parse(source)
    lines = source.split(b"\n")
    sites: dict[tuple[int, str], list[tuple[int, int]]] = {}
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in ("SuffixExpr", "FieldOrFnCall"):
            children = node.children
            for index, child in enumerate(children):
                if child.type != "FnCallArguments" or index == 0:
                    continue
                previous = children[index - 1]
                if previous.type != "IDENTIFIER":
                    break
                callee = previous.text.decode("utf-8", errors="replace")
                line0 = previous.start_point[0]
                byte_col = previous.start_point[1]
                line_bytes = lines[line0] if line0 < len(lines) else b""
                utf16_col = _utf16_column(line_bytes, byte_col)
                sites.setdefault((line0 + 1, callee), []).append(
                    (line0, utf16_col),
                )
                break
        stack.extend(node.children)
    return sites


def _utf16_column(line_bytes: bytes, byte_col: int) -> int:
    prefix = line_bytes[:byte_col]
    try:
        return len(prefix.decode("utf-8").encode("utf-16-le")) // 2
    except UnicodeDecodeError:
        return byte_col


# ---------------------------------------------------------------------------
# Node index and matching
# ---------------------------------------------------------------------------


def _node_index(store) -> dict[str, list[dict[str, Any]]]:
    rows = store._conn.execute(
        "SELECT qualified_name, name, file_path, line_start, line_end, "
        "kind, parent_name FROM nodes "
        "WHERE language = 'zig' AND kind IN ('Function', 'Test')",
    ).fetchall()
    index: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        index.setdefault(row["file_path"], []).append(dict(row))
    return index


def _match_node(
    index: dict[str, list[dict[str, Any]]],
    file_path: str,
    definition_line: int,
    callee: str,
) -> Optional[dict[str, Any]]:
    nodes = index.get(file_path, [])
    exact = [
        node for node in nodes
        if node["line_start"] == definition_line + 1
        and node["name"] == callee
    ]
    if exact:
        return exact[0]
    containing = [
        node for node in nodes
        if node["line_start"] <= definition_line + 1 <= node["line_end"]
    ]
    if not containing:
        return None
    # A definition range that lands inside a body is ambiguous; only accept
    # the declaration line of the narrowest candidate.
    containing.sort(key=lambda node: node["line_end"] - node["line_start"])
    best = containing[0]
    if best["line_start"] == definition_line + 1 or best["name"] == callee:
        return best
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def enrich_zls_calls(
    store,
    repo_root: Path,
    *,
    limit: int = 0,
    request_timeout: float = _ZLS_DEFINITION_TIMEOUT,
    max_seconds: float = 1800.0,
) -> dict[str, Any]:
    """Resolve unresolved Zig CALLS through ZLS definitions."""
    repo_root = repo_root.resolve()
    started = time.monotonic()

    rows = store._conn.execute(
        "SELECT id, source_qualified, target_qualified, file_path, line, extra "
        "FROM edges WHERE kind = 'CALLS' AND file_path LIKE '%.zig' "
        "AND target_resolution = 'unresolved' "
        "AND extra NOT LIKE '%\"external\"%'",
    ).fetchall()
    if not rows:
        return {"skipped": True, "reason": "no unresolved Zig calls"}

    client = ZlsClient(repo_root, request_timeout=request_timeout)
    if not client.start():
        return {"skipped": True, "reason": "zls unavailable"}
    index = _node_index(store)
    stats: dict[str, Any] = {
        "candidates": len(rows),
        "call_sites": 0,
        "resolved": 0,
        "external": 0,
        "unmatched": 0,
        "errors": 0,
    }
    source_cache: dict[str, bytes] = {}
    sites_cache: dict[str, dict[tuple[int, str], list[tuple[int, int]]]] = {}
    groups: dict[tuple[str, int, str], list[Any]] = {}
    for row in rows:
        key = (row["file_path"], row["line"], row["target_qualified"])
        groups.setdefault(key, []).append(row)
    if limit:
        keys = list(groups)[:limit]
    else:
        keys = list(groups)
    stats["call_sites"] = len(keys)

    try:
        for position, key in enumerate(keys):
            if time.monotonic() - started > max_seconds:
                stats["stopped_early"] = True
                break
            file_path, line, callee = key
            source = source_cache.get(file_path)
            if source is None:
                try:
                    source = Path(file_path).read_bytes()
                except OSError:
                    stats["errors"] += 1
                    continue
                source_cache[file_path] = source
            sites = sites_cache.get(file_path)
            if sites is None:
                sites = _collect_call_sites(source)
                sites_cache[file_path] = sites
            positions = sites.get((line, callee))
            if not positions:
                stats["unmatched"] += 1
                continue
            try:
                client.open_document(
                    file_path, source.decode("utf-8", "replace"),
                )
                location = client.definition(
                    file_path, positions[0][0], positions[0][1],
                )
            except Exception:  # noqa: BLE001 - ZLS transport is best-effort
                stats["errors"] += 1
                continue
            if location is None:
                stats["errors"] += 1
                continue
            target_path, definition_line = location
            target_node = None
            for _ in range(_ZLS_MAX_DEFINITION_DEPTH):
                target_node = _match_node(
                    index, target_path, definition_line, callee,
                )
                if target_node is not None:
                    break
                # A definition may land on a re-export/alias; ask ZLS to
                # follow it once more before giving up.
                extra_location = client.definition(
                    target_path, definition_line,
                    _definition_probe_column(target_path, definition_line),
                )
                if extra_location is None:
                    break
                target_path, definition_line = extra_location
            if target_node is not None:
                _rewrite_group(
                    store, groups[key], target_node["qualified_name"],
                )
                stats["resolved"] += len(groups[key])
                continue
            if _path_is_external(repo_root, target_path):
                _mark_external(store, groups[key], target_path)
                stats["external"] += len(groups[key])
            else:
                stats["unmatched"] += len(groups[key])
            if (position + 1) % 200 == 0:
                logger.info(
                    "ZLS enrichment: %d/%d sites, resolved=%d",
                    position + 1, len(keys), stats["resolved"],
                )
    finally:
        client.stop()

    if stats["resolved"] or stats["external"]:
        try:
            store.refresh_target_resolution()
        except Exception:  # noqa: BLE001 - best-effort classification refresh
            logger.debug("ZLS resolve refresh failed", exc_info=True)
        store.commit()
    stats["elapsed_seconds"] = round(time.monotonic() - started, 2)
    logger.info(
        "ZLS enrichment: resolved=%d external=%d unmatched=%d errors=%d",
        stats["resolved"], stats["external"],
        stats["unmatched"], stats["errors"],
    )
    return stats


def _rewrite_group(store, rows, target_qualified: str) -> None:
    for row in rows:
        extra = _load_extra(row["extra"])
        for key in (
            "ambiguous_targets", "ambiguous_target_count",
            "ambiguous_targets_truncated", "unresolved_targets",
            "unresolved_target_count", "unresolved_targets_truncated",
            "bare_call_target",
        ):
            extra.pop(key, None)
        extra["resolution_basis"] = "zls"
        store._conn.execute(
            "UPDATE edges SET target_qualified = ?, target_resolution = 'direct', "
            "extra = ? WHERE id = ?",
            (target_qualified, json.dumps(extra, sort_keys=True), row["id"]),
        )


def _mark_external(store, rows, target_path: str) -> None:
    for row in rows:
        extra = _load_extra(row["extra"])
        extra["external"] = True
        extra["external_module"] = target_path
        store._conn.execute(
            "UPDATE edges SET extra = ? WHERE id = ?",
            (json.dumps(extra, sort_keys=True), row["id"]),
        )


def _load_extra(raw: Any) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _path_is_external(repo_root: Path, file_path: str) -> bool:
    try:
        Path(file_path).resolve().relative_to(repo_root)
        return False
    except ValueError:
        return True


def _definition_probe_column(file_path: str, line0: int) -> int:
    """Column to probe when a definition lands on an alias/re-export.

    ``pub const getOrInit = bootstrap.getOrInit;`` must be followed at the
    right-hand symbol, not at ``const``.  Falling back to the line indent is
    still better than column zero for ordinary declarations.
    """
    try:
        text = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    lines = text.splitlines()
    if line0 >= len(lines):
        return 0
    line = lines[line0]
    if "=" not in line:
        return len(line) - len(line.lstrip())
    rhs_start = line.index("=") + 1
    rhs = line[rhs_start:]
    stripped = rhs.lstrip()
    offset = rhs_start + (len(rhs) - len(stripped))
    if stripped.startswith("@import"):
        return offset
    # Last identifier in ``bootstrap.getOrInit`` / ``Foo.Bar``.
    last = list(re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", stripped))
    if not last:
        return offset
    return offset + last[-1].start()
