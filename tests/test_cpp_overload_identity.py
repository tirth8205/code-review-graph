"""Regression coverage for stable C++ overload identities (#622)."""

from pathlib import Path
from unittest.mock import patch

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import CPP_IDENTITY_VERSION, incremental_update
from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo
from code_review_graph.tools.query import query_graph


def _index_source(tmp_path: Path, source: str) -> tuple[Path, GraphStore]:
    source_path = tmp_path / "IWorkspace.cpp"
    source_path.write_text(source, encoding="utf-8")

    nodes, edges = CodeParser().parse_file(source_path)
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir(exist_ok=True)
    store = GraphStore(graph_dir / "graph.db")
    store.store_file_nodes_edges(str(source_path), nodes, edges)
    return source_path, store


def test_cpp_overloads_keep_distinct_scoped_signature_identities(tmp_path: Path):
    source_path, store = _index_source(
        tmp_path,
        """void markChanged() {}
void IWorkspace::deleteDataFile(
    DataFile* file,
    bool refresh,
    bool preservePlot)
{
    markChanged();
}
void IWorkspace::deleteDataFile(DataFile* file, bool refresh) {}
""",
    )
    prefix = str(source_path)
    three_arg = f"{prefix}::IWorkspace.deleteDataFile(DataFile*,bool,bool)"
    two_arg = f"{prefix}::IWorkspace.deleteDataFile(DataFile*,bool)"
    changed = f"{prefix}::markChanged()"

    try:
        overloads = [
            node
            for node in store.get_nodes_by_file(str(source_path))
            if node.name == "deleteDataFile"
        ]
        assert {node.qualified_name for node in overloads} == {three_arg, two_arg}
        assert {node.parent_name for node in overloads} == {"IWorkspace"}
        assert store.get_node(three_arg).line_start == 2
        assert store.get_node(three_arg).line_end == 8
    finally:
        store.close()

    callers = query_graph("callers_of", changed, repo_root=str(tmp_path))
    assert callers["status"] == "ok"
    assert [result["qualified_name"] for result in callers["results"]] == [three_arg]


def test_cpp_signature_normalizes_parameter_types_not_names_or_defaults(
    tmp_path: Path,
):
    source_path, store = _index_source(
        tmp_path,
        """void Widget::update(
    const std::vector<int>& values,
    DataFile * file,
    bool refresh = true) {}
void commented(int /* identity-neutral */ value) {}
void unnamed(int /* identity-neutral */) {}
void attributed([[maybe_unused]] int value) {}
""",
    )

    try:
        functions = [
            node
            for node in store.get_nodes_by_file(str(source_path))
            if node.kind == "Function"
        ]
        assert {node.qualified_name for node in functions} == {
            f"{source_path}::Widget.update(const std::vector<int>&,DataFile*,bool)",
            f"{source_path}::commented(int)",
            f"{source_path}::unnamed(int)",
            f"{source_path}::attributed(int)",
        }
    finally:
        store.close()


def test_ambiguous_cpp_call_records_candidates_without_claiming_an_overload(
    tmp_path: Path,
):
    source_path, store = _index_source(
        tmp_path,
        """void process(int value) {}
void process(double value) {}
void caller() { process(1); }
""",
    )
    prefix = str(source_path)
    int_overload = f"{prefix}::process(int)"
    double_overload = f"{prefix}::process(double)"
    caller = f"{prefix}::caller()"

    try:
        call_edges = [
            edge
            for edge in store.get_edges_by_source(caller)
            if edge.kind == "CALLS"
        ]
        assert len(call_edges) == 1
        assert call_edges[0].target_qualified == "process"
        assert set(call_edges[0].extra["ambiguous_targets"]) == {
            int_overload,
            double_overload,
        }
    finally:
        store.close()

    ambiguous = query_graph("callers_of", "process", repo_root=str(tmp_path))
    assert ambiguous["status"] == "ambiguous"
    assert {
        candidate["qualified_name"] for candidate in ambiguous["disambiguation"]
    } == {int_overload, double_overload}

    exact = query_graph("callers_of", int_overload, repo_root=str(tmp_path))
    assert exact["status"] == "ok"
    assert exact["results"] == []

    callees = query_graph("callees_of", caller, repo_root=str(tmp_path))
    assert callees["status"] == "ok"
    assert callees["results"] == [{
        "kind": "Function",
        "name": "process",
        "qualified_name": "process",
        "resolution": "ambiguous",
        "candidates": [int_overload, double_overload],
        "candidate_count": 2,
        "candidates_truncated": False,
    }]
    assert callees["edges"][0]["ambiguous_targets"] == [
        int_overload,
        double_overload,
    ]
    assert callees["edges"][0]["ambiguous_target_count"] == 2
    assert callees["edges"][0]["ambiguous_targets_truncated"] is False


