"""One process watches every repository a manifest names (``watch --manifest``).

Real repositories, real graphs, the real watchdog observer: the tests prove
that both repositories are watched by this one process, that an edit reaches
the right graph, that replacing the manifest stops and starts watchers live,
and that a manifest naming an unbuilt graph is refused before anything runs.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from code_review_graph.daemon import read_watch_health, watch_health_path
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.watch_set import load_manifest, watch_set


def cli_main_argv(argv: list[str]) -> None:
    """Run the real console entry point with ``argv``."""
    import sys
    from unittest.mock import patch

    from code_review_graph.cli import main

    with patch.object(sys, "argv", ["code-review-graph", *argv]):
        main()

_DEADLINE_SECONDS = 60.0


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "crg-home"))
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")


def _repo(root: Path, name: str) -> tuple[Path, Path]:
    repo = root / name
    repo.mkdir()
    (repo / "app.py").write_text("def first():\n    return 1\n", encoding="utf-8")
    data_dir = root / f"{name}-graph"
    data_dir.mkdir()
    with GraphStore(data_dir / "graph.db") as store:
        full_build(repo, store)
    return repo, data_dir


def _write_manifest(path: Path, entries: list[tuple[Path, Path]]) -> None:
    staged = path.with_suffix(".tmp")
    staged.write_text(
        json.dumps({"repos": [{"repo": str(r), "data_dir": str(d)} for r, d in entries]}),
        encoding="utf-8",
    )
    os.replace(staged, path)


def _until(predicate, what: str) -> None:
    deadline = time.monotonic() + _DEADLINE_SECONDS
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.2)


def _watching(repo: Path) -> bool:
    health = read_watch_health(repo)
    return bool(health) and health.get("phase") == "watching"


def _has_function(data_dir: Path, name: str) -> bool:
    with GraphStore(data_dir / "graph.db") as store:
        return any(node.name == name for node in store.get_all_nodes())


class TestWatchSet:
    def test_one_process_watches_every_repo_and_follows_the_manifest(self, tmp_path):
        first, first_data = _repo(tmp_path, "first")
        second, second_data = _repo(tmp_path, "second")
        manifest = tmp_path / "watch.json"
        _write_manifest(manifest, [(first, first_data), (second, second_data)])
        stop = threading.Event()
        failures: list[BaseException] = []

        def run() -> None:
            try:
                watch_set(manifest, stop_event=stop)
            except BaseException as failure:  # surfaced to the test thread
                failures.append(failure)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            _until(lambda: _watching(first) and _watching(second), "both watchers")
            pids = {read_watch_health(repo)["pid"] for repo in (first, second)}
            assert pids == {os.getpid()}

            (first / "added.py").write_text("def added_later():\n    return 2\n", encoding="utf-8")
            _until(lambda: _has_function(first_data, "added_later"), "the edit in first")
            assert not _has_function(second_data, "added_later")

            _write_manifest(manifest, [(first, first_data)])
            _until(lambda: not watch_health_path(second).exists(), "second to stop")
            assert _watching(first)

            _write_manifest(manifest, [(first, first_data), (second, second_data)])
            _until(lambda: _watching(second), "second to start again")
            assert read_watch_health(second)["pid"] == os.getpid()
        finally:
            stop.set()
            thread.join(timeout=_DEADLINE_SECONDS)
        assert not thread.is_alive()
        assert failures == []
        assert not watch_health_path(first).exists()

    def test_shared_manifest_routes_edits_to_the_selected_checkout(self, tmp_path, monkeypatch):
        database = tmp_path / "shared.db"
        monkeypatch.setenv("CRG_SHARED_DB", str(database))
        entries = [(tmp_path / name, tmp_path / "unused") for name in ("left", "right")]
        for repo, _ in entries:
            repo.mkdir()
            (repo / "app.py").write_text("def initial(): return 1\n", encoding="utf-8")
            cli_main_argv(["build", "--repo", str(repo)])
        manifest = tmp_path / "shared.json"
        _write_manifest(manifest, entries)
        stop = threading.Event()
        failures: list[BaseException] = []

        def run() -> None:
            from code_review_graph.postprocessing import run_post_processing

            try:
                watch_set(manifest, stop_event=stop, on_files_updated=run_post_processing)
            except BaseException as failure:
                failures.append(failure)

        def names(repo: Path) -> set[str]:
            with GraphStore(database, checkout=repo, read_only=True) as store:
                return {node.name for node in store.get_all_nodes() if node.kind == "Function"}

        left, right = (repo for repo, _ in entries)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            _until(lambda: _watching(left) and _watching(right), "shared manifest watchers")
            (left / "app.py").write_text("def changed_on_left(): return 2\n", encoding="utf-8")
            _until(lambda: names(left) == {"changed_on_left"}, "left checkout edit")
            assert names(right) == {"initial"}
            assert list(tmp_path.rglob("*.db")) == [database]
        finally:
            stop.set()
            thread.join(timeout=_DEADLINE_SECONDS)
        assert not thread.is_alive()
        assert failures == []

    def test_unbuilt_graph_is_refused_before_watching(self, tmp_path):
        repo = tmp_path / "unbuilt"
        repo.mkdir()
        empty = tmp_path / "unbuilt-graph"
        empty.mkdir()
        manifest = tmp_path / "watch.json"
        _write_manifest(manifest, [(repo, empty)])

        with pytest.raises(ValueError, match="build it before watching"):
            watch_set(manifest, stop_event=threading.Event())
        assert not watch_health_path(repo).exists()

    def test_duplicate_repository_is_refused(self, tmp_path):
        repo, data_dir = _repo(tmp_path, "dup")
        other = tmp_path / "other-graph"
        other.mkdir()
        (other / "graph.db").write_bytes((data_dir / "graph.db").read_bytes())
        manifest = tmp_path / "watch.json"
        _write_manifest(manifest, [(repo, data_dir), (repo, other)])

        with pytest.raises(ValueError, match="duplicate repository"):
            load_manifest(manifest)


class TestWatchManifestCli:
    def test_manifest_rejects_repo_and_data_dir(self, tmp_path, capsys):
        manifest = tmp_path / "watch.json"
        _write_manifest(manifest, [])
        with pytest.raises(SystemExit) as exited:
            cli_main_argv(["watch", "--manifest", str(manifest), "--repo", str(tmp_path)])
        assert exited.value.code == 2
        assert "drop --repo and --data-dir" in capsys.readouterr().err

    def test_failing_watcher_exits_one(self, tmp_path, capsys):
        repo, data_dir = _repo(tmp_path, "broken")
        # A graph whose file is not a database fails when the watcher opens it.
        (data_dir / "graph.db").write_bytes(b"not a sqlite database")
        manifest = tmp_path / "watch.json"
        _write_manifest(manifest, [(repo, data_dir)])
        with pytest.raises(SystemExit) as exited:
            cli_main_argv(["watch", "--manifest", str(manifest)])
        assert exited.value.code == 1
        assert f"watch for {repo} failed" in capsys.readouterr().err


class TestQueuedWatcherLiveness:
    def test_dead_observer_while_queued_fails_the_watcher(self, tmp_path, monkeypatch):
        """A watcher waiting for another repository's build still dies loudly."""
        from watchdog.observers import Observer

        from code_review_graph.incremental import watch

        repo, data_dir = _repo(tmp_path, "queued")
        startup_lock = threading.Lock()
        observer = Observer()
        monkeypatch.setattr("watchdog.observers.Observer", lambda: observer)
        failures: list[BaseException] = []

        def run() -> None:
            with GraphStore(data_dir / "graph.db") as store:
                try:
                    watch(repo, store, startup_lock=startup_lock)
                except BaseException as failure:  # surfaced to the test thread
                    failures.append(failure)

        startup_lock.acquire()
        thread = threading.Thread(target=run, daemon=True)
        try:
            thread.start()
            _until(
                lambda: (read_watch_health(repo) or {}).get("phase") == "queued",
                "the watcher to queue",
            )
            _until(lambda: bool(observer.emitters), "the observer to schedule")
            # The dispatcher thread dying is a death no reschedule repairs.
            observer.stop()
            thread.join(timeout=_DEADLINE_SECONDS)
        finally:
            startup_lock.release()
        assert not thread.is_alive()
        assert len(failures) == 1
        assert isinstance(failures[0], RuntimeError)
        assert "stopped while queued" in str(failures[0])
