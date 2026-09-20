"""Payload-scoped graph updates for agent hooks."""

from __future__ import annotations

import json
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


def _hook_repo() -> Path | None:
    try:
        payload = json.load(sys.stdin)
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
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    root = result.stdout.strip()
    return Path(root).resolve() if root else None


def run_update_hook() -> None:
    """Update only the repository named by a valid hook payload cwd."""
    repo_root = _hook_repo()
    if repo_root is None:
        return
    _update_repo(repo_root)


def _update_repo(repo_root: Path) -> None:
    from .tools.build import build_or_update_graph

    build_or_update_graph(full_rebuild=False, repo_root=str(repo_root), postprocess="minimal")


def run_status_hook() -> None:
    """Delegate to the normal status command for a valid hook payload cwd."""
    repo_root = _hook_repo()
    if repo_root is None:
        return
    subprocess.run(
        ["code-review-graph", "status", "--repo", str(repo_root)],
        check=True,
    )