def test_cpp_call_resolution_prefers_the_lexical_class_scope(tmp_path: Path):
    source_path, store = _index_source(
        tmp_path,
        """struct A {
    void process() {}
    void caller() { process(); }
};
struct B { void process() {} };
""",
    )
    prefix = str(source_path)
    caller = f"{prefix}::A.caller()"
    target = f"{prefix}::A.process()"

    try:
        call_edges = [
            edge
            for edge in store.get_edges_by_source(caller)
            if edge.kind == "CALLS"
        ]
        assert [(edge.target_qualified, edge.extra) for edge in call_edges] == [
            (target, {}),
        ]
    finally:
        store.close()


def test_cpp_member_call_does_not_bind_to_the_enclosing_class(tmp_path: Path):
    source_path, store = _index_source(
        tmp_path,
        """struct B { void process() {} };
struct A {
    void process() {}
    void caller(B& b) { b.process(); }
};
""",
    )
    prefix = str(source_path)
    caller = f"{prefix}::A.caller(B&)"

    try:
        call_edges = [
            edge
            for edge in store.get_edges_by_source(caller)
            if edge.kind == "CALLS"
        ]
        assert len(call_edges) == 1
        assert call_edges[0].target_qualified == "process"
        assert call_edges[0].extra["receiver"] == "b"
        assert set(call_edges[0].extra["unresolved_targets"]) == {
            f"{prefix}::A.process()",
            f"{prefix}::B.process()",
        }
        assert call_edges[0].extra["unresolved_target_count"] == 2
        assert call_edges[0].extra["unresolved_targets_truncated"] is False
    finally:
        store.close()


def test_cpp_this_call_resolves_to_signature_identity(tmp_path: Path):
    source_path, store = _index_source(
        tmp_path,
        """struct A {
    void process() {}
    void caller() { this->process(); }
};
""",
    )
    caller = f"{source_path}::A.caller()"
    target = f"{source_path}::A.process()"

    try:
        call_edges = [
            edge
            for edge in store.get_edges_by_source(caller)
            if edge.kind == "CALLS"
        ]
        assert [(edge.target_qualified, edge.extra) for edge in call_edges] == [
            (target, {"receiver": "this"}),
        ]
        assert store.get_node(target) is not None
    finally:
        store.close()


def test_cpp_overloaded_this_call_stays_ambiguous_within_its_class(
    tmp_path: Path,
):
    source_path, store = _index_source(
        tmp_path,
        """struct B { void process(int value) {} };
struct A {
    void process(int value) {}
    void process(double value) {}
    void caller() { this->process(1); }
};
""",
    )
    caller = f"{source_path}::A.caller()"

    try:
        call_edges = [
            edge
            for edge in store.get_edges_by_source(caller)
            if edge.kind == "CALLS"
        ]
        assert len(call_edges) == 1
        assert call_edges[0].target_qualified == "process"
        assert set(call_edges[0].extra["ambiguous_targets"]) == {
            f"{source_path}::A.process(int)",
            f"{source_path}::A.process(double)",
        }
        assert call_edges[0].extra["ambiguous_target_count"] == 2
        assert call_edges[0].extra["ambiguous_targets_truncated"] is False
        assert call_edges[0].extra["receiver"] == "this"
    finally:
        store.close()


def test_cpp_call_resolution_walks_enclosing_namespace_scopes(tmp_path: Path):
    source_path, store = _index_source(
        tmp_path,
        """namespace N {
void helper() {}
namespace M { void caller() { helper(); } }
struct A { void caller() { helper(); } };
}
""",
    )
    prefix = str(source_path)
    target = f"{prefix}::N.helper()"

    try:
        for caller in (f"{prefix}::N.M.caller()", f"{prefix}::N.A.caller()"):
            call_edges = [
                edge
                for edge in store.get_edges_by_source(caller)
                if edge.kind == "CALLS"
            ]
            assert [(edge.target_qualified, edge.extra) for edge in call_edges] == [
                (target, {}),
            ]
    finally:
        store.close()


def test_cpp_explicit_scope_prefers_the_callers_lexical_namespace(tmp_path: Path):
    source_path, store = _index_source(
        tmp_path,
        """struct A { static void run() {} };
namespace N {
struct A { static void run() {} };
void caller() { A::run(); }
}
""",
    )
    caller = f"{source_path}::N.caller()"
    target = f"{source_path}::N.A.run()"

    try:
        call_edges = [
            edge
            for edge in store.get_edges_by_source(caller)
            if edge.kind == "CALLS"
        ]
        assert [(edge.target_qualified, edge.extra) for edge in call_edges] == [
            (target, {}),
        ]
    finally:
        store.close()


