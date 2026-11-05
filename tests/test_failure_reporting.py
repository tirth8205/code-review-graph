"""What the tool says when it cannot do the job.

Three release blockers, all the same shape: the tool answered as though
nothing were wrong, or crashed with a traceback, instead of naming the
problem.

* an unreadable, foreign or newer-schema ``graph.db`` tracebacked out of
  ``GraphStore.__init__`` on every command;
* an unwritable data directory did the same;
* ``detect-changes`` printed ``No changes detected.`` and exited 0 when it
  could not run git at all, which is a false all-clear for any CI gate keyed
  on that exit code.

``tests/test_cli_surface.py`` drives all of this through real subprocesses,
but it is an opt-in release gate (``-m cli_surface``) that the ordinary CI
run does not collect. These are the in-suite regression tests, at the level
of the functions that carry the contract.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from code_review_graph import cli
from code_review_graph.errors import (
    ChangeDiscoveryError,
    CodeReviewGraphError,
    GraphRootMismatchError,
    GraphStoreError,
    is_lock_error,
)
from code_review_graph.graph import CorruptGraphDatabaseError, GraphStore
from code_review_graph.incremental import (
    assert_graph_serves_root,
    full_build,
    get_changed_files,
    get_db_path,
    get_staged_and_unstaged,
)
from code_review_graph.migrations import LATEST_VERSION

# ---------------------------------------------------------------------------
# 1. An unusable graph.db
# ---------------------------------------------------------------------------


def _built_db(tmp_path: Path) -> Path:
    """A real graph.db with one file in it, closed and ready to damage."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "app.py").write_text("def handle():\n    return 1\n", encoding="utf-8")
    db_path = repo / ".code-review-graph" / "graph.db"
    store = GraphStore(db_path)
    try:
        full_build(repo, store)
    finally:
        store.close()
    for suffix in ("-wal", "-shm"):
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
    return db_path


def test_garbage_bytes_are_reported_not_raised(tmp_path):
    db = _built_db(tmp_path)
    db.write_bytes(os.urandom(64 * 1024))
    with pytest.raises(GraphStoreError) as caught:
        GraphStore(db)
    message = str(caught.value)
    assert str(db) in message
    assert "build" in message
    assert not isinstance(caught.value, sqlite3.Error)


def test_a_truncated_database_is_reported(tmp_path):
    """A partial write: a real SQLite header over a body that ends early."""
    db = _built_db(tmp_path)
    db.write_bytes(db.read_bytes()[:512])
    with pytest.raises(GraphStoreError) as caught:
        GraphStore(db)
    assert str(db) in str(caught.value)


def test_a_foreign_sqlite_file_is_not_adopted(tmp_path):
    """``CREATE TABLE IF NOT EXISTS`` would otherwise graft our schema on."""
    db = _built_db(tmp_path)
    db.unlink()
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY, total REAL)")
    conn.commit()
    conn.close()

    with pytest.raises(GraphStoreError, match="not a code-review-graph"):
        GraphStore(db)

    # And it is left exactly as it was found.
    conn = sqlite3.connect(str(db))
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()
    assert tables == {"invoices"}


def test_a_newer_schema_is_refused_with_both_versions(tmp_path):
    """run_migrations is a no-op above LATEST_VERSION, so nothing else notices."""
    db = _built_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES ('schema_version', ?)",
        (str(LATEST_VERSION + 5),),
    )
    conn.commit()
    conn.close()

    with pytest.raises(GraphStoreError) as caught:
        GraphStore(db)
    message = str(caught.value)
    assert f"v{LATEST_VERSION + 5}" in message
    assert f"v{LATEST_VERSION}" in message
    assert "newer" in message


