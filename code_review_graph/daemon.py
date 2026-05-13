"""Multi-repo watch daemon for code-review-graph.

Reads ``~/.code-review-graph/watch.toml`` to configure which repositories
to watch, then spawns one ``code-review-graph watch`` child process per
repo.  Monitors the config file for live changes (adding/removing repos)
and health-checks child processes, restarting any that die.

No external dependencies beyond Python stdlib — no tmux required.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config file location
# ---------------------------------------------------------------------------

CONFIG_PATH: Path = Path.home() / ".code-review-graph" / "watch.toml"
PID_PATH: Path = Path.home() / ".code-review-graph" / "daemon.pid"
STATE_PATH: Path = Path.home() / ".code-review-graph" / "daemon-state.json"
_HEALTH_CHECK_INTERVAL = 30

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class WatchRepo:
    """A single repository to watch."""

    path: str
    """Resolved absolute path to the repository root."""

    alias: str
    """Short name for this repo (derived from directory name when not specified)."""


@dataclass
class DaemonConfig:
    """Top-level daemon configuration."""

    session_name: str = "crg-watch"
    """Logical daemon name (used in log messages and status output)."""

    log_dir: Path = field(default_factory=lambda: Path.home() / ".code-review-graph" / "logs")
    """Directory for per-repo log files."""

    poll_interval: int = 2
    """Seconds between file-system polls for config changes."""

    repos: list[WatchRepo] = field(default_factory=list)
    """Repositories the daemon watches."""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_config(path: Path | None = None) -> DaemonConfig:
    """Load daemon configuration from a TOML file.

    Args:
        path: Explicit config path.  Falls back to :data:`CONFIG_PATH`.

    Returns:
        A fully-validated :class:`DaemonConfig`.

    Raises:
        RuntimeError: If ``tomllib`` / ``tomli`` is unavailable on Python < 3.11.
    """
    if tomllib is None:
        raise RuntimeError(
            "TOML parsing requires the 'tomli' package on Python < 3.11. "
            "Install it with:  pip install tomli"
        )

    config_path = path or CONFIG_PATH

    if not config_path.exists():
        logger.info("Config file not found at %s — using defaults", config_path)
        return DaemonConfig()

    with open(config_path, "rb") as fh:
        raw: dict[str, Any] = tomllib.load(fh)

    # -- [daemon] section ---------------------------------------------------
    daemon_section: dict[str, Any] = raw.get("daemon", {})
    session_name: str = daemon_section.get("session_name", "crg-watch")
    log_dir = Path(daemon_section.get("log_dir", str(DaemonConfig().log_dir)))
    poll_interval: int = int(daemon_section.get("poll_interval", 2))

    # -- [[repos]] array ----------------------------------------------------
    repos: list[WatchRepo] = []
    seen_aliases: set[str] = set()

    for entry in raw.get("repos", []):
        repo_path_str: str = entry.get("path", "")
        if not repo_path_str:
            logger.warning("Skipping repo entry with empty path")
            continue

        repo_path = Path(repo_path_str).expanduser().resolve()

        if not repo_path.is_dir():
            logger.warning("Skipping repo %s — directory does not exist", repo_path)
            continue

        if not (repo_path / ".git").exists() and not (repo_path / ".code-review-graph").exists():
            logger.warning(
                "Skipping repo %s — no .git or .code-review-graph directory found",
                repo_path,
            )
            continue

        alias: str = entry.get("alias", "") or repo_path.name

        if alias in seen_aliases:
            logger.warning("Skipping duplicate alias '%s' for repo %s", alias, repo_path)
            continue

        seen_aliases.add(alias)
        repos.append(WatchRepo(path=str(repo_path), alias=alias))

    return DaemonConfig(
        session_name=session_name,
        log_dir=log_dir,
        poll_interval=poll_interval,
        repos=repos,
    )


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


def _serialize_toml(config: DaemonConfig) -> str:
    """Serialize a :class:`DaemonConfig` to TOML text.

    ``tomllib`` is read-only, so we build the TOML manually.
    """
    lines: list[str] = [
        "[daemon]",
        f'session_name = "{config.session_name}"',
        f'log_dir = "{config.log_dir}"',
        f"poll_interval = {config.poll_interval}",
    ]
    for repo in config.repos:
        lines.append("")
        lines.append("[[repos]]")
        lines.append(f'path = "{repo.path}"')
        lines.append(f'alias = "{repo.alias}"')
    lines.append("")  # trailing newline
    return "\n".join(lines)


def save_config(config: DaemonConfig, path: Path | None = None) -> None:
    """Write *config* back to a TOML file.

    Creates parent directories if they do not exist.

    Args:
        config: The daemon configuration to persist.
        path:   Explicit config path.  Falls back to :data:`CONFIG_PATH`.
    """
    config_path = path or CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(_serialize_toml(config), encoding="utf-8")
    logger.info("Config saved to %s", config_path)


# ---------------------------------------------------------------------------
# Convenience helpers (used by CLI commands)
# ---------------------------------------------------------------------------


def add_repo_to_config(
    repo_path: str,
    alias: str | None = None,
    config_path: Path | None = None,
) -> DaemonConfig:
    """Add a repository to the daemon config and persist the change.

    Args:
        repo_path:   Path to the repository (will be resolved to absolute).
        alias:       Optional short name.  Derived from dirname if *None*.
        config_path: Explicit config file path.  Falls back to :data:`CONFIG_PATH`.

    Returns:
        The updated :class:`DaemonConfig`.

    Raises:
        ValueError: If the path is not a valid repository directory.
    """
    resolved = Path(repo_path).expanduser().resolve()

    if not resolved.is_dir():
        raise ValueError(f"Not a directory: {resolved}")

    if not (resolved / ".git").exists() and not (resolved / ".code-review-graph").exists():
        raise ValueError(f"No .git or .code-review-graph directory in {resolved}")

    effective_alias = alias or resolved.name

    config = load_config(config_path)

    # Check for duplicate path or alias
    for existing in config.repos:
        if existing.path == str(resolved):
            logger.warning("Repo %s is already configured — skipping", resolved)
            return config
        if existing.alias == effective_alias:
            raise ValueError(f"Alias '{effective_alias}' is already in use by {existing.path}")

    config.repos.append(WatchRepo(path=str(resolved), alias=effective_alias))
    save_config(config, config_path)
    return config


def remove_repo_from_config(
    path_or_alias: str,
    config_path: Path | None = None,
) -> DaemonConfig:
    """Remove a repository from the daemon config by path or alias.

    Args:
        path_or_alias: Either the absolute/relative repo path or its alias.
        config_path:   Explicit config file path.  Falls back to :data:`CONFIG_PATH`.

    Returns:
        The updated :class:`DaemonConfig`.
    """
    config = load_config(config_path)
    resolved = str(Path(path_or_alias).expanduser().resolve())

    original_count = len(config.repos)
    config.repos = [r for r in config.repos if r.path != resolved and r.alias != path_or_alias]

    if len(config.repos) == original_count:
        logger.warning(
            "No repo matching '%s' found in config — nothing removed",
            path_or_alias,
        )
    else:
        save_config(config, config_path)

    return config


# ---------------------------------------------------------------------------
# PID file management
# ---------------------------------------------------------------------------


def write_pid(pid: int | None = None, path: Path | None = None) -> None:
    """Write the current (or given) PID to the PID file."""
    pid_path = path or PID_PATH
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(pid or os.getpid()), encoding="utf-8")


def read_pid(path: Path | None = None) -> int | None:
    """Read the daemon PID from disk. Returns None if missing/invalid."""
    pid_path = path or PID_PATH
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def clear_pid(path: Path | None = None) -> None:
    """Remove the PID file."""
    pid_path = path or PID_PATH
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        pass


def is_daemon_running(path: Path | None = None) -> bool:
    """Check whether a daemon process is alive."""
    pid = read_pid(path)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)  # signal 0 = existence check
        return True
    except ProcessLookupError:
        # Stale PID file — clean up
        clear_pid(path)
        return False
    except PermissionError:
        return True  # process exists but owned by another user


# ---------------------------------------------------------------------------
# Child state persistence (for cross-process status queries)
# ---------------------------------------------------------------------------


def load_state(path: Path | None = None) -> dict[str, Any]:
    """Load persisted child process state from disk.

    Returns a dict mapping alias to ``{"pid": int, "path": str}``.
    Returns an empty dict if the file is missing or corrupt.
    """
    state_path = path or STATE_PATH
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        return {}


def _is_pid_alive(pid: int) -> bool:
    """Check whether a process with the given PID is running."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user


