"""Failed budget promotion must preserve coverage until its replacement is live."""

import json
import threading

from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver

from code_review_graph.incremental import _load_ignore_patterns, _WatchSupervisor
from tests.test_watch_robustness import FakeObserver


class PromotionObserver(FakeObserver):
    """Model watchdog's distinct recursive/non-recursive handles at one path."""

    def __init__(self, root):
        super().__init__()
        self.root = str(root)
        self.active = set()
        self.fail_promotion = True
        self.coverage_at_promotion = []

    def schedule(self, handler, path, *, recursive=False, event_filter=None):
        if path == self.root and recursive:
            self.coverage_at_promotion.append(set(self.active))
            if self.fail_promotion:
                raise OSError("No space left on device")
        handle = super().schedule(handler, path, recursive=recursive, event_filter=event_filter)
        self.active.add((path, recursive))
        return handle

    def unschedule(self, handle):
        self.active.remove((handle.path, handle.is_recursive))
        super().unschedule(handle)


def supervisor_with_full_budget(root):
    (root / "src").mkdir()
    for index in range(6):
        (root / "node_modules" / f"dep{index}").mkdir(parents=True)
    observer = PromotionObserver(root)
    supervisor = _WatchSupervisor(
        observer,
        root,
        _load_ignore_patterns(root),
        health_path=root / "health.json",
        max_schedules=2,
    )
    supervisor.schedule_initial(object())
    return supervisor, observer


def test_failed_promotion_keeps_coverage_and_retries(tmp_path, monkeypatch):
    import code_review_graph.incremental as incremental_module

    clock = [1000.0]
    monkeypatch.setattr(incremental_module.time, "monotonic", lambda: clock[0])
    supervisor, observer = supervisor_with_full_budget(tmp_path)
    previous = {(str(tmp_path), False), (str(tmp_path / "src"), True)}
    assert observer.active == previous
    (tmp_path / "new").mkdir()

    adopted, _ = supervisor.sync_watches()
    assert observer.active == previous
    assert set(supervisor.watched_paths) == {str(tmp_path), str(tmp_path / "src")}
    assert adopted == []

    # The retry is rate-limited; once the cool-down has passed it goes ahead.
    clock[0] += incremental_module._PROMOTION_RETRY_SECONDS
    observer.fail_promotion = False
    adopted, _ = supervisor.sync_watches()
    assert adopted == [str(tmp_path / "new")]
    assert observer.active == {(str(tmp_path), True)}
    assert supervisor.watched_paths == [str(tmp_path)]
    assert observer.coverage_at_promotion == [previous, previous]


def test_failed_promotion_reports_partial_coverage(tmp_path):
    supervisor, observer = supervisor_with_full_budget(tmp_path)
    (tmp_path / "new").mkdir()
    supervisor.sync_watches()
    supervisor.report_health(observer_alive=True, force=True)
    health = json.loads((tmp_path / "health.json").read_text())
    assert health["watched_paths"] == 2
    assert health["degraded"] is True


def test_vanished_unadopted_directory_clears_transient_degradation(tmp_path):
    supervisor, observer = supervisor_with_full_budget(tmp_path)
    (tmp_path / "new").mkdir()
    supervisor.sync_watches()
    (tmp_path / "new").rmdir()
    supervisor.sync_watches()
    supervisor.report_health(observer_alive=True, force=True)
    health = json.loads((tmp_path / "health.json").read_text())
    assert health["watched_paths"] == 2
    assert health["degraded"] is False


def test_polling_observer_keeps_events_across_promotion_failure(tmp_path, monkeypatch):
    import code_review_graph.incremental as incremental_module

    # This test is about event delivery across a failed promotion, not about
    # the retry rate, so let the retry happen on the next tick.
    monkeypatch.setattr(incremental_module, "_PROMOTION_RETRY_SECONDS", 0.0)
    (tmp_path / "src").mkdir()
    source = tmp_path / "src" / "existing.py"
    source.write_text("before\n")
    for index in range(6):
        (tmp_path / "node_modules" / f"dep{index}").mkdir(parents=True)
    delivered = threading.Event()
    wanted = [str(source)]

    class Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            if event.src_path == wanted[0]:
                delivered.set()

    observer = PollingObserver(timeout=0.02)
    supervisor = _WatchSupervisor(
        observer,
        tmp_path,
        _load_ignore_patterns(tmp_path),
        health_path=None,
        max_schedules=2,
    )
    supervisor.schedule_initial(Handler())
    observer.start()
    original_schedule = observer.schedule

    def fail_promotion(handler, path, *, recursive=False, **kwargs):
        if path == str(tmp_path) and recursive:
            raise OSError("No space left on device")
        return original_schedule(handler, path, recursive=recursive, **kwargs)

    try:
        (tmp_path / "new").mkdir()
        monkeypatch.setattr(observer, "schedule", fail_promotion)
        assert supervisor.sync_watches()[0] == []
        source.write_text("after failure: still watched\n")
        assert delivered.wait(3), "working source stopped producing events after failed promotion"
        monkeypatch.setattr(observer, "schedule", original_schedule)
        assert supervisor.sync_watches()[0] == [str(tmp_path / "new")]
        wanted[0] = str(tmp_path / "new" / "added.py")
        delivered.clear()
        (tmp_path / "new" / "added.py").write_text("new coverage\n")
        assert delivered.wait(3), "successful promotion did not cover the new subtree"
        assert len(observer.emitters) == 1
    finally:
        observer.stop()
        observer.join(timeout=3)


def test_failed_promotion_waits_before_retrying(tmp_path, monkeypatch):
    """One failed promotion per cool-down, not one per one-second tick.

    On Linux every attempt walks the parent subtree and can leak an inotify
    instance, so retrying each tick consumes the quota it is waiting for.
    """
    import code_review_graph.incremental as incremental_module

    clock = [1000.0]
    monkeypatch.setattr(incremental_module.time, "monotonic", lambda: clock[0])
    supervisor, observer = supervisor_with_full_budget(tmp_path)
    (tmp_path / "new").mkdir()

    supervisor.sync_watches()
    assert len(observer.coverage_at_promotion) == 1
    for _ in range(5):
        clock[0] += 1.0
        supervisor.sync_watches()
    assert len(observer.coverage_at_promotion) == 1
    assert supervisor.degraded is True

    clock[0] += incremental_module._PROMOTION_RETRY_SECONDS
    observer.fail_promotion = False
    adopted, _ = supervisor.sync_watches()
    assert adopted == [str(tmp_path / "new")]
    assert len(observer.coverage_at_promotion) == 2
    assert supervisor.degraded is True  # recursive coverage is still coarser
