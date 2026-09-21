"""Route one stdio MCP session to one server process per Git worktree.

The normal MCP server already resolves an explicit ``repo_root`` per tool
call. This module adds process isolation for linked worktrees: each root gets
its own child server and therefore its own SQLite connection state and graph
database path. Only the primary checkout and worktrees reported by Git for
the same ``git common dir`` are accepted.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

from .incremental import find_project_root, get_db_path

logger = logging.getLogger(__name__)

_MAX_FRAME_BYTES = 4 * 1024 * 1024
_MAX_CHILD_FRAME_BYTES = 64 * 1024 * 1024
_MAX_CHILDREN = 32
_MAX_INFLIGHT_REQUESTS = 256
_CHILD_START_TIMEOUT = 60.0
_CHILD_STOP_TIMEOUT = 10.0
_GIT_TIMEOUT = 10.0
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_CHILD_ENV_KEYS = frozenset(
    {
        "HOME",
        "LANG",
        "NO_COLOR",
        "PATH",
        "TMPDIR",
        "USER",
        "LOGNAME",
        "VIRTUAL_ENV",
        "SYSTEMROOT",
        "WINDIR",
        "CRG_HOME",
        "CRG_BFS_ENGINE",
        "CRG_CHURN_WINDOW_DAYS",
        "CRG_DISCOVERY_TIMEOUT",
        "CRG_GIT_TIMEOUT",
        "CRG_MSG",
        "CRG_NESTED_OUTPUT_SCAN",
        "CRG_PARSE_EXECUTOR",
        "CRG_PARSE_WORKERS",
        "CRG_RECURSE_SUBMODULES",
        "CRG_SERIAL_PARSE",
        "CRG_TOOLS",
        "PYTHONIOENCODING",
        "PYTHONUNBUFFERED",
    }
)


class MultiWorktreeError(RuntimeError):
    """Raised when a worktree cannot be safely routed."""


def _request_key(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _git_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _run_git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_git_env(),
            stdin=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise MultiWorktreeError(f"Git command failed: {' '.join(args)}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise MultiWorktreeError(
            f"Git command failed: {' '.join(args)}" + (f" ({detail})" if detail else "")
        )
    return result.stdout.strip()


def _canonical_git_identity(path: str | Path) -> tuple[Path, Path]:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise MultiWorktreeError("repo_root must be an absolute path")
    candidate = candidate.resolve()
    if not candidate.is_dir():
        raise MultiWorktreeError(f"repo_root is not a directory: {candidate}")

    root = Path(_run_git(candidate, "rev-parse", "--show-toplevel")).resolve()
    common_raw = Path(_run_git(root, "rev-parse", "--git-common-dir"))
    common = (root / common_raw if not common_raw.is_absolute() else common_raw).resolve()
    return root, common


def _listed_worktree_roots(primary_root: Path) -> set[Path]:
    roots: set[Path] = set()
    output = _run_git(primary_root, "worktree", "list", "--porcelain")
    for line in output.splitlines():
        if not line.startswith("worktree "):
            continue
        candidate = Path(line.removeprefix("worktree ").strip()).expanduser()
        try:
            candidate = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if candidate.is_dir():
            roots.add(candidate)
    return roots


class WorktreeAuthorizer:
    """Authorize the primary checkout and its currently linked worktrees."""

    def __init__(self, repo_root: str | None) -> None:
        raw_root = repo_root or str(find_project_root())
        self.primary_root, self.primary_common = _canonical_git_identity(raw_root)

    def authorize(self, raw_root: object) -> Path:
        if not isinstance(raw_root, str) or not raw_root:
            raise MultiWorktreeError("repo_root must be a non-empty absolute path")
        root, common = _canonical_git_identity(raw_root)
        if root == self.primary_root:
            return root
        if common != self.primary_common:
            raise MultiWorktreeError(
                "repo_root must be the primary repository or a linked worktree"
            )
        if root not in _listed_worktree_roots(self.primary_root):
            raise MultiWorktreeError(
                "repo_root is not an active linked worktree of the primary repository"
            )
        return root


def _json_error(request_id: object, code: int, message: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _iter_bounded_lines(stream: Any, max_bytes: int):
    """Yield newline-delimited frames without buffering an unbounded line."""
    buffer = bytearray()
    oversized = False
    read = getattr(stream, "read1", stream.read)
    while True:
        chunk = read(8192)
        if not chunk:
            if oversized:
                yield None
            elif buffer:
                yield bytes(buffer)
            return
        buffer.extend(chunk)
        while b"\n" in buffer:
            end = buffer.index(b"\n") + 1
            line = bytes(buffer[:end])
            del buffer[:end]
            if oversized or len(line) > max_bytes:
                yield None
            else:
                yield line
            oversized = False
        if len(buffer) > max_bytes:
            buffer.clear()
            oversized = True


class _Child:
    def __init__(self, router: "MultiWorktreeRouter", root: Path) -> None:
        self.router = router
        self.root = root
        self.process: subprocess.Popen[bytes] | None = None
        self.stdin_lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self.pending: dict[str, tuple[threading.Event, dict[str, Any]]] = {}
        self.reader_thread: threading.Thread | None = None
        self.stderr_thread: threading.Thread | None = None
        self.stopped = threading.Event()
        self.starting = False
        self.ready = threading.Event()

    def start(self, initialize: bool = True) -> None:
        verified_root = self.router.authorizer.authorize(str(self.root))
        if verified_root != self.root:
            raise MultiWorktreeError("worktree root changed during child startup")
        bootstrap = (
            "import runpy,sys; sys.path.pop(0); "
            "runpy.run_module('code_review_graph', run_name='__main__')"
        )
        command = [
            sys.executable,
            "-c",
            bootstrap,
            "serve",
            "--repo",
            str(self.root),
        ]
        if self.router.tools:
            command.extend(["--tools", self.router.tools])
        if self.router.auto_watch:
            command.append("--auto-watch")

        env = {
            key: value
            for key, value in os.environ.items()
            if key in _CHILD_ENV_KEYS or key.startswith("LC_")
        }
        env["PYTHONPATH"] = str(_PACKAGE_ROOT)
        # A single process-wide override would make every worktree use the
        # same database. Per-repository Registry data_dir remains supported.
        env.pop("CRG_DATA_DIR", None)
        env.pop("CRG_REPO_ROOT", None)
        self.process = subprocess.Popen(
            command,
            cwd=str(self.root),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
        )
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self.reader_thread = threading.Thread(
            target=self._read_stdout,
            name=f"crg-multi-worktree-stdout-{self.root.name}",
            daemon=True,
        )
        self.stderr_thread = threading.Thread(
            target=self._read_stderr,
            name=f"crg-multi-worktree-stderr-{self.root.name}",
            daemon=True,
        )
        self.reader_thread.start()
        self.stderr_thread.start()
        if initialize and self.router.initialize_params is not None:
            self._initialize(self.router.initialize_params)

    def _initialize(self, params: dict[str, Any]) -> None:
        request_id = f"multi-worktree-init-{uuid.uuid4()}"
        event = threading.Event()
        result: dict[str, Any] = {}
        key = _request_key(request_id)
        with self.pending_lock:
            self.pending[key] = (event, result)
        try:
            self.send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "initialize",
                    "params": params,
                }
            )
            if not event.wait(_CHILD_START_TIMEOUT):
                raise MultiWorktreeError(f"MCP child did not initialize for {self.root}")
            response = result.get("message")
            if not isinstance(response, dict) or "error" in response:
                raise MultiWorktreeError(f"MCP child initialization failed for {self.root}")
            self.send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                }
            )
        finally:
            with self.pending_lock:
                self.pending.pop(key, None)

    def send(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            raise MultiWorktreeError(f"MCP child is not running for {self.root}")
        encoded = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        with self.stdin_lock:
            try:
                process.stdin.write(encoded)
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise MultiWorktreeError(f"MCP child stopped for {self.root}") from exc

    def _read_stdout(self) -> None:
        assert self.process is not None
        assert self.process.stdout is not None
        try:
            for line in _iter_bounded_lines(self.process.stdout, _MAX_CHILD_FRAME_BYTES):
                if line is None:
                    self.router.child_failed(
                        self, "MCP child emitted an oversized JSON-RPC frame"
                    )
                    return
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self.router.child_failed(self, "MCP child emitted invalid JSON")
                    return
                if not isinstance(message, dict):
                    self.router.child_failed(self, "MCP child emitted a non-object message")
                    return
                message_id = message.get("id")
                if message_id is not None:
                    key = _request_key(message_id)
                    with self.pending_lock:
                        pending = self.pending.get(key)
                    if pending is not None:
                        event, result = pending
                        result["message"] = message
                        event.set()
                        continue
                self.router.child_message(self, message)
        finally:
            self.router.child_failed(self, "MCP child exited")

    def _read_stderr(self) -> None:
        assert self.process is not None
        assert self.process.stderr is not None
        for line in iter(self.process.stderr.readline, b""):
            if len(line) > _MAX_FRAME_BYTES:
                line = line[:_MAX_FRAME_BYTES] + b"\n"
            try:
                sys.stderr.buffer.write(line)
                sys.stderr.buffer.flush()
            except (BrokenPipeError, OSError):
                return

    def stop(self) -> None:
        process = self.process
        if process is None:
            return
        if process.poll() is not None:
            self.stopped.set()
            return
        with self.stdin_lock:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except (OSError, ValueError):
                    pass
        try:
            process.wait(timeout=_CHILD_STOP_TIMEOUT)
            self.stopped.set()
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=_CHILD_STOP_TIMEOUT)
        except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                try:
                    if os.name != "nt":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except (ProcessLookupError, OSError):
                    pass
                try:
                    process.wait(timeout=_CHILD_STOP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    pass
        self.stopped.set()


class MultiWorktreeRouter:
    """Multiplex JSON-RPC requests across repository-bound child servers."""

    def __init__(
        self,
        authorizer: WorktreeAuthorizer,
        tools: str | None,
        auto_watch: bool,
    ) -> None:
        self.authorizer = authorizer
        self.tools = tools
        self.auto_watch = auto_watch
        self.children: dict[Path, _Child] = {}
        self.db_paths: dict[Path, Path] = {}
        self.children_lock = threading.Lock()
        self.routes: dict[str, tuple[_Child, object]] = {}
        self.child_routes: dict[str, tuple[_Child, object]] = {}
        self.routes_lock = threading.Lock()
        self.output_lock = threading.Lock()
        self.initialize_params: dict[str, Any] | None = None
        self.shutdown = False

    @property
    def primary_root(self) -> Path:
        return self.authorizer.primary_root

    def write(self, message: dict[str, Any]) -> None:
        encoded = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        with self.output_lock:
            try:
                sys.stdout.buffer.write(encoded)
                sys.stdout.buffer.flush()
            except (BrokenPipeError, OSError):
                self.shutdown = True

    def child_message(self, child: _Child, message: dict[str, Any]) -> None:
        if "id" in message and "method" not in message:
            key = _request_key(message["id"])
            with self.routes_lock:
                route = self.routes.pop(key, None)
            if route is not None:
                routed_child, request_id = route
                if routed_child is child:
                    message["id"] = request_id
                    self.write(message)
                    return
            logger.warning("Ignoring unknown response from MCP child %s", child.root)
            return
        if "id" in message:
            parent_id = f"multi-worktree-server-{uuid.uuid4()}"
            reject = False
            with self.routes_lock:
                if len(self.child_routes) >= _MAX_INFLIGHT_REQUESTS:
                    reject = True
                else:
                    self.child_routes[_request_key(parent_id)] = (child, message["id"])
            if reject:
                logger.warning("Too many in-flight child MCP requests for %s", child.root)
                try:
                    child.send(_json_error(message["id"], -32002, "too many in-flight requests"))
                except MultiWorktreeError as exc:
                    self.child_failed(child, str(exc))
                return
            message["id"] = parent_id
            self.write(message)
            return
        # Forward server notifications and requests. The normal server does
        # not emit client requests during review, but forwarding is safer than
        # silently dropping protocol messages.
        self.write(message)

    def child_failed(self, child: _Child, reason: str) -> None:
        with self.children_lock:
            if self.children.get(child.root) is not child:
                return
            self.children.pop(child.root, None)
            self.db_paths.pop(child.root, None)
        with child.pending_lock:
            pending = list(child.pending.values())
            child.pending.clear()
        for event, result in pending:
            result["message"] = _json_error(None, -32001, reason)
            event.set()
        failed: list[object] = []
        with self.routes_lock:
            for key, (routed_child, request_id) in list(self.routes.items()):
                if routed_child is child:
                    self.routes.pop(key, None)
                    failed.append(request_id)
            for key, (routed_child, _request_id) in list(self.child_routes.items()):
                if routed_child is child:
                    self.child_routes.pop(key, None)
        for request_id in failed:
            self.write(_json_error(request_id, -32001, f"{reason}: {child.root}"))
        if not child.stopped.is_set():
            logger.error("code-review-graph child failed for %s: %s", child.root, reason)
            child.stop()

    def get_child(self, root: Path, *, initialize: bool = True) -> _Child:
        db_path = get_db_path(root, read_only=True).resolve()
        while True:
            with self.children_lock:
                child = self.children.get(root)
                if child is not None and child.starting:
                    ready = child.ready
                elif (
                    child is not None
                    and child.process is not None
                    and child.process.poll() is None
                ):
                    return child
                else:
                    for other_root, other_db_path in self.db_paths.items():
                        if other_root != root and other_db_path == db_path:
                            raise MultiWorktreeError(
                                "worktrees must not share a graph database: "
                                f"{root} and {other_root} resolve to {db_path}"
                            )
                    if len(self.children) >= _MAX_CHILDREN:
                        raise MultiWorktreeError("too many active worktree MCP children")
                    child = _Child(self, root)
                    child.starting = True
                    self.children[root] = child
                    self.db_paths[root] = db_path
                    ready = None
            if ready is not None:
                if not ready.wait(_CHILD_START_TIMEOUT):
                    raise MultiWorktreeError(f"MCP child did not start for {root}")
                continue
            break
        try:
            child.start(initialize=initialize)
        except Exception:
            with self.children_lock:
                if self.children.get(root) is child:
                    self.children.pop(root, None)
                    self.db_paths.pop(root, None)
            child.stop()
            raise
        finally:
            child.starting = False
            child.ready.set()
        return child

    def _forward(self, child: _Child, message: dict[str, Any]) -> None:
        if "id" in message:
            key = _request_key(message["id"])
            with self.routes_lock:
                if key in self.routes:
                    raise MultiWorktreeError("duplicate in-flight JSON-RPC request id")
                if len(self.routes) >= _MAX_INFLIGHT_REQUESTS:
                    raise MultiWorktreeError("too many in-flight JSON-RPC requests")
                self.routes[key] = (child, message["id"])
        try:
            child.send(message)
        except Exception:
            if "id" in message:
                with self.routes_lock:
                    self.routes.pop(_request_key(message["id"]), None)
            raise

    def handle(self, message: object) -> None:
        if not isinstance(message, dict):
            self.write(_json_error(None, -32600, "JSON-RPC message must be an object"))
            return
        method = message.get("method")
        if method == "initialize":
            params = message.get("params") or {}
            if not isinstance(params, dict):
                self.write(
                    _json_error(
                        message.get("id"),
                        -32602,
                        "initialize params must be an object",
                    )
                )
                return
            self.initialize_params = params
            try:
                # The parent's initialize request is the primary child's
                # protocol handshake. Other children perform an internal
                # handshake using the same negotiated client parameters.
                self._forward(self.get_child(self.primary_root, initialize=False), message)
            except Exception as exc:
                logger.exception("Failed to initialize MCP child")
                if "id" in message:
                    self.write(_json_error(message["id"], -32001, str(exc)))
            return

        if method == "notifications/initialized":
            with self.children_lock:
                primary = self.children.get(self.primary_root)
            if primary is not None:
                try:
                    primary.send(message)
                except MultiWorktreeError as exc:
                    self.child_failed(primary, str(exc))
            return

        if method == "notifications/cancelled":
            params = message.get("params") or {}
            request_id = params.get("requestId") if isinstance(params, dict) else None
            child = None
            if request_id is not None:
                with self.routes_lock:
                    route = self.routes.get(_request_key(request_id))
                if route is not None:
                    child = route[0]
            if child is not None:
                try:
                    child.send(message)
                except MultiWorktreeError as exc:
                    self.child_failed(child, str(exc))
            return

        if "method" not in message and "id" in message:
            with self.routes_lock:
                route = self.child_routes.pop(_request_key(message["id"]), None)
            if route is not None:
                child, child_id = route
                message["id"] = child_id
                try:
                    child.send(message)
                except MultiWorktreeError as exc:
                    self.child_failed(child, str(exc))
            return

        if method == "exit":
            with self.children_lock:
                children = list(self.children.values())
            for child in children:
                try:
                    child.send(message)
                except MultiWorktreeError as exc:
                    self.child_failed(child, str(exc))
            self.shutdown = True
            return

        root = self.primary_root
        if method == "tools/call":
            params = message.get("params") or {}
            if not isinstance(params, dict):
                self.write(
                    _json_error(
                        message.get("id"),
                        -32602,
                        "tools/call params must be an object",
                    )
                )
                return
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                self.write(
                    _json_error(
                        message.get("id"),
                        -32602,
                        "tools/call arguments must be an object",
                    )
                )
                return
            raw_root = arguments.get("repo_root")
            if raw_root is not None:
                try:
                    root = self.authorizer.authorize(raw_root)
                except MultiWorktreeError as exc:
                    if "id" in message:
                        self.write(_json_error(message["id"], -32602, str(exc)))
                    return
                params = dict(params)
                arguments = dict(arguments)
                arguments["repo_root"] = str(root)
                params["arguments"] = arguments
                message = dict(message)
                message["params"] = params
        try:
            self._forward(self.get_child(root), message)
        except Exception as exc:
            logger.exception("Failed to route MCP request")
            if "id" in message:
                self.write(_json_error(message["id"], -32001, str(exc)))

    def run(self) -> None:
        try:
            for raw_line in _iter_bounded_lines(sys.stdin.buffer, _MAX_FRAME_BYTES):
                if raw_line is None:
                    self.write(_json_error(None, -32700, "JSON-RPC frame is too large"))
                    continue
                try:
                    message = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self.write(_json_error(None, -32700, "invalid JSON-RPC message"))
                    continue
                self.handle(message)
                if self.shutdown:
                    break
        finally:
            self.close()

    def close(self) -> None:
        self.shutdown = True
        with self.children_lock:
            children = list(self.children.values())
            self.children.clear()
            self.db_paths.clear()
        for child in children:
            child.stop()


def run_multi_worktree_server(
    repo_root: str | None = None,
    tools: str | None = None,
    auto_watch: bool = False,
) -> None:
    """Run the opt-in multi-worktree stdio MCP server."""
    if os.environ.get("CRG_DATA_DIR", "").strip():
        raise ValueError(
            "--multi-worktree cannot be combined with CRG_DATA_DIR; "
            "use per-repository Registry data_dir entries instead"
        )
    router = MultiWorktreeRouter(
        WorktreeAuthorizer(repo_root),
        tools,
        auto_watch,
    )
    atexit.register(router.close)

    def _stop_on_signal(signum: int, _frame: Any) -> None:
        router.close()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGINT, _stop_on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop_on_signal)
    router.run()
