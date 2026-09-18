"""Multi-repo watch daemon for code-review-graph.

Reads ``~/.code-review-graph/watch.toml`` to configure which repositories
to watch, then spawns one ``code-review-graph watch`` child process per
repo.  Monitors the config file for live changes (adding/removing repos)
and health-checks child processes, restarting any that die.

No external dependencies beyond Python stdlib — no tmux required.
"""

from __future__ import annotations

import hashlib
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

from .constants import crg_home, env_float

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config file location
# ---------------------------------------------------------------------------

def default_config_path() -> Path:
    """Path to ``watch.toml`` under the per-user state directory."""
    return crg_home() / "watch.toml"


def default_pid_path() -> Path:
    """Path to the daemon PID file."""
    return crg_home() / "daemon.pid"


def default_state_path() -> Path:
    """Path to the persisted daemon state."""
    return crg_home() / "daemon-state.json"


def default_log_dir() -> Path:
    """Directory for per-repo daemon logs."""
    return crg_home() / "logs"


# These four were module-level constants built from Path.home(). They resolve
# per call now so $CRG_HOME can redirect them: an import-time constant is
# frozen before any caller — a test fixture, a sandboxed run — gets the chance
# to set the variable, which is how the test suite ended up writing into the
# real home directory of whoever ran it.
#
# The PEP 562 shim below keeps the old attribute names working for anything
# that already imported them: both ``daemon.CONFIG_PATH`` and
# ``from …daemon import CONFIG_PATH`` route through ``__getattr__``, and
# ``__dir__`` keeps them visible to introspection. Not covered: ``import *``
# (this module defines no ``__all__``, and adding one would change what the
# star exports for every other name) and static analysers, which cannot see
# dynamic attributes. Both are acceptable — these were never public API, and
# the alternative is deleting the names outright.
_LAZY_PATHS = {
    "CONFIG_PATH": default_config_path,
    "PID_PATH": default_pid_path,
    "STATE_PATH": default_state_path,
}


def __getattr__(name: str) -> Path:
    if name in _LAZY_PATHS:
        return _LAZY_PATHS[name]()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy names so ``dir()`` and tab-completion still find them.

    ``__getattr__`` alone covers attribute access and ``from … import X``,
    but names absent from module globals are otherwise invisible to
    ``dir()``, ``from … import *`` and static analysers.
    """
    return sorted(set(globals()) | set(_LAZY_PATHS))


_HEALTH_CHECK_INTERVAL = 30

# Restarting a watcher costs a full initial update, so a repo that cannot stay
# up must not be restarted every health check forever.  The delay doubles per
# consecutive failure and resets once a watcher has stayed up this long.
_RESTART_BACKOFF_BASE = env_float("CRG_RESTART_BACKOFF", 30.0)
_RESTART_BACKOFF_MAX = env_float("CRG_RESTART_BACKOFF_MAX", 900.0)
_RESTART_HEALTHY_SECONDS = env_float("CRG_RESTART_HEALTHY_AFTER", 600.0)

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

    log_dir: Path = field(default_factory=default_log_dir)
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
        path: Explicit config path.  Falls back to :func:`default_config_path`.

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

    config_path = path or default_config_path()

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

        has_repo_marker = (
            (repo_path / ".git").exists()
            or (repo_path / ".svn").exists()
            or (repo_path / ".code-review-graph").exists()
        )
        if not has_repo_marker:
            logger.warning(
                "Skipping repo %s — no .git, .svn, or .code-review-graph directory found",
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


# TOML basic strings have named escapes for these; everything else in
# the control range must use the \uXXXX form.
_TOML_SHORT_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _toml_str(value: object) -> str:
    """Render *value* as a TOML basic string.

    Backslashes and double quotes are escape characters in TOML basic
    strings, so Windows paths like ``C:\\Users\\x`` must be escaped or
    the file fails to parse on the next load. Control characters
    (U+0000-U+001F, U+007F) are forbidden unescaped by the TOML spec,
    so they are escaped too — ``tomllib`` rejects the file otherwise.
    """
    chars: list[str] = []
    for ch in str(value):
        esc = _TOML_SHORT_ESCAPES.get(ch)
        if esc is not None:
            chars.append(esc)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            chars.append(f"\\u{ord(ch):04X}")
        else:
            chars.append(ch)
    return '"' + "".join(chars) + '"'


def _serialize_toml(config: DaemonConfig) -> str:
    """Serialize a :class:`DaemonConfig` to TOML text.

    ``tomllib`` is read-only, so we build the TOML manually.
    """
    lines: list[str] = [
        "[daemon]",
        f"session_name = {_toml_str(config.session_name)}",
        f"log_dir = {_toml_str(config.log_dir)}",
        f"poll_interval = {config.poll_interval}",
    ]
    for repo in config.repos:
        lines.append("")
        lines.append("[[repos]]")
        lines.append(f"path = {_toml_str(repo.path)}")
        lines.append(f"alias = {_toml_str(repo.alias)}")
    lines.append("")  # trailing newline
    return "\n".join(lines)


def save_config(config: DaemonConfig, path: Path | None = None) -> None:
    """Write *config* back to a TOML file.

    Creates parent directories if they do not exist.

    Args:
        config: The daemon configuration to persist.
        path:   Explicit config path.  Falls back to :func:`default_config_path`.
    """
    config_path = path or default_config_path()
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
        config_path: Explicit config file path.  Falls back to :func:`default_config_path`.

    Returns:
        The updated :class:`DaemonConfig`.

    Raises:
        ValueError: If the path is not a valid repository directory.
    """
    resolved = Path(repo_path).expanduser().resolve()

    if not resolved.is_dir():
        raise ValueError(f"Not a directory: {resolved}")

    has_repo_marker = (
        (resolved / ".git").exists()
        or (resolved / ".svn").exists()
        or (resolved / ".code-review-graph").exists()
    )
    if not has_repo_marker:
        raise ValueError(f"No .git, .svn, or .code-review-graph directory in {resolved}")

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
        config_path:   Explicit config file path.  Falls back to :func:`default_config_path`.

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


