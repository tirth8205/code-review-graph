import json
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update
from code_review_graph.parser import CodeParser
from code_review_graph.tools.query import query_graph


def _parse_java(path: Path, source: str):
    return CodeParser().parse_bytes(path, source.encode())


def test_event_edges_use_package_and_import_qualified_identity(tmp_path: Path) -> None:
    path = tmp_path / "Listeners.java"
    _, edges = _parse_java(
        path,
        """
        package alpha.listeners;
        import beta.events.ExternalEvent;
        import org.springframework.context.event.EventListener;

        class LocalEvent {}
        class Listeners {
            @EventListener
            void local(LocalEvent event) {}

            @EventListener(classes = {LocalEvent.class, ExternalEvent.class})
            void several() {}
        }
        """,
    )

    handles = [edge for edge in edges if edge.kind == "HANDLES"]
    assert {edge.target for edge in handles} == {
        "event::alpha.listeners.LocalEvent",
        "event::beta.events.ExternalEvent",
    }
    assert all(edge.extra["event_type"] in edge.target for edge in handles)


def test_publish_event_new_expression_uses_qualified_identity(tmp_path: Path) -> None:
    path = tmp_path / "Publisher.java"
    _, edges = _parse_java(
        path,
        """
        package alpha.publishers;
        import beta.events.ExternalEvent;

        class Publisher {
            void publish() {
                applicationEvents.publishEvent(new ExternalEvent());
            }
        }
        """,
    )

    publishes = [edge for edge in edges if edge.kind == "PUBLISHES"]
    assert len(publishes) == 1
    assert publishes[0].target == "event::beta.events.ExternalEvent"
    assert publishes[0].extra["event_type"] == "beta.events.ExternalEvent"


def _write_event_package(root: Path, package: str) -> tuple[Path, Path, Path]:
    directory = root / package
    directory.mkdir(parents=True)
    event = directory / "SharedEvent.java"
    publisher = directory / "Publisher.java"
    listener = directory / "Listener.java"
    event.write_text(
        f"package {package};\nclass SharedEvent {{}}\n",
        encoding="utf-8",
    )
    publisher.write_text(
        f"""package {package};
        class Publisher {{
            void publish() {{ events.publishEvent(new SharedEvent()); }}
        }}
        """,
        encoding="utf-8",
    )
    listener.write_text(
        f"""package {package};
        import org.springframework.context.event.EventListener;
        class Listener {{
            @EventListener void on(SharedEvent event) {{}}
        }}
        """,
        encoding="utf-8",
    )
    return event, publisher, listener


def _event_calls(store: GraphStore):
    rows = store._conn.execute(
        "SELECT source_qualified, target_qualified, extra FROM edges "
        "WHERE kind = 'CALLS'"
    ).fetchall()
    return [
        row for row in rows
        if json.loads(row["extra"] or "{}").get("spring_event_resolved")
    ]


def test_event_resolver_does_not_cross_link_same_named_packages(tmp_path: Path) -> None:
    _write_event_package(tmp_path, "alpha")
    _write_event_package(tmp_path, "beta")
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()

    with GraphStore(graph_dir / "graph.db") as store:
        result = full_build(tmp_path, store)
        calls = _event_calls(store)

        assert result["event_resolution"]["calls_emitted"] == 2
        assert len(calls) == 2
        assert all(
            ("/alpha/" in row["source_qualified"] and "/alpha/" in row["target_qualified"])
            or ("/beta/" in row["source_qualified"] and "/beta/" in row["target_qualified"])
            for row in calls
        )
        assert store.get_node("event::alpha.SharedEvent") is not None
        assert store.get_node("event::beta.SharedEvent") is not None


def test_incremental_listener_change_removes_stale_event_call(tmp_path: Path) -> None:
    _, _, listener = _write_event_package(tmp_path, "alpha")
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()

    with GraphStore(graph_dir / "graph.db") as store:
        first = full_build(tmp_path, store)
        assert first["event_resolution"]["calls_emitted"] == 1
        assert len(_event_calls(store)) == 1

        listener.write_text(
            """package alpha;
            import org.springframework.context.event.EventListener;
            class OtherEvent {}
            class Listener {
                @EventListener void on(OtherEvent event) {}
            }
            """,
            encoding="utf-8",
        )
        updated = incremental_update(
            tmp_path,
            store,
            changed_files=["alpha/Listener.java"],
        )

        assert updated["event_resolution"]["calls_emitted"] == 0
        assert _event_calls(store) == []


def test_event_query_patterns_return_publishers_and_listeners(tmp_path: Path) -> None:
    _write_event_package(tmp_path, "alpha")
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()
    with GraphStore(graph_dir / "graph.db") as store:
        full_build(tmp_path, store)

    publishers = query_graph(
        "publishers_of",
        "event::alpha.SharedEvent",
        repo_root=str(tmp_path),
    )
    assert publishers["status"] == "ok"
    assert [result["name"] for result in publishers["results"]] == ["publish"]
    assert {edge["kind"] for edge in publishers["edges"]} == {"PUBLISHES"}

    listeners = query_graph(
        "listeners_of",
        "event::alpha.SharedEvent",
        repo_root=str(tmp_path),
    )
    assert listeners["status"] == "ok"
    assert [result["name"] for result in listeners["results"]] == ["on"]
    assert {edge["kind"] for edge in listeners["edges"]} == {"HANDLES"}
