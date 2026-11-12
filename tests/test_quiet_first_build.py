"""A first build logs what the user needs, not every step it took.

From the developer-experience review (item 7): a brand-new database
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
from code_review_graph.python_resolver import resolve_python_imports

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


def _python_file(store: GraphStore, path: str) -> None:
    store.upsert_node(NodeInfo(
        kind="File", name=path, file_path=path, line_start=1, line_end=1, language="python",
    ), file_hash=path)


def _python_resolver_levels(caplog: pytest.LogCaptureFixture) -> list[int]:
    return [
        r.levelno for r in caplog.records if r.getMessage().startswith("Python import resolution")
    ]


def test_python_resolver_with_nothing_to_resolve_logs_at_debug(tmp_path: Path, caplog) -> None:
    """Every build ran it, and every build logged its line of zeros at INFO."""
    caplog.set_level(logging.DEBUG, logger="code_review_graph.python_resolver")
    with GraphStore(tmp_path / "graph.db") as store:
        _python_file(store, "app/auth.py")
        store.commit()
        assert resolve_python_imports(store)["imports_updated"] == 0

    assert _python_resolver_levels(caplog) == [logging.DEBUG]


def test_python_resolver_that_resolved_imports_still_logs_at_info(
    tmp_path: Path, caplog,
) -> None:
    caplog.set_level(logging.DEBUG, logger="code_review_graph.python_resolver")
    with GraphStore(tmp_path / "graph.db") as store:
        _python_file(store, "app/auth.py")
        _python_file(store, "app/views.py")
        store.upsert_edge(EdgeInfo(
            kind="IMPORTS_FROM", source="app/views.py", target="app.auth",
            file_path="app/views.py", line=1,
        ))
        store.commit()
        assert resolve_python_imports(store)["imports_resolved"] == 1

    assert _python_resolver_levels(caplog) == [logging.INFO]


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


def _graph_at(db: Path, version: int) -> None:
    """A graph with one node, stamped as an older schema version."""
    store = GraphStore(db)
    store.upsert_node(NodeInfo(
        kind="Function", name="f", file_path="a.py",
        line_start=1, line_end=2, language="python",
    ), file_hash="h")
    store.commit()
    store.close()
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE metadata SET value = ? WHERE key = 'schema_version'", (str(version),),
    )
    conn.commit()
    conn.close()


def test_an_upgrade_across_v15_says_to_build_once(tmp_path: Path, caplog) -> None:
    """``update`` after the upgrade said "0 files updated" and left new file
    kinds out until a full build (release-docs review, item 7)."""
    db = tmp_path / "graph.db"
    _graph_at(db, 14)
    caplog.set_level(logging.INFO, logger=MIGRATIONS_LOGGER)

    GraphStore(db).close()

    hints = [
        line for line in _info_lines(caplog, MIGRATIONS_LOGGER)
        if "Run code-review-graph build once" in line
    ]
    assert hints == [
        f"Graph upgraded to schema v{LATEST_VERSION}. Run code-review-graph build once "
        "(and code-review-graph embed if you use embeddings): an update parses only "
        "changed files, so files this release can newly read stay out until then."
    ]


def test_a_new_or_current_graph_gets_no_build_hint(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.INFO, logger=MIGRATIONS_LOGGER)
    db = tmp_path / "graph.db"
    GraphStore(db).close()
    GraphStore(db).close()

    assert not any(
        "build once" in line for line in _info_lines(caplog, MIGRATIONS_LOGGER)
    )