# A PID on its own is not an identity.  PIDs are recycled, so a stale
# ``daemon.pid`` naming a number the kernel has since handed to something else
# made ``start`` refuse to run and ``stop`` SIGTERM then SIGKILL a stranger.
#
# The identity is an exclusive advisory lock the daemon holds for its whole
# lifetime.  The kernel releases it the instant the process dies — including a
# SIGKILL or an OOM kill, where no cleanup code of ours ever runs — so "the
# lock is free" is proof that our daemon is gone, whatever the PID file says.
# ``flock`` is used rather than ``lockf`` deliberately: flock locks belong to
# the open file description, so probing with a second descriptor cannot
# silently drop a lock this process already holds.
_lock_handles: dict[str, Any] = {}


class DaemonAlreadyRunningError(RuntimeError):
    """A live process already holds the daemon lock, so this one must not run.

    ``is_daemon_running`` is a look, not a claim, and two ``crg-daemon start``
    invocations a millisecond apart both used to pass it.  The lock is the only
    thing that can decide between them, so refusing to start is expressed as an
    exception out of the call that takes it rather than a boolean a caller can
    ignore.
    """

    def __init__(self, pid: int | None, lock_path: Path) -> None:
        self.pid = pid
        self.lock_path = lock_path
        holder = f"PID {pid}" if pid is not None else "an unrecorded PID"
        super().__init__(
            f"another code-review-graph daemon ({holder}) holds {lock_path}"
        )


def daemon_lock_path(path: Path | None = None) -> Path:
    """Companion lock file for a PID file.

    Kept separate so ``daemon.pid`` stays exactly what every other tool
    expects it to be: one integer and nothing else.
    """
    return (path or default_pid_path()).with_suffix(".lock")


def _flock_supported() -> bool:
    return sys.platform != "win32"


def _flock(handle: Any, blocking: bool = False) -> bool:
    """Take an exclusive lock on *handle*. Returns False when it is held."""
    import fcntl

    flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
    try:
        fcntl.flock(handle.fileno(), flags)
    except OSError:
        return False
    return True


def acquire_daemon_lock(path: Path | None = None) -> bool:
    """Claim the daemon lock for this process, and hold it until it exits.

    Returns True when this process owns the lock afterwards (including when it
    already did), False when another live process holds it.  On platforms
    without ``flock`` this is a no-op that reports success, and identity falls
    back to the process command line.
    """
    if not _flock_supported():
        return True
    lock_path = daemon_lock_path(path)
    key = str(lock_path)
    if key in _lock_handles:
        return True
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")  # noqa: SIM115 - held for the process lifetime
    if not _flock(handle):
        handle.close()
        return False
    _lock_handles[key] = handle
    return True


