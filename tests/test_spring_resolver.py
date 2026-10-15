"""Regression coverage for Spring DI declared-type resolution.

PR #1075 (issue #1074): a concrete injected type keeps its declared target,
and a production caller never resolves to a test-source implementor.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update
from code_review_graph.parser import CodeParser
from code_review_graph.postprocessing import run_post_processing
from code_review_graph.spring_resolver import resolve_spring_di_calls


def _build_repo(tmp_path: Path, files: dict[str, str]) -> GraphStore:
    for relative, source in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    store = GraphStore(str(tmp_path / "graph.db"))
    full_build(tmp_path, store)
    run_post_processing(store)
    return store


def _publisher_call_target(store: GraphStore) -> str:
    return _call_target(store, "Publisher.publish")


def _call_target(store: GraphStore, source_suffix: str) -> str:
    rows = store._conn.execute(
        "SELECT target_qualified FROM edges "
        "WHERE kind = 'CALLS' AND source_qualified LIKE ?",
        (f"%::{source_suffix}",),
    ).fetchall()
    assert len(rows) == 1
    return rows[0]["target_qualified"]


def _concrete_files(subclass_path: str) -> dict[str, str]:
    return {
        "src/main/java/app/Sink.java": (
            "package app;\n"
            "public class Sink { public void emit(String event) { } }\n"
        ),
        "src/main/java/app/Publisher.java": (
            "package app;\n"
            "import lombok.RequiredArgsConstructor;\n"
            "import org.springframework.beans.factory.annotation.Autowired;\n"
            "@RequiredArgsConstructor(onConstructor_ = {@Autowired})\n"
            "public class Publisher {\n"
            "    private final Sink sink;\n"
            "    public void publish(String event) { sink.emit(event); }\n"
            "}\n"
        ),
        subclass_path: (
            "package app;\n"
            "public class RecordingSink extends Sink {\n"
            "    public void emit(String event) { }\n"
            "}\n"
        ),
    }


def test_concrete_injected_type_does_not_resolve_to_test_subclass(tmp_path: Path):
    with _build_repo(
        tmp_path,
        _concrete_files("src/test/java/app/RecordingSink.java"),
    ) as store:
        assert _publisher_call_target(store).endswith("::Sink.emit")


def test_concrete_injected_type_does_not_resolve_to_production_subclass(tmp_path: Path):
    with _build_repo(
        tmp_path,
        _concrete_files("src/main/java/app/RecordingSink.java"),
    ) as store:
        assert _publisher_call_target(store).endswith("::Sink.emit")


def test_abstract_injected_type_resolves_to_unique_production_subclass(
    tmp_path: Path,
):
    files = _concrete_files("src/main/java/app/RecordingSink.java")
    files["src/main/java/app/Sink.java"] = (
        "package app;\n"
        "public abstract class Sink { public abstract void emit(String event); }\n"
    )
    with _build_repo(tmp_path, files) as store:
        assert _publisher_call_target(store).endswith("::RecordingSink.emit")


def test_interface_injected_type_resolves_to_unique_production_implementor(
    tmp_path: Path,
):
    files = {
        "src/main/java/app/Publisher.java": (
            "package app;\n"
            "import lombok.RequiredArgsConstructor;\n"
            "@RequiredArgsConstructor\n"
            "public class Publisher {\n"
            "    private final Sink sink;\n"
            "    public void publish(String event) { sink.emit(event); }\n"
            "}\n"
        ),
        "src/main/java/app/Sink.java": (
            "package app;\n"
            "public interface Sink { void emit(String event); }\n"
        ),
        "src/main/java/app/RecordingSink.java": (
            "package app;\n"
            "public class RecordingSink implements Sink {\n"
            "    public void emit(String event) { }\n"
            "}\n"
        ),
    }
    with _build_repo(tmp_path, files) as store:
        assert _publisher_call_target(store).endswith("::RecordingSink.emit")


def test_production_caller_does_not_resolve_to_test_only_implementor(
    tmp_path: Path,
):
    files = {
        "src/main/java/app/Publisher.java": (
            "package app;\n"
            "import lombok.RequiredArgsConstructor;\n"
            "@RequiredArgsConstructor\n"
            "public class Publisher {\n"
            "    private final Sink sink;\n"
            "    public void publish(String event) { sink.emit(event); }\n"
            "}\n"
        ),
        "src/main/java/app/Sink.java": (
            "package app;\n"
            "public interface Sink { void emit(String event); }\n"
        ),
        "src/test/java/app/RecordingSink.java": (
            "package app;\n"
            "public class RecordingSink implements Sink {\n"
            "    public void emit(String event) { }\n"
            "}\n"
        ),
    }
    with _build_repo(tmp_path, files) as store:
        assert _publisher_call_target(store).endswith("Sink.emit")


def test_java_type_metadata_distinguishes_interface_abstract_and_concrete():
    nodes, _ = CodeParser().parse_bytes(
        Path("Types.java"),
        b"interface Contract {} abstract class Base {} class Concrete {}",
    )
    kinds = {
        node.name: node.extra.get("java_kind")
        for node in nodes
        if node.kind == "Class"
    }
    assert kinds == {
        "Contract": "interface",
        "Base": "abstract",
        "Concrete": "concrete",
    }


# ---------------------------------------------------------------------------
# Edge cases added while hardening #1075.
# ---------------------------------------------------------------------------

_PUBLISHER = (
    "package app;\n"
    "import lombok.RequiredArgsConstructor;\n"
    "@RequiredArgsConstructor\n"
    "public class Publisher {\n"
    "    private final Sink sink;\n"
    "    public void publish(String event) { sink.emit(event); }\n"
    "}\n"
)
_HARNESS = (
    "package app;\n"
    "import lombok.RequiredArgsConstructor;\n"
    "@RequiredArgsConstructor\n"
    "public class PublisherHarness {\n"
    "    private final Sink sink;\n"
    "    public void drive(String event) { sink.emit(event); }\n"
    "}\n"
)
_SINK_INTERFACE = "package app;\npublic interface Sink { void emit(String event); }\n"
_SINK_ABSTRACT = (
    "package app;\npublic abstract class Sink { public abstract void emit(String event); }\n"
)
_SINK_CONCRETE = "package app;\npublic class Sink { public void emit(String event) { } }\n"
_DECLARED = "src/main/java/app/Sink.java::Sink.emit"


def _implementor(name: str, relation: str) -> str:
    return (
        "package app;\n"
        f"public class {name} {relation} Sink {{\n"
        "    public void emit(String event) { }\n"
        "}\n"
    )


def _store_from_parsed(
    tmp_path: Path,
    files: list[tuple[str, str]],
    *,
    drop_java_kind: bool = False,
) -> GraphStore:
    """Parse *files* straight into a store in the given order, without post-passes.

    Insertion order is the row order the resolver reads, so this pins it.
    ``drop_java_kind`` reproduces a graph written before the Java metadata
    existed, including the qualified type fields added by this fix.
    """
    parser = CodeParser()
    store = GraphStore(str(tmp_path / "graph.db"))
    for relative, source in files:
        nodes, edges = parser.parse_bytes(Path(relative), source.encode("utf-8"))
        for node in nodes:
            if drop_java_kind:
                node.extra.pop("java_kind", None)
                node.extra.pop("java_package", None)
            store.upsert_node(node)
        for edge in edges:
            if drop_java_kind:
                edge.extra.pop("java_type", None)
            store.upsert_edge(edge)
    store.commit()
    return store


@pytest.mark.parametrize("repo_dir", ["repo", "my repo ü"])
def test_issue_1074_nested_test_double_does_not_capture_production_call(
    tmp_path: Path, repo_dir: str,
):
    """The exact reproduction from #1074, also under a path with a space and unicode."""
    root = tmp_path / repo_dir
    files = {
        "src/main/java/app/Sink.java": (
            "package app;\n"
            "import org.springframework.stereotype.Component;\n"
            "@Component\n"
            "public class Sink {\n"
            "    public void emit(String event) { }\n"
            "}\n"
        ),
        "src/main/java/app/Publisher.java": (
            "package app;\n"
            "import lombok.RequiredArgsConstructor;\n"
            "import org.springframework.beans.factory.annotation.Autowired;\n"
            "import org.springframework.stereotype.Component;\n"
            "@Component\n"
            "@RequiredArgsConstructor(onConstructor_ = {@Autowired})\n"
            "public class Publisher {\n"
            "    private final Sink sink;\n"
            "    public void publish(String event) { sink.emit(event); }\n"
            "}\n"
        ),
        "src/test/java/app/RecordingSinkConfig.java": (
            "package app;\n"
            "import org.springframework.boot.test.context.TestConfiguration;\n"
            "@TestConfiguration\n"
            "public class RecordingSinkConfig {\n"
            "    public static final class RecordingSink extends Sink {\n"
            "        @Override\n"
            "        public void emit(String event) { }\n"
            "    }\n"
            "}\n"
        ),
    }
    with _build_repo(root, files) as store:
        target = _publisher_call_target(store)
        assert target.endswith("/" + _DECLARED)
        callers = {
            edge.source_qualified
            for edge in store.get_edges_by_target(target)
            if edge.kind == "CALLS"
        }
        assert [c for c in callers if c.endswith("::Publisher.publish")]


