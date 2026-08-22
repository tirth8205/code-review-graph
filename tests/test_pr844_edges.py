"""Edge-case tests for the cross-platform daemon stop path (PR #844, issue #843).

Covers boundaries and interleavings beyond the PR's own tests:
- process dying mid-wait and on the very last poll (no escalation)
- the missing-SIGKILL fallback simulated with a plain namespace, so the
  behavior cannot pass by accident of MagicMock attribute magic
- escalation racing with process death (ProcessLookupError swallowed)
- PID cleanup when the liveness probe itself blows up mid-loop
- restart interleavings: escalation succeeding vs. failing
- real-process integration on POSIX: graceful SIGTERM stop and a child
  that ignores SIGTERM and must be force-stopped
"""

from __future__ import annotations

import signal
import subprocess
import sys
import threading
import time
import types
from unittest.mock import MagicMock, patch

import pytest

from code_review_graph.daemon_cli import _handle_restart, _handle_stop

REAL_SLEEP = time.sleep
PID = 4242


def _win_signal() -> types.SimpleNamespace:
    """A signal-module stand-in without SIGKILL, as on Windows.

    A plain namespace (unlike MagicMock) cannot fabricate attributes, so
    ``getattr(signal, "SIGKILL", ...)`` genuinely falls back.
    """
    return types.SimpleNamespace(SIGTERM=signal.SIGTERM)


class TestStopWaitLoopEdges:
    def test_stop_breaks_when_process_dies_mid_wait(self, capsys):
        """Death partway through the wait loop stops polling and skips escalation."""
        with (
            patch("code_review_graph.daemon.is_daemon_running", return_value=True),
            patch("code_review_graph.daemon.read_pid", return_value=PID),
            patch(
                "code_review_graph.daemon.pid_alive",
                side_effect=[True] * 10 + [False],
            ) as alive,
            patch("code_review_graph.daemon.clear_pid") as clear,
            patch("code_review_graph.daemon_cli.os.kill") as kill,
            patch("code_review_graph.daemon_cli.time.sleep") as sleep,
        ):
            _handle_stop(MagicMock())

        assert kill.call_count == 1  # only the initial SIGTERM
        assert alive.call_count == 11
        assert sleep.call_count == 10
        clear.assert_called_once()
        out = capsys.readouterr().out
        assert "force-stopping" not in out
        assert "Daemon stopped." in out

    def test_stop_death_on_final_poll_avoids_escalation(self, capsys):
        """A False on the 50th and last liveness check must still break, not escalate."""
        with (
            patch("code_review_graph.daemon.is_daemon_running", return_value=True),
            patch("code_review_graph.daemon.read_pid", return_value=PID),
            patch(
                "code_review_graph.daemon.pid_alive",
                side_effect=[True] * 49 + [False],
            ) as alive,
            patch("code_review_graph.daemon.clear_pid") as clear,
            patch("code_review_graph.daemon_cli.os.kill") as kill,
            patch("code_review_graph.daemon_cli.time.sleep"),
        ):
            _handle_stop(MagicMock())

        assert kill.call_count == 1
        assert alive.call_count == 50
        clear.assert_called_once()
        assert "force-stopping" not in capsys.readouterr().out

    def test_wait_loop_crash_still_clears_pid(self):
        """An unexpected error from the liveness probe must not leave a stale PID file."""
        with (
            patch("code_review_graph.daemon.is_daemon_running", return_value=True),
            patch("code_review_graph.daemon.read_pid", return_value=PID),
            patch(
                "code_review_graph.daemon.pid_alive",
                side_effect=RuntimeError("probe blew up"),
            ),
            patch("code_review_graph.daemon.clear_pid") as clear,
            patch("code_review_graph.daemon_cli.os.kill"),
            patch("code_review_graph.daemon_cli.time.sleep"),
            pytest.raises(RuntimeError, match="probe blew up"),
        ):
            _handle_stop(MagicMock())

        clear.assert_called_once()


