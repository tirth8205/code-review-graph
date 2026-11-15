"""Adapt native multi-path streams to the watcher's existing event boundary."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Collection
from pathlib import Path

from watchdog.events import (
    DirCreatedEvent,
    DirModifiedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers.api import ObservedWatch
from watchfiles import Change, watch

logger = logging.getLogger(__name__)
Route = tuple[ObservedWatch, tuple[FileSystemEventHandler, ...]]


class _Stream(threading.Thread):
    """One native stream for paths sharing their recursive mode."""

    def __init__(self, routes: tuple[Route, ...], recursive: bool) -> None:
        super().__init__(name=f"crg-watch-{'recursive' if recursive else 'shallow'}", daemon=True)
        self.routes = routes
        self.recursive = recursive
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.failure: BaseException | None = None

    def is_alive(self) -> bool:
        # Polling liveness must retain the native stream's original failure.
        if self.failure is not None and not self.stop_event.is_set():
            raise self.failure
        return super().is_alive()

    @staticmethod
    def _event(change: Change, path: str) -> FileSystemEvent:
        if change == Change.deleted:
            return FileDeletedEvent(path)
        if change == Change.added:
            return DirCreatedEvent(path) if Path(path).is_dir() else FileCreatedEvent(path)
        return DirModifiedEvent(path) if Path(path).is_dir() else FileModifiedEvent(path)

    def run(self) -> None:
        try:
            for changes in watch(
                *(entry.path for entry, _ in self.routes),
                watch_filter=None,
                recursive=self.recursive,
                stop_event=self.stop_event,
                yield_on_timeout=True,
                rust_timeout=100,
                debounce=1,
                step=1,
                force_polling=False,
                ignore_permission_denied=False,
            ):
                self.ready.set()
                for change, path in changes:
                    candidate = Path(path)
                    event = self._event(change, path)
                    dispatched: set[FileSystemEventHandler] = set()
                    for entry, handlers in self.routes:
                        root = Path(entry.path)
                        if not (
                            candidate == root
                            or (entry.is_recursive and root in candidate.parents)
                            or (not entry.is_recursive and candidate.parent == root)
                        ):
                            continue
                        for handler in handlers:
                            if handler not in dispatched:
                                handler.dispatch(event)
                                dispatched.add(handler)
            if not self.stop_event.is_set():
                raise RuntimeError("native watch stream ended without a stop request")
        except BaseException as error:
            self.failure = error
        finally:
            self.ready.set()


class WatchfilesObserver:
    """Own at most two streams without changing the caller's watch plan."""

    def __init__(self, *, timeout: float, max_streams: int) -> None:
        if max_streams < 1:
            raise ValueError("native watch stream budget must be positive")
        self._timeout = timeout
        self._max_streams = max_streams
        self._routes: dict[ObservedWatch, list[FileSystemEventHandler]] = {}
        self._streams: tuple[_Stream, ...] = ()
        self._running = False

    @property
    def ident(self) -> int | None:
        return self._streams[0].ident if self._streams else None

    @property
    def emitters(self) -> Collection[threading.Thread]:
        return self._streams

    def schedule(
        self, event_handler: FileSystemEventHandler, path: str, *, recursive: bool = False
    ) -> ObservedWatch:
        entry = ObservedWatch(path, recursive=recursive)
        handlers = self._routes.setdefault(entry, [])
        if event_handler not in handlers:
            handlers.append(event_handler)
        if self._running:
            self._replace()
        return entry

    def unschedule(self, watch: ObservedWatch) -> None:
        del self._routes[watch]
        if self._running:
            self._replace()

    def start(self) -> None:
        if self._running or not self._routes:
            raise RuntimeError("watch observer requires unused, scheduled paths")
        self._running = True
        try:
            self._replace()
        except BaseException:
            self._running = False
            raise

    def _replace(self) -> None:
        for stream in self._streams:
            stream.is_alive()
        groups = tuple(
            _Stream(routes, recursive)
            for recursive in (False, True)
            if (routes := tuple(
                (entry, tuple(handlers))
                for entry, handlers in self._routes.items()
                if entry.is_recursive == recursive
            ))
        )
        if len(groups) > self._max_streams:
            raise RuntimeError("native watch streams exceed the declared observer budget")
        started: list[_Stream] = []
        deadline = time.monotonic() + self._timeout
        try:
            for stream in groups:
                stream.start()
                started.append(stream)
                if not stream.ready.wait(max(0, deadline - time.monotonic())):
                    raise TimeoutError("native watch stream did not become ready")
                if not stream.is_alive():
                    raise RuntimeError("native watch stream stopped during startup")
        except BaseException:
            try:
                self._retire(started, self._timeout)
            except BaseException:
                logger.exception("Native watch startup cleanup also failed")
            raise
        previous, self._streams = self._streams, groups
        # New streams cover all retained paths before old streams are stopped.
        self._retire(previous, self._timeout)

    @staticmethod
    def _retire(streams: Collection[_Stream], timeout: float) -> None:
        failures: list[BaseException] = []
        for stream in streams:
            stream.stop_event.set()
        deadline = time.monotonic() + timeout
        for stream in streams:
            stream.join(timeout=max(0, deadline - time.monotonic()))
            if stream.is_alive():
                failures.append(TimeoutError("native watch stream did not stop"))
            if stream.failure is not None:
                failures.append(stream.failure)
        if failures:
            for error in failures[1:]:
                logger.error(
                    "Native watch cleanup also failed",
                    exc_info=(type(error), error, error.__traceback__),
                )
            raise failures[0]

    def stop(self) -> None:
        self._running = False
        for stream in self._streams:
            stream.stop_event.set()

    def join(self, timeout: float | None = None) -> None:
        self._retire(self._streams, self._timeout if timeout is None else timeout)

    def is_alive(self) -> bool:
        return any(stream.is_alive() for stream in self._streams)
