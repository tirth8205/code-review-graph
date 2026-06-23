"""Regression tests for the crg-daemon CLI handlers (daemon_cli.py).

Covers:
- #554: ``crg-daemon start --foreground`` must write the PID file and install
  signal handlers (via ``WatchDaemon.setup_foreground``).
- #511: ``_handle_stop`` must not crash on Windows where ``os.kill(pid, 0)``
  raises ``WinError 87``, ``signal.SIGKILL`` is undefined, and ``os.kill`` may
  raise a generic ``OSError``.
"""

from __future__ import annotations

import signal
from unittest.mock import MagicMock, patch

import pytest

from code_review_graph.daemon import DaemonConfig, WatchDaemon

# ===========================================================================
# #554 — foreground start writes PID + installs handlers
# ===========================================================================


class TestForegroundStart:
    def test_setup_foreground_writes_pid_file(self, tmp_path):
        """setup_foreground writes the PID file and installs signal handlers."""
        pid_path = tmp_path / "daemon.pid"
        daemon = WatchDaemon(config=DaemonConfig(repos=[]))

        with (
            patch("code_review_graph.daemon.PID_PATH", pid_path),
            patch.object(daemon, "_setup_signal_handlers") as mock_handlers,
        ):
            daemon.setup_foreground()

        assert pid_path.exists(), "PID file should be written on foreground start"
        mock_handlers.assert_called_once()

    def test_handle_start_foreground_calls_setup_foreground(self):
        """_handle_start invokes setup_foreground (not daemonize) in foreground."""
        from code_review_graph.daemon_cli import _handle_start

        args = MagicMock()
        args.foreground = True

        fake_daemon = MagicMock()

        with (
            patch("code_review_graph.daemon.is_daemon_running", return_value=False),
            patch("code_review_graph.daemon.load_config", return_value=DaemonConfig(repos=[])),
            patch("code_review_graph.daemon.WatchDaemon", return_value=fake_daemon),
        ):
            _handle_start(args)

        fake_daemon.setup_foreground.assert_called_once()
        fake_daemon.daemonize.assert_not_called()
        fake_daemon.run_forever.assert_called_once()

    def test_handle_start_background_calls_daemonize(self):
        """_handle_start invokes daemonize (not setup_foreground) in background."""
        from code_review_graph.daemon_cli import _handle_start

        args = MagicMock()
        args.foreground = False

        fake_daemon = MagicMock()

        with (
            patch("code_review_graph.daemon.is_daemon_running", return_value=False),
            patch("code_review_graph.daemon.load_config", return_value=DaemonConfig(repos=[])),
            patch("code_review_graph.daemon.WatchDaemon", return_value=fake_daemon),
        ):
            _handle_start(args)

        fake_daemon.daemonize.assert_called_once()
        fake_daemon.setup_foreground.assert_not_called()


# ===========================================================================
# #511 — _handle_stop is cross-platform safe
# ===========================================================================


class TestHandleStopCrossPlatform:
    def test_stop_uses_pid_alive_not_oskill_zero(self):
        """The wait-loop must use pid_alive(), not os.kill(pid, 0).

        On Windows os.kill(pid, 0) raises WinError 87. We simulate a process
        that is alive for a couple of polls then dies, and assert the loop
        escalates via pid_alive() and never probes liveness with os.kill(., 0).
        """
        from code_review_graph.daemon_cli import _handle_stop

        args = MagicMock()
        # Alive, alive, then dead.
        alive_values = iter([True, True, False])

        def fake_pid_alive(_pid):
            return next(alive_values, False)

        kill_calls = []

        def fake_kill(_pid, sig):
            kill_calls.append(sig)

        with (
            patch("code_review_graph.daemon.is_daemon_running", return_value=True),
            patch("code_review_graph.daemon.read_pid", return_value=4321),
            patch("code_review_graph.daemon.pid_alive", side_effect=fake_pid_alive),
            patch("code_review_graph.daemon.clear_pid"),
            patch("code_review_graph.daemon_cli.os.kill", side_effect=fake_kill),
            patch("code_review_graph.daemon_cli.time.sleep"),
            patch("builtins.print"),
        ):
            _handle_stop(args)

        # Only the graceful SIGTERM should have been sent (process died before
        # the loop exhausted), and liveness was never probed with signal 0.
        assert kill_calls == [signal.SIGTERM]

    def test_stop_escalates_with_sigkill_fallback(self, monkeypatch):
        """A stuck process is force-killed; SIGKILL falls back when undefined.

        Simulates Windows where signal.SIGKILL does not exist: the escalation
        must not raise AttributeError and must fall back to SIGTERM.
        """
        from code_review_graph.daemon_cli import _handle_stop

        # Pretend SIGKILL is unavailable (Windows behavior).
        monkeypatch.delattr(signal, "SIGKILL", raising=False)

        args = MagicMock()
        kill_calls = []

        def fake_kill(_pid, sig):
            kill_calls.append(sig)

        with (
            patch("code_review_graph.daemon.is_daemon_running", return_value=True),
            patch("code_review_graph.daemon.read_pid", return_value=4321),
            patch("code_review_graph.daemon.pid_alive", return_value=True),
            patch("code_review_graph.daemon.clear_pid") as mock_clear,
            patch("code_review_graph.daemon_cli.os.kill", side_effect=fake_kill),
            patch("code_review_graph.daemon_cli.time.sleep"),
            patch("builtins.print"),
        ):
            _handle_stop(args)

        # First the graceful SIGTERM, then the force-kill which falls back to
        # SIGTERM because SIGKILL is unavailable on this platform.
        assert kill_calls == [signal.SIGTERM, signal.SIGTERM]
        mock_clear.assert_called_once()

    def test_stop_handles_oserror_from_initial_kill(self):
        """A generic OSError (e.g. WinError) from the SIGTERM must not crash."""
        from code_review_graph.daemon_cli import _handle_stop

        args = MagicMock()

        with (
            patch("code_review_graph.daemon.is_daemon_running", return_value=True),
            patch("code_review_graph.daemon.read_pid", return_value=4321),
            patch(
                "code_review_graph.daemon_cli.os.kill",
                side_effect=OSError(87, "The parameter is incorrect"),
            ),
            patch("code_review_graph.daemon.clear_pid"),
            patch("builtins.print"),
            pytest.raises(SystemExit) as exc_info,
        ):
            _handle_stop(args)

        # Handled gracefully via sys.exit(1), not an unhandled OSError.
        assert exc_info.value.code == 1
