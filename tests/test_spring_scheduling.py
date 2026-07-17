from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser
from code_review_graph.tools.query import query_graph

SOURCE = """
import java.util.concurrent.TimeUnit;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.scheduling.annotation.Schedules;

class Tasks {
    @Scheduled(cron = "0 0 * * * *", zone = "UTC")
    @Scheduled(fixedRate = 5, timeUnit = TimeUnit.SECONDS)
    void sync() {}

    @Schedules({
        @Scheduled(fixedDelayString = "${cleanup.delay}"),
        @Scheduled(initialDelay = 30, timeUnit = TimeUnit.SECONDS)
    })
    void cleanup() {}

    void helper() {}
}
"""


def _parsed(path: Path):
    return CodeParser().parse_bytes(path, SOURCE.encode())


def test_scheduled_annotations_create_addressable_nodes_and_edges(tmp_path: Path) -> None:
    path = tmp_path / "Tasks.java"
    nodes, edges = _parsed(path)

    schedules = [node for node in nodes if node.kind == "Scheduler"]
    triggers = [edge for edge in edges if edge.kind == "TRIGGERS"]

    assert len(schedules) == 4
    assert len({node.name for node in schedules}) == 4
    assert len(triggers) == 4
    assert {node.extra["schedule_kind"] for node in schedules} == {
        "cron",
        "fixedRate",
        "fixedDelay",
        "initialDelay",
    }
    assert {edge.source for edge in triggers} == {
        f"{path}::Tasks.{node.name}" for node in schedules
    }
    assert {edge.target for edge in triggers} == {
        f"{path}::Tasks.sync",
        f"{path}::Tasks.cleanup",
    }


def test_scheduled_metadata_preserves_repeatable_values(tmp_path: Path) -> None:
    nodes, _ = _parsed(tmp_path / "Tasks.java")
    by_kind = {
        node.extra["schedule_kind"]: node.extra
        for node in nodes
        if node.kind == "Scheduler"
    }

    assert by_kind["cron"] == {
        "annotation": "Scheduled",
        "schedule_kind": "cron",
        "cron": "0 0 * * * *",
        "zone": "UTC",
    }
    assert by_kind["fixedRate"]["fixedRate"] == "5"
    assert by_kind["fixedRate"]["timeUnit"] == "TimeUnit.SECONDS"
    assert by_kind["fixedDelay"]["fixedDelayString"] == "${cleanup.delay}"
    assert by_kind["initialDelay"]["initialDelay"] == "30"
    assert by_kind["initialDelay"]["timeUnit"] == "TimeUnit.SECONDS"


def test_schedule_queries_follow_triggers_edges(tmp_path: Path) -> None:
    path = tmp_path / "Tasks.java"
    nodes, edges = _parsed(path)
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()
    db_path = graph_dir / "graph.db"
    with GraphStore(db_path) as store:
        store.store_file_nodes_edges(str(path), nodes, edges, "hash")

    cron = next(
        node for node in nodes
        if node.kind == "Scheduler" and node.extra["schedule_kind"] == "cron"
    )
    cron_qn = f"{path}::Tasks.{cron.name}"

    triggered = query_graph("triggers_of", cron_qn, repo_root=str(tmp_path))
    assert triggered["status"] == "ok"
    assert [result["name"] for result in triggered["results"]] == ["sync"]
    assert {edge["kind"] for edge in triggered["edges"]} == {"TRIGGERS"}

    schedulers = query_graph(
        "triggered_by",
        f"{path}::Tasks.sync",
        repo_root=str(tmp_path),
    )
    assert schedulers["status"] == "ok"
    assert len(schedulers["results"]) == 2
    assert {result["kind"] for result in schedulers["results"]} == {"Scheduler"}
    assert {edge["kind"] for edge in schedulers["edges"]} == {"TRIGGERS"}