@pytest.mark.parametrize(
    ("sink_source", "relation"),
    [(_SINK_INTERFACE, "implements"), (_SINK_ABSTRACT, "extends")],
    ids=["interface", "abstract"],
)
def test_production_caller_keeps_declared_type_when_only_implementor_is_a_test(
    tmp_path: Path, sink_source: str, relation: str,
):
    files = {
        "src/main/java/app/Publisher.java": _PUBLISHER,
        "src/main/java/app/Sink.java": sink_source,
        "src/test/java/app/RecordingSink.java": _implementor("RecordingSink", relation),
    }
    with _build_repo(tmp_path, files) as store:
        # Exact file and owner: "endswith('Sink.emit')" would also accept RecordingSink.emit.
        assert _publisher_call_target(store).endswith("/" + _DECLARED)


@pytest.mark.parametrize(
    ("sink_source", "relation"),
    [(_SINK_INTERFACE, "implements"), (_SINK_ABSTRACT, "extends")],
    ids=["interface", "abstract"],
)
def test_production_caller_picks_sole_production_implementor_beside_test_double(
    tmp_path: Path, sink_source: str, relation: str,
):
    files = {
        "src/main/java/app/Publisher.java": _PUBLISHER,
        "src/main/java/app/Sink.java": sink_source,
        "src/main/java/app/KafkaSink.java": _implementor("KafkaSink", relation),
        "src/test/java/app/RecordingSink.java": _implementor("RecordingSink", relation),
    }
    with _build_repo(tmp_path, files) as store:
        assert _publisher_call_target(store).endswith(
            "/src/main/java/app/KafkaSink.java::KafkaSink.emit"
        )


