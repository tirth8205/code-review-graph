"""Dead-code edge-query scaling and resolution regressions, issue #982."""

from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.refactor import find_dead_code


def _node(store, name, kind="Function", file="/repo/app.py", language="python"):
    return store.upsert_node(NodeInfo(
        kind=kind, name=name, file_path=file, line_start=1, line_end=2, language=language,
    ))


def _queries(tmp_path, count):
    with GraphStore(tmp_path / f"graph-{count}.db") as store:
        for i in range(count):
            _node(store, f"unused_{i}")
        store.commit()
        statements = []
        store._conn.set_trace_callback(statements.append)
        assert len(find_dead_code(store)) == count
        store._conn.set_trace_callback(None)
        return [s for s in statements if "FROM EDGES" in s.upper()]


def test_edge_query_count_does_not_grow_with_candidates(tmp_path):
    small = _queries(tmp_path, 10)
    large = _queries(tmp_path, 200)
    assert len(large) <= len(small) + 1
    assert not any("LIKE" in s.upper() for s in large)


def test_partial_qualified_call_and_member_call_keep_symbols_alive(tmp_path):
    with GraphStore(tmp_path / "graph.db") as store:
        _node(store, "helper")
        _node(store, "Widget", kind="Class")
        for target in ("package::helper", "Widget.run"):
            store.upsert_edge(EdgeInfo(
                kind="CALLS", source="/repo/app.py::caller", target=target,
                file_path="/repo/app.py", line=1,
            ))
        store.commit()
        assert find_dead_code(store) == []


def test_same_named_bare_call_in_another_language_is_not_a_caller(tmp_path):
    with GraphStore(tmp_path / "graph.db") as store:
        _node(store, "helper")
        _node(store, "foreign", file="/repo/app.go", language="go")
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source="/repo/app.go::foreign", target="helper",
            file_path="/repo/app.go", line=1,
        ))
        store.commit()
        assert "helper" in {n["name"] for n in find_dead_code(store)}


def test_inheritance_evidence_does_not_become_call_evidence(tmp_path):
    with GraphStore(tmp_path / "graph.db") as store:
        _node(store, "helper")
        _node(store, "Child", kind="Class")
        store.upsert_edge(EdgeInfo(
            kind="INHERITS", source="/repo/app.py::Child", target="helper",
            file_path="/repo/app.py", line=1,
        ))
        store.commit()
        assert "helper" in {n["name"] for n in find_dead_code(store)}


def test_member_call_keeps_legacy_substring_heuristic(tmp_path):
    with GraphStore(tmp_path / "graph.db") as store:
        _node(store, "Widget", kind="Class")
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source="/repo/app.py::caller", target="MyWidget.run",
            file_path="/repo/app.py", line=1,
        ))
        store.commit()
        assert find_dead_code(store) == []


def test_partial_qualified_call_respects_import_ambiguity(tmp_path):
    with GraphStore(tmp_path / "graph.db") as store:
        _node(store, "helper", file="/repo/one.py")
        _node(store, "helper", file="/repo/two.py")
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source="/repo/use.py::caller", target="package::helper",
            file_path="/repo/use.py", line=1,
        ))
        store.upsert_edge(EdgeInfo(
            kind="IMPORTS_FROM", source="/repo/use.py", target="/repo/one.py",
            file_path="/repo/use.py", line=1,
        ))
        store.commit()
        dead = find_dead_code(store)
        assert {n["file_path"] for n in dead} == {"/repo/two.py"}
