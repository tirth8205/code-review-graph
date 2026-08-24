"""Watch-mode robustness: ignore-aware scheduling and observer liveness.

Covers issue #811, where one recursive watch on the repository root made the
OS register a watch inside every ignored tree.  A build tool churning through
``target/`` then killed a watchdog thread, the process stayed up, and the graph
silently stopped updating while ``crg-daemon status`` still said "alive".

Every test here is deterministic: observers are fakes, and a "dead" watchdog
thread is a real thread that has been joined, never a crash we tried to
provoke through the filesystem.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_review_graph.daemon import (
    DaemonConfig,
    WatchRepo,
    read_watch_health,
    watch_health_path,
    watcher_status,
)
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import (
    _WATCH_SPLIT_MIN_DIRS,
    _load_ignore_patterns,
    _plan_watch_paths,
    _should_ignore,
    _WatchSupervisor,
    clear_nested_ignore_cache,
    incremental_update,
    watch,
)
from code_review_graph.parser import NodeInfo


@pytest.fixture(autouse=True)
def _fresh_nested_ignore_cache():
    """Nested build-output patterns are cached per repo; tests build repos."""
    clear_nested_ignore_cache()
    yield
    clear_nested_ignore_cache()


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeWatch:
    """Stand-in for watchdog's ObservedWatch."""

    def __init__(self, path: str, recursive: bool) -> None:
        self.path = path
        self.is_recursive = recursive


class FakeEmitter:
    """Stand-in for a watchdog emitter that owns a backend reader thread.

    inotify keeps its buffer thread exactly like this, and that buffer thread
    is the one that dies in #811 — the emitter itself stays alive.
    """

    def __init__(self, reader: threading.Thread | None = None, root: str | None = None) -> None:
        self._inotify = reader
        self.watch = FakeWatch(root, True) if root is not None else None


class FakeObserver:
    """Records what would have been handed to the OS."""

    def __init__(self) -> None:
        self.scheduled: list[tuple[str, bool]] = []
        self.unscheduled: list[str] = []
        self.emitters: list[object] = []
        self.handler = None
        self.started = False
        self.stopped = False

    def schedule(self, handler, path, *, recursive=False, event_filter=None):
        self.handler = handler
        self.scheduled.append((path, recursive))
        return FakeWatch(path, recursive)

    def unschedule(self, watch_handle) -> None:
        self.unscheduled.append(watch_handle.path)
        # watchdog drops the emitter (and its threads) with the watch; the
        # liveness check depends on that, so the double must do it too.
        self.emitters = [
            emitter
            for emitter in self.emitters
            if getattr(getattr(emitter, "watch", None), "path", None) != watch_handle.path
        ]

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def join(self, timeout=None) -> None:
        return None


def _live_thread() -> tuple[threading.Thread, threading.Event]:
    """A thread that stays alive until its event is set."""
    gate = threading.Event()
    thread = threading.Thread(target=gate.wait, name="fake-inotify-buffer", daemon=True)
    thread.start()
    return thread, gate



def _tick_driver(*actions):
    """Replacement for ``time.sleep`` that runs one scripted action per tick.

    After the script is exhausted the watch loop is stopped with a
    KeyboardInterrupt, which is how a real ``Ctrl+C`` leaves it.
    """
    state = {"tick": 0}

    def _sleep(_seconds):
        index = state["tick"]
        state["tick"] += 1
        if index >= len(actions):
            raise KeyboardInterrupt
        actions[index]()

    return _sleep


def _maven_repo(root: Path) -> None:
    """A monorepo shaped like the one in #811: nested modules with target/."""
    (root / ".git" / "objects").mkdir(parents=True)
    for index in range(8):
        (root / ".git" / "objects" / f"{index:02d}").mkdir()
    (root / "pom.xml").write_text("<project/>", encoding="utf-8")
    module = root / "intranet-backend"
    (module / "src" / "main" / "java").mkdir(parents=True)
    (module / "pom.xml").write_text("<project/>", encoding="utf-8")
    for index in range(6):
        (module / "target" / f"surefire-{index}").mkdir(parents=True)
    (module / "target" / "classes").mkdir(parents=True)


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


