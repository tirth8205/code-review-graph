from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser
from code_review_graph.tools.query import query_graph

SOURCE = """
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping({"/api/", "/v2"})
class CatalogController {
    @GetMapping({"/items", "items/{id}"})
    Object items() { return null; }

    @RequestMapping(
        path = {"/search", "/lookup"},
        method = {RequestMethod.GET, RequestMethod.POST}
    )
    Object search() { return null; }

    @PostMapping
    void submit() {}

    @GetMapping("/same")
    void first() {}

    @GetMapping("/same")
    void second() {}
}
"""


def _parsed(path: Path):
    return CodeParser().parse_bytes(path, SOURCE.encode())


def test_spring_mappings_compose_class_paths_and_http_methods(tmp_path: Path) -> None:
    path = tmp_path / "CatalogController.java"
    nodes, edges = _parsed(path)

    endpoints = [node for node in nodes if node.kind == "Endpoint"]
    handles = [edge for edge in edges if edge.kind == "HANDLES"]

    assert len(endpoints) == 18
    assert len(handles) == 18
    assert len({f"{node.parent_name}.{node.name}" for node in endpoints}) == 18
    assert {
        (node.extra["http_method"], node.extra["route"])
        for node in endpoints
        if node.extra["handler"] == "items"
    } == {
        ("GET", "/api/items"),
        ("GET", "/api/items/{id}"),
        ("GET", "/v2/items"),
        ("GET", "/v2/items/{id}"),
    }
    assert {
        (node.extra["http_method"], node.extra["route"])
        for node in endpoints
        if node.extra["handler"] == "search"
    } == {
        (method, f"{prefix}/{path_part}")
        for method in ("GET", "POST")
        for prefix in ("/api", "/v2")
        for path_part in ("search", "lookup")
    }
    assert {
        (node.extra["http_method"], node.extra["route"])
        for node in endpoints
        if node.extra["handler"] == "submit"
    } == {("POST", "/api"), ("POST", "/v2")}


def test_duplicate_routes_remain_linked_to_distinct_handlers(tmp_path: Path) -> None:
    path = tmp_path / "CatalogController.java"
    nodes, edges = _parsed(path)
    duplicate_endpoints = [
        node
        for node in nodes
        if node.kind == "Endpoint" and node.extra["route"].endswith("/same")
    ]

    assert len(duplicate_endpoints) == 4
    assert {node.extra["handler"] for node in duplicate_endpoints} == {
        "first",
        "second",
    }
    targets = {
        edge.target
        for edge in edges
        if edge.kind == "HANDLES" and edge.extra["route"].endswith("/same")
    }
    assert len(targets) == 4


def test_endpoint_queries_follow_addressable_handles_edges(tmp_path: Path) -> None:
    path = tmp_path / "CatalogController.java"
    nodes, edges = _parsed(path)
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()
    with GraphStore(graph_dir / "graph.db") as store:
        store.store_file_nodes_edges(str(path), nodes, edges, "hash")

    endpoint = next(
        node
        for node in nodes
        if node.kind == "Endpoint"
        and node.extra["handler"] == "items"
        and node.extra["route"] == "/api/items"
    )
    endpoint_qn = f"{path}::CatalogController.{endpoint.name}"
    handler_qn = f"{path}::CatalogController.items"

    handlers = query_graph("handlers_of", endpoint_qn, repo_root=str(tmp_path))
    assert handlers["status"] == "ok"
    assert [result["qualified_name"] for result in handlers["results"]] == [
        handler_qn,
    ]
    assert {edge["kind"] for edge in handlers["edges"]} == {"HANDLES"}

    routes = query_graph("endpoints_for", handler_qn, repo_root=str(tmp_path))
    assert routes["status"] == "ok"
    assert len(routes["results"]) == 4
    assert {result["kind"] for result in routes["results"]} == {"Endpoint"}
    assert {edge["kind"] for edge in routes["edges"]} == {"HANDLES"}
