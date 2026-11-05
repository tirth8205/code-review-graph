"""Cross-process contention, interruption, daemon lifecycle and watcher stress.

Everything else in the suite exercises one process against one database. This
module exercises the case the product actually ships into: a watch child, a
pre-commit hook, an MCP server and a hand-run ``build`` all pointed at the same
``.code-review-graph/graph.db`` at the same time, and processes that die in the
middle of writing it. That is where a persistent local database corrupts
quietly.

What protects cross-process access today, verified rather than assumed:

* ``GraphStore`` opens SQLite in WAL mode with ``busy_timeout=5000`` and writes
  each file in a ``BEGIN IMMEDIATE`` transaction. That is the *whole* of it.
* ``threading.Lock`` in ``GraphStore`` guards the in-memory NetworkX cache
  only. It is a thread lock; it does nothing across processes.
* Nothing serialises two ``code-review-graph`` processes against the graph
  itself: no lock file next to ``graph.db``, no advisory lock on it, and no
  handshake that tells a second process a first is already writing.

So the guarantee on the database is exactly SQLite's: single writer,
five-second wait, then an error. What is layered on top of it is recovery
rather than exclusion — a ``build_state`` metadata row that marks a build
half-finished until post-processing ends, and a write that fails stopping the
build before the VCS anchor claims the graph is current. The daemon does hold
one exclusive lock, on its PID file in ``$CRG_HOME``, purely so a recycled PID
cannot be mistaken for it.

The tests below pin what all of that does and does not buy.

Run them::

    uv run --python 3.13 python -m pytest tests/test_concurrency_and_daemon.py \\
        -m concurrency -q

They are opt-in: the ordinary suite skips this module because these checks
spawn real processes, kill them, and sleep while filesystem events settle.
Every test also asserts a canary — that the lock really was held, that the
victim process really was killed by a signal, that the watcher really saw
events — so a check cannot pass by silently doing nothing.

Nothing here touches the developer's machine: ``HOME``, ``CRG_HOME`` and the
git identity are redirected into ``tmp_path`` for every subprocess, on top of
the autouse redirect in ``tests/conftest.py``.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from code_review_graph import daemon as daemon_mod
from code_review_graph import migrations as migrations_mod
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import _WatchSupervisor

pytestmark = pytest.mark.concurrency

# A build of the sample repo below takes ~2s; a daemon needs a few seconds to
# fork, spawn a child and publish its state. Caps, not sleeps: every wait loop
# polls for the condition and gives up at the cap.
_PROC_TIMEOUT = 180
_SETTLE = 30.0


@pytest.fixture(autouse=True)
def _opt_in(request: pytest.FixtureRequest) -> None:
    """Skip unless the marker was explicitly selected.

    Registering a marker does not deselect anything on its own, and this
    module must not lengthen the ordinary suite by minutes. Requesting the
    marker (or setting the env var, for a CI job that selects by path) opts
    in.
    """
    selected = request.config.getoption("-m", default="") or ""
    if "concurrency" in selected:
        return
    if os.environ.get("CRG_CONCURRENCY_TESTS") == "1":
        return
    pytest.skip("opt-in: run with -m concurrency or CRG_CONCURRENCY_TESTS=1")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(root: Path, n_files: int = 12) -> Path:
    """A committed git repository of *n_files* small Python modules.

    Each module contributes a File node, a Function, a Class and a method, so
    node counts are a stable multiple of the file count and a partially
    written build is visible as a count that is not that multiple.
    """
    src = root / "src"
    src.mkdir(parents=True, exist_ok=True)
    for i in range(n_files):
        (src / f"mod{i}.py").write_text(
            f"def f{i}(x):\n"
            f"    return x + {i}\n"
            f"\n"
            f"\n"
            f"class C{i}:\n"
            f"    def m{i}(self):\n"
            f"        return f{i}(1)\n",
            encoding="utf-8",
        )
    env = _git_env()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True, env=env)
    return root


def _git_commit_all(repo: Path, message: str = "more") -> None:
    env = _git_env()
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True, env=env)


def _git_env() -> dict[str, str]:
    """Environment that keeps git off the developer's real configuration."""
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "crg-test",
        "GIT_AUTHOR_EMAIL": "crg-test@example.invalid",
        "GIT_COMMITTER_NAME": "crg-test",
        "GIT_COMMITTER_EMAIL": "crg-test@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }


def _isolated_env(home: Path, *, serial: bool = False, **extra: str) -> dict[str, str]:
    """Subprocess environment with HOME and CRG_HOME inside *home*.

    *serial* sets ``CRG_SERIAL_PARSE``. It matters more than it looks:
    ``full_build`` has two loops, and they handle a failed write differently.
    The parallel loop lets the store exception propagate; the serial loop
    (used for repos under 8 files, or whenever this variable is set) catches
    ``Exception`` per file and files it under "Error parsing". Several tests
    below exist only because of that difference.
    """
    crg_home = home / ".code-review-graph"
    crg_home.mkdir(parents=True, exist_ok=True)
    env = {
        **_git_env(),
        "HOME": str(home),
        "CRG_HOME": str(crg_home),
        "HERMES_HOME": str(home / ".hermes"),
        # The watcher rate-limits its health file to one write per interval
        # unless its state changed. A test that waits on `events_seen` would
        # otherwise sit for ten seconds behind a stale document.
        "CRG_WATCH_HEALTH_INTERVAL": "1",
    }
    if serial:
        env["CRG_SERIAL_PARSE"] = "1"
    env.update(extra)
    return env


def _crg(*args: str, env: dict[str, str], **kwargs) -> subprocess.CompletedProcess:
    """Run the CLI the way a user does, as its own process."""
    return subprocess.run(
        [sys.executable, "-m", "code_review_graph", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=_PROC_TIMEOUT,
        check=False,
        **kwargs,
    )


def _daemon_cli(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "code_review_graph.daemon_cli", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=_PROC_TIMEOUT,
        check=False,
    )


def _db_path(repo: Path) -> Path:
    return repo / ".code-review-graph" / "graph.db"


def _seed_empty_db(repo: Path) -> Path:
    """Create the schema without any content, so a lock can be taken first.

    Contention tests need the database to exist before the build starts;
    otherwise the lock holder would race the build to create the file, and a
    test that never actually contended would pass.
    """
    db = _db_path(repo)
    GraphStore(db).close()
    assert db.exists()
    return db


def _open_readonly(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _integrity(db: Path) -> str:
    conn = _open_readonly(db)
    try:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        conn.close()


def _fts_integrity(db: Path) -> str:
    """FTS5's own consistency check between the index and its content table."""
    conn = _open_readonly(db)
    try:
        conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('integrity-check')")
        return "ok"
    except sqlite3.DatabaseError as exc:  # pragma: no cover - failure path
        return f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()


def _counts(db: Path) -> dict[str, int]:
    conn = _open_readonly(db)
    try:
        return {
            "nodes": conn.execute("SELECT count(*) FROM nodes").fetchone()[0],
            "edges": conn.execute("SELECT count(*) FROM edges").fetchone()[0],
            "files": conn.execute(
                "SELECT count(*) FROM nodes WHERE kind = 'File'"
            ).fetchone()[0],
            "flows": conn.execute("SELECT count(*) FROM flows").fetchone()[0],
            "fts_hits": conn.execute(
                "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'f1'"
            ).fetchone()[0],
        }
    finally:
        conn.close()


def _metadata(db: Path, key: str) -> str | None:
    conn = _open_readonly(db)
    try:
        row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])
    finally:
        conn.close()


def _set_metadata(db: Path, key: str, value: str) -> None:
    """Put the graph into a chosen state, the way a crashed run would leave it."""
    conn = _open_readonly(db)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", (key, value)
        )
        conn.commit()
    finally:
        conn.close()


