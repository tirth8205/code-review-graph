"""Optional ZLS enrichment for Zig CALLS resolution.

Tree-sitter sees syntax, not types.  ZLS knows aliases, container methods,
package imports and concrete comptime instantiations, so this module asks the
running language server for the definition at each unresolved Zig call site
and rewrites the edge when the definition lands on an indexed project node.

The pass is deliberately opt-in: ZLS startup and per-call requests are much
slower than parsing, so callers run it after a build (or on a schedule), not
on every incremental update.

What the pass guarantees, and what the tests hold it to:

* every call site recorded on a row is asked, and the row is rewritten only
  when all of them agree — a row merged from two sites answered by two
  definitions stays unresolved instead of guessing one;
* nothing is written until the whole pass is known, in one transaction, so an
  interrupted pass leaves the graph exactly as it found it (issue #721);
* the ``TESTED_BY`` mirror the parser minted from a rewritten call moves with
  it, otherwise a resolved function reports that no test covers it;
* an out-of-repository definition is stored as a portable module label, never
  as this machine's absolute path;
* the LSP transport uses blocking reads on a reader thread, because
  ``select()`` does not work on Windows pipes; and
* a pass that resolved nothing says so, because "nothing answered" is almost
  always a repository that does not build.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from .graph import CallResolution
from .parser import normalize_file_path

logger = logging.getLogger(__name__)

_ZLS_DEFINITION_TIMEOUT = 8.0
_ZLS_START_TIMEOUT = 20.0
_ZLS_MAX_DEFINITION_DEPTH = 2
_ZLS_READ_SIZE = 65536
_ZLS_RESOLUTION_BASIS = "zls"
#: Rows per progress log line, so a long pass is not silent on a busy terminal.
_ZLS_LOG_EVERY = 200


@dataclass(frozen=True)
class DefinitionResult:
    """The outcome of one ``textDocument/definition`` request.

    Three states, all distinguishable by callers:

    * ``ok=True``  — ZLS returned a location in ``location``.
    * ``ok=False, reason="no_definition"`` — ZLS answered the request and
      said there is no definition here.  This is a legitimate answer, not a
      failure, and it points at a workspace that does not build.
    * ``ok=False, reason="error"`` — the request never came back (timeout,
      EOF, RPC error).  The server is running but not answering.
    """

    ok: bool
    location: Optional[tuple[str, int]] = None
    reason: str = ""


#: Sentinel for a request that ZLS answered with "no definition".
_NO_DEFINITION = DefinitionResult(ok=False, reason="no_definition")
#: Sentinel for a request that never came back (timeout, EOF, RPC error).
_REQUEST_ERROR = DefinitionResult(ok=False, reason="error")


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
        self._uri_root = repo_root.as_uri()
        #: Messages arrive on a reader thread. Reading here instead of
        #: ``select()``-polling the pipe is what makes the pass work on
        #: Windows, where ``select()`` only supports sockets.
        self._incoming: queue.Queue[Optional[dict]] = queue.Queue()
        self._reader: Optional[threading.Thread] = None

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
        if self._proc.stdout is None:  # pragma: no cover - PIPE above
            self.stop()
            return False
        self._reader = threading.Thread(
            target=_frame_reader,
            args=(self._proc.stdout, self._incoming),
            name="zls-reader",
            daemon=True,
        )
        self._reader.start()
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
    ) -> DefinitionResult:
        """Return the definition location for a call-site position.

        Distinguishes three outcomes so callers can report the right fix:
        a location when ZLS answered, ``no_definition`` when ZLS said the
        symbol has no definition, and ``error`` when the request never came
        back (timeout, EOF, RPC error).
        """
        result = self._request("textDocument/definition", {
            "textDocument": {"uri": Path(file_path).as_uri()},
            "position": {"line": line, "character": character},
        })
        if result is None:
            # The request timed out, hit EOF, or returned an RPC error.
            # That is a transport failure, not a "no definition" answer.
            return _REQUEST_ERROR
        location = _definition_location(result)
        if location is None:
            # ZLS answered the request, but the response had no usable
            # location — the symbol genuinely has no definition here.
            return _NO_DEFINITION
        return DefinitionResult(ok=True, location=location)

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
            try:
                message = self._incoming.get(timeout=remaining)
            except queue.Empty:
                logger.debug("ZLS request %s timed out", method)
                return None
            if message is None:
                # The reader hit end of stream: the server is gone.
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


def _extract_frame(buffer: bytearray) -> Optional[bytes]:
    """Cut one ``Content-Length`` framed body off *buffer*, in place.

    Returns the body and consumes it, or ``None`` when the frame is not
    complete yet — in which case *buffer* is left untouched so the next read
    can finish it. A header without a usable ``Content-Length`` is discarded
    with its body unknown, which cannot be recovered, so the caller sees the
    stream as broken instead of resynchronising on garbage.
    """
    header_end = buffer.find(b"\r\n\r\n")
    separator = 4
    if header_end < 0:
        header_end = buffer.find(b"\n\n")
        separator = 2
    if header_end < 0:
        return None
    length: Optional[int] = None
    for line in bytes(buffer[:header_end]).splitlines():
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            try:
                length = int(value.strip())
            except ValueError:
                return None
    if length is None:
        return None
    body_start = header_end + separator
    if len(buffer) - body_start < length:
        return None
    body = bytes(buffer[body_start:body_start + length])
    del buffer[:body_start + length]
    return body


def _frame_reader(stream, out: "queue.Queue[Optional[dict]]") -> None:
    """Read LSP frames from *stream* into *out* until the pipe closes.

    Runs on its own thread so the blocking read costs a thread and not the
    request deadline: ``_request`` waits on the queue with its own timeout.
    ``None`` is the end-of-stream sentinel, which is how a crashed server
    turns into "no answer" instead of an infinite wait.

    Uses ``read1()`` instead of ``read()`` so a single ``read`` syscall
    returns immediately with whatever data is available, instead of blocking
    until the buffer is full.  This matters on Windows, where ``subprocess``
    pipes are wrapped in a ``BufferedReader`` whose ``read(n)`` will wait
    for ``n`` bytes even when the server has already sent a complete frame.
    """
    buffer = bytearray()
    try:
        while True:
            frame = _extract_frame(buffer)
            if frame is None:
                try:
                    # ``read1`` returns at most one underlying raw read,
                    # so a short frame does not stall waiting for the
                    # buffer to fill.  Fall back to ``read`` for objects
                    # that do not provide ``read1`` (e.g. ``io.BytesIO``
                    # in tests, which already returns all available data).
                    if hasattr(stream, "read1"):
                        chunk = stream.read1(_ZLS_READ_SIZE)
                    else:
                        chunk = stream.read(_ZLS_READ_SIZE)
                except OSError:
                    break
                if not chunk:
                    break
                buffer.extend(chunk)
                continue
            try:
                out.put(json.loads(frame.decode("utf-8", errors="replace")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                logger.debug("Discarding malformed ZLS frame")
    except Exception:  # noqa: BLE001 - the reader must never crash silently
        logger.debug("ZLS frame reader terminated unexpectedly", exc_info=True)
    finally:
        out.put(None)


def uri_to_file_path(uri: str, *, windows: Optional[bool] = None) -> Optional[str]:
    """Decode a ``file:`` URI into the path the graph indexes.

    Two spellings are not the round trip of ``urlparse``: a Windows drive URI
    keeps an empty authority, so ``file:///C:/x`` must lose its leading slash,
    while ``file://host/share/x`` is UNC and has to keep the authority or the
    path points at a directory that does not exist.
    """
    parsed = urlparse(uri)
    if parsed.scheme and parsed.scheme != "file":
        return None
    path = unquote(parsed.path or "")
    if windows is None:
        windows = os.name == "nt"
    if not windows:
        return path
    if parsed.netloc:
        return normalize_file_path(f"//{parsed.netloc}{path}")
    if re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return normalize_file_path(path)


def _definition_location(result: Any) -> Optional[tuple[str, int]]:
    """First ``(path, zero_based_line)`` in a definition response."""
    if not result:
        return None
    item = result[0] if isinstance(result, list) else result
    if not isinstance(item, dict):
        return None
    uri = item.get("uri") or item.get("targetUri")
    if not isinstance(uri, str):
        return None
    path = uri_to_file_path(uri)
    if not path:
        return None
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
    """Map ``(1-based line, callee name)`` to UTF-16 call positions.

    A line can hold several sites for the same name (``x.doIt(); y.doIt();``).
    They are all returned, because they are all recorded on one edge row and
    the row may only be rewritten to an answer they share.
    """
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


def _resolve_definition(
    client,
    index: dict[str, list[dict[str, Any]]],
    location: tuple[str, int],
    callee: str,
) -> tuple[Optional[dict[str, Any]], str]:
    """Follow a definition response to an indexed node.

    Returns ``(node, path)``: the node when the definition names something the
    graph holds, and the final path either way, because a path inside the
    repository that matched nothing is a different outcome from a call that
    left it.
    """
    target_path, definition_line = location
    for depth in range(_ZLS_MAX_DEFINITION_DEPTH):
        node = _match_node(index, target_path, definition_line, callee)
        if node is not None:
            return node, target_path
        if depth == _ZLS_MAX_DEFINITION_DEPTH - 1:
            break
        # A definition may land on a re-export/alias; ask ZLS to follow it
        # once more before giving up.
        follow = client.definition(
            target_path, definition_line,
            _definition_probe_column(target_path, definition_line),
        )
        if not follow.ok:
            break
        target_path, definition_line = follow.location  # type: ignore[misc]
    return None, target_path


# ---------------------------------------------------------------------------
# Edge metadata
# ---------------------------------------------------------------------------


def _resolved_extra(
    node: dict[str, Any], receiver: Optional[str], original: dict | None = None,
) -> dict:
    """Metadata for a call whose definition ZLS confirmed.

    The definition also names the receiver's type, which is the binding the
    parser could not derive: ``x.doIt()`` resolving to ``a.zig::A.doIt`` says
    ``x`` is an ``A``. Recording it in the vocabulary the parser uses keeps
    the read path's receiver rule agreeing with the target, instead of
    rejecting an edge it can no longer explain.

    Metadata unrelated to target resolution (anything the parser recorded that
    is not in ``_SUPERSEDED_TARGET_METADATA``) is preserved from *original*, so
    enrichment does not silently drop context the graph already held.
    """
    from .graph import _SUPERSEDED_TARGET_METADATA

    extra: dict[str, Any] = {}
    if original:
        for key, value in original.items():
            if key not in _SUPERSEDED_TARGET_METADATA:
                extra[key] = value
    extra["resolution_basis"] = _ZLS_RESOLUTION_BASIS
    if receiver:
        extra["receiver"] = receiver
    parent = node.get("parent_name")
    if isinstance(parent, str) and parent:
        extra["receiver_binding"] = "class"
        extra["receiver_class"] = parent
    else:
        extra["receiver_binding"] = "module"
        extra["receiver_module"] = node["file_path"]
    return extra


def _external_extra(
    receiver: Optional[str], module_label: str, original: dict | None = None,
) -> dict:
    """Metadata for a call whose definition lives outside the repository.

    Preserves unrelated metadata from *original* the same way
    :func:`_resolved_extra` does.
    """
    from .graph import _SUPERSEDED_TARGET_METADATA

    extra: dict[str, Any] = {}
    if original:
        for key, value in original.items():
            if key not in _SUPERSEDED_TARGET_METADATA:
                extra[key] = value
    extra["external"] = True
    extra["external_module"] = module_label
    extra["receiver_binding"] = "external"
    extra["resolution_basis"] = _ZLS_RESOLUTION_BASIS
    if receiver:
        extra["receiver"] = receiver
    return extra


def _is_absolute_path(path: str) -> bool:
    """True for POSIX (``/x``), Windows drive-letter (``C:/x``, ``C:\\x``) and UNC paths."""
    if path.startswith("/"):
        return True
    # Windows drive-letter path: ``C:/...`` or ``C:\...``
    if len(path) >= 3 and path[1] in (":",) and path[0].isalpha() and path[2] in ("/", "\\"):
        return True
    return False


def _portable_module_label(file_path: str) -> str:
    """Label for an out-of-repository definition.

    The absolute path is where the definition happened to live on this
    machine: storing it makes graphs incomparable between machines and writes
    a user directory into a shared artifact. The tail that names the module
    (``std/mem/Allocator.zig``) says what the call reaches without either.
    """
    path = normalize_file_path(file_path)
    if not _is_absolute_path(path):
        return path
    # Split on both ``/`` and ``\`` so Windows paths are handled the same way.
    parts = [part for part in re.split(r"[\\/]", path) if part]
    # A Windows drive letter (``C:``) is machine-specific and carries no
    # module information, so drop it before taking the tail.
    if parts and len(parts[0]) == 2 and parts[0][1] == ":":
        parts = parts[1:]
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[-1] if parts else path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _unresolved_zig_call_rows(store) -> list:
    """Zig ``CALLS`` rows whose target is still a bare name.

    That is what ``target_resolution = 'unresolved'`` records for them, but the
    column is only filled by post-processing, and ``--zls`` can be asked for on
    a store where it has not run yet. Reading the target itself keeps the pass
    working on either store instead of reporting "nothing to do".

    Rows already known to leave the repository are dropped: that answer is
    recorded, and re-asking ZLS for them would repeat the expensive part of
    every run for nothing. The decision reads the parsed metadata rather than
    matching the JSON blob with ``LIKE``, because a receiver variable named
    ``external`` would otherwise hide a call that still needs an answer.
    """
    rows = store._conn.execute(
        "SELECT id, source_qualified, target_qualified, file_path, line, extra "
        "FROM edges WHERE kind = 'CALLS' AND file_path LIKE '%.zig' "
        "AND target_qualified NOT LIKE '%::%'",
    ).fetchall()
    return [
        row for row in rows
        if not _load_extra(row["extra"]).get("external")
    ]


def count_unresolved_zig_calls(store) -> int:
    """How many Zig call targets are still a bare name.

    ``update`` re-parses the files it touched and rewrites their edges from
    source, which drops the targets a ZLS pass had settled. The graph keeps no
    other trace of that work, so this is how the caller notices it went away.
    """
    return len(_unresolved_zig_call_rows(store))


def enrich_zls_calls(
    store,
    repo_root: Path,
    *,
    limit: int = 0,
    request_timeout: float = _ZLS_DEFINITION_TIMEOUT,
    max_seconds: float = 1800.0,
    client=None,
) -> dict[str, Any]:
    """Resolve unresolved Zig CALLS through ZLS definitions.

    ``client`` lets a caller (or a test) supply a running language server; the
    pass then does not stop it, because it did not start it.
    """
    repo_root = Path(repo_root).resolve()
    started = time.monotonic()

    rows = _unresolved_zig_call_rows(store)
    if not rows:
        return {"skipped": True, "reason": "no unresolved Zig calls"}

    owns_client = client is None
    if owns_client:
        client = ZlsClient(repo_root, request_timeout=request_timeout)
        if not client.start():
            return {
                "skipped": True,
                "reason": (
                    "zls did not start (not on PATH, or the workspace has no "
                    "build.zig for it to configure from)"
                ),
            }
    index = _node_index(store)
    stats: dict[str, Any] = {
        "candidates": len(rows),
        "call_sites": 0,
        "resolved": 0,
        "external": 0,
        "unmatched": 0,
        "no_definition": 0,
        "site_conflicts": 0,
        "errors": 0,
        "touched_files": set(),
    }
    source_cache: dict[str, bytes] = {}
    sites_cache: dict[str, dict[tuple[int, str], list[tuple[int, int]]]] = {}
    # Rows that share a file, line and callee are one call site recorded for
    # several callers; they get one answer and are rewritten together.
    groups: dict[tuple[str, int, str], list[Any]] = {}
    for row in rows:
        key = (row["file_path"], row["line"], row["target_qualified"])
        groups.setdefault(key, []).append(row)
    keys = list(groups)[:limit] if limit else list(groups)
    stats["call_sites"] = len(keys)

    resolutions: list[CallResolution] = []
    try:
        for position, key in enumerate(keys):
            if time.monotonic() - started > max_seconds:
                stats["stopped_early"] = True
                # Account for the sites that were never asked: without this
                # the total would not add up to ``candidates``, and the
                # caller could not tell how much work was left.
                remaining = sum(len(groups[k]) for k in keys[position:])
                stats.setdefault("skipped", 0)
                stats["skipped"] += remaining
                break
            file_path, line, callee = key
            group = groups[key]
            source = source_cache.get(file_path)
            if source is None:
                try:
                    source = Path(file_path).read_bytes()
                except OSError:
                    stats["errors"] += len(group)
                    continue
                source_cache[file_path] = source
            sites = sites_cache.get(file_path)
            if sites is None:
                sites = _collect_call_sites(source)
                sites_cache[file_path] = sites
            positions = sites.get((line, callee))
            if not positions:
                stats["unmatched"] += len(group)
                continue
            try:
                text = source.decode("utf-8", "replace")
                client.open_document(file_path, text)
                answers = [
                    client.definition(file_path, line0, column)
                    for line0, column in positions
                ]
            except Exception as exc:  # noqa: BLE001 - ZLS transport is best-effort
                logger.debug("ZLS request failed for %s:%d: %s", file_path, line, exc)
                stats["errors"] += len(group)
                continue
            # Every site must have been answered, and they must all agree.
            # A site that returned ``error`` (timeout/EOF/RPC failure) is not
            # confirmed: accepting the others would mark an unconfirmed call as
            # resolved.  A site that returned ``no_definition`` is a legitimate
            # answer of "nothing here", which is different from an error.
            if any(a.reason == "error" for a in answers):
                stats["errors"] += len(group)
                continue
            if any(a.reason == "no_definition" for a in answers):
                stats["no_definition"] += len(group)
                continue
            distinct = {a.location for a in answers if a.ok}
            if len(distinct) > 1:
                # Two sites merged onto one row disagree, so the row has no
                # single honest target. Leave it unresolved and say why.
                stats["site_conflicts"] += len(group)
                continue
            if not distinct:
                # All sites returned ok with no location — defensive, since
                # the no_definition and error checks above should have caught
                # those states already.
                stats["no_definition"] += len(group)
                continue
            node, target_path = _resolve_definition(
                client, index, next(iter(distinct)), callee,
            )
            if node is not None:
                for row in group:
                    original = _load_extra(row["extra"])
                    resolutions.append(CallResolution(
                        edge_id=row["id"],
                        target_qualified=node["qualified_name"],
                        extra=_resolved_extra(
                            node, original.get("receiver"), original,
                        ),
                    ))
                stats["resolved"] += len(group)
                stats["touched_files"].add(file_path)
                stats["touched_files"].add(node["file_path"])
            elif _path_is_external(repo_root, target_path):
                label = _portable_module_label(target_path)
                for row in group:
                    original = _load_extra(row["extra"])
                    resolutions.append(CallResolution(
                        edge_id=row["id"],
                        target_qualified=None,
                        extra=_external_extra(
                            original.get("receiver"), label, original,
                        ),
                    ))
                stats["external"] += len(group)
                stats["touched_files"].add(file_path)
            else:
                stats["unmatched"] += len(group)
            if (position + 1) % _ZLS_LOG_EVERY == 0:
                logger.info(
                    "ZLS enrichment: %d/%d sites, resolved=%d",
                    position + 1, len(keys), stats["resolved"],
                )
    finally:
        if owns_client:
            client.stop()

    applied = store.apply_call_resolutions(resolutions)
    stats["mirrors"] = applied["mirror_edges"]
    stats["ambiguous_mirrors"] = applied["ambiguous_mirrors"]
    stats["external_mirror_edges"] = applied.get("external_mirror_edges", 0)
    if stats["resolved"] or stats["external"]:
        # A marker, not a memo: the next update rewrites these edges from
        # source and the only way it can warn about that is by knowing the
        # graph was enriched once before.  Both resolved and external-only
        # passes are lost on re-parse, so both must leave the marker.
        store.set_metadata("zls_enriched_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
    if not stats["resolved"] and not stats["external"] and stats["candidates"]:
        if stats["errors"] > stats["no_definition"]:
            stats["warning"] = (
                f"ZLS requests failed for {stats['errors']} of "
                f"{stats['candidates']} unresolved Zig call sites. The server "
                "is running but not answering: check that zls and zig agree "
                "on the version, and raise --timeout if the workspace is "
                "large enough to answer slowly."
            )
        else:
            stats["warning"] = (
                f"ZLS answered no definition for any of the "
                f"{stats['candidates']} unresolved Zig call sites. That is "
                "usually a workspace that does not build: run 'zig build' "
                "and check that zls and zig agree on the version."
            )
    stats["elapsed_seconds"] = round(time.monotonic() - started, 2)
    logger.info(
        "ZLS enrichment: resolved=%d external=%d unmatched=%d conflicts=%d "
        "no-definition=%d errors=%d",
        stats["resolved"], stats["external"], stats["unmatched"],
        stats["site_conflicts"], stats["no_definition"], stats["errors"],
    )
    return stats


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