class TestIgnoreAwareScheduling:
    def test_ignored_top_level_directories_are_never_scheduled(self, tmp_path):
        """node_modules and .git must not reach the OS watch list at all."""
        (tmp_path / "src").mkdir()
        for index in range(6):
            (tmp_path / "node_modules" / f"pkg{index}").mkdir(parents=True)
        (tmp_path / ".git" / "objects").mkdir(parents=True)

        plan = _plan_watch_paths(tmp_path, _load_ignore_patterns(tmp_path))
        paths = [str(path) for path, _ in plan]

        assert (tmp_path, False) in plan, "the repo root needs a non-recursive watch"
        assert (tmp_path / "src", True) in plan
        assert not any("node_modules" in path for path in paths)
        assert not any(".git" in path for path in paths)

    def test_root_watch_is_non_recursive_so_ignored_trees_stay_out(self, tmp_path):
        """A recursive root watch would re-register everything underneath it."""
        (tmp_path / "src").mkdir()
        for index in range(6):
            (tmp_path / "node_modules" / f"pkg{index}").mkdir(parents=True)

        plan = _plan_watch_paths(tmp_path, _load_ignore_patterns(tmp_path))

        assert (tmp_path, True) not in plan

    def test_clean_repository_keeps_a_single_recursive_watch(self, tmp_path):
        """Nothing to exclude means nothing to split: one watch, as before."""
        (tmp_path / "src" / "inner").mkdir(parents=True)

        plan = _plan_watch_paths(tmp_path, _load_ignore_patterns(tmp_path))

        assert plan == [(tmp_path, True)]

    def test_small_ignored_directory_does_not_buy_its_own_watch(self, tmp_path):
        """A lone __pycache__ costs one OS watch; a split costs a thread."""
        (tmp_path / "pkg" / "__pycache__").mkdir(parents=True)

        plan = _plan_watch_paths(tmp_path, _load_ignore_patterns(tmp_path))

        assert plan == [(tmp_path, True)]

    def test_plan_falls_back_to_one_recursive_watch_when_over_budget(self, tmp_path):
        """The watch count is bounded; an over-budget repo keeps the old shape."""
        for index in range(10):
            (tmp_path / f"pkg{index}").mkdir()
        for index in range(6):
            (tmp_path / "node_modules" / f"dep{index}").mkdir(parents=True)

        plan = _plan_watch_paths(
            tmp_path, _load_ignore_patterns(tmp_path), max_schedules=3
        )

        assert plan == [(tmp_path, True)]

    def test_nested_module_output_is_ignored_and_never_watched(self, tmp_path):
        """`moduleA/target/` is build output when `moduleA/pom.xml` says so."""
        _maven_repo(tmp_path)
        patterns = _load_ignore_patterns(tmp_path)

        assert "/intranet-backend/target/**" in patterns
        assert _should_ignore("intranet-backend/target/surefire-0/a.xml", patterns)
        assert not _should_ignore("intranet-backend/src/main/java/A.java", patterns)

        plan = _plan_watch_paths(tmp_path, patterns)
        paths = [str(path) for path, _ in plan]

        assert not any("target" in path for path in paths)
        assert (tmp_path / "intranet-backend" / "src", True) in plan

    def test_nested_output_name_without_a_manifest_keeps_its_files(self, tmp_path):
        """Root anchoring stays intact for anyone whose nested target/ is source."""
        module = tmp_path / "moduleB"
        (module / "target").mkdir(parents=True)
        (module / "target" / "handler.py").write_text("x = 1\n", encoding="utf-8")

        patterns = _load_ignore_patterns(tmp_path)

        assert "/moduleB/target/**" not in patterns
        assert not _should_ignore("moduleB/target/handler.py", patterns)

    def test_root_level_output_dirs_still_match_the_anchored_pattern(self, tmp_path):
        """The nested scan adds patterns; it never removes existing ones."""
        patterns = _load_ignore_patterns(tmp_path)

        assert _should_ignore("target/classes/A.class", patterns)
        assert _should_ignore("build/output.js", patterns)
        assert not _should_ignore("src/build/output.js", patterns)

    def test_nested_scan_can_be_disabled(self, tmp_path, monkeypatch):
        _maven_repo(tmp_path)
        monkeypatch.setenv("CRG_NESTED_OUTPUT_SCAN", "0")
        clear_nested_ignore_cache()

        patterns = _load_ignore_patterns(tmp_path)

        assert "/intranet-backend/target/**" not in patterns

    def test_a_single_path_can_opt_out(self, tmp_path):
        """Dropping files from the graph needs an escape smaller than a kill switch."""
        _maven_repo(tmp_path)
        (tmp_path / ".code-review-graphignore").write_text(
            "# keep our hand-written module\n!intranet-backend/target\n", encoding="utf-8"
        )
        clear_nested_ignore_cache()

        patterns = _load_ignore_patterns(tmp_path)

        assert "/intranet-backend/target/**" not in patterns
        assert not _should_ignore("intranet-backend/target/Main.java", patterns)

    def test_excluded_directories_are_logged(self, tmp_path, caplog):
        """An exclusion drops files from the graph, so it has to be discoverable."""
        _maven_repo(tmp_path)
        clear_nested_ignore_cache()

        with caplog.at_level(logging.INFO, logger="code_review_graph.incremental"):
            _load_ignore_patterns(tmp_path)

        assert "intranet-backend/target" in caplog.text
        assert ".code-review-graphignore" in caplog.text


