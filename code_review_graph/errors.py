"""Errors the CLI is expected to report as one line, never as a traceback.

Every exception here means "the tool cannot do the job, and it knows why".
``cli.main`` turns each of them into the house failure style — a single
``Error: ...`` line on stderr and exit 1 — so the message is the only thing
the user sees. The MCP tools turn them into ``{"status": "error", ...}``.

Keep the ``str()`` of each one short enough to read on a terminal line and
specific enough to act on: what is wrong, where, and what to do about it.
"""

from __future__ import annotations

import sqlite3


class CodeReviewGraphError(Exception):
    """Base class for every self-explaining failure in this package."""


class GraphStoreError(CodeReviewGraphError):
    """The graph database cannot be opened, read, or written.

    Raised for a corrupt or foreign ``graph.db``, one written by a newer
    schema than this installation understands, and for a data directory the
    process may not write to.
    """


class GraphRootMismatchError(GraphStoreError):
    """The graph on disk was built for a different repository root.

    Answering from it would serve one repository another repository's
    symbols, so every consumer refuses instead.
    """


class ChangeDiscoveryError(CodeReviewGraphError, RuntimeError):
    """The set of changed files or lines could not be determined.

    Distinct from "there are no changes" on purpose: a review gate keyed on
    an all-clear must not pass a pull request that was never looked at.
    Raised when the VCS binary is missing, times out, or fails.

    Also a ``RuntimeError``: ``get_changed_files(strict=True)`` has raised
    one since before this class existed, and callers that catch
    ``RuntimeError`` there keep working.
    """


# SQLite's answer to a writer that waited out ``busy_timeout`` is
# ``OperationalError: database is locked``. Nothing between there and the
# process boundary used to catch it, so a user whose watcher happened to be
# mid-update saw twenty lines of internal traceback.
#
# Contention is emphatically not one of the failures above. A graph another
# process is writing is healthy; it needs a second try, not the rebuild a
# :class:`GraphStoreError` prescribes. The two are told apart here, once, so
# that no caller can quietly relabel one as the other.
_LOCK_ERRORS = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
)


def is_lock_error(exc: BaseException) -> bool:
    """True when SQLite refused because another process holds the write lock."""
    return isinstance(exc, sqlite3.OperationalError) and any(
        needle in str(exc).lower() for needle in _LOCK_ERRORS
    )


__all__ = [
    "ChangeDiscoveryError",
    "CodeReviewGraphError",
    "GraphRootMismatchError",
    "GraphStoreError",
    "is_lock_error",
]
