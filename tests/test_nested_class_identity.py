"""A nested class and its methods share one scope chain.

FLEXT nests every component class inside a facade class. The parser kept the
legacy leaf spelling for Python class bodies, so a method of ``Outer.Inner`` was
keyed ``file::Inner.method`` while its container was ``file::Outer.Inner``: the
class had no methods, rename missed them, and a protocol method could not be
linked to the protocol that declares it.
"""

from __future__ import annotations

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.parser import CodeParser
from code_review_graph.tools.build import build_or_update_graph

SOURCE = '''\
class Facade:
    class OutputStream:
        def isatty(self) -> bool:
            return False

    class Logger:
        def emit(self, msg: str) -> None:
            ...
'''


def test_a_nested_class_method_carries_the_full_scope(tmp_path):
    path = tmp_path / "app.py"
    path.write_text(SOURCE, encoding="utf-8")
    nodes, _ = CodeParser().parse_file(path)
    methods = {n.name: n for n in nodes if n.name in ("isatty", "emit")}
    assert methods["isatty"].parent_name == "Facade.OutputStream"
    assert methods["emit"].parent_name == "Facade.Logger"


def test_the_class_node_owns_its_methods_in_a_built_graph(tmp_path):
    root = tmp_path.resolve()
    (root / ".code-review-graph").mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text(SOURCE, encoding="utf-8")
    result = build_or_update_graph(full_rebuild=True, repo_root=str(root))
    assert result["status"] == "ok", result
    with GraphStore(get_db_path(root)) as store:
        containers = [
            row["target_qualified"]
            for row in store._conn.execute(
                "SELECT target_qualified FROM edges WHERE kind = 'CONTAINS'"
            ).fetchall()
        ]
    assert any(target.endswith("::Facade.OutputStream.isatty") for target in containers)
    assert any(target.endswith("::Facade.Logger.emit") for target in containers)
