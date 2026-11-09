"""``forget``, ``embed`` and ``watch`` obey the write lock too. See: #1073, #1156

``build``, ``update`` and ``postprocess`` already stood down for each other. The
other three writers ignored the lock, so one could rewrite a graph that a build
had only half written. ``forget`` and ``embed`` take it for the whole run.
``watch`` runs for days, so it takes it per write: holding it throughout would
stop every build for as long as the watcher lived.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from watchdog.events import FileModifiedEvent

from code_review_graph import cli, incremental
from code_review_graph.graph import GraphStore
from code_review_graph.locking import WriteLock
from code_review_graph.migrations import LATEST_VERSION
from code_review_graph.tools import docs as docs_module
from tests.test_watch_robustness import FakeObserver

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the write lock is flock, which Windows does not have",
)

_BOUND = 15.0


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A built repository with two source files."""
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    monkeypatch.setattr(incremental, "_DEBOUNCE_SECONDS", 0.05)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "keep.py").write_text("def keepHelper():\n    return 1\n", encoding="utf-8")
    (root / "drop.py").write_text("def dropHelper():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    with GraphStore(incremental.get_db_path(root)) as store:
        assert incremental.full_build(root, store)["errors"] == []
    return root


@pytest.fixture
def held(repo):
    """The repository's write lock, held by someone else until released."""
    lock = WriteLock(incremental.get_db_path(repo))
    assert lock.acquire()
    yield lock
    lock.release()


def _run_cli(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["code-review-graph", *argv])
    try:
        cli.main()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    return 0


def _names(repo, path) -> set[str]:
    with GraphStore(incremental.get_db_path(repo)) as store:
        return {node.name for node in store.get_nodes_by_file(str(path))}


def _lock_is_free(repo) -> bool:
    probe = WriteLock(incremental.get_db_path(repo))
    free = probe.acquire()
    probe.release()
    return free


def _wait_for(condition, bound: float = _BOUND) -> bool:
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def _release_soon(lock: WriteLock) -> threading.Timer:
    timer = threading.Timer(0.3, lock.release)
    timer.start()
    return timer


# --- forget -------------------------------------------------------------------


def test_forget_stands_down_and_changes_nothing(repo, held, monkeypatch, capsys):
    code = _run_cli(monkeypatch, "forget", "drop.py", "--repo", str(repo))

    assert code == cli.EXIT_TEMPFAIL
    err = capsys.readouterr().err
    assert "nothing was forgotten" in err
    assert "repeating the work" not in err
    assert "dropHelper" in _names(repo, repo / "drop.py")


def test_forget_dry_run_writes_nothing_so_needs_no_lock(repo, held, monkeypatch, capsys):
    code = _run_cli(monkeypatch, "forget", "drop.py", "--dry-run", "--repo", str(repo))

    assert code == 0
    assert "[dry-run]" in capsys.readouterr().out
    assert "dropHelper" in _names(repo, repo / "drop.py")


def _schema_version(db) -> int:
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
    return int(row[0])


def test_forget_dry_run_never_upgrades_an_older_graph(repo, held, monkeypatch, capsys):
    """A writable open migrates, so the preview reads the graph read-only."""
    db = incremental.get_db_path(repo, read_only=True)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
            (str(LATEST_VERSION - 1),),
        )
    conn.close()

    code = _run_cli(monkeypatch, "forget", "drop.py", "--dry-run", "--repo", str(repo))

    assert code == 1
    assert "code-review-graph update" in capsys.readouterr().err
    assert _schema_version(db) == LATEST_VERSION - 1


@pytest.fixture
def legacy_repo(repo):
    """The same graph in the old top-level ``.code-review-graph.db`` layout."""
    data_dir = repo / ".code-review-graph"
    source = sqlite3.connect(data_dir / "graph.db")
    target = sqlite3.connect(repo / ".code-review-graph.db")
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()
    shutil.rmtree(data_dir)
    return repo