def _wait_for(predicate, timeout: float = _SETTLE, interval: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _wait_for_watching(health_dir: Path, timeout: float = _SETTLE) -> dict:
    """Block until a watcher has left its initial build and is really watching.

    The health file appears during ``phase="initial-build"``, before the
    observer exists. Treating its mere presence as readiness would let a test
    make all its edits before anything was listening, then assert on an
    observer that never saw them.
    """

    def ready() -> bool:
        files = list(health_dir.glob("*.json")) if health_dir.is_dir() else []
        if not files:
            return False
        try:
            return json.loads(files[0].read_text(encoding="utf-8")).get("phase") == "watching"
        except (json.JSONDecodeError, OSError):  # pragma: no cover - torn read
            return False

    assert _wait_for(ready, timeout=timeout), "watcher never reached the watching phase"
    return json.loads(next(health_dir.glob("*.json")).read_text(encoding="utf-8"))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - other user's process
        return True
    return True


_DETACHER = (
    "import os, subprocess, sys\n"
    "proc = subprocess.Popen(sys.argv[1:], stdout=subprocess.DEVNULL,"
    " stderr=subprocess.DEVNULL, start_new_session=True)\n"
    "print(proc.pid)\n"
    "sys.stdout.flush()\n"
    "os._exit(0)\n"
)


def _spawn_detached(*cmd: str) -> int:
    """Start a process that is nobody's child, and return its PID.

    A ``subprocess.Popen`` child that this process never waits on becomes a
    zombie when it is killed, and a zombie still answers ``os.kill(pid, 0)``.
    A test using one would conclude that the CLI *failed* to kill it. The
    intermediate here exits immediately, so the grandchild is reparented and
    behaves like the unrelated process it is standing in for.
    """
    out = subprocess.run(
        [sys.executable, "-c", _DETACHER, *cmd],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return int(out.stdout.strip())


def _read_health(health_dir: Path) -> dict:
    files = list(health_dir.glob("*.json")) if health_dir.is_dir() else []
    if not files:
        return {}
    try:
        return json.loads(files[0].read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):  # pragma: no cover - torn read
        return {}


def _reap(pids) -> None:
    """Best-effort cleanup so no test can leave a process behind."""
    for pid in pids:
        if pid is None:
            continue
        try:
            os.kill(int(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, ValueError, TypeError):
            pass


# A separate process that takes the write lock and holds it. Printing before
# the sleep is the canary: the test refuses to proceed until the lock is
# provably held, so it can never "pass" against an unlocked database.
_LOCK_HOLDER = """
import sqlite3, sys, time
conn = sqlite3.connect(sys.argv[1], timeout=30, isolation_level=None)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("BEGIN IMMEDIATE")
conn.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES ('_lock_probe','1')")
print("HELD", flush=True)
time.sleep(float(sys.argv[2]))
conn.rollback()
"""


class _LockHeld:
    """Context manager owning a process that holds the SQLite write lock."""

    def __init__(self, db: Path, seconds: float, env: dict[str, str]) -> None:
        self._db = db
        self._seconds = seconds
        self._env = env
        self.proc: subprocess.Popen[str] | None = None

    def __enter__(self) -> "_LockHeld":
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _LOCK_HOLDER, str(self._db), str(self._seconds)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._env,
        )
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline().strip()
        assert line == "HELD", f"lock holder never took the lock: {line!r}"
        return self

    def __exit__(self, *_exc) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
        if self.proc is not None:
            self.proc.wait(timeout=30)


# ---------------------------------------------------------------------------
# 1. Concurrent access to one database
# ---------------------------------------------------------------------------


class TestConcurrentAccess:
    """Two processes on one graph.db."""

    def test_the_only_cross_process_protection_is_sqlites_busy_timeout(
        self, tmp_path: Path
    ) -> None:
        """Pin the protection surface the rest of this module reasons about.

        Everything else here is a consequence of these four facts, so they are
        asserted rather than assumed: WAL journalling, a five-second busy
        timeout, autocommit, and — on disk — nothing but the database itself.
        No lock file, no PID file, no "build in progress" marker that a second
        process could notice before it starts writing.
        """
        data_dir = tmp_path / ".code-review-graph"
        store = GraphStore(data_dir / "graph.db")
        try:
            assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
            assert store._conn.isolation_level is None
            store.store_file_nodes_edges(
                "src/a.py", [_node("src/a.py", "File", "a.py")], [], fhash="h"
            )
            on_disk = {path.name for path in data_dir.iterdir()}
        finally:
            store.close()

        assert on_disk <= {"graph.db", "graph.db-wal", "graph.db-shm"}, on_disk
        # The lock GraphStore does hold is a thread lock over the NetworkX
        # cache. It is invisible to any other process by construction.
        assert isinstance(store._cache_lock, type(threading.Lock()))

    def test_blocked_writer_fails_fast_and_keeps_the_raw_cause(
        self, tmp_path: Path
    ) -> None:
        """A writer that waits out busy_timeout stops, and says why.

        The decision the CLI makes here is "fail loudly", not "wait" or
        "retry": ``build`` and ``update`` run from commit and editor hooks
        where an unbounded wait reads as a hang, and there is nothing to
        salvage by waiting longer — a build that stops writes no anchor, so
        the next run rebuilds. This pins the timing (one busy_timeout, not a
        retry ladder) and that the underlying SQLite message survives into
        stderr for anyone diagnosing it.
        """
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "repo", 24)
        env = _isolated_env(home)
        _seed_empty_db(repo)

        with _LockHeld(_db_path(repo), 30.0, env):
            started = time.monotonic()
            blocked = _crg("build", "--repo", str(repo), "-q", env=env)
            waited = time.monotonic() - started

        # Canary: it really contended for the lock. Returning instantly would
        # mean it never tried to write; waiting the full hold would mean the
        # busy timeout was not what ended it.
        assert 3.0 < waited < 25.0, f"no real contention window: waited {waited:.1f}s"
        assert blocked.returncode != 0
        assert "database is locked" in blocked.stderr

    def test_blocked_writer_should_explain_itself(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "repo", 24)
        env = _isolated_env(home)
        _seed_empty_db(repo)

        with _LockHeld(_db_path(repo), 30.0, env):
            blocked = _crg("build", "--repo", str(repo), "-q", env=env)

        assert "Traceback" not in blocked.stderr
        combined = (blocked.stdout + blocked.stderr).lower()
        assert any(
            word in combined for word in ("another process", "in use", "try again", "busy")
        ), blocked.stderr

    def test_build_that_died_on_the_lock_is_repaired_by_the_next_run(
        self, tmp_path: Path
    ) -> None:
        """Loud failure is recoverable: no anchor written, so the next run rebuilds.

        ``full_build`` writes ``git_head_sha`` only after the last file is
        stored. A build that raises part way through therefore leaves no
        anchor, ``resolve_incremental_base`` returns None, and
        ``build_or_update_graph`` promotes the next update to a full rebuild.
        This is the protection that makes the parallel path survivable.
        """
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "repo", 24)
        env = _isolated_env(home)
        _seed_empty_db(repo)

        with _LockHeld(_db_path(repo), 30.0, env):
            failed = _crg("build", "--repo", str(repo), "-q", env=env)
        assert failed.returncode != 0  # canary: it really did fail
        assert _metadata(_db_path(repo), "git_head_sha") is None
        assert _integrity(_db_path(repo)) == "ok"

        recovered = _crg("update", "--repo", str(repo), env=env)
        assert recovered.returncode == 0, recovered.stderr
        after = _counts(_db_path(repo))
        assert after["files"] == 24
        assert after["nodes"] == 24 * 4
        assert after["fts_hits"] > 0
        assert _integrity(_db_path(repo)) == "ok"
        assert _fts_integrity(_db_path(repo)) == "ok"

    def test_serial_build_under_contention_is_repaired_by_the_next_run(
        self, tmp_path: Path
    ) -> None:
        """The serial path's recovery, which used to be impossible.

        ``full_build``'s serial loop wraps each file's *parse* in ``except
        Exception``; a ``database is locked`` from the *store* call used to
        land in that handler too and be reported as a parse error. The build
        kept going, wrote ``last_updated`` and ``git_head_sha`` as though it
        had indexed everything, and exited 0 — so the locked-out files were
        gone for good, the anchor telling every later ``update`` that the
        graph was current.

        Now the write is outside the parse handlers: it stops the build, no
        anchor is written, and a later run reconstructs the whole graph.

        The serial loop is not an exotic path: it is what every repository of
        fewer than 8 files uses, and what ``CRG_SERIAL_PARSE=1`` selects.
        """
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "repo", 12)
        env = _isolated_env(home, serial=True)
        _seed_empty_db(repo)

        with _LockHeld(_db_path(repo), 25.0, env):
            build = _crg("build", "--repo", str(repo), env=env)

        # Canary: contention really happened, and was not mistaken for a
        # parse failure this time.
        combined = build.stdout + build.stderr
        assert "database is locked" in combined
        assert "Error parsing" not in combined, combined[-2000:]

        assert build.returncode != 0, "a build that lost files exited successfully"
        db = _db_path(repo)
        assert _metadata(db, "git_head_sha") is None, (
            "the anchor was written despite the missing files"
        )

        # And the damage is repairable: with no anchor the next update is
        # promoted to a full rebuild.
        update = _crg("update", "--repo", str(repo), env=env)
        assert update.returncode == 0, update.stderr
        assert _counts(db)["files"] == 12
        assert _integrity(db) == "ok"
        assert _fts_integrity(db) == "ok"

    def test_serial_build_under_contention_should_not_lose_files(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "repo", 12)
        env = _isolated_env(home, serial=True)
        _seed_empty_db(repo)

        with _LockHeld(_db_path(repo), 25.0, env):
            build = _crg("build", "--repo", str(repo), env=env)

        db = _db_path(repo)
        if build.returncode != 0:
            # Failing loudly is an acceptable outcome; losing files quietly
            # while claiming success is not.
            assert _metadata(db, "git_head_sha") is None
            return
        assert _counts(db)["files"] == 12

    def test_concurrent_builds_do_not_corrupt_or_lose_files(
        self, tmp_path: Path
    ) -> None:
        """Three simultaneous full builds: intact database, nothing missing.

        This is ``build`` racing a daemon-started watcher's initial build,
        racing the PostToolUse hook — the shipped configuration, not a
        contrivance.
        """
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "repo", 60)
        env = _isolated_env(home)
        assert _crg("build", "--repo", str(repo), "-q", env=env).returncode == 0
        clean = _counts(_db_path(repo))
        assert clean["files"] == 60  # canary: the baseline really is the repo

        procs = [
            subprocess.Popen(
                [sys.executable, "-m", "code_review_graph", "build", "--repo", str(repo), "-q"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            for _ in range(3)
        ]
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=_PROC_TIMEOUT)
            results.append((proc.returncode, out, err))

        # No deadlock: every process terminated inside the timeout above.
        # A loser may legitimately die on the busy timeout; what it may not do
        # is leave the database broken or short of files.
        assert any(rc == 0 for rc, _, _ in results), results
        for rc, _out, err in results:
            if rc != 0:
                assert "database is locked" in err, err

        assert _integrity(_db_path(repo)) == "ok"
        assert _fts_integrity(_db_path(repo)) == "ok"
        after = _counts(_db_path(repo))
        assert after["files"] == clean["files"], "a concurrent build lost files"
        assert after["nodes"] == clean["nodes"]

    def test_queries_during_a_build_never_raise(self, tmp_path: Path) -> None:
        """A reader runs throughout a rebuild and must not see an error.

        WAL means readers do not block on the writer. This pins that the
        read-side CLI paths stay usable while the graph is being replaced
        underneath them.
        """
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "repo", 80)
        env = _isolated_env(home)
        assert _crg("build", "--repo", str(repo), "-q", env=env).returncode == 0

        failures: list[str] = []
        reads = {"n": 0}
        stop = threading.Event()

        def reader() -> None:
            while not stop.is_set():
                res = _crg("status", "--repo", str(repo), env=env)
                reads["n"] += 1
                if res.returncode != 0:
                    failures.append(res.stderr)
                    return

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            writer = _crg("build", "--repo", str(repo), "-q", env=env)
        finally:
            stop.set()
            thread.join(timeout=60)

        assert writer.returncode == 0, writer.stderr
        # Canary: the reader really ran concurrently with the writer.
        assert reads["n"] >= 2, f"reader only managed {reads['n']} query(ies)"
        assert failures == [], failures[0]

    def test_get_stats_holds_one_snapshot_across_its_statements(
        self, tmp_path: Path
    ) -> None:
        """A commit landing mid-``get_stats`` is invisible to the rest of it.

        ``GraphStore`` opens with ``isolation_level=None``, so without a read
        transaction each of ``get_stats``' six statements would get its own
        snapshot, and a writer committing between two of them — exactly what a
        watcher does while ``code-review-graph status`` runs — would make
        ``total_nodes`` disagree with the per-kind breakdown that is supposed
        to sum to it.

        The interleave is forced here rather than raced for, and it fires
        after the first read has pinned the snapshot, so every later statement
        must still see the pre-commit graph.
        """
        db = tmp_path / "graph.db"
        store = GraphStore(db)
        try:
            for i in range(20):
                store.store_file_nodes_edges(
                    f"src/mod{i}.py",
                    [
                        _node(f"src/mod{i}.py", "File", f"mod{i}.py"),
                        _node(f"src/mod{i}.py", "Function", f"f{i}"),
                    ],
                    [],
                    fhash=f"h{i}",
                )
            baseline = store.get_stats()
            # Canary: with no interference the invariant holds.
            assert sum(baseline.nodes_by_kind.values()) == baseline.total_nodes == 40

            writer = sqlite3.connect(str(db), timeout=30, isolation_level=None)
            writer.execute("PRAGMA busy_timeout=5000")
            fired = {"n": 0}

            def _delete() -> None:
                fired["n"] += 1
                writer.execute("DELETE FROM nodes WHERE file_path LIKE 'src/mod1%'")

            try:
                store._conn = _InterleavingConnection(
                    store._conn,
                    # 1 = BEGIN DEFERRED, 2 = the first COUNT, which pins the
                    # snapshot; the delete lands before statement 3.
                    after_statement=2,
                    action=_delete,
                )
                during = store.get_stats()
            finally:
                writer.close()
        finally:
            store.close()

        # Canary: the concurrent delete really was committed mid-read.
        assert fired["n"] == 1
        assert sum(during.nodes_by_kind.values()) == during.total_nodes == 40, (
            "a commit landing mid-read leaked into the later statements"
        )
        # And it really happened: a fresh read sees the smaller graph.
        after = GraphStore(db)
        try:
            assert after.get_stats().total_nodes == 18
        finally:
            after.close()

    def test_get_stats_should_be_internally_consistent(self, tmp_path: Path) -> None:
        db = tmp_path / "graph.db"
        store = GraphStore(db)
        try:
            for i in range(20):
                store.store_file_nodes_edges(
                    f"src/mod{i}.py",
                    [
                        _node(f"src/mod{i}.py", "File", f"mod{i}.py"),
                        _node(f"src/mod{i}.py", "Function", f"f{i}"),
                    ],
                    [],
                    fhash=f"h{i}",
                )
            writer = sqlite3.connect(str(db), timeout=30, isolation_level=None)
            writer.execute("PRAGMA busy_timeout=5000")
            try:
                store._conn = _InterleavingConnection(
                    store._conn,
                    after_statement=1,
                    action=lambda: writer.execute(
                        "DELETE FROM nodes WHERE file_path LIKE 'src/mod1%'"
                    ),
                )
                stats = store.get_stats()
            finally:
                writer.close()
        finally:
            store.close()

        assert sum(stats.nodes_by_kind.values()) == stats.total_nodes

    def test_concurrent_first_open_should_not_race_in_migrations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two openers, both past the column check before either alters.

        The barrier reproduces the real window deterministically instead of
        hoping the scheduler lands in it; against ten spawned processes on a
        fresh database it reproduces on its own roughly one run in five.
        """
        db = tmp_path / "graph.db"
        # A v1 database: schema in place, every migration still pending.
        # parent_name and extra are in the base CREATE TABLE and no migration
        # adds them, so a real v1 database carries both; the backfills from v13
        # on read them, and a seed without them would fail for that reason
        # rather than for the race this test is about.
        seed = sqlite3.connect(str(db))
        seed.executescript(
            "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "CREATE TABLE nodes (id INTEGER PRIMARY KEY, kind TEXT, name TEXT,"
            " qualified_name TEXT UNIQUE, file_path TEXT, parent_name TEXT,"
            " extra TEXT DEFAULT '{}', updated_at REAL);"
            "CREATE TABLE edges (id INTEGER PRIMARY KEY, kind TEXT,"
            " source_qualified TEXT, target_qualified TEXT, file_path TEXT,"
            " extra TEXT DEFAULT '{}', updated_at REAL);"
            "INSERT INTO metadata (key, value) VALUES ('schema_version', '1');"
        )
        seed.commit()
        seed.close()

        gate = threading.Barrier(2, timeout=30)
        real_has_column = migrations_mod._has_column
        seen: list[str] = []

        def gated_has_column(conn, table: str, column: str) -> bool:
            result = real_has_column(conn, table, column)
            if (table, column) == ("nodes", "signature"):
                seen.append("checked")
                gate.wait()  # both openers have now decided the column is absent
            return result

        monkeypatch.setattr(migrations_mod, "_has_column", gated_has_column)

        errors: list[BaseException] = []

        def open_store() -> None:
            try:
                conn = sqlite3.connect(str(db), timeout=30, isolation_level=None)
                conn.execute("PRAGMA busy_timeout=5000")
                try:
                    migrations_mod.run_migrations(conn)
                finally:
                    conn.close()
            except BaseException as exc:  # noqa: BLE001 - recorded, re-asserted
                errors.append(exc)

        threads = [threading.Thread(target=open_store) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        # Canary: both really reached the check-then-act window.
        assert len(seen) == 2, f"barrier never engaged both openers: {seen}"
        assert errors == [], f"{type(errors[0]).__name__}: {errors[0]}"

    def test_watcher_and_hook_updates_converge(self, tmp_path: Path) -> None:
        """The shipped hook (``update``) firing while the watcher is mid-update.

        ``hooks/hooks.json`` runs ``code-review-graph update --skip-flows`` on
        every Write/Edit/Bash. With a daemon running that is a second writer on
        the same database, for the same files, at the same moment — and a
        hand-run ``build`` makes a third.
        """
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "repo", 15)
        env = _isolated_env(home)
        assert _crg("build", "--repo", str(repo), "-q", env=env).returncode == 0

        watcher = subprocess.Popen(
            [sys.executable, "-m", "code_review_graph", "watch", "--repo", str(repo)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        try:
            health_dir = Path(env["CRG_HOME"]) / "watch-health"
            _wait_for_watching(health_dir)

            hook_failures: list[str] = []
            for i in range(8):
                (repo / "src" / f"hook{i}.py").write_text(
                    f"def hook{i}():\n    return {i}\n", encoding="utf-8"
                )
                res = _crg("update", "--repo", str(repo), "--skip-flows", "-q", env=env)
                if res.returncode != 0:
                    hook_failures.append(res.stderr)

            # And a hand-run full rebuild on top of both of them.
            by_hand = _crg("build", "--repo", str(repo), "-q", env=env)

            # Canary: the watcher really observed the churn.
            assert _wait_for(
                lambda: _read_health(health_dir).get("events_seen", 0) > 0
            ), _read_health(health_dir)
            assert watcher.poll() is None, "watcher died during concurrent updates"
            assert hook_failures == [], hook_failures[0]
            assert by_hand.returncode == 0, by_hand.stderr
        finally:
            watcher.send_signal(signal.SIGTERM)
            try:
                watcher.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover
                watcher.kill()
                watcher.wait(timeout=30)

        assert _integrity(_db_path(repo)) == "ok"
        assert _fts_integrity(_db_path(repo)) == "ok"
        # And a final reconciling update sees every file that exists.
        assert _crg("update", "--repo", str(repo), env=env).returncode == 0
        assert _counts(_db_path(repo))["files"] == 23


def _node(file_path: str, kind: str, name: str):
    from code_review_graph.parser import NodeInfo

    return NodeInfo(
        kind=kind,
        name=name,
        file_path=file_path,
        line_start=1,
        line_end=2,
        language="python",
    )


class _InterleavingConnection:
    """Wraps a connection and runs *action* before the *n*-th ``execute``.

    Stands in for a concurrent commit landing inside a multi-statement read.
    Only ``execute`` is intercepted; everything else is delegated, so the
    store behaves normally either side of the interleave.
    """

    def __init__(self, conn, after_statement: int, action) -> None:
        self._conn = conn
        self._after = after_statement
        self._action = action
        self._count = 0
        self._fired = False

    def execute(self, *args, **kwargs):
        self._count += 1
        if not self._fired and self._count > self._after:
            self._fired = True
            self._action()
        return self._conn.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


# ---------------------------------------------------------------------------
# 2. Interruption
# ---------------------------------------------------------------------------

# Runs a real build and SIGKILLs itself at a named point. SIGKILL, not an
# exception: an exception would let finally-blocks close the database
# cleanly, which is the opposite of what is being tested.
_KILL_RUNNER = '''
import os, signal, sys

point, repo = sys.argv[1], sys.argv[2]

def die():
    os.kill(os.getpid(), signal.SIGKILL)

import code_review_graph.incremental as inc
import code_review_graph.graph as graph_mod
import code_review_graph.search as search_mod

if point == "parse":
    original = inc.CodeParser.parse_bytes
    seen = {"n": 0}
    def patched(self, path, source):
        seen["n"] += 1
        if seen["n"] > 5:
            die()
        return original(self, path, source)
    inc.CodeParser.parse_bytes = patched
elif point == "mid_file_write":
    # Kill between a file's DELETEs and its INSERTs, driving the real
    # _replace_file_data. Only the surrounding BEGIN IMMEDIATE can put the
    # deleted rows back.
    original_init = graph_mod.GraphStore.__init__
    def init(self, db_path):
        original_init(self, db_path)
        real_conn = self._conn
        counter = {"n": 0}
        class _Killer:
            def execute(self, sql, *args, **kwargs):
                result = real_conn.execute(sql, *args, **kwargs)
                if sql.startswith("DELETE FROM edges WHERE file_path"):
                    counter["n"] += 1
                    if counter["n"] >= 6:
                        die()
                return result
            def __getattr__(self, name):
                return getattr(real_conn, name)
        self._conn = _Killer()
    graph_mod.GraphStore.__init__ = init
elif point == "edge_write":
    original = graph_mod.GraphStore._replace_file_data
    seen = {"n": 0}
    def patched(self, file_path, nodes, edges, fhash=""):
        seen["n"] += 1
        result = original(self, file_path, nodes, edges, fhash)
        if seen["n"] > 5:
            die()  # inside the open BEGIN IMMEDIATE, rows written, not committed
        return result
    graph_mod.GraphStore._replace_file_data = patched
elif point == "after_anchor":
    original = inc._store_vcs_metadata
    def patched(repo_root, store):
        result = original(repo_root, store)
        store.commit()
        die()  # every file stored and the anchor written; postprocess never runs
        return result
    inc._store_vcs_metadata = patched
elif point == "fts":
    # Drive the real rebuild_fts_index and kill it the instant its DROP has
    # run, so the test measures that function's own transaction handling
    # rather than a re-implementation of it.
    original = search_mod.rebuild_fts_index
    def patched(store):
        real_conn = store._conn
        class _Killer:
            def execute(self, sql, *args, **kwargs):
                result = real_conn.execute(sql, *args, **kwargs)
                if "DROP TABLE IF EXISTS nodes_fts" in sql:
                    die()
                return result
            def __getattr__(self, name):
                return getattr(real_conn, name)
        store._conn = _Killer()
        return original(store)
    search_mod.rebuild_fts_index = patched
else:
    raise SystemExit("unknown kill point: " + point)

from code_review_graph.tools.build import build_or_update_graph
build_or_update_graph(full_rebuild=True, repo_root=repo)
print("NOT KILLED")
'''


def _kill_during_build(point: str, repo: Path, env: dict[str, str]) -> int:
    proc = subprocess.run(
        [sys.executable, "-c", _KILL_RUNNER, point, str(repo)],
        capture_output=True,
        text=True,
        env=env,
        timeout=_PROC_TIMEOUT,
        check=False,
    )
    assert "NOT KILLED" not in proc.stdout, f"kill point {point!r} never fired"
    # Canary: killed by a signal, not a tidy exception with finally-blocks run.
    assert proc.returncode in (-signal.SIGKILL, 128 + signal.SIGKILL), (
        f"kill point {point!r} exited {proc.returncode}: {proc.stderr[-800:]}"
    )
    return proc.returncode


class TestInterruption:
    """A build killed mid-write, at four points."""

    @pytest.mark.parametrize("point", ["parse", "edge_write"])
    def test_kill_before_the_anchor_leaves_a_repairable_graph(
        self, tmp_path: Path, point: str
    ) -> None:
        """Killed early: intact, no anchor, and the next run rebuilds it all.

        ``full_build`` writes ``git_head_sha`` only after every file is
        stored. Without it ``resolve_incremental_base`` returns None and
        ``build_or_update_graph`` promotes the next update to a full rebuild.
        That is the one thing standing between an interrupted build and a
        permanently half-indexed repository.
        """
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "repo", 24)
        env = _isolated_env(home, serial=True)

        _kill_during_build(point, repo, env)
        db = _db_path(repo)
        assert db.exists()
        assert _integrity(db) == "ok"
        assert _metadata(db, "git_head_sha") is None
        partial = _counts(db)
        # Canary: it really did write something before dying, so the repair
        # below is repairing rather than building from nothing.
        assert partial["nodes"] > 0
        assert partial["files"] < 24

        repaired = _crg("update", "--repo", str(repo), env=env)
        assert repaired.returncode == 0, repaired.stderr
        after = _counts(db)
        assert after["files"] == 24
        assert after["nodes"] == 24 * 4
        assert _integrity(db) == "ok"
        assert _fts_integrity(db) == "ok"
        assert after["fts_hits"] > 0

    def test_kill_inside_an_open_transaction_discards_uncommitted_rows(
        self, tmp_path: Path
    ) -> None:
        """WAL rollback, not a torn file: whole files or nothing.

        Each source file contributes exactly four nodes, so a count that is
        not a multiple of four means a half-written file survived the kill.
        """
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "repo", 24)
        env = _isolated_env(home, serial=True)

        _kill_during_build("edge_write", repo, env)
        db = _db_path(repo)
        counts = _counts(db)
        assert _integrity(db) == "ok"
        assert counts["nodes"] > 0  # canary
        assert counts["nodes"] % 4 == 0, counts
        assert counts["nodes"] == counts["files"] * 4, counts

    def test_kill_between_a_files_delete_and_its_insert_restores_the_rows(
        self, tmp_path: Path
    ) -> None:
        """``_replace_file_data`` deletes before it inserts. Rollback must undo it.

        A rebuild is where this bites: the DELETE is destructive, and without
        the surrounding ``BEGIN IMMEDIATE`` a kill in the gap would leave the
        file's rows gone from an otherwise healthy graph — a file silently
        absent from search and impact analysis, with no error anywhere.
        """
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "repo", 24)
        env = _isolated_env(home, serial=True)
        assert _crg("build", "--repo", str(repo), "-q", env=env).returncode == 0
        db = _db_path(repo)
        before = _counts(db)
        assert before["files"] == 24 and before["nodes"] == 96  # canary

        _kill_during_build("mid_file_write", repo, env)

        assert _integrity(db) == "ok"
        after = _counts(db)
        assert after["files"] == 24, "a file lost its rows to the interrupted delete"
        assert after["nodes"] == 96, after

    def test_kill_during_the_fts_rebuild_keeps_the_index_table(
        self, tmp_path: Path
    ) -> None:
        """DROP + CREATE + rebuild is one transaction, so the table survives.

        Without the surrounding ``BEGIN IMMEDIATE`` the DROP would land and
        the CREATE would not, leaving a graph with no FTS table at all.
        """
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "repo", 24)
        env = _isolated_env(home, serial=True)
        assert _crg("build", "--repo", str(repo), "-q", env=env).returncode == 0
        db = _db_path(repo)
        assert _counts(db)["fts_hits"] > 0  # canary: an index existed first

        _kill_during_build("fts", repo, env)

        conn = _open_readonly(db)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            conn.close()
        assert "nodes_fts" in tables, "the FTS table was dropped and never recreated"
        assert _integrity(db) == "ok"
        assert _fts_integrity(db) == "ok"

        repaired = _crg("postprocess", "--repo", str(repo), env=env)
        assert repaired.returncode == 0, repaired.stderr
        assert _counts(db)["fts_hits"] > 0

    def test_kill_after_the_anchor_is_visible_as_an_incomplete_build(
        self, tmp_path: Path
    ) -> None:
        """A half-built graph now says so, instead of looking healthy.

        ``full_build`` stores ``last_updated`` and ``git_head_sha`` as soon as
        the last file is stored. A kill between that and post-processing
        leaves every node in place, the anchor current, an empty FTS index and
        no flows — a state freshness metadata cannot express, which is why
        ``status`` used to print a perfectly healthy graph. The build-state
        marker, written first and cleared last, is the evidence that it is
        not.
        """
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "repo", 24)
        env = _isolated_env(home, serial=True)

        _kill_during_build("after_anchor", repo, env)
        db = _db_path(repo)
        assert _integrity(db) == "ok"
        counts = _counts(db)
        # Canary: the nodes really are all there, so an empty index is not
        # just an empty graph.
        assert counts["files"] == 24
        assert counts["nodes"] == 24 * 4
        assert _metadata(db, "git_head_sha") is not None
        assert counts["fts_hits"] == 0
        assert counts["flows"] == 0
        # Every file was stored, so the marker records the narrower failure:
        # derived data outstanding, not files missing. Which one it is decides
        # whether a hand repair can finish the graph.
        assert _metadata(db, "build_state") == "postprocess-pending"

        status = _crg("status", "--repo", str(repo), env=env)
        assert status.returncode == 0
        assert "Nodes: 96" in status.stdout
        assert "INCOMPLETE" in status.stdout
        assert "update" in status.stdout

    def test_next_run_should_repair_a_build_killed_before_postprocessing(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "repo", 24)
        env = _isolated_env(home, serial=True)

        _kill_during_build("after_anchor", repo, env)
        db = _db_path(repo)
        assert _counts(db)["fts_hits"] == 0  # canary: the damage is present

        repaired = _crg("update", "--repo", str(repo), env=env)
        assert repaired.returncode == 0, repaired.stderr
        after = _counts(db)
        assert after["fts_hits"] > 0, "FTS index still empty after update"
        assert after["flows"] > 0, "flows still missing after update"

    def test_a_hand_repair_of_a_stored_graph_clears_the_marker(
        self, tmp_path: Path
    ) -> None:
        """``postprocess`` finishes the build it can finish, and says so.

        Killed after the last file was stored, the graph's contents are whole
        and only the derived data is missing. That is the one state
        ``code-review-graph postprocess`` exists to repair. Clearing the
        marker afterwards is not a courtesy: leaving it set would promote
        every later update to a full rebuild of a graph that is already right.
        """
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "repo", 24)
        env = _isolated_env(home, serial=True)

        _kill_during_build("after_anchor", repo, env)
        db = _db_path(repo)
        counts = _counts(db)
        # Canaries: every file landed, and the derived data really is missing,
        # so the repair below repairs something.
        assert counts["files"] == 24
        assert counts["nodes"] == 24 * 4
        assert counts["fts_hits"] == 0
        # The marker distinguishes this from a build that never stored the
        # graph, which is what makes the repair below legitimate.
        assert _metadata(db, "build_state") == "postprocess-pending"

        repaired = _crg("postprocess", "--repo", str(repo), env=env)
        assert repaired.returncode == 0, repaired.stderr
        after = _counts(db)
        assert after["fts_hits"] > 0, "FTS index still empty after postprocess"
        assert after["flows"] > 0, "flows still missing after postprocess"
        assert _metadata(db, "build_state") == "complete", (
            "a genuine repair could not clear the marker it exists to clear"
        )

        status = _crg("status", "--repo", str(repo), env=env)
        assert "INCOMPLETE" not in status.stdout

    def test_postprocess_cannot_clear_a_build_that_never_stored_the_graph(
        self, tmp_path: Path
    ) -> None:
        """The other direction: derived data over missing files is not health.

        Killed mid-parse, the graph holds a handful of files out of 24. Every
        post-processing stage reads the stored nodes, so all of them succeed
        and none of them can notice: flows, communities and a search index get
        rebuilt, correctly, for a graph that is missing most of the
        repository. Clearing the marker there hands back a half-built graph
        labelled healthy, which is the exact failure the marker was added to
        prevent, reached from the repair side.
        """
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "repo", 24)
        env = _isolated_env(home, serial=True)

        _kill_during_build("parse", repo, env)
        db = _db_path(repo)
        partial = _counts(db)
        # Canary: it stored something, and it is genuinely short of the repo.
        assert partial["nodes"] > 0
        assert 0 < partial["files"] < 24
        assert _metadata(db, "build_state") == "in-progress"

        ran = _crg("postprocess", "--repo", str(repo), env=env)
        assert ran.returncode == 0, ran.stderr
        assert _metadata(db, "build_state") == "in-progress", (
            "postprocess declared a graph with missing files complete"
        )
        # And it is not reported as a repair that worked.
        assert "INCOMPLETE" in ran.stdout, ran.stdout

        # The graph still says what it is, and the repair that works is still
        # the one offered.
        status = _crg("status", "--repo", str(repo), env=env)
        assert "INCOMPLETE" in status.stdout
        assert _counts(db)["files"] < 24

        rebuilt = _crg("build", "--repo", str(repo), env=env)
        assert rebuilt.returncode == 0, rebuilt.stderr
        assert _counts(db)["files"] == 24
        assert _metadata(db, "build_state") == "complete"


# ---------------------------------------------------------------------------
# 3. Daemon lifecycle
# ---------------------------------------------------------------------------


@pytest.fixture
def daemon_env(tmp_path: Path):
    """An isolated HOME/CRG_HOME plus teardown that kills anything left."""
    home = tmp_path / "daemon-home"
    env = _isolated_env(home)
    spawned: list[int] = []
    yield env, spawned
    _daemon_cli("stop", env=env)
    _reap(spawned)
    pid_file = Path(env["CRG_HOME"]) / "daemon.pid"
    if pid_file.exists():
        try:
            _reap([int(pid_file.read_text().strip())])
        except ValueError:  # pragma: no cover
            pass


def _daemon_pid(env: dict[str, str]) -> int | None:
    pid_file = Path(env["CRG_HOME"]) / "daemon.pid"
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except ValueError:  # pragma: no cover
        return None


def _child_pids(env: dict[str, str]) -> dict[str, int]:
    state_file = Path(env["CRG_HOME"]) / "daemon-state.json"
    if not state_file.exists():
        return {}
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:  # pragma: no cover
        return {}
    return {alias: entry["pid"] for alias, entry in state.items() if entry.get("pid")}


class TestDaemonLifecycle:
    """start, status, reconcile, stop, and a crash in between."""

    def test_start_status_reconcile_stop(self, tmp_path: Path, daemon_env) -> None:
        env, spawned = daemon_env
        repo_a = _make_repo(tmp_path / "repo-a", 10)
        repo_b = _make_repo(tmp_path / "repo-b", 10)

        assert _daemon_cli("add", str(repo_a), "--alias", "a", env=env).returncode == 0
        assert _daemon_cli("start", env=env).returncode == 0

        assert _wait_for(lambda: bool(_child_pids(env).get("a")))
        pid = _daemon_pid(env)
        assert pid is not None and _alive(pid), "PID file does not name a live daemon"
        child_a = _child_pids(env)["a"]
        spawned.extend([pid, child_a])
        assert _alive(child_a)

        status = _daemon_cli("status", env=env)
        assert f"running (PID {pid})" in status.stdout
        assert "a " in status.stdout and "alive" in status.stdout

        # Reconcile: adding a repo edits watch.toml, which the daemon watches.
        assert _daemon_cli("add", str(repo_b), "--alias", "b", env=env).returncode == 0
        assert _wait_for(lambda: bool(_child_pids(env).get("b")))
        child_b = _child_pids(env)["b"]
        spawned.append(child_b)
        assert _alive(child_b)
        assert _child_pids(env)["a"] == child_a, "reconcile restarted an unchanged repo"

        # Reconcile the other way: removing a repo must stop its child.
        assert _daemon_cli("remove", "a", env=env).returncode == 0
        assert _wait_for(lambda: not _alive(child_a)), "removed repo's watcher lives on"
        assert "a" not in _child_pids(env)

        stop = _daemon_cli("stop", env=env)
        assert stop.returncode == 0, stop.stdout + stop.stderr
        assert _wait_for(lambda: not _alive(pid))
        assert _wait_for(lambda: not _alive(child_b)), "stop left a watcher running"
        assert _daemon_pid(env) is None, "PID file survived a clean stop"
        assert not (Path(env["CRG_HOME"]) / "daemon-state.json").exists()

    def test_stale_pid_file_from_a_dead_process_is_cleared(self, tmp_path: Path) -> None:
        """The easy half of stale-PID handling, and it works."""
        crg_home = tmp_path / "state"
        crg_home.mkdir()
        pid_file = crg_home / "daemon.pid"

        victim = subprocess.Popen([sys.executable, "-c", "pass"])
        victim.wait(timeout=30)
        dead_pid = victim.pid
        assert not _alive(dead_pid)  # canary: really dead before we claim it is

        pid_file.write_text(str(dead_pid), encoding="utf-8")
        assert daemon_mod.is_daemon_running(pid_file) is False
        assert not pid_file.exists(), "a stale PID file was left on disk"

    def test_recycled_pid_is_not_mistaken_for_the_daemon(
        self, tmp_path: Path
    ) -> None:
        """A live PID in the file is no longer enough to read as 'the daemon'.

        ``is_daemon_running`` used to ask only whether *a* process with that
        PID exists. PIDs are recycled, so after a reboot or a wrap-around the
        file can name something else entirely; ``start`` then refused to run
        and ``stop`` signalled the stranger. The daemon now also holds an
        exclusive lock for its lifetime, which the kernel releases the moment
        it dies, so a PID file with no lock behind it is stale by definition.
        """
        env = _isolated_env(tmp_path / "home")
        pid_file = Path(env["CRG_HOME"]) / "daemon.pid"

        victim = _spawn_detached(sys.executable, "-c", "import time; time.sleep(120)")
        try:
            assert _alive(victim)  # canary: a live, unrelated process
            pid_file.write_text(str(victim), encoding="utf-8")

            status = _daemon_cli("status", env=env)
            assert "not running" in status.stdout
            assert str(victim) not in status.stdout

            stop = _daemon_cli("stop", env=env)
            assert stop.returncode == 1
            assert "not running" in stop.stdout
            assert _alive(victim), "the CLI signalled an unrelated process"

            # And the stale file is cleared rather than blocking a start.
            assert not pid_file.exists()
        finally:
            _reap([victim])

    def test_recycled_pid_should_not_be_adopted(self, tmp_path: Path) -> None:
        env = _isolated_env(tmp_path / "home")
        pid_file = Path(env["CRG_HOME"]) / "daemon.pid"

        victim = _spawn_detached(sys.executable, "-c", "import time; time.sleep(60)")
        try:
            pid_file.write_text(str(victim), encoding="utf-8")
            _daemon_cli("stop", env=env)
            assert _alive(victim), "the CLI killed an unrelated process"
        finally:
            _reap([victim])

    def test_daemon_crash_leaves_watchers_that_status_and_start_handle(
        self, tmp_path: Path, daemon_env
    ) -> None:
        """A crashed daemon's watchers are visible, reapable, and never doubled.

        SIGKILL the daemon — an OOM kill, a crash, ``kill -9`` — and its
        watcher children survive, because they are plain ``subprocess.Popen``
        children in its session. ``status`` used to report "not running" and
        list nothing while a live watcher kept writing the graph, ``stop``
        exited 1 without touching it, and a fresh ``start`` spawned a *second*
        watcher for the same repository: two writers on one database, one of
        them invisible.
        """
        env, spawned = daemon_env
        repo = _make_repo(tmp_path / "repo", 10)
        assert _daemon_cli("add", str(repo), "--alias", "r", env=env).returncode == 0
        assert _daemon_cli("start", env=env).returncode == 0
        assert _wait_for(lambda: bool(_child_pids(env).get("r")))

        pid = _daemon_pid(env)
        orphan = _child_pids(env)["r"]
        spawned.extend([pid, orphan])
        assert pid is not None and _alive(pid) and _alive(orphan)  # canary
        _wait_for_watching(Path(env["CRG_HOME"]) / "watch-health")

        os.kill(pid, signal.SIGKILL)
        assert _wait_for(lambda: not _alive(pid))
        assert _alive(orphan), "child died with the daemon (behaviour changed)"

        status = _daemon_cli("status", env=env)
        assert "not running" in status.stdout
        assert str(orphan) in status.stdout, "status hides the orphaned watcher"
        assert "orphan" in status.stdout

        # `start` reaps before it spawns, so exactly one watcher exists after.
        assert _daemon_cli("start", env=env).returncode == 0
        assert _wait_for(lambda: not _alive(orphan)), "start left the orphan running"
        assert _wait_for(lambda: bool(_child_pids(env).get("r")))
        replacement = _child_pids(env)["r"]
        spawned.extend([_daemon_pid(env), replacement])
        assert replacement != orphan
        assert _alive(replacement)

    def test_crashed_daemon_children_should_not_survive(
        self, tmp_path: Path, daemon_env
    ) -> None:
        env, spawned = daemon_env
        repo = _make_repo(tmp_path / "repo", 10)
        assert _daemon_cli("add", str(repo), "--alias", "r", env=env).returncode == 0
        assert _daemon_cli("start", env=env).returncode == 0
        assert _wait_for(lambda: bool(_child_pids(env).get("r")))

        pid = _daemon_pid(env)
        child = _child_pids(env)["r"]
        spawned.extend([pid, child])
        os.kill(pid, signal.SIGKILL)
        assert _wait_for(lambda: not _alive(pid))

        # Either the child goes with it, or `stop` cleans it up afterwards.
        _daemon_cli("stop", env=env)
        assert _wait_for(lambda: not _alive(child), timeout=15), (
            "watcher orphaned by the daemon crash is still running"
        )

    def test_dead_child_is_restarted_and_state_file_follows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A watcher killed under a live daemon comes back, and state tracks it.

        Driven in-process so the 30-second health interval can be shortened;
        the code path (``_check_health`` -> ``_start_watcher`` ->
        ``_save_state``) is the one the packaged daemon runs.
        """
        repo = _make_repo(tmp_path / "repo", 8)
        monkeypatch.setattr(daemon_mod, "_HEALTH_CHECK_INTERVAL", 1)
        monkeypatch.setattr(daemon_mod, "_RESTART_BACKOFF_BASE", 0.1)

        config = daemon_mod.DaemonConfig(
            repos=[daemon_mod.WatchRepo(path=str(repo), alias="r")],
            log_dir=tmp_path / "logs",
        )
        daemon = daemon_mod.WatchDaemon(config=config, config_path=tmp_path / "watch.toml")
        spawned: list[int] = []
        try:
            daemon.start()
            first = _child_pids_from(daemon)
            spawned.append(first)
            assert _alive(first)  # canary
            assert daemon.status()["repos"][0]["pid"] == first

            os.kill(first, signal.SIGKILL)
            assert _wait_for(lambda: not _alive(first))
            assert _wait_for(lambda: _child_pids_from(daemon) not in (None, first))

            second = _child_pids_from(daemon)
            spawned.append(second)
            assert second != first
            assert _alive(second)
            assert daemon.status()["repos"][0]["pid"] == second
            assert daemon.restart_count("r") >= 1
        finally:
            daemon.stop()
            _reap(spawned)

        assert _wait_for(lambda: not any(_alive(pid) for pid in spawned))


