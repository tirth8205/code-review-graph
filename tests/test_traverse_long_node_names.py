"""traverse_graph starts from the node search found, however long its name.

Search results cut a qualified name at 256 characters before they leave the
tool. The walk looked the start node up by that cut name, found nothing, and
returned an empty traversal. See: crg-hi3
"""

from __future__ import annotations

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.tools import query

LONG = "start_" + "x" * 300


def test_traverse_walks_from_a_node_whose_name_is_over_256_characters(tmp_path):
    root = tmp_path.resolve()
    (root / ".git").mkdir()
    (root / "long_names.py").write_text(
        f"def {LONG}():\n"
        "    next_step()\n"
        "\n"
        "def next_step():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    store = GraphStore(root / ".code-review-graph" / "graph.db")
    try:
        full_build(root, store)
    finally:
        store.close()

    result = query.traverse_graph_func(
        LONG, depth=2, token_budget=10_000, repo_root=str(root),
    )

    names = [entry["name"] for entry in result["traversal"]]
    assert "next_step" in names
    assert result["nodes_visited"] >= 2