def test_forget_leaves_a_legacy_graph_alone_when_it_stands_down(legacy_repo, monkeypatch):
    lock = WriteLock(legacy_repo / ".code-review-graph" / "graph.db")
    assert lock.acquire()
    try:
        code = _run_cli(monkeypatch, "forget", "drop.py", "--repo", str(legacy_repo))
    finally:
        lock.release()

    assert code == cli.EXIT_TEMPFAIL
    assert (legacy_repo / ".code-review-graph.db").is_file()
    assert not (legacy_repo / ".code-review-graph" / "graph.db").exists()


def test_forget_dry_run_reads_a_legacy_graph_where_it_is(legacy_repo, monkeypatch, capsys):
    code = _run_cli(
        monkeypatch, "forget", "drop.py", "--dry-run", "--repo", str(legacy_repo),
    )

    assert code == 0
    assert "drop.py" in capsys.readouterr().out
    assert (legacy_repo / ".code-review-graph.db").is_file()
    assert not (legacy_repo / ".code-review-graph" / "graph.db").exists()


def test_forget_moves_a_legacy_graph_under_the_lock_then_forgets(legacy_repo, monkeypatch):
    assert _run_cli(monkeypatch, "forget", "drop.py", "--repo", str(legacy_repo)) == 0

    assert not (legacy_repo / ".code-review-graph.db").exists()
    assert _names(legacy_repo, legacy_repo / "drop.py") == set()
    assert "keepHelper" in _names(legacy_repo, legacy_repo / "keep.py")


def test_forget_wait_runs_once_the_writer_has_finished(repo, held, monkeypatch):
    timer = _release_soon(held)
    try:
        code = _run_cli(monkeypatch, "forget", "drop.py", "--wait", "--repo", str(repo))
    finally:
        timer.join()

    assert code == 0
    assert _names(repo, repo / "drop.py") == set()
    assert "keepHelper" in _names(repo, repo / "keep.py")


def test_forget_lets_go_of_the_lock(repo, monkeypatch):
    assert _run_cli(monkeypatch, "forget", "drop.py", "--repo", str(repo)) == 0
    assert _lock_is_free(repo)


def test_forget_holds_the_lock_while_it_writes(repo, monkeypatch):
    seen = []
    real = cli._match_files_to_forget

    def probing(*args, **kwargs):
        seen.append(_lock_is_free(repo))
        return real(*args, **kwargs)

    monkeypatch.setattr(cli, "_match_files_to_forget", probing)
    assert _run_cli(monkeypatch, "forget", "drop.py", "--repo", str(repo)) == 0
    assert seen == [False]


# --- embed --------------------------------------------------------------------


class _FakeEmbeddingStore:
    available = True

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def count(self) -> int:
        return 3

    def close(self) -> None:
        pass


def _fake_embedding(monkeypatch, seen: list[bool], repo) -> None:
    def embed_all(_store, _emb_store):
        seen.append(_lock_is_free(repo))
        return 3

    monkeypatch.setattr(docs_module, "EmbeddingStore", _FakeEmbeddingStore)
    monkeypatch.setattr(docs_module, "embed_all_nodes", embed_all)


def test_embed_tool_stands_down_without_embedding(repo, held, monkeypatch):
    seen: list[bool] = []
    _fake_embedding(monkeypatch, seen, repo)

    result = docs_module.embed_graph(repo_root=str(repo))

    assert result["status"] == "contended"
    assert "nothing was embedded" in result["summary"]
    assert seen == []


def test_embed_cli_exits_as_contended(repo, held, monkeypatch, capsys):
    seen: list[bool] = []
    _fake_embedding(monkeypatch, seen, repo)

    assert _run_cli(monkeypatch, "embed", "--repo", str(repo)) == cli.EXIT_TEMPFAIL
    assert "nothing was embedded" in capsys.readouterr().err
    assert seen == []


def test_embed_cli_wait_runs_once_the_writer_has_finished(repo, held, monkeypatch):
    seen: list[bool] = []
    _fake_embedding(monkeypatch, seen, repo)
    timer = _release_soon(held)
    try:
        assert _run_cli(monkeypatch, "embed", "--wait", "--repo", str(repo)) == 0
    finally:
        timer.join()
    assert seen == [False]


