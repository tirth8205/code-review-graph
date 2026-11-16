"""Renaming a base class must rewrite every subclass declaration.

A subclass names its base in ``class Child(Base)``. That is one of the rename
sites, and missing it leaves the subclass pointing at a name that no longer
exists.
"""

from __future__ import annotations

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.refactor import apply_refactor, rename_preview
from code_review_graph.tools.build import build_or_update_graph

BASE = '''\
class Base:
    pass
'''

CHILD = '''\
from base import Base


class Child(Base):
    pass
'''


def test_renaming_a_base_rewrites_the_subclass_declaration(tmp_path):
    root = tmp_path.resolve()
    (root / ".code-review-graph").mkdir(parents=True, exist_ok=True)
    (root / "base.py").write_text(BASE, encoding="utf-8")
    (root / "child.py").write_text(CHILD, encoding="utf-8")
    result = build_or_update_graph(full_rebuild=True, repo_root=str(root))
    assert result["status"] == "ok", result
    store = GraphStore(get_db_path(root))
    try:
        preview = rename_preview(store, "Base", "Root")
        assert preview is not None
        sites = {(edit["file"], edit["line"]) for edit in preview["edits"]}
        assert (str(root / "child.py"), 4) in sites
        applied = apply_refactor(preview["refactor_id"], root)
        assert applied is not None
    finally:
        store.close()
    assert "class Child(Root)" in (root / "child.py").read_text(encoding="utf-8")


def test_renaming_a_base_rewrites_a_subclass_in_the_same_module(tmp_path):
    root = tmp_path.resolve()
    (root / ".code-review-graph").mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text(
        "class Base:\n    pass\n\n\nclass Child(Base):\n    pass\n",
        encoding="utf-8",
    )
    result = build_or_update_graph(full_rebuild=True, repo_root=str(root))
    assert result["status"] == "ok", result
    store = GraphStore(get_db_path(root))
    try:
        preview = rename_preview(store, "Base", "Root")
        assert preview is not None
        lines = {edit["line"] for edit in preview["edits"]}
        assert 5 in lines
        assert apply_refactor(preview["refactor_id"], root) is not None
    finally:
        store.close()
    assert "class Child(Root)" in (root / "app.py").read_text(encoding="utf-8")
