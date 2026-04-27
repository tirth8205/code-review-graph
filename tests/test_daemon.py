"""Tests for daemon config, PID management, WatchDaemon, and CLI."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_review_graph.daemon import (
    DaemonConfig,
    WatchDaemon,
    WatchRepo,
    add_repo_to_config,
    clear_pid,
    is_daemon_running,
    load_config,
    load_state,
    read_pid,
    remove_repo_from_config,
    save_config,
    write_pid,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_config_file(tmp_path):
    """Create a valid watch.toml with temp repos that have .git dirs."""
    repo_a = tmp_path / "repo-a"
    repo_a.mkdir()
    (repo_a / ".git").mkdir()

    repo_b = tmp_path / "repo-b"
    repo_b.mkdir()
    (repo_b / ".git").mkdir()

    config = tmp_path / "watch.toml"
    config.write_text(
        f"[daemon]\n"
        f'session_name = "test-session"\n'
        f'log_dir = "{tmp_path / "logs"}"\n'
        f"poll_interval = 5\n"
        f"\n"
        f"[[repos]]\n"
        f'path = "{repo_a}"\n'
        f'alias = "alpha"\n'
        f"\n"
        f"[[repos]]\n"
        f'path = "{repo_b}"\n'
        f'alias = "beta"\n',
        encoding="utf-8",
    )
    return config


@pytest.fixture()
def pid_path(tmp_path):
    """Return a temporary PID file path."""
    return tmp_path / "daemon.pid"


# ===========================================================================
# Config Parsing Tests
# ===========================================================================


class TestConfigParsing:
    def test_load_config_valid(self, sample_config_file, tmp_path):
        """Parse a complete watch.toml from a tmp file."""
        cfg = load_config(sample_config_file)
        assert cfg.session_name == "test-session"
        assert cfg.log_dir == tmp_path / "logs"
        assert cfg.poll_interval == 5
        assert len(cfg.repos) == 2
        assert cfg.repos[0].alias == "alpha"
        assert cfg.repos[1].alias == "beta"

    def test_load_config_defaults(self, tmp_path):
        """Missing config file returns DaemonConfig with defaults."""
        missing = tmp_path / "nonexistent.toml"
        cfg = load_config(missing)
        assert cfg.session_name == "crg-watch"
        assert cfg.poll_interval == 2
        assert cfg.repos == []

    def test_load_config_missing_alias(self, tmp_path):
        """Alias is derived from directory name when not specified."""
        repo = tmp_path / "my-project"
        repo.mkdir()
        (repo / ".git").mkdir()

        config_file = tmp_path / "watch.toml"
        config_file.write_text(
            f'[[repos]]\npath = "{repo}"\n',
            encoding="utf-8",
        )
        cfg = load_config(config_file)
        assert len(cfg.repos) == 1
        assert cfg.repos[0].alias == "my-project"

    def test_load_config_invalid_path(self, tmp_path):
        """Bad repo path is skipped with a warning."""
        config_file = tmp_path / "watch.toml"
        config_file.write_text(
            '[[repos]]\npath = "/no/such/directory/ever"\nalias = "gone"\n',
            encoding="utf-8",
        )
        cfg = load_config(config_file)
        assert len(cfg.repos) == 0

    def test_load_config_duplicate_alias(self, tmp_path):
        """Duplicate aliases are rejected with a warning."""
        repo_a = tmp_path / "aaa"
        repo_a.mkdir()
        (repo_a / ".git").mkdir()

        repo_b = tmp_path / "bbb"
        repo_b.mkdir()
        (repo_b / ".git").mkdir()

        config_file = tmp_path / "watch.toml"
        config_file.write_text(
            f'[[repos]]\npath = "{repo_a}"\nalias = "dup"\n\n'
            f'[[repos]]\npath = "{repo_b}"\nalias = "dup"\n',
            encoding="utf-8",
        )
        cfg = load_config(config_file)
        assert len(cfg.repos) == 1
        assert cfg.repos[0].path == str(repo_a.resolve())

    def test_load_config_no_git_dir(self, tmp_path):
        """Repos without .git or .code-review-graph are skipped."""
        bare = tmp_path / "bare-dir"
        bare.mkdir()

        config_file = tmp_path / "watch.toml"
        config_file.write_text(
            f'[[repos]]\npath = "{bare}"\nalias = "bare"\n',
            encoding="utf-8",
        )
        cfg = load_config(config_file)
        assert len(cfg.repos) == 0

    def test_serialize_roundtrip(self, tmp_path):
        """save then load produces the same config."""
        repo = tmp_path / "roundtrip"
        repo.mkdir()
        (repo / ".git").mkdir()

        original = DaemonConfig(
            session_name="rt-session",
            log_dir=tmp_path / "rt-logs",
            poll_interval=7,
            repos=[WatchRepo(path=str(repo.resolve()), alias="rt")],
        )
        config_file = tmp_path / "roundtrip.toml"
        save_config(original, config_file)
        loaded = load_config(config_file)

        assert loaded.session_name == original.session_name
        assert loaded.log_dir == original.log_dir
        assert loaded.poll_interval == original.poll_interval
        assert len(loaded.repos) == 1
        assert loaded.repos[0].alias == "rt"
        assert loaded.repos[0].path == str(repo.resolve())

    def test_add_repo_to_config(self, tmp_path):
        """add_repo_to_config adds a repo and saves."""
        repo = tmp_path / "new-repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        config_file = tmp_path / "watch.toml"
        # start empty
        config_file.write_text("[daemon]\n", encoding="utf-8")

        cfg = add_repo_to_config(str(repo), alias="fresh", config_path=config_file)
        assert len(cfg.repos) == 1
        assert cfg.repos[0].alias == "fresh"

        # Verify persisted
        reloaded = load_config(config_file)
        assert len(reloaded.repos) == 1

    def test_add_repo_duplicate(self, tmp_path):
        """Adding an existing repo path is a no-op."""
        repo = tmp_path / "dup-repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        config_file = tmp_path / "watch.toml"
        config_file.write_text("[daemon]\n", encoding="utf-8")

        add_repo_to_config(str(repo), alias="first", config_path=config_file)
        cfg = add_repo_to_config(str(repo), alias="second", config_path=config_file)
        assert len(cfg.repos) == 1
        assert cfg.repos[0].alias == "first"

    def test_add_repo_duplicate_alias(self, tmp_path):
        """Adding a repo with an alias already in use raises ValueError."""
        repo_a = tmp_path / "repo-a"
        repo_a.mkdir()
        (repo_a / ".git").mkdir()

        repo_b = tmp_path / "repo-b"
        repo_b.mkdir()
        (repo_b / ".git").mkdir()

        config_file = tmp_path / "watch.toml"
        config_file.write_text("[daemon]\n", encoding="utf-8")

        add_repo_to_config(str(repo_a), alias="taken", config_path=config_file)
        with pytest.raises(ValueError, match="already in use"):
            add_repo_to_config(str(repo_b), alias="taken", config_path=config_file)

    def test_remove_repo_by_path(self, sample_config_file):
        """Removes a repo by its path."""
        cfg = load_config(sample_config_file)
        path_to_remove = cfg.repos[0].path

        updated = remove_repo_from_config(path_to_remove, config_path=sample_config_file)
        assert len(updated.repos) == 1
        assert updated.repos[0].alias == "beta"

    def test_remove_repo_by_alias(self, sample_config_file):
        """Removes a repo by its alias."""
        updated = remove_repo_from_config("alpha", config_path=sample_config_file)
        assert len(updated.repos) == 1
        assert updated.repos[0].alias == "beta"

    def test_remove_repo_not_found(self, sample_config_file):
        """Removing a non-existent repo is a no-op with warning."""
        original = load_config(sample_config_file)
        updated = remove_repo_from_config("nonexistent", config_path=sample_config_file)
        assert len(updated.repos) == len(original.repos)


# ===========================================================================
# PID Management Tests
# ===========================================================================


class TestPIDManagement:
    def test_write_read_pid(self, pid_path):
        """write then read returns the same PID."""
        write_pid(42, pid_path)
        assert read_pid(pid_path) == 42

    def test_read_pid_missing(self, pid_path):
        """Returns None when PID file does not exist."""
        assert read_pid(pid_path) is None

    def test_read_pid_invalid(self, pid_path):
        """Corrupt file returns None."""
        pid_path.write_text("not-a-number", encoding="utf-8")
        assert read_pid(pid_path) is None

    def test_clear_pid(self, pid_path):
        """Removes the PID file."""
        write_pid(99, pid_path)
        assert pid_path.exists()
        clear_pid(pid_path)
        assert not pid_path.exists()

    @patch("os.kill")
    def test_is_daemon_running_alive(self, mock_kill, pid_path):
        """os.kill(pid, 0) succeeds — daemon is running."""
        write_pid(1234, pid_path)
        mock_kill.return_value = None  # no exception = process exists
        assert is_daemon_running(pid_path) is True
        mock_kill.assert_called_once_with(1234, 0)

    @patch("os.kill", side_effect=ProcessLookupError)
    def test_is_daemon_running_dead(self, mock_kill, pid_path):
        """ProcessLookupError clears stale PID and returns False."""
        write_pid(9999, pid_path)
        assert is_daemon_running(pid_path) is False
        # Stale PID file should be cleaned up
        assert not pid_path.exists()


# ===========================================================================
# WatchDaemon Tests (mock subprocess.Popen)
# ===========================================================================


class TestWatchDaemon:
    @pytest.fixture()
    def daemon_env(self, tmp_path):
        """Set up a WatchDaemon with temp repos and graph.db stubs."""
        repo_a = tmp_path / "repo-a"
        repo_a.mkdir()
        (repo_a / ".git").mkdir()
        (repo_a / ".code-review-graph").mkdir()
        # Create graph.db so _initial_build is skipped
        (repo_a / ".code-review-graph" / "graph.db").touch()

        repo_b = tmp_path / "repo-b"
        repo_b.mkdir()
        (repo_b / ".git").mkdir()
        (repo_b / ".code-review-graph").mkdir()
        (repo_b / ".code-review-graph" / "graph.db").touch()

        config = DaemonConfig(
            session_name="test-sess",
            log_dir=tmp_path / "logs",
            poll_interval=1,
            repos=[
                WatchRepo(path=str(repo_a), alias="alpha"),
                WatchRepo(path=str(repo_b), alias="beta"),
            ],
        )
        config_file = tmp_path / "watch.toml"
        save_config(config, config_file)

        daemon = WatchDaemon(config=config, config_path=config_file)

        return {
            "daemon": daemon,
            "config": config,
            "tmp_path": tmp_path,
            "repo_a": repo_a,
            "repo_b": repo_b,
            "config_file": config_file,
        }

    @patch("code_review_graph.daemon.subprocess.Popen")
    @patch("code_review_graph.registry.Registry")
    def test_start_spawns_children(self, mock_registry_cls, mock_popen, daemon_env):
        """start() spawns a Popen child per repo."""
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        daemon = daemon_env["daemon"]
        daemon.start()
        try:
            # One Popen call per repo
            assert mock_popen.call_count == 2
            # Children are tracked
            assert len(daemon._children) == 2
            assert "alpha" in daemon._children
            assert "beta" in daemon._children
        finally:
            daemon.stop()

    @patch("code_review_graph.daemon.subprocess.Popen")
    @patch("code_review_graph.registry.Registry")
    def test_start_registers_repos(self, mock_registry_cls, mock_popen, daemon_env):
        """start() calls Registry.register for each repo."""
        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        daemon = daemon_env["daemon"]
        mock_registry = mock_registry_cls.return_value

        daemon.start()
        try:
            assert mock_registry.register.call_count == 2
            aliases = {c.kwargs["alias"] for c in mock_registry.register.call_args_list}
            assert aliases == {"alpha", "beta"}
        finally:
            daemon.stop()

    def test_reconcile_add(self, daemon_env):
        """New repo in config is registered, built if needed, and spawned."""
        daemon = daemon_env["daemon"]
        config = daemon_env["config"]
        daemon._state_path = daemon_env["tmp_path"] / "daemon-state.json"

        # Simulate initial state with only alpha
        mock_alpha = MagicMock()
        mock_alpha.pid = 100
        mock_alpha.poll.return_value = None
        daemon._current_repos = {"alpha": config.repos[0]}
        daemon._children = {"alpha": mock_alpha}

        # Remove graph.db for beta so _initial_build is triggered
        beta_db = Path(config.repos[1].path) / ".code-review-graph" / "graph.db"
        beta_db.unlink()

        with (
            patch("code_review_graph.daemon.subprocess.Popen") as mock_popen,
            patch("code_review_graph.daemon.subprocess.run") as mock_run,
            patch("code_review_graph.registry.Registry") as mock_registry_cls,
        ):
            mock_new = MagicMock()
            mock_new.pid = 999
            mock_popen.return_value = mock_new

            mock_run.return_value = MagicMock(returncode=0)
            mock_registry = mock_registry_cls.return_value

            # Reconcile with full config (alpha + beta)
            daemon.reconcile(config)

            # beta should have been registered in the registry
            mock_registry.register.assert_called_once_with(config.repos[1].path, alias="beta")

            # beta should have been built (no graph.db)
            assert mock_run.call_count == 1

            # beta should have been spawned
            assert mock_popen.call_count == 1
            assert "beta" in daemon._children

    def test_reconcile_add_skips_build_when_db_exists(self, daemon_env):
        """New repo with existing graph.db is registered and spawned without building."""
        daemon = daemon_env["daemon"]
        config = daemon_env["config"]
        daemon._state_path = daemon_env["tmp_path"] / "daemon-state.json"

        # Simulate initial state with only alpha
        mock_alpha = MagicMock()
        mock_alpha.pid = 100
        mock_alpha.poll.return_value = None
        daemon._current_repos = {"alpha": config.repos[0]}
        daemon._children = {"alpha": mock_alpha}

        # beta already has graph.db (from fixture) — build should be skipped

        with (
            patch("code_review_graph.daemon.subprocess.Popen") as mock_popen,
            patch("code_review_graph.daemon.subprocess.run") as mock_run,
            patch("code_review_graph.registry.Registry") as mock_registry_cls,
        ):
            mock_new = MagicMock()
            mock_new.pid = 999
            mock_popen.return_value = mock_new

            mock_registry = mock_registry_cls.return_value

            # Reconcile with full config (alpha + beta)
            daemon.reconcile(config)

            # beta should have been registered
            mock_registry.register.assert_called_once_with(config.repos[1].path, alias="beta")

            # No build should have been triggered (graph.db exists)
            mock_run.assert_not_called()

            # beta should have been spawned
            assert mock_popen.call_count == 1
            assert "beta" in daemon._children

    def test_reconcile_remove(self, daemon_env):
        """Removed repo from config terminates the child process."""
        daemon = daemon_env["daemon"]
        config = daemon_env["config"]
        daemon._state_path = daemon_env["tmp_path"] / "daemon-state.json"

        # Current state has both repos
        mock_alpha = MagicMock()
        mock_alpha.pid = 100
        mock_alpha.poll.return_value = None
        mock_beta = MagicMock()
        mock_beta.pid = 200
        mock_beta.poll.return_value = None
        daemon._current_repos = {r.alias: r for r in config.repos}
        daemon._children = {"alpha": mock_alpha, "beta": mock_beta}

        # Reconcile with only alpha
        new_config = DaemonConfig(
            session_name=config.session_name,
            log_dir=config.log_dir,
            poll_interval=config.poll_interval,
            repos=[config.repos[0]],
        )
        daemon.reconcile(new_config)

        mock_beta.terminate.assert_called_once()
        assert "beta" not in daemon._children
        assert "beta" not in daemon._current_repos

    def test_reconcile_noop(self, daemon_env):
        """No changes means no processes started or stopped."""
        daemon = daemon_env["daemon"]
        config = daemon_env["config"]
        daemon._state_path = daemon_env["tmp_path"] / "daemon-state.json"

        mock_alpha = MagicMock()
        mock_alpha.pid = 100
        mock_alpha.poll.return_value = None
        mock_beta = MagicMock()
        mock_beta.pid = 200
        mock_beta.poll.return_value = None
        daemon._current_repos = {r.alias: r for r in config.repos}
        daemon._children = {"alpha": mock_alpha, "beta": mock_beta}

        with patch("code_review_graph.daemon.subprocess.Popen") as mock_popen:
            daemon.reconcile(config)
            mock_popen.assert_not_called()
            mock_alpha.terminate.assert_not_called()
            mock_beta.terminate.assert_not_called()

    def test_reconcile_update_path(self, daemon_env, tmp_path):
        """Same alias but different path = register, build if needed, terminate + new child."""
        daemon = daemon_env["daemon"]
        config = daemon_env["config"]
        daemon._state_path = daemon_env["tmp_path"] / "daemon-state.json"

        mock_alpha = MagicMock()
        mock_alpha.pid = 100
        mock_alpha.poll.return_value = None
        mock_beta = MagicMock()
        mock_beta.pid = 200
        mock_beta.poll.return_value = None
        daemon._current_repos = {r.alias: r for r in config.repos}
        daemon._children = {"alpha": mock_alpha, "beta": mock_beta}

        # Create a new repo directory for alpha with a different path (no graph.db)
        new_repo = tmp_path / "repo-a-v2"
        new_repo.mkdir()
        (new_repo / ".git").mkdir()

        updated_config = DaemonConfig(
            session_name=config.session_name,
            log_dir=config.log_dir,
            poll_interval=config.poll_interval,
            repos=[
                WatchRepo(path=str(new_repo), alias="alpha"),
                config.repos[1],
            ],
        )

        with (
            patch("code_review_graph.daemon.subprocess.Popen") as mock_popen,
            patch("code_review_graph.daemon.subprocess.run") as mock_run,
            patch("code_review_graph.registry.Registry") as mock_registry_cls,
        ):
            mock_new = MagicMock()
            mock_new.pid = 777
            mock_popen.return_value = mock_new

            mock_run.return_value = MagicMock(returncode=0)
            mock_registry = mock_registry_cls.return_value

            daemon.reconcile(updated_config)

            # alpha should be registered at the new path
            mock_registry.register.assert_called_once_with(str(new_repo), alias="alpha")

            # alpha should be built (new path has no graph.db)
            assert mock_run.call_count == 1

            # alpha should be terminated then respawned
            mock_alpha.terminate.assert_called_once()
            assert mock_popen.call_count == 1
            assert daemon._children["alpha"] is mock_new

    def test_status_with_children(self, daemon_env):
        """status() returns correct dict with child process info."""
        daemon = daemon_env["daemon"]
        config = daemon_env["config"]

        mock_alpha = MagicMock()
        mock_alpha.pid = 111
        mock_alpha.poll.return_value = None  # alive
        mock_beta = MagicMock()
        mock_beta.pid = 222
        mock_beta.poll.return_value = 1  # dead

        daemon._current_repos = {r.alias: r for r in config.repos}
        daemon._children = {"alpha": mock_alpha, "beta": mock_beta}

        result = daemon.status()
        assert result["session_name"] == "test-sess"
        assert result["running"] is True
        assert len(result["repos"]) == 2

        repo_map = {r["alias"]: r for r in result["repos"]}
        assert repo_map["alpha"]["alive"] is True
        assert repo_map["alpha"]["pid"] == 111
        assert repo_map["beta"]["alive"] is False
        assert repo_map["beta"]["pid"] == 222

    def test_check_health_restarts_dead(self, daemon_env):
        """_check_health restarts a child whose poll() returns non-None."""
        daemon = daemon_env["daemon"]
        config = daemon_env["config"]
        daemon._state_path = daemon_env["tmp_path"] / "daemon-state.json"

        mock_alpha = MagicMock()
        mock_alpha.pid = 100
        mock_alpha.poll.return_value = 1  # dead
        mock_beta = MagicMock()
        mock_beta.pid = 200
        mock_beta.poll.return_value = None  # alive

        daemon._current_repos = {r.alias: r for r in config.repos}
        daemon._children = {"alpha": mock_alpha, "beta": mock_beta}

        with patch("code_review_graph.daemon.subprocess.Popen") as mock_popen:
            mock_new = MagicMock()
            mock_new.pid = 555
            mock_popen.return_value = mock_new

            daemon._check_health()

            # alpha should be restarted, beta untouched
            assert mock_popen.call_count == 1
            assert daemon._children["alpha"] is mock_new
            assert daemon._children["beta"] is mock_beta

    def test_stop_terminates_all_children(self, daemon_env):
        """stop() calls terminate on all children."""
        daemon = daemon_env["daemon"]
        config = daemon_env["config"]

        mock_alpha = MagicMock()
        mock_alpha.poll.return_value = None
        mock_beta = MagicMock()
        mock_beta.poll.return_value = None

        daemon._current_repos = {r.alias: r for r in config.repos}
        daemon._children = {"alpha": mock_alpha, "beta": mock_beta}

        daemon.stop()

        mock_alpha.terminate.assert_called_once()
        mock_beta.terminate.assert_called_once()
        assert len(daemon._children) == 0
        assert len(daemon._current_repos) == 0

    @patch("code_review_graph.daemon.subprocess.Popen")
    @patch("code_review_graph.registry.Registry")
    def test_start_persists_state(self, mock_registry_cls, mock_popen, daemon_env):
        """start() writes child PIDs to the state file on disk."""
        mock_proc_a = MagicMock()
        mock_proc_a.pid = 1001
        mock_proc_a.poll.return_value = None
        mock_proc_b = MagicMock()
        mock_proc_b.pid = 1002
        mock_proc_b.poll.return_value = None
        mock_popen.side_effect = [mock_proc_a, mock_proc_b]

        daemon = daemon_env["daemon"]
        state_path = daemon_env["tmp_path"] / "daemon-state.json"
        daemon._state_path = state_path

        daemon.start()
        try:
            state = load_state(state_path)
            assert state["alpha"]["pid"] == 1001
            assert state["beta"]["pid"] == 1002
        finally:
            daemon.stop()

    def test_health_check_updates_state(self, daemon_env):
        """_check_health persists updated PIDs after restarting a dead child."""
        daemon = daemon_env["daemon"]
        config = daemon_env["config"]
        state_path = daemon_env["tmp_path"] / "daemon-state.json"
        daemon._state_path = state_path

        mock_alpha = MagicMock()
        mock_alpha.pid = 2001
        mock_alpha.poll.return_value = 1  # dead
        mock_beta = MagicMock()
        mock_beta.pid = 2002
        mock_beta.poll.return_value = None  # alive

        daemon._current_repos = {r.alias: r for r in config.repos}
        daemon._children = {"alpha": mock_alpha, "beta": mock_beta}

        with patch("code_review_graph.daemon.subprocess.Popen") as mock_popen:
            mock_new = MagicMock()
            mock_new.pid = 3001
            mock_popen.return_value = mock_new

            daemon._check_health()

            state = load_state(state_path)
            assert state["alpha"]["pid"] == 3001
            assert state["beta"]["pid"] == 2002

    def test_status_from_state_reports_alive(self, daemon_env, tmp_path):
        """A fresh WatchDaemon can report status from persisted state file."""
        config = daemon_env["config"]
        state_path = tmp_path / "daemon-state.json"

        import json
        import os

        # Simulate a running daemon that persisted state with our own PID
        # (so os.kill(pid, 0) will succeed)
        our_pid = os.getpid()
        state = {
            "alpha": {"pid": our_pid, "path": config.repos[0].path},
            "beta": {"pid": our_pid, "path": config.repos[1].path},
        }
        state_path.write_text(json.dumps(state), encoding="utf-8")

        # Create a *fresh* WatchDaemon (like _handle_status does) with
        # the state path pointing to our persisted file
        fresh_daemon = WatchDaemon(config=config, config_path=daemon_env["config_file"])
        fresh_daemon._state_path = state_path

        result = fresh_daemon.status()
        repo_map = {r["alias"]: r for r in result["repos"]}

        # Bug: without the fix, both would show alive=False because
        # _children is empty on the fresh daemon instance
        assert repo_map["alpha"]["alive"] is True
        assert repo_map["beta"]["alive"] is True
        assert repo_map["alpha"]["pid"] == our_pid
        assert repo_map["beta"]["pid"] == our_pid


# ===========================================================================
# CLI Handler Tests
# ===========================================================================


class TestDaemonCLI:
    def test_handle_add_success(self, tmp_path):
        """_handle_add adds a repo and prints confirmation."""
        from code_review_graph.daemon_cli import _handle_add

        repo = tmp_path / "cli-repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        args = MagicMock()
        args.path = str(repo)
        args.alias = "cli-alias"

        with (
            patch(
                "code_review_graph.daemon.add_repo_to_config",
            ) as mock_add,
            patch(
                "code_review_graph.daemon.is_daemon_running",
                return_value=False,
            ),
            patch("builtins.print") as mock_print,
        ):
            _handle_add(args)
            mock_add.assert_called_once_with(str(repo), alias="cli-alias")
            # Verify confirmation printed
            printed = " ".join(str(c) for c in mock_print.call_args_list)
            assert "cli-alias" in printed

    def test_handle_remove_success(self):
        """_handle_remove removes a repo and prints confirmation."""
        from code_review_graph.daemon_cli import _handle_remove

        args = MagicMock()
        args.path_or_alias = "some-alias"

        repo = WatchRepo(path="/tmp/r", alias="some-alias")
        cfg_before = DaemonConfig(repos=[repo])
        cfg_after = DaemonConfig(repos=[])

        with (
            patch(
                "code_review_graph.daemon.load_config",
                return_value=cfg_before,
            ),
            patch(
                "code_review_graph.daemon.remove_repo_from_config",
                return_value=cfg_after,
            ),
            patch(
                "code_review_graph.daemon.is_daemon_running",
                return_value=False,
            ),
            patch("builtins.print") as mock_print,
        ):
            _handle_remove(args)
            printed = " ".join(str(c) for c in mock_print.call_args_list)
            assert "some-alias" in printed

    def test_handle_stop_not_running(self):
        """_handle_stop exits when daemon is not running."""
        from code_review_graph.daemon_cli import _handle_stop

        args = MagicMock()

        with (
            patch(
                "code_review_graph.daemon.is_daemon_running",
                return_value=False,
            ),
            patch("builtins.print"),
            pytest.raises(SystemExit) as exc_info,
        ):
            _handle_stop(args)

        assert exc_info.value.code == 1

    def test_handle_status_not_running(self):
        """_handle_status displays 'not running' when daemon is down."""
        from code_review_graph.daemon_cli import _handle_status

        args = MagicMock()
        cfg = DaemonConfig(repos=[])

        with (
            patch(
                "code_review_graph.daemon.is_daemon_running",
                return_value=False,
            ),
            patch(
                "code_review_graph.daemon.load_config",
                return_value=cfg,
            ),
            patch(
                "code_review_graph.daemon.read_pid",
                return_value=None,
            ),
            patch("builtins.print") as mock_print,
        ):
            _handle_status(args)
            printed = " ".join(str(c) for c in mock_print.call_args_list)
            assert "not running" in printed

    def test_handle_status_shows_alive_for_running_watchers(self, tmp_path):
        """_handle_status reports 'alive' for watchers whose PIDs are running.

        Regression test: previously _handle_status created a fresh WatchDaemon
        with an empty _children dict, so all repos appeared dead even when
        watcher processes were running.
        """
        import os

        from code_review_graph.daemon_cli import _handle_status

        repo = tmp_path / "my-repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        args = MagicMock()
        our_pid = os.getpid()
        cfg = DaemonConfig(
            repos=[WatchRepo(path=str(repo), alias="myrepo")],
            log_dir=tmp_path / "logs",
        )

        state = {"myrepo": {"pid": our_pid, "path": str(repo)}}

        with (
            patch(
                "code_review_graph.daemon.is_daemon_running",
                return_value=True,
            ),
            patch(
                "code_review_graph.daemon.load_config",
                return_value=cfg,
            ),
            patch(
                "code_review_graph.daemon.read_pid",
                return_value=our_pid,
            ),
            patch(
                "code_review_graph.daemon.load_state",
                return_value=state,
            ),
            patch("builtins.print") as mock_print,
        ):
            _handle_status(args)
            printed = " ".join(str(c) for c in mock_print.call_args_list)
            assert "alive" in printed
            assert "dead" not in printed

    def test_handle_start_already_running(self):
        """_handle_start exits with error when daemon is already running."""
        from code_review_graph.daemon_cli import _handle_start

        args = MagicMock()
        args.foreground = False

        with (
            patch(
                "code_review_graph.daemon.is_daemon_running",
                return_value=True,
            ),
            patch("builtins.print"),
            pytest.raises(SystemExit) as exc_info,
        ):
            _handle_start(args)

        assert exc_info.value.code == 1

    def test_handle_logs_missing_file(self, tmp_path):
        """_handle_logs exits when log file does not exist."""
        from code_review_graph.daemon_cli import _handle_logs

        args = MagicMock()
        args.repo = None
        args.follow = False
        args.lines = 50

        cfg = DaemonConfig(log_dir=tmp_path / "no-logs")

        with (
            patch(
                "code_review_graph.daemon.load_config",
                return_value=cfg,
            ),
            patch("builtins.print"),
            pytest.raises(SystemExit) as exc_info,
        ):
            _handle_logs(args)

        assert exc_info.value.code == 1

    def test_handle_logs_reads_lines(self, tmp_path):
        """_handle_logs reads last N lines from log file."""
        from code_review_graph.daemon_cli import _handle_logs

        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        log_file = log_dir / "daemon.log"
        log_file.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")

        args = MagicMock()
        args.repo = None
        args.follow = False
        args.lines = 3

        cfg = DaemonConfig(log_dir=log_dir)

        with (
            patch(
                "code_review_graph.daemon.load_config",
                return_value=cfg,
            ),
            patch("builtins.print") as mock_print,
        ):
            _handle_logs(args)
            # Should have printed last 3 lines
            assert mock_print.call_count == 3
            printed_lines = [str(c.args[0]) for c in mock_print.call_args_list]
            assert printed_lines == ["line3", "line4", "line5"]
