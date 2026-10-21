"""MCP watcher/store shutdown ordering, adapted from the issue #1121 reproduction.

Source: https://github.com/tirth8205/code-review-graph/issues/1121

Exercise real main/start_watch_thread/watch/handler/debouncer lifecycles. Replace
the store, observer and update/post-processing boundaries with instrumented
doubles. Event gates hold work while the transport returns or raises; every
thread is released and joined during teardown. No SQLite connection is opened.

Post-processing cases are conditional on the callback API added by #1108; that
independent PR is unnecessary for the shutdown fix itself.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
import threading
import time

import pytest
from watchdog.events import FileModifiedEvent

from code_review_graph import incremental, postprocessing
from code_review_graph import main as crg_main

_BOUND = 10.0  # cap on every wait; never what produces the ordering
_OBSERVE = 2.0  # how long a fixed main() gets to (wrongly) close while work is held
_STOP_TIMEOUT = 0.2  # shortened join timeout, so "join with timeout, then close" is caught

STARTUP = ("startup_reconciliation", "startup_post_processing")
BATCH = ("event_batch", "batch_post_processing")
POST_PROCESSING = ("startup_post_processing", "batch_post_processing")


class _Trace:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[dict] = []

    def record(self, name: str, **detail) -> None:
        with self._lock:
            self.events.append({"event": name, "thread": threading.current_thread().name,
                                **detail})

    def names(self) -> list[str]:
        with self._lock:
            return [e["event"] for e in self.events]


class InstrumentedStore:
    """Stands in for main()'s GraphStore: counts users, records closes, closes nothing."""

    def __init__(self, trace: _Trace, watcher_threads) -> None:
        self._trace = trace
        self._watcher_threads = watcher_threads
        self._lock = threading.Lock()
        self.active = 0
        self.close_calls: list[dict] = []
        self.closed_during_active_work = False
        self.close_called = threading.Event()

    def enter(self) -> None:
        with self._lock:
            self.active += 1

    def leave(self) -> None:
        with self._lock:
            self.active -= 1

    def close(self) -> None:
        with self._lock:
            snapshot = {"active": self.active, "watcher_threads_alive": self._watcher_threads()}
            self.close_calls.append(snapshot)
            if snapshot["active"]:
                self.closed_during_active_work = True
        self._trace.record("graphstore_close_called", **snapshot)
        self.close_called.set()


class FakeObserver:
    """In-memory watchdog Observer: remembers the handler, starts no OS watches."""

    def __init__(self) -> None:
        self.handler = None
        self.emitters: list = []
        self.started = threading.Event()

    def schedule(self, handler, path, *, recursive=False, event_filter=None):
        self.handler = handler
        watch = type("FakeWatch", (), {})()
        watch.path, watch.is_recursive = path, recursive
        return watch

    def unschedule(self, watch) -> None:
        pass

    def start(self) -> None:
        self.started.set()

    def stop(self) -> None:
        pass

    def join(self, timeout=None) -> None:
        pass