class TestNewDirectoryAdoption:
    """Adoption is driven by a per-tick listing, never by directory events.

    macOS drops every directory event for a child of a non-recursive watch, so
    an event-driven design is permanently blind to a new top-level directory.
    """

    def _supervisor(self, tmp_path, **kwargs) -> tuple[_WatchSupervisor, FakeObserver]:
        (tmp_path / "src").mkdir(exist_ok=True)
        for index in range(6):
            (tmp_path / "node_modules" / f"pkg{index}").mkdir(parents=True, exist_ok=True)
        observer = FakeObserver()
        supervisor = _WatchSupervisor(
            observer, tmp_path, _load_ignore_patterns(tmp_path), health_path=None, **kwargs
        )
        supervisor.schedule_initial(MagicMock())
        return supervisor, observer

    def test_new_top_level_directory_is_picked_up(self, tmp_path):
        """A non-recursive root watch only helps if new children get scheduled."""
        supervisor, observer = self._supervisor(tmp_path)
        before = list(observer.scheduled)

        created = tmp_path / "services"
        created.mkdir()
        adopted, vanished = supervisor.sync_watches()

        assert adopted == [str(created)]
        assert vanished == []
        assert (str(created), True) in observer.scheduled
        assert (str(created), True) not in before

    def test_recreated_directory_is_watched_again(self, tmp_path):
        """`rm -rf src && mkdir src` must not leave src unwatched forever."""
        supervisor, observer = self._supervisor(tmp_path)

        shutil.rmtree(tmp_path / "src")
        _, vanished = supervisor.sync_watches()
        assert vanished == [str(tmp_path / "src")]
        assert observer.unscheduled == [str(tmp_path / "src")]

        (tmp_path / "src").mkdir()
        adopted, _ = supervisor.sync_watches()

        assert adopted == [str(tmp_path / "src")]
        assert observer.scheduled.count((str(tmp_path / "src"), True)) == 2

    def test_new_ignored_directory_is_not_picked_up(self, tmp_path):
        supervisor, observer = self._supervisor(tmp_path)

        created = tmp_path / "dist"
        created.mkdir()
        adopted, _ = supervisor.sync_watches()

        assert adopted == []
        assert not any(path == str(created) for path, _ in observer.scheduled)

    def test_directory_inside_a_recursive_watch_is_not_rescheduled(self, tmp_path):
        """watchdog already covers those; a second watch would be waste."""
        supervisor, observer = self._supervisor(tmp_path)

        created = tmp_path / "src" / "nested"
        created.mkdir()
        adopted, _ = supervisor.sync_watches()

        assert adopted == []
        assert not any(path == str(created) for path, _ in observer.scheduled)

    def test_deleted_directory_releases_its_watch(self, tmp_path):
        supervisor, observer = self._supervisor(tmp_path)

        shutil.rmtree(tmp_path / "src")
        adopted, vanished = supervisor.sync_watches()

        assert vanished == [str(tmp_path / "src")]
        assert observer.unscheduled == [str(tmp_path / "src")]

    def test_a_quiet_tick_touches_nothing(self, tmp_path):
        supervisor, observer = self._supervisor(tmp_path)
        before = list(observer.scheduled)

        assert supervisor.sync_watches() == ([], [])
        assert observer.scheduled == before
        assert observer.unscheduled == []

    def test_relative_repo_root_still_adopts_and_releases(self, tmp_path, monkeypatch):
        """`--repo .` stays relative in the CLI; the supervisor must resolve it."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "src").mkdir()
        for index in range(6):
            (tmp_path / "node_modules" / f"pkg{index}").mkdir(parents=True)
        observer = FakeObserver()
        supervisor = _WatchSupervisor(
            observer, Path("."), _load_ignore_patterns(Path(".")), health_path=None
        )
        supervisor.schedule_initial(MagicMock())

        (tmp_path / "services").mkdir()
        adopted, _ = supervisor.sync_watches()

        assert adopted == [str(tmp_path / "services")]

        shutil.rmtree(tmp_path / "services")
        _, vanished = supervisor.sync_watches()

        assert vanished == [str(tmp_path / "services")]

    def test_adopted_subtree_is_planned_like_the_repository(self, tmp_path):
        """A module arriving from a branch switch must not re-register its junk.

        Watching an adopted directory recursively would hand `node_modules`
        and `target` straight back to the OS — the exposure #811 is about, for
        everything created after startup.
        """
        supervisor, observer = self._supervisor(tmp_path)

        module = tmp_path / "moduleA"
        (module / "src").mkdir(parents=True)
        for index in range(6):
            (module / "node_modules" / f"pkg{index}").mkdir(parents=True)
        adopted, _ = supervisor.sync_watches()

        assert adopted == [str(module)]
        assert (str(module), False) in observer.scheduled, "module needs a shallow watch"
        assert (str(module / "src"), True) in observer.scheduled
        assert not any(
            "node_modules" in path for path, _ in observer.scheduled
        ), "the adopted module's node_modules was handed to the OS"

    def test_a_tight_budget_narrows_the_plan_before_promoting(self, tmp_path):
        """A fix for #811 must not be able to re-create #811.

        Promoting the parent — usually the repository root — hands every
        ignored tree in the repository back to the OS. A coarser plan for the
        new directory alone costs one slot and leaves the rest filtered.
        """
        for index in range(6):
            (tmp_path / "node_modules" / f"pkg{index}").mkdir(parents=True)
        (tmp_path / "keep").mkdir()
        observer = FakeObserver()
        supervisor = _WatchSupervisor(
            observer,
            tmp_path,
            _load_ignore_patterns(tmp_path),
            health_path=None,
            max_schedules=3,
        )
        supervisor.schedule_initial(MagicMock())
        assert len(supervisor.watched_paths) == 2, "precondition: one slot left"

        module = tmp_path / "moduleA"
        (module / "src" / "lib").mkdir(parents=True)
        for index in range(6):
            (module / "src" / "node_modules" / f"pkg{index}").mkdir(parents=True)
        adopted, _ = supervisor.sync_watches()

        assert adopted == [str(module)]
        assert supervisor.degraded is False, "the repository root was promoted"
        assert (str(tmp_path), True) not in observer.scheduled
        assert str(tmp_path) in supervisor._shallow, "the root lost its filtering"
        assert (str(module), True) in observer.scheduled, "the new module is unwatched"

    def test_promotion_releases_grandchildren_too(self, tmp_path):
        """A stale grandchild watch duplicates every event and burns a slot."""
        (tmp_path / "src").mkdir()
        for index in range(6):
            (tmp_path / "node_modules" / f"pkg{index}").mkdir(parents=True)
        module = tmp_path / "moduleA"
        (module / "src").mkdir(parents=True)
        for index in range(6):
            (module / "node_modules" / f"pkg{index}").mkdir(parents=True)
        observer = FakeObserver()
        supervisor = _WatchSupervisor(
            observer,
            tmp_path,
            _load_ignore_patterns(tmp_path),
            health_path=None,
            max_schedules=4,
        )
        supervisor.schedule_initial(MagicMock())
        assert str(module / "src") in supervisor.watched_paths, "precondition: grandchild watch"

        (tmp_path / "services").mkdir()
        supervisor.sync_watches()

        assert supervisor.degraded is True
        assert supervisor.watched_paths == [str(tmp_path)], "grandchildren outlived the promotion"

    def test_budget_exhaustion_degrades_to_a_recursive_watch(self, tmp_path):
        """Covering less would be silent blindness; cover more instead."""
        supervisor, observer = self._supervisor(tmp_path, max_schedules=2)
        assert supervisor.degraded is False

        (tmp_path / "services").mkdir()
        (tmp_path / "services" / "deep").mkdir()
        adopted, _ = supervisor.sync_watches()

        assert supervisor.degraded is True
        assert (str(tmp_path), True) in observer.scheduled, "root now watched recursively"
        assert supervisor.watched_paths == [str(tmp_path)]
        assert adopted == [str(tmp_path / "services")]

    def test_degraded_watchers_are_reported_as_partial(self, tmp_path):
        supervisor, _ = self._supervisor(tmp_path, max_schedules=2)
        supervisor._health_path = tmp_path / "health.json"

        (tmp_path / "services").mkdir()
        supervisor.sync_watches()
        supervisor.report_health(observer_alive=True, force=True)

        health = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
        assert health["degraded"] is True
        assert watcher_status(True, {**health, "stalled": False}) == "partial"


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------


class TestObserverLiveness:
    def _watched(self, tmp_path, name="lib"):
        """A supervisor with one real watch on tmp_path/<name>, plus its emitter."""
        watched = tmp_path / name
        watched.mkdir(exist_ok=True)
        observer = FakeObserver()
        supervisor = _WatchSupervisor(observer, tmp_path, [], health_path=None)
        supervisor._handler = MagicMock()
        supervisor._schedule(watched, recursive=True)
        thread, gate = _live_thread()
        observer.emitters = [FakeEmitter(thread, root=str(watched))]
        return supervisor, observer, watched, thread, gate

    def _watched_as_planned(self, tmp_path, name="src"):
        """A supervisor planned the way startup plans a repository.

        The root is watched non-recursively and each top-level directory
        recursively, which is the shape ``sync_watches`` reconciles.  The
        bare ``_watched`` fixture has no shallow parent, so a recreated
        directory there can only be noticed by the liveness check; here both
        mechanisms are in play, as they are in a real watcher.
        """
        # An ignored tree worth excluding is what makes the planner split the
        # root at all; without one it covers everything with a single
        # recursive watch and there is no shallow parent to reconcile.
        (tmp_path / "node_modules").mkdir(exist_ok=True)
        for index in range(_WATCH_SPLIT_MIN_DIRS + 2):
            (tmp_path / "node_modules" / f"pkg{index}").mkdir(exist_ok=True)
        watched = tmp_path / name
        watched.mkdir(exist_ok=True)
        observer = FakeObserver()
        supervisor = _WatchSupervisor(
            observer, tmp_path, _load_ignore_patterns(tmp_path), health_path=None
        )
        supervisor.schedule_initial(MagicMock())
        assert (str(watched), True) in observer.scheduled
        assert str(tmp_path) in supervisor._shallow, "the root must be the shallow parent"
        thread, gate = _live_thread()
        observer.emitters = [FakeEmitter(thread, root=str(watched))]
        return supervisor, observer, watched, thread, gate

    def test_dead_backend_reader_thread_is_reported(self, tmp_path):
        """The inotify shape: the emitter lives on, its buffer thread does not."""
        supervisor, observer, watched, thread, gate = self._watched(tmp_path)

        assert supervisor.check_liveness() == ([], [])

        gate.set()
        thread.join(timeout=5)

        # The directory is untouched, so the first death buys one reschedule.
        dead, repaired = supervisor.check_liveness()
        assert dead == []
        assert repaired == [str(watched)]

        # watchdog gives the rescheduled watch a fresh emitter; if that one
        # dies too, the fault is real and has to stay loud.
        replacement, replacement_gate = _live_thread()
        observer.emitters = [FakeEmitter(replacement, root=str(watched))]
        assert supervisor.check_liveness() == ([], [])

        replacement_gate.set()
        replacement.join(timeout=5)
        dead, repaired = supervisor.check_liveness()

        assert dead == ["fake-inotify-buffer"]
        assert repaired == []

    def test_dead_emitter_thread_is_reported(self, tmp_path):
        """The Windows shape: the dispatch/emitter thread itself ends."""
        gate = threading.Event()

        class ThreadEmitter(threading.Thread):
            def __init__(self) -> None:
                super().__init__(name="fake-emitter", daemon=True)

            def run(self) -> None:
                gate.wait()

        emitter = ThreadEmitter()
        emitter.start()
        observer = FakeObserver()
        observer.emitters = [emitter]
        supervisor = _WatchSupervisor(observer, tmp_path, [], health_path=None)

        assert supervisor.check_liveness() == ([], [])

        gate.set()
        emitter.join(timeout=5)

        # No watch root to attribute it to, so there is nothing to reschedule.
        assert supervisor.check_liveness() == (["fake-emitter"], [])

    def test_unstarted_thread_is_never_mistaken_for_a_dead_one(self, tmp_path):
        """A watch scheduled mid-tick must not read as a corpse."""
        observer = FakeObserver()
        observer.emitters = [FakeEmitter(threading.Thread(target=lambda: None))]
        supervisor = _WatchSupervisor(observer, tmp_path, [], health_path=None)

        assert supervisor.check_liveness() == ([], [])
        assert supervisor.check_liveness() == ([], [])

    def test_unschedulable_observer_reports_no_deaths(self, tmp_path):
        """A mock or stub observer must not fake a death every tick."""
        supervisor = _WatchSupervisor(MagicMock(), tmp_path, [], health_path=None)

        assert supervisor.check_liveness() == ([], [])

    def test_deleting_a_watched_directory_is_not_a_death(self, tmp_path):
        """Both backends stop an emitter whose own root disappears.

        `rm -rf lib/` during normal work must not exit the watcher — with the
        daemon restarting on every exit, that is a restart loop paying for a
        full initial update each time.
        """
        supervisor, _, watched, thread, gate = self._watched(tmp_path)

        assert supervisor.check_liveness() == ([], [])

        shutil.rmtree(watched)
        gate.set()
        thread.join(timeout=5)

        assert supervisor.check_liveness() == ([], [])

    def test_recreated_directory_is_not_a_death(self, tmp_path):
        """`rm -rf src && mkdir src` inside one tick: same name, new directory.

        Calling that a death exits the watcher *and* loses the recreated
        contents, because the restarted watcher only reconciles stale rows
        away.  The watch is stale, not the watcher.

        Which mechanism notices is deliberately not asserted, because it is
        platform-dependent.  Where the inode changes -- macOS, and Linux
        whenever the filesystem does not hand it straight back --
        ``sync_watches`` releases the stale watch and adopts the replacement;
        where it does not, the once-per-root repair reschedules.  The
        guarantee is the outcome: no death, and the directory still covered by
        a watch that was scheduled after the swap.
        """
        supervisor, observer, watched, thread, gate = self._watched_as_planned(tmp_path)

        assert supervisor.check_liveness() == ([], [])
        scheduled_before = len(observer.scheduled)

        shutil.rmtree(watched)
        watched.mkdir()  # same path, new directory -- one tick, no gap
        gate.set()
        thread.join(timeout=5)

        # The watch loop syncs the watch list first, then checks liveness.
        supervisor.sync_watches()
        dead, _ = supervisor.check_liveness()

        assert dead == [], "a recreated directory was reported as a dead watcher"
        assert str(watched) in supervisor.watched_paths
        assert len(observer.scheduled) > scheduled_before, "the watch was never renewed"

    def test_recreated_directory_with_a_reused_inode_is_repaired(self, tmp_path, monkeypatch):
        """The Linux shape: the filesystem hands the same inode straight back.

        Linux has no ``st_birthtime``, so identity is blind to that swap and
        the once-per-root repair is what has to carry it.  Frozen here rather
        than left to the filesystem, so the path is pinned on every platform
        instead of only where inode reuse happens to occur.
        """
        monkeypatch.setattr(
            "code_review_graph.incremental._watch_identity", lambda path: (1, 2, 0.0)
        )
        supervisor, observer, watched, thread, gate = self._watched_as_planned(tmp_path)

        assert supervisor.check_liveness() == ([], [])
        scheduled_before = len(observer.scheduled)

        shutil.rmtree(watched)
        watched.mkdir()
        gate.set()
        thread.join(timeout=5)

        assert supervisor.sync_watches() == ([], []), "identity cannot see a reused inode"

        dead, repaired = supervisor.check_liveness()

        assert dead == []
        assert repaired == [str(watched)], "the repair path did not carry the recreate"
        assert len(observer.scheduled) > scheduled_before
        assert (str(watched), True) in observer.scheduled[scheduled_before:]

    def test_failed_reschedule_is_reported_rather_than_counted_as_repaired(self, tmp_path):
        """ENOSPC from inotify is #811's own trigger; it must not pass as a repair."""
        supervisor, observer, watched, thread, gate = self._watched(tmp_path)
        assert supervisor.check_liveness() == ([], [])

        gate.set()
        thread.join(timeout=5)
        observer.schedule = MagicMock(side_effect=OSError("No space left on device"))
        dead, repaired = supervisor.check_liveness()

        assert repaired == [], "an unwatched directory was reported as repaired"
        assert dead == ["fake-inotify-buffer"]
        assert str(watched) not in supervisor._repaired_roots, "bookkeeping outlived the watch"

    def test_replaced_directory_is_released_and_re_adopted(self, tmp_path):
        """The identity change has to reach the watch list, not just liveness.

        The replacement is built beside the original and renamed over it, so
        the two inodes are distinct on every filesystem. Plain delete-then-
        recreate cannot be used here: Linux commonly hands the same inode back,
        which is the repair path's job, covered separately.
        """
        (tmp_path / "node_modules").mkdir()
        for index in range(6):
            (tmp_path / "node_modules" / f"pkg{index}").mkdir()
        watched = tmp_path / "src"
        watched.mkdir()
        observer = FakeObserver()
        supervisor = _WatchSupervisor(
            observer, tmp_path, _load_ignore_patterns(tmp_path), health_path=None
        )
        supervisor.schedule_initial(MagicMock())
        assert (str(watched), True) in observer.scheduled

        replacement = tmp_path / "src.incoming"
        replacement.mkdir()  # allocated while the original still holds its inode
        assert replacement.stat().st_ino != watched.stat().st_ino
        shutil.rmtree(watched)
        replacement.rename(watched)
        adopted, vanished = supervisor.sync_watches()

        assert vanished == [str(watched)], "the stale watch was not released"
        assert adopted == [str(watched)], "the replacement was not adopted"
        assert observer.scheduled.count((str(watched), True)) == 2


