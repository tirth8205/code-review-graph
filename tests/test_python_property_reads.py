"""Python property reads retain their getter and declared value type."""

from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser


def _parse_repo(root: Path, database: Path) -> GraphStore:
    store = GraphStore(database)
    parser = CodeParser(repo_root=root)
    for path in sorted(root.rglob("*.py")):
        nodes, edges = parser.parse_file(path)
        for node in nodes:
            store.upsert_node(node)
        for edge in edges:
            store.upsert_edge(edge)
    store.commit()
    store.resolve_bare_call_targets()
    return store


def test_property_read_and_return_type_resolve_to_the_declared_classes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    engine = root / "engine.py"
    engine.write_text(
        "class Engine:\n"
        "    def run(self) -> str:\n"
        "        return 'engine'\n\n"
        "class OtherEngine:\n"
        "    def run(self) -> str:\n"
        "        return 'other'\n\n"
        "class Orchestrator:\n"
        "    @property\n"
        "    def secondary_engine(self) -> Engine:\n"
        "        return Engine()\n\n"
        "class OtherOrchestrator:\n"
        "    @property\n"
        "    def secondary_engine(self) -> OtherEngine:\n"
        "        return OtherEngine()\n",
        encoding="utf-8",
    )
    consumer = root / "consumer.py"
    consumer.write_text(
        "from engine import Orchestrator, OtherOrchestrator\n\n"
        "class Holder:\n"
        "    def __init__(self) -> None:\n"
        "        self.orch = Orchestrator()\n"
        "        self.other = OtherOrchestrator()\n\n"
        "    def reads_property(self) -> str:\n"
        "        engine = self.orch.secondary_engine\n"
        "        return engine.run()\n\n"
        "    def reads_other_property(self) -> str:\n"
        "        engine = self.other.secondary_engine\n"
        "        return engine.run()\n\n"
        "    def reads_unknown(self, factory) -> str:\n"
        "        engine = factory.secondary_engine\n"
        "        return engine.run()\n",
        encoding="utf-8",
    )

    store = _parse_repo(root, tmp_path / "graph.db")
    try:
        rows = store._conn.execute(
            "SELECT source_qualified, target_qualified "
            "FROM edges WHERE kind = 'CALLS' AND file_path = ?",
            (consumer.resolve().as_posix(),),
        ).fetchall()
        calls = {
            (row["source_qualified"], row["target_qualified"])
            for row in rows
        }

        assert (
            f"{consumer.resolve().as_posix()}::Holder.reads_property",
            f"{engine.resolve().as_posix()}::Orchestrator.secondary_engine",
        ) in calls
        assert (
            f"{consumer.resolve().as_posix()}::Holder.reads_property",
            f"{engine.resolve().as_posix()}::Engine.run",
        ) in calls
        assert (
            f"{consumer.resolve().as_posix()}::Holder.reads_other_property",
            f"{engine.resolve().as_posix()}::OtherOrchestrator.secondary_engine",
        ) in calls
        assert (
            f"{consumer.resolve().as_posix()}::Holder.reads_other_property",
            f"{engine.resolve().as_posix()}::OtherEngine.run",
        ) in calls
        assert (
            f"{consumer.resolve().as_posix()}::Holder.reads_unknown",
            "run",
        ) in calls
        assert not any(
            source.endswith("::Holder.reads_unknown")
            and target.endswith(("::Engine.run", "::OtherEngine.run"))
            for source, target in calls
        )
    finally:
        store.close()