class Harness:
    def __init__(self, tmp_path, monkeypatch, phase: str) -> None:
        self.phase = phase
        self.trace = _Trace()
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        self.source = self.repo / "mod.py"
        self.source.write_text("def alpha():\n    return 1\n", encoding="utf-8")
        self.observer = FakeObserver()
        self.stores: list[InstrumentedStore] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.held_on_thread: str | None = None
        self.transport_returned = threading.Event()
        self.escape = threading.Event()
        self.handed_stops: list[threading.Event] = []
        self.sqlite_connects: list[tuple] = []
        self.main_error: BaseException | None = None
        self.main_thread: threading.Thread | None = None
        self.threads_before = set(threading.enumerate())
        self.stdio_before = incremental._MCP_STDIO_ACTIVE

        # Per-user state (registry, watcher health files) goes under tmp_path.
        monkeypatch.setenv("CRG_HOME", str(tmp_path / "crg-home"))
        monkeypatch.delenv("CRG_DATA_DIR", raising=False)
        monkeypatch.delenv("CRG_TOOLS", raising=False)
        monkeypatch.setattr(crg_main, "_default_repo_root", None)

        def refuse_sqlite(*args, **kwargs):
            self.sqlite_connects.append(args)
            raise AssertionError("reproduction must not open a real SQLite database")

        monkeypatch.setattr(sqlite3, "connect", refuse_sqlite)
        monkeypatch.setattr(crg_main, "GraphStore", self._new_store)
        monkeypatch.setattr("watchdog.observers.Observer", lambda: self.observer)
        monkeypatch.setattr(incremental, "_DEBOUNCE_SECONDS", 0.05)
        monkeypatch.setattr(incremental, "_WATCH_STOP_TIMEOUT", _STOP_TIMEOUT)
        monkeypatch.setattr(incremental, "incremental_update", self._update)
        monkeypatch.setattr(postprocessing, "run_post_processing", self._postprocess)
        monkeypatch.setattr(crg_main.mcp, "run", self._transport)

        real_watch = incremental.watch

        def watch_with_test_exit(repo_root, store, on_files_updated=None, stop_event=None):
            if stop_event is not None:
                self.handed_stops.append(stop_event)
            return real_watch(repo_root, store, on_files_updated=on_files_updated,
                              stop_event=stop_event if stop_event is not None else self.escape)

        monkeypatch.setattr(incremental, "watch", watch_with_test_exit)

    # -- boundary doubles -------------------------------------------------

    def watcher_threads(self) -> list[str]:
        """Live threads started during the test, other than the one running main()."""
        return sorted(t.name for t in threading.enumerate()
                      if t not in self.threads_before and t is not self.main_thread
                      and t.is_alive())

    def _new_store(self, *_args, **_kwargs) -> InstrumentedStore:
        store = InstrumentedStore(self.trace, self.watcher_threads)
        self.stores.append(store)
        return store

    def _use(self, store: InstrumentedStore, operation: str) -> None:
        store.enter()
        self.trace.record("watcher_entered_store_operation", operation=operation)
        try:
            if operation == self.phase and not self.entered.is_set():
                self.held_on_thread = threading.current_thread().name
                self.entered.set()
                if not self.release.wait(_BOUND):
                    self.trace.record("gate_timeout", operation=operation)
        finally:
            store.leave()
            self.trace.record("watcher_operation_finished", operation=operation)

    def _update(self, repo_root, store, *, changed_files, **_kwargs):
        if changed_files:
            self._use(store, "event_batch")
            return {"files_updated": len(changed_files), "errors": []}
        self._use(store, "startup_reconciliation")
        startup_changed = 1 if self.phase == "startup_post_processing" else 0
        return {"files_updated": startup_changed, "errors": []}

    def _postprocess(self, store, **_kwargs):
        on_startup = threading.current_thread().name == "crg-watch"
        self._use(store, "startup_post_processing" if on_startup else "batch_post_processing")
        return {}

    def _transport(self, **_kwargs):
        self.trace.record("mcp_transport_started")
        if self.phase in BATCH and self.observer.started.wait(_BOUND):
            # Re-dispatched (bounded) because watchdog's own debouncer, used before
            # #1108, can miss a notification sent before its first wait.  This only
            # gets the batch started; it does not shape the ordering under test.
            deadline = time.monotonic() + _BOUND
            while not self.entered.is_set() and time.monotonic() < deadline:
                self.observer.handler.dispatch(FileModifiedEvent(str(self.source)))
                self.entered.wait(0.25)
        # Return (client disconnect) only once the watcher is inside the held operation.
        self.entered.wait(_BOUND)
        self.trace.record("mcp_transport_returned")
        self.transport_returned.set()

    # -- driving ------------------------------------------------------------

    def run_main(self) -> None:
        def target():
            try:
                crg_main.main(repo_root=str(self.repo), auto_watch=True)
            except BaseException as exc:  # noqa: BLE001 - reported by the test
                self.main_error = exc
            finally:
                self.trace.record("main_returned")

        self.main_thread = threading.Thread(target=target, name="mcp-main", daemon=True)
        self.main_thread.start()

    def release_and_join(self) -> list[str]:
        self.release.set()
        self.escape.set()
        for stop in self.handed_stops:
            stop.set()
        started = [t for t in threading.enumerate() if t not in self.threads_before]
        deadline = time.monotonic() + _BOUND
        for thread in started:
            thread.join(max(0.0, deadline - time.monotonic()))
        return sorted(t.name for t in started if t.is_alive())


