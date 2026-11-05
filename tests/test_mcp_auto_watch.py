"""MCP auto-watch must run the shared post-processing pipeline (issue #1103)."""

from __future__ import annotations

import subprocess
import threading
from unittest.mock import ANY, Mock, patch

import pytest

from code_review_graph import incremental
from code_review_graph import main as crg_main
from code_review_graph.graph import GraphStore
from code_review_graph.postprocessing import run_post_processing


@pytest.fixture(autouse=True)
def isolate_mcp_state(monkeypatch):
    """Keep main() calls from leaking their default root or changing tool exposure."""
    monkeypatch.setattr(crg_main, "_default_repo_root", None)
    monkeypatch.delenv("CRG_TOOLS", raising=False)


def test_main_auto_watch_supplies_shared_postprocessing(tmp_path, monkeypatch):
    store = Mock(spec=GraphStore)
    start_watch = Mock(return_value=Mock(spec=threading.Thread))
    monkeypatch.setattr(crg_main, "get_db_path", Mock(return_value=tmp_path / "graph.db"))
    monkeypatch.setattr(crg_main, "GraphStore", Mock(return_value=store))
    monkeypatch.setattr(crg_main, "start_watch_thread", start_watch)
    monkeypatch.setattr(crg_main.mcp, "run", Mock())

    crg_main.main(repo_root=str(tmp_path), auto_watch=True)

    start_watch.assert_called_once_with(
        tmp_path, store, daemon=True,
        on_files_updated=run_post_processing, stop_event=ANY,
    )
    store.close.assert_called_once_with()


def test_main_without_auto_watch_does_not_start_watcher(tmp_path, monkeypatch):
    open_store = Mock()
    start_watch = Mock()
    monkeypatch.setattr(crg_main, "GraphStore", open_store)
    monkeypatch.setattr(crg_main, "start_watch_thread", start_watch)
    monkeypatch.setattr(crg_main.mcp, "run", Mock())

    crg_main.main(repo_root=str(tmp_path), auto_watch=False)

    start_watch.assert_not_called()
    open_store.assert_not_called()


def test_start_watch_thread_forwards_callback_to_watch(tmp_path, monkeypatch):
    callback = Mock(return_value={})
    watch = Mock()
    monkeypatch.setattr(incremental, "watch", watch)

    with GraphStore(tmp_path / "graph.db") as store:
        thread = incremental.start_watch_thread(
            tmp_path, store, daemon=True, on_files_updated=callback,
        )
        assert thread is not None
        try:
            thread.join(timeout=5)
            assert not thread.is_alive(), "the mocked watch call did not finish"
            watch.assert_called_once_with(tmp_path, store, on_files_updated=callback)
        finally:
            thread.join(timeout=5)


