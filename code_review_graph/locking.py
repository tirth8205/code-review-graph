"""Cross-process single-flight guard for graph writes.

``build`` and ``update`` are not only typed by hand.  They are fired from
commit hooks, editor-save hooks, agent tool hooks and the file watcher, and
several of those can start the same command within seconds of each other.
Until this module existed the only cross-process protection the graph had was
SQLite's busy timeout, which is not reached until the *first write*: every
duplicate run therefore paid for a complete parse — including spawning its own
process pool — before discovering that an older run had already done that work.
A hook firing ten times in a row started ten builds of the same working tree,
and the machine spent its CPU on nine builds whose output the last one
overwrote.

The lock is taken at the front door instead.  One run parses, a later run
returns immediately and names the process that owns the work, so a queue of
pending hook invocations collapses into a single build of the newest state.
``postprocess`` takes the same lock: it rewrites derived data (flows,
communities, FTS) for a graph another process may be half way through
building, which was the other half of the report in #1073.

Properties this relies on, all of them deliberate:

* **Advisory and exclusive.** A cooperating process must ask for it; nothing
  else is prevented from reading or writing the database.
* **Scoped to one repository's database.** The lock file sits in the data
  directory next to ``graph.db``, so two repositories built at once never
  block each other. The name is fixed (``write.lock``) and documented, so
  external tooling can exclude itself from a build with
  ``flock -n .code-review-graph/write.lock code-review-graph build``.
* **Non-blocking by default.** Skipping is the right answer for a hook: the
  in-flight build already covers the same working tree. Callers that want to
  wait (a human running ``build``/``update`` by hand) ask for it.
* **Kernel-released.** ``flock`` drops the lock when the holder dies, including
  on ``SIGKILL`` where no cleanup code of ours runs, so a crashed build never
  wedges a repository.
* **``flock``, not ``lockf``.** flock locks belong to the open file
  description, so a probe from a second descriptor cannot silently drop a lock
  this process already holds.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Name of the lock file inside the data directory.
#:
#: Fixed and documented rather than derived from the database name so that
#: git hooks and wrapper scripts can take the same lock with
#: ``flock -n .code-review-graph/write.lock ...`` and be excluded by it.
WRITE_LOCK_NAME = "write.lock"


def write_lock_path(db_path: Path) -> Path:
    """Return the write-lock path that governs *db_path*.

    Lives in the data directory rather than the repository so a graph stored
    outside the working tree (``CRG_DATA_DIR``, ``--data-dir``) keeps its lock
    with it. One database, one lock: everything sharing a ``graph.db`` also
    shares this file, which is what makes the exclusion complete.
    """
    return db_path.with_name(WRITE_LOCK_NAME)


def _flock_supported() -> bool:
    return sys.platform != "win32"


def _flock(handle: Any, blocking: bool) -> bool:
    """Take an exclusive lock on *handle*. Returns False when it is held."""
    import fcntl

    flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
    try:
        fcntl.flock(handle.fileno(), flags)
    except OSError:
        return False
    return True


def read_write_lock_holder(db_path: Path) -> int | None:
    """Return the PID that recorded itself as the lock holder, if any.

    Only meaningful while the lock is actually held: a released lock is
    truncated by [_WriteLock.release], so an empty file reads as ``None``.
    """
    try:
        raw = write_lock_path(db_path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


class WriteLock:
    """Exclusive, per-repository guard held for the whole write window.

    ``acquire`` is idempotent within one instance so a caller can hold the lock
    across several stages, and re-entrancy is per instance rather than global:
    a second instance in the same process still contends, which is what makes
    the overlap testable without spawning a second process.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.path = write_lock_path(db_path)
        self._handle: Any = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self, *, blocking: bool = False) -> bool:
        """Claim the lock, or report that somebody else holds it.

        Returns True when this instance owns the lock afterwards. On platforms
        without ``flock`` this is a no-op that reports success, so behaviour
        there is exactly what it was before this module existed.
        """
        if self._handle is not None:
            return True
        if not _flock_supported():  # pragma: no cover - POSIX in CI
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115
        except OSError as exc:
            # A lock file we cannot even open must not fail the build: losing
            # the guard degrades to the old behaviour, which is what the user
            # asked for anyway.
            logger.warning("Cannot open write lock %s: %s", self.path, exc)
            return True
        if not _flock(handle, blocking):
            handle.close()
            return False
        self._handle = handle
        self._record_holder(handle)
        return True

    def _record_holder(self, handle: Any) -> None:
        """Write this process's PID so a skipped run can name it.

        Best effort: the diagnostic is not worth failing a build over, and a
        stale PID here can only be read by a process that just failed to take
        the lock, which means a live holder is about to overwrite it.
        """
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n")
            handle.flush()
        except OSError:  # pragma: no cover - best-effort diagnostic
            logger.debug("Cannot record write lock holder in %s", self.path)

    def release(self) -> None:
        """Drop the lock if this instance holds it."""
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            handle.truncate()
            handle.flush()
        except OSError:  # pragma: no cover - best-effort cleanup
            pass
        try:
            handle.close()  # closing the description releases the flock
        except OSError:  # pragma: no cover - best-effort cleanup
            pass

    def holder_pid(self) -> int | None:
        """PID of the process holding the lock, as recorded by its holder."""
        return read_write_lock_holder(self.db_path)

    def __enter__(self) -> WriteLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()
