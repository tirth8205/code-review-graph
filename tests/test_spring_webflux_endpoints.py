from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser
from code_review_graph.tools.query import query_graph

SOURCE = b"""
import static org.springframework.web.reactive.function.server.RouterFunctions.route;
import org.springframework.web.reactive.function.server.RouterFunction;
import org.springframework.web.reactive.function.server.ServerResponse;

class Routes {
    RouterFunction<ServerResponse> routes(OrderHandler handler) {
        return route()
            .GET("/orders", handler::list)
            .POST("/orders", handler::create)
            .build();
    }
}

class OrderHandler {
    Object list(Object request) { return null; }
    Object create(Object request) { return null; }
}
"""


def _parsed(path: Path):
    return CodeParser().parse_bytes(path, SOURCE)


def test_webflux_routes_link_endpoints_to_actual_typed_handlers(tmp_path: Path) -> None:
    path = tmp_path / "Routes.java"
    nodes, edges = _parsed(path)
    endpoints = [node for node in nodes if node.kind == "Endpoint"]
    handles = [edge for edge in edges if edge.kind == "HANDLES"]

    assert {
        (node.extra["http_method"], node.extra["route"])
        for node in endpoints
    } == {("GET", "/orders"), ("POST", "/orders")}
    assert {edge.source for edge in handles} == {
        f"{path}::OrderHandler.list",
        f"{path}::OrderHandler.create",
    }
    assert {edge.target for edge in handles} == {
        f"{path}::Routes.{node.name}" for node in endpoints
    }


def test_webflux_endpoint_queries_use_addressable_nodes(tmp_path: Path) -> None:
    path = tmp_path / "Routes.java"
    nodes, edges = _parsed(path)
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()
    with GraphStore(graph_dir / "graph.db") as store:
        store.store_file_nodes_edges(str(path), nodes, edges, "hash")

    endpoint = next(
        node
        for node in nodes
        if node.kind == "Endpoint" and node.extra["http_method"] == "GET"
    )
    endpoint_qn = f"{path}::Routes.{endpoint.name}"
    handler_qn = f"{path}::OrderHandler.list"

    handlers = query_graph("handlers_of", endpoint_qn, repo_root=str(tmp_path))
    assert [result["qualified_name"] for result in handlers["results"]] == [
        handler_qn,
    ]

    endpoints = query_graph("endpoints_for", handler_qn, repo_root=str(tmp_path))
    assert [result["qualified_name"] for result in endpoints["results"]] == [
        endpoint_qn,
    ]


def test_unrelated_or_nested_get_calls_are_not_webflux_endpoints(tmp_path: Path) -> None:
    unrelated = b"class Client { void call(Api api) { api.GET(\"/orders\"); } }"
    nested = b"""
import static org.springframework.web.reactive.function.server.RouterFunctions.route;
class Routes {
    Object routes(OrderHandler handler) {
        return route().path("/api", builder ->
            builder.GET("/orders", handler::list)).build();
    }
}
"""
    parser = CodeParser()

    unrelated_nodes, _ = parser.parse_bytes(tmp_path / "Client.java", unrelated)
    nested_nodes, _ = parser.parse_bytes(tmp_path / "Nested.java", nested)

    assert not any(node.kind == "Endpoint" for node in unrelated_nodes)
    assert not any(node.kind == "Endpoint" for node in nested_nodes)