def test_embed_holds_the_lock_while_it_works_then_lets_go(repo, monkeypatch):
    seen: list[bool] = []
    _fake_embedding(monkeypatch, seen, repo)

    assert docs_module.embed_graph(repo_root=str(repo))["status"] == "ok"

    assert seen == [False]
    assert _lock_is_free(repo)


def test_embed_writes_the_graph_whose_lock_it_holds(repo, monkeypatch, tmp_path):
    """The path is resolved once: a registration that changes during ``--wait``
    must not send the embeddings to a graph whose lock this run never took."""
    locked = (repo / ".code-review-graph" / "graph.db").resolve()
    elsewhere = tmp_path / "elsewhere"
    used: list[Path] = []

    class Recording(_FakeEmbeddingStore):
        def __init__(self, db_path, *_args, **_kwargs) -> None:
            used.append(Path(db_path).resolve())

    def embed_all(store, _emb_store):
        used.append(Path(store.db_path).resolve())
        return 0

    real_lock_or_skip = docs_module._lock_or_skip

    def moved_while_waiting(*args, **kwargs):
        outcome = real_lock_or_skip(*args, **kwargs)
        monkeypatch.setenv("CRG_DATA_DIR", str(elsewhere))
        return outcome

    monkeypatch.setattr(docs_module, "EmbeddingStore", Recording)
    monkeypatch.setattr(docs_module, "embed_all_nodes", embed_all)
    monkeypatch.setattr(docs_module, "_lock_or_skip", moved_while_waiting)

    assert docs_module.embed_graph(repo_root=str(repo))["status"] == "ok"

    assert used == [locked, locked]
    assert not (elsewhere / "graph.db").exists()


def test_embed_lets_go_of_the_lock_when_it_fails(repo):
    result = docs_module.embed_graph(repo_root=str(repo), provider="moonbase")

    assert result["status"] == "error"
    assert _lock_is_free(repo)


# --- watch, one batch at a time ---------------------------------------------


def _edit(repo, name: str) -> FileModifiedEvent:
    path = repo / "keep.py"
    path.write_text(
        f"def keepHelper():\n    return 1\n\ndef {name}():\n    return 2\n",
        encoding="utf-8",
    )
    return FileModifiedEvent(str(path))


def test_watch_batch_writes_under_the_lock_and_then_lets_go(repo):
    seen = []

    def on_files_updated(_store):
        seen.append(_lock_is_free(repo))

    with GraphStore(incremental.get_db_path(repo)) as store:
        handler = incremental._create_watch_handler(repo, store, on_files_updated)
        handler.process([_edit(repo, "underLock")])

    assert seen == [False]
    assert "underLock" in _names(repo, repo / "keep.py")
    assert _lock_is_free(repo)


def test_watch_batch_waits_out_another_writer_and_is_not_lost(repo, held):
    with GraphStore(incremental.get_db_path(repo)) as store:
        handler = incremental._create_watch_handler(repo, store, None)
        handler.start()
        try:
            handler.dispatch(_edit(repo, "afterWriter"))
            time.sleep(0.5)
            assert "afterWriter" not in _names(repo, repo / "keep.py")

            held.release()
            assert _wait_for(lambda: "afterWriter" in _names(repo, repo / "keep.py"))
        finally:
            handler.stop()


def test_watch_batch_retried_behind_a_writer_is_counted_once(repo, held):
    with GraphStore(incremental.get_db_path(repo)) as store:
        handler = incremental._create_watch_handler(repo, store, None)
        handler.start()
        try:
            handler.dispatch(_edit(repo, "countedOnce"))
            time.sleep(0.5)
            held.release()
            assert _wait_for(lambda: "countedOnce" in _names(repo, repo / "keep.py"))
            assert handler.events_seen == 1
        finally:
            handler.stop()


