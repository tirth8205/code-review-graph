"""Opt-in JSON export + understand-quickly registry publish.

Wraps :func:`code_review_graph.visualization.export_graph_data` with the
metadata block looptech-ai/understand-quickly expects, writes the result to
``<data_dir>/graph.json``, and optionally fires a ``repository_dispatch`` so
the registry resyncs the entry. Gated on ``UNDERSTAND_QUICKLY_TOKEN`` — without
it, the JSON is still written and the dispatch is skipped (an informational
message is printed to stdout pointing at the nightly sync fallback).

Protocol: https://github.com/looptech-ai/understand-quickly/blob/main/docs/integrations/protocol.md
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess  # nosec B404
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple
from urllib import error as urlerror
from urllib import request as urlrequest

from .graph import GraphStore
from .visualization import export_graph_data

logger = logging.getLogger(__name__)

REGISTRY_REPO = "looptech-ai/understand-quickly"
DISPATCH_URL = f"https://api.github.com/repos/{REGISTRY_REPO}/dispatches"
TOOL_NAME = "code-review-graph"


def _git_head_sha(repo_root: Path) -> Optional[str]:
    try:
        out = subprocess.check_output(  # nosec B603 B607
            ["git", "rev-parse", "HEAD"], cwd=str(repo_root), stderr=subprocess.DEVNULL,
        )
        sha = out.decode().strip()
        if re.fullmatch(r"[0-9a-f]{40}", sha):
            return sha
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.debug("git rev-parse HEAD failed: %s", exc)
    return None


def _git_origin_owner_repo(repo_root: Path) -> Optional[Tuple[str, str]]:
    """Parse ``owner/repo`` from ``git remote get-url origin`` (https or ssh)."""
    try:
        out = subprocess.check_output(  # nosec B603 B607
            ["git", "remote", "get-url", "origin"],
            cwd=str(repo_root), stderr=subprocess.DEVNULL,
        )
        url = out.decode().strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.debug("git remote get-url failed: %s", exc)
        return None
    for pat in (
        r"https?://[^/]+/([^/]+)/([^/]+?)(?:\.git)?/?$",
        r"[^@]+@[^:]+:([^/]+)/([^/]+?)(?:\.git)?$",
    ):
        m = re.match(pat, url)
        if m:
            return m.group(1), m.group(2)
    return None


def _tool_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as pkg_version
        return pkg_version("code-review-graph")
    except PackageNotFoundError:
        return "dev"
    except Exception as exc:  # noqa: BLE001
        logger.debug("tool_version lookup failed: %s", exc)
        return "dev"


def build_publish_payload(store: GraphStore, repo_root: Path) -> dict:
    """Return ``export_graph_data(store)`` with a registry-shaped metadata block."""
    data = export_graph_data(store)
    metadata = {
        "tool": TOOL_NAME,
        "tool_version": _tool_version(),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    sha = _git_head_sha(repo_root)
    if sha:
        metadata["commit"] = sha
    data["metadata"] = metadata
    return data


def write_publish_json(store: GraphStore, repo_root: Path, output_path: Path) -> Path:
    payload = build_publish_payload(store, repo_root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return output_path


def fire_dispatch(owner: str, repo: str, token: str) -> Tuple[bool, str]:
    """POST a ``sync-entry`` repository_dispatch. Returns ``(ok, message)``."""
    body = json.dumps(
        {"event_type": "sync-entry", "client_payload": {"id": f"{owner}/{repo}"}}
    ).encode()
    req = urlrequest.Request(  # nosec B310 - fixed https URL
        DISPATCH_URL, data=body, method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": f"{TOOL_NAME}-publish",
        },
    )
    try:
        with urlrequest.urlopen(req, timeout=15) as resp:  # nosec B310
            status = getattr(resp, "status", 0) or resp.getcode()
            if 200 <= status < 300:
                return True, f"dispatch sent (HTTP {status})"
            return False, f"unexpected HTTP {status}"
    except urlerror.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except urlerror.URLError as exc:
        return False, f"network error: {exc.reason}"


def publish(
    store: GraphStore, repo_root: Path, output_path: Path, publish_to_uq: bool = False,
) -> Path:
    """Write JSON and optionally ping the registry. Always writes the JSON."""
    written = write_publish_json(store, repo_root, output_path)
    print(f"JSON exported: {written}")

    if not publish_to_uq:
        return written

    token = os.environ.get("UNDERSTAND_QUICKLY_TOKEN")
    if not token:
        print(
            "[understand-quickly] UNDERSTAND_QUICKLY_TOKEN not set; "
            "skipping repository_dispatch (nightly sync will pick this up)."
        )
        return written

    owner_repo = _git_origin_owner_repo(repo_root)
    if owner_repo is None:
        print(
            "[understand-quickly] could not derive owner/repo from "
            "`git remote get-url origin`; skipping dispatch."
        )
        return written

    owner, repo = owner_repo
    ok, message = fire_dispatch(owner, repo, token)
    if ok:
        print(f"[understand-quickly] {message} for {owner}/{repo}")
    else:
        print(
            f"[understand-quickly] dispatch failed for {owner}/{repo}: {message}. "
            "If this repo is not yet registered, register it once with: "
            "npx @understand-quickly/cli add"
        )
    return written