def _child_pids_from(daemon: daemon_mod.WatchDaemon) -> int | None:
    proc = daemon._children.get("r")
    return None if proc is None else proc.pid


# ---------------------------------------------------------------------------
# 4. Watcher under stress
# ---------------------------------------------------------------------------


class _ExhaustedObserver:
    """An observer whose every schedule fails the way inotify does at ENOSPC."""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def schedule(self, _handler, path: str, recursive: bool = False):
        self.attempts.append(path)
        raise OSError(28, "inotify watch limit reached")

    def unschedule(self, _handle) -> None:  # pragma: no cover - never reached
        raise AssertionError("nothing was ever scheduled")


class _WorkingObserver:
    """Minimal observer that records what it was asked to watch."""

    def __init__(self) -> None:
        self.scheduled: list[str] = []

    def schedule(self, _handler, path: str, recursive: bool = False):
        self.scheduled.append(path)
        return object()

    def unschedule(self, _handle) -> None:
        pass


class TestWatcherUnderStress:
    """Rapid churn, a vanished directory, and an exhausted watch budget."""

    def _start_watcher(self, repo: Path, env: dict[str, str]):
        proc = subprocess.Popen(
            [sys.executable, "-m", "code_review_graph", "watch", "--repo", str(repo)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        health_dir = Path(env["CRG_HOME"]) / "watch-health"
        _wait_for_watching(health_dir)
        return proc, health_dir

    @staticmethod
    def _stop(proc: subprocess.Popen) -> str:
        proc.send_signal(signal.SIGTERM)
        try:
            out, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
            out, _ = proc.communicate(timeout=30)
        return out or ""

    @staticmethod
    def _health(health_dir: Path) -> dict:
        return _read_health(health_dir)

    @staticmethod
    def _graph_files(repo: Path) -> set[str]:
        conn = _open_readonly(_db_path(repo))
        try:
            rows = conn.execute(
                "SELECT DISTINCT file_path FROM nodes WHERE kind = 'File'"
            ).fetchall()
        finally:
            conn.close()
        out = set()
        for row in rows:
            path = str(row[0])
            marker = f"/{repo.name}/"
            out.add(path.split(marker, 1)[1] if marker in path else path)
        return out

    @staticmethod
    def _disk_files(repo: Path) -> set[str]:
        return {
            path.relative_to(repo).as_posix()
            for path in (repo / "src").rglob("*.py")
        }

    def test_rapid_create_modify_delete_rename_converges(self, tmp_path: Path) -> None:
        """Forty creations, twenty deletions and a rename, then convergence.

        The assertion is set equality against the filesystem, so a watcher
        that quietly stopped after the first batch fails here rather than
        passing on "no exception raised".
        """
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "repo", 10)
        env = _isolated_env(home)
        assert _crg("build", "--repo", str(repo), "-q", env=env).returncode == 0

        before = self._graph_files(repo)
        assert before == {f"src/mod{i}.py" for i in range(10)}  # canary

        proc, health_dir = self._start_watcher(repo, env)
        try:
            src = repo / "src"
            for i in range(40):
                (src / f"new{i}.py").write_text(f"def n{i}(): return {i}\n", encoding="utf-8")
            for i in range(20):
                (src / f"new{i}.py").unlink()
            for i in range(20, 30):  # modify ten survivors
                (src / f"new{i}.py").write_text(
                    f"def n{i}(): return {i} * 2\n", encoding="utf-8"
                )
            (src / "mod0.py").rename(src / "renamed0.py")

            expected = self._disk_files(repo)
            # Canary: the churn really reshaped the tree, so convergence is a
            # claim about the watcher rather than about nothing having moved.
            assert len(expected) == 30, sorted(expected)
            assert expected != before
            assert _wait_for(
                lambda: self._graph_files(repo) == expected, timeout=60
            ), (
                f"graph never converged; missing="
                f"{sorted(expected - self._graph_files(repo))} "
                f"extra={sorted(self._graph_files(repo) - expected)}"
            )
            # Canary: the watcher observed real events and still calls itself
            # live. Health is republished on a timer, so poll rather than
            # read once.
            assert _wait_for(
                lambda: self._health(health_dir).get("events_seen", 0) > 0
            ), self._health(health_dir)
            health = self._health(health_dir)
            assert health["observer_alive"] is True
            assert health["dead_threads"] == []
            assert proc.poll() is None, "watcher exited during churn"
        finally:
            output = self._stop(proc)
        assert "Traceback" not in output, output[-2000:]
        assert _integrity(_db_path(repo)) == "ok"

    def test_deleting_and_recreating_a_watched_directory(self, tmp_path: Path) -> None:
        """``rm -rf lib && mkdir lib`` must not blind the watcher.

        The replacement directory carries a different inode behind the same
        name, so the watch scheduled on the old one is dead. A watcher that
        calls that a dead thread exits and gets restarted every 30s; one that
        ignores it never indexes the new contents and keeps reporting healthy.
        Neither is acceptable, and only re-adoption gets it right.

        The repository is shaped so the root is watched *non-recursively* — a
        heavy ignored ``node_modules`` makes the planner split it — because
        that is the only shape in which a new top-level directory has to be
        adopted rather than being covered by a recursive parent watch.
        """
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "repo", 10)
        for i in range(8):  # heavy enough to be worth excluding
            pkg_dir = repo / "node_modules" / f"p{i}"
            pkg_dir.mkdir(parents=True)
            (pkg_dir / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
        lib = repo / "lib"
        lib.mkdir()
        (lib / "before.py").write_text("def before(): pass\n", encoding="utf-8")
        # ``build`` indexes tracked files, so lib/ has to be committed before
        # the baseline; the watcher picks up untracked churn itself.
        _git_commit_all(repo)
        env = _isolated_env(home)
        assert _crg("build", "--repo", str(repo), "-q", env=env).returncode == 0
        assert "lib/before.py" in self._graph_files(repo)  # canary

        proc, health_dir = self._start_watcher(repo, env)
        try:
            # Canary: the root really is split, so adoption is really needed.
            health = self._health(health_dir)
            assert health["watched_paths"] > 1, health

            for path in lib.iterdir():
                path.unlink()
            lib.rmdir()
            assert _wait_for(
                lambda: "lib/before.py" not in self._graph_files(repo), timeout=60
            ), "deleted directory's files never left the graph"

            lib.mkdir()
            (lib / "after.py").write_text("def after(): pass\n", encoding="utf-8")
            assert _wait_for(
                lambda: "lib/after.py" in self._graph_files(repo), timeout=60
            ), "recreated directory was never indexed — the watcher went blind"

            assert proc.poll() is None, "watcher exited on directory replacement"
            assert _wait_for(
                lambda: self._health(health_dir).get("events_seen", 0) > 0
            ), self._health(health_dir)
            assert self._health(health_dir)["observer_alive"] is True
        finally:
            output = self._stop(proc)
        assert "Traceback" not in output, output[-2000:]

    def test_exhausted_watch_budget_leaves_nothing_watched(self, tmp_path: Path) -> None:
        """An exhausted watch budget: nothing watched, and it says so.

        ``_WatchSupervisor._schedule`` catches ``OSError`` from
        ``observer.schedule`` — the inotify ENOSPC that issue #811 is about.
        It used to log a warning and return, recording no watch and leaving
        ``degraded`` False, so the supervisor watched nothing while
        ``report_health`` published ``observer_alive: true, degraded: false``
        and ``crg-daemon status`` printed ``ok``. The refused directories are
        now tracked, which is what the health file and ``watcher_status``
        read.

        ``check_liveness`` still reports nothing, and deliberately so: there
        are no threads to find. Total blindness is caught by the watch loop
        instead, which re-attempts the plan and then exits.
        """
        repo = _make_repo(tmp_path / "repo", 6)
        observer = _ExhaustedObserver()
        health_path = tmp_path / "health.json"
        supervisor = _WatchSupervisor(
            observer, repo, [], health_path=health_path, max_schedules=8
        )
        supervisor.schedule_initial(handler=object())

        # Canary: it really tried to watch something and really was refused.
        assert observer.attempts, "schedule_initial planned no watches at all"
        assert supervisor.watched_paths == []

        supervisor.report_health(observer_alive=True, force=True)
        health = json.loads(health_path.read_text(encoding="utf-8"))
        assert health["watched_paths"] == 0
        assert health["observer_alive"] is True
        assert health["degraded"] is True
        assert health["dead_threads"] == []
        assert daemon_mod.watcher_status(True, health) == "partial"
        assert supervisor.unwatched_paths == sorted(set(observer.attempts))

        # No threads died, so the liveness check has nothing to report.
        dead, repaired = supervisor.check_liveness()
        assert dead == [] and repaired == []

        # Recovery is attempted before the loop gives up, and reports honestly.
        assert supervisor.rewatch_all() is False
        supervisor._observer = _WorkingObserver()
        assert supervisor.rewatch_all() is True
        assert supervisor.watched_paths
        assert supervisor.degraded is False

    def test_exhausted_watch_budget_should_be_visible(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path / "repo", 6)
        observer = _ExhaustedObserver()
        health_path = tmp_path / "health.json"
        supervisor = _WatchSupervisor(
            observer, repo, [], health_path=health_path, max_schedules=8
        )
        supervisor.schedule_initial(handler=object())
        supervisor.report_health(observer_alive=True, force=True)
        health = json.loads(health_path.read_text(encoding="utf-8"))

        assert supervisor.degraded is True
        assert daemon_mod.watcher_status(True, health) != "ok"

    def test_partial_schedule_failure_is_reported(self, tmp_path: Path) -> None:
        """A directory adopted mid-run that fails to register says so.

        ``_adopt_directory`` logs "Watching new directory X (n watch(es))" and
        returns True whenever ``required`` is False, whether or not
        ``_schedule`` actually took. ``required`` is only True for a directory
        that a changed ignore rule newly included, so the ordinary case — a
        new package appearing under a non-recursive watch — used to lose
        coverage without a trace. Same root cause as the exhausted budget
        above, and the same fix: a refused schedule is recorded.
        """
        repo = _make_repo(tmp_path / "repo", 6)
        observer = _WorkingObserver()
        supervisor = _WatchSupervisor(observer, repo, [], max_schedules=8)
        supervisor.schedule_initial(handler=object())
        assert supervisor.watched_paths, "nothing was watched to begin with"  # canary

        new_dir = repo / "src" / "late"
        new_dir.mkdir()
        supervisor._observer = _ExhaustedObserver()
        adopted = supervisor._adopt_directory(str(new_dir))

        # Adoption still succeeds, because the directory's current contents
        # must be indexed either way; what it no longer does is hide that the
        # watch behind it was refused.
        assert adopted is True
        assert str(new_dir) not in supervisor.watched_paths
        assert supervisor.degraded is True
        assert str(new_dir) in supervisor.unwatched_paths


# ---------------------------------------------------------------------------
# 5. Telling a lock timeout apart from a genuine failure
# ---------------------------------------------------------------------------


# Poison one post-processing stage with a chosen sqlite3.OperationalError and
# report the whole build result as JSON. An error constructed in Python carries
# no ``sqlite_errorcode`` (the sqlite3 module sets that only on errors it
# raises itself, and only from 3.11), so this drives the message fallback that
# Python 3.10 relies on for every classification.
_POISONED_BUILD = """
import json, sqlite3, sys
from code_review_graph.graph import GraphStore
from code_review_graph.tools.build import build_or_update_graph

repo, message = sys.argv[1], sys.argv[2]


def boom(self, *args, **kwargs):
    raise sqlite3.OperationalError(message)


GraphStore.update_node_signatures = boom
print("RESULT " + json.dumps(build_or_update_graph(
    full_rebuild=True, repo_root=repo, postprocess="full",
)))
"""

# The same poison, but through the hand-repair entry point.
_POISONED_POSTPROCESS = """
import json, sqlite3, sys
from code_review_graph.graph import GraphStore
from code_review_graph.tools.build import run_postprocess

repo, message = sys.argv[1], sys.argv[2]


def boom(self, *args, **kwargs):
    raise sqlite3.OperationalError(message)


GraphStore.resolve_bare_call_targets = boom
print("RESULT " + json.dumps(run_postprocess(repo_root=repo)))
"""


def _run_poisoned(script: str, repo: Path, message: str, env: dict[str, str]) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", script, str(repo), message],
        capture_output=True,
        text=True,
        env=env,
        timeout=_PROC_TIMEOUT,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")), None
    )
    assert line is not None, proc.stdout[-2000:]
    return json.loads(line[len("RESULT "):])