@pytest.mark.parametrize(
    ("sink_source", "relation", "expected"),
    [
        (
            _SINK_INTERFACE,
            "implements",
            "src/test/java/app/RecordingSink.java::RecordingSink.emit",
        ),
        (_SINK_CONCRETE, "extends", _DECLARED),
    ],
    ids=["interface-uses-test-implementor", "concrete-keeps-declared-type"],
)
def test_test_source_caller_resolution(
    tmp_path: Path, sink_source: str, relation: str, expected: str,
):
    """Test code may be wired to a test double; a concrete type still stays itself."""
    files = {
        "src/main/java/app/Sink.java": sink_source,
        "src/test/java/app/RecordingSink.java": _implementor("RecordingSink", relation),
        "src/test/java/app/PublisherHarness.java": _HARNESS,
    }
    with _build_repo(tmp_path, files) as store:
        assert _call_target(store, "PublisherHarness.drive").endswith("/" + expected)


def test_two_production_implementors_keep_declared_type(tmp_path: Path):
    files = {
        "src/main/java/app/Publisher.java": _PUBLISHER,
        "src/main/java/app/Sink.java": _SINK_INTERFACE,
        "src/main/java/app/KafkaSink.java": _implementor("KafkaSink", "implements"),
        "src/main/java/app/LogSink.java": _implementor("LogSink", "implements"),
    }
    with _build_repo(tmp_path, files) as store:
        assert _publisher_call_target(store).endswith("/" + _DECLARED)


@pytest.mark.parametrize(
    ("subclass_path", "expected"),
    [
        (
            "src/main/java/app/RecordingSink.java",
            "src/main/java/app/RecordingSink.java::RecordingSink.emit",
        ),
        ("src/test/java/app/RecordingSink.java", _DECLARED),
    ],
    ids=["production-subclass", "test-subclass"],
)
def test_graph_without_java_kind_keeps_previous_rules(
    tmp_path: Path, subclass_path: str, expected: str,
):
    """Nodes written before ``java_kind`` existed fall back to unique-implementor
    substitution, but a production caller still never lands on test code."""
    files = [
        ("src/main/java/app/Sink.java", _SINK_CONCRETE),
        ("src/main/java/app/Publisher.java", _PUBLISHER),
        (subclass_path, _implementor("RecordingSink", "extends")),
    ]
    with _store_from_parsed(tmp_path, files, drop_java_kind=True) as store:
        extras = [
            json.loads(row["extra"] or "{}")
            for row in store._conn.execute("SELECT extra FROM nodes").fetchall()
        ]
        assert not [extra for extra in extras if "java_kind" in extra]
        assert resolve_spring_di_calls(store)["calls_resolved"] == 1
        assert _publisher_call_target(store) == expected