class TestForcedStopEdges:
    def test_missing_sigkill_falls_back_to_sigterm_plain_namespace(self, capsys):
        """The SIGTERM fallback must work with a real attribute miss, not a mock quirk."""
        with (
            patch("code_review_graph.daemon.is_daemon_running", return_value=True),
            patch("code_review_graph.daemon.read_pid", return_value=PID),
            patch("code_review_graph.daemon.pid_alive", return_value=True),
            patch("code_review_graph.daemon.clear_pid") as clear,
            patch("code_review_graph.daemon_cli.signal", _win_signal()),
            patch("code_review_graph.daemon_cli.os.kill") as kill,
            patch("code_review_graph.daemon_cli.time.sleep"),
        ):
            _handle_stop(MagicMock())

        assert [c.args for c in kill.call_args_list] == [
            (PID, signal.SIGTERM),
            (PID, signal.SIGTERM),
        ]
        clear.assert_called_once()
        out = capsys.readouterr().out
        assert "force-stopping" in out
        assert "Daemon stopped." in out

    def test_forced_stop_process_already_gone_is_swallowed(self, capsys):
        """The process dying exactly at the 5s boundary must not crash the escalation."""
        with (
            patch("code_review_graph.daemon.is_daemon_running", return_value=True),
            patch("code_review_graph.daemon.read_pid", return_value=PID),
            patch("code_review_graph.daemon.pid_alive", return_value=True),
            patch("code_review_graph.daemon.clear_pid") as clear,
            patch(
                "code_review_graph.daemon_cli.os.kill",
                side_effect=[None, ProcessLookupError()],
            ),
            patch("code_review_graph.daemon_cli.time.sleep"),
        ):
            _handle_stop(MagicMock())

        clear.assert_called_once()
        assert "Daemon stopped." in capsys.readouterr().out


class TestRestartInterleavings:
    def test_windows_restart_with_forced_stop_still_starts(self):
        """Escalation during restart on Windows must not prevent the new start."""
        args = MagicMock()
        with (
            patch("code_review_graph.daemon.is_daemon_running", return_value=True),
            patch("code_review_graph.daemon.read_pid", return_value=PID),
            patch("code_review_graph.daemon.pid_alive", return_value=True),
            patch("code_review_graph.daemon.clear_pid") as clear,
            patch("code_review_graph.daemon_cli.signal", _win_signal()),
            patch("code_review_graph.daemon_cli.os.kill") as kill,
            patch("code_review_graph.daemon_cli.time.sleep"),
            patch("code_review_graph.daemon_cli._handle_start") as start,
        ):
            _handle_restart(args)

        assert kill.call_count == 2
        clear.assert_called_once()
        start.assert_called_once_with(args)

    def test_restart_aborts_start_when_forced_stop_fails(self):
        """A hard escalation failure aborts the restart but still clears the PID file."""
        with (
            patch("code_review_graph.daemon.is_daemon_running", return_value=True),
            patch("code_review_graph.daemon.read_pid", return_value=PID),
            patch("code_review_graph.daemon.pid_alive", return_value=True),
            patch("code_review_graph.daemon.clear_pid") as clear,
            patch(
                "code_review_graph.daemon_cli.os.kill",
                side_effect=[None, OSError("kill rejected")],
            ),
            patch("code_review_graph.daemon_cli.time.sleep"),
            patch("code_review_graph.daemon_cli._handle_start") as start,
            pytest.raises(OSError, match="kill rejected"),
        ):
            _handle_restart(MagicMock())

        start.assert_not_called()
        clear.assert_called_once()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX real-process integration")
class TestStopRealProcess:
    def test_graceful_stop_of_real_process(self, tmp_path, monkeypatch, capsys):
        """End to end on POSIX: SIGTERM stops a real child and removes the PID file."""
        monkeypatch.setenv("CRG_HOME", str(tmp_path))
        from code_review_graph.daemon import default_pid_path, write_pid

        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            write_pid(proc.pid)
            # Reap concurrently: an unreaped zombie would read as alive forever.
            reaper = threading.Thread(target=proc.wait, daemon=True)
            reaper.start()

            _handle_stop(MagicMock())

            reaper.join(timeout=10)
            assert not reaper.is_alive()
            assert proc.returncode == -signal.SIGTERM
            assert not default_pid_path().exists()
            out = capsys.readouterr().out
            assert "Daemon stopped." in out
            assert "force-stopping" not in out
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_sigterm_ignoring_process_is_force_stopped(self, tmp_path, monkeypatch, capsys):
        """A child that ignores SIGTERM must be escalated to SIGKILL and reaped."""
        monkeypatch.setenv("CRG_HOME", str(tmp_path))
        from code_review_graph.daemon import default_pid_path, write_pid

        child_src = (
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('ready', flush=True)\n"
            "time.sleep(60)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", child_src], stdout=subprocess.PIPE
        )
        try:
            assert proc.stdout is not None
            assert proc.stdout.readline().strip() == b"ready"
            write_pid(proc.pid)

            # Shrink the 5s wait to ~0.5s of real polling.
            with patch(
                "code_review_graph.daemon_cli.time.sleep",
                new=lambda _s: REAL_SLEEP(0.01),
            ):
                _handle_stop(MagicMock())

            proc.wait(timeout=10)
            assert proc.returncode == -signal.SIGKILL
            assert not default_pid_path().exists()
            assert "force-stopping" in capsys.readouterr().out
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