def test_cpp_callable_candidate_wins_over_same_named_class(tmp_path: Path):
    source_path, store = _index_source(
        tmp_path,
        """struct process {};
void process(int value) {}
void caller() { process(1); }
""",
    )
    caller = f"{source_path}::caller()"
    target = f"{source_path}::process(int)"

    try:
        call_edges = [
            edge
            for edge in store.get_edges_by_source(caller)
            if edge.kind == "CALLS"
        ]
        assert [(edge.target_qualified, edge.extra) for edge in call_edges] == [
            (target, {}),
        ]
    finally:
        store.close()


def test_cpp_candidate_metadata_is_bounded_and_reports_truncation(tmp_path: Path):
    overloads = "\n".join(
        f"void process(Type{index} value) {{}}" for index in range(25)
    )
    source_path, store = _index_source(
        tmp_path,
        f"{overloads}\nvoid caller() {{ process(1); }}\n",
    )
    caller = f"{source_path}::caller()"

    try:
        call_edges = [
            edge
            for edge in store.get_edges_by_source(caller)
            if edge.kind == "CALLS"
        ]
        assert len(call_edges) == 1
        assert len(call_edges[0].extra["ambiguous_targets"]) == 20
        assert call_edges[0].extra["ambiguous_target_count"] == 25
        assert call_edges[0].extra["ambiguous_targets_truncated"] is True
    finally:
        store.close()

    callees = query_graph("callees_of", caller, repo_root=str(tmp_path))
    assert len(callees["results"][0]["candidates"]) == 20
    assert callees["results"][0]["candidate_count"] == 25
    assert callees["results"][0]["candidates_truncated"] is True
    assert callees["edges"][0]["ambiguous_target_count"] == 25
    assert callees["edges"][0]["ambiguous_targets_truncated"] is True

    ambiguous = query_graph("callers_of", "process", repo_root=str(tmp_path))
    assert ambiguous["status"] == "ambiguous"
    assert len(ambiguous["disambiguation"]) == 20
    assert ambiguous["candidate_count"] == 25
    assert ambiguous["candidates_truncated"] is True
    assert "matches 25 node(s)" in ambiguous["summary"]


def test_cpp_explicit_scope_calls_resolve_or_preserve_ambiguity(tmp_path: Path):
    source_path, store = _index_source(
        tmp_path,
        """struct A {
    static void unique() {}
    static void overloaded(int value) {}
    static void overloaded(double value) {}
};
namespace N { void helper() {} }
void caller() { A::unique(); A::overloaded(1); N::helper(); }
""",
    )
    caller = f"{source_path}::caller()"

    try:
        call_edges = [
            edge
            for edge in store.get_edges_by_source(caller)
            if edge.kind == "CALLS"
        ]
        assert {
            edge.target_qualified
            for edge in call_edges
            if not edge.extra.get("ambiguous_targets")
        } == {
            f"{source_path}::A.unique()",
            f"{source_path}::N.helper()",
        }
        ambiguous_edges = [
            edge for edge in call_edges if edge.extra.get("ambiguous_targets")
        ]
        assert len(ambiguous_edges) == 1
        assert ambiguous_edges[0].target_qualified == "A::overloaded"
        assert set(ambiguous_edges[0].extra["ambiguous_targets"]) == {
            f"{source_path}::A.overloaded(int)",
            f"{source_path}::A.overloaded(double)",
        }
    finally:
        store.close()

    callees = query_graph("callees_of", caller, repo_root=str(tmp_path))
    ambiguous_result = next(
        result
        for result in callees["results"]
        if result.get("resolution") == "ambiguous"
    )
    assert ambiguous_result["qualified_name"] == "A::overloaded"
    assert ambiguous_result["candidate_count"] == 2
    assert ambiguous_result["candidates_truncated"] is False


def test_cpp_identity_includes_member_qualifiers_and_variadic_marker(
    tmp_path: Path,
):
    source_path, store = _index_source(
        tmp_path,
        """struct Widget {
    void update() {}
    void update() const {}
    void visit() & {}
    void visit() && {}
};
void logValues(int value, ...) {}
""",
    )

    try:
        identities = {
            node.qualified_name
            for node in store.get_nodes_by_file(str(source_path))
            if node.kind == "Function"
        }
        assert identities == {
            f"{source_path}::Widget.update()",
            f"{source_path}::Widget.update() const",
            f"{source_path}::Widget.visit() &",
            f"{source_path}::Widget.visit() &&",
            f"{source_path}::logValues(int,...)",
        }
    finally:
        store.close()


