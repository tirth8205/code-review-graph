"""What the graph write lock does when flock misbehaves or a process forks.

From the review of PR #1126. The lock must not report a live writer when no
writer exists, and parse workers forked during a build must not keep it.
"""

from __future__ import annotations

import errno
import json
import multiprocessing
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from code_review_graph.locking import WriteLock

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="no flock on Windows")


@pytest.mark.parametrize("code", [errno.ENOSYS, errno.ENOLCK, errno.EOPNOTSUPP])
def test_a_filesystem_without_flock_is_not_reported_as_a_live_writer(tmp_path, code):
    lock = WriteLock(tmp_path / "graph.db")

    with patch("fcntl.flock", side_effect=OSError(code, "no locks here")):
        assert lock.acquire() is True

    assert lock.held is False


@pytest.mark.parametrize("code", [errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES])
def test_a_held_lock_is_still_reported_as_held(tmp_path, code):
    lock = WriteLock(tmp_path / "graph.db")

    with patch("fcntl.flock", side_effect=OSError(code, "held")):
        assert lock.acquire() is False


def _hold_until_told(started, done) -> None:
    started.set()
    done.wait(30)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_a_forked_child_does_not_keep_the_lock(tmp_path):
    """Parse workers fork on Linux, and they can outlive a killed build."""
    context = multiprocessing.get_context("fork")
    db = tmp_path / "graph.db"
    lock = WriteLock(db)
    assert lock.acquire()
    started = context.Event()
    done = context.Event()
    child = context.Process(target=_hold_until_told, args=(started, done))
    child.start()
    try:
        assert started.wait(30), "the forked child never started"
        lock.release()
        probe = WriteLock(db)
        assert probe.acquire(), "the forked child kept the lock"
        probe.release()
    finally:
        done.set()
        child.join(30)


def test_the_plugin_hook_tolerates_a_skipped_update():
    """A skipped update exits 75, which must not show up as a hook error."""
    hooks = json.loads(
        (Path(__file__).parent.parent / "hooks" / "hooks.json").read_text(encoding="utf-8"),
    )
    commands = [
        hook["command"]
        for groups in hooks["hooks"].values()
        for group in groups
        for hook in group["hooks"]
        if "code-review-graph update" in hook["command"]
    ]

    assert commands
    assert all(command.endswith("|| true") for command in commands)
