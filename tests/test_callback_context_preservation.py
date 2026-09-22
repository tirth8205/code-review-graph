"""Preserve existing caller context while reviewing wrapped-function inference (#972)."""

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import CodeParser
from code_review_graph.tools.query import query_graph


def test_value_callback_calls_remain_owned_by_enclosing_function(tmp_path):
    source = tmp_path / "app.ts"
    source.write_text(
        "function compute(): number { return 1; }\n"
        "function evaluate(cb: () => number): number { return cb(); }\n"
        "export function setup(): number { "
        "const result = evaluate(() => compute()); return result; }\n"
    )
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()
    with GraphStore(graph_dir / "graph.db") as store:
        assert full_build(tmp_path, store)["errors"] == []

    setup = f"{source.as_posix()}::setup"
    callees = query_graph("callees_of", setup, repo_root=str(tmp_path))
    assert callees["status"] == "ok"
    assert {node["qualified_name"] for node in callees["results"]} == {
        f"{source.as_posix()}::evaluate",
        f"{source.as_posix()}::compute",
    }
    callers = query_graph("callers_of", f"{source.as_posix()}::compute", repo_root=str(tmp_path))
    assert callers["status"] == "ok"
    assert {node["qualified_name"] for node in callers["results"]} == {setup}


def test_wrapped_declaration_keeps_sibling_initializer_call(tmp_path):
    source = tmp_path / "component.tsx"
    source.write_text(
        "import { memo } from 'react';\n"
        "function nextToken(): number { return 1; }\n"
        "export function setup() {\n"
        "  const Button = memo(() => paint()), token = nextToken();\n"
        "  return [Button, token];\n"
        "}\n"
    )
    _, edges = CodeParser(tmp_path).parse_file(source)
    # This does not constrain whether Button itself is indexed as a component.
    assert any(
        edge.kind == "CALLS"
        and edge.source == f"{source.as_posix()}::setup"
        and edge.target == f"{source.as_posix()}::nextToken"
        for edge in edges
    )
