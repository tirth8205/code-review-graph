"""Shared utilities for tool sub-modules."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from ..graph import GraphStore
from ..incremental import find_project_root, get_db_path


def _error_response(
    message: str, status: str = "error", **extra: Any,
) -> dict[str, Any]:
    """Build a standardised error response dict."""
    return {"status": status, "error": message, "summary": message, **extra}

# Common JS/TS builtin method names filtered from callers_of results.
# "Who calls .map()?" returns hundreds of hits and is never useful.
# These are kept in the graph (callees_of still shows them) but excluded
# when doing reverse call tracing to reduce noise.
_BUILTIN_CALL_NAMES: set[str] = {
    "map", "filter", "reduce", "reduceRight", "forEach", "find", "findIndex",
    "some", "every", "includes", "indexOf", "lastIndexOf",
    "push", "pop", "shift", "unshift", "splice", "slice",
    "concat", "join", "flat", "flatMap", "sort", "reverse", "fill",
    "keys", "values", "entries", "from", "isArray", "of", "at",
    "trim", "trimStart", "trimEnd", "split", "replace", "replaceAll",
    "match", "matchAll", "search", "substring", "substr",
    "toLowerCase", "toUpperCase", "startsWith", "endsWith",
    "padStart", "padEnd", "repeat", "charAt", "charCodeAt",
    "assign", "freeze", "defineProperty", "getOwnPropertyNames",
    "hasOwnProperty", "create", "is", "fromEntries",
    "log", "warn", "error", "info", "debug", "trace", "dir", "table",
    "time", "timeEnd", "assert", "clear", "count",
    "then", "catch", "finally", "resolve", "reject", "all", "allSettled", "race", "any",
    "parse", "stringify",
    "floor", "ceil", "round", "random", "max", "min", "abs", "pow", "sqrt",
    "addEventListener", "removeEventListener", "querySelector", "querySelectorAll",
    "getElementById", "createElement", "appendChild", "removeChild",
    "setAttribute", "getAttribute", "preventDefault", "stopPropagation",
    "setTimeout", "clearTimeout", "setInterval", "clearInterval",
    "toString", "valueOf", "toJSON", "toISOString",
    "getTime", "getFullYear", "now",
    "isNaN", "parseInt", "parseFloat", "toFixed",
    "encodeURIComponent", "decodeURIComponent",
    "call", "apply", "bind", "next",
    "emit", "on", "off", "once",
    "pipe", "write", "read", "end", "close", "destroy",
    "send", "status", "json", "redirect",
    "set", "get", "delete", "has",
    "findUnique", "findFirst", "findMany", "createMany",
    "update", "updateMany", "deleteMany", "upsert",
    "aggregate", "groupBy", "transaction",
    "describe", "it", "test", "expect", "beforeEach", "afterEach",
    "beforeAll", "afterAll", "mock", "spyOn",
    "require", "fetch",
}


_CHANGED_FILES_CACHE_LOCK = threading.Lock()
_CHANGED_FILES_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
_CHANGED_FILES_CACHE_TTL = float(os.environ.get("CRG_CHANGED_FILES_CACHE_TTL", "3.0"))
_FAST_DIFF_TIMEOUT = float(os.environ.get("CRG_FAST_DIFF_TIMEOUT", "5.0"))


def _run_git(root: Path, args: list[str], timeout_s: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(root),
        timeout=timeout_s,
        stdin=subprocess.DEVNULL,
        check=False,
    )


def _normalize_changed_files(files: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for f in files:
        rel = f.strip().replace("\\", "/")
        if not rel or rel in seen:
            continue
        seen.add(rel)
        out.append(rel)
    return out


def _parse_porcelain_paths(stdout: str) -> list[str]:
    files: list[str] = []
    for line in stdout.splitlines():
        if not line.strip() or len(line) < 4:
            continue
        path_part = line[3:].strip()
        # Handle rename lines: "old/path -> new/path"
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1].strip()
        files.append(path_part)
    return files


def resolve_changed_files(
    root: Path,
    changed_files: list[str] | None,
    base: str,
) -> tuple[list[str], dict[str, Any]]:
    """Resolve changed files with fast auto-detect and short-lived cache.

    Returns:
        (files, meta)
        meta includes:
          - source: explicit | cache | auto
          - auto_detect_timed_out: bool
          - timeout_seconds: float
          - cache_hit: bool
    """
    if changed_files is not None:
        return _normalize_changed_files(changed_files), {
            "source": "explicit",
            "auto_detect_timed_out": False,
            "timeout_seconds": _FAST_DIFF_TIMEOUT,
            "cache_hit": False,
        }

    cache_key = (str(root.resolve()), base)
    now = time.monotonic()
    with _CHANGED_FILES_CACHE_LOCK:
        cached = _CHANGED_FILES_CACHE.get(cache_key)
        if cached and (now - float(cached["ts"])) <= _CHANGED_FILES_CACHE_TTL:
            return list(cached["files"]), {
                "source": "cache",
                "auto_detect_timed_out": False,
                "timeout_seconds": _FAST_DIFF_TIMEOUT,
                "cache_hit": True,
            }

    timed_out = False
    files: list[str] = []
    try:
        diff = _run_git(root, ["diff", "--name-only", base, "--"], _FAST_DIFF_TIMEOUT)
        if diff.returncode == 0:
            files.extend(diff.stdout.splitlines())
        else:
            cached_only = _run_git(
                root,
                ["diff", "--name-only", "--cached"],
                _FAST_DIFF_TIMEOUT,
            )
            if cached_only.returncode == 0:
                files.extend(cached_only.stdout.splitlines())

        status = _run_git(
            root,
            ["status", "--porcelain", "--untracked-files=no"],
            _FAST_DIFF_TIMEOUT,
        )
        if status.returncode == 0:
            files.extend(_parse_porcelain_paths(status.stdout))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        timed_out = True

    normalized = _normalize_changed_files(files)
    with _CHANGED_FILES_CACHE_LOCK:
        _CHANGED_FILES_CACHE[cache_key] = {"ts": now, "files": normalized}

    return normalized, {
        "source": "auto",
        "auto_detect_timed_out": timed_out,
        "timeout_seconds": _FAST_DIFF_TIMEOUT,
        "cache_hit": False,
    }


def _validate_repo_root(path: "Path | str") -> Path:
    """Validate that a path is a plausible project root.

    Ensures the path is an existing directory that contains a ``.git``,
    ``.svn``, or ``.code-review-graph`` directory, preventing arbitrary
    file-system traversal via the ``repo_root`` parameter.
    """
    resolved = Path(path).resolve()
    if not resolved.is_dir():
        raise ValueError(
            f"repo_root is not an existing directory: {resolved}"
        )
    has_vcs = (
        (resolved / ".git").exists()
        or (resolved / ".svn").exists()
        or (resolved / ".code-review-graph").exists()
    )
    if not has_vcs:
        raise ValueError(
            f"repo_root does not look like a project root "
            f"(no .git, .svn, or .code-review-graph directory found): "
            f"{resolved}"
        )
    return resolved


def _resolve_root(repo_root: str | None = None) -> Path:
    """Resolve and validate the repository root without opening a store."""
    return _validate_repo_root(Path(repo_root)) if repo_root else find_project_root()


def _get_store(repo_root: str | None = None) -> tuple[GraphStore, Path]:
    """Resolve repo root and open the graph store.

    Callers own the returned store and must close it (try/finally or
    context manager) to avoid leaking SQLite file descriptors.
    """
    root = _resolve_root(repo_root)
    db_path = get_db_path(root)
    return GraphStore(db_path), root


def _resolve_graph_file_paths(
    store: GraphStore, root: Path, file_paths: list[str],
) -> list[str]:
    """Resolve user-facing file paths to the paths stored in the graph.

    Graphs may contain absolute paths, repo-relative paths, or cwd-relative
    paths depending on how they were built. Tool inputs are usually relative to
    repo root, so exact matching alone can miss existing graph nodes.
    """
    resolved: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        if path not in seen:
            resolved.append(path)
            seen.add(path)

    for file_path in file_paths:
        raw = file_path.replace("\\", "/")
        candidates = [raw]
        path = Path(file_path)
        if path.is_absolute():
            try:
                candidates.append(str(path.resolve().relative_to(root)).replace("\\", "/"))
            except ValueError:
                pass
        else:
            candidates.append(str(root / path))

        for candidate in candidates:
            if store.get_nodes_by_file(candidate):
                add(candidate)

        suffixes = []
        for candidate in candidates:
            normalized = candidate.replace("\\", "/")
            if normalized not in suffixes:
                suffixes.append(normalized)

        for suffix in suffixes:
            for matched_path in store.get_files_matching(suffix):
                add(matched_path)

    return resolved


def compact_response(
    summary: str,
    key_entities: list[str] | None = None,
    risk: str = "unknown",
    communities: list[str] | None = None,
    flows_affected: list[str] | None = None,
    next_tool_suggestions: list[str] | None = None,
    data: dict[str, Any] | None = None,
    detail_level: str = "minimal",
) -> dict[str, Any]:
    """Standard compact response format for token efficiency."""
    resp: dict[str, Any] = {
        "status": "ok",
        "summary": summary,
    }
    if key_entities:
        resp["key_entities"] = key_entities[:10]
    if risk != "unknown":
        resp["risk"] = risk
    if communities:
        resp["communities"] = communities[:5]
    if flows_affected:
        resp["flows_affected"] = flows_affected[:5]
    if next_tool_suggestions:
        resp["next_tool_suggestions"] = next_tool_suggestions[:3]
    if detail_level != "minimal" and data:
        resp["data"] = data
    return resp