def test_an_empty_file_is_still_a_new_database(tmp_path):
    """SQLite's own rule, and the one `build` relies on. Not corruption."""
    db = _built_db(tmp_path)
    db.write_bytes(b"")
    store = GraphStore(db)
    try:
        assert store.get_stats().total_nodes == 0
    finally:
        store.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores the read-only bit",
)
def test_unwritable_data_directory_names_the_directory(tmp_path):
    db = _built_db(tmp_path)
    data_dir = db.parent
    original = data_dir.stat().st_mode
    data_dir.chmod(0o500)
    try:
        with pytest.raises(GraphStoreError) as caught:
            GraphStore(db)
    finally:
        data_dir.chmod(original)
    message = str(caught.value)
    assert str(data_dir) in message
    assert "CRG_DATA_DIR" in message


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores the read-only bit",
)
def test_a_data_directory_that_cannot_be_created_is_reported(tmp_path):
    """The same failure one step earlier than the test above.

    ``GraphStore`` never sees this one: ``get_data_dir`` creates the
    directory while the path is still being resolved, so an unwritable parent
    escaped as a ``PermissionError`` from ``pathlib.mkdir``.
    """
    parent = tmp_path / "ro"
    parent.mkdir()
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    original = parent.stat().st_mode
    parent.chmod(0o500)
    try:
        with patch.dict(os.environ, {"CRG_DATA_DIR": str(parent / "fresh")}):
            with pytest.raises(GraphStoreError) as caught:
                get_db_path(repo)
    finally:
        parent.chmod(original)
    message = str(caught.value)
    assert str(parent / "fresh") in message
    assert "CRG_DATA_DIR" in message


def test_a_failed_open_leaves_no_connection_behind(tmp_path):
    """Whatever went wrong, the file handle is not leaked to the caller."""
    db = _built_db(tmp_path)
    db.write_bytes(os.urandom(4096))
    with pytest.raises(GraphStoreError):
        GraphStore(db)
    # Proven by the file being replaceable straight away on every platform,
    # including Windows, where an open handle would block the unlink.
    db.unlink()


# ---------------------------------------------------------------------------
# 2. A graph that belongs to another repository
# ---------------------------------------------------------------------------


def _graph_of(repo: Path) -> GraphStore:
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    store = GraphStore(repo / ".code-review-graph" / "graph.db")
    full_build(repo, store)
    return store


def test_a_foreign_repository_graph_is_refused(tmp_path):
    donor = tmp_path / "donor"
    donor.mkdir()
    (donor / "donor_mod.py").write_text("def donor_only():\n    return 1\n", "utf-8")
    host = tmp_path / "host"
    host.mkdir()
    (host / ".git").mkdir()

    store = _graph_of(donor)
    try:
        with pytest.raises(GraphRootMismatchError) as caught:
            assert_graph_serves_root(host, store)
    finally:
        store.close()
    message = str(caught.value)
    assert str(host) in message
    assert "different repository root" in message