class TestWatchLoop:
    def _watch_with(self, tmp_path, store, observer, sleeper, health_interval=0.0, **kwargs):
        with (
            patch("watchdog.observers.Observer", return_value=observer),
            patch("time.sleep", side_effect=sleeper),
            patch(
                "code_review_graph.incremental._WATCH_HEALTH_INTERVAL",
                health_interval,
            ),
        ):
            watch(tmp_path, store, **kwargs)

    def test_dead_observer_exits_loudly_instead_of_stalling(self, tmp_path, caplog):
        """The whole point of #811: never keep running with a dead watcher."""
        (tmp_path / "src").mkdir()
        thread, gate = _live_thread()
        observer = FakeObserver()
        observer.emitters = [FakeEmitter(thread)]
        store = GraphStore(tmp_path / "graph.db")

        def kill_the_reader():
            gate.set()
            thread.join(timeout=5)

        try:
            with caplog.at_level(logging.ERROR):
                with pytest.raises(RuntimeError, match="watch observer stopped"):
                    self._watch_with(
                        tmp_path,
                        store,
                        observer,
                        _tick_driver(lambda: None, kill_the_reader),
                    )
        finally:
            store.close()

        assert "fake-inotify-buffer" in caplog.text
        assert str(tmp_path) in caplog.text
        assert observer.stopped is True

        health = read_watch_health(tmp_path)
        assert health is not None
        assert health["observer_alive"] is False
        assert health["stalled"] is True
        assert health["dead_threads"] == ["fake-inotify-buffer"]

    def test_cli_watch_turns_a_dead_observer_into_exit_code_1(self):
        """The daemon restarts on process exit, so the exit code has to be non-zero."""
        from code_review_graph import cli

        argv = ["code-review-graph", "watch", "--repo", "repo-root"]
        with (
            patch.object(sys, "argv", argv),
            patch("code_review_graph.graph.GraphStore", return_value=MagicMock()),
            patch("code_review_graph.incremental.get_db_path", return_value=MagicMock()),
            patch(
                "code_review_graph.incremental.watch",
                side_effect=RuntimeError("watch observer stopped: dead thread(s) Thread-3"),
            ),
            pytest.raises(SystemExit) as exit_info,
        ):
            cli.main()

        assert exit_info.value.code == 1

    def test_live_observer_publishes_health_for_daemon_status(self, tmp_path):
        (tmp_path / "src").mkdir()
        thread, gate = _live_thread()
        observer = FakeObserver()
        observer.emitters = [FakeEmitter(thread)]
        store = GraphStore(tmp_path / "graph.db")
        seen: list[dict] = []

        try:
            self._watch_with(
                tmp_path,
                store,
                observer,
                _tick_driver(
                    lambda: seen.append(read_watch_health(tmp_path) or {}),
                ),
            )
        finally:
            gate.set()
            store.close()

        assert seen and seen[0]["observer_alive"] is True
        assert seen[0]["stalled"] is False
        assert seen[0]["watched_paths"] >= 1
        # A clean Ctrl+C removes the file it published.
        assert not watch_health_path(tmp_path).exists()

    def test_normal_file_change_still_updates_the_graph(self, tmp_path):
        """End to end: scheduling changes must not break ordinary updates."""
        from watchdog.events import FileCreatedEvent

        (tmp_path / "src").mkdir()
        for index in range(6):
            (tmp_path / "node_modules" / f"pkg{index}").mkdir(parents=True)
        source = tmp_path / "src" / "app.py"
        source.write_text("def handler():\n    return 1\n", encoding="utf-8")

        observer = FakeObserver()
        store = GraphStore(tmp_path / "graph.db")
        callbacks: list[int] = []
        health: dict[str, object] = {}

        def deliver_event():
            observer.handler.process([FileCreatedEvent(str(source))])

        def capture_health():
            health.update(json.loads(watch_health_path(tmp_path).read_text("utf-8")))

        try:
            self._watch_with(
                tmp_path,
                store,
                observer,
                _tick_driver(deliver_event, capture_health),
                on_files_updated=lambda _store: callbacks.append(1),
            )
            assert store.get_nodes_by_file(str(source)), "watch never parsed the change"
        finally:
            store.close()

        assert callbacks, "the post-processing callback never ran"
        assert health["observer_alive"] is True
        assert isinstance(health["last_event_at"], float)
        assert health["events_seen"] == 1
        # src is watched, node_modules is not.
        assert (str(tmp_path / "src"), True) in observer.scheduled
        assert not any("node_modules" in path for path, _ in observer.scheduled)

    def test_relative_repo_keeps_a_graph_built_with_an_absolute_root(
        self, tmp_path, monkeypatch
    ):
        """`watch --repo .` used to reconcile every file away on startup.

        Stored paths are spelled by whoever built the graph, and reconciliation
        deletes anything it cannot place under the current root — so a
        relative root deleted the lot.
        """
        (tmp_path / "src").mkdir()
        source = tmp_path / "src" / "app.py"
        source.write_text("def handler():\n    return 1\n", encoding="utf-8")
        store = GraphStore(tmp_path / "graph.db")
        incremental_update(tmp_path, store, changed_files=["src/app.py"])
        assert store.get_all_files(), "precondition: the graph has files"

        monkeypatch.chdir(tmp_path)
        try:
            self._watch_with(Path("."), store, FakeObserver(), _tick_driver())
            survivors = store.get_all_files()
        finally:
            store.close()

        assert survivors, "the relative root reconciled the whole graph away"

    def test_a_graph_from_another_root_is_refused_not_emptied(self, tmp_path):
        """A total mismatch is a misconfiguration, not a mass deletion."""
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
        store = GraphStore(tmp_path / "graph.db")
        incremental_update(tmp_path / "elsewhere", store, changed_files=[])
        store.store_file_nodes_edges(
            "/somewhere/else/mod.py",
            [
                NodeInfo(
                    kind="File",
                    name="mod.py",
                    file_path="/somewhere/else/mod.py",
                    line_start=1,
                    line_end=2,
                    language="python",
                )
            ],
            [],
            "hash",
        )
        store.commit()

        try:
            with (
                patch("watchdog.observers.Observer") as observer,
                pytest.raises(RuntimeError, match="different repository root"),
            ):
                watch(repo, store)
            assert store.get_all_files() == ["/somewhere/else/mod.py"], "nothing was deleted"
            observer.assert_not_called()
        finally:
            store.close()

    def test_repair_reconciles_files_deleted_during_the_outage(self, tmp_path):
        """A repaired watch must re-read the directory in both directions.

        Only the deletion event contributes the *stored* descendants, and watch
        batches run with ``reconcile_stale=False``, so a file removed while the
        watch was down keeps its rows forever if the repair only announces a
        creation — the silent divergence this whole issue is about.
        """
        source_dir = tmp_path / "src"
        source_dir.mkdir()
        for index in range(6):
            (tmp_path / "node_modules" / f"pkg{index}").mkdir(parents=True)
        (source_dir / "doomed.py").write_text("def doomed():\n    return 1\n", encoding="utf-8")
        (source_dir / "keeper.py").write_text("def keeper():\n    return 2\n", encoding="utf-8")
        store = GraphStore(tmp_path / "graph.db")
        incremental_update(
            tmp_path, store, changed_files=["src/doomed.py", "src/keeper.py"]
        )
        assert any(p.endswith("doomed.py") for p in store.get_all_files())

        observer = FakeObserver()
        thread, gate = _live_thread()
        reader = GraphStore(tmp_path / "graph.db")
        # The tick driver replaces time.sleep; keep the real one for polling.
        real_sleep = time.sleep

        def emitter_appears():
            observer.emitters = [FakeEmitter(thread, root=str(source_dir))]

        def outage():
            gate.set()
            thread.join(timeout=5)
            (source_dir / "doomed.py").unlink()
            (source_dir / "arrived.py").write_text(
                "def arrived():\n    return 3\n", encoding="utf-8"
            )

        def settle():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                files = reader.get_all_files()
                if any(p.endswith("arrived.py") for p in files) and not any(
                    p.endswith("doomed.py") for p in files
                ):
                    return
                real_sleep(0.05)

        try:
            with patch("code_review_graph.incremental._DEBOUNCE_SECONDS", 0.01):
                self._watch_with(
                    tmp_path,
                    store,
                    observer,
                    _tick_driver(emitter_appears, outage, settle),
                )
            files = reader.get_all_files()
        finally:
            store.close()
            reader.close()

        assert any(p.endswith("arrived.py") for p in files), "the repair never re-read the tree"
        assert any(p.endswith("keeper.py") for p in files), "an untouched file was dropped"
        assert not any(
            p.endswith("doomed.py") for p in files
        ), "a file deleted during the outage kept its rows"

    def test_health_is_published_before_the_first_build(self, tmp_path):
        """A repo whose first build takes minutes must not read as stalled."""
        (tmp_path / "src").mkdir()
        source = tmp_path / "src" / "app.py"
        source.write_text("def handler():\n    return 1\n", encoding="utf-8")
        store = GraphStore(tmp_path / "graph.db")
        seen: list[dict] = []

        def record_initial_health(*args, **kwargs):
            seen.append(read_watch_health(tmp_path) or {})
            return {"files_updated": 0}

        try:
            with (
                patch("watchdog.observers.Observer", return_value=FakeObserver()),
                patch("time.sleep", side_effect=KeyboardInterrupt),
                patch(
                    "code_review_graph.incremental.incremental_update",
                    side_effect=record_initial_health,
                ),
            ):
                watch(tmp_path, store)
        finally:
            store.close()

        assert seen, "incremental_update was never reached"
        assert seen[0].get("phase") == "initial-build"
        assert seen[0].get("stalled") is False

    def test_sigterm_clears_the_health_file(self, tmp_path):
        """`crg-daemon stop` sends SIGTERM; a leftover file reads as stalled."""
        import os
        import signal

        (tmp_path / "src").mkdir()
        store = GraphStore(tmp_path / "graph.db")

        def send_sigterm():
            os.kill(os.getpid(), signal.SIGTERM)

        try:
            self._watch_with(tmp_path, store, FakeObserver(), _tick_driver(send_sigterm))
        finally:
            store.close()

        assert not watch_health_path(tmp_path).exists()
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL, "handler not restored"

    def test_health_is_not_rewritten_on_every_tick(self, tmp_path):
        """Watch mode runs for days; the heartbeat must stay rate-limited."""
        (tmp_path / "src").mkdir()
        supervisor = _WatchSupervisor(
            FakeObserver(), tmp_path, [], health_path=tmp_path / "health.json"
        )
        supervisor.report_health(observer_alive=True, force=True)
        first = (tmp_path / "health.json").read_text(encoding="utf-8")

        for _ in range(30):
            supervisor.report_health(observer_alive=True, last_event_at=time.time())

        assert (tmp_path / "health.json").read_text(encoding="utf-8") == first

        supervisor.report_health(observer_alive=False, dead_threads=("Thread-3",))

        assert (tmp_path / "health.json").read_text(encoding="utf-8") != first


