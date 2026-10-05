"""Rule refresh must update actual watch coverage, not just batch filtering."""

import threading

import pytest
from watchdog.events import FileModifiedEvent
from watchdog.observers.polling import PollingObserver

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import (
    _WATCH_SPLIT_MIN_DIRS,
    _create_watch_handler,
    _load_ignore_patterns,
    _sync_watch_tree,
    _WatchSupervisor,
    clear_nested_ignore_cache,
)
from tests.test_watch_robustness import FakeObserver


@pytest.fixture(autouse=True)
def fresh_cache():
    clear_nested_ignore_cache()
    yield
    clear_nested_ignore_cache()


def excluded_tree(root):
    config = root / ".code-review-graphignore"
    config.write_text("generated/\n")
    for index in range(_WATCH_SPLIT_MIN_DIRS + 1):
        (root / "generated" / str(index)).mkdir(parents=True)
    source = root / "generated" / "0" / "user.py"
    source.write_text("def before(): return 1\n")
    return config, source


def create_watch(root, store, observer, callback=None, *, max_schedules=128):
    supervisor = _WatchSupervisor(
        observer,
        root,
        _load_ignore_patterns(root),
        max_schedules=max_schedules,
    )
    handler = _create_watch_handler(root, store, callback)
    supervisor.schedule_initial(handler)
    return supervisor, handler


def test_relaxed_rule_adopts_existing_excluded_tree(tmp_path):
    config, source = excluded_tree(tmp_path)
    observer = FakeObserver()
    with GraphStore(tmp_path / "graph.db") as store:
        supervisor, handler = create_watch(tmp_path, store, observer)
        assert observer.scheduled == [(str(tmp_path), False)]
        config.write_text("")
        handler.process([FileModifiedEvent(str(config))])
        handler.raise_if_failed()
        assert store.get_nodes_by_file(str(source))
        _sync_watch_tree(supervisor, handler)
        assert (str(tmp_path / "generated"), True) in observer.scheduled


def test_stricter_rules_prevent_adopting_new_excluded_tree(tmp_path):
    config, _ = excluded_tree(tmp_path)
    observer = FakeObserver()
    with GraphStore(tmp_path / "graph.db") as store:
        supervisor, handler = create_watch(tmp_path, store, observer)
        excluded = tmp_path / "newly_blocked"
        excluded.mkdir()
        (excluded / "user.py").write_text("def excluded(): return 1\n")
        config.write_text("generated/\nnewly_blocked/\n")
        handler.process([FileModifiedEvent(str(config))])
        handler.raise_if_failed()
        _sync_watch_tree(supervisor, handler)
        assert str(excluded) not in supervisor.watched_paths
        assert store.get_all_files() == []


@pytest.mark.parametrize("max_schedules", [1, 128])
def test_failed_relaxed_rule_adoption_is_explicit(tmp_path, monkeypatch, max_schedules):
    config, _ = excluded_tree(tmp_path)
    observer = FakeObserver()
    with GraphStore(tmp_path / "graph.db") as store:
        supervisor, handler = create_watch(
            tmp_path,
            store,
            observer,
            max_schedules=max_schedules,
        )
        before = supervisor.watched_paths
        config.write_text("")
        handler.process([FileModifiedEvent(str(config))])
        handler.raise_if_failed()

        def fail(*args, **kwargs):
            raise OSError("No space left on device")

        monkeypatch.setattr(observer, "schedule", fail)
        with pytest.raises(RuntimeError, match="newly included"):
            _sync_watch_tree(supervisor, handler)
        assert supervisor.watched_paths == before


def test_polling_observer_delivers_subsequent_deep_edit_after_rule_relaxation(
    tmp_path,
    monkeypatch,
):
    config, source = excluded_tree(tmp_path)
    monkeypatch.setattr("code_review_graph.incremental._DEBOUNCE_SECONDS", 0.02)
    observer = PollingObserver(timeout=0.02)
    delivered = threading.Event()
    updated = threading.Event()
    with GraphStore(tmp_path / "graph.db") as store:

        def callback(graph):
            if any(node.name == "after" for node in graph.get_nodes_by_file(str(source))):
                updated.set()

        supervisor, handler = create_watch(tmp_path, store, observer, callback)
        original_dispatch = handler.dispatch

        def record_delivery(event):
            if event.src_path == str(source) and event.event_type == "modified":
                delivered.set()
            original_dispatch(event)

        handler.dispatch = record_delivery
        config.write_text("")
        handler.process([FileModifiedEvent(str(config))])
        handler.raise_if_failed()
        _sync_watch_tree(supervisor, handler)
        handler.start()
        observer.start()
        try:
            source.write_text("def after(): return 12345\n")
            assert delivered.wait(3), "newly included deep file has no observer coverage"
            assert updated.wait(3), "delivered edit did not refresh the graph"
            handler.raise_if_failed()
        finally:
            observer.stop()
            observer.join(timeout=3)
            handler.stop()
