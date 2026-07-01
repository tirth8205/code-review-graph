"""Tests for CSS selector indexing + scoping in link_css_styles.

STYLES edges are scoped: a component links to a selector only when the
selector lives in the same file or in a stylesheet the component's file
imports (recorded on the File node as ``css_import_files``). Selector
indexing extracts every bare class token, so compound/descendant/pseudo
selectors still match a plain className.
"""

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import link_css_styles
from code_review_graph.parser import NodeInfo


def _selector(name: str, file_path: str) -> NodeInfo:
    return NodeInfo(
        kind="Class",
        name=name,
        file_path=file_path,
        line_start=1,
        line_end=1,
        language="css",
        extra={"css_kind": "selector"},
    )


def _file_importing(file_path: str, css_files: list[str]) -> NodeInfo:
    return NodeInfo(
        kind="File",
        name=file_path,
        file_path=file_path,
        line_start=1,
        line_end=1,
        language="typescript",
        extra={"css_import_files": css_files},
    )


class TestStaticScoping:
    """Static className refs link only to imported/same-file selectors (b0)."""

    def test_static_css_classes_respect_import_scoping(self, tmp_path):
        store = GraphStore(tmp_path / "test.db")
        try:
            store.upsert_node(_selector(".btn", "/app/styles.css"))
            store.upsert_node(_selector(".btn", "/vendored/vendor.css"))
            # Button imports only /app/styles.css, not the vendored sheet.
            store.upsert_node(_file_importing(
                "/app/Button.tsx", ["/app/styles.css"],
            ))
            store.upsert_node(NodeInfo(
                kind="Function",
                name="Button",
                file_path="/app/Button.tsx",
                line_start=1,
                line_end=10,
                language="typescript",
                extra={"css_classes": ["btn"]},
            ))
            store.commit()

            link_css_styles(store)

            edges = store.get_edges_by_source("/app/Button.tsx::Button")
            styles = [e for e in edges if e.kind == "STYLES"]
            assert len(styles) == 1, (
                f"Expected 1 STYLES edge (only the imported sheet), got "
                f"{len(styles)}."
            )
            assert styles[0].target_qualified == "/app/styles.css::.btn"
        finally:
            store.close()

    def test_static_unimported_selector_is_not_linked(self, tmp_path):
        """A .btn the component never imports must produce no edge."""
        store = GraphStore(tmp_path / "test.db")
        try:
            store.upsert_node(_selector(".btn", "/vendored/vendor.css"))
            store.upsert_node(_file_importing(
                "/app/Button.tsx", ["/app/styles.css"],
            ))
            store.upsert_node(NodeInfo(
                kind="Function",
                name="Button",
                file_path="/app/Button.tsx",
                line_start=1,
                line_end=10,
                language="typescript",
                extra={"css_classes": ["btn"]},
            ))
            store.commit()

            link_css_styles(store)

            edges = store.get_edges_by_source("/app/Button.tsx::Button")
            assert [e for e in edges if e.kind == "STYLES"] == []
        finally:
            store.close()


class TestVueScoping:
    """Vue template classes link only within the SFC's own <style> (b1)."""

    def test_vue_template_classes_respect_same_file_scoping(self, tmp_path):
        store = GraphStore(tmp_path / "test.db")
        try:
            store.upsert_node(_selector(".card", "/components/Card.vue"))
            store.upsert_node(_selector(".card", "/other/Other.css"))
            store.upsert_node(NodeInfo(
                kind="Function",
                name="CardComponent",
                file_path="/components/Card.vue",
                line_start=1,
                line_end=50,
                language="vue",
                extra={"vue_template_classes": ["card"]},
            ))
            store.commit()

            link_css_styles(store)

            edges = store.get_edges_by_source(
                "/components/Card.vue::CardComponent",
            )
            styles = [e for e in edges if e.kind == "STYLES"]
            assert len(styles) == 1
            assert styles[0].target_qualified == "/components/Card.vue::.card"
        finally:
            store.close()


class TestCompoundSelectorIndexing:
    """Compound/pseudo selectors are indexed by each bare class (b2/b3)."""

    def _build(self, store, selector_name):
        store.upsert_node(_selector(selector_name, "/app/styles.css"))
        store.upsert_node(_file_importing(
            "/app/Button.tsx", ["/app/styles.css"],
        ))
        store.upsert_node(NodeInfo(
            kind="Function",
            name="Button",
            file_path="/app/Button.tsx",
            line_start=1,
            line_end=10,
            language="typescript",
            extra={"css_classes": ["btn"]},
        ))
        store.commit()

    def test_compound_selector_indexed_by_each_class(self, tmp_path):
        store = GraphStore(tmp_path / "test.db")
        try:
            self._build(store, ".btn.active")  # className 'btn' must match
            link_css_styles(store)
            edges = store.get_edges_by_source("/app/Button.tsx::Button")
            assert len([e for e in edges if e.kind == "STYLES"]) >= 1
        finally:
            store.close()

    def test_pseudo_selector_indexed_by_base_class(self, tmp_path):
        store = GraphStore(tmp_path / "test.db")
        try:
            self._build(store, ".btn:hover")  # className 'btn' must match
            link_css_styles(store)
            edges = store.get_edges_by_source("/app/Button.tsx::Button")
            assert len([e for e in edges if e.kind == "STYLES"]) >= 1
        finally:
            store.close()

    def test_descendant_selector_indexed_by_each_class(self, tmp_path):
        store = GraphStore(tmp_path / "test.db")
        try:
            self._build(store, ".card .btn")  # trailing 'btn' must match
            link_css_styles(store)
            edges = store.get_edges_by_source("/app/Button.tsx::Button")
            assert len([e for e in edges if e.kind == "STYLES"]) >= 1
        finally:
            store.close()
