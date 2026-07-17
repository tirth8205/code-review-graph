"""End-to-end regression for MCP stdio executor shutdown (PR #615)."""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _send(proc: subprocess.Popen[str], message: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()


def _read_response(
    proc: subprocess.Popen[str],
    request_id: int,
    timeout: float = 20,
) -> dict:
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select(
            [proc.stdout],
            [],
            [],
            max(0, deadline - time.monotonic()),
        )
        if not ready:
            break
        line = proc.stdout.readline()
        if not line:
            break
        response = json.loads(line)
        if response.get("id") == request_id:
            return response
    raise AssertionError(f"MCP response {request_id} did not arrive within {timeout}s")


@pytest.mark.skipif(os.name == "nt", reason="select() cannot poll Windows pipes")
def test_stdio_server_parallel_build_then_eof_exits_cleanly(tmp_path):
    """The real stdio server must build in parallel and exit cleanly on EOF."""
    (tmp_path / ".git").mkdir()
    for index in range(10):
        (tmp_path / f"module_{index}.py").write_text(
            f"def function_{index}():\n    return {index}\n",
            encoding="utf-8",
        )

    env = os.environ.copy()
    env.pop("CRG_PARSE_EXECUTOR", None)
    env.pop("CRG_SERIAL_PARSE", None)
    env.pop("CRG_TOOLS", None)
    env.pop("CRG_DATA_DIR", None)
    env.pop("CRG_REPO_ROOT", None)
    env["CRG_PARSE_WORKERS"] = "2"
    repo_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (repo_root, env.get("PYTHONPATH")) if value
    )

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "code_review_graph",
            "serve",
            "--repo",
            str(tmp_path),
        ],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "capabilities": {},
                    "clientInfo": {"name": "shutdown-test", "version": "1"},
                    "protocolVersion": "2024-11-05",
                },
            },
        )
        assert "result" in _read_response(proc, 1)
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
        )
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "build_or_update_graph_tool",
                    "arguments": {
                        "repo_root": str(tmp_path),
                        "full_rebuild": True,
                        "postprocess": "none",
                    },
                },
            },
        )
        build_response = _read_response(proc, 2)
        assert "error" not in build_response
        build_payload = json.loads(build_response["result"]["content"][0]["text"])
        assert build_payload["status"] == "ok"
        assert build_payload["build_type"] == "full"
        assert build_payload["files_parsed"] == 10
        assert (tmp_path / ".code-review-graph" / "graph.db").is_file()

        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(timeout=10)
        stderr = proc.stderr.read() if proc.stderr is not None else ""
        assert proc.returncode == 0, stderr
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)
