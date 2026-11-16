"""A playbook task, an HCL resource or a SQL statement is not dead code.

They have no call site because an orchestrator, a provider or a query engine
runs them. Reporting them sends a reviewer to delete infrastructure.
"""

from __future__ import annotations

from code_review_graph.graph import GraphStore
from code_review_graph.parser import NodeInfo
from code_review_graph.refactor import find_dead_code


def _node(tmp_path, name: str, language: str) -> NodeInfo:
    return NodeInfo(
        kind="Function",
        name=name,
        file_path=str(tmp_path / f"{name}.{language}"),
        line_start=1,
        line_end=2,
        language=language,
    )


def test_infrastructure_documents_are_not_dead_code_subjects(tmp_path):
    with GraphStore(tmp_path / "graph.db") as store:
        for name, language in (
            ("ensure_package", "ansible"),
            ("aws_instance", "hcl"),
            ("select_orders", "sql"),
        ):
            store.upsert_node(_node(tmp_path, name, language))
        store.upsert_node(_node(tmp_path, "orphan_python", "python"))
        store.commit()
        names = {row["name"] for row in find_dead_code(store, root=tmp_path)}
    assert names == {"orphan_python"}
