"""Tests for graph visualization export."""

import json
import tempfile
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo


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


def test_generate_html(store_with_data, tmp_path):
    from code_review_graph.visualization import generate_html

    output_path = tmp_path / "graph.html"
    generate_html(store_with_data, output_path)
    assert output_path.exists()
    content = output_path.read_text()
    assert "d3js.org" in content or "d3.v7" in content
    assert "auth.py" in content
    assert "AuthService" in content
    assert "<!DOCTYPE html>" in content
    assert "</html>" in content


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
