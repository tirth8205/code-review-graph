"""Tests for cross-file CSS linking and conflict detection."""

import tempfile
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import (
    full_build,
)
from code_review_graph.parser import CodeParser

FIXTURES = Path(__file__).parent / "fixtures"


def _build_graph_from_files(tmp_dir: Path, files: dict[str, bytes]) -> GraphStore:
    """Write files to tmp_dir, build a graph, and return the store."""
    for rel_path, content in files.items():
        p = tmp_dir / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    db_path = tmp_dir / ".code-review-graph" / "graph.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = GraphStore(str(db_path))
    full_build(tmp_dir, store)
    return store


class TestCamelToKebab:
    def test_simple(self):
        assert CodeParser._camel_to_kebab("btnPrimary") == "btn-primary"

    def test_multiple_words(self):
        assert CodeParser._camel_to_kebab("navItemActive") == "nav-item-active"

    def test_already_kebab(self):
        assert CodeParser._camel_to_kebab("btn-primary") == "btn-primary"

    def test_single_word(self):
        assert CodeParser._camel_to_kebab("btn") == "btn"


class TestCSSStylesLinking:
    def test_static_styles_edge(self):
        """TSX className should create STYLES edge to matching CSS selector."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            store = _build_graph_from_files(tmp_dir, {
                "App.tsx": b'''
function App() {
  return <div className="btn">Click</div>;
}
''',
                "styles.css": b'.btn { color: blue; }',
            })
            try:
                edges = store.get_all_edges()
                styles = [e for e in edges if e.kind == "STYLES"]
                assert len(styles) >= 1
                assert any(
                    "btn" in e.extra.get("class_name", "")
                    for e in styles
                )
            finally:
                store.close()

    def test_vue_same_file_styles_edge(self):
        """Vue template class + inline <style> should create STYLES edge."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            store = _build_graph_from_files(tmp_dir, {
                "App.vue": b'''<template>
  <div class="container">Hello</div>
</template>
<style>
.container { max-width: 1200px; }
</style>
''',
            })
            try:
                edges = store.get_all_edges()
                styles = [e for e in edges if e.kind == "STYLES"]
                assert len(styles) >= 1
                assert any(
                    e.extra.get("class_name") == "container"
                    for e in styles
                )
            finally:
                store.close()

    def test_no_styles_edge_for_missing_selector(self):
        """className with no matching CSS should NOT create STYLES edge."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            store = _build_graph_from_files(tmp_dir, {
                "App.tsx": b'''
function App() {
  return <div className="nonexistent">Hello</div>;
}
''',
            })
            try:
                edges = store.get_all_edges()
                styles = [e for e in edges if e.kind == "STYLES"]
                assert len(styles) == 0
            finally:
                store.close()


class TestCrossFileConflicts:
    def test_detects_same_selector_conflict(self):
        """Same .btn selector in two files should create POTENTIAL_CONFLICT."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            store = _build_graph_from_files(tmp_dir, {
                "base.css": FIXTURES.joinpath("conflict_a.css").read_bytes(),
                "theme.css": FIXTURES.joinpath("conflict_b.css").read_bytes(),
            })
            try:
                edges = store.get_all_edges()
                conflicts = [
                    e for e in edges if e.kind == "POTENTIAL_CONFLICT"
                ]
                assert len(conflicts) >= 1
                assert any(
                    ".btn" in e.extra.get("class_name", "")
                    for e in conflicts
                )
            finally:
                store.close()

    def test_no_conflict_unique_selectors(self):
        """Unique selectors (.card, .sidebar) should NOT conflict."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            store = _build_graph_from_files(tmp_dir, {
                "base.css": FIXTURES.joinpath("conflict_a.css").read_bytes(),
                "theme.css": FIXTURES.joinpath("conflict_b.css").read_bytes(),
            })
            try:
                edges = store.get_all_edges()
                conflicts = [
                    e for e in edges if e.kind == "POTENTIAL_CONFLICT"
                ]
                # .card only in conflict_a, .sidebar only in conflict_b
                assert not any(
                    e.extra.get("class_name") == ".card" for e in conflicts
                )
                assert not any(
                    e.extra.get("class_name") == ".sidebar" for e in conflicts
                )
            finally:
                store.close()

    def test_conflict_has_metadata(self):
        """Conflict edges should have specificity and confidence metadata."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            store = _build_graph_from_files(tmp_dir, {
                "a.css": b'.alert { color: red; }',
                "b.css": b'.alert { color: green; }',
            })
            try:
                edges = store.get_all_edges()
                conflicts = [
                    e for e in edges if e.kind == "POTENTIAL_CONFLICT"
                ]
                assert len(conflicts) >= 1
                c = conflicts[0]
                assert "conflict_confidence" in c.extra
                assert "source_specificity" in c.extra
                assert "target_specificity" in c.extra
            finally:
                store.close()