def test_cpp_identity_includes_lexical_namespace_scope(tmp_path: Path):
    source_path, store = _index_source(
        tmp_path,
        """namespace Alpha {
void process(int value) {}
struct Widget : Base { void read() const {} };
}
namespace Beta { void process(int value) {} }
""",
    )

    try:
        identities = {
            node.qualified_name
            for node in store.get_nodes_by_file(str(source_path))
            if node.kind == "Function"
        }
        assert identities == {
            f"{source_path}::Alpha.process(int)",
            f"{source_path}::Alpha.Widget.read() const",
            f"{source_path}::Beta.process(int)",
        }
        class_qn = f"{source_path}::Widget"
        widget = store.get_node(class_qn)
        assert widget is not None
        assert widget.parent_name is None
        assert any(
            edge.kind == "CONTAINS" and edge.target_qualified == class_qn
            for edge in store.get_edges_by_source(str(source_path))
        )
        assert any(
            edge.kind == "INHERITS" and edge.target_qualified == "Base"
            for edge in store.get_edges_by_source(class_qn)
        )
        assert any(
            edge.kind == "CONTAINS"
            and edge.target_qualified == f"{source_path}::Alpha.Widget.read() const"
            for edge in store.get_edges_by_source(class_qn)
        )
    finally:
        store.close()


def test_cpp_nested_class_keys_stay_legacy_while_function_scope_is_complete(
    tmp_path: Path,
):
    source_path, store = _index_source(
        tmp_path,
        """struct Outer {
    struct Inner {
        struct Deep : Base { void run() {} };
    };
};
""",
    )
    prefix = str(source_path)
    outer = f"{prefix}::Outer"
    inner = f"{prefix}::Outer.Inner"
    deep = f"{prefix}::Inner.Deep"
    run = f"{prefix}::Outer.Inner.Deep.run()"

    try:
        class_ids = {
            node.qualified_name
            for node in store.get_nodes_by_file(str(source_path))
            if node.kind == "Class"
        }
        assert class_ids == {outer, inner, deep}
        assert store.get_node(run) is not None
        assert any(
            edge.kind == "INHERITS" and edge.target_qualified == "Base"
            for edge in store.get_edges_by_source(deep)
        )
        assert any(
            edge.kind == "CONTAINS" and edge.target_qualified == run
            for edge in store.get_edges_by_source(deep)
        )
    finally:
        store.close()


def test_reindex_replaces_legacy_unsuffixed_cpp_identity(tmp_path: Path):
    source_path = tmp_path / "IWorkspace.cpp"
    source_path.write_text(
        "void IWorkspace::deleteDataFile(DataFile* file, bool refresh) {}\n",
        encoding="utf-8",
    )
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()
    store = GraphStore(graph_dir / "graph.db")
    legacy_qn = f"{source_path}::IWorkspace.deleteDataFile"

    try:
        store.upsert_node(
            NodeInfo(
                kind="Function",
                name="deleteDataFile",
                file_path=str(source_path),
                line_start=1,
                line_end=1,
                language="cpp",
                parent_name="IWorkspace",
            )
        )
        store.commit()
        assert store.get_node(legacy_qn) is not None

        nodes, edges = CodeParser().parse_file(source_path)
        store.store_file_nodes_edges(str(source_path), nodes, edges)

        assert store.get_node(legacy_qn) is None
        assert store.get_node(
            f"{source_path}::IWorkspace.deleteDataFile(DataFile*,bool)"
        ) is not None
    finally:
        store.close()


def test_incremental_upgrade_rebuilds_cpp_identities_and_removes_stale_edges(
    tmp_path: Path,
):
    callee_path = tmp_path / "callee.cpp"
    caller_path = tmp_path / "caller.cpp"
    callee_path.write_text("void run(int value) {}\n", encoding="utf-8")
    caller_path.write_text("void caller() { run(1); }\n", encoding="utf-8")
    store = GraphStore(tmp_path / "graph.db")
    legacy_callee = f"{callee_path}::run"
    legacy_caller = f"{caller_path}::caller"

    try:
        for path, name in ((callee_path, "run"), (caller_path, "caller")):
            store.upsert_node(NodeInfo(
                kind="Function",
                name=name,
                file_path=str(path),
                line_start=1,
                line_end=1,
                language="cpp",
            ))
        store.upsert_edge(EdgeInfo(
            kind="CALLS",
            source=legacy_caller,
            target=legacy_callee,
            file_path=str(caller_path),
            line=1,
        ))
        store.commit()

        with patch(
            "code_review_graph.incremental.get_all_tracked_files",
            return_value=["callee.cpp", "caller.cpp"],
        ):
            result = incremental_update(tmp_path, store, changed_files=[])

        assert result["identity_rebuild"] is True
        assert store.get_metadata("cpp_identity_version") == CPP_IDENTITY_VERSION
        assert store.get_node(legacy_callee) is None
        assert store.get_node(f"{callee_path}::run(int)") is not None
        assert all(
            edge.target_qualified != legacy_callee
            for edge in store.get_edges_by_source(f"{caller_path}::caller()")
        )
    finally:
        store.close()


