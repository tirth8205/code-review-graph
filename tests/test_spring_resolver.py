"""Regression coverage for Spring DI declared-type resolution."""

from __future__ import annotations

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
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