# ---------------------------------------------------------------------------
# ConfigWatcher — monitors config file for live changes
# ---------------------------------------------------------------------------


class ConfigWatcher:
    """Watches the daemon config file for changes and triggers reconciliation."""

    def __init__(
        self,
        config_path: Path,
        callback: Callable[[], None],
        poll_interval: int = 2,
    ) -> None:
        self._config_path = config_path
        self._callback = callback
        self._poll_interval = poll_interval
        self._observer: Any = None  # watchdog Observer when available
        self._last_mtime: float = 0.0
        self._poll_thread: threading.Thread | None = None
        self._stop_event: threading.Event = threading.Event()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Begin watching the config file for modifications."""
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            watcher = self

            class _Handler(FileSystemEventHandler):  # type: ignore[misc]
                def on_modified(self, event: Any) -> None:
                    if Path(event.src_path).resolve() == watcher._config_path.resolve():
                        watcher._on_config_changed()

            handler = _Handler()
            self._observer = Observer()
            self._observer.schedule(
                handler,
                str(self._config_path.parent),
                recursive=False,
            )
            self._observer.daemon = True
            self._observer.start()
            logger.info(
                "Config watcher started (watchdog) for %s",
                self._config_path,
            )
        except ImportError:
            # Fallback to polling when watchdog is unavailable
            logger.info(
                "watchdog not available — falling back to polling for %s",
                self._config_path,
            )
            self._start_polling()

    def stop(self) -> None:
        """Stop watching the config file."""
        self._stop_event.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=5)
            self._poll_thread = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _start_polling(self) -> None:
        """Poll the config file mtime in a background thread."""
        if self._config_path.exists():
            self._last_mtime = self._config_path.stat().st_mtime

        def _poll() -> None:
            while not self._stop_event.is_set():
                self._stop_event.wait(self._poll_interval)
                if self._stop_event.is_set():
                    break
                try:
                    if not self._config_path.exists():
                        continue
                    mtime = self._config_path.stat().st_mtime
                    if mtime != self._last_mtime:
                        self._last_mtime = mtime
                        self._on_config_changed()
                except OSError:
                    pass

        self._poll_thread = threading.Thread(
            target=_poll,
            daemon=True,
            name="config-poller",
        )
        self._poll_thread.start()

    def _on_config_changed(self) -> None:
        """Handle a detected config file modification."""
        logger.info("Config file changed, triggering reconciliation")
        try:
            self._callback()
        except Exception:
            logger.exception("Error during config-change reconciliation")


# ---------------------------------------------------------------------------
# WatchDaemon — manages child processes for multi-repo watching
# ---------------------------------------------------------------------------


class WatchDaemon:
    """Manages child processes for multi-repo file watching.

    Each watched repository gets a ``code-review-graph watch`` child process
    managed via :mod:`subprocess`.  No external tools (tmux, screen, etc.)
    are required.
    """

    def __init__(
        self,
        config: DaemonConfig | None = None,
        config_path: Path | None = None,
    ) -> None:
        self._config: DaemonConfig = config or load_config(config_path)
        self._config_path: Path = config_path or CONFIG_PATH
        self._state_path: Path = STATE_PATH
        self._children: dict[str, subprocess.Popen[bytes]] = {}
        self._current_repos: dict[str, WatchRepo] = {}
        self._config_watcher: ConfigWatcher | None = None
        self._health_thread: threading.Thread | None = None
        self._health_stop: threading.Event = threading.Event()
        self._lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn a watcher child process for each configured repo."""
        logger.info("Starting daemon '%s'", self._config.session_name)

        # Auto-register repos in the central registry
        from .registry import Registry

        registry = Registry()
        for repo in self._config.repos:
            registry.register(repo.path, alias=repo.alias)

        # Build initial graph for repos that lack a database
        for repo in self._config.repos:
            db_path = Path(repo.path) / ".code-review-graph" / "graph.db"
            if not db_path.exists():
                self._initial_build(repo)

        # Spawn a watcher child for every repo
        for repo in self._config.repos:
            self._start_watcher(repo)

        # Track current state
        self._current_repos = {r.alias: r for r in self._config.repos}

        # Persist child PIDs to disk for cross-process status queries
        self._save_state()

        # Start watching the config file for live changes
        self.start_config_watcher()

        # Start health checker to auto-restart dead watchers
        self.start_health_checker()

        msg = f"Daemon started — watching {len(self._config.repos)} repo(s)"
        logger.info(msg)
        print(msg)  # noqa: T201

    def stop(self) -> None:
        """Tear down the daemon: stop watchers, terminate children."""
        self.stop_config_watcher()
        self.stop_health_checker()

        with self._lock:
            for alias, proc in list(self._children.items()):
                self._terminate_child(alias, proc)
            self._children.clear()

        self._current_repos.clear()
        self._clear_state()
        clear_pid()
        logger.info("Daemon stopped")

    def reconcile(self, new_config: DaemonConfig | None = None) -> None:
        """Reconcile running watchers with the (possibly updated) config.

        Child processes are started, stopped, or restarted to match the
        desired state.  New repos are registered in the central registry
        and their graphs are built automatically (mirroring ``start()``).
        """
        if new_config is not None:
            self._config = new_config

        desired: dict[str, WatchRepo] = {r.alias: r for r in self._config.repos}
        current: set[str] = set(self._current_repos.keys())

        to_add: set[str] = desired.keys() - current
        to_remove: set[str] = current - desired.keys()
        to_update: set[str] = {
            alias
            for alias in desired.keys() & current
            if desired[alias].path != self._current_repos[alias].path
        }

        # Register new/updated repos and build graphs *before* acquiring
        # the lock so that long-running builds don't block health checks.
        if to_add or to_update:
            from .registry import Registry

            registry = Registry()

            repos_needing_build: list[WatchRepo] = []
            for alias in to_add | to_update:
                repo = desired[alias]
                registry.register(repo.path, alias=repo.alias)
                db_path = Path(repo.path) / ".code-review-graph" / "graph.db"
                if not db_path.exists():
                    repos_needing_build.append(repo)

            for repo in repos_needing_build:
                self._initial_build(repo)

        with self._lock:
            # Remove stale watchers
            for alias in to_remove:
                proc = self._children.pop(alias, None)
                if proc is not None:
                    self._terminate_child(alias, proc)
                del self._current_repos[alias]

            # Add new watchers
            for alias in to_add:
                repo = desired[alias]
                self._start_watcher(repo)
                self._current_repos[alias] = repo

            # Update changed watchers (path changed for same alias)
            for alias in to_update:
                proc = self._children.pop(alias, None)
                if proc is not None:
                    self._terminate_child(alias, proc)
                repo = desired[alias]
                self._start_watcher(repo)
                self._current_repos[alias] = repo

        # Persist updated state
        self._save_state()

        logger.info(
            "Reconcile complete — added: %d, removed: %d, updated: %d",
            len(to_add),
            len(to_remove),
            len(to_update),
        )

    def status(self) -> dict[str, Any]:
        """Return a summary of daemon state.

        When called from the daemon process itself, uses the in-memory
        ``_children`` dict.  When called from a separate process (e.g. the
        CLI ``status`` command), falls back to the persisted state file and
        checks liveness via ``os.kill(pid, 0)``.
        """
        repos: list[dict[str, Any]] = []
        with self._lock:
            if self._children:
                # In-process: we have live Popen handles
                for alias, repo in self._current_repos.items():
                    proc = self._children.get(alias)
                    alive = proc is not None and proc.poll() is None
                    repos.append(
                        {
                            "alias": alias,
                            "path": repo.path,
                            "alive": alive,
                            "pid": proc.pid if proc is not None else None,
                        }
                    )
            else:
                # Cross-process: read persisted state from disk
                state = load_state(self._state_path)
                for repo in self._config.repos:
                    entry = state.get(repo.alias, {})
                    pid: int | None = entry.get("pid")
                    alive = pid is not None and _is_pid_alive(pid)
                    repos.append(
                        {
                            "alias": repo.alias,
                            "path": repo.path,
                            "alive": alive,
                            "pid": pid,
                        }
                    )
        return {
            "session_name": self._config.session_name,
            "running": True,
            "repos": repos,
        }

    # ------------------------------------------------------------------
    # Config watching
    # ------------------------------------------------------------------

    def start_config_watcher(self) -> None:
        """Begin watching the config file for live edits."""
        self._config_watcher = ConfigWatcher(
            config_path=self._config_path,
            callback=self._on_config_change,
            poll_interval=self._config.poll_interval,
        )
        self._config_watcher.start()

    def _on_config_change(self) -> None:
        """Reload configuration and reconcile running watchers."""
        try:
            new_config = load_config(self._config_path)
        except Exception:
            logger.warning(
                "Failed to parse config file — keeping last good config",
                exc_info=True,
            )
            return
        self.reconcile(new_config)

    def stop_config_watcher(self) -> None:
        """Stop the config file watcher if running."""
        if self._config_watcher is not None:
            self._config_watcher.stop()
            self._config_watcher = None

    # ------------------------------------------------------------------
    # Health checking
    # ------------------------------------------------------------------

    def start_health_checker(self) -> None:
        """Start the background health-check thread."""
        self._health_stop = threading.Event()
        self._health_thread = threading.Thread(
            target=self._health_loop,
            daemon=True,
            name="health-checker",
        )
        self._health_thread.start()
        logger.info(
            "Health checker started (interval=%ds)",
            _HEALTH_CHECK_INTERVAL,
        )

    def stop_health_checker(self) -> None:
        """Stop the health-check thread."""
        if hasattr(self, "_health_stop"):
            self._health_stop.set()
        if hasattr(self, "_health_thread") and self._health_thread is not None:
            self._health_thread.join(timeout=5)
            self._health_thread = None

    def _health_loop(self) -> None:
        """Periodically check child processes and restart dead ones."""
        while not self._health_stop.is_set():
            self._health_stop.wait(_HEALTH_CHECK_INTERVAL)
            if self._health_stop.is_set():
                break
            self._check_health()

    def _check_health(self) -> None:
        """Check each watcher child and restart if dead."""
        restarted = False
        with self._lock:
            for alias, repo in list(self._current_repos.items()):
                proc = self._children.get(alias)
                if proc is None or proc.poll() is not None:
                    logger.warning("Watcher for '%s' is dead — restarting", alias)
                    # Clean up dead process entry
                    self._children.pop(alias, None)
                    self._start_watcher(repo)
                    restarted = True
        if restarted:
            self._save_state()

    # ------------------------------------------------------------------
    # Daemonization
    # ------------------------------------------------------------------

    def daemonize(self) -> None:
        """Fork to background using the double-fork pattern.

        Redirects stdout/stderr to the daemon log file.  Writes PID file.
        Sets up SIGTERM handler for graceful shutdown.

        On Windows, forking is not supported — the daemon runs in the
        foreground and a warning is logged.
        """
        if sys.platform == "win32":
            logger.warning("Forking is not supported on Windows — running in foreground")
            write_pid()
            self._setup_signal_handlers()
            return

        # First fork
        pid = os.fork()
        if pid > 0:
            # Parent exits
            sys.exit(0)

        # Become session leader
        os.setsid()

        # Second fork (prevent acquiring a controlling terminal)
        pid = os.fork()
        if pid > 0:
            sys.exit(0)

        # Redirect file descriptors
        sys.stdout.flush()
        sys.stderr.flush()

        self._config.log_dir.mkdir(parents=True, exist_ok=True)
        log_file = self._config.log_dir / "daemon.log"

        # Open log file for stdout/stderr
        fd = os.open(
            str(log_file),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o644,
        )
        os.dup2(fd, sys.stdout.fileno())
        os.dup2(fd, sys.stderr.fileno())

        # Redirect stdin from /dev/null
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, sys.stdin.fileno())
        os.close(devnull)
        if fd > 2:
            os.close(fd)

        # Write PID file
        write_pid()

        # Set up signal handlers
        self._setup_signal_handlers()

        logger.info("Daemonized (PID %d)", os.getpid())

    def _setup_signal_handlers(self) -> None:
        """Install SIGTERM/SIGHUP handlers for graceful shutdown."""

        def _handle_sigterm(signum: int, frame: Any) -> None:
            logger.info("Received signal %d — shutting down", signum)
            self.stop()
            sys.exit(0)

        signal.signal(signal.SIGTERM, _handle_sigterm)
        if sys.platform != "win32":
            signal.signal(signal.SIGHUP, _handle_sigterm)

    def run_forever(self) -> None:
        """Block forever, keeping the daemon alive.

        The config watcher and health checker run in background threads.
        This method sleeps in the main thread until interrupted.
        """
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt — stopping daemon")
            self.stop()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _save_state(self) -> None:
        """Persist child PIDs and repo paths to disk for cross-process queries.

        Called after any mutation of ``_children`` so that ``status`` commands
        running in a separate process can determine which watchers are alive.
        """
        state: dict[str, dict[str, Any]] = {}
        for alias, proc in self._children.items():
            repo = self._current_repos.get(alias)
            state[alias] = {
                "pid": proc.pid,
                "path": repo.path if repo else "",
            }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(state), encoding="utf-8")
        except OSError:
            logger.warning("Failed to persist daemon state to %s", self._state_path)

    def _clear_state(self) -> None:
        """Remove the state file from disk."""
        try:
            self._state_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _start_watcher(self, repo: WatchRepo) -> None:
        """Spawn a child process running ``code-review-graph watch`` for *repo*."""
        self._config.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._config.log_dir / f"{repo.alias}.log"

        crg_bin = shutil.which("code-review-graph")
        if crg_bin:
            cmd: list[str] = [crg_bin, "watch", "--repo", repo.path]
        else:
            cmd = [
                sys.executable,
                "-m",
                "code_review_graph",
                "watch",
                "--repo",
                repo.path,
            ]

        log_fd = open(log_path, "ab")  # noqa: SIM115
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=repo.path,
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
        except Exception:
            log_fd.close()
            logger.exception("Failed to start watcher for '%s'", repo.alias)
            return

        # The log fd is inherited by the child; we can close our copy.
        # The child keeps the fd open via its own reference.
        log_fd.close()

        self._children[repo.alias] = proc
        logger.info(
            "Started watcher for '%s' (PID %d) — log: %s",
            repo.alias,
            proc.pid,
            log_path,
        )

    @staticmethod
    def _terminate_child(alias: str, proc: subprocess.Popen[bytes]) -> None:
        """Gracefully terminate a child process (SIGTERM, then SIGKILL)."""
        if proc.poll() is not None:
            return  # already dead

        logger.info("Terminating watcher '%s' (PID %d)", alias, proc.pid)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("Watcher '%s' did not stop — sending SIGKILL", alias)
            proc.kill()
            proc.wait(timeout=5)

    def _initial_build(self, repo: WatchRepo) -> None:
        """Run a one-off graph build for a repo that has no database yet."""
        logger.info("Building initial graph for %s...", repo.alias)

        crg_bin = shutil.which("code-review-graph")
        if crg_bin:
            cmd: list[str] = [crg_bin, "build", "--repo", repo.path]
        else:
            cmd = [
                sys.executable,
                "-m",
                "code_review_graph",
                "build",
                "--repo",
                repo.path,
            ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "Initial build for '%s' failed (rc=%d): %s",
                repo.alias,
                result.returncode,
                result.stderr.strip(),
            )
