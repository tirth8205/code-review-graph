"""UTF-8 output and Windows vanished-PID races from issue #866."""

import io
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from code_review_graph import daemon_cli


def test_daemon_main_prints_unicode_log_on_legacy_streams(tmp_path, monkeypatch):
    log = tmp_path / "daemon.log"
    log.write_text("Watching 中文/project ✓\n", encoding="utf-8")
    stdout_bytes, stderr_bytes = io.BytesIO(), io.BytesIO()
    stdout = io.TextIOWrapper(stdout_bytes, encoding="cp1252")
    stderr = io.TextIOWrapper(stderr_bytes, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(sys, "argv", ["crg-daemon", "logs"])
    with patch(
        "code_review_graph.daemon.load_config", return_value=SimpleNamespace(log_dir=tmp_path),
    ):
        daemon_cli.main()
    stdout.flush()
    assert stdout.encoding == stderr.encoding == "utf-8"
    assert "中文/project ✓" in stdout_bytes.getvalue().decode("utf-8")


def _windows_error(code):
    error = OSError("Windows process operation failed")
    error.winerror = code
    return error


@pytest.mark.parametrize("escalated", [False, True])
def test_stop_handles_windows_process_disappearing(escalated, capsys):
    gone = _windows_error(87)
    with (
        patch("code_review_graph.daemon.is_daemon_running", return_value=True),
        patch("code_review_graph.daemon.read_pid", return_value=4242),
        patch("code_review_graph.daemon.pid_alive", return_value=True),
        patch("code_review_graph.daemon.clear_pid") as clear,
        patch("code_review_graph.daemon.reap_orphaned_watchers", return_value=[]),
        patch("code_review_graph.daemon_cli.time.sleep"),
        patch(
            "code_review_graph.daemon_cli.os.kill", side_effect=[None, gone] if escalated else gone,
        ),
    ):
        daemon_cli._handle_stop(MagicMock())
    clear.assert_called_once()
    assert "Daemon stopped" in capsys.readouterr().out


@pytest.mark.parametrize("code", [5, 6, 123])
def test_other_windows_signal_errors_preserve_pid(code):
    with (
        patch("code_review_graph.daemon.is_daemon_running", return_value=True),
        patch("code_review_graph.daemon.read_pid", return_value=4242),
        patch("code_review_graph.daemon.pid_alive", return_value=True),
        patch("code_review_graph.daemon.clear_pid") as clear,
        patch("code_review_graph.daemon_cli.time.sleep"),
        patch("code_review_graph.daemon_cli.os.kill", side_effect=[None, _windows_error(code)]),
        pytest.raises(OSError),
    ):
        daemon_cli._handle_stop(MagicMock())
    clear.assert_not_called()