class TestErrorClassification:
    """A lock timeout is transient; every other SQLite error is not."""

    def test_only_a_lock_timeout_counts_as_contention(self, tmp_path: Path) -> None:
        """``is_lock_contention`` separates SQLITE_BUSY from a real failure.

        The two are indistinguishable by type — both arrive as
        ``sqlite3.OperationalError`` — and treating the whole class as
        contention is what pinned a graph as incomplete forever: the
        build-state marker is deliberately left set for contention so the next
        run redoes the lost stage, and an error that will recur every run can
        never clear it.

        Both errors here are raised by SQLite itself, so on Python 3.11+ this
        exercises ``sqlite_errorcode``; the message fallback that 3.10 needs is
        covered by the poisoned builds below.
        """
        from code_review_graph.tools.build import is_lock_contention

        db = tmp_path / "probe.db"
        holder = sqlite3.connect(str(db), timeout=0.1, isolation_level=None)
        victim = sqlite3.connect(str(db), timeout=0.1, isolation_level=None)
        try:
            holder.execute("CREATE TABLE t (x)")
            holder.execute("BEGIN EXCLUSIVE")
            with pytest.raises(sqlite3.OperationalError) as busy:
                victim.execute("INSERT INTO t VALUES (1)")
            holder.rollback()
            with pytest.raises(sqlite3.OperationalError) as malformed:
                victim.execute("SELECT * FROM no_such_table")
        finally:
            holder.close()
            victim.close()

        # Canary: SQLite really produced two different failures, not one.
        assert "locked" in str(busy.value)
        assert "no such table" in str(malformed.value)

        assert is_lock_contention(busy.value) is True
        assert is_lock_contention(malformed.value) is False
        # A disk problem and a read-only file are failures, not contention.
        assert is_lock_contention(sqlite3.OperationalError("disk I/O error")) is False
        assert is_lock_contention(
            sqlite3.OperationalError("attempt to write a readonly database")
        ) is False
        assert is_lock_contention(ImportError("leidenalg")) is False

    def test_a_genuine_postprocess_error_does_not_pin_the_graph_incomplete(
        self, tmp_path: Path
    ) -> None:
        """A malformed statement clears the marker; a lock timeout keeps it.

        ``build_state`` is written before the first node and cleared after the
        last post-processing stage, and contention deliberately leaves it set:
        the next run then redoes the stage it lost. Classifying *every*
        ``sqlite3.OperationalError`` as contention turned that recovery into a
        trap — a malformed statement or a failing disk kept the marker set on
        every run, so the graph was marked incomplete forever and every later
        update was promoted to a full rebuild that could not clear it.
        """
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "repo", 6)
        env = _isolated_env(home)
        db = _db_path(repo)

        failed = _run_poisoned(_POISONED_BUILD, repo, "no such column: bogus", env)
        assert any("Signature computation failed" in w for w in failed["warnings"])
        assert _metadata(db, "build_state") == "complete", (
            "a permanent SQLite error left the graph marked half built"
        )
        # And it is not passed off as a clean build.
        assert failed["status"] == "partial"
        assert failed.get("postprocess_contended") is not True

        # The contrast, on the same code path: real contention still holds the
        # marker, which is the behaviour this must not have broken.
        contended = _run_poisoned(_POISONED_BUILD, repo, "database is locked", env)
        assert contended["postprocess_contended"] is True
        # The files were all stored before the poisoned stage ran, so the
        # marker holds at the post-processing state rather than claiming the
        # graph is missing files.
        assert _metadata(db, "build_state") == "postprocess-pending"

    def test_hand_repair_clears_the_marker_despite_a_warning(
        self, tmp_path: Path
    ) -> None:
        """``postprocess`` is the hand repair, so a warning must not block it.

        Clearing the marker only on an empty warning list meant the command
        people reach for to repair a half-built graph could not repair it in
        exactly the cases that produce warnings — a missing optional extra, a
        stage that failed for its own reasons. Only genuine contention holds
        the marker now, because only contention is worth redoing.
        """
        home = tmp_path / "home"
        repo = _make_repo(tmp_path / "repo", 6)
        env = _isolated_env(home)
        db = _db_path(repo)
        assert _crg("build", "--repo", str(repo), env=env).returncode == 0
        # The state a build that stored every file and then died leaves: the
        # graph's contents are whole, its derived data is not. That is the
        # state this command exists to clear.
        _set_metadata(db, "build_state", "postprocess-pending")
        assert _metadata(db, "build_state") == "postprocess-pending"  # canary

        result = _run_poisoned(
            _POISONED_POSTPROCESS, repo, "no such column: bogus", env
        )
        assert any("Call-target resolution failed" in w for w in result["warnings"])
        assert _metadata(db, "build_state") == "complete", (
            "a hand repair could not clear the marker it exists to clear"
        )

        # Contention is still the one reason to leave the repair unfinished.
        _set_metadata(db, "build_state", "postprocess-pending")
        contended = _run_poisoned(
            _POISONED_POSTPROCESS, repo, "database is locked", env
        )
        assert contended["warnings"]
        assert _metadata(db, "build_state") == "postprocess-pending"


