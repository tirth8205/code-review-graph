"""A restart must retain its PID until the old daemon's exit is confirmed."""

import signal
from unittest.mock import MagicMock, patch

import pytest

from code_review_graph.daemon import default_pid_path, read_pid, write_pid
from code_review_graph.daemon_cli import _handle_restart, _handle_stop

PID = 4242


@pytest.mark.parametrize("forced_live_probes", [0, 24, 49])
def test_restart_waits_for_forced_exit_confirmation(tmp_path, monkeypatch, forced_live_probes):
    monkeypatch.setenv("CRG_HOME", str(tmp_path))
    write_pid(PID)
    probes = iter([True] * (50 + forced_live_probes) + [False])
    observed = []

    def alive(pid):
        assert pid == PID
        result = next(probes)
        observed.append(result)
        return result

    def start(_args):
        assert observed[-1] is False, "restart began before the old daemon exited"
        assert not default_pid_path().exists()

    with (
        patch("code_review_graph.daemon.is_daemon_running", return_value=True),
        patch("code_review_graph.daemon.pid_alive", side_effect=alive),
        patch("code_review_graph.daemon_cli.os.kill") as kill,
        patch("code_review_graph.daemon_cli.time.sleep") as sleep,
        patch("code_review_graph.daemon_cli._handle_start", side_effect=start) as started,
    ):
        _handle_restart(MagicMock())

    assert [call.args for call in kill.call_args_list] == [
        (PID, signal.SIGTERM),
        (PID, getattr(signal, "SIGKILL", signal.SIGTERM)),
    ]
    assert len(observed) == 51 + forced_live_probes
    assert sleep.call_count == 50 + forced_live_probes
    started.assert_called_once()


def test_restart_retains_pid_when_forced_signal_does_not_end_process(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path))
    write_pid(PID)
    with (
        patch("code_review_graph.daemon.is_daemon_running", return_value=True),
        patch("code_review_graph.daemon.pid_alive", return_value=True) as alive,
        patch("code_review_graph.daemon_cli.os.kill"),
        patch("code_review_graph.daemon_cli.time.sleep") as sleep,
        patch("code_review_graph.daemon_cli._handle_start") as started,
        pytest.raises(SystemExit) as error,
    ):
        _handle_restart(MagicMock())

    assert error.value.code == 1
    assert read_pid() == PID
    assert alive.call_count == 100
    assert sum(call.args[0] for call in sleep.call_args_list) == pytest.approx(10)
    started.assert_not_called()


def test_restart_retains_pid_when_forced_exit_probe_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path))
    write_pid(PID)
    with (
        patch("code_review_graph.daemon.is_daemon_running", return_value=True),
        patch(
            "code_review_graph.daemon.pid_alive", side_effect=[True] * 50 + [RuntimeError("probe")]
        ),
        patch("code_review_graph.daemon_cli.os.kill"),
        patch("code_review_graph.daemon_cli.time.sleep"),
        patch("code_review_graph.daemon_cli._handle_start") as started,
        pytest.raises(RuntimeError, match="probe"),
    ):
        _handle_restart(MagicMock())

    assert read_pid() == PID
    started.assert_not_called()


@pytest.mark.parametrize("failure", [PermissionError("denied"), OSError("failed")])
def test_restart_retains_pid_when_forced_signal_fails(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("CRG_HOME", str(tmp_path))
    write_pid(PID)
    with (
        patch("code_review_graph.daemon.is_daemon_running", return_value=True),
        patch("code_review_graph.daemon.pid_alive", return_value=True),
        patch("code_review_graph.daemon_cli.os.kill", side_effect=[None, failure]),
        patch("code_review_graph.daemon_cli.time.sleep"),
        patch("code_review_graph.daemon_cli._handle_start") as started,
        pytest.raises(type(failure)),
    ):
        _handle_restart(MagicMock())

    assert read_pid() == PID
    started.assert_not_called()


def test_restart_clears_stale_pid_before_starting(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path))
    write_pid(PID)

    def start(_args):
        assert read_pid() is None

    with (
        patch("code_review_graph.daemon.pid_alive", return_value=False),
        patch("code_review_graph.daemon_cli.os.kill") as kill,
        patch("code_review_graph.daemon_cli._handle_start", side_effect=start) as started,
    ):
        _handle_restart(MagicMock())

    kill.assert_not_called()
    started.assert_called_once()


def test_stop_does_not_signal_if_pid_disappears_after_running_check(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path))
    with (
        patch("code_review_graph.daemon.is_daemon_running", return_value=True),
        patch("code_review_graph.daemon_cli.os.kill") as kill,
        pytest.raises(SystemExit) as error,
    ):
        _handle_stop(MagicMock())

    assert error.value.code == 1
    kill.assert_not_called()
