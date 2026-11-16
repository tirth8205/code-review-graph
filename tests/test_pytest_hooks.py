"""Pytest finds hook implementations by name; they are never dead code.

A plugin or ``conftest.py`` declares ``pytest_sessionstart``,
``pytest_runtest_logreport`` and friends with no caller in the source: the
plugin manager calls every hookimpl by its prefix. Reporting them dead sends a
reviewer to delete the plugin's behavior.
"""

from __future__ import annotations

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.refactor import find_dead_code
from code_review_graph.tools.build import build_or_update_graph

CONFTEST = '''\
import pytest


class Collector:
    def pytest_sessionstart(self, session):
        return None

    def pytest_runtest_logreport(self, report):
        return None

    def pytest_warning_recorded(self, warning_message):
        return None

    def ordinary_helper(self):
        return 1


def pytest_collection_finish(session):
    return None
'''


def test_pytest_hooks_are_live_and_an_ordinary_method_is_not(tmp_path):
    root = tmp_path.resolve()
    (root / ".code-review-graph").mkdir(parents=True, exist_ok=True)
    (root / "plugin.py").write_text(CONFTEST, encoding="utf-8")
    (root / "conftest.py").write_text(CONFTEST, encoding="utf-8")
    result = build_or_update_graph(full_rebuild=True, repo_root=str(root))
    assert result["status"] == "ok", result
    with GraphStore(get_db_path(root)) as store:
        dead = {row["name"] for row in find_dead_code(store, root=root)}
    assert "pytest_sessionstart" not in dead
    assert "pytest_runtest_logreport" not in dead
    assert "pytest_warning_recorded" not in dead
    assert "pytest_collection_finish" not in dead
    assert "ordinary_helper" in dead
