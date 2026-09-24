"""Python property reads retain their getter and declared value type."""

from pathlib import Path

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