def claim_daemon_lock(path: Path | None = None) -> None:
    """Take the daemon lock, or refuse and name the process that holds it.

    Call this before doing anything a second daemon must not do — forking,
    spawning watchers, writing the PID file.  Under ``flock`` the lock belongs
    to the open file description, so a daemon that forks after claiming keeps
    holding it once the parents exit.

    On Windows ``acquire_daemon_lock`` is a no-op that reports success, so this
    never refuses there and two simultaneous starts are still possible.
    """
    if not acquire_daemon_lock(path):
        raise DaemonAlreadyRunningError(read_pid(path), daemon_lock_path(path))


def release_daemon_lock(path: Path | None = None) -> None:
    """Drop the daemon lock this process holds, if any."""
    handle = _lock_handles.pop(str(daemon_lock_path(path)), None)
    if handle is not None:
        try:
            handle.close()  # closing the description releases the flock
        except OSError:  # pragma: no cover - best-effort cleanup
            pass


def daemon_lock_held(path: Path | None = None) -> bool:
    """True when some live process holds the daemon lock."""
    if not _flock_supported():  # pragma: no cover - POSIX in CI
        return True
    key = str(daemon_lock_path(path))
    if key in _lock_handles:
        return True  # this process is the daemon
    lock_path = daemon_lock_path(path)
    if not lock_path.exists():
        return False
    try:
        probe = open(lock_path, "a+")  # noqa: SIM115 - closed below
    except OSError:  # pragma: no cover - unreadable state dir
        return False
    try:
        if _flock(probe):
            return False  # nobody held it; our probe did, and now releases it
        return True
    finally:
        probe.close()


