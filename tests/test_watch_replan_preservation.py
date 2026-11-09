"""Failed watch-plan transitions must preserve existing event coverage."""

import shutil
from unittest.mock import MagicMock

import pytest


def test_973_child_watch_failure_preserves_recursive_root_coverage(tmp_path, monkeypatch):
    from code_review_graph.incremental import _load_ignore_patterns, _WatchSupervisor
    from tests.test_watch_robustness import FakeObserver

    source = tmp_path / "src"
    source.mkdir()
    observer = FakeObserver()
    supervisor = _WatchSupervisor(observer, tmp_path, _load_ignore_patterns(tmp_path))
    supervisor.schedule_initial(MagicMock())
    assert (str(tmp_path), True) in observer.scheduled
    for index in range(6):
        (tmp_path / "node_modules" / str(index)).mkdir(parents=True)
    original = observer.schedule

    def fail_child(handler, path, **kwargs):
        if path == str(source):
            raise OSError("watch budget exhausted")
        return original(handler, path, **kwargs)

    monkeypatch.setattr(observer, "schedule", fail_child)
    if hasattr(supervisor, "request_replan"):
        supervisor.request_replan()
    supervisor.sync_watches()
    assert str(tmp_path) in supervisor.watched_paths
    assert str(tmp_path) not in supervisor._shallow, "recursive root released before child coverage"


def test_973_failed_root_collapse_preserves_existing_child_coverage(tmp_path, monkeypatch):
    from code_review_graph.incremental import _load_ignore_patterns, _WatchSupervisor
    from tests.test_watch_robustness import FakeObserver

    source = tmp_path / "src"
    source.mkdir()
    ignored = tmp_path / "node_modules"
    for index in range(6):
        (ignored / str(index)).mkdir(parents=True)
    observer = FakeObserver()
    supervisor = _WatchSupervisor(observer, tmp_path, _load_ignore_patterns(tmp_path))
    supervisor.schedule_initial(MagicMock())
    assert str(source) in supervisor.watched_paths
    for child in ignored.iterdir():
        child.rmdir()
    ignored.rmdir()
    original = observer.schedule

    def fail_root(handler, path, **kwargs):
        if path == str(tmp_path) and kwargs.get("recursive"):
            raise OSError("cannot allocate root watch")
        return original(handler, path, **kwargs)

    monkeypatch.setattr(observer, "schedule", fail_root)
    if hasattr(supervisor, "request_replan"):
        supervisor.request_replan()
    supervisor.sync_watches()
    assert str(source) in supervisor.watched_paths, "child released despite failed root replacement"


@pytest.mark.parametrize("transition", ["split", "collapse"])
def test_973_failed_replan_keeps_delivering_deep_edits(tmp_path, monkeypatch, transition):
    import threading

    from watchdog.events import FileSystemEventHandler
    from watchdog.observers.polling import PollingObserver

    from code_review_graph.incremental import _load_ignore_patterns, _WatchSupervisor

    source = tmp_path / "src"
    file = source / "deep" / "held.py"
    file.parent.mkdir(parents=True)
    file.write_text("before = 1\n")
    ignored = tmp_path / "node_modules"

    def create_ignored():
        for index in range(6):
            (ignored / str(index)).mkdir(parents=True)

    if transition == "collapse":
        create_ignored()
    delivered = threading.Event()

    class Handler(FileSystemEventHandler):
        def on_modified(self, event):
            if event.src_path == str(file):
                delivered.set()

    observer = PollingObserver(timeout=0.03)
    supervisor = _WatchSupervisor(observer, tmp_path, _load_ignore_patterns(tmp_path))
    supervisor.schedule_initial(Handler())
    observer.start()
    original = observer.schedule

    def fail_registration(handler, path, **kwargs):
        if (transition == "split" and path == str(source)) or (
            transition == "collapse" and path == str(tmp_path) and kwargs.get("recursive")
        ):
            raise OSError("controlled replacement registration failure")
        return original(handler, path, **kwargs)

    try:
        if transition == "split":
            create_ignored()
        else:
            for child in ignored.iterdir():
                child.rmdir()
            ignored.rmdir()
        monkeypatch.setattr(observer, "schedule", fail_registration)
        if hasattr(supervisor, "request_replan"):
            supervisor.request_replan()
        supervisor.sync_watches()
        file.write_text("after = 'subsequent deep edit'\n")
        assert delivered.wait(2.0), "deep edit delivery lost after failed replan"
    finally:
        observer.stop()
        observer.join(timeout=5)


def _big_ignored_tree(root, count=6):
    for index in range(count):
        (root / "node_modules" / str(index)).mkdir(parents=True)