def test_cross_file_bare_call_is_not_claimed_by_each_exact_overload(
    tmp_path: Path,
):
    callee_path = tmp_path / "callee.cpp"
    caller_path = tmp_path / "caller.cpp"
    callee_path.write_text(
        "void process(int value) {}\nvoid process(double value) {}\n",
        encoding="utf-8",
    )
    caller_path.write_text(
        "void caller() { process(1); }\n",
        encoding="utf-8",
    )
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()
    store = GraphStore(graph_dir / "graph.db")

    try:
        parser = CodeParser()
        for path in (callee_path, caller_path):
            nodes, edges = parser.parse_file(path)
            store.store_file_nodes_edges(str(path), nodes, edges)
        bare_edges = [
            edge
            for edge in store.get_edges_by_source(f"{caller_path}::caller()")
            if edge.kind == "CALLS"
        ]
        assert [(edge.target_qualified, edge.extra) for edge in bare_edges] == [
            ("process", {}),
        ]
    finally:
        store.close()

    for overload in ("process(int)", "process(double)"):
        callers = query_graph(
            "callers_of",
            f"{callee_path}::{overload}",
            repo_root=str(tmp_path),
        )
        assert callers["status"] == "ok"
        assert callers["results"] == []


def test_failed_cpp_identity_upgrade_remains_pending_and_retries(tmp_path: Path):
    source_path = tmp_path / "run.cpp"
    source_path.write_text("void run(int value) {}\n", encoding="utf-8")
    legacy_qn = f"{source_path}::run"
    store = GraphStore(tmp_path / "graph.db")

    try:
        store.upsert_node(NodeInfo(
            kind="Function",
            name="run",
            file_path=str(source_path),
            line_start=1,
            line_end=1,
            language="cpp",
        ))
        store.commit()

        with (
            patch(
                "code_review_graph.incremental.get_all_tracked_files",
                return_value=["run.cpp"],
            ),
            patch(
                "code_review_graph.incremental.CodeParser.parse_bytes",
                side_effect=RuntimeError("simulated parse failure"),
            ),
        ):
            failed = incremental_update(tmp_path, store, changed_files=[])

        assert failed["identity_rebuild"] is True
        assert failed["errors"]
        assert store.get_metadata("cpp_identity_version") is None
        assert store.get_node(legacy_qn) is not None

        with patch(
            "code_review_graph.incremental.get_all_tracked_files",
            return_value=["run.cpp"],
        ):
            retried = incremental_update(tmp_path, store, changed_files=[])

        assert retried["identity_rebuild"] is True
        assert retried["errors"] == []
        assert store.get_metadata("cpp_identity_version") == CPP_IDENTITY_VERSION
        assert store.get_node(legacy_qn) is None
        assert store.get_node(f"{source_path}::run(int)") is not None
    finally:
        store.close()


def test_cpp_reference_return_and_operator_overloads_keep_callable_identity(
    tmp_path: Path,
):
    source_path, store = _index_source(
        tmp_path,
        """struct A {
    A& operator=(const A& other) { return *this; }
    operator bool() const { return true; }
};
A& clone(int value) { static A result; return result; }
A& clone(double value) { static A result; return result; }
""",
    )

    try:
        functions = {
            node.qualified_name
            for node in store.get_nodes_by_file(str(source_path))
            if node.kind == "Function"
        }
        assert functions == {
            f"{source_path}::A.operator=(const A&)",
            f"{source_path}::A.operator bool() const",
            f"{source_path}::clone(int)",
            f"{source_path}::clone(double)",
        }
    finally:
        store.close()


def test_cpp_leading_global_scope_is_normalized(tmp_path: Path):
    source_path, store = _index_source(
        tmp_path,
        """namespace N { struct A { static void run(); }; }
void ::N::A::run() {}
""",
    )

    try:
        run = store.get_node(f"{source_path}::N.A.run()")
        assert run is not None
        assert run.parent_name == "N.A"
        assert store.get_node(f"{source_path}::.N.A.run()") is None
    finally:
        store.close()