def test_the_graph_of_this_repository_is_served(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def handle():\n    return 1\n", "utf-8")
    store = _graph_of(repo)
    try:
        assert_graph_serves_root(repo, store) is None
    finally:
        store.close()


def test_an_equivalent_spelling_of_the_same_root_is_served(tmp_path):
    """A symlinked checkout is the same repository, not a foreign one.

    This is the macOS ``/var`` against ``/private/var`` case, and any repo
    reached through a symlinked parent on Linux.
    """
    real = tmp_path / "real"
    real.mkdir()
    (real / "app.py").write_text("def handle():\n    return 1\n", "utf-8")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    store = _graph_of(real)
    try:
        assert_graph_serves_root(link, store) is None
    finally:
        store.close()


def test_a_relative_path_graph_carries_no_root_identity(tmp_path):
    """Relative markers cannot be attributed to any root, so they pass."""
    from code_review_graph.parser import NodeInfo

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    store = GraphStore(repo / ".code-review-graph" / "graph.db")
    try:
        store.upsert_node(NodeInfo(
            kind="File", name="src/app.py", file_path="src/app.py",
            line_start=1, line_end=2, language="python",
        ))
        assert assert_graph_serves_root(repo, store) is None
    finally:
        store.close()


def test_a_graph_of_files_not_on_this_machine_is_stale_not_foreign(tmp_path):
    """Nothing is being served another live checkout's answers here."""
    from code_review_graph.parser import NodeInfo

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    store = GraphStore(repo / ".code-review-graph" / "graph.db")
    try:
        store.upsert_node(NodeInfo(
            kind="File", name="/nowhere/src/app.py", file_path="/nowhere/src/app.py",
            line_start=1, line_end=2, language="python",
        ))
        assert assert_graph_serves_root(repo, store) is None
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 3. Change discovery that could not look
# ---------------------------------------------------------------------------


@pytest.fixture()
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError(2, "No such file or directory", "git"),
        subprocess.TimeoutExpired("git", 30),
    ],
    ids=["missing-binary", "timeout"],
)
def test_require_vcs_turns_an_unrunnable_git_into_an_error(failure, git_repo):
    with patch("code_review_graph.incremental.subprocess.run", side_effect=failure):
        with pytest.raises(ChangeDiscoveryError) as caught:
            get_changed_files(git_repo, "HEAD~1", require_vcs=True)
    assert "could not determine the changes" in str(caught.value)

    with patch("code_review_graph.incremental.subprocess.run", side_effect=failure):
        with pytest.raises(ChangeDiscoveryError):
            get_staged_and_unstaged(git_repo, require_vcs=True)


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError(2, "No such file or directory", "git"),
        subprocess.TimeoutExpired("git", 30),
    ],
    ids=["missing-binary", "timeout"],
)
def test_without_require_vcs_the_lenient_contract_is_unchanged(failure, git_repo):
    """``update`` recovers by re-parsing and hashing, so it keeps the [] path."""
    with patch("code_review_graph.incremental.subprocess.run", side_effect=failure):
        assert get_changed_files(git_repo, "HEAD~1") == []
        assert get_staged_and_unstaged(git_repo) == []


def test_the_timeout_message_names_the_knob(git_repo):
    with patch(
        "code_review_graph.incremental.subprocess.run",
        side_effect=subprocess.TimeoutExpired("git", 30),
    ):
        with pytest.raises(ChangeDiscoveryError) as caught:
            get_changed_files(git_repo, "HEAD~1", require_vcs=True)
    assert "CRG_GIT_TIMEOUT" in str(caught.value)


def test_a_base_ref_that_does_not_resolve_is_not_an_unrunnable_git(git_repo):
    """A repository with no commits still has to work.

    ``require_vcs`` is about a VCS that could not be run at all. A base ref
    that simply is not there keeps the documented ``--cached`` fallback, or
    ``detect-changes`` would fail on every fresh ``git init``.
    """
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)

        class _Result:
            returncode = 128 if len(calls) == 1 else 0
            stdout = b""

        return _Result()

    with patch("code_review_graph.incremental.subprocess.run", side_effect=fake_run):
        assert get_changed_files(git_repo, "HEAD~1", require_vcs=True) == []
    assert len(calls) == 2, "the --cached fallback was skipped"


def test_analyze_changes_degrades_rather_than_failing_on_a_partial_diff(tmp_path):
    """Files are known, lines are not: precision is lost, honesty is not."""
    from code_review_graph.changes import analyze_changes

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "app.py").write_text("def handle():\n    return 1\n", "utf-8")
    store = GraphStore(repo / ".code-review-graph" / "graph.db")
    try:
        full_build(repo, store)
        with patch(
            "code_review_graph.changes.parse_diff_ranges",
            side_effect=ChangeDiscoveryError("git timed out after 30s"),
        ):
            result = analyze_changes(
                store,
                changed_files=["app.py"],
                repo_root=str(repo),
                require_vcs=True,
            )
    finally:
        store.close()

    assert "git timed out" in result["diff_ranges_unavailable"]
    assert "line-level diff unavailable" in result["summary"]
    assert result["changed_functions"], "degraded to nothing at all"


