"""A Python closure passed as a value is used code, not dead code.

`find_dead_code` only looked at call-shaped edges, so a function handed to a
library (`plan.map(published)`, `publisher(..., verify)`) had no incoming edge
and was reported dead. These tests drive the real build pipeline and assert the
reviewer-facing verdict.
"""

from __future__ import annotations

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.refactor import find_dead_code
from code_review_graph.tools.build import build_or_update_graph

SOURCE = '''\
class Plan:
    def map(self, callback):
        return callback(())


class Publisher:
    def publish(self, plan):
        def published(paths):
            return True

        return plan.map(published)

    def publish_with(self, plan):
        def verify(changes):
            return bool(changes)

        return plan.map(callback=verify)

    def admit(self):
        return plan.map(callback=self._admit)

    def _admit(self, item):
        return True

    def choose(self, gates):
        def check(item):
            return True

        return plan.map(callback=check if gates is not None else None)

    def nested(self, gates):
        def validate(item):
            return True

        return plan.map(options=Options(validator=validate))


class Options:
    def __init__(self, validator=None):
        self.validator = validator
'''


def _build(root: Path, files: dict[str, str]) -> GraphStore:
    root = root.resolve()
    (root / ".code-review-graph").mkdir(parents=True, exist_ok=True)
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    result = build_or_update_graph(full_rebuild=True, repo_root=str(root))
    assert result["status"] == "ok", result
    return GraphStore(get_db_path(root))


def _dead_names(store: GraphStore, root: Path) -> set[str]:
    return {row["name"] for row in find_dead_code(store, root=root)}


def test_closure_passed_positionally_is_not_dead(tmp_path):
    store = _build(tmp_path, {"app.py": SOURCE})
    try:
        assert "published" not in _dead_names(store, tmp_path)
    finally:
        store.close()


def test_closure_passed_as_keyword_is_not_dead(tmp_path):
    store = _build(tmp_path, {"app.py": SOURCE})
    try:
        assert "verify" not in _dead_names(store, tmp_path)
    finally:
        store.close()


def test_method_passed_as_keyword_is_not_dead(tmp_path):
    store = _build(tmp_path, {"app.py": SOURCE})
    try:
        assert "_admit" not in _dead_names(store, tmp_path)
    finally:
        store.close()


def test_closure_in_a_conditional_argument_is_not_dead(tmp_path):
    store = _build(tmp_path, {"app.py": SOURCE})
    try:
        assert "check" not in _dead_names(store, tmp_path)
    finally:
        store.close()


def test_closure_passed_to_a_nested_call_is_not_dead(tmp_path):
    store = _build(tmp_path, {"app.py": SOURCE})
    try:
        assert "validate" not in _dead_names(store, tmp_path)
    finally:
        store.close()
