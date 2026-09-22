"""Kotlin classes implementing Java interfaces take part in DI call resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser


@pytest.mark.parametrize("framework", ["spring", "temporal"])
def test_kotlin_implementor_of_java_interface_resolves_call(tmp_path, framework):
    from code_review_graph.spring_resolver import resolve_spring_di_calls
    from code_review_graph.temporal_resolver import resolve_temporal_calls

    java_source = "@ActivityInterface interface WorkActivity { void work(); }\n"
    if framework == "spring":
        java_source += "class Client { @Autowired WorkActivity dep; void run() { dep.work(); } }\n"
        resolve = resolve_spring_di_calls
    else:
        java_source += (
            "class Client { WorkActivity dep = Workflow.newActivityStub(WorkActivity.class); "
            "void run() { dep.work(); } }\n"
        )
        resolve = resolve_temporal_calls

    parser = CodeParser()
    java_nodes, java_edges = parser.parse_bytes(Path("sample.java"), java_source.encode())
    kotlin_nodes, kotlin_edges = parser.parse_bytes(
        Path("sample.kt"),
        b"class KotlinWorkActivity : WorkActivity {\n    override fun work() {}\n}\n",
    )
    with GraphStore(tmp_path / "graph.db") as store:
        for node in [*java_nodes, *kotlin_nodes]:
            store.upsert_node(node)
        for edge in [*java_edges, *kotlin_edges]:
            store.upsert_edge(edge)
        store.commit()
        result = resolve(store)
        assert result["calls_resolved"] == 1
        calls = [
            edge.target_qualified
            for edge in store.get_edges_by_source("sample.java::Client.run")
            if edge.kind == "CALLS"
        ]
    assert calls == ["sample.kt::KotlinWorkActivity.work"]
