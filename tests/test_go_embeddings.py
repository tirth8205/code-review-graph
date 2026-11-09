"""Go embedding relationships must preserve their declaring type (#834)."""

from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser
from code_review_graph.tools import query_graph


@pytest.mark.parametrize(
    ("declaration", "targets"),
    [
        ("struct { Base }", {"Base"}),
        ("struct { *Base }", {"Base"}),
        ("struct { pkg.Base }", {"pkg.Base"}),
        ("struct { *pkg.Base }", {"pkg.Base"}),
        ("struct { Box[int] }", {"Box"}),
        ("struct { *pkg.Box[string] }", {"pkg.Box"}),
        ('struct { Base `json:"base"` }', {"Base"}),
        ("struct { /* before */ Base /* after */ }", {"Base"}),
        ("interface { Base }", {"Base"}),
        ("interface { pkg.Base }", {"pkg.Base"}),
        ("interface { Box[int] }", {"Box"}),
        ("interface { pkg.Box[string] }", {"pkg.Box"}),
        ("interface { /* before */ Base /* after */ }", {"Base"}),
        ("struct { value Base; a, b pkg.Other; nested struct { Hidden } }", set()),
        ("interface { Method(Base) Other; ~int | string; Left | Right }", set()),
        ("interface { ~Base; *Pointer; []Element; func() Result }", set()),
    ],
)
def test_extracts_only_direct_embedded_type_names(declaration, targets):
    _, edges = CodeParser().parse_bytes(
        Path("sample.go"),
        f"package sample\ntype Child {declaration}\n".encode(),
    )
    inherits = [(edge.source, edge.target) for edge in edges if edge.kind == "INHERITS"]
    assert set(inherits) == {("sample.go::Child", target) for target in targets}
    assert len(inherits) == len(targets)


def test_grouped_declarations_keep_separate_embedding_owners():
    source = b"""package sample
type (
    // First embeds a struct.
    First struct { StructBase }
    // Second embeds an interface.
    Second interface { InterfaceBase }
    Plain int
)
"""
    nodes, edges = CodeParser().parse_bytes(Path("sample.go"), source)
    classes = {node.name: node for node in nodes if node.kind == "Class"}
    assert set(classes) == {"First", "Second", "Plain"}
    assert classes["First"].extra["docstring"] == "First embeds a struct."
    assert classes["Second"].extra["docstring"] == "Second embeds an interface."
    assert {(edge.source, edge.target) for edge in edges if edge.kind == "INHERITS"} == {
        ("sample.go::First", "StructBase"),
        ("sample.go::Second", "InterfaceBase"),
    }
    assert all(node.parent_name is None for node in classes.values())


def test_inheritors_query_finds_struct_and_interface_embeddings(tmp_path):
    source_path = tmp_path / "sample.go"
    source_path.write_text(
        "package sample\n"
        "// Base documents the original declaration.\n"
        "type Base interface { Method() }\n"
        "type StructChild struct { Base }\n"
        "type InterfaceChild interface { Base }\n",
        encoding="utf-8",
    )
    nodes, edges = CodeParser().parse_file(source_path)
    base = next(node for node in nodes if node.name == "Base")
    assert base.extra["docstring"] == "Base documents the original declaration."
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()
    with GraphStore(graph_dir / "graph.db") as store:
        for node in nodes:
            store.upsert_node(node)
        for edge in edges:
            store.upsert_edge(edge)
        store.commit()

    result = query_graph(pattern="inheritors_of", target="Base", repo_root=str(tmp_path))
    assert result["status"] == "ok"
    assert {item["name"] for item in result["results"]} == {"StructChild", "InterfaceChild"}


@pytest.mark.parametrize("framework", ["spring", "temporal"])
@pytest.mark.parametrize("has_java_implementation", [False, True])
def test_go_embedding_does_not_change_java_implementation_resolution(
    tmp_path,
    framework,
    has_java_implementation,
):
    from code_review_graph.spring_resolver import resolve_spring_di_calls
    from code_review_graph.temporal_resolver import resolve_temporal_calls

    java_source = "@ActivityInterface interface WorkActivity { void work(); }\n"
    if has_java_implementation:
        java_source += "class JavaWorkActivity implements WorkActivity { public void work() {} }\n"
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
    go_nodes, go_edges = parser.parse_bytes(
        Path("sample.go"),
        b"package sample\ntype WorkActivity interface { work() }\n"
        b"type GoWorkActivity struct { WorkActivity }\n",
    )
    # This is real parser output, so the test also covers the newly restored
    # Go relationship that previously never reached these Java resolvers.
    assert any(edge.kind == "INHERITS" for edge in go_edges)
    with GraphStore(tmp_path / "graph.db") as store:
        for node in [*java_nodes, *go_nodes]:
            store.upsert_node(node)
        for edge in [*java_edges, *go_edges]:
            store.upsert_edge(edge)
        store.commit()
        result = resolve(store)
        assert result["calls_resolved"] == 1
        calls = [
            edge.target_qualified
            for edge in store.get_edges_by_source("sample.java::Client.run")
            if edge.kind == "CALLS"
        ]
    expected_class = "JavaWorkActivity" if has_java_implementation else "WorkActivity"
    assert calls == [f"sample.java::{expected_class}.work"]