def test_analyze_changes_refuses_when_there_is_nothing_else_to_go_on(tmp_path):
    """With no file list either, an empty answer would be an unearned all-clear."""
    from code_review_graph.changes import analyze_changes

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    store = GraphStore(repo / ".code-review-graph" / "graph.db")
    try:
        with patch(
            "code_review_graph.changes.parse_diff_ranges",
            side_effect=ChangeDiscoveryError("git could not be run"),
        ):
            with pytest.raises(ChangeDiscoveryError):
                analyze_changes(
                    store, changed_files=[], repo_root=str(repo), require_vcs=True
                )
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 4. The house style, at the top level
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        GraphStoreError("the graph database at /x/graph.db is unreadable"),
        GraphRootMismatchError("the graph at /x was built for a different root"),
        ChangeDiscoveryError("could not determine the changes: git timed out"),
    ],
    ids=["store", "root", "discovery"],
)
def test_main_reports_a_self_explaining_failure_in_one_line(error, capsys):
    """One ``Error: ...`` line on stderr, exit 1, nothing on stdout."""
    with patch.object(cli, "_dispatch", side_effect=error):
        with patch.object(sys, "argv", ["code-review-graph", "status"]):
            with pytest.raises(SystemExit) as caught:
                cli.main()
    assert caught.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == f"Error: {error}"
    assert "Traceback" not in captured.err


def test_main_does_not_swallow_an_unforeseen_bug(capsys):
    """Only self-explaining failures lose their traceback."""
    with patch.object(cli, "_dispatch", side_effect=ZeroDivisionError("boom")):
        with patch.object(sys, "argv", ["code-review-graph", "status"]):
            with pytest.raises(ZeroDivisionError):
                cli.main()


def test_every_reported_failure_shares_one_base_class():
    """``main`` catches one family by class; this is what has to be under it."""
    for error_type in (GraphStoreError, GraphRootMismatchError, ChangeDiscoveryError):
        assert issubclass(error_type, CodeReviewGraphError)


def test_contention_is_reported_as_contention_not_as_damage(capsys):
    """The second family ``main`` handles, and it must stay the second one.

    A graph another process is writing is healthy. Folding it into
    ``GraphStoreError`` would print the rebuild advice that belongs to a
    corrupt file, over a graph that needs nothing but a second try.
    """
    locked = sqlite3.OperationalError("database is locked")
    assert not isinstance(locked, CodeReviewGraphError)
    with patch.object(cli, "_dispatch", side_effect=locked):
        with patch.object(sys, "argv", ["code-review-graph", "build"]):
            with pytest.raises(SystemExit) as caught:
                cli.main()
    assert caught.value.code == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "another process" in captured.err
    assert "database is locked" in captured.err
    # The repair for a damaged graph, which this is not.
    assert "delete" not in captured.err.lower()


def test_a_contended_open_is_not_dressed_up_as_a_corrupt_graph(tmp_path):
    """The two failures meet inside ``GraphStore.__init__``; they stay apart.

    ``run_migrations`` retries a held write lock and re-raises the
    ``OperationalError`` when it runs out of attempts. That lands in the same
    handler that turns a corrupt file into a ``GraphStoreError``, and being
    told to delete a healthy graph is the worst possible advice.
    """
    db_path = tmp_path / "graph.db"
    with patch(
        "code_review_graph.graph.run_migrations",
        side_effect=sqlite3.OperationalError("database is locked"),
    ):
        with pytest.raises(sqlite3.OperationalError) as caught:
            GraphStore(db_path)
    assert not isinstance(caught.value, GraphStoreError)
    assert is_lock_error(caught.value)


def test_an_unwritable_directory_is_still_a_graph_store_error(tmp_path):
    """The other half of the pair above: damage keeps its own message."""
    locked_dir = tmp_path / "ro"
    locked_dir.mkdir()
    (locked_dir / "graph.db").write_bytes(b"not a database at all")
    locked_dir.chmod(0o500)
    try:
        with pytest.raises(GraphStoreError) as caught:
            GraphStore(locked_dir / "graph.db")
    finally:
        locked_dir.chmod(0o700)
    assert not is_lock_error(caught.value)
    assert str(locked_dir / "graph.db") in str(caught.value)