# ---------------------------------------------------------------------------
# 6. Two daemons starting at once
# ---------------------------------------------------------------------------


# Holds the daemon lock without writing a PID file: exactly the window a second
# ``crg-daemon start`` used to slip through, between one daemon forking and
# that daemon labelling its lock.
_LOCK_SQUATTER = """
import sys, time
from code_review_graph.daemon import acquire_daemon_lock
assert acquire_daemon_lock(), "squatter could not take the lock"
print("HELD", flush=True)
time.sleep(float(sys.argv[1]))
"""

# Takes the lock the way the daemon does, PID file and all.
_PID_HOLDER = """
import sys, time
from pathlib import Path
from code_review_graph.daemon import write_pid
write_pid(path=Path(sys.argv[1]))
print("HELD", flush=True)
time.sleep(float(sys.argv[2]))
"""


class _HeldLock:
    """Context manager owning a process that holds the daemon lock."""

    def __init__(self, script: str, *args: str, env: dict[str, str]) -> None:
        self._cmd = [sys.executable, "-c", script, *args]
        self._env = env
        self.proc: subprocess.Popen[str] | None = None

    def __enter__(self) -> "_HeldLock":
        self.proc = subprocess.Popen(
            self._cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._env,
        )
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline().strip()
        assert line == "HELD", f"holder never took the daemon lock: {line!r}"
        return self

    @property
    def pid(self) -> int:
        assert self.proc is not None
        return self.proc.pid

    def __exit__(self, *_exc) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
        if self.proc is not None:
            self.proc.wait(timeout=30)


