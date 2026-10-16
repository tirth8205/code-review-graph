"""Startup work and event batches share one serialized processing path.

PR #1108 runs post-processing after the watcher's startup reconciliation.
``watch()`` therefore starts the filesystem observer *before* that startup work
and holds the batch processor's lock from before the observer starts until the
startup work is done.  An event captured meanwhile waits in the debouncer and
becomes an ordinary batch once startup releases the lock; the two never write
to the shared ``GraphStore`` at the same time, and a startup failure still
stops the observer and the debouncer before ``watch()`` raises.
"""

from __future__ import annotations

import subprocess
import threading
import time

import pytest
from watchdog.events import FileModifiedEvent

from code_review_graph import incremental
from code_review_graph.graph import GraphStore
from code_review_graph.postprocessing import run_post_processing
from tests.test_watch_robustness import FakeObserver

_WATCH_THREAD = "crg-watch-under-test"
_LEAF_EDIT = "def leafHelper():\n    return 1\n\ndef lateEditHelper():\n    return 2\n"
# Long enough for the debouncer to close a batch and block on the processing lock.
_BATCH_READY_SECONDS = incremental._DEBOUNCE_SECONDS + 1.0


class _RecordingObserver(FakeObserver):
    def __init__(self, log: list) -> None:
        super().__init__()
        self.log = log
        self.joined = False

    def start(self) -> None:
        self.log.append("observer-start")
        super().start()

    def join(self, timeout=None) -> None:
        self.joined = True