def test_main_auto_watch_reconciliation_keeps_fts_consistent(tmp_path, monkeypatch):
    """Use real MCP wiring and startup reconciliation, with no native OS observer."""
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "mod.py"
    source.write_text(
        "def alphaHelper():\n    return 1\n\n"
        "def betaCaller():\n    return alphaHelper()\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "mod.py"], cwd=repo, check=True, capture_output=True)
    db_path = incremental.get_db_path(repo)

    with GraphStore(db_path) as store:
        built = incremental.full_build(repo, store)
        assert built["errors"] == []
        processed = run_post_processing(store)
        assert not processed.get("warnings"), processed
        node_ids = {row[0] for row in store._conn.execute("SELECT id FROM nodes")}
        indexed_ids = {
            row[0] for row in store._conn.execute("SELECT id FROM nodes_fts_docsize")
        }
        assert indexed_ids == node_ids
        assert store._conn.execute(
            "SELECT COUNT(*) FROM nodes_fts f JOIN nodes n ON n.id = f.rowid "
            "WHERE nodes_fts MATCH ? AND n.name = ?",
            ("alphaHelper", "alphaHelper"),
        ).fetchone()[0] == 1

    source.write_text(
        source.read_text(encoding="utf-8")
        + "\ndef gammaHelper():\n    return alphaHelper() + 2\n",
        encoding="utf-8",
    )
    start_watch = incremental.start_watch_thread

    def start_and_join(*args, **kwargs):
        # Forward only what main supplies: never inject the missing callback here.
        # Stop already set, so watch() does its startup work and leaves its loop.
        kwargs["stop_event"].set()
        thread = start_watch(*args, **kwargs)
        assert thread is not None
        thread.join(timeout=5)
        assert not thread.is_alive(), "startup reconciliation did not finish"
        return thread

    monkeypatch.setattr(crg_main, "start_watch_thread", start_and_join)
    monkeypatch.setattr(crg_main.mcp, "run", Mock())
    with patch("watchdog.observers.Observer") as observer:
        crg_main.main(repo_root=str(repo), auto_watch=True)
    observer.return_value.start.assert_called_once_with()

    with GraphStore(db_path) as store:
        assert "gammaHelper" in {node.name for node in store.get_nodes_by_file(str(source))}
        matches = {
            name: store._conn.execute(
                "SELECT COUNT(*) FROM nodes_fts f JOIN nodes n ON n.id = f.rowid "
                "WHERE nodes_fts MATCH ? AND n.name = ?",
                (name, name),
            ).fetchone()[0]
            for name in ("gammaHelper", "alphaHelper")
        }
        node_ids = {row[0] for row in store._conn.execute("SELECT id FROM nodes")}
        indexed_ids = {
            row[0] for row in store._conn.execute("SELECT id FROM nodes_fts_docsize")
        }
        assert matches == {"gammaHelper": 1, "alphaHelper": 1} and indexed_ids == node_ids, (
            f"matches={matches}; missing FTS IDs={sorted(node_ids - indexed_ids)}; "
            f"stale FTS IDs={sorted(indexed_ids - node_ids)}"
        )


def test_mcp_auto_watch_event_batches_keep_fts_consistent(tmp_path, monkeypatch):
    """MCP's callback must sync real FTS after each post-startup event batch."""
    from watchdog.events import FileCreatedEvent, FileDeletedEvent, FileModifiedEvent

    from tests.test_watch_robustness import FakeObserver

    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "mod.py"
    source.write_text(
        "def alphaHelper():\n    return 1\n\n"
        "def betaCaller():\n    return 2\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "mod.py"], cwd=repo, check=True, capture_output=True)
    db_path = incremental.get_db_path(repo)

    def assert_fts_consistent(store, present, absent=()):
        node_ids = {row[0] for row in store._conn.execute("SELECT id FROM nodes")}
        # FTS is an external-content table: its docsize rows reveal the actual
        # indexed IDs, whereas a plain SELECT from nodes_fts reads live nodes.
        indexed_ids = {
            row[0] for row in store._conn.execute("SELECT id FROM nodes_fts_docsize")
        }
        assert not node_ids - indexed_ids, f"missing FTS IDs: {node_ids - indexed_ids}"
        assert not indexed_ids - node_ids, f"stale FTS IDs: {indexed_ids - node_ids}"
        for name in (*present, *absent):
            graph_ids = {
                row[0] for row in store._conn.execute(
                    "SELECT id FROM nodes WHERE name = ?", (name,),
                )
            }
            assert len(graph_ids) == (1 if name in present else 0), name
            matched_ids = {
                row[0] for row in store._conn.execute(
                    "SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH ?", (name,),
                )
            }
            assert matched_ids == graph_ids, f"{name}: FTS={matched_ids}, graph={graph_ids}"

    with GraphStore(db_path) as store:
        assert incremental.full_build(repo, store)["errors"] == []
        # Establish a healthy baseline only; subsequent batches must repair
        # FTS through the callback supplied by main(), with no test injection.
        processed = run_post_processing(store)
        assert not processed.get("warnings"), processed
        assert_fts_consistent(store, ("alphaHelper", "betaCaller"))

    observer = FakeObserver()
    ready = threading.Event()
    start_watch = incremental.start_watch_thread

    def capture_thread(*args, **kwargs):
        # The first loop tick occurs after reconciliation, handler.start(),
        # and observer.start(). main() sets this event once MCP has returned.
        stop_event = kwargs["stop_event"]
        real_wait = stop_event.wait

        def signal_ready_then_wait(timeout=None):
            ready.set()
            return real_wait(timeout)

        stop_event.wait = signal_ready_then_wait
        thread = start_watch(*args, **kwargs)
        assert thread is not None
        return thread

    def exercise_batches(**kwargs):
        assert ready.wait(timeout=5), "watcher did not reach its post-startup loop"
        assert observer.started and observer.handler is not None
        handler = observer.handler
        assert handler.events_seen == 0
        with GraphStore(db_path) as store:
            assert_fts_consistent(store, ("alphaHelper", "betaCaller"))

            created = repo / "extra.py"
            created.write_text("def gammaHelper():\n    return 3\n", encoding="utf-8")
            # Reuse the synchronous batch boundary used by existing watcher
            # tests: process() returns after real updates and post-processing.
            handler.process([FileCreatedEvent(str(created))])
            handler.raise_if_failed()
            assert handler.events_seen == 1
            assert_fts_consistent(store, ("alphaHelper", "betaCaller", "gammaHelper"))

            source.write_text(
                "def deltaHelper():\n    return 4\n\n"
                "def betaCaller():\n    return 2\n",
                encoding="utf-8",
            )
            handler.process([FileModifiedEvent(str(source))])
            handler.raise_if_failed()
            assert handler.events_seen == 2
            assert_fts_consistent(
                store, ("deltaHelper", "betaCaller", "gammaHelper"), ("alphaHelper",),
            )

            created.unlink()
            handler.process([FileDeletedEvent(str(created))])
            handler.raise_if_failed()
            assert handler.events_seen == 3
            assert not store.get_nodes_by_file(str(created))
            assert_fts_consistent(
                store, ("deltaHelper", "betaCaller"), ("alphaHelper", "gammaHelper"),
            )

    monkeypatch.setattr(crg_main, "start_watch_thread", capture_thread)
    monkeypatch.setattr(crg_main.mcp, "run", exercise_batches)
    with patch("watchdog.observers.Observer", return_value=observer):
        crg_main.main(repo_root=str(repo), auto_watch=True)
    assert observer.stopped