def _supervisor(root, observer=None, **kwargs):
    from code_review_graph.incremental import _load_ignore_patterns, _WatchSupervisor
    from tests.test_watch_robustness import FakeObserver

    observer = observer or FakeObserver()
    supervisor = _WatchSupervisor(observer, root, _load_ignore_patterns(root), **kwargs)
    supervisor.schedule_initial(MagicMock())
    return supervisor, observer


def test_successful_replan_clears_a_startup_budget_fallback(tmp_path):
    """Once the exact plan fits again, `crg-daemon status` must stop saying partial."""
    for name in ("a", "b", "c"):
        _big_ignored_tree(tmp_path / name)
    supervisor, _ = _supervisor(tmp_path, max_schedules=2)
    assert supervisor.degraded is True

    for name in ("a", "b", "c"):
        shutil.rmtree(tmp_path / name / "node_modules")
    supervisor.request_replan()
    supervisor.sync_watches()

    assert supervisor.watched_paths == [str(tmp_path)]
    assert supervisor.degraded is False


def test_replan_clears_refusals_the_new_plan_covers(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    _big_ignored_tree(tmp_path)
    supervisor, _ = _supervisor(tmp_path)
    assert str(source) in supervisor.watched_paths
    refused = tmp_path / "refused"
    refused.mkdir()
    supervisor._note_unwatched(str(refused))
    assert supervisor.degraded is True

    shutil.rmtree(tmp_path / "node_modules")
    supervisor.request_replan()
    supervisor.sync_watches()

    assert supervisor.watched_paths == [str(tmp_path)]
    assert supervisor.unwatched_paths == []
    assert supervisor.degraded is False


def test_replan_that_cannot_fit_keeps_the_affordable_plan_installed(tmp_path):
    """No plan fits, but the watches in place do: the root must not take over."""
    keep = tmp_path / "keep"
    keep.mkdir()
    _big_ignored_tree(tmp_path)
    supervisor, observer = _supervisor(tmp_path, max_schedules=3)
    before = supervisor.watched_paths
    assert str(tmp_path) in supervisor._shallow

    module = tmp_path / "moduleA"
    (module / "src").mkdir(parents=True)
    _big_ignored_tree(module)
    supervisor.sync_watches()
    assert str(module) in supervisor.watched_paths
    installed = supervisor.watched_paths
    assert installed != before

    supervisor.request_replan()
    supervisor.sync_watches()

    assert supervisor.watched_paths == installed
    assert str(tmp_path) in supervisor._shallow
    assert observer.unscheduled == []
    assert (str(tmp_path), True) not in observer.scheduled


def test_failed_replan_retries_by_itself_after_the_cool_down(tmp_path, monkeypatch):
    """Nothing else asks again on a quiet repository, so the request has to survive."""
    from code_review_graph import incremental

    source = tmp_path / "src"
    source.mkdir()
    supervisor, observer = _supervisor(tmp_path)
    assert supervisor.watched_paths == [str(tmp_path)]
    _big_ignored_tree(tmp_path)
    original = observer.schedule
    refusing = {"on": True}

    def refuse_child(handler, path, **kwargs):
        if refusing["on"] and path == str(source):
            raise OSError("watch limit")
        return original(handler, path, **kwargs)

    monkeypatch.setattr(observer, "schedule", refuse_child)
    clock = {"now": 1000.0}
    monkeypatch.setattr(incremental.time, "monotonic", lambda: clock["now"])

    supervisor.request_replan()
    supervisor.sync_watches()
    assert supervisor.watched_paths == [str(tmp_path)]
    assert supervisor.degraded is True

    refusing["on"] = False
    attempts = len(observer.scheduled)
    supervisor.sync_watches()
    assert len(observer.scheduled) == attempts, "retried inside the cool-down"
    assert supervisor.degraded is True

    clock["now"] += incremental._PROMOTION_RETRY_SECONDS + 1
    supervisor.sync_watches()

    assert str(tmp_path) in supervisor._shallow
    assert str(source) in supervisor.watched_paths
    assert not any("node_modules" in path for path in supervisor.watched_paths)
    assert supervisor.degraded is False


def test_directory_events_reach_the_replan_request(tmp_path):
    """The wiring in watch(): without it nothing asks for a replan on a recursive root."""
    from watchdog.events import DirCreatedEvent, DirModifiedEvent, FileCreatedEvent

    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import _create_watch_handler

    requests = []
    with GraphStore(tmp_path / "graph.db") as store:
        handler = _create_watch_handler(
            tmp_path, store, None, lambda: requests.append(True),
        )
        handler.dispatch(DirModifiedEvent(str(tmp_path / "a")))
        handler.dispatch(FileCreatedEvent(str(tmp_path / "a.py")))
        assert requests == []
        handler.dispatch(DirCreatedEvent(str(tmp_path / "node_modules")))
        assert requests == [True]