@pytest.fixture(params=STARTUP + BATCH)
def harness(request, tmp_path, monkeypatch):
    phase = request.param
    forwards = "on_files_updated" in inspect.signature(incremental.start_watch_thread).parameters
    if phase in POST_PROCESSING and not forwards:
        pytest.skip("this revision runs no post-processing in MCP auto-watch")
    h = Harness(tmp_path, monkeypatch, phase)
    yield h
    leftover = h.release_and_join()
    cleanup = {
        "phase": h.phase,
        "threads_alive_after_join": leftover,
        "sqlite_connect_attempts": len(h.sqlite_connects),
        "stdio_flag_restored": incremental._MCP_STDIO_ACTIVE == h.stdio_before,
        "main_exception": repr(h.main_error) if h.main_error else None,
    }
    print("TRACE " + json.dumps(h.trace.events))
    print("CLEANUP " + json.dumps(cleanup))
    assert not leftover, f"threads still running after teardown: {leftover}"
    assert not h.sqlite_connects
    assert cleanup["stdio_flag_restored"]


@pytest.mark.parametrize("transport_error", [False, True])
def test_mcp_waits_for_watcher_before_closing_store(harness, monkeypatch, transport_error):
    if transport_error:
        def failing_transport(**kwargs):
            harness._transport(**kwargs)
            raise RuntimeError("client disconnected")

        monkeypatch.setattr(crg_main.mcp, "run", failing_transport)
    harness.run_main()

    # Preconditions: a watcher thread is inside the held store operation and the
    # transport has returned.  A failure here is a harness problem, not the bug.
    assert harness.entered.wait(_BOUND), "HARNESS: watcher never reached the held operation"
    if harness.phase in STARTUP:
        assert harness.held_on_thread == "crg-watch", f"HARNESS: ran on {harness.held_on_thread}"
    else:
        assert harness.held_on_thread not in ("crg-watch", "mcp-main"), (
            f"HARNESS: batch ran on {harness.held_on_thread}"
        )
    assert harness.transport_returned.wait(_BOUND), "HARNESS: transport never returned"
    assert len(harness.stores) == 1, "HARNESS: main() did not create exactly one watch store"
    store = harness.stores[0]

    # The current main() closes synchronously right after the transport returns.
    # A correct one must not close while the operation is still held.
    store.close_called.wait(_OBSERVE)
    assert not store.closed_during_active_work, (
        "MCP closed GraphStore while its background watcher was still active: "
        f"close() saw {store.close_calls[0]} during {harness.phase} "
        f"(held on thread {harness.held_on_thread}); order={harness.trace.names()}"
    )

    # The same run must still end cleanly once the work is released.
    harness.release.set()
    harness.main_thread.join(_BOUND)
    assert not harness.main_thread.is_alive(), "main() never returned after the work finished"
    if transport_error:
        assert isinstance(harness.main_error, RuntimeError)
        assert str(harness.main_error) == "client disconnected"
    else:
        assert harness.main_error is None, f"main() raised {harness.main_error!r}"
    assert store.close_calls == [{"active": 0, "watcher_threads_alive": []}]


@pytest.mark.parametrize("start_error", [False, True])
def test_unavailable_or_failed_watcher_still_closes_store(tmp_path, monkeypatch, start_error):
    from unittest.mock import MagicMock

    store = MagicMock()
    monkeypatch.setattr(crg_main, "_apply_tool_filter", lambda _tools: None)
    monkeypatch.setattr(crg_main, "GraphStore", lambda _path: store)
    monkeypatch.setattr(crg_main, "_default_repo_root", None)
    monkeypatch.setattr(crg_main.mcp, "run", MagicMock())
    previous = incremental._MCP_STDIO_ACTIVE

    def start(*args, **kwargs):
        if start_error:
            raise RuntimeError("thread could not start")
        return None

    monkeypatch.setattr(crg_main, "start_watch_thread", start)
    if start_error:
        with pytest.raises(RuntimeError, match="thread could not start"):
            crg_main.main(repo_root=str(tmp_path), auto_watch=True)
        crg_main.mcp.run.assert_not_called()
    else:
        crg_main.main(repo_root=str(tmp_path), auto_watch=True)
        crg_main.mcp.run.assert_called_once()
    store.close.assert_called_once()
    assert incremental._MCP_STDIO_ACTIVE == previous
