"""Regression tests for MCP stdio zombie / FD-inheritance issues.

The MCP server communicates over stdio (stdin pipe).  When the host
disconnects (closes its end of the pipe), the server must exit cleanly
without leaving orphaned worker, fork-server, or zombie processes.

These tests verify:
1. ``_select_executor_kind()`` respects ``CRG_PARSE_EXECUTOR`` overrides
   and auto-switches to threads on Windows when stdin is not a TTY.
2. An end-to-end subprocess test that spawns the real MCP server with
   pipe stdin, triggers a parse, closes the parent side of stdin, and
   asserts the server exits within a short timeout with no zombies.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from code_review_graph.incremental import _select_executor_kind

# ---------------------------------------------------------------------------
# Unit tests for _select_executor_kind
# ---------------------------------------------------------------------------

class TestSelectExecutorKind:
    """Verify the executor selection logic and CRG_PARSE_EXECUTOR overrides."""

    def test_default_returns_process(self, monkeypatch):
        """Without any override, the default is 'process' on non-Windows."""
        monkeypatch.delenv("CRG_PARSE_EXECUTOR", raising=False)
        result = _select_executor_kind()
        if sys.platform == "win32" and not sys.stdin.isatty():
            assert result == "thread"
        else:
            assert result == "process"

    def test_explicit_process_override(self, monkeypatch):
        monkeypatch.setenv("CRG_PARSE_EXECUTOR", "process")
        assert _select_executor_kind() == "process"

    def test_explicit_thread_override(self, monkeypatch):
        monkeypatch.setenv("CRG_PARSE_EXECUTOR", "thread")
        assert _select_executor_kind() == "thread"

    def test_override_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("CRG_PARSE_EXECUTOR", "THREAD")
        assert _select_executor_kind() == "thread"

    def test_override_with_whitespace(self, monkeypatch):
        monkeypatch.setenv("CRG_PARSE_EXECUTOR", "  process  ")
        assert _select_executor_kind() == "process"

    def test_empty_string_ignored(self, monkeypatch):
        """An empty CRG_PARSE_EXECUTOR should fall through to platform logic."""
        monkeypatch.setenv("CRG_PARSE_EXECUTOR", "")
        result = _select_executor_kind()
        assert result in ("process", "thread")

    def test_invalid_value_ignored(self, monkeypatch):
        """An unrecognized value falls through to platform logic."""
        monkeypatch.setenv("CRG_PARSE_EXECUTOR", "fork")
        result = _select_executor_kind()
        assert result in ("process", "thread")

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only logic")
    def test_windows_stdio_auto_switches_to_thread(self, monkeypatch):
        """On Windows with non-TTY stdin (MCP stdio), auto-select thread."""
        monkeypatch.delenv("CRG_PARSE_EXECUTOR", raising=False)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        assert _select_executor_kind() == "thread"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only logic")
    def test_windows_tty_keeps_process(self, monkeypatch):
        """On Windows with a real TTY, keep the process executor."""
        monkeypatch.delenv("CRG_PARSE_EXECUTOR", raising=False)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        assert _select_executor_kind() == "process"


# ---------------------------------------------------------------------------
# End-to-end regression: MCP server exits cleanly on stdin close
# ---------------------------------------------------------------------------

def _descendants(pids: set[int]) -> set[int]:
    """Return all descendant PIDs of the given set (recursive)."""
    all_desc: set[int] = set()
    frontier = set(pids)
    while frontier:
        next_frontier: set[int] = set()
        for pid in frontier:
            try:
                children = subprocess.check_output(
                    ["pgrep", "-P", str(pid)],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
                for line in children.splitlines():
                    child = int(line)
                    if child not in all_desc:
                        all_desc.add(child)
                        next_frontier.add(child)
            except (subprocess.CalledProcessError, ValueError):
                pass
        frontier = next_frontier
    return all_desc


def _any_alive(pids: set[int]) -> list[int]:
    """Return PIDs that are still alive (kill -0 succeeds)."""
    alive = []
    for pid in pids:
        try:
            os.kill(pid, 0)
            alive.append(pid)
        except OSError:
            pass
    return alive


class TestMcpStdioZombieRegression:
    """End-to-end: server must exit cleanly when the host closes stdin."""

    TIMEOUT_S = 15  # generous for CI; real exit is < 2s

    def _find_server_script(self) -> str:
        """Locate the MCP server entry point for subprocess invocation."""
        # The package is installed; use `code-review-graph serve` via the
        # console_scripts entry point, or fall back to `python -m`.
        return sys.executable

    def test_server_exits_on_stdin_close(self, tmp_path):
        """Spawn MCP server with pipe stdin, close parent side, assert exit.

        Steps:
        1. Start ``code-review-graph serve`` with stdin=PIPE so the server
           reads from a pipe (not a TTY).
        2. Send a minimal JSON-RPC initialize message to trigger the
           server's read loop.
        3. Close the parent's write end of stdin to simulate a host
           disconnect.
        4. Wait up to TIMEOUT_S for the server to exit.
        5. Assert the exit code is 0 or the process was terminated cleanly.
        6. Assert no child/zombie processes remain.
        """
        env = os.environ.copy()
        env["CRG_PARSE_EXECUTOR"] = "thread"  # avoid fork issues in test
        env.pop("CRG_TOOLS", None)

        proc = subprocess.Popen(
            [self._find_server_script(), "-m", "code_review_graph.cli", "serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(tmp_path),
        )

        try:
            # Record children before we do anything.
            initial_children = _descendants({proc.pid})

            # Send a minimal JSON-RPC initialize request so the server
            # enters its read loop (it blocks on stdin.readline()).
            init_msg = json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0.1"},
                    "protocolVersion": "2024-11-05",
                },
            }) + "\n"
            try:
                proc.stdin.write(init_msg.encode())
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                # Server may have already exited (fast path).
                pass

            # Give the server a moment to process the initialize.
            time.sleep(0.5)

            # Close stdin to simulate host disconnect.
            try:
                proc.stdin.close()
            except OSError:
                pass

            # Wait for the server to exit.
            try:
                proc.wait(timeout=self.TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
                pytest.fail(
                    f"MCP server did not exit within {self.TIMEOUT_S}s "
                    f"after stdin was closed (pid={proc.pid})"
                )

            # Collect any new children that appeared during the run.
            time.sleep(1.0)
            final_children = _descendants({proc.pid})
            new_children = final_children - initial_children
            alive_orphans = _any_alive(new_children)

            assert not alive_orphans, (
                f"Orphan child processes still alive after server exit: "
                f"{alive_orphans} (server pid={proc.pid})"
            )

        finally:
            # Belt-and-suspenders cleanup.
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)

    def test_server_exits_on_stdin_close_with_parse(self, tmp_path):
        """Same as above but triggers a real parse before disconnecting.

        This exercises the process/thread executor path and verifies that
        worker processes are properly cleaned up when stdin closes mid-parse.
        """
        # Create a tiny repo so the server has something to parse.
        (tmp_path / ".git").mkdir()
        (tmp_path / "hello.py").write_text("def hello():\n    return 42\n")

        env = os.environ.copy()
        env["CRG_PARSE_EXECUTOR"] = "thread"
        env.pop("CRG_TOOLS", None)

        proc = subprocess.Popen(
            [self._find_server_script(), "-m", "code_review_graph.cli", "serve",
             "--repo", str(tmp_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(tmp_path),
        )

        try:
            initial_children = _descendants({proc.pid})

            # Send initialize.
            init_msg = json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0.1"},
                    "protocolVersion": "2024-11-05",
                },
            }) + "\n"
            try:
                proc.stdin.write(init_msg.encode())
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

            # Send a tools/call to trigger a build (uses the executor).
            build_msg = json.dumps({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {
                    "name": "build_or_update_graph_tool",
                    "arguments": {"repo_root": str(tmp_path)},
                },
            }) + "\n"
            try:
                proc.stdin.write(build_msg.encode())
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

            # Give the parse some time to start.
            time.sleep(1.0)

            # Close stdin mid-parse.
            try:
                proc.stdin.close()
            except OSError:
                pass

            try:
                proc.wait(timeout=self.TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
                pytest.fail(
                    f"MCP server did not exit within {self.TIMEOUT_S}s "
                    f"after stdin close during parse (pid={proc.pid})"
                )

            time.sleep(1.0)
            final_children = _descendants({proc.pid})
            new_children = final_children - initial_children
            alive_orphans = _any_alive(new_children)

            assert not alive_orphans, (
                f"Orphan child processes still alive after mid-parse "
                f"stdin close: {alive_orphans} (server pid={proc.pid})"
            )

        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)
