"""Corpus gate mechanics: precision, recall, and refusal to pass vacuously."""

from __future__ import annotations

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import NodeInfo
from code_review_graph.refactor import find_dead_code
from tests.dead_code_corpus import (
    DEAD,
    LIVE,
    Entry,
    evaluate,
    gate,
    load_corpus,
    record,
)


def _corpus(*entries: dict) -> dict:
    return {"version": 1, "repos": {"demo": {}}, "entries": list(entries)}


def _entry(name: str, verdict: str, **extra: str) -> dict:
    return {
        "repo": "demo",
        "name": name,
        "kind": "Function",
        "verdict": verdict,
        "reason_code": "value_reference",
        "provenance": "test",
        **extra,
    }


def test_live_symbol_that_is_reported_is_a_false_positive():
    corpus = _corpus(_entry("helper", LIVE, path="app.py"))
    report = evaluate(corpus, {"demo": [{"name": "helper", "relative_path": "app.py"}]}, [])
    assert [o.entry.name for o in report.false_positives] == ["helper"]
    assert gate(report, {"false_negatives": 0, "recall": None}) == 1


def test_dead_symbol_that_is_not_reported_is_a_false_negative():
    corpus = _corpus(_entry("gone", DEAD, path="app.py"))
    report = evaluate(corpus, {"demo": []}, [])
    assert [o.entry.name for o in report.false_negatives] == ["gone"]
    assert report.recall == 0.0
    assert gate(report, {"false_negatives": 0, "recall": 0.0}) == 1


def test_recall_regression_fails_even_without_new_false_negatives():
    corpus = _corpus(
        _entry("a", DEAD, path="app.py"),
        _entry("b", DEAD, path="app.py"),
    )
    rows = {"demo": [{"name": "a", "relative_path": "app.py"}]}
    report = evaluate(corpus, rows, [])
    assert report.recall == 0.5
    assert gate(report, {"false_negatives": 1, "recall": 1.0}) == 1
    assert gate(report, {"false_negatives": 1, "recall": 0.5}) == 0


def test_qualified_name_match_does_not_need_a_path():
    corpus = _corpus(_entry("Widget.run", LIVE, qualified_name="app.py::Widget.run"))
    rows = {"demo": [{"name": "run", "qualified_name": "app.py::Widget.run"}]}
    report = evaluate(corpus, rows, [])
    assert [o.reported for o in report.outcomes] == [True]


def test_missing_root_fails_loud_with_its_own_exit_code():
    corpus = _corpus(_entry("helper", LIVE, path="app.py"))
    report = evaluate(corpus, {}, ["demo"])
    assert report.missing_roots == ["demo"]
    assert gate(report, {"false_negatives": 0, "recall": None}) == 2


def test_an_empty_corpus_is_refused():
    with pytest.raises(ValueError, match="empty"):
        evaluate(_corpus(), {}, [])


def test_record_captures_the_measured_numbers():
    corpus = _corpus(_entry("a", DEAD, path="app.py"))
    report = evaluate(corpus, {"demo": [{"name": "a", "relative_path": "app.py"}]}, [])
    assert record(report, {}) == {
        "expected_dead": 1,
        "false_negatives": 0,
        "recall": 1.0,
        "recorded_from": "reviewed corpus",
    }


def test_entry_requires_a_qualified_name_or_a_path():
    corpus = _corpus(_entry("helper", LIVE))
    corpus["repos"] = {"demo": {}}
    entry = corpus["entries"][0]
    assert Entry(**entry).path is None
    with pytest.raises(ValueError, match="qualified_name or a path"):
        evaluate(corpus, {"demo": []}, [])


def test_shipped_corpus_is_well_formed_and_guards_only_live_symbols():
    corpus = load_corpus()
    assert corpus["entries"], "the shipped corpus must not be empty"
    assert {raw["verdict"] for raw in corpus["entries"]} == {LIVE}


def test_a_real_graph_reports_an_unreferenced_function(tmp_path):
    with GraphStore(tmp_path / "graph.db") as store:
        store.upsert_node(
            NodeInfo(
                kind="Function",
                name="orphan",
                file_path=str(tmp_path / "app.py"),
                line_start=1,
                line_end=2,
                language="python",
            )
        )
        store.commit()
        rows = find_dead_code(store, root=tmp_path)
    corpus = _corpus(_entry("orphan", DEAD, path="app.py"))
    report = evaluate(corpus, {"demo": rows}, [])
    assert report.false_negatives == []
    assert report.recall == 1.0