class TestRealObserver:
    """The one place a fake observer cannot be trusted.

    Backend semantics differ: on macOS a non-recursive watch delivers no
    directory event at all for a child, and no file event from inside it
    (``FSEventsEmitter._is_recursive_event``).  Any design that learns about
    new directories from events is silently blind there, so this drives the
    real ``Observer`` and asserts the file reaches the graph.
    """

    def _wait_for(self, predicate, timeout=20.0, interval=0.05):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return False

    def test_new_top_level_directory_is_indexed(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        # Enough ignored directories that the root is planned non-recursively;
        # a recursive root watch would hide the bug this test exists for.
        for index in range(6):
            (repo / "node_modules" / f"pkg{index}").mkdir(parents=True)
        (repo / "src" / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")

        store = GraphStore(repo / "graph.db")
        reader = GraphStore(repo / "graph.db")
        stop = threading.Event()
        failure: list[BaseException] = []

        def run_watch():
            try:
                watch(repo, store, stop_event=stop)
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assertions
                failure.append(exc)

        with patch("code_review_graph.incremental._WATCH_TICK_SECONDS", 0.05):
            thread = threading.Thread(target=run_watch, name="watch-under-test", daemon=True)
            thread.start()
            try:
                assert self._wait_for(
                    lambda: watch_health_path(repo).exists()
                ), "the watch loop never started"

                # The whole point: this directory did not exist when the
                # watches were planned.
                created = repo / "services"
                created.mkdir()
                (created / "svc.py").write_text("def service():\n    return 3\n", encoding="utf-8")

                indexed = self._wait_for(
                    lambda: any(
                        path.endswith("svc.py") for path in reader.get_all_files()
                    )
                )
            finally:
                stop.set()
                thread.join(timeout=20)
                store.close()
                reader.close()

        assert not failure, f"watch() raised: {failure[0]!r}"
        assert indexed, (
            "a top-level directory created after startup was never watched or indexed"
        )

    def test_recreated_directory_survives_and_is_reindexed(self, tmp_path):
        """`rm -rf src && mkdir src` inside one tick, on a real Observer.

        Both halves matter and only fail together: the emitter stops itself on
        the delete, so a path-keyed watch list calls it a death and exits 1,
        and nothing re-adopts the replacement, so its files never reach the
        graph — while ``crg-daemon status`` still reports ok.
        """
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        for index in range(6):
            (repo / "node_modules" / f"pkg{index}").mkdir(parents=True)
        (repo / "src" / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")

        store = GraphStore(repo / "graph.db")
        incremental_update(repo, store, changed_files=["src/app.py"])
        assert store.get_all_files(), "precondition: the graph knows the original file"
        reader = GraphStore(repo / "graph.db")
        stop = threading.Event()
        failure: list[BaseException] = []

        def run_watch():
            try:
                watch(repo, store, stop_event=stop)
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assertions
                failure.append(exc)

        with patch("code_review_graph.incremental._WATCH_TICK_SECONDS", 0.05):
            thread = threading.Thread(target=run_watch, name="watch-under-test", daemon=True)
            thread.start()
            try:
                assert self._wait_for(lambda: watch_health_path(repo).exists())

                # Delete and recreate back to back, well inside one tick.
                shutil.rmtree(repo / "src")
                (repo / "src").mkdir()
                (repo / "src" / "reborn.py").write_text(
                    "def reborn():\n    return 5\n", encoding="utf-8"
                )

                reindexed = self._wait_for(
                    lambda: any(p.endswith("reborn.py") for p in reader.get_all_files())
                )
                survived = thread.is_alive()
            finally:
                stop.set()
                thread.join(timeout=20)
                store.close()
                reader.close()

        assert not failure, f"delete-and-recreate killed the watcher: {failure[0]!r}"
        assert survived, "the watcher exited on a recreated directory"
        assert reindexed, "the recreated directory's files never reached the graph"

    def test_deleting_a_watched_directory_does_not_kill_the_watcher(self, tmp_path):
        """The emitter stops itself when its root vanishes; that is not a death."""
        repo = tmp_path / "repo"
        (repo / "lib").mkdir(parents=True)
        for index in range(6):
            (repo / "node_modules" / f"pkg{index}").mkdir(parents=True)
        (repo / "lib" / "mod.py").write_text("def mod():\n    return 1\n", encoding="utf-8")

        store = GraphStore(repo / "graph.db")
        stop = threading.Event()
        failure: list[BaseException] = []

        def run_watch():
            try:
                watch(repo, store, stop_event=stop)
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assertions
                failure.append(exc)

        with patch("code_review_graph.incremental._WATCH_TICK_SECONDS", 0.05):
            thread = threading.Thread(target=run_watch, name="watch-under-test", daemon=True)
            thread.start()
            try:
                assert self._wait_for(lambda: watch_health_path(repo).exists())
                shutil.rmtree(repo / "lib")
                time.sleep(1.0)  # several ticks
                still_running = thread.is_alive()
            finally:
                stop.set()
                thread.join(timeout=20)
                store.close()

        assert not failure, f"deleting a watched directory killed the watcher: {failure[0]!r}"
        assert still_running


# ---------------------------------------------------------------------------
# Status reporting
# ---------------------------------------------------------------------------


def _write_health(repo_root: Path, **fields) -> None:
    payload = {
        "repo": str(repo_root),
        "pid": 4242,
        "started_at": time.time(),
        "updated_at": time.time(),
        "observer_alive": True,
        "last_event_at": time.time(),
        "events_seen": 3,
        "watched_paths": 4,
        "dead_threads": [],
    }
    payload.update(fields)
    path = watch_health_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestStatusSurfacesStalls:
    def test_missing_health_reads_as_unknown(self, tmp_path):
        assert read_watch_health(tmp_path) is None
        assert watcher_status(True, None) == "unknown"

    def test_corrupt_health_file_is_ignored(self, tmp_path):
        path = watch_health_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

        assert read_watch_health(tmp_path) is None

    def test_dead_observer_reads_as_stalled(self, tmp_path):
        _write_health(tmp_path, observer_alive=False, dead_threads=["Thread-3"])

        health = read_watch_health(tmp_path)

        assert health is not None
        assert health["stalled"] is True
        assert watcher_status(True, health) == "stalled"

    def test_frozen_heartbeat_reads_as_stalled(self, tmp_path):
        _write_health(tmp_path, updated_at=time.time() - 600)

        health = read_watch_health(tmp_path)

        assert health is not None
        assert health["stalled"] is True
        assert health["age"] > 500

    def test_fresh_heartbeat_reads_as_ok(self, tmp_path):
        _write_health(tmp_path)

        health = read_watch_health(tmp_path)

        assert health is not None
        assert health["stalled"] is False
        assert watcher_status(True, health) == "ok"

    def test_daemon_status_reports_the_stall(self, tmp_path):
        from code_review_graph.daemon import WatchDaemon

        repo = tmp_path / "repo"
        repo.mkdir()
        _write_health(repo, observer_alive=False, dead_threads=["Thread-3"])
        config = DaemonConfig(
            repos=[WatchRepo(path=str(repo), alias="repo")],
            log_dir=tmp_path / "logs",
        )
        daemon = WatchDaemon(config=config, config_path=tmp_path / "watch.toml")

        with patch(
            "code_review_graph.daemon.load_state",
            return_value={"repo": {"pid": 4242, "path": str(repo)}},
        ), patch("code_review_graph.daemon._is_pid_alive", return_value=True):
            entry = daemon.status()["repos"][0]

        assert entry["alive"] is True, "the process really is still running"
        assert entry["watcher"] == "stalled"
        assert entry["observer_alive"] is False

    def test_daemon_cli_status_prints_a_stalled_watcher(self, tmp_path):
        from code_review_graph.daemon_cli import _handle_status

        repo = tmp_path / "repo"
        repo.mkdir()
        _write_health(repo, observer_alive=False, dead_threads=["Thread-3"])
        config = DaemonConfig(
            repos=[WatchRepo(path=str(repo), alias="repo")],
            log_dir=tmp_path / "logs",
        )

        with (
            patch("code_review_graph.daemon.is_daemon_running", return_value=True),
            patch("code_review_graph.daemon.load_config", return_value=config),
            patch("code_review_graph.daemon.read_pid", return_value=4242),
            patch(
                "code_review_graph.daemon.load_state",
                return_value={"repo": {"pid": 4242, "path": str(repo)}},
            ),
            patch("code_review_graph.daemon.pid_alive", return_value=True),
            patch("builtins.print") as printer,
        ):
            _handle_status(MagicMock())

        printed = "\n".join(str(call) for call in printer.call_args_list)
        assert "stalled" in printed
        assert "alive" in printed, "the process column still says alive"

    def test_restart_backs_off_instead_of_looping(self, tmp_path, caplog):
        """A watcher that keeps dying must not repay a full build every 30s."""
        from code_review_graph.daemon import WatchDaemon

        repo = tmp_path / "repo"
        repo.mkdir()
        config = DaemonConfig(
            repos=[WatchRepo(path=str(repo), alias="repo")],
            log_dir=tmp_path / "logs",
        )
        daemon = WatchDaemon(config=config, config_path=tmp_path / "watch.toml")
        daemon._current_repos = {"repo": config.repos[0]}
        dead = MagicMock()
        dead.poll.return_value = 1

        with patch.object(WatchDaemon, "_start_watcher") as restart:
            daemon._children = {"repo": dead}
            daemon._check_health()
            assert restart.call_count == 1, "the first death restarts immediately"
            assert daemon.restart_count("repo") == 1

            daemon._children = {"repo": dead}
            daemon._check_health()
            assert restart.call_count == 1, "the second death inside the window waits"
            assert daemon.restart_count("repo") == 1

            daemon._restarts["repo"]["next_attempt"] = 0.0  # window elapsed
            daemon._children = {"repo": dead}
            daemon._check_health()
            assert restart.call_count == 2
            assert daemon.restart_count("repo") == 2

    def test_restart_counter_resets_after_a_healthy_run(self, tmp_path):
        from code_review_graph.daemon import WatchDaemon

        repo = tmp_path / "repo"
        repo.mkdir()
        config = DaemonConfig(
            repos=[WatchRepo(path=str(repo), alias="repo")],
            log_dir=tmp_path / "logs",
        )
        daemon = WatchDaemon(config=config, config_path=tmp_path / "watch.toml")
        daemon._current_repos = {"repo": config.repos[0]}
        daemon._restarts["repo"] = {
            "count": 5,
            "next_attempt": 0.0,
            "started_at": time.monotonic() - 100000,
        }
        dead = MagicMock()
        dead.poll.return_value = 1

        with patch.object(WatchDaemon, "_start_watcher"):
            daemon._children = {"repo": dead}
            daemon._check_health()

        assert daemon.restart_count("repo") == 1, "a long healthy run clears the history"

    def test_reaping_a_child_clears_its_health_file(self, tmp_path):
        """A killed child cannot clear its own file; the leftover reads as stalled."""
        from code_review_graph.daemon import WatchDaemon

        repo = tmp_path / "repo"
        repo.mkdir()
        _write_health(repo)
        assert watch_health_path(repo).exists()

        proc = MagicMock()
        proc.poll.return_value = 0
        WatchDaemon._terminate_child("repo", proc, str(repo))

        assert not watch_health_path(repo).exists()

    def test_daemon_health_check_warns_about_a_stalled_watcher(self, tmp_path, caplog):
        from code_review_graph.daemon import WatchDaemon

        repo = tmp_path / "repo"
        repo.mkdir()
        _write_health(repo, observer_alive=False)
        config = DaemonConfig(
            repos=[WatchRepo(path=str(repo), alias="repo")],
            log_dir=tmp_path / "logs",
        )
        daemon = WatchDaemon(config=config, config_path=tmp_path / "watch.toml")
        child = MagicMock()
        child.poll.return_value = None
        daemon._current_repos = {"repo": config.repos[0]}
        daemon._children = {"repo": child}

        with caplog.at_level(logging.WARNING):
            with patch.object(WatchDaemon, "_start_watcher") as restart:
                daemon._check_health()

        assert "stalled" in caplog.text
        restart.assert_not_called()