def test_cross_file_receiver_call_without_type_evidence_stays_unresolved(
    tmp_path: Path,
):
    callee_path = tmp_path / "callee.cpp"
    caller_path = tmp_path / "caller.cpp"
    callee_path.write_text(
        "struct A { void process() {} };\n",
        encoding="utf-8",
    )
    caller_path.write_text(
        "struct B; void test_receiver(B& b) { b.process(); }\n",
        encoding="utf-8",
    )
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()
    store = GraphStore(graph_dir / "graph.db")

    target = f"{callee_path}::A.process()"
    caller = f"{caller_path}::test_receiver(B&)"
    try:
        parser = CodeParser()
        for path in (callee_path, caller_path):
            nodes, edges = parser.parse_file(path)
            store.store_file_nodes_edges(str(path), nodes, edges)
        store.upsert_edge(EdgeInfo(
            kind="IMPORTS_FROM",
            source=str(caller_path),
            target=str(callee_path),
            file_path=str(caller_path),
            line=1,
        ))
        store.commit()

        call = next(
            edge
            for edge in store.get_edges_by_source(caller)
            if edge.kind == "CALLS"
        )
        assert call.target_qualified == "process"
        assert call.extra["receiver"] == "b"
        assert call.extra["unresolved_targets"] == []
        assert call.extra["unresolved_target_count"] == 0
        assert store.resolve_bare_call_targets() == 0
        assert store.resolve_bare_tested_by_sources() == 0
        call_after_postprocess = next(
            edge
            for edge in store.get_edges_by_source(caller)
            if edge.kind == "CALLS"
        )
        assert call_after_postprocess.target_qualified == "process"
        tested_by = [
            edge
            for edge in store.get_edges_by_target(caller)
            if edge.kind == "TESTED_BY"
        ]
        assert len(tested_by) == 1
        assert tested_by[0].source_qualified == "process"
        assert tested_by[0].extra["unresolved_targets"] == []
        assert store.get_transitive_tests(target, max_depth=0) == []
    finally:
        store.close()

    callers = query_graph("callers_of", target, repo_root=str(tmp_path))
    assert callers["status"] == "ok"
    assert callers["results"] == []


def test_cross_file_scoped_calls_resolve_or_keep_bounded_overload_candidates(
    tmp_path: Path,
):
    callee_path = tmp_path / "callee.cpp"
    caller_path = tmp_path / "caller.cpp"
    callee_path.write_text(
        """void A::unique() {}
void A::run(int value) {}
void A::run(double value) {}
""",
        encoding="utf-8",
    )
    caller_path.write_text(
        "void test_run() { A::unique(); A::run(1); }\n",
        encoding="utf-8",
    )
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()
    store = GraphStore(graph_dir / "graph.db")
    caller = f"{caller_path}::test_run()"

    try:
        parser = CodeParser()
        for path in (callee_path, caller_path):
            nodes, edges = parser.parse_file(path)
            store.store_file_nodes_edges(str(path), nodes, edges)

        assert store.resolve_cpp_scoped_call_targets() == 1
        assert store.resolve_cpp_scoped_call_targets() == 0
        calls = [
            edge
            for edge in store.get_edges_by_source(caller)
            if edge.kind == "CALLS"
        ]
        unique = next(edge for edge in calls if edge.target_qualified.endswith("unique()"))
        assert unique.target_qualified == f"{callee_path}::A.unique()"

        overloaded = next(edge for edge in calls if edge.target_qualified == "A::run")
        assert set(overloaded.extra["ambiguous_targets"]) == {
            f"{callee_path}::A.run(int)",
            f"{callee_path}::A.run(double)",
        }
        assert overloaded.extra["ambiguous_target_count"] == 2
        assert overloaded.extra["ambiguous_targets_truncated"] is False

        tested_by = {
            edge.source_qualified: edge
            for edge in store.get_edges_by_target(caller)
            if edge.kind == "TESTED_BY"
        }
        unique_target = f"{callee_path}::A.unique()"
        assert tested_by[unique_target].extra["cpp_scoped_target"] == "A::unique"
        assert tested_by["A::run"].extra == overloaded.extra
        assert [
            match["qualified_name"]
            for match in store.get_transitive_tests(unique_target, max_depth=0)
        ] == [caller]
        assert store.get_transitive_tests(
            f"{callee_path}::A.run(int)", max_depth=0,
        ) == []
    finally:
        store.close()

    tests_for_unique = query_graph(
        "tests_for", unique_target, repo_root=str(tmp_path),
    )
    assert [
        result["qualified_name"] for result in tests_for_unique["results"]
    ] == [caller]
    tests_for_overload = query_graph(
        "tests_for", f"{callee_path}::A.run(int)", repo_root=str(tmp_path),
    )
    assert tests_for_overload["results"] == []


def test_ambiguous_scoped_calls_do_not_create_indirect_test_coverage(
    tmp_path: Path,
):
    callee_path = tmp_path / "callee.cpp"
    production_path = tmp_path / "production.cpp"
    test_path = tmp_path / "scenario_test.cpp"
    callee_path.write_text(
        "void A::run(int value) {}\nvoid A::run(double value) {}\n",
        encoding="utf-8",
    )
    production_path.write_text(
        "void production() { A::run(1); }\n",
        encoding="utf-8",
    )
    test_path.write_text(
        "void test_scenario() { A::run(1); }\n",
        encoding="utf-8",
    )
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()
    store = GraphStore(graph_dir / "graph.db")
    production = f"{production_path}::production()"

    try:
        parser = CodeParser()
        for path in (callee_path, production_path, test_path):
            nodes, edges = parser.parse_file(path)
            store.store_file_nodes_edges(str(path), nodes, edges)

        assert store.resolve_cpp_scoped_call_targets() == 0
        assert store.get_transitive_tests(production) == []
    finally:
        store.close()

    tests_for_production = query_graph(
        "tests_for", production, repo_root=str(tmp_path),
    )
    assert tests_for_production["results"] == []


