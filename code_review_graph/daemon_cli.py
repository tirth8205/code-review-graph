"""CLI entry point for the crg-daemon multi-repo watcher.

Usage:
    crg-daemon start [--foreground]
    crg-daemon stop
    crg-daemon restart [--foreground]
    crg-daemon status
    crg-daemon logs [--repo ALIAS] [--follow] [--lines N]
    crg-daemon add <path> [--alias ALIAS]
    crg-daemon remove <path_or_alias>
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _handle_start(args: argparse.Namespace) -> None:
    """Start the daemon process."""
    from .daemon import WatchDaemon, is_daemon_running, load_config

    if is_daemon_running():
        print("Error: Daemon is already running.")
        sys.exit(1)

    config = load_config()
    daemon = WatchDaemon(config=config)
    daemon.start()

    if not args.foreground:
        daemon.daemonize()

    daemon.run_forever()


def _handle_stop(_args: argparse.Namespace) -> None:
    """Stop the running daemon process."""
    from .daemon import clear_pid, is_daemon_running, read_pid

    if not is_daemon_running():
        print("Daemon is not running.")
        sys.exit(1)

    pid = read_pid()
    if pid is None:
        print("Error: Could not read daemon PID.")
        sys.exit(1)

    print(f"Stopping daemon (PID {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        clear_pid()
        print("Daemon stopped (process already gone).")
        return
    except PermissionError:
        print(f"Error: Permission denied sending signal to PID {pid}.")
        sys.exit(1)

    # Wait up to 5 seconds for process to die
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        # Still alive after 5s — send SIGKILL
        print("Daemon did not stop gracefully, sending SIGKILL...")
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    clear_pid()
    print("Daemon stopped.")


def _handle_restart(args: argparse.Namespace) -> None:
    """Restart the daemon (stop + start)."""
    from .daemon import is_daemon_running

    if is_daemon_running():
        _handle_stop(args)
    else:
        print("Daemon is not running, starting fresh.")

    _handle_start(args)


def _handle_status(_args: argparse.Namespace) -> None:
    """Show daemon status and configuration."""
    from .daemon import is_daemon_running, load_config, load_state, read_pid

    config = load_config()
    running = is_daemon_running()

    if running:
        pid = read_pid()
        print(f"Daemon:  running (PID {pid})")
    else:
        print("Daemon:  not running")

    print(f"Name:    {config.session_name}")
    print(f"Log dir: {config.log_dir}")
    print(f"Poll:    {config.poll_interval}s")
    print()

    if not config.repos:
        print("No repositories configured.")
        print("Use: crg-daemon add <path> [--alias NAME]")
        return

    # Header
    alias_width = max(len(r.alias) for r in config.repos)
    alias_width = max(alias_width, 5)  # minimum "Alias" header width

    if running:
        state = load_state()
        print(f"  {'Alias':<{alias_width}}  {'Status':<8}  {'PID':<8}  Path")
        print(f"  {'-' * alias_width}  {'-' * 8}  {'-' * 8}  {'-' * 40}")
        for repo in config.repos:
            entry = state.get(repo.alias, {})
            child_pid: int | None = entry.get("pid")
            alive = False
            if child_pid is not None:
                try:
                    os.kill(child_pid, 0)
                    alive = True
                except ProcessLookupError:
                    alive = False
                except PermissionError:
                    alive = True
            status_str = "alive" if alive else "dead"
            pid_str = str(child_pid) if child_pid is not None else "-"
            print(f"  {repo.alias:<{alias_width}}  {status_str:<8}  {pid_str:<8}  {repo.path}")
    else:
        print(f"  {'Alias':<{alias_width}}  Path")
        print(f"  {'-' * alias_width}  {'-' * 40}")
        for repo in config.repos:
            print(f"  {repo.alias:<{alias_width}}  {repo.path}")


def _handle_logs(args: argparse.Namespace) -> None:
    """Show daemon or per-repo log files."""
    from .daemon import load_config

    config = load_config()

    if args.repo:
        log_file = config.log_dir / f"{args.repo}.log"
    else:
        log_file = config.log_dir / "daemon.log"

    if not log_file.exists():
        print(f"Log file not found: {log_file}")
        sys.exit(1)

    if args.follow:
        try:
            subprocess.run(["tail", "-f", str(log_file)], check=False)
        except KeyboardInterrupt:
            pass
        return

    # Read last N lines
    lines_count = args.lines
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"Error reading log file: {exc}")
        sys.exit(1)

    lines = text.splitlines()
    tail = lines[-lines_count:] if len(lines) > lines_count else lines
    for line in tail:
        print(line)


def _handle_add(args: argparse.Namespace) -> None:
    """Add a repository to the daemon config."""
    from .daemon import add_repo_to_config, is_daemon_running

    try:
        add_repo_to_config(args.path, alias=args.alias)
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    # Find the repo we just added to show confirmation
    alias = args.alias or os.path.basename(os.path.abspath(args.path))
    print(f"Added repository: {args.path} (alias: {alias})")

    if is_daemon_running():
        print("Daemon will pick up the change automatically.")


def _handle_remove(args: argparse.Namespace) -> None:
    """Remove a repository from the daemon config."""
    from .daemon import is_daemon_running, load_config, remove_repo_from_config

    config_before = load_config()
    count_before = len(config_before.repos)

    config_after = remove_repo_from_config(args.path_or_alias)
    count_after = len(config_after.repos)

    if count_before == count_after:
        print(f"No repository matching '{args.path_or_alias}' found in config.")
        sys.exit(1)

    print(f"Removed repository: {args.path_or_alias}")

    if is_daemon_running():
        print("Daemon will pick up the change automatically.")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point for the crg-daemon CLI."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    ap = argparse.ArgumentParser(
        prog="crg-daemon",
        description="Multi-repo watch daemon for code-review-graph",
    )
    sub = ap.add_subparsers(dest="command")

    # start
    start_cmd = sub.add_parser("start", help="Start the daemon")
    start_cmd.add_argument(
        "--foreground",
        action="store_true",
        help="Run in the foreground instead of daemonizing",
    )

    # stop
    sub.add_parser("stop", help="Stop the daemon")

    # restart
    restart_cmd = sub.add_parser("restart", help="Restart the daemon")
    restart_cmd.add_argument(
        "--foreground",
        action="store_true",
        help="Run in the foreground instead of daemonizing",
    )

    # status
    sub.add_parser("status", help="Show daemon status and configuration")

    # logs
    logs_cmd = sub.add_parser("logs", help="Show daemon or per-repo logs")
    logs_cmd.add_argument(
        "--repo",
        default=None,
        metavar="ALIAS",
        help="Show logs for a specific repo (by alias)",
    )
    logs_cmd.add_argument(
        "--follow",
        "-f",
        action="store_true",
        help="Follow log output (tail -f)",
    )
    logs_cmd.add_argument(
        "--lines",
        "-n",
        type=int,
        default=50,
        help="Number of lines to show (default: 50)",
    )

    # add
    add_cmd = sub.add_parser("add", help="Add a repository to the daemon config")
    add_cmd.add_argument("path", help="Path to the repository")
    add_cmd.add_argument(
        "--alias",
        default=None,
        help="Short alias for the repository (default: directory name)",
    )

    # remove
    remove_cmd = sub.add_parser("remove", help="Remove a repository from the daemon config")
    remove_cmd.add_argument("path_or_alias", help="Repository path or alias to remove")

    args = ap.parse_args()

    if not args.command:
        ap.print_help()
        sys.exit(0)

    handlers: dict[str, object] = {
        "start": _handle_start,
        "stop": _handle_stop,
        "restart": _handle_restart,
        "status": _handle_status,
        "logs": _handle_logs,
        "add": _handle_add,
        "remove": _handle_remove,
    }

    handler = handlers.get(args.command)
    if handler is None:
        ap.print_help()
        sys.exit(1)

    handler(args)  # type: ignore[operator]


if __name__ == "__main__":
    main()
