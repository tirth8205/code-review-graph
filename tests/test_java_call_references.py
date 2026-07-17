from pathlib import Path

import pytest

from code_review_graph.flows import detect_entry_points
from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo


def test_java_method_references_chained_calls_and_constructors_are_calls() -> None:
    path = Path("RouterConfig.java")
    _, edges = CodeParser().parse_bytes(
        path,
        b"""
        class Handler {
            void handle() {}
        }
        class RouterConfig {
            void routes(Handler handler) {
                route().GET("/orders", handler::handle);
                new Handler();
            }
        }
        """,
    )

    calls = [edge for edge in edges if edge.kind == "CALLS"]
    route_calls = [edge for edge in calls if edge.source.endswith("RouterConfig.routes")]

    method_reference = next(
        edge for edge in route_calls
        if edge.target == f"{path}::Handler.handle"
    )
    assert method_reference.extra["receiver"] == "handler"
    assert method_reference.extra["call_syntax"] == "method_reference"
    assert any(edge.target == "GET" for edge in route_calls)
    assert any(edge.target == f"{path}::Handler" for edge in route_calls)


@pytest.mark.parametrize(
    "decorator",
    [
        "KafkaListener(topics = \"orders\")",
        "WorkflowMethod",
        "ActivityMethod",
    ],
)
def test_java_framework_callbacks_remain_flow_entry_points(
    tmp_path: Path,
    decorator: str,
) -> None:
    callback_path = str(tmp_path / "Callback.java")
    caller_path = str(tmp_path / "Caller.java")
    callback_qn = f"{callback_path}::Callback.execute"
    with GraphStore(tmp_path / "graph.db") as store:
        store.upsert_node(NodeInfo(
            kind="Function",
            name="execute",
            file_path=callback_path,
            line_start=1,
            line_end=2,
            language="java",
            parent_name="Callback",
            extra={"decorators": [decorator]},
        ))
        store.upsert_node(NodeInfo(
            kind="Function",
            name="invoke",
            file_path=caller_path,
            line_start=1,
            line_end=2,
            language="java",
            parent_name="Caller",
        ))
        store.upsert_edge(EdgeInfo(
            kind="CALLS",
            source=f"{caller_path}::Caller.invoke",
            target=callback_qn,
            file_path=caller_path,
            line=2,
        ))
        store.commit()

        assert callback_qn in {
            node.qualified_name for node in detect_entry_points(store)
        }