def test_deleted_scoped_candidate_becomes_explicitly_unresolved(tmp_path: Path):
    callee_path = tmp_path / "callee.cpp"
    production_path = tmp_path / "production.cpp"
    test_path = tmp_path / "scenario_test.cpp"
    callee_path.write_text("void A::run(int value) {}\n", encoding="utf-8")
    production_path.write_text(
        "void production() { A::run(1); }\n",
        encoding="utf-8",
    )
    test_path.write_text(
        "void test_scenario() { A::run(1); }\n",
        encoding="utf-8",
    )
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()
    store = GraphStore(graph_dir / "graph.db")
    parser = CodeParser()
    production = f"{production_path}::production()"
    test = f"{test_path}::test_scenario()"

    try:
        for path in (callee_path, production_path, test_path):
            nodes, edges = parser.parse_file(path)
            store.store_file_nodes_edges(str(path), nodes, edges)

        assert store.resolve_cpp_scoped_call_targets() == 2
        assert [
            match["qualified_name"]
            for match in store.get_transitive_tests(production)
        ] == [test]

        callee_path.write_text("// A::run was removed\n", encoding="utf-8")
        nodes, edges = parser.parse_file(callee_path)
        store.store_file_nodes_edges(str(callee_path), nodes, edges)

        assert store.resolve_cpp_scoped_call_targets() == 0
        call = next(
            edge
            for edge in store.get_edges_by_source(production)
            if edge.kind == "CALLS"
        )
        tested_by = next(
            edge
            for edge in store.get_edges_by_target(test)
            if edge.kind == "TESTED_BY"
        )
        assert call.target_qualified == "A::run"
        assert call.extra["unresolved_targets"] == []
        assert call.extra["unresolved_target_count"] == 0
        assert tested_by.source_qualified == "A::run"
        assert tested_by.extra == call.extra
        assert store.get_transitive_tests(production) == []
    finally:
        store.close()

    tests_for_production = query_graph(
        "tests_for", production, repo_root=str(tmp_path),
    )
    assert tests_for_production["results"] == []


def test_missing_scoped_candidate_rechecks_when_definition_appears(tmp_path: Path):
    callee_path = tmp_path / "callee.cpp"
    caller_path = tmp_path / "caller.cpp"
    caller_path.write_text(
        "void caller() { A::run(1); }\n",
        encoding="utf-8",
    )
    store = GraphStore(tmp_path / "graph.db")
    parser = CodeParser()
    caller = f"{caller_path}::caller()"

    try:
        nodes, edges = parser.parse_file(caller_path)
        store.store_file_nodes_edges(str(caller_path), nodes, edges)

        assert store.resolve_cpp_scoped_call_targets() == 0
        call = next(
            edge
            for edge in store.get_edges_by_source(caller)
            if edge.kind == "CALLS"
        )
        assert call.target_qualified == "A::run"
        assert call.extra["cpp_scoped_target"] == "A::run"
        assert call.extra["unresolved_targets"] == []
        assert store.resolve_cpp_scoped_call_targets() == 0

        callee_path.write_text("void A::run(int value) {}\n", encoding="utf-8")
        nodes, edges = parser.parse_file(callee_path)
        store.store_file_nodes_edges(str(callee_path), nodes, edges)

        assert store.resolve_cpp_scoped_call_targets() == 1
        call = next(
            edge
            for edge in store.get_edges_by_source(caller)
            if edge.kind == "CALLS"
        )
        assert call.target_qualified == f"{callee_path}::A.run(int)"
        assert "unresolved_targets" not in call.extra
    finally:
        store.close()