@pytest.mark.parametrize(
    "concrete_homonym_last",
    [
        pytest.param(False, id="homonym-first"),
        pytest.param(True, id="homonym-last"),
    ],
)
def test_interface_injection_ignores_concrete_homonym_in_another_package(
    tmp_path: Path, concrete_homonym_last: bool,
):
    """A same-named concrete class elsewhere must not cancel interface resolution.

    Types are keyed by simple name.  Before #1075 an interface with one
    implementor resolved to it whatever other ``Sink`` classes existed, and the
    answer must not now depend on which homonym happens to be stored last.
    """
    homonym = (
        "src/main/java/audit/Sink.java",
        "package audit;\npublic class Sink { public void emit(String event) { } }\n",
    )
    app_files = [
        ("src/main/java/app/Sink.java", _SINK_INTERFACE),
        ("src/main/java/app/KafkaSink.java", _implementor("KafkaSink", "implements")),
        ("src/main/java/app/Publisher.java", _PUBLISHER),
    ]
    files = [*app_files, homonym] if concrete_homonym_last else [homonym, *app_files]
    with _store_from_parsed(tmp_path, files) as store:
        resolve_spring_di_calls(store)
        assert _publisher_call_target(store) == (
            "src/main/java/app/KafkaSink.java::KafkaSink.emit"
        )


def test_interface_injection_uses_imported_declared_type(tmp_path: Path):
    files = {
        "src/main/java/app/Publisher.java": (
            "package app;\n"
            "import deps.Sink;\n"
            "import lombok.RequiredArgsConstructor;\n"
            "@RequiredArgsConstructor\n"
            "public class Publisher {\n"
            "    private final Sink sink;\n"
            "    public void publish(String event) { sink.emit(event); }\n"
            "}\n"
        ),
        "src/main/java/deps/Sink.java": _SINK_INTERFACE.replace(
            "package app;", "package deps;",
        ),
        "src/main/java/deps/KafkaSink.java": (
            "package deps;\n"
            "public class KafkaSink implements Sink {\n"
            "    public void emit(String event) { }\n"
            "}\n"
        ),
    }
    with _build_repo(tmp_path, files) as store:
        assert _publisher_call_target(store).endswith(
            "src/main/java/deps/KafkaSink.java::KafkaSink.emit"
        )


def test_resolver_rerun_and_rebuild_are_idempotent(tmp_path: Path):
    files = _concrete_files("src/test/java/app/RecordingSink.java")
    with _build_repo(tmp_path, files) as store:
        first = _publisher_call_target(store)
        assert first.endswith("/" + _DECLARED)
        assert resolve_spring_di_calls(store)["calls_resolved"] == 0
        assert _publisher_call_target(store) == first
        full_build(tmp_path, store)
        run_post_processing(store)
        assert _publisher_call_target(store) == first


def test_incremental_update_keeps_declared_type_when_test_subclass_appears(
    tmp_path: Path,
):
    files = _concrete_files("src/test/java/app/RecordingSink.java")
    subclass_source = files.pop("src/test/java/app/RecordingSink.java")
    with _build_repo(tmp_path, files) as store:
        assert _publisher_call_target(store).endswith("/" + _DECLARED)

        subclass = tmp_path / "src/test/java/app/RecordingSink.java"
        subclass.parent.mkdir(parents=True)
        subclass.write_text(subclass_source, encoding="utf-8")
        publisher = tmp_path / "src/main/java/app/Publisher.java"
        publisher.write_text(
            publisher.read_text(encoding="utf-8") + "// edited\n", encoding="utf-8",
        )
        incremental_update(
            tmp_path,
            store,
            changed_files=[
                "src/main/java/app/Publisher.java",
                "src/test/java/app/RecordingSink.java",
            ],
        )
        assert _publisher_call_target(store).endswith("/" + _DECLARED)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("@Component public abstract class Svc { }", "abstract"),
        ("public @Deprecated abstract class Svc { }", "abstract"),
        ("abstract class Svc<T extends Comparable<T>> { }", "abstract"),
        ("class Outer { public abstract static class Svc { } }", "abstract"),
        ("class Outer { interface Svc { } }", "interface"),
        ("sealed interface Svc permits Impl { } final class Impl implements Svc { }",
         "interface"),
        ("@FunctionalInterface interface Svc { void run(); }", "interface"),
        ("final class Svc { }", "concrete"),
        ("@Abstract class Svc { }", "concrete"),
        ("/** an abstract helper */ class Svc { }", "concrete"),
        ("enum Svc { A { int f() { return 1; } }; abstract int f(); }", "concrete"),
    ],
)
def test_java_kind_detection_variants(source: str, expected: str):
    nodes, _ = CodeParser().parse_bytes(Path("Svc.java"), source.encode("utf-8"))
    kinds = {node.name: node.extra.get("java_kind") for node in nodes if node.kind == "Class"}
    assert kinds["Svc"] == expected


def test_java_kind_is_only_recorded_for_java():
    nodes, _ = CodeParser().parse_bytes(
        Path("Base.kt"), b"abstract class Base { abstract fun f() }\ninterface Port\n",
    )
    classes = [node for node in nodes if node.kind == "Class"]
    assert {node.name for node in classes} == {"Base", "Port"}
    assert not [node for node in classes if "java_kind" in node.extra]
