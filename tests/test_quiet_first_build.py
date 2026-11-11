"""A first build logs what the user needs, not every step it took.

From the gstack developer-experience review (item 7): a brand-new database
logged about thirty migration lines, a resolver logged a line of zeros, and
the community step said "igraph not available" without saying how to get it.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from code_review_graph import communities
from code_review_graph.event_resolver import resolve_spring_events
from code_review_graph.graph import GraphStore
from code_review_graph.migrations import LATEST_VERSION
from code_review_graph.parser import EdgeInfo, NodeInfo

MIGRATIONS_LOGGER = "code_review_graph.migrations"


def _info_lines(caplog: pytest.LogCaptureFixture, name: str) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == name and record.levelno >= logging.INFO
    ]


def test_new_database_logs_one_created_line(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger=MIGRATIONS_LOGGER)

    GraphStore(tmp_path / "graph.db").close()

    assert _info_lines(caplog, MIGRATIONS_LOGGER) == [
        f"Created graph database (schema v{LATEST_VERSION})."
    ]
    # The steps are still there for anyone who asks for DEBUG.
    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert f"Running migration v{LATEST_VERSION}" in debug


def test_upgrading_a_graph_still_logs_each_step(tmp_path: Path, caplog) -> None:
    db = tmp_path / "graph.db"
    store = GraphStore(db)
    store.upsert_node(NodeInfo(
        kind="Function", name="f", file_path="a.py",
        line_start=1, line_end=2, language="python",
    ), file_hash="h")
    store.commit()
    store.close()
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
        (str(LATEST_VERSION - 1),),
    )
    conn.commit()
    conn.close()
    caplog.set_level(logging.INFO, logger=MIGRATIONS_LOGGER)

    GraphStore(db).close()

    lines = _info_lines(caplog, MIGRATIONS_LOGGER)
    assert f"Running migration v{LATEST_VERSION}" in lines
    assert not any(line.startswith("Created graph database") for line in lines)


def test_progress_level_does_not_leak_after_a_new_database(tmp_path: Path, caplog) -> None:
    """The DEBUG drop is scoped to the one call, not left on the thread."""
    from code_review_graph.migrations import _progress_level

    GraphStore(tmp_path / "graph.db").close()

    assert _progress_level.get() == logging.INFO


def test_spring_resolver_with_nothing_found_logs_at_debug(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="code_review_graph.event_resolver")
    with GraphStore(tmp_path / "graph.db") as store:
        resolve_spring_events(store)

    records = [r for r in caplog.records if "Spring event resolver" in r.getMessage()]
    assert [r.levelno for r in records] == [logging.DEBUG]


def test_spring_resolver_that_found_events_still_logs_at_info(
    tmp_path: Path, caplog,
) -> None:
    caplog.set_level(logging.DEBUG, logger="code_review_graph.event_resolver")
    with GraphStore(tmp_path / "graph.db") as store:
        store.upsert_edge(EdgeInfo(
            kind="HANDLES", source="L.java::L.on", target="event::app.Ev",
            file_path="L.java", line=3, extra={"event_type": "app.Ev"},
        ))
        store.commit()
        resolve_spring_events(store)

    records = [r for r in caplog.records if "Spring event resolver" in r.getMessage()]
    assert [r.levelno for r in records] == [logging.INFO]


def test_missing_igraph_gives_a_tip_with_the_quoted_extra(
    tmp_path: Path, caplog, monkeypatch,
) -> None:
    monkeypatch.setattr(communities, "_load_igraph", lambda: False)
    caplog.set_level(logging.INFO, logger="code_review_graph.communities")
    with GraphStore(tmp_path / "graph.db") as store:
        communities.detect_communities(store)

    lines = _info_lines(caplog, "code_review_graph.communities")
    assert any(
        line.startswith('Tip: pip install "code-review-graph[communities]"')
        for line in lines
    ), lines
    assert not any("igraph not available" in line for line in lines)
