"""Tests for graph visualization export."""

import base64
import hashlib
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from importlib import resources

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo


class _ScriptExtractor(HTMLParser):
    """Collect external script URLs and inline script bodies from HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.sources: list[str] = []
        self.inline_scripts: list[str] = []
        self._inline_chunks: list[str] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag != "script":
            return
        source = dict(attrs).get("src")
        if source is not None:
            self.sources.append(source)
            self._inline_chunks = None
        else:
            self._inline_chunks = []

    def handle_data(self, data: str) -> None:
        if self._inline_chunks is not None:
            self._inline_chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._inline_chunks is not None:
            self.inline_scripts.append("".join(self._inline_chunks))
            self._inline_chunks = None


def _extract_scripts(content: str) -> tuple[list[str], list[str]]:
    parser = _ScriptExtractor()
    parser.feed(content)
    parser.close()
    return parser.sources, parser.inline_scripts


@pytest.fixture
def store_with_data(tmp_path):
    db_path = tmp_path / "test.db"
    store = GraphStore(db_path)
    file_node = NodeInfo(
        kind="File",
        name="auth.py",
        file_path="src/auth.py",
        line_start=1,
        line_end=50,
        language="python",
        parent_name=None,
        params=None,
        return_type=None,
        modifiers=None,
        is_test=False,
        extra={},
    )
    class_node = NodeInfo(
        kind="Class",
        name="AuthService",
        file_path="src/auth.py",
        line_start=5,
        line_end=45,
        language="python",
        parent_name=None,
        params=None,
        return_type=None,
        modifiers=None,
        is_test=False,
        extra={},
    )
    func_node = NodeInfo(
        kind="Function",
        name="login",
        file_path="src/auth.py",
        line_start=10,
        line_end=20,
        language="python",
        parent_name="AuthService",
        params="username, password",
        return_type="bool",
        modifiers=None,
        is_test=False,
        extra={},
    )
    test_file = NodeInfo(
        kind="File",
        name="test_auth.py",
        file_path="tests/test_auth.py",
        line_start=1,
        line_end=10,
        language="python",
        parent_name=None,
        params=None,
        return_type=None,
        modifiers=None,
        is_test=False,
        extra={},
    )
    test_node = NodeInfo(
        kind="Test",
        name="test_login",
        file_path="tests/test_auth.py",
        line_start=1,
        line_end=10,
        language="python",
        parent_name=None,
        params=None,
        return_type=None,
        modifiers=None,
        is_test=True,
        extra={},
    )
    store.upsert_node(file_node)
    store.upsert_node(class_node)
    store.upsert_node(func_node)
    store.upsert_node(test_file)
    store.upsert_node(test_node)
    contains_edge = EdgeInfo(
        kind="CONTAINS",
        source="src/auth.py",
        target="src/auth.py::AuthService",
        file_path="src/auth.py",
        line=5,
        extra={},
    )
    calls_edge = EdgeInfo(
        kind="CALLS",
        source="tests/test_auth.py::test_login",
        target="src/auth.py::AuthService.login",
        file_path="tests/test_auth.py",
        line=5,
        extra={},
    )
    store.upsert_edge(contains_edge)
    store.upsert_edge(calls_edge)
    store.commit()
    return store


def test_export_graph_data(store_with_data):
    from code_review_graph.visualization import export_graph_data

    data = export_graph_data(store_with_data)
    assert "nodes" in data
    assert "edges" in data
    assert "stats" in data
    assert len(data["nodes"]) == 5
    assert len(data["edges"]) == 2
    node_names = {n["name"] for n in data["nodes"]}
    assert "auth.py" in node_names
    assert "AuthService" in node_names
    assert "login" in node_names
    edge_kinds = {e["kind"] for e in data["edges"]}
    assert "CONTAINS" in edge_kinds
    assert "CALLS" in edge_kinds
    json.dumps(data)  # must be serializable


def test_export_json_writes_utf8_graph_data(store_with_data, tmp_path):
    from code_review_graph.exports import export_json

    output_path = tmp_path / "nested" / "graph.json"
    result = export_json(store_with_data, output_path)

    assert result == output_path
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert {node["name"] for node in payload["nodes"]} >= {
        "auth.py",
        "AuthService",
        "login",
    }
    assert {edge["kind"] for edge in payload["edges"]} == {
        "CALLS",
        "CONTAINS",
    }


def test_export_json_failure_preserves_existing_file(
    store_with_data, tmp_path, monkeypatch
):
    from code_review_graph import exports

    output_path = tmp_path / "graph.json"
    output_path.write_text("existing export\n", encoding="utf-8")
    monkeypatch.setattr(
        exports,
        "export_graph_data",
        lambda _store: {"not_json": {object()}},
    )

    with pytest.raises(TypeError):
        exports.export_json(store_with_data, output_path)

    assert output_path.read_text(encoding="utf-8") == "existing export\n"
    assert list(tmp_path.glob(".graph.json.*.tmp")) == []


def test_generate_html(store_with_data, tmp_path):
    from code_review_graph.visualization import generate_html

    output_path = tmp_path / "graph.html"
    generate_html(store_with_data, output_path)
    assert output_path.exists()
    content = output_path.read_text()
    script_sources, _inline_scripts = _extract_scripts(content)
    assert script_sources == [_D3_FILENAME]
    assert "auth.py" in content
    assert "AuthService" in content
    assert "<!DOCTYPE html>" in content
    assert "</html>" in content


# Pinned D3 contract for the visualization templates (issue #475): the page
# must load D3 from a same-origin vendored file so `visualize --serve` works
# on offline/filtered networks, while keeping SRI integrity verification.
_D3_FILENAME = "d3.v7.min.js"
_D3_CDN_URL = "https://d3js.org/d3.v7.min.js"
_D3_SRI_HASH = "sha384-CjloA8y00+1SDAUkjs099PVfnY2KmDC2BZnws9kh8D/lX1s46w6EPhpXdqMfjK6i"


def _sha384_sri(data: bytes) -> str:
    return "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode()


@pytest.mark.parametrize("vis_mode", ["full", "community"])
def test_generated_html_loads_d3_same_origin_with_sri(store_with_data, tmp_path, vis_mode):
    """Regression test for #475: `visualize --serve` must not depend on the
    d3js.org CDN being reachable. The generated page loads a vendored,
    same-origin D3 file (with the SRI hash intact) and only falls back to
    the CDN — still SRI-pinned with crossorigin — if the local copy fails."""
    from code_review_graph.visualization import generate_html

    output_path = tmp_path / "graph.html"
    generate_html(store_with_data, output_path, mode=vis_mode)
    content = output_path.read_text()

    script_sources, inline_scripts = _extract_scripts(content)
    # Same-origin, offline-first D3 reference — no external host required.
    assert script_sources == [_D3_FILENAME]

    # The local script tag keeps SRI integrity verification.
    local_tag = re.search(r"<script src=\"d3\.v7\.min\.js\"[^>]*>", content)
    assert local_tag is not None
    assert f'integrity="{_D3_SRI_HASH}"' in local_tag.group(0)

    # CDN fallback (only used when the local asset is missing) keeps the
    # security invariant: SRI hash AND crossorigin on the d3js.org tag.
    fallback = [s for s in inline_scripts if _D3_CDN_URL in s]
    assert len(fallback) == 1
    assert f'integrity="{_D3_SRI_HASH}"' in fallback[0]
    assert 'crossorigin="anonymous"' in fallback[0]

    # The vendored asset is written next to the HTML, i.e. inside the
    # directory `visualize --serve` exposes, so GET /d3.v7.min.js succeeds.
    asset = tmp_path / _D3_FILENAME
    assert asset.exists()
    assert _sha384_sri(asset.read_bytes()) == _D3_SRI_HASH


def test_bundled_d3_asset_is_packaged_and_pinned():
    """The pinned D3 build ships inside the Python package so generated
    visualizations work without network access (issue #475)."""
    asset = resources.files("code_review_graph") / "assets" / _D3_FILENAME
    data = asset.read_bytes()
    assert data.startswith(b"// https://d3js.org v7")
    assert _sha384_sri(data) == _D3_SRI_HASH


@pytest.mark.parametrize("vis_mode", ["full", "community"])
def test_graph_data_containing_script_sentinel_is_not_expanded(tmp_path, vis_mode):
    """Repo content must never be run through the __D3_SCRIPTS__ substitution.

    The template placeholders are substituted scripts-first, data-last: a node
    literally named __D3_SCRIPTS__ (valid in Python) would otherwise be
    rewritten into <script> markup inside the graphData script, truncating it
    and promoting the remaining repo-derived JSON to live HTML.
    """
    from code_review_graph.visualization import generate_html

    store = GraphStore(tmp_path / "test.db")
    store.upsert_node(
        NodeInfo(
            kind="File", name="evil.py", file_path="src/evil.py",
            line_start=1, line_end=10, language="python", parent_name=None,
            params=None, return_type=None, modifiers=None, is_test=False,
            extra={},
        )
    )
    store.upsert_node(
        NodeInfo(
            kind="Function", name="__D3_SCRIPTS__", file_path="src/evil.py",
            line_start=2, line_end=4, language="python", parent_name=None,
            params=None, return_type=None, modifiers=None, is_test=False,
            extra={},
        )
    )
    store.upsert_node(
        NodeInfo(
            kind="Function", name="<img src=x onerror=alert(1)>",
            file_path="src/evil.py", line_start=6, line_end=8,
            language="python", parent_name=None, params=None,
            return_type=None, modifiers=None, is_test=False, extra={},
        )
    )

    output_path = tmp_path / "graph.html"
    generate_html(store, output_path, mode=vis_mode)
    content = output_path.read_text()

    script_sources, inline_scripts = _extract_scripts(content)
    # Exactly the vendored D3 reference — no injected external script tags.
    assert script_sources == [_D3_FILENAME]
    data_scripts = [s for s in inline_scripts if "graphData" in s]
    assert data_scripts, "graphData script missing — data script was truncated"
    for script in data_scripts:
        assert "<script" not in script
    # The parser must not see repo-derived markup as real elements.
    class _TagCollector(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=False)
            self.tags: set[str] = set()

        def handle_starttag(
            self, tag: str, attrs: list[tuple[str, str | None]]
        ) -> None:
            self.tags.add(tag)

    collector = _TagCollector()
    collector.feed(content)
    assert "img" not in collector.tags


def test_script_extraction_handles_case_insensitive_html_tags():
    content = (
        '<SCRIPT SRC="https://d3js.org/d3.v7.min.js"></SCRIPT>'
        "<SCRIPT>const responsive = 1 < 2;</SCRIPT>"
    )

    script_sources, inline_scripts = _extract_scripts(content)

    assert script_sources == ["https://d3js.org/d3.v7.min.js"]
    assert inline_scripts == ["const responsive = 1 < 2;"]


def test_cpp_include_resolution(tmp_path):
    """IMPORTS_FROM edges with bare C++ include paths should resolve to File nodes
    stored under absolute paths — previously these were dropped, leaving the
    graph almost entirely disconnected for C/C++ projects."""
    from code_review_graph.visualization import export_graph_data

    db_path = tmp_path / "test.db"
    store = GraphStore(db_path)

    def _file(name, path, lang="cpp"):
        return NodeInfo(
            kind="File", name=name, file_path=path,
            line_start=1, line_end=10, language=lang,
            parent_name=None, params=None, return_type=None,
            modifiers=None, is_test=False, extra={},
        )

    store.upsert_node(_file("main.cpp",  "/abs/src/main.cpp"))
    store.upsert_node(_file("Renderer.hpp", "/abs/libs/rendering/Renderer.hpp"))
    store.upsert_node(_file("Utils.hpp",    "/abs/libs/utils/Utils.hpp"))

    # Parser emits bare include paths as targets — exactly what Tree-sitter sees
    store.upsert_edge(EdgeInfo(
        kind="IMPORTS_FROM",
        source="/abs/src/main.cpp",
        target="rendering/Renderer.hpp",   # relative, one directory level
        file_path="/abs/src/main.cpp", line=1, extra={},
    ))
    store.upsert_edge(EdgeInfo(
        kind="IMPORTS_FROM",
        source="/abs/src/main.cpp",
        target="Utils.hpp",                # bare filename only
        file_path="/abs/src/main.cpp", line=2, extra={},
    ))
    store.commit()

    data = export_graph_data(store)
    resolved_targets = {e["target"] for e in data["edges"] if e["kind"] == "IMPORTS_FROM"}

    assert "/abs/libs/rendering/Renderer.hpp" in resolved_targets, (
        "bare relative include 'rendering/Renderer.hpp' was not resolved to its absolute path"
    )
    assert "/abs/libs/utils/Utils.hpp" in resolved_targets, (
        "bare filename include 'Utils.hpp' was not resolved to its absolute path"
    )


def test_generate_html_overwrites(store_with_data, tmp_path):
    from code_review_graph.visualization import generate_html

    output_path = tmp_path / "graph.html"
    output_path.write_text("old content")
    generate_html(store_with_data, output_path)
    content = output_path.read_text()
    assert "old content" not in content
    assert "<!DOCTYPE html>" in content


def test_export_includes_flows(store_with_data):
    """Export data should include a 'flows' key (list, possibly empty)."""
    from code_review_graph.visualization import export_graph_data

    data = export_graph_data(store_with_data)
    assert "flows" in data
    assert isinstance(data["flows"], list)


def test_export_includes_communities(store_with_data):
    """Export data should include a 'communities' key (list, possibly empty)."""
    from code_review_graph.visualization import export_graph_data

    data = export_graph_data(store_with_data)
    assert "communities" in data
    assert isinstance(data["communities"], list)


def test_generate_html_includes_all_edge_types(store_with_data, tmp_path):
    """Generated HTML should define colors and legend entries for all 7 edge types."""
    from code_review_graph.visualization import generate_html

    output_path = tmp_path / "graph.html"
    generate_html(store_with_data, output_path)
    content = output_path.read_text()
    for edge_kind in ["CALLS", "IMPORTS_FROM", "INHERITS", "CONTAINS",
                       "IMPLEMENTS", "TESTED_BY", "DEPENDS_ON"]:
        assert edge_kind in content, f"Edge type {edge_kind} missing from HTML"


def test_generate_html_includes_interactive_features(store_with_data, tmp_path):
    """Generated HTML should include new interactive features."""
    from code_review_graph.visualization import generate_html

    output_path = tmp_path / "graph.html"
    generate_html(store_with_data, output_path)
    content = output_path.read_text()
    # Detail panel
    assert "detail-panel" in content
    # Community coloring button
    assert "btn-community" in content
    # Flow dropdown
    assert "flow-select" in content
    # Filter panel
    assert "filter-panel" in content
    # Search results dropdown
    assert "search-results" in content
    # Accessibility: skip link
    assert "skip-link" in content
    # Accessibility: live region
    assert 'aria-live="polite"' in content
    # Node shapes mapping
    assert "KIND_SHAPE" in content


def test_generate_html_includes_node_shapes(store_with_data, tmp_path):
    """Generated HTML should use d3.symbol() for distinct node shapes."""
    from code_review_graph.visualization import generate_html

    output_path = tmp_path / "graph.html"
    generate_html(store_with_data, output_path)
    content = output_path.read_text()
    assert "d3.symbol()" in content or "symbolCircle" in content
    assert "symbolSquare" in content
    assert "symbolTriangle" in content
    assert "symbolDiamond" in content
    assert "symbolCross" in content


def test_generate_html_includes_help_overlay(store_with_data, tmp_path):
    """Generated HTML should include a help overlay for onboarding."""
    from code_review_graph.visualization import generate_html

    output_path = tmp_path / "graph.html"
    generate_html(store_with_data, output_path)
    content = output_path.read_text()
    assert "help-overlay" in content
    assert "btn-help" in content
    assert "Click a file" in content


def test_generate_html_includes_aria_attributes(store_with_data, tmp_path):
    """Generated HTML should include key ARIA attributes for accessibility."""
    from code_review_graph.visualization import generate_html

    output_path = tmp_path / "graph.html"
    generate_html(store_with_data, output_path)
    content = output_path.read_text()
    assert 'role="tooltip"' in content
    assert 'role="dialog"' in content
    assert 'role="listbox"' in content
    assert 'aria-pressed="false"' in content  # community button
    assert 'aria-modal="false"' in content  # detail panel


def test_generate_html_includes_loading_and_empty_state(store_with_data, tmp_path):
    """Generated HTML should include loading overlay and empty state markup."""
    from code_review_graph.visualization import generate_html

    output_path = tmp_path / "graph.html"
    generate_html(store_with_data, output_path)
    content = output_path.read_text()
    assert "loading-overlay" in content
    assert "empty-state" in content
    assert "No nodes to display" in content


def test_generate_html_uses_id_selector_for_svg(store_with_data, tmp_path):
    """Regression test for #523: d3.select("svg") selects the legend icon, not the canvas.

    The legend <nav> contains inline <svg> icons that appear before #graph-svg
    in document order. d3.select("svg") returns the first match — a 16px legend
    icon — causing the entire force graph to render inside it. The fix targets
    #graph-svg by id.
    """
    from code_review_graph.visualization import generate_html

    output_path = tmp_path / "graph.html"
    generate_html(store_with_data, output_path)
    content = output_path.read_text()
    assert 'd3.select("#graph-svg")' in content, (
        "HTML should use d3.select('#graph-svg') to target the main canvas, "
        "not d3.select('svg') which selects the first inline legend icon"
    )
    assert 'd3.select("svg")' not in content, (
        "No bare d3.select('svg') should remain — it selects legend icons"
    )


def test_community_mode_uses_id_selector_for_svg(large_store, tmp_path):
    """Regression test for #523: community/aggregated template must also use #graph-svg."""
    from code_review_graph.visualization import generate_html

    output_path = tmp_path / "community.html"
    generate_html(large_store, output_path, mode="community")
    content = output_path.read_text()
    assert 'd3.select("#graph-svg")' in content
    assert 'd3.select("svg")' not in content
    assert 'id="graph-svg"' in content, (
        "Aggregated template's <svg> must have id='graph-svg' for the selector to work"
    )


def _assert_responsive_graph_script(content):
    """Check the generated graph script remains responsive and valid JavaScript."""
    assert 'var svgEl = document.getElementById("graph-svg");' in content
    assert "function getW()" in content
    assert "function getH()" in content
    assert "function fitGraph(retries)" in content
    assert "if (retries === undefined) retries = 10;" in content
    assert "requestAnimationFrame(function() { fitGraph(retries - 1); });" in content
    assert 'window.addEventListener("resize", function() {' in content
    assert r'window.addEventListener(\"resize\"' not in content

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for generated JavaScript syntax validation")
    _script_sources, inline_scripts = _extract_scripts(content)
    assert inline_scripts
    for script in inline_scripts:
        if not script.strip():
            continue
        result = subprocess.run(
            [node, "--check"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_full_mode_retries_layout_and_tracks_viewport(store_with_data, tmp_path):
    """Full mode must recover if layout is unavailable before the first paint."""
    from code_review_graph.visualization import generate_html

    output_path = tmp_path / "graph.html"
    generate_html(store_with_data, output_path, mode="full")
    _assert_responsive_graph_script(output_path.read_text())


def test_community_mode_retries_layout_and_tracks_viewport(large_store, tmp_path):
    """Aggregated mode must use the same bounded layout recovery path."""
    from code_review_graph.visualization import generate_html

    output_path = tmp_path / "community.html"
    generate_html(large_store, output_path, mode="community")
    _assert_responsive_graph_script(output_path.read_text())


def test_generate_html_includes_focus_visible(store_with_data, tmp_path):
    """Generated HTML should include :focus-visible styles."""
    from code_review_graph.visualization import generate_html

    output_path = tmp_path / "graph.html"
    generate_html(store_with_data, output_path)
    content = output_path.read_text()
    assert ":focus-visible" in content


# ---------------------------------------------------------------------------
# Phase 9: Visualization Aggregation
# ---------------------------------------------------------------------------


@pytest.fixture
def large_store(tmp_path):
    """Store with enough nodes/communities to test aggregation."""
    db_path = tmp_path / "large.db"
    store = GraphStore(db_path)

    # Create nodes across multiple files (simulates a larger codebase)
    files = [f"src/mod{i}.py" for i in range(5)]
    for fp in files:
        file_node = NodeInfo(
            kind="File", name=fp.split("/")[-1], file_path=fp,
            line_start=1, line_end=100, language="python",
            parent_name=None, params=None, return_type=None,
            modifiers=None, is_test=False, extra={},
        )
        store.upsert_node(file_node)
        # Add some functions per file
        for j in range(3):
            func_node = NodeInfo(
                kind="Function", name=f"func_{j}",
                file_path=fp, line_start=10 + j * 10, line_end=20 + j * 10,
                language="python", parent_name=None,
                params="x", return_type="int",
                modifiers=None, is_test=False, extra={},
            )
            store.upsert_node(func_node)
            # CONTAINS edge from file to function
            store.upsert_edge(EdgeInfo(
                kind="CONTAINS", source=fp,
                target=f"{fp}::func_{j}",
                file_path=fp, line=10 + j * 10, extra={},
            ))

    # Add some cross-file CALLS edges
    store.upsert_edge(EdgeInfo(
        kind="CALLS",
        source="src/mod0.py::func_0",
        target="src/mod1.py::func_1",
        file_path="src/mod0.py", line=15, extra={},
    ))
    store.upsert_edge(EdgeInfo(
        kind="CALLS",
        source="src/mod2.py::func_0",
        target="src/mod3.py::func_2",
        file_path="src/mod2.py", line=12, extra={},
    ))
    store.upsert_edge(EdgeInfo(
        kind="CALLS",
        source="src/mod1.py::func_2",
        target="src/mod4.py::func_0",
        file_path="src/mod1.py", line=35, extra={},
    ))

    # Set community_id on nodes (simulate community detection)
    store._conn.execute(
        "UPDATE nodes SET community_id = 0 WHERE file_path IN ('src/mod0.py', 'src/mod1.py')"
    )
    store._conn.execute(
        "UPDATE nodes SET community_id = 1 WHERE file_path IN ('src/mod2.py', 'src/mod3.py')"
    )
    store._conn.execute(
        "UPDATE nodes SET community_id = 2 WHERE file_path = 'src/mod4.py'"
    )

    # Create communities table and insert communities
    store._conn.execute("""
        CREATE TABLE IF NOT EXISTS communities (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            level INTEGER DEFAULT 0,
            cohesion REAL DEFAULT 0.0,
            size INTEGER DEFAULT 0,
            dominant_language TEXT DEFAULT '',
            description TEXT DEFAULT ''
        )
    """)
    store._conn.execute("""
        CREATE TABLE IF NOT EXISTS community_members (
            community_id INTEGER, node_id INTEGER,
            FOREIGN KEY (community_id) REFERENCES communities(id)
        )
    """)
    store._conn.execute(
        "INSERT INTO communities (id, name, level, cohesion, size, dominant_language, description) "
        "VALUES (0, 'Core Module', 0, 0.8, 8, 'python', 'Core functionality')"
    )
    store._conn.execute(
        "INSERT INTO communities (id, name, level, cohesion, size, dominant_language, description) "
        "VALUES (1, 'Data Module', 0, 0.7, 8, 'python', 'Data processing')"
    )
    store._conn.execute(
        "INSERT INTO communities (id, name, level, cohesion, size, dominant_language, description) "
        "VALUES (2, 'Utils', 0, 0.5, 4, 'python', 'Utility functions')"
    )
    # Insert community_members so get_communities works
    for row in store._conn.execute(
        "SELECT id, qualified_name, community_id FROM nodes WHERE community_id IS NOT NULL"
    ).fetchall():
        store._conn.execute(
            "INSERT INTO community_members (community_id, node_id) VALUES (?, ?)",
            (row["community_id"], row["id"]),
        )

    store.commit()
    return store


def test_community_mode_fewer_nodes(large_store, tmp_path):
    """Community mode should produce fewer nodes than full mode."""
    from code_review_graph.visualization import (
        _aggregate_community,
        export_graph_data,
    )

    data = export_graph_data(large_store)
    full_node_count = len(data["nodes"])

    agg = _aggregate_community(data)
    community_node_count = len(agg["nodes"])

    assert community_node_count < full_node_count, (
        f"Community mode ({community_node_count} nodes) should have fewer nodes "
        f"than full mode ({full_node_count} nodes)"
    )
    # All aggregated nodes should be of kind "Community"
    for n in agg["nodes"]:
        assert n["kind"] == "Community"
    # Edges should be CROSS_COMMUNITY type
    for e in agg["edges"]:
        assert e["kind"] == "CROSS_COMMUNITY"
    # Should have community_details for drill-down
    assert "community_details" in agg
    assert len(agg["community_details"]) > 0


def test_file_mode_aggregation(large_store, tmp_path):
    """File mode should produce one node per file."""
    from code_review_graph.visualization import (
        _aggregate_file,
        export_graph_data,
    )

    data = export_graph_data(large_store)
    full_node_count = len(data["nodes"])

    agg = _aggregate_file(data)
    file_node_count = len(agg["nodes"])

    assert file_node_count < full_node_count, (
        f"File mode ({file_node_count} nodes) should have fewer nodes "
        f"than full mode ({full_node_count} nodes)"
    )
    # All nodes should be of kind "File"
    for n in agg["nodes"]:
        assert n["kind"] == "File"
    # Edges should be DEPENDS_ON type
    for e in agg["edges"]:
        assert e["kind"] == "DEPENDS_ON"
    # Mode should be set
    assert agg["mode"] == "file"


def test_auto_mode_switches_at_threshold(large_store, tmp_path):
    """Auto mode should switch to community when nodes exceed threshold."""
    from code_review_graph.visualization import generate_html

    output_path = tmp_path / "auto_low.html"
    # Threshold higher than node count -> should use full template
    generate_html(large_store, output_path, mode="auto", max_full_nodes=100000)
    content = output_path.read_text()
    # Full template has btn-community and flow-select
    assert "btn-community" in content
    assert "flow-select" in content

    output_path2 = tmp_path / "auto_high.html"
    # Threshold of 1 -> should switch to community mode
    generate_html(large_store, output_path2, mode="auto", max_full_nodes=1)
    content2 = output_path2.read_text()
    # Aggregated template has btn-back and community_details
    assert "btn-back" in content2
    assert "community_details" in content2


def test_auto_mode_switches_on_edge_count(large_store, tmp_path):
    """Auto mode must switch to an aggregated view when edges exceed the cap.

    Regression for issue #609: node count under the limit but edge count
    over it must not fall through to the full force-layout template.
    """
    from code_review_graph.visualization import generate_html

    # Under both limits -> full template
    output_full = tmp_path / "auto_under_both.html"
    generate_html(
        large_store, output_full, mode="auto",
        max_full_nodes=100000, max_full_edges=100000,
    )
    content_full = output_full.read_text()
    assert "btn-community" in content_full
    assert "flow-select" in content_full

    # Under the node limit but over the edge limit -> aggregated template
    output_agg = tmp_path / "auto_over_edges.html"
    generate_html(
        large_store, output_agg, mode="auto",
        max_full_nodes=100000, max_full_edges=1,
    )
    content_agg = output_agg.read_text()
    assert "btn-back" in content_agg
    assert "community_details" in content_agg


def test_auto_mode_decision_at_issue_609_boundary():
    """The reported 2792-node/17488-edge graph must pick an aggregated view.

    Regression for issue #609 using the exact reported boundary against the
    shipped defaults, without building a 17k-edge store.
    """
    from code_review_graph.visualization import (
        DEFAULT_MAX_FULL_EDGES,
        DEFAULT_MAX_FULL_NODES,
        _resolve_auto_mode,
    )

    # Shipped defaults: node cap unchanged, edge cap derived from it
    assert DEFAULT_MAX_FULL_NODES == 3000
    assert DEFAULT_MAX_FULL_EDGES == 3 * DEFAULT_MAX_FULL_NODES

    # A graph under both caps stays in full mode
    assert _resolve_auto_mode(
        node_count=2792, edge_count=8000,
        max_full_nodes=DEFAULT_MAX_FULL_NODES,
        max_full_edges=DEFAULT_MAX_FULL_EDGES,
        has_communities=True,
    ) == "full"

    # The exact graph from issue #609: 2792 nodes (under), 17488 edges (over)
    assert _resolve_auto_mode(
        node_count=2792, edge_count=17488,
        max_full_nodes=DEFAULT_MAX_FULL_NODES,
        max_full_edges=DEFAULT_MAX_FULL_EDGES,
        has_communities=True,
    ) == "community"

    # Node count over the cap still switches (pre-existing behavior)
    assert _resolve_auto_mode(
        node_count=3001, edge_count=100,
        max_full_nodes=DEFAULT_MAX_FULL_NODES,
        max_full_edges=DEFAULT_MAX_FULL_EDGES,
        has_communities=True,
    ) == "community"

    # Without community data the aggregated view falls back to file mode
    assert _resolve_auto_mode(
        node_count=2792, edge_count=17488,
        max_full_nodes=DEFAULT_MAX_FULL_NODES,
        max_full_edges=DEFAULT_MAX_FULL_EDGES,
        has_communities=False,
    ) == "file"


def test_generate_html_defaults_match_constants():
    """generate_html defaults must stay wired to the documented constants."""
    import inspect

    from code_review_graph.visualization import (
        DEFAULT_MAX_FULL_EDGES,
        DEFAULT_MAX_FULL_NODES,
        generate_html,
    )

    sig = inspect.signature(generate_html)
    assert sig.parameters["max_full_nodes"].default == DEFAULT_MAX_FULL_NODES
    assert sig.parameters["max_full_edges"].default == DEFAULT_MAX_FULL_EDGES


def test_auto_mode_falls_back_to_file_without_communities(
    store_with_data, tmp_path
):
    """Auto-switch without community data must aggregate by file, not lump
    everything into a single 'Uncategorized' community super-node."""
    from code_review_graph.visualization import generate_html

    output_path = tmp_path / "auto_no_communities.html"
    generate_html(
        store_with_data, output_path, mode="auto",
        max_full_nodes=1, max_full_edges=100000,
    )
    content = output_path.read_text()
    # Aggregated template, file mode data
    assert "btn-back" in content
    assert '"mode": "file"' in content
    assert '"mode": "community"' not in content


def test_community_mode_html_generation(large_store, tmp_path):
    """Community mode generates valid HTML with aggregated data."""
    from code_review_graph.visualization import generate_html

    output_path = tmp_path / "community.html"
    generate_html(large_store, output_path, mode="community")
    content = output_path.read_text()
    assert "<!DOCTYPE html>" in content
    assert "</html>" in content
    assert "btn-back" in content
    assert "community_details" in content
    assert "drillIntoCommunity" in content


def test_file_mode_html_generation(large_store, tmp_path):
    """File mode generates valid HTML with file-level data."""
    from code_review_graph.visualization import generate_html

    output_path = tmp_path / "file.html"
    generate_html(large_store, output_path, mode="file")
    content = output_path.read_text()
    assert "<!DOCTYPE html>" in content
    assert "</html>" in content
    assert "DEPENDS_ON" in content


def test_full_mode_backward_compatible(store_with_data, tmp_path):
    """Full mode should produce identical output to the original 2-arg call."""
    from code_review_graph.visualization import generate_html

    # Original 2-arg call (backward compat)
    output1 = tmp_path / "compat.html"
    generate_html(store_with_data, output1)
    content1 = output1.read_text()
    assert "btn-community" in content1
    assert "flow-select" in content1

    # Explicit full mode
    output2 = tmp_path / "full.html"
    generate_html(store_with_data, output2, mode="full")
    content2 = output2.read_text()
    assert "btn-community" in content2
    assert "flow-select" in content2


def test_community_detail_data_complete(large_store):
    """Each community's detail data should contain its member nodes."""
    from code_review_graph.visualization import (
        _aggregate_community,
        export_graph_data,
    )

    data = export_graph_data(large_store)
    agg = _aggregate_community(data)

    for cid_str, detail in agg["community_details"].items():
        assert "nodes" in detail
        assert "edges" in detail
        # Detail nodes should exist
        assert isinstance(detail["nodes"], list)
        assert isinstance(detail["edges"], list)

    # All original nodes should appear in exactly one community detail
    all_detail_qns = set()
    for detail in agg["community_details"].values():
        for n in detail["nodes"]:
            all_detail_qns.add(n["qualified_name"])
    original_qns = {n["qualified_name"] for n in data["nodes"]}
    assert original_qns == all_detail_qns, (
        "All original nodes should be accounted for in community details"
    )