class TestDaemonExclusion:
    """The daemon lock has to exclude a second daemon, not just label one."""

    def test_write_pid_refuses_when_another_process_holds_the_lock(
        self, tmp_path: Path, daemon_env
    ) -> None:
        """A second claim is refused, and says whose it is.

        ``write_pid`` took the lock and discarded the answer, so the lock
        identified the daemon but excluded nothing: the second caller
        overwrote the PID file and carried on, leaving one of the two daemons
        invisible to every later ``status`` and ``stop``.
        """
        env, _spawned = daemon_env
        pid_path = Path(env["CRG_HOME"]) / "daemon.pid"

        with _HeldLock(_PID_HOLDER, str(pid_path), "120", env=env) as holder:
            assert pid_path.read_text(encoding="utf-8").strip() == str(holder.pid)

            with pytest.raises(daemon_mod.DaemonAlreadyRunningError) as refused:
                daemon_mod.write_pid(path=pid_path)

            # It names the holder, so the message is actionable.
            assert str(holder.pid) in str(refused.value)
            assert str(daemon_mod.daemon_lock_path(pid_path)) in str(refused.value)
            assert refused.value.pid == holder.pid
            # And the refusal left the holder's label untouched.
            assert pid_path.read_text(encoding="utf-8").strip() == str(holder.pid)

    def test_second_start_is_refused_while_the_lock_is_held(
        self, tmp_path: Path, daemon_env
    ) -> None:
        """Two ``crg-daemon start`` calls at once: one daemon, not two.

        ``is_daemon_running`` is a read of the PID file, and a daemon that has
        forked but not yet written that file is invisible to it — the same
        window two simultaneous starts race through. The lock is what decides,
        so the start that loses it stops, names the winner, and exits non-zero
        instead of forking a second daemon onto the same repositories.
        """
        env, spawned = daemon_env

        with _HeldLock(_LOCK_SQUATTER, "120", env=env):
            pid_file = Path(env["CRG_HOME"]) / "daemon.pid"
            # Canary: the PID file really is absent, so `is_daemon_running`
            # cannot be what refuses this start.
            assert not pid_file.exists()

            started = _daemon_cli("start", env=env)
            output = started.stdout + started.stderr
            if started.returncode == 0 and _daemon_pid(env) is not None:
                spawned.append(_daemon_pid(env))  # pragma: no cover - failure path

            assert started.returncode != 0, output
            assert "already running" in output.lower(), output
            assert not pid_file.exists(), "a refused start still labelled the lock"


