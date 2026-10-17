"""Watch keeps running when an explicit embedding refresh finds no index (#1070).

Watch fails closed on post-processing warnings. The one exception is the
notice that a requested refresh had no index to refresh: the graph update
succeeded, so stopping the watch over it would only stop graph updates.
"""

from unittest.mock import patch

import pytest
from watchdog.events import FileCreatedEvent

from code_review_graph.embeddings import REFRESH_SKIPPED_WARNING
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import (
    _create_watch_handler,
    _raise_watch_postprocess_warnings,
    incremental_update,
    watch,
)
from code_review_graph.postprocessing import run_post_processing


def _recording_refresh(results: list[dict]):
    """The CLI's watch callback for an explicit refresh pair, keeping each result."""

    def refresh(store: GraphStore) -> dict:
        result = run_post_processing(
            store,
            embedding_provider="local",
            embedding_model="test-model",
        )
        results.append(result)
        return result

    return refresh


def test_startup_reconciliation_keeps_the_watch_running(tmp_path):
    deleted = tmp_path / "offline.py"
    deleted.write_text("def offline():\n    pass\n")
    store = GraphStore(tmp_path / "graph.db")
    incremental_update(tmp_path, store, changed_files=["offline.py"])
    deleted.unlink()
    results: list[dict] = []
    try:
        with (
            patch("code_review_graph.embeddings.get_provider") as get_provider,
            patch("watchdog.observers.Observer") as observer,
            patch("time.sleep", side_effect=KeyboardInterrupt),
        ):
            watch(tmp_path, store, on_files_updated=_recording_refresh(results))
        get_provider.assert_not_called()
        assert [result.get("embeddings_refresh_skipped") for result in results] == [True]
        observer.return_value.start.assert_called_once()
    finally:
        store.close()


def test_watch_update_keeps_the_watch_running(tmp_path):
    source = tmp_path / "source.py"
    source.write_text("def source():\n    pass\n")
    store = GraphStore(tmp_path / "graph.db")
    results: list[dict] = []
    handler = _create_watch_handler(tmp_path, store, _recording_refresh(results))
    try:
        with patch("code_review_graph.embeddings.get_provider") as get_provider:
            handler.process([FileCreatedEvent(str(source))])
            handler.raise_if_failed()
        get_provider.assert_not_called()
        assert [result.get("embeddings_refresh_skipped") for result in results] == [True]
        assert len(store.get_all_files()) == 1
    finally:
        store.close()


def test_the_refresh_notice_alone_is_not_a_failure():
    _raise_watch_postprocess_warnings(
        {"embeddings_refresh_skipped": True, "warnings": [REFRESH_SKIPPED_WARNING]},
    )


@pytest.mark.parametrize(
    ("warnings", "failure"),
    [
        # A step failure in the same batch as the notice.
        (
            [REFRESH_SKIPPED_WARNING, "FTS index rebuild failed: OperationalError: forced"],
            "FTS index rebuild failed",
        ),
        # A refresh against an existing index that the provider refuses.
        (
            ["Embedding refresh failed: ValueError: Embedding refresh refused"],
            "Embedding refresh failed",
        ),
    ],
)
def test_any_other_warning_still_fails_the_update(warnings, failure):
    with pytest.raises(RuntimeError, match="post-processing reported warnings") as exc_info:
        _raise_watch_postprocess_warnings({"warnings": warnings})
    assert failure in str(exc_info.value)
    assert REFRESH_SKIPPED_WARNING not in str(exc_info.value)
