"""Shared utilities for tool sub-modules."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from ..graph import GraphStore
from ..incremental import find_project_root, get_db_path


def _error_response(
    message: str, status: str = "error", **extra: Any,
) -> dict[str, Any]:
    """Build a standardised error response dict."""
    return {"status": status, "error": message, "summary": message, **extra}


def graph_provenance(repo_root: str | None = None) -> dict[str, Any] | None:
    """Freshness/provenance envelope for a repo's graph, or None.

    Reads build metadata (when the graph was last updated, and on which
    git branch/commit it was built) so every tool response can carry the
    context an agent needs to judge whether the answer is stale — a graph
    built three days ago on another branch answers questions about *that*
    tree, not the current one.

    Best-effort by design: any failure (no graph, unreadable DB, missing
    metadata) returns None and must never fail the tool call itself.
    """
    try:
        root = _resolve_root(repo_root)
        db_path = get_db_path(root)
        if not db_path.exists():
            return None
        # as_uri() percent-escapes URI-significant characters (#, %, ?)
        # that a plain f-string would hand to SQLite's URI parser raw.
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            rows = dict(conn.execute(
                "SELECT key, value FROM metadata WHERE key IN "
                "('last_updated', 'git_branch', 'git_head_sha')"
            ).fetchall())
        finally:
            conn.close()
        updated_at = rows.get("last_updated")
        if not updated_at:
            return None
        provenance: dict[str, Any] = {"updated_at": updated_at}
        try:
            # Stored via time.strftime("%Y-%m-%dT%H:%M:%S") in local time.
            built = datetime.fromisoformat(updated_at)
            provenance["age_seconds"] = max(
                0, int((datetime.now() - built).total_seconds()),
            )
        except ValueError:
            pass
        if rows.get("git_branch"):
            provenance["built_on_branch"] = rows["git_branch"]
        if rows.get("git_head_sha"):
            provenance["built_at_sha"] = rows["git_head_sha"]
        return provenance
    except Exception:
        return None


def with_provenance(result: Any, repo_root: str | None = None) -> Any:
    """Attach a ``_graph`` provenance envelope to a tool response dict.

    No-op for non-dict results, results that already carry ``_graph``,
    and repos where provenance cannot be determined.
    """
    if not isinstance(result, dict) or "_graph" in result:
        return result
    provenance = graph_provenance(repo_root)
    if provenance:
        result["_graph"] = provenance
    return result

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
