"""Regression coverage for Spring DI declared-type resolution."""

from __future__ import annotations

import json
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
        target = _publisher_call_target(store)
        assert target.endswith("::Sink.emit")
        assert "RecordingSink" not in target

        inject = store._conn.execute(
            "SELECT target_qualified, extra FROM edges WHERE kind = 'INJECTS'"
        ).fetchone()
        assert inject["target_qualified"] == "Sink"
        assert json.loads(inject["extra"])["field_name"] == "sink"


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


def test_ambiguous_declared_type_does_not_choose_an_arbitrary_subclass(
    tmp_path: Path,
):
    files = _concrete_files("src/main/java/app/RecordingSink.java")
    files["src/main/java/other/Sink.java"] = (
        "package other;\n"
        "public class Sink { public void emit(String event) { } }\n"
    )
    with _build_repo(tmp_path, files) as store:
        target = _publisher_call_target(store)
        assert target.endswith("Sink.emit")
        assert "RecordingSink" not in target


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


def test_legacy_interface_without_java_kind_uses_declared_type_fallback(
    tmp_path: Path,
):
    """Legacy Java interfaces without java_kind use the declared type fallback."""
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
        store._conn.execute(
            "UPDATE nodes SET extra = '{}' WHERE name = 'Sink' AND language = 'java'"
        )
        legacy_node = store._conn.execute(
            "SELECT extra FROM nodes WHERE name = 'Sink' AND language = 'java'"
        ).fetchone()
        assert json.loads(legacy_node["extra"]).get("java_kind") is None
        row = store._conn.execute(
            "SELECT id, extra FROM edges "
            "WHERE kind = 'CALLS' AND source_qualified LIKE '%::Publisher.publish'"
        ).fetchone()
        call_extra = json.loads(row["extra"])
        call_extra.pop("spring_resolved", None)
        call_extra.pop("injected_type", None)
        store._conn.execute(
            "UPDATE edges SET target_qualified = ?, extra = ? WHERE id = ?",
            ("emit", json.dumps(call_extra), row["id"]),
        )
        store.commit()

        result = resolve_spring_di_calls(store)
        assert result["calls_resolved"] == 1
        target = _publisher_call_target(store)
        assert target.endswith("::Sink.emit")
        assert "RecordingSink" not in target


def test_legacy_concrete_without_java_kind_never_substitutes_test_subclass(
    tmp_path: Path,
):
    """Legacy concrete Sink metadata must not select its production subclass."""
    with _build_repo(
        tmp_path,
        _concrete_files("src/main/java/app/RecordingSink.java"),
    ) as store:
        store._conn.execute(
            "UPDATE nodes SET extra = '{}' WHERE name = 'Sink' AND language = 'java'"
        )
        row = store._conn.execute(
            "SELECT id, target_qualified, extra FROM edges "
            "WHERE kind = 'CALLS' AND source_qualified LIKE '%::Publisher.publish'"
        ).fetchone()
        original_extra = json.loads(row["extra"])
        call_extra = json.loads(row["extra"])
        call_extra.pop("spring_resolved", None)
        call_extra.pop("injected_type", None)
        store._conn.execute(
            "UPDATE edges SET target_qualified = ?, extra = ? WHERE id = ?",
            ("emit", json.dumps(call_extra), row["id"]),
        )
        store.commit()

        reset = store._conn.execute(
            "SELECT target_qualified, extra FROM edges WHERE id = ?", (row["id"],)
        ).fetchone()
        assert reset["target_qualified"] == "emit"
        assert json.loads(reset["extra"]) == {
            key: value
            for key, value in original_extra.items()
            if key not in {"spring_resolved", "injected_type"}
        }

        result = resolve_spring_di_calls(store)
        assert result["calls_resolved"] == 1
        target = _publisher_call_target(store)
        assert target.endswith("::Sink.emit")
        assert "RecordingSink" not in target


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
        assert "RecordingSink" not in _publisher_call_target(store)


