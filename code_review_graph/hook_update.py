"""Payload-scoped graph updates for agent hooks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


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


def _read_payload() -> Any:
    """Read the event JSON as bytes and let ``json`` find their encoding.

    Hosts write UTF-8, but a text-mode stdin on Windows decodes a pipe with the
    ANSI code page. A ``cwd`` such as ``C:\\Users\\José\\repo`` then names a
    folder that does not exist, and the hook quietly did nothing.
    """
    return json.loads(getattr(sys.stdin, "buffer", sys.stdin).read())


def _hook_repo() -> Path | None:
    try:
        payload = _read_payload()
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    cwd = _payload_cwd(payload)
    if cwd is None:
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            check=True,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # Git writes the path as file-system bytes (UTF-8 on Windows), not in the
    # code page a text-mode pipe would decode with.
    root = os.fsdecode(result.stdout).strip()
    return Path(root).resolve() if root else None


def run_update_hook() -> None:
    """Update only the repository named by a valid hook payload cwd."""
    repo_root = _hook_repo()
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