def _process_command(pid: int) -> str | None:
    """The command line of *pid*, or None when it cannot be determined.

    Windows returns None, and ``_looks_like_our_daemon`` reads that as "keep
    the old behaviour", so none of the PID-identity work below reaches a
    Windows user: there is no ``flock`` to fall back from either, so a PID
    file naming any live process is still adopted there. What that costs is
    written out in :func:`_looks_like_our_daemon`. Closing it needs a real
    command line, which Windows exposes only through WMI or
    ``NtQueryInformationProcess`` — neither is a change to make blind, with no
    Windows machine to test it on.
    """
    if sys.platform == "win32":  # pragma: no cover - POSIX in CI
        return None
    try:
        result = subprocess.run(
            # -ww: never truncate, so a long repository path still matches.
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no ps
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _looks_like_our_daemon(pid: int) -> bool:
    """Fallback identity check for platforms without ``flock``.

    On Windows this always returns True, because ``_process_command`` cannot
    answer there. A Windows user therefore keeps the pre-existing behaviour in
    full: ``crg-daemon status`` reports a daemon whenever ``daemon.pid`` names
    a live process, and ``crg-daemon stop`` signals it, even when the PID was
    recycled and now belongs to something unrelated. For the same reason
    ``claim_daemon_lock`` cannot exclude a second daemon on Windows — see
    :func:`acquire_daemon_lock`.
    """
    command = _process_command(pid)
    if command is None:  # pragma: no cover - cannot tell; keep old behaviour
        return True
    return "crg-daemon" in command or "code_review_graph.daemon" in command


def write_pid(pid: int | None = None, path: Path | None = None) -> None:
    """Claim the daemon lock, then write the current (or given) PID.

    The lock is the exclusion; the PID file is only a label on it.  Discarding
    the result of :func:`acquire_daemon_lock` here meant the new lock could
    identify the daemon but never exclude a second one: two ``crg-daemon
    start`` invocations that both raced past ``is_daemon_running`` each wrote
    the PID file and each went on running, one of them invisible to every
    later ``status`` and ``stop``.

    Raises:
        DaemonAlreadyRunningError: another live process holds the lock. The PID
            file is left exactly as its owner wrote it.
    """
    pid_path = path or default_pid_path()
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    claim_daemon_lock(path)
    pid_path.write_text(str(pid or os.getpid()), encoding="utf-8")


def read_pid(path: Path | None = None) -> int | None:
    """Read the daemon PID from disk. Returns None if missing/invalid."""
    pid_path = path or default_pid_path()
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def clear_pid(path: Path | None = None) -> None:
    """Remove the PID file and release the daemon lock."""
    pid_path = path or default_pid_path()
    release_daemon_lock(path)
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        pass


# Win32 constants for the OpenProcess-based liveness check (#511).
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_ERROR_ACCESS_DENIED = 5
_WAIT_OBJECT_0 = 0x0
_WAIT_FAILED = 0xFFFFFFFF


def _pid_alive_windows(
    pid: int,
    kernel32: Any,
    get_last_error: Callable[[], int] | None = None,
) -> bool:
    """Win32 PID liveness check via OpenProcess/WaitForSingleObject.

    The access mask must include SYNCHRONIZE: a handle opened with only
    PROCESS_QUERY_LIMITED_INFORMATION cannot be waited on, so
    WaitForSingleObject returns WAIT_FAILED (ERROR_ACCESS_DENIED) and
    every exited process reads as alive.

    The kernel32 interface is injected so tests can drive handle/wait
    outcomes on non-Windows platforms. *get_last_error* defaults to
    ``kernel32.GetLastError``; the real caller passes
    ``ctypes.get_last_error`` (reliable with ``use_last_error=True``).
    """
    if get_last_error is None:
        get_last_error = kernel32.GetLastError
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid)
    if not handle:
        # NULL handle: process is dead, or we lack access. ACCESS_DENIED
        # means it exists but is owned by another user — treat as alive.
        return get_last_error() == _ERROR_ACCESS_DENIED
    try:
        result = kernel32.WaitForSingleObject(handle, 0)
        if result == _WAIT_FAILED:
            # The wait itself errored — we cannot prove the process dead,
            # so err alive, consistent with the ACCESS_DENIED branch.
            logger.debug(
                "WaitForSingleObject on PID %d failed (error %d); presuming alive",
                pid,
                get_last_error(),
            )
            return True
        # WAIT_OBJECT_0 means the process handle is signaled (it exited).
        return result != _WAIT_OBJECT_0
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """Cross-platform check whether a process with *pid* is running.

    On Windows ``os.kill(pid, 0)`` routes to GenerateConsoleCtrlEvent and
    raises ``OSError`` (WinError 87) for alive PIDs outside the caller's
    console process group (#511), so the Win32 API is used instead.
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Explicit prototypes: ctypes otherwise defaults every argument and
        # return value to c_int, which truncates 64-bit HANDLEs and returns
        # WAIT_FAILED as -1 — never equal to the unsigned 0xFFFFFFFF constant.
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        return _pid_alive_windows(pid, kernel32, ctypes.get_last_error)
    try:
        os.kill(pid, 0)  # signal 0 = existence check
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists but owned by another user
    except OSError as exc:
        # Unexpected platform quirk — treat as not alive rather than crash.
        logger.debug("PID %d liveness check failed: %s", pid, exc)
        return False


def is_daemon_running(path: Path | None = None) -> bool:
    """Check whether *our* daemon process is alive.

    A live PID is necessary but not sufficient: PIDs are recycled, and the
    file outlives the process that wrote it.  The daemon holds an exclusive
    lock for its whole lifetime, so a PID file naming a live process that does
    not hold that lock names a stranger, and is cleared rather than adopted.
    Without it, ``stop`` signalled that stranger.
    """
    pid = read_pid(path)
    if pid is None:
        return False
    if not pid_alive(pid):
        clear_pid(path)  # stale PID file — clean up
        return False
    identified = daemon_lock_held(path) if _flock_supported() else _looks_like_our_daemon(pid)
    if identified:
        return True
    logger.info(
        "PID file names live process %d, which is not this daemon "
        "(it holds no daemon lock); treating the file as stale",
        pid,
    )
    clear_pid(path)
    return False


# ---------------------------------------------------------------------------
# Child state persistence (for cross-process status queries)
# ---------------------------------------------------------------------------


def load_state(path: Path | None = None) -> dict[str, Any]:
    """Load persisted child process state from disk.

    Returns a dict mapping alias to ``{"pid": int, "path": str}``.
    Returns an empty dict if the file is missing or corrupt.
    """
    state_path = path or default_state_path()
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        return {}


def _is_pid_alive(pid: int) -> bool:
    """Check whether a process with the given PID is running."""
    return pid_alive(pid)


def clear_state(path: Path | None = None) -> None:
    """Remove the persisted child-state file."""
    state_path = path or default_state_path()
    try:
        state_path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - best-effort cleanup
        pass


def is_our_watcher(pid: int, repo_path: str) -> bool:
    """Is *pid* really a ``code-review-graph watch`` child for *repo_path*?

    Same reasoning as the daemon PID file: signalling a number recorded on
    disk is only safe once something ties that number to the process we mean.
    The command line is that tie, and it is checked first — a health file
    naming a PID proves only that some watcher once had it, and PIDs are
    recycled, so trusting it alone could both block a legitimate start and
    send SIGKILL to a stranger.  The health file is the fallback for platforms
    with no ``ps``, and there only while its heartbeat is still fresh.
    """
    command = _process_command(pid)
    if command is not None:
        return (
            "watch" in command
            and ("code-review-graph" in command or "code_review_graph" in command)
            and repo_path in command
        )
    health = read_watch_health(repo_path)  # pragma: no cover - POSIX in CI
    return (
        isinstance(health, dict)
        and health.get("pid") == pid
        and not health.get("stalled")
    )


def find_orphaned_watchers(
    state_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Watcher children still running with no daemon left to manage them.

    The watchers are plain ``subprocess.Popen`` children in the daemon's
    session, so a SIGKILLed daemon leaves every one of them running.  Nothing
    used to look for them: ``status`` reported "not running" and listed
    nothing while a live watcher kept writing ``graph.db``, and the next
    ``start`` added a second watcher per repository — two writers on one
    database, one of them invisible.
    """
    orphans: list[dict[str, Any]] = []
    for alias, entry in load_state(state_path).items():
        if not isinstance(entry, dict):
            continue
        pid = entry.get("pid")
        repo_path = str(entry.get("path") or "")
        if not isinstance(pid, int) or not repo_path:
            continue
        if not _is_pid_alive(pid):
            continue
        if not is_our_watcher(pid, repo_path):
            logger.info(
                "PID %d recorded for '%s' is no longer our watcher; leaving it alone",
                pid,
                alias,
            )
            continue
        orphans.append({"alias": alias, "pid": pid, "path": repo_path})
    return orphans


