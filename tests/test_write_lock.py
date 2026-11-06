"""Write-lock lifecycle and pre-initialization exclusion (PR #1126)."""

import errno
import os
import sys

import pytest

from code_review_graph import cli
from code_review_graph.graph import CorruptGraphDatabaseError
from code_review_graph.locking import WriteLock
from code_review_graph.tools.build import build_or_update_graph, run_postprocess

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock")


@pytest.mark.parametrize("blocking", [False, True])
@pytest.mark.parametrize(
    "error", [errno.EAGAIN, errno.EACCES, errno.ENOSYS, errno.ENOLCK, errno.EOPNOTSUPP],
)
def test_only_lock_contention_is_reported_as_a_skip(tmp_path, monkeypatch, error, blocking):
    import fcntl

    def fail_flock(*args):
        raise OSError(error, os.strerror(error))

    lock = WriteLock(tmp_path / "graph.db")
    with monkeypatch.context() as patch:
        patch.setattr(fcntl, "flock", fail_flock)
        if error in (errno.EAGAIN, errno.EACCES):
            assert lock.acquire(blocking=blocking) is False
        else:
            # No flock support: run without the lock, as with a lock file that
            # cannot be opened. It must not stop the build.
            assert lock.acquire(blocking=blocking) is True
    assert not lock.held
    # A failed acquisition must leave the instance reusable.
    assert lock.acquire()
    lock.release()


@pytest.mark.parametrize(
    "entry", ["build", "update", "postprocess", "tool-build", "tool-postprocess"]
)
@pytest.mark.parametrize("state", ["missing", "legacy", "corrupt"])
def test_contended_writers_leave_database_files_untouched(tmp_path, monkeypatch, entry, state):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    db = repo / ".code-review-graph" / "graph.db"
    legacy = repo / ".code-review-graph.db"
    lock = WriteLock(db)
    assert lock.acquire()
    target = legacy if state == "legacy" else db
    contents = b"unfinished database owned by another writer"
    if state != "missing":
        target.write_bytes(contents)
    sidecar = repo / ".code-review-graph.db-journal"
    sidecar.write_bytes(b"owned by the active writer")
    try:
        if entry.startswith("tool-"):
            call = build_or_update_graph if entry == "tool-build" else run_postprocess
            result = call(repo_root=str(repo))
            assert result["status"] == "contended"
            assert result["holder_pid"] == os.getpid()
        else:
            monkeypatch.setattr(sys, "argv", ["crg", entry, "--repo", str(repo)])
            with pytest.raises(SystemExit) as caught:
                cli.main()
            assert caught.value.code == cli.EXIT_TEMPFAIL
        if state != "missing":
            assert target.read_bytes() == contents
        if state != "corrupt":
            assert not db.exists()
        assert sidecar.read_bytes() == b"owned by the active writer"
    finally:
        lock.release()


@pytest.mark.parametrize("location", ["environment", "cli"])
def test_contended_build_uses_the_configured_data_directory(tmp_path, monkeypatch, location):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    data_dir = tmp_path / "data"
    lock = WriteLock(data_dir / "graph.db")
    assert lock.acquire()
    argv = ["crg", "build", "--repo", str(repo)]
    if location == "environment":
        monkeypatch.setenv("CRG_DATA_DIR", str(data_dir))
    else:
        argv.extend(["--data-dir", str(data_dir)])
    monkeypatch.setattr(sys, "argv", argv)
    try:
        with pytest.raises(SystemExit) as caught:
            cli.main()
        assert caught.value.code == cli.EXIT_TEMPFAIL
        assert not (data_dir / "graph.db").exists()
        assert not (repo / ".code-review-graph" / "graph.db").exists()
    finally:
        lock.release()


@pytest.mark.parametrize("call", [build_or_update_graph, run_postprocess])
def test_initialization_failure_releases_the_write_lock(tmp_path, call):
    data = tmp_path / ".code-review-graph"
    data.mkdir()
    db = data / "graph.db"
    db.write_bytes(b"corrupt cached graph")
    with pytest.raises(CorruptGraphDatabaseError):
        call(repo_root=str(tmp_path))
    probe = WriteLock(db)
    assert probe.acquire(), "failed initialization left the lock held"
    probe.release()


@pytest.mark.parametrize("command", ["build", "update", "postprocess"])
def test_writers_still_accept_a_plain_directory(tmp_path, monkeypatch, capsys, command):
    """They reach tool validation first now, so they must set up the marker it wants."""
    repo = tmp_path / "plain"
    repo.mkdir()
    (repo / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["crg", command, "--repo", str(repo)])

    cli.main()

    assert (repo / ".code-review-graph" / "graph.db").exists()
    assert (repo / ".code-review-graph" / "write.lock").exists()
