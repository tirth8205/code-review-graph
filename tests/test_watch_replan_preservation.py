"""Failed watch-plan transitions must preserve existing event coverage."""

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