# ---------------------------------------------------------------------------
# 5. Where the two rounds of work meet: reporting and recovery
# ---------------------------------------------------------------------------
#
# Reporting an unusable graph and rebuilding one are the same decision seen
# from two sides, and they are made in one place. ``cli._open_graph_store``
# discards exactly what ``CorruptGraphDatabaseError`` names, for exactly the
# one command that was going to rewrite the graph anyway. Everything else --
# contention, a read-only checkout, somebody else's SQLite file, a graph from
# a newer release -- is reported and left untouched, because discarding any of
# those destroys something that was never broken.


def _unreadable(tmp_path: Path) -> Path:
    db = _built_db(tmp_path)
    db.write_bytes(os.urandom(64 * 1024))
    return db


def test_build_discards_an_unreadable_database_and_rebuilds_it(tmp_path):
    db = _unreadable(tmp_path)
    store = cli._open_graph_store(db, "build")
    try:
        assert store.get_stats().total_nodes == 0
    finally:
        store.close()
    assert db.read_bytes()[:16] == b"SQLite format 3\x00"


def test_a_read_command_reports_the_unreadable_database_and_keeps_it(tmp_path):
    """One line through ``main``'s house style, and the file is still there.

    A read command has nothing to rebuild from, so discarding would only
    trade a nameable failure for a silently empty graph.
    """
    db = _unreadable(tmp_path)
    before = db.read_bytes()
    with pytest.raises(GraphStoreError) as caught:
        cli._open_graph_store(db, "status")
    assert isinstance(caught.value, CorruptGraphDatabaseError)
    assert str(db) in str(caught.value)
    assert "build" in str(caught.value)
    assert db.read_bytes() == before


def test_build_does_not_discard_a_contended_database(tmp_path):
    """The one that would be unrecoverable: a healthy graph, deleted."""
    db = _built_db(tmp_path)
    before = db.read_bytes()
    with patch(
        "code_review_graph.graph.run_migrations",
        side_effect=sqlite3.OperationalError("database is locked"),
    ):
        with pytest.raises(sqlite3.OperationalError) as caught:
            cli._open_graph_store(db, "build")
    assert is_lock_error(caught.value)
    assert not isinstance(caught.value, CorruptGraphDatabaseError)
    assert db.read_bytes() == before


def test_build_does_not_discard_somebody_elses_sqlite_file(tmp_path):
    db = _built_db(tmp_path)
    db.unlink()
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY, total REAL)")
    conn.execute("INSERT INTO invoices VALUES (1, 99.5)")
    conn.commit()
    conn.close()

    with pytest.raises(GraphStoreError) as caught:
        cli._open_graph_store(db, "build")
    assert not isinstance(caught.value, CorruptGraphDatabaseError)
    conn = sqlite3.connect(str(db))
    try:
        assert list(conn.execute("SELECT * FROM invoices")) == [(1, 99.5)]
    finally:
        conn.close()


def test_build_does_not_discard_a_graph_from_a_newer_release(tmp_path):
    """Too old to read it is not the same as it is broken."""
    db = _built_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES ('schema_version', ?)",
        (str(LATEST_VERSION + 5),),
    )
    conn.commit()
    conn.close()
    before = db.read_bytes()

    with pytest.raises(GraphStoreError) as caught:
        cli._open_graph_store(db, "build")
    assert not isinstance(caught.value, CorruptGraphDatabaseError)
    assert db.read_bytes() == before


def test_the_discarded_database_takes_its_wal_sidecars_with_it(tmp_path):
    """A stale -wal beside a fresh database is its own corruption."""
    db = _unreadable(tmp_path)
    for suffix in ("-wal", "-shm"):
        db.with_name(db.name + suffix).write_bytes(b"stale")
    store = cli._open_graph_store(db, "build")
    store.close()
    for suffix in ("-wal", "-shm"):
        sidecar = db.with_name(db.name + suffix)
        assert not sidecar.exists() or sidecar.read_bytes() != b"stale", suffix