def test_cross_file_scoped_resolution_rechecks_candidate_changes(tmp_path: Path):
    callee_path = tmp_path / "callee.cpp"
    caller_path = tmp_path / "caller.cpp"
    callee_path.write_text("void A::run(int value) {}\n", encoding="utf-8")
    caller_path.write_text(
        "void test_caller() { A::run(1); }\n",
        encoding="utf-8",
    )
    store = GraphStore(tmp_path / "graph.db")
    parser = CodeParser()
    caller = f"{caller_path}::test_caller()"

    try:
        for path in (callee_path, caller_path):
            nodes, edges = parser.parse_file(path)
            store.store_file_nodes_edges(str(path), nodes, edges)

        assert store.resolve_cpp_scoped_call_targets() == 1
        call = next(
            edge
            for edge in store.get_edges_by_source(caller)
            if edge.kind == "CALLS"
        )
        assert call.target_qualified == f"{callee_path}::A.run(int)"
        assert call.extra["cpp_scoped_target"] == "A::run"
        tested_by = next(
            edge
            for edge in store.get_edges_by_target(caller)
            if edge.kind == "TESTED_BY"
        )
        assert tested_by.source_qualified == f"{callee_path}::A.run(int)"
        assert tested_by.extra == call.extra

        callee_path.write_text(
            "void A::run(int value) {}\nvoid A::run(double value) {}\n",
            encoding="utf-8",
        )
        nodes, edges = parser.parse_file(callee_path)
        store.store_file_nodes_edges(str(callee_path), nodes, edges)

        assert store.resolve_cpp_scoped_call_targets() == 0
        call = next(
            edge
            for edge in store.get_edges_by_source(caller)
            if edge.kind == "CALLS"
        )
        assert call.target_qualified == "A::run"
        assert call.extra["ambiguous_target_count"] == 2
        tested_by = next(
            edge
            for edge in store.get_edges_by_target(caller)
            if edge.kind == "TESTED_BY"
        )
        assert tested_by.source_qualified == "A::run"
        assert tested_by.extra == call.extra

        callee_path.write_text(
            "void A::run(double value) {}\n",
            encoding="utf-8",
        )
        nodes, edges = parser.parse_file(callee_path)
        store.store_file_nodes_edges(str(callee_path), nodes, edges)

        assert store.resolve_cpp_scoped_call_targets() == 1
        call = next(
            edge
            for edge in store.get_edges_by_source(caller)
            if edge.kind == "CALLS"
        )
        assert call.target_qualified == f"{callee_path}::A.run(double)"
        assert call.extra["cpp_scoped_target"] == "A::run"
        assert "ambiguous_targets" not in call.extra
        assert "ambiguous_target_count" not in call.extra
        assert "ambiguous_targets_truncated" not in call.extra
        tested_by = next(
            edge
            for edge in store.get_edges_by_target(caller)
            if edge.kind == "TESTED_BY"
        )
        assert tested_by.source_qualified == f"{callee_path}::A.run(double)"
        assert tested_by.extra == call.extra
    finally:
        store.close()


def test_non_cpp_failure_does_not_repeat_cpp_identity_migration(tmp_path: Path):
    cpp_path = tmp_path / "run.cpp"
    python_path = tmp_path / "broken.py"
    cpp_path.write_text("void run(int value) {}\n", encoding="utf-8")
    python_path.write_text("def broken(): pass\n", encoding="utf-8")
    store = GraphStore(tmp_path / "graph.db")
    original_parse_bytes = CodeParser.parse_bytes

    def parse_with_python_failure(parser, path, source):
        if Path(path).suffix == ".py":
            raise RuntimeError("simulated non-C++ parse failure")
        return original_parse_bytes(parser, path, source)

    try:
        store.upsert_node(NodeInfo(
            kind="Function",
            name="run",
            file_path=str(cpp_path),
            line_start=1,
            line_end=1,
            language="cpp",
        ))
        store.commit()

        with (
            patch(
                "code_review_graph.incremental.get_all_tracked_files",
                return_value=["run.cpp", "broken.py"],
            ),
            patch.object(CodeParser, "parse_bytes", new=parse_with_python_failure),
        ):
            migrated = incremental_update(tmp_path, store, changed_files=[])

        assert migrated["identity_rebuild"] is True
        assert migrated["errors"] == [
            {"file": "broken.py", "error": "simulated non-C++ parse failure"},
        ]
        assert store.get_metadata("cpp_identity_version") == CPP_IDENTITY_VERSION
        assert store.get_node(f"{cpp_path}::run(int)") is not None

        no_retry = incremental_update(tmp_path, store, changed_files=[])
        assert no_retry.get("identity_rebuild") is None
    finally:
        store.close()


def test_non_cpp_scoped_unresolved_callee_query_behavior_stays_unchanged(
    tmp_path: Path,
):
    source_path = tmp_path / "lib.rs"
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()
    store = GraphStore(graph_dir / "graph.db")
    caller = f"{source_path}::caller"

    try:
        store.upsert_node(NodeInfo(
            kind="Function",
            name="caller",
            file_path=str(source_path),
            line_start=1,
            line_end=1,
            language="rust",
        ))
        store.upsert_edge(EdgeInfo(
            kind="CALLS",
            source=caller,
            target="external::missing",
            file_path=str(source_path),
            line=1,
        ))
        store.commit()
    finally:
        store.close()

    result = query_graph("callees_of", caller, repo_root=str(tmp_path))
    assert result["status"] == "ok"
    assert result["results"] == []