def test_property_reads_require_an_instance_method_receiver(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    module = root / "models.py"
    module.write_text(
        "from __future__ import annotations\n\n"
        "class Component:\n"
        "    def run(self) -> int:\n"
        "        return 1\n\n"
        "class C:\n"
        "    @property\n"
        "    def value(self) -> int:\n"
        "        return 1\n\n"
        "    @property\n"
        "    def component(self) -> Component:\n"
        "        return Component()\n\n"
        "    @classmethod\n"
        "    def read_through_class(cls) -> object:\n"
        "        return cls.value\n\n"
        "    @classmethod\n"
        "    def read_through_annotated_class(cls: C) -> object:\n"
        "        component = cls.component\n"
        "        return component.run()\n\n"
        "    @staticmethod\n"
        "    def read_through_static_parameter(self) -> object:\n"
        "        return self.value\n\n"
        "    def read_through_instance(self) -> int:\n"
        "        return self.value\n",
        encoding="utf-8",
    )

    store = _parse_repo(root, tmp_path / "graph.db")
    try:
        rows = store._conn.execute(
            "SELECT source_qualified, target_qualified "
            "FROM edges WHERE kind = 'CALLS' AND file_path = ?",
            (module.resolve().as_posix(),),
        ).fetchall()
        calls = {
            (row["source_qualified"], row["target_qualified"])
            for row in rows
        }
        target = f"{module.resolve().as_posix()}::C.value"
        component = f"{module.resolve().as_posix()}::C.component"
        component_run = f"{module.resolve().as_posix()}::Component.run"

        assert (
            f"{module.resolve().as_posix()}::C.read_through_instance",
            target,
        ) in calls
        assert (
            f"{module.resolve().as_posix()}::C.read_through_class",
            target,
        ) not in calls
        assert (
            f"{module.resolve().as_posix()}::C.read_through_static_parameter",
            target,
        ) not in calls
        annotated_class_reader = (
            f"{module.resolve().as_posix()}::C.read_through_annotated_class"
        )
        assert (annotated_class_reader, component) not in calls
        assert (annotated_class_reader, component_run) not in calls
    finally:
        store.close()


def test_property_store_targets_are_not_reads_but_augmented_assignment_is(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    module = root / "models.py"
    source = (
        "class C:\n"
        "    @property\n"
        "    def value(self) -> int:\n"
        "        return 1\n\n"
        "    def update(self) -> None:\n"
        "        self.value = 2\n"
        "        del self.value\n"
        "        self.value, other = 2, 3\n"
        "        for self.value in range(3):\n"
        "            pass\n"
        "        self.value += 1\n"
    )
    module.write_text(source, encoding="utf-8")

    store = _parse_repo(root, tmp_path / "graph.db")
    try:
        rows = store._conn.execute(
            "SELECT source_qualified, target_qualified, line "
            "FROM edges WHERE kind = 'CALLS' AND file_path = ?",
            (module.resolve().as_posix(),),
        ).fetchall()
        target = f"{module.resolve().as_posix()}::C.value"
        update = f"{module.resolve().as_posix()}::C.update"
        property_read_lines = {
            row["line"]
            for row in rows
            if row["source_qualified"] == update
            and row["target_qualified"] == target
        }
        augmented_assignment_line = source.splitlines().index(
            "        self.value += 1",
        ) + 1

        assert property_read_lines == {augmented_assignment_line}
    finally:
        store.close()


def test_property_reads_skip_ambiguous_same_named_classes(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    module = root / "models.py"
    module.write_text(
        "class C:\n"
        "    @property\n"
        "    def value(self) -> str:\n"
        "        return 'top'\n\n"
        "    def read_top(self) -> str:\n"
        "        return self.value\n\n"
        "class Outer:\n"
        "    class C:\n"
        "        @property\n"
        "        def value(self) -> int:\n"
        "            return 1\n\n"
        "        def read_nested(self) -> int:\n"
        "            return self.value\n",
        encoding="utf-8",
    )

    _, edges = CodeParser(repo_root=root).parse_file(module)

    property_reads = [
        edge
        for edge in edges
        if edge.extra and edge.extra.get("python_property_read")
    ]
    assert property_reads == []


GETTER = (
    "class C:\n"
    "    @property\n"
    "    def value(self) -> int:\n"
    "        return 1\n\n"
)


def _targets(root: Path, tmp_path: Path, caller: str, file_name: str = "models.py") -> set[str]:
    store = _parse_repo(root, tmp_path / "graph.db")
    try:
        path = (root / file_name).resolve().as_posix()
        rows = store._conn.execute(
            "SELECT target_qualified FROM edges "
            "WHERE kind = 'CALLS' AND source_qualified = ?",
            (f"{path}::{caller}",),
        ).fetchall()
        return {row["target_qualified"].rsplit("/", 1)[-1] for row in rows}
    finally:
        store.close()


def _repo(tmp_path: Path, **files: str) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for name, source in files.items():
        (root / f"{name}.py").write_text(source, encoding="utf-8")
    return root


@pytest.mark.parametrize(
    "body",
    [
        "        with mgr() as self.value:\n            pass\n",
        "        return [None for self.value in mgr]\n",
    ],
    ids=["with-as", "comprehension"],
)
def test_binding_forms_store_to_the_property_without_reading_it(tmp_path, body):
    root = _repo(tmp_path, models=GETTER + "    def m(self, mgr):\n" + body)
    assert "models.py::C.value" not in _targets(root, tmp_path, "C.m")


@pytest.mark.parametrize(
    "body",
    [
        "        with self.value as other:\n            pass\n",
        "        return [None for item in self.value]\n",
    ],
    ids=["with-value", "comprehension-iterable"],
)
def test_values_of_binding_forms_are_still_reads(tmp_path, body):
    root = _repo(tmp_path, models=GETTER + "    def m(self):\n" + body)
    assert "models.py::C.value" in _targets(root, tmp_path, "C.m")


def test_closure_reads_through_the_captured_receiver(tmp_path):
    root = _repo(
        tmp_path,
        models=GETTER
        + "    def m(self):\n"
        + "        def callback():\n            return self.value\n"
        + "        return callback\n",
    )
    assert "models.py::C.value" in _targets(root, tmp_path, "C.callback")


def test_closure_parameter_hides_the_receiver(tmp_path):
    root = _repo(
        tmp_path,
        models=GETTER
        + "    def m(self):\n"
        + "        def callback(self):\n            return self.value\n"
        + "        return callback\n",
    )
    assert "models.py::C.value" not in _targets(root, tmp_path, "C.callback")


@pytest.mark.parametrize(
    "guard, header",
    [("False", ""), ("TYPE_CHECKING", "from typing import TYPE_CHECKING\n")],
    ids=["false", "type-checking"],
)
def test_unreachable_read_is_not_a_getter_call(tmp_path, guard, header):
    root = _repo(
        tmp_path,
        models=header + GETTER
        + f"    def dead(self):\n        if {guard}:\n            return self.value\n"
        + "    def live(self):\n"
        + f"        if {guard}:\n            pass\n        return self.value\n",
    )
    assert "models.py::C.value" not in _targets(root, tmp_path, "C.dead")
    assert "models.py::C.value" in _targets(root, tmp_path, "C.live")


def test_name_imported_from_two_modules_gives_no_getter_edge(tmp_path):
    root = _repo(
        tmp_path,
        a=GETTER,
        b=GETTER,
        models=(
            "def f():\n    from a import C\n    c = C()\n    return c.value\n"
            "def g():\n    from b import C\n    c = C()\n    return c.value\n"
        ),
    )
    for caller in ("f", "g"):
        targets = _targets(root, tmp_path, caller)
        assert "a.py::C.value" not in targets
        assert "b.py::C.value" not in targets


def test_parameter_does_not_inherit_a_field_of_the_same_name(tmp_path):
    root = _repo(
        tmp_path,
        models=GETTER
        + "class Holder:\n"
        + "    def __init__(self):\n        self.orch = C()\n"
        + "    def unknown(self, orch):\n        return orch.value\n"
        + "    def typed(self, orch: C):\n        return orch.value\n"
        + "    def field(self):\n        return self.orch.value\n",
    )
    assert "models.py::C.value" not in _targets(root, tmp_path, "Holder.unknown")
    assert "models.py::C.value" in _targets(root, tmp_path, "Holder.typed")
    assert "models.py::C.value" in _targets(root, tmp_path, "Holder.field")


def test_return_type_from_another_module_is_not_a_same_named_local_class(tmp_path):
    root = _repo(
        tmp_path,
        a=(
            "class Engine:\n    @property\n    def token(self) -> int:\n        return 1\n\n"
            "class Orch:\n    @property\n    def engine(self) -> Engine:\n"
            "        return Engine()\n"
        ),
        models=(
            "from a import Orch\n\n"
            "class Engine:\n    @property\n    def token(self) -> int:\n        return 2\n\n"
            "def f(o: Orch):\n    v = o.engine\n    return v.token\n"
        ),
    )
    targets = _targets(root, tmp_path, "f")
    assert "a.py::Orch.engine" in targets
    assert "models.py::Engine.token" not in targets


def test_return_type_imported_from_the_getters_module_is_kept(tmp_path):
    root = _repo(
        tmp_path,
        a=(
            "class Engine:\n    @property\n    def token(self) -> int:\n        return 1\n\n"
            "class Orch:\n    @property\n    def engine(self) -> Engine:\n"
            "        return Engine()\n"
        ),
        models=(
            "from a import Engine, Orch\n\n"
            "def f(o: Orch):\n    v = o.engine\n    return v.token\n"
        ),
    )
    assert "a.py::Engine.token" in _targets(root, tmp_path, "f")


def test_imported_module_too_deep_to_walk_keeps_the_callers_edges(tmp_path):
    root = _repo(
        tmp_path,
        a=GETTER + "x = " + "+".join(["1"] * 60000) + "\n",
        models="from a import C\n\ndef f():\n    c = C()\n    helper()\n    return c.value\n",
    )
    assert "helper" in {
        edge.target
        for edge in CodeParser(repo_root=root).parse_file(root / "models.py")[1]
        if edge.kind == "CALLS"
    }
