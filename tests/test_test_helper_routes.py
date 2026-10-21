"""Test-file helper CALLS paths retain real test identities (issue #1109)."""

import subprocess

import pytest

from code_review_graph.changes import analyze_changes
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import EdgeInfo, NodeInfo


@pytest.fixture
def graph(tmp_path):
    with GraphStore(tmp_path / "g.db") as store:
        store.set_metadata("repo_root", "/repo")
        yield store


def node(store, name, file="/repo/widget.tsx", kind="Function", **kwargs):
    value = NodeInfo(kind, name, file, 1, 10, "tsx", **kwargs)
    store.upsert_node(value)
    return f"{file}::{name}"


def call(store, source, target, **extra):
    store.upsert_edge(EdgeInfo("CALLS", source, target, source.split("::")[0], 2, extra=extra))


def chain(store, count=1):
    widget = node(store, "Widget")
    helper = node(store, "view", "/repo/widget.spec.tsx", is_test=True)
    call(store, helper, widget)
    tests = []
    for index in range(count):
        test = node(store, f"renders_{index}", "/repo/widget.spec.tsx", kind="Test", is_test=True)
        call(store, test, helper)
        tests.append(test)
    store.commit()
    return widget, helper, tests


def test_gap_report_names_the_test_that_calls_the_helper(graph):
    widget, _, tests = chain(graph)
    result = analyze_changes(graph, changed_files=["/repo/widget.tsx"], repo_root="/repo")
    gap = next(g for g in result["test_gaps"] if g["name"] == "Widget")
    assert gap["coverage"] == "indirect"
    assert gap["covered_by"] == tests
    assert "Untested: Widget" not in result["summary"]
    assert graph.get_caller_test_routes([widget])[widget]["tests"] == tests


def test_tests_for_finds_all_tests_that_call_the_same_helper(graph):
    widget, _, tests = chain(graph, count=17)
    result = graph.get_transitive_tests(widget)
    assert {row["qualified_name"] for row in result} == set(tests)
    assert all(row["indirect"] for row in result)


def test_helper_tested_by_edges_alone_do_not_claim_a_test_path(graph):
    widget = node(graph, "Widget")
    helper = node(graph, "fixture", "/repo/widget.spec.tsx", is_test=True)
    test = node(graph, "some_test", "/repo/widget.spec.tsx", kind="Test", is_test=True)
    call(graph, helper, widget)
    graph.upsert_edge(EdgeInfo("TESTED_BY", helper, test, "/repo/widget.spec.tsx", 2))
    graph.commit()
    assert graph.get_caller_test_routes([widget]) == {}
    assert graph.get_transitive_tests(widget) == []


def test_same_named_helper_in_another_spec_is_not_a_route(graph):
    widget = node(graph, "Widget")
    uncalled = node(graph, "view", "/repo/widget.spec.tsx", is_test=True)
    other = node(graph, "view", "/repo/other.spec.tsx", is_test=True)
    test = node(graph, "renders", "/repo/other.spec.tsx", kind="Test", is_test=True)
    call(graph, uncalled, widget)
    call(graph, test, other)
    graph.commit()
    assert graph.get_caller_test_routes([widget]) == {}
    assert graph.get_transitive_tests(widget) == []


@pytest.mark.parametrize("extra", [
    {"unresolved_targets": ["unknown"]}, {"ambiguous_targets": ["one", "two"]},
])
def test_unresolved_call_evidence_is_not_coverage(graph, extra):
    widget = node(graph, "Widget")
    helper = node(graph, "view", "/repo/widget.spec.tsx", is_test=True)
    test = node(graph, "renders", "/repo/widget.spec.tsx", kind="Test", is_test=True)
    call(graph, helper, widget)
    call(graph, test, helper, **extra)
    graph.commit()
    assert graph.get_transitive_tests(widget) == []


def test_depth_and_fanout_limits_are_respected(graph):
    widget, _, _ = chain(graph, count=3)
    assert graph.get_caller_test_routes([widget], max_depth=1) == {}
    assert graph.get_caller_test_routes([widget], max_callers_per_node=2) == {}
    assert widget in graph.get_caller_test_routes([widget], max_depth=2)


def test_direct_tests_and_helper_tests_are_both_retained(graph):
    widget, _, tests = chain(graph)
    direct = node(graph, "direct", "/repo/widget.spec.tsx", kind="Test", is_test=True)
    call(graph, direct, widget)
    graph.upsert_edge(EdgeInfo("TESTED_BY", widget, direct, "/repo/widget.spec.tsx", 2))
    graph.commit()
    found = {row["qualified_name"]: row for row in graph.get_transitive_tests(widget)}
    assert found[direct]["indirect"] is False
    assert found[tests[0]]["indirect"] is True


def test_unflagged_spec_helpers_and_cycles_keep_test_identity(graph):
    widget = node(graph, "Widget")
    helper = node(graph, "view", "/repo/widget.spec.tsx")
    nested = node(graph, "makeView", "/repo/widget.spec.tsx")
    test = node(graph, "renders", "/repo/widget.spec.tsx", kind="Test", is_test=True)
    call(graph, helper, widget)
    call(graph, nested, helper)
    call(graph, helper, nested)
    call(graph, test, nested)
    graph.commit()
    assert graph.get_transitive_tests(widget) == []
    assert [r["qualified_name"] for r in graph.get_transitive_tests(widget, max_depth=8)] == [test]
    assert graph.get_caller_test_routes([widget], max_depth=8)[widget]["depth"] == 3


def test_helper_paths_do_not_hide_routes_for_other_symbols(graph):
    widget, _, _ = chain(graph, count=3)
    other = node(graph, "Other")
    helper = node(graph, "otherView", "/repo/other.spec.tsx")
    test = node(graph, "other_test", "/repo/other.spec.tsx", kind="Test", is_test=True)
    call(graph, helper, other)
    call(graph, test, helper)
    graph.commit()
    routes = graph.get_caller_test_routes([widget, other], max_callers_per_node=2)
    assert widget not in routes
    assert routes[other]["tests"] == [test]


def test_real_react_spec_and_interface_report(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    component = tmp_path / "widget.tsx"
    component.write_text(
        "export interface WidgetProps { title: string }\n"
        "export function Widget({ title }: WidgetProps) { return <h1>{title}</h1>; }\n",
        encoding="utf-8",
    )
    spec = tmp_path / "widget.spec.tsx"
    spec.write_text(
        "import { render } from '@testing-library/react';\n"
        "import { Widget } from './widget';\n"
        "const view = () => render(<Widget title='Hi' />);\n"
        "describe('Widget', () => { it('renders', () => { view(); }); });\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "--", "widget.tsx", "widget.spec.tsx"], cwd=tmp_path,
                   check=True, capture_output=True)
    with GraphStore(tmp_path / "g.db") as store:
        full_build(tmp_path, store)
        qn = f"{component.as_posix()}::Widget"
        assert store.get_transitive_tests(qn)
        result = analyze_changes(store, changed_files=[component.as_posix()],
                                 repo_root=str(tmp_path))
        gaps = {g["name"]: g for g in result["test_gaps"]}
        assert "WidgetProps" not in gaps
        assert gaps["Widget"]["coverage"] == "indirect"
