"""The watch debouncer must not lose a wake-up sent before it first waits.

PR #1108 review: watchdog 6.0.0's ``EventDebouncer.run`` starts with an
unconditional ``Condition.wait()``.  ``start()`` returns before the consumer
thread gets there, so an event (or a stop) delivered in between appends and
notifies nobody; the consumer then sleeps on a queued event until some other
event arrives, or never wakes to stop at all.  With the observer started
before startup work, that first event can be the only edit the user makes.

These tests hold the consumer thread *before* its run body, deliver exactly
one event (or the stop) through the production handler, then release it.
No second event, notification or polling tick is ever supplied.  The graph
update is stubbed, so no SQLite connection is involved.
"""

from __future__ import annotations

import threading
import time

import pytest
from watchdog.events import FileModifiedEvent

from code_review_graph import incremental

_BOUND = incremental._DEBOUNCE_SECONDS + 5.0


@pytest.fixture
def held_debouncers(monkeypatch):
    """Start every debouncer thread held before its run body, until released."""
    release = threading.Event()
    stop_sent = threading.Event()
    held: list[threading.Thread] = []
    real_start = threading.Thread.start

    def start(self):
        if hasattr(self, "handle_event"):
            run, stop = self.run, self.stop

            def held_run():
                release.wait(_BOUND)
                run()

            def signalled_stop():
                stop()
                stop_sent.set()

            self.run, self.stop = held_run, signalled_stop
            held.append(self)
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    return release, held, stop_sent


@pytest.fixture
def updates(monkeypatch):
    """Record graph updates instead of performing them."""
    calls: list[list[str]] = []
    done = threading.Event()

    def fake_update(repo_root, store, *, changed_files, **_kwargs):
        calls.append(list(changed_files))
        done.set()
        return {"files_updated": len(changed_files), "errors": []}

    monkeypatch.setattr(incremental, "incremental_update", fake_update)
    return calls, done


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "mod.py"
    path.write_text("def alphaHelper():\n    return 1\n", encoding="utf-8")
    return path


def _stop_bounded(handler) -> threading.Thread:
    """Stop the handler on a helper thread; a hang must fail the test, not wedge it."""
    stopper = threading.Thread(target=handler.stop, name="debouncer-stopper", daemon=True)
    stopper.start()
    stopper.join(_BOUND)
    return stopper


def test_event_queued_before_the_consumer_first_waits_is_processed(
    tmp_path, source, held_debouncers, updates,
):
    release, held, _stop_sent = held_debouncers
    calls, done = updates
    # No store is touched on this path; anything that tried would fail loudly.
    handler = incremental._create_watch_handler(tmp_path, object(), None)
    handler.start()
    try:
        assert len(held) == 1, "the debouncer thread was not intercepted"
        assert held[0].is_alive()
        # Queued and notified while the consumer cannot yet be waiting.
        handler.dispatch(FileModifiedEvent(str(source)))
        release.set()

        processed = done.wait(_BOUND)
        pending = len(getattr(held[0], "_events", []))
        assert processed, (
            f"single event processed: False; events still pending: {pending}"
        )
        assert calls == [["mod.py"]]
        handler.raise_if_failed()
    finally:
        release.set()
        stopper = _stop_bounded(handler)
    assert not stopper.is_alive(), "stopping the debouncer hung"
    assert not held[0].is_alive()


def test_stop_before_the_consumer_first_waits_does_not_hang(
    tmp_path, held_debouncers, updates,
):
    release, held, stop_sent = held_debouncers
    calls, _done = updates
    handler = incremental._create_watch_handler(tmp_path, object(), None)
    handler.start()
    assert len(held) == 1
    stopper = threading.Thread(target=handler.stop, name="debouncer-stopper", daemon=True)
    stopper.start()
    # The stop is signalled while the consumer cannot yet be waiting.
    assert stop_sent.wait(_BOUND), "stop() itself never returned"
    release.set()
    stopper.join(_BOUND)
    assert not stopper.is_alive(), "a stop sent before the first wait was lost"
    assert not held[0].is_alive()
    assert calls == []


def test_debouncer_coalesces_a_burst_in_arrival_order_and_restarts_its_timer():
    batches: list[list[str]] = []
    batch_seen = threading.Event()

    def callback(events):
        batches.append(list(events))
        batch_seen.set()

    debouncer = incremental._WatchDebouncer(0.3, callback)
    debouncer.start()
    try:
        started = time.monotonic()
        for name in ("c", "a", "b"):
            debouncer.handle_event(name)
            time.sleep(0.1)  # each event restarts the quiet interval
        assert batch_seen.wait(5)
        elapsed = time.monotonic() - started
        assert batches == [["c", "a", "b"]]
        assert elapsed >= 0.2 + 0.3, "the batch fired before a full quiet interval"

        batch_seen.clear()
        debouncer.handle_event("d")
        assert batch_seen.wait(5)
        assert batches == [["c", "a", "b"], ["d"]]
    finally:
        debouncer.stop()
        debouncer.join(5)
    assert not debouncer.is_alive()