def reap_orphaned_watchers(
    state_path: Path | None = None,
    *,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """Terminate every orphaned watcher and forget the state that named them.

    Returns the entries that were signalled, so the caller can report them.
    """
    orphans = find_orphaned_watchers(state_path)
    for orphan in orphans:
        pid = int(orphan["pid"])
        logger.info(
            "Reaping orphaned watcher for '%s' (PID %d)", orphan["alias"], pid
        )
        clear_watch_health(orphan["path"])
        _signal_until_dead(pid, timeout=timeout)
    if orphans:
        clear_state(state_path)
    return orphans


def _signal_until_dead(pid: int, *, timeout: float = 5.0) -> bool:
    """SIGTERM, then SIGKILL, then confirm. Returns True when the PID is gone."""
    for sig in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return True
        except PermissionError:  # pragma: no cover - another user's process
            logger.warning("Not permitted to signal PID %d", pid)
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _is_pid_alive(pid):
                return True
            time.sleep(0.1)
    return not _is_pid_alive(pid)


# ---------------------------------------------------------------------------
# Watcher health (written by each watch child, read here)
# ---------------------------------------------------------------------------

# A watcher whose observer thread died keeps its process alive, so ``poll()``
# alone reports it healthy forever.  Each watch child publishes its observer
# state and last-event time here instead; anything older than this is a stall.
# See: #811.
_WATCH_HEALTH_STALE_SECONDS = env_float("CRG_WATCH_HEALTH_STALE", 90.0)


def watch_health_dir() -> Path:
    """Directory holding one health file per watched repository."""
    return crg_home() / "watch-health"


def watch_health_path(repo_root: str | Path) -> Path:
    """Health file for *repo_root*.

    Keyed by the real path so the watch child and the daemon agree even when
    one of them was handed a symlinked or relative path.  Deliberately free of
    heavy imports: ``crg-daemon status`` must stay instant.
    """
    key = os.path.realpath(os.path.expanduser(str(repo_root)))
    digest = hashlib.sha256(key.encode("utf-8", "surrogateescape")).hexdigest()[:16]
    return watch_health_dir() / f"{digest}.json"


def read_watch_health(
    repo_root: str | Path,
    *,
    stale_after: float = _WATCH_HEALTH_STALE_SECONDS,
) -> dict[str, Any] | None:
    """Read a watcher's published health, or None when it never published any.

    The returned dict gains ``age`` (seconds since the last heartbeat) and
    ``stalled`` (observer reported dead, or heartbeat too old).
    """
    try:
        raw = watch_health_path(repo_root).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    updated_at = data.get("updated_at")
    age = time.time() - updated_at if isinstance(updated_at, (int, float)) else None
    data["age"] = age
    data["stalled"] = bool(
        data.get("observer_alive") is False or (age is not None and age > stale_after)
    )
    return data


def clear_watch_health(repo_root: str | Path) -> None:
    """Drop a watcher's health file (the daemon reaped or dropped the child)."""
    try:
        watch_health_path(repo_root).unlink(missing_ok=True)
    except OSError:  # pragma: no cover - best-effort cleanup
        pass


def watcher_status(alive: bool, health: dict[str, Any] | None) -> str:
    """Summarise one watcher: ``dead``, ``stalled``, ``partial``, ``unknown``, ``ok``.

    ``partial`` means the watcher ran out of watch budget and fell back to a
    coarser recursive watch — still watching everything, but no longer
    filtering ignored trees.
    """
    if not alive:
        return "dead"
    if health is None:
        return "unknown"
    if health.get("stalled"):
        return "stalled"
    return "partial" if health.get("degraded") else "ok"


def _health_fields(repo_path: str, alive: bool) -> dict[str, Any]:
    """Watcher-health columns for one repo entry in :meth:`WatchDaemon.status`."""
    health = read_watch_health(repo_path)
    return {
        "watcher": watcher_status(alive, health),
        "observer_alive": None if health is None else health.get("observer_alive"),
        "last_event_at": None if health is None else health.get("last_event_at"),
        "health_age": None if health is None else health.get("age"),
        "degraded": None if health is None else bool(health.get("degraded")),
        "phase": None if health is None else health.get("phase"),
    }


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
        self._config_path: Path = config_path or default_config_path()
        self._state_path: Path = default_state_path()
        self._children: dict[str, subprocess.Popen[bytes]] = {}
        self._current_repos: dict[str, WatchRepo] = {}
        self._config_watcher: ConfigWatcher | None = None
        self._health_thread: threading.Thread | None = None
        self._health_stop: threading.Event = threading.Event()
        self._lock: threading.Lock = threading.Lock()
        self._restarts: dict[str, dict[str, float]] = {}

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
                repo = self._current_repos.get(alias)
                self._terminate_child(alias, proc, repo.path if repo else None)
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
                    self._terminate_child(alias, proc, self._current_repos[alias].path)
                self._restarts.pop(alias, None)
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
                    self._terminate_child(alias, proc, self._current_repos[alias].path)
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

        A live process is not the same as a working watcher, so every entry
        also carries the watcher health the child publishes: ``watcher``
        (ok/stalled/unknown/dead), ``observer_alive`` and ``last_event_at``.
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
                            "restarts": self.restart_count(alias),
                            **_health_fields(repo.path, alive),
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
                            "restarts": self.restart_count(repo.alias),
                            **_health_fields(repo.path, alive),
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

    def _restart_delay(self, alias: str) -> float:
        """Seconds to wait before the next restart of *alias*.

        A watcher that keeps dying — a broken repo, an unreadable database —
        used to be restarted every 30s forever, each attempt paying for a full
        initial update.  The delay doubles per failure and resets once a
        watcher has stayed up long enough to count as healthy.
        """
        state = self._restarts.setdefault(alias, {"count": 0, "next_attempt": 0.0})
        started_at = state.get("started_at")
        if isinstance(started_at, (int, float)):
            if time.monotonic() - started_at >= _RESTART_HEALTHY_SECONDS:
                state["count"] = 0
        state["count"] = int(state["count"]) + 1
        delay = min(
            _RESTART_BACKOFF_BASE * (2 ** (int(state["count"]) - 1)),
            _RESTART_BACKOFF_MAX,
        )
        return float(delay)

    def restart_count(self, alias: str) -> int:
        """How many times *alias* has been restarted since it was last healthy."""
        return int(self._restarts.get(alias, {}).get("count", 0))

    def _check_health(self) -> None:
        """Check each watcher child and restart if dead.

        A watcher that died takes the process with it and is restarted here,
        with exponential backoff so a repo that cannot start does not burn a
        full initial update every 30s.  One that is merely stalled — observer
        threads gone, heartbeat frozen — is reported, not restarted: the child
        exits on its own when it detects that, and restarting on a stale
        heartbeat alone risks a restart loop.
        """
        restarted = False
        with self._lock:
            for alias, repo in list(self._current_repos.items()):
                proc = self._children.get(alias)
                if proc is None or proc.poll() is not None:
                    self._children.pop(alias, None)
                    state = self._restarts.get(alias, {})
                    now = time.monotonic()
                    if now < float(state.get("next_attempt", 0.0)):
                        logger.debug(
                            "Watcher for '%s' is dead; waiting %.0fs before restart %d",
                            alias,
                            float(state["next_attempt"]) - now,
                            self.restart_count(alias) + 1,
                        )
                        continue
                    delay = self._restart_delay(alias)
                    self._restarts[alias]["next_attempt"] = now + delay
                    logger.warning(
                        "Watcher for '%s' is dead — restarting (attempt %d, "
                        "next retry no sooner than %.0fs)",
                        alias,
                        self.restart_count(alias),
                        delay,
                    )
                    self._start_watcher(repo)
                    restarted = True
                    continue
                health = read_watch_health(repo.path)
                if health is not None and health.get("stalled"):
                    age = health.get("age")
                    logger.warning(
                        "Watcher for '%s' is running but stalled "
                        "(observer_alive=%s, last heartbeat %s) — "
                        "the graph is not being updated",
                        alias,
                        health.get("observer_alive"),
                        f"{age:.0f}s ago" if isinstance(age, (int, float)) else "unknown",
                    )
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

    def _existing_watcher(self, repo: WatchRepo) -> int | None:
        """PID of a live watcher on *repo* that this daemon does not own.

        Two watchers on one repository means two processes writing one
        ``graph.db``.  The usual way to get there was a daemon crash: the
        children survived, ``status`` could not see them, and the next
        ``start`` spawned a full second set.  A watcher publishes its PID in
        its health file, so the check costs one small read.
        """
        health = read_watch_health(repo.path)
        if not isinstance(health, dict):
            return None
        pid = health.get("pid")
        if not isinstance(pid, int) or not _is_pid_alive(pid):
            return None
        mine = self._children.get(repo.alias)
        if mine is not None and mine.pid == pid and mine.poll() is None:
            return None  # our own child, already accounted for
        return pid if is_our_watcher(pid, repo.path) else None

    def _start_watcher(self, repo: WatchRepo) -> None:
        """Spawn a child process running ``code-review-graph watch`` for *repo*."""
        existing = self._existing_watcher(repo)
        if existing is not None:
            logger.warning(
                "A watcher for '%s' is already running (PID %d); not starting a "
                "second one. Run `crg-daemon stop` first if it is an orphan from "
                "a crashed daemon.",
                repo.alias,
                existing,
            )
            return

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
        self._restarts.setdefault(repo.alias, {"count": 0, "next_attempt": 0.0})
        self._restarts[repo.alias]["started_at"] = time.monotonic()
        logger.info(
            "Started watcher for '%s' (PID %d) — log: %s",
            repo.alias,
            proc.pid,
            log_path,
        )

    @staticmethod
    def _terminate_child(
        alias: str,
        proc: subprocess.Popen[bytes],
        repo_path: str | None = None,
    ) -> None:
        """Gracefully terminate a child process (SIGTERM, then SIGKILL).

        The child clears its own health file on SIGTERM, but a killed or
        already-dead one cannot, and a leftover file reads as a stalled
        watcher forever — so the reaper clears it too.
        """
        if repo_path:
            clear_watch_health(repo_path)
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
