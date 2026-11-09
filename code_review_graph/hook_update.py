"""Payload-scoped graph updates for agent hooks.

An edit hook acts on the repository that holds the edited file, not on the
directory the agent was started in. One session can sit in a folder of sibling
repositories, in an outer repository with nested ones, or in the main checkout
while it edits a linked worktree; in each case the right graph is the one next
to the file (#311).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

# Keys an edit tool uses for the file it changed: ``file_path`` for Claude
# Code's Edit, Write and MultiEdit, ``notebook_path`` for its NotebookEdit.
_FILE_PATH_KEYS = ("file_path", "notebook_path")


def _payload_cwd(payload: Any) -> Path | None:
    if not isinstance(payload, dict):
        return None
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    path = Path(cwd)
    if not path.is_absolute() or not path.is_dir():
        return None
    return path.resolve()


def _payload_file(payload: Any, cwd: Path | None) -> Path | None:
    """Return the file the tool call changed, when the payload names one.

    A relative path is read against the payload ``cwd``, and ignored without
    one, so a guess about where the file lives can never pick a repository.
    """
    if not isinstance(payload, dict):
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    for key in _FILE_PATH_KEYS:
        value = tool_input.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        path = Path(value)
        if not path.is_absolute():
            if cwd is None:
                return None
            path = cwd / path
        try:
            return path.resolve()
        except (OSError, RuntimeError):
            return None
    return None


def _git(directory: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=directory,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def _toplevel(directory: Path) -> Path | None:
    """Return the work tree that holds ``directory``: the innermost one."""
    root = _git(directory, "rev-parse", "--show-toplevel")
    return Path(root).resolve() if root else None


def _superproject(repo_root: Path) -> Path | None:
    """Return the repository this one is a submodule of, if it is one.

    A nested repository that is not a submodule, and a linked worktree, are
    checkouts of their own: the outer repository's graph never holds their
    files, so they have no superproject here.
    """
    root = _git(repo_root, "rev-parse", "--show-superproject-working-tree")
    return Path(root).resolve() if root else None


def _recorded_root(db_path: Path) -> Path | None:
    """The repository root a graph says it was built for, read without writing."""
    # As in GraphStore: ``immutable`` reads a checkpointed WAL database
    # without creating its ``-shm`` sidecar.
    sidecars = (db_path.with_name(db_path.name + s) for s in ("-wal", "-shm"))
    query = "mode=ro" if any(p.exists() for p in sidecars) else "mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?{query}", uri=True, timeout=1)
        try:
            row = conn.execute(
                "SELECT value FROM metadata WHERE key = ?", ("repo_root",)
            ).fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError):
        return None
    if not row or not isinstance(row[0], str) or not row[0]:
        return None
    try:
        return Path(row[0]).resolve()
    except (OSError, RuntimeError):
        return None


def _has_graph(repo_root: Path) -> bool:
    """True when ``repo_root`` has a graph of its own.

    A graph in the default data directory, or at the legacy
    ``.code-review-graph.db`` that the update migrates, is the repository's.
    One anywhere else (``CRG_DATA_DIR`` or a registered ``--data-dir``) can be
    the database every repository resolves to, so it counts only when it
    records this repository as the one it was built for.
    """
    from .incremental import get_db_path

    try:
        db_path = get_db_path(repo_root, read_only=True)
        default_dir = (repo_root / ".code-review-graph").resolve()
        if db_path.parent.resolve() == default_dir:
            return db_path.is_file() or (repo_root / ".code-review-graph.db").is_file()
        return db_path.is_file() and _recorded_root(db_path) == repo_root.resolve()
    except (OSError, ValueError):
        return False


def _existing_dir(path: Path) -> Path | None:
    """The nearest existing directory at or above ``path``."""
    for candidate in (path, *path.parents):
        if candidate.is_dir():
            return candidate
    return None


def _repo_for_file(path: Path, session_repo: Path | None) -> Path | None:
    """Pick the graph to update for an edit to ``path``.

    The repository that holds the file is the answer when it already has a
    graph, or when it is the session's own repository, which is updated (and
    built on first use) exactly as before. Any other repository is left alone,
    so an edit somewhere else on disk cannot start a build there. A submodule
    with no graph of its own hands the edit to its superproject, whose graph
    can hold the submodule's files.
    """
    start = _existing_dir(path.parent)
    repo = _toplevel(start) if start is not None else None
    seen: set[Path] = set()
    while repo is not None and repo not in seen:
        if repo == session_repo or _has_graph(repo):
            return repo
        seen.add(repo)
        repo = _superproject(repo)
    return None


def _read_payload() -> Any:
    try:
        return json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def _session_repo(payload: Any) -> Path | None:
    cwd = _payload_cwd(payload)
    return _toplevel(cwd) if cwd is not None else None


def _hook_repo() -> Path | None:
    """The repository named by the payload ``cwd``, for the status hook."""
    return _session_repo(_read_payload())


def _update_target(payload: Any) -> Path | None:
    """The repository an edit hook should update, or None to do nothing."""
    session_repo = _session_repo(payload)
    edited = _payload_file(payload, _payload_cwd(payload))
    if edited is None:
        return session_repo
    return _repo_for_file(edited, session_repo)


def run_update_hook() -> None:
    """Update the graph of the repository that holds the edited file.

    Without a file path in the payload (a shell command, for example) the
    repository named by a valid payload ``cwd`` is updated, as before.
    """
    repo_root = _update_target(_read_payload())
    if repo_root is None:
        return
    _update_repo(repo_root)


def _update_repo(repo_root: Path) -> None:
    from .tools.build import build_or_update_graph

    try:
        result = build_or_update_graph(
            full_rebuild=False, repo_root=str(repo_root), postprocess="minimal"
        )
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    if result.get("status") == "error":
        print(f"Error: {result.get('summary', 'update failed')}", file=sys.stderr)
        sys.exit(1)


def run_status_hook() -> None:
    """Delegate to the normal status command for a valid hook payload cwd.

    Status only informs. A repository with no graph is normal, so its
    diagnostic is enough and the session start must not report a failed hook.
    """
    repo_root = _hook_repo()
    if repo_root is None:
        return
    try:
        subprocess.run(["code-review-graph", "status", "--repo", str(repo_root)], check=False)
    except OSError as exc:
        print(f"Error: {exc}", file=sys.stderr)
