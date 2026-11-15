"""Numeric analysis and benchmark reports over a real SQLite graph."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from pathlib import Path

import pytest

from code_review_graph.analysis import (
    find_hub_nodes,
    find_knowledge_gaps,
    find_surprising_connections,
)
from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.token_benchmark import run_token_benchmark


@pytest.fixture
def numeric_graph(tmp_path: Path) -> Iterator[tuple[GraphStore, Path]]:
    root = tmp_path / "repo"
    root.mkdir()
    hub = root / "hub.py"
    hub.write_text("def hub():\n    return 1\n", encoding="utf-8")
    with GraphStore(root / "graph.db") as store:
        store.upsert_node(NodeInfo("Function", "hub", str(hub), 1, 2, language="python"))
        for index in range(6):
            suffix = ".py" if index % 2 == 0 else ".ts"
            source = root / f"caller_{index}{suffix}"
            name = f"caller_{index}"
            source.write_text(
                f"def {name}():\n    return hub()\n"
                if suffix == ".py"
                else f"function {name}() {{ return hub(); }}\n",
                encoding="utf-8",
            )
            store.upsert_node(
                NodeInfo(
                    "Test" if index == 0 else "Function",
                    name,
                    str(source),
                    1,
                    2 if suffix == ".py" else 1,
                    language="python" if suffix == ".py" else "typescript",
                    is_test=index == 0,
                )
            )
            store.upsert_edge(
                EdgeInfo(
                    "CALLS", f"{source}::{name}", f"{hub}::hub", str(source),
                    line=2 if suffix == ".py" else 1,
                )
            )
        store.commit()
        yield store, root


def test_numeric_reports_keep_rank_order_and_gap_categories(numeric_graph):
    store, _root = numeric_graph
    hubs = find_hub_nodes(store, top_n=20)
    assert hubs[0]["name"] == "hub"
    assert hubs[0]["total_degree"] == 6
    totals = [row["total_degree"] for row in hubs]
    assert totals == sorted(totals, reverse=True)

    gaps = find_knowledge_gaps(store)
    assert set(gaps) == {
        "isolated_nodes",
        "thin_communities",
        "untested_hotspots",
        "single_file_communities",
        "import_cycles",
    }
    assert gaps["import_cycles"] == []
    assert gaps["untested_hotspots"][0]["name"] == "hub"
    assert gaps["untested_hotspots"][0]["degree"] == 6

    surprises = find_surprising_connections(store)
    scores = [row["surprise_score"] for row in surprises]
    assert scores and scores == sorted(scores, reverse=True)
    assert all(math.isfinite(score) and score > 0 for score in scores)
    reports = {"hubs": hubs, "gaps": gaps, "surprises": surprises}
    assert json.loads(json.dumps(reports)) == reports


def test_benchmark_keeps_numeric_question_evidence(numeric_graph):
    store, root = numeric_graph
    report = run_token_benchmark(store, root, questions=["hub"])
    assert report["naive_corpus_tokens"] > 0
    assert len(report["per_question"]) == 1
    question = report["per_question"][0]
    assert question["question"] == "hub"
    assert question["graph_tokens"] > 0
    assert question["naive_tokens"] == report["naive_corpus_tokens"]
    assert math.isfinite(question["reduction_ratio"])
    assert math.isfinite(report["average_reduction_ratio"])
    assert json.loads(json.dumps(report)) == report