@pytest.fixture
def debouncers(monkeypatch):
    """Every debouncer ``watch()`` creates, with every event handed to it."""
    created = []

    class RecordingDebouncer(incremental._WatchDebouncer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.received = []
            created.append(self)

        def handle_event(self, event):
            self.received.append(event)
            super().handle_event(event)

    monkeypatch.setattr(incremental, "_WatchDebouncer", RecordingDebouncer)
    return created


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A built repository with one offline change for startup to reconcile."""
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mod.py").write_text("def alphaHelper():\n    return 1\n", encoding="utf-8")
    (root / "leaf.py").write_text("def leafHelper():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    with GraphStore(incremental.get_db_path(root)) as store:
        assert incremental.full_build(root, store)["errors"] == []
        assert not run_post_processing(store).get("warnings")
    (root / "mod.py").write_text(
        "def alphaHelper():\n    return 1\n\ndef betaHelper():\n    return 2\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def update_log(monkeypatch):
    """Log each graph update as (begin|end, startup|batch), in the order they happen."""
    log: list = []
    real_update = incremental.incremental_update

    def tracking_update(*args, **kwargs):
        role = "startup" if threading.current_thread().name == _WATCH_THREAD else "batch"
        log.append(("update-begin", role))
        try:
            return real_update(*args, **kwargs)
        finally:
            log.append(("update-end", role))

    monkeypatch.setattr(incremental, "incremental_update", tracking_update)
    return log


def _run_watch(monkeypatch, repo, observer, on_files_updated):
    monkeypatch.setattr("watchdog.observers.Observer", lambda: observer)
    store = GraphStore(incremental.get_db_path(repo))
    stop = threading.Event()
    outcome: dict = {}

    def run() -> None:
        try:
            incremental.watch(repo, store, on_files_updated=on_files_updated, stop_event=stop)
        except BaseException as exc:  # noqa: BLE001 - the test inspects it
            outcome["error"] = exc

    thread = threading.Thread(target=run, name=_WATCH_THREAD, daemon=True)
    thread.start()
    return thread, stop, store, outcome


def _graph_has(repo, path, name, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with GraphStore(incremental.get_db_path(repo)) as store:
            if name in {node.name for node in store.get_nodes_by_file(str(path))}:
                return True
        time.sleep(0.1)
    return False


def _finish(thread, stop, store, *workers) -> None:
    """Stop the watcher, and close the store only once nothing can write to it.

    Every thread that may use the store gets a bounded join.  If one is still
    alive the store is left open on purpose — closing a connection under a
    live writer can crash the interpreter — and the test fails instead.
    """
    stop.set()
    owners = (*workers, thread)
    for owner in owners:
        owner.join(timeout=30)
    alive = [owner.name for owner in owners if owner.is_alive()]
    if alive:
        pytest.fail(f"still running, so the store was left open: {alive}")
    store.close()


def _assert_fts_consistent(repo, present: tuple[str, ...]) -> None:
    with GraphStore(incremental.get_db_path(repo)) as store:
        node_ids = {row[0] for row in store._conn.execute("SELECT id FROM nodes")}
        indexed = {row[0] for row in store._conn.execute("SELECT id FROM nodes_fts_docsize")}
        assert indexed == node_ids, (
            f"missing FTS ids {node_ids - indexed}; stale FTS ids {indexed - node_ids}"
        )
        for name in present:
            graph_ids = {
                row[0] for row in store._conn.execute(
                    "SELECT id FROM nodes WHERE name = ?", (name,),
                )
            }
            matched = {
                row[0] for row in store._conn.execute(
                    "SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH ?", (name,),
                )
            }
            assert len(graph_ids) == 1 and matched == graph_ids, (name, graph_ids, matched)


def test_single_event_captured_during_startup_reaches_graph_after_startup(
    repo, monkeypatch, debouncers, update_log,
):
    leaf = repo / "leaf.py"
    observer = _RecordingObserver(update_log)
    startups = []

    def on_files_updated(store):
        if not startups:
            startups.append(True)
            update_log.append("startup-postprocess-begin")
            # Saved after reconciliation read leaf.py: only the observer sees it.
            leaf.write_text(_LEAF_EDIT, encoding="utf-8")
            observer.handler.dispatch(FileModifiedEvent(str(leaf)))
            time.sleep(_BATCH_READY_SECONDS)
            result = run_post_processing(store)
            update_log.append("startup-postprocess-end")
            return result
        return run_post_processing(store)

    thread, stop, store, outcome = _run_watch(monkeypatch, repo, observer, on_files_updated)
    try:
        # No further event is ever dispatched: the one captured event must
        # become a batch on its own once startup lets go.
        found = _graph_has(repo, leaf, "lateEditHelper", timeout=20)
    finally:
        _finish(thread, stop, store, *debouncers)

    assert "error" not in outcome, outcome
    assert found, "an event captured during startup never reached the graph"
    _assert_fts_consistent(repo, ("alphaHelper", "betaHelper", "leafHelper", "lateEditHelper"))
    assert [event.src_path for d in debouncers for event in d.received] == [str(leaf)]
    assert update_log == [
        "observer-start",
        ("update-begin", "startup"),
        ("update-end", "startup"),
        "startup-postprocess-begin",
        "startup-postprocess-end",
        ("update-begin", "batch"),
        ("update-end", "batch"),
    ]


def test_batch_ready_the_moment_the_observer_starts_waits_for_startup(
    repo, monkeypatch, debouncers, update_log,
):
    """The lock is already held when the observer starts: there is no gap to race."""
    leaf = repo / "leaf.py"
    batches: list[threading.Thread] = []
    batch_started = threading.Event()

    class BatchAtStartObserver(_RecordingObserver):
        def start(self) -> None:
            super().start()
            leaf.write_text(_LEAF_EDIT, encoding="utf-8")
            batch = threading.Thread(
                target=self.handler.process,
                args=([FileModifiedEvent(str(leaf))],),
                name="instant-batch",
            )
            batches.append(batch)
            batch.start()
            # Give the batch every chance to get in ahead of startup.
            batch.join(timeout=0.5)
            batch_started.set()

    observer = BatchAtStartObserver(update_log)
    thread, stop, store, outcome = _run_watch(monkeypatch, repo, observer, run_post_processing)
    try:
        assert batch_started.wait(timeout=20), "the observer never started"
        batches[0].join(timeout=20)
        assert not batches[0].is_alive(), "the batch never finished"
        observer.handler.raise_if_failed()
    finally:
        # This batch bypasses the debouncer, so watch() does not join it.
        _finish(thread, stop, store, *batches, *debouncers)

    assert "error" not in outcome, outcome
    assert update_log == [
        "observer-start",
        ("update-begin", "startup"),
        ("update-end", "startup"),
        ("update-begin", "batch"),
        ("update-end", "batch"),
    ]
    with GraphStore(incremental.get_db_path(repo)) as check:
        assert "betaHelper" in {n.name for n in check.get_nodes_by_file(str(repo / "mod.py"))}
        assert "lateEditHelper" in {n.name for n in check.get_nodes_by_file(str(leaf))}


@pytest.mark.parametrize(
    ("failure", "error_type", "message"),
    [
        ("reconciliation", RuntimeError, "initial watch reconciliation reported errors"),
        ("postprocess-warning", RuntimeError, "post-processing reported warnings"),
        ("postprocess-crash", ValueError, "forced startup crash"),
    ],
)
def test_startup_failure_stops_observer_and_debouncer_and_keeps_its_error(
    repo, monkeypatch, debouncers, update_log, failure, error_type, message,
):
    leaf = repo / "leaf.py"
    observer = _RecordingObserver(update_log)

    def queue_one_batch() -> None:
        # A batch is waiting on the processing lock when startup fails.
        leaf.write_text(_LEAF_EDIT, encoding="utf-8")
        observer.handler.dispatch(FileModifiedEvent(str(leaf)))
        time.sleep(_BATCH_READY_SECONDS)
        update_log.append("startup-failed")

    tracking_update = incremental.incremental_update

    def failing_reconciliation(*args, **kwargs):
        result = tracking_update(*args, **kwargs)
        if threading.current_thread().name != _WATCH_THREAD:
            return result
        queue_one_batch()
        return {**result, "errors": [{"file": "mod.py", "error": "forced"}]}

    if failure == "reconciliation":
        monkeypatch.setattr(incremental, "incremental_update", failing_reconciliation)

    def on_files_updated(store):
        if threading.current_thread().name != _WATCH_THREAD:
            return run_post_processing(store)
        queue_one_batch()
        if failure == "postprocess-crash":
            raise ValueError("forced startup crash")
        return {"warnings": ["forced startup warning"]}

    thread, stop, store, outcome = _run_watch(monkeypatch, repo, observer, on_files_updated)
    thread.join(timeout=30)
    returned_on_its_own = not thread.is_alive()
    _finish(thread, stop, store, *debouncers)
    assert returned_on_its_own, "watch() did not return after a startup failure"

    # The startup error is what surfaces, unmasked by teardown or the queued batch.
    assert isinstance(outcome.get("error"), error_type), outcome
    assert message in str(outcome["error"])
    assert observer.started and observer.stopped and observer.joined
    assert debouncers and not any(d.is_alive() for d in debouncers)
    # The queued batch ran only after startup let go, and had finished (no
    # writer left on the store) by the time watch() returned.
    assert update_log == [
        "observer-start",
        ("update-begin", "startup"),
        ("update-end", "startup"),
        "startup-failed",
        ("update-begin", "batch"),
        ("update-end", "batch"),
    ]