def test_test_caller_can_resolve_to_test_implementor(tmp_path: Path):
    files = {
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
        "src/test/java/app/PublisherTest.java": (
            "package app;\n"
            "import lombok.RequiredArgsConstructor;\n"
            "@RequiredArgsConstructor\n"
            "public class PublisherTest {\n"
            "    private final Sink sink;\n"
            "    public void publish(String event) { sink.emit(event); }\n"
            "}\n"
        ),
    }
    with _build_repo(tmp_path, files) as store:
        rows = store._conn.execute(
            "SELECT target_qualified FROM edges "
            "WHERE kind = 'CALLS' AND source_qualified LIKE '%::PublisherTest.publish'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["target_qualified"].endswith("::RecordingSink.emit")


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


def test_java_caller_resolves_kotlin_interface_to_unique_kotlin_implementation(
    tmp_path: Path,
):
    files = {
        "src/main/java/app/Client.java": (
            "package app;\n"
            "import org.springframework.beans.factory.annotation.Autowired;\n"
            "public class Client {\n"
            "    @Autowired KotlinContract dep;\n"
            "    public void run() { dep.work(); }\n"
            "}\n"
        ),
        "src/main/kotlin/app/KotlinContract.kt": (
            "package app\n"
            "interface KotlinContract { fun work() }\n"
        ),
        "src/main/kotlin/app/KotlinImpl.kt": (
            "package app\n"
            "class KotlinImpl : KotlinContract { override fun work() {} }\n"
        ),
    }
    with _build_repo(tmp_path, files) as store:
        target = _call_target(store, "Client.run")
        assert target.endswith("KotlinImpl.work")


def test_java_caller_resolves_scala_trait_to_unique_scala_implementation(
    tmp_path: Path,
):
    files = {
        "src/main/java/app/Client.java": (
            "package app;\n"
            "import org.springframework.beans.factory.annotation.Autowired;\n"
            "public class Client {\n"
            "    @Autowired ScalaContract dep;\n"
            "    public void run() { dep.work(); }\n"
            "}\n"
        ),
        "src/main/scala/app/ScalaContract.scala": (
            "package app\n"
            "trait ScalaContract { def work(): Unit }\n"
        ),
        "src/main/scala/app/ScalaImpl.scala": (
            "package app\n"
            "class ScalaImpl extends ScalaContract { def work(): Unit = {} }\n"
        ),
    }
    with _build_repo(tmp_path, files) as store:
        target = _call_target(store, "Client.run")
        assert target.endswith("ScalaImpl.work")


def test_nested_test_configuration_uses_declared_qualified_method_fallback(
    tmp_path: Path,
):
    files = {
        "src/test/java/app/RecordingSinkConfig.java": (
            "package app;\n"
            "import org.springframework.beans.factory.annotation.Autowired;\n"
            "import org.springframework.boot.test.context.TestConfiguration;\n"
            "public class Client {\n"
            "    @Autowired RecordingSink dep;\n"
            "    public void run() { dep.work(); }\n"
            "}\n"
            "@TestConfiguration\n"
            "class RecordingSinkConfig {\n"
            "    public static final class RecordingSink { public void work() {} }\n"
            "}\n"
        ),
    }
    with _build_repo(tmp_path, files) as store:
        recording_sink = store._conn.execute(
            "SELECT qualified_name FROM nodes WHERE name = 'RecordingSink' AND kind = 'Class'"
        ).fetchone()
        method = store._conn.execute(
            "SELECT qualified_name FROM nodes "
            "WHERE name = 'work' AND parent_name = 'RecordingSink'"
        ).fetchone()
        expected_class = (
            f"{tmp_path.as_posix()}/src/test/java/app/RecordingSinkConfig.java"
            "::RecordingSinkConfig.RecordingSink"
        )
        expected_method = (
            f"{tmp_path.as_posix()}/src/test/java/app/RecordingSinkConfig.java::RecordingSink.work"
        )
        assert recording_sink["qualified_name"] == expected_class
        assert method["qualified_name"] == expected_method
        assert _call_target(store, "Client.run") == expected_method