# ---------------------------------------------------------------------------
# 7. The refused-watch record stays bounded
# ---------------------------------------------------------------------------


class TestUnwatchedRecord:
    """What the supervisor remembers about directories it could not watch."""

    def test_refusals_for_vanished_directories_are_forgotten(
        self, tmp_path: Path
    ) -> None:
        """Churned build directories must not accumulate for the daemon's life.

        A refused directory never enters ``_watches``, so neither a successful
        reschedule nor ``_release_directory`` ever reaches it. A tree that is
        created and deleted on every build therefore left one entry per run:
        ``degraded`` stayed true forever over directories that no longer
        exist, and every health file published the growing list.
        """
        repo = _make_repo(tmp_path / "repo", 6)
        observer = _WorkingObserver()
        supervisor = _WatchSupervisor(observer, repo, [], max_schedules=512)
        supervisor.schedule_initial(handler=object())
        assert supervisor.watched_paths, "nothing was watched to begin with"
        assert supervisor.degraded is False  # canary: a clean start

        churned = [repo / "src" / f"build{i}" for i in range(40)]
        supervisor._observer = _ExhaustedObserver()
        for directory in churned:
            directory.mkdir()
            supervisor._adopt_directory(str(directory))
        # Canary: every refusal really was recorded, which is the behaviour
        # that must survive.
        assert {str(d) for d in churned} <= set(supervisor.unwatched_paths)
        assert supervisor.degraded is True

        for directory in churned:
            directory.rmdir()
        supervisor._observer = observer
        supervisor.sync_watches()

        assert [p for p in supervisor.unwatched_paths if p.startswith(
            str(repo / "src" / "build")
        )] == [], supervisor.unwatched_paths
        assert supervisor.degraded is False

    def test_the_record_is_capped_even_if_nothing_is_ever_pruned(
        self, tmp_path: Path
    ) -> None:
        """A cap bounds the record between reconciliations.

        Pruning runs once a tick; the cap is what keeps a burst inside one tick
        from growing the health payload without limit. The newest refusals are
        the ones kept, because they are the ones that still describe the tree.
        """
        from code_review_graph.incremental import _MAX_UNWATCHED_TRACKED

        repo = _make_repo(tmp_path / "repo", 6)
        supervisor = _WatchSupervisor(_WorkingObserver(), repo, [], max_schedules=8)
        total = _MAX_UNWATCHED_TRACKED + 25
        for i in range(total):
            supervisor._note_unwatched(str(repo / "src" / f"gone{i:05d}"))

        assert len(supervisor.unwatched_paths) == _MAX_UNWATCHED_TRACKED
        assert str(repo / "src" / f"gone{total - 1:05d}") in supervisor.unwatched_paths
        assert str(repo / "src" / "gone00000") not in supervisor.unwatched_paths