def test_watch_can_stop_while_a_batch_waits_for_the_lock(repo, held):
    with GraphStore(incremental.get_db_path(repo)) as store:
        handler = incremental._create_watch_handler(repo, store, None)
        handler.start()
        handler.dispatch(_edit(repo, "neverWritten"))
        time.sleep(0.3)

        stopper = threading.Thread(target=handler.stop, daemon=True)
        stopper.start()
        stopper.join(timeout=_BOUND)
        hung = stopper.is_alive()

    assert not hung, "stopping the watcher hung behind another writer's lock"


def test_watch_batch_failure_still_lets_go_of_the_lock(repo):
    def failing(_store):
        raise ValueError("post-processing broke")

    with GraphStore(incremental.get_db_path(repo)) as store:
        handler = incremental._create_watch_handler(repo, store, failing)
        handler.process([_edit(repo, "failedBatch")])
        with pytest.raises(RuntimeError):
            handler.raise_if_failed()

    assert _lock_is_free(repo)


def test_a_recursive_watch_is_not_woken_by_its_own_write_lock(repo):
    """Taking the lock rewrites ``write.lock``. A watch on the whole repository
    sees that, and acting on it took the lock again, once a debounce, for ever."""
    from watchdog.observers import Observer

    with GraphStore(incremental.get_db_path(repo)) as store:
        handler = incremental._create_watch_handler(repo, store, None)
        observer = Observer()
        observer.schedule(handler, str(repo), recursive=True)
        handler.start()
        observer.start()
        try:
            handler.dispatch(_edit(repo, "oneBatch"))
            assert _wait_for(lambda: handler.events_seen > 0)
            time.sleep(1.5)  # the batch's own graph writes come back once
            settled = handler.events_seen
            time.sleep(1.5)
            assert handler.events_seen == settled
        finally:
            observer.stop()
            observer.join(timeout=_BOUND)
            handler.stop()


# --- watch, start-up ----------------------------------------------------------


def _run_watch(monkeypatch, repo):
    monkeypatch.setattr("watchdog.observers.Observer", FakeObserver)
    store = GraphStore(incremental.get_db_path(repo))
    stop = threading.Event()
    outcome: dict = {}

    def run() -> None:
        try:
            incremental.watch(repo, store, stop_event=stop)
        except BaseException as exc:  # noqa: BLE001 - the test inspects it
            outcome["error"] = exc

    thread = threading.Thread(target=run, name="crg-watch-under-test", daemon=True)
    thread.start()
    return thread, stop, store, outcome


def _finish(thread, stop, store) -> None:
    stop.set()
    thread.join(timeout=_BOUND)
    if thread.is_alive():
        pytest.fail("watch() did not stop, so its store was left open")
    store.close()


def test_watch_startup_waits_out_another_writer(repo, held, monkeypatch):
    (repo / "keep.py").write_text(
        "def keepHelper():\n    return 1\n\ndef offlineEdit():\n    return 2\n",
        encoding="utf-8",
    )
    thread, stop, store, outcome = _run_watch(monkeypatch, repo)
    try:
        time.sleep(0.5)
        assert "offlineEdit" not in _names(repo, repo / "keep.py")

        held.release()
        assert _wait_for(lambda: "offlineEdit" in _names(repo, repo / "keep.py"))
    finally:
        _finish(thread, stop, store)
    assert outcome == {}


def test_watch_can_stop_while_startup_waits_for_the_lock(repo, held, monkeypatch):
    (repo / "keep.py").write_text(
        "def keepHelper():\n    return 1\n\ndef neverReconciled():\n    return 2\n",
        encoding="utf-8",
    )
    thread, stop, store, outcome = _run_watch(monkeypatch, repo)
    time.sleep(0.3)

    _finish(thread, stop, store)

    assert outcome == {}
    assert "neverReconciled" not in _names(repo, repo / "keep.py")


def test_a_running_watcher_does_not_block_a_build(repo, monkeypatch):
    """The reason ``watch`` locks per write and not for its whole life."""
    thread, stop, store, outcome = _run_watch(monkeypatch, repo)
    try:
        # Start-up may hold it briefly; after that it must stay free.
        assert _wait_for(lambda: _lock_is_free(repo))
        time.sleep(0.3)
        assert _lock_is_free(repo)
    finally:
        _finish(thread, stop, store)
    assert outcome == {}
