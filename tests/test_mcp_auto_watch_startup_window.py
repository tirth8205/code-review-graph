"""MCP auto-watch must not go blind while its startup post-processing runs.

PR #1108 runs the shared post-processing pipeline from ``watch()`` right after
startup reconciliation, and ``watch()`` only creates and starts the filesystem
observer after that call returns. Any edit saved while post-processing runs is
therefore never observed: the reconciliation scan has already read the file and
the observer snapshot/stream starts after the edit. The file stays stale in the
graph until it is edited again or the server restarts.
"""

from __future__ import annotations

import functools
import subprocess
import threading
import time

from code_review_graph import incremental, postprocessing
from code_review_graph import main as crg_main
from code_review_graph.graph import GraphStore

# Startup post-processing (FTS sync, full flow trace, community detection) over
# a large graph. Measured at ~1.3-1.7 s for a 17k-node graph (CPython stdlib).
SLOW_POSTPROCESS_SECONDS = 3.0
EDIT_AFTER_RECONCILIATION_SECONDS = 1.0


def test_edit_saved_during_startup_postprocessing_reaches_graph(tmp_path, monkeypatch):
    import watchdog.observers
    from watchdog.observers.polling import PollingObserver

    monkeypatch.setattr(crg_main, "_default_repo_root", None)
    monkeypatch.delenv("CRG_TOOLS", raising=False)
    # Portable, deterministic observer: its snapshot is taken when it starts.
    monkeypatch.setattr(
        watchdog.observers, "Observer", lambda: PollingObserver(timeout=0.2),
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    module = repo / "mod.py"
    leaf = repo / "leaf.py"
    module.write_text("def alphaHelper():\n    return 1\n", encoding="utf-8")
    leaf.write_text("def leafHelper():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    db_path = incremental.get_db_path(repo)
    with GraphStore(db_path) as store:
        assert incremental.full_build(repo, store)["errors"] == []
        assert not postprocessing.run_post_processing(store).get("warnings")

    # Offline change (e.g. a `git pull` while the editor was closed): startup
    # reconciliation has something to update.
    module.write_text(
        "def alphaHelper():\n    return 1\n\ndef betaHelper():\n    return 2\n",
        encoding="utf-8",
    )

    real_postprocess = postprocessing.run_post_processing

    def slow_postprocess(store, **kwargs):
        time.sleep(SLOW_POSTPROCESS_SECONDS)  # a large graph
        return real_postprocess(store, **kwargs)

    monkeypatch.setattr(postprocessing, "run_post_processing", slow_postprocess)

    reconciled = threading.Event()
    real_update = incremental.incremental_update

    def tracking_update(*args, **kwargs):
        result = real_update(*args, **kwargs)
        reconciled.set()
        return result

    monkeypatch.setattr(incremental, "incremental_update", tracking_update)

    stop = threading.Event()
    monkeypatch.setattr(
        incremental, "watch", functools.partial(incremental.watch, stop_event=stop),
    )
    threads: list[threading.Thread] = []
    real_start = crg_main.start_watch_thread

    def capture(*args, **kwargs):
        thread = real_start(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(crg_main, "start_watch_thread", capture)
    outcome: dict[str, bool] = {}

    def serve(**_kwargs):
        try:
            assert reconciled.wait(30), "startup reconciliation never finished"
            time.sleep(EDIT_AFTER_RECONCILIATION_SECONDS)
            # The user (or agent) saves a file while the server is starting.
            leaf.write_text(
                "def leafHelper():\n    return 1\n\ndef lateEditHelper():\n    return 2\n",
                encoding="utf-8",
            )
            deadline = time.time() + SLOW_POSTPROCESS_SECONDS + 10
            found = False
            while time.time() < deadline and not found:
                with GraphStore(db_path) as store:
                    found = "lateEditHelper" in {
                        node.name for node in store.get_nodes_by_file(str(leaf))
                    }
                time.sleep(0.2)
            outcome["found"] = found
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=SLOW_POSTPROCESS_SECONDS + 15)

    monkeypatch.setattr(crg_main.mcp, "run", serve)
    crg_main.main(repo_root=str(repo), auto_watch=True)

    with GraphStore(db_path) as store:
        assert "betaHelper" in {n.name for n in store.get_nodes_by_file(str(module))}
    assert outcome.get("found"), (
        "an edit saved while MCP auto-watch was running its startup post-processing "
        "never reached the graph"
    )
