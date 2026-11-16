"""Pydantic models and their fields are used through the library, not calls.

A model declared in a module and constructed elsewhere has no call-shaped
caller for its fields; ``Model(field=value)`` names the field in a keyword
argument and ``obj.field`` reads it as an attribute. Reporting the model or its
fields as dead sends a reviewer to delete a live contract.
"""

from __future__ import annotations

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.refactor import find_dead_code
from code_review_graph.tools.build import build_or_update_graph

MODEL_SOURCE = '''\
from pydantic import BaseModel, Field


class Account(BaseModel):
    name: str
    balance: float = Field(default=0.0, ge=0)
    currency: str = "BRL"

    def label(self) -> str:
        return f"{self.name} ({self.currency})"
'''

USE_SOURCE = '''\
from app import Account


def describe() -> str:
    account = Account(name="Ada", balance=1.0)
    return account.label()
'''


def _build(root: Path, files: dict[str, str]) -> GraphStore:
    root = root.resolve()
    (root / ".code-review-graph").mkdir(parents=True, exist_ok=True)
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    result = build_or_update_graph(full_rebuild=True, repo_root=str(root))
    assert result["status"] == "ok", result
    return GraphStore(get_db_path(root))


def test_a_model_constructed_elsewhere_is_not_dead(tmp_path):
    store = _build(tmp_path, {"app.py": MODEL_SOURCE, "use.py": USE_SOURCE})
    try:
        dead = {row["name"] for row in find_dead_code(store, root=tmp_path)}
        assert "Account" not in dead
    finally:
        store.close()


def test_a_method_used_on_a_constructed_model_is_not_dead(tmp_path):
    store = _build(tmp_path, {"app.py": MODEL_SOURCE, "use.py": USE_SOURCE})
    try:
        dead = {row["name"] for row in find_dead_code(store, root=tmp_path)}
        assert "label" not in dead
    finally:
        store.close()


def test_a_lazily_exported_member_is_not_dead(tmp_path):
    store = _build(tmp_path, {
        "__init__.py": '''\
from flext_core.lazy import install_lazy_exports

install_lazy_exports(
    __name__,
    globals(),
    {"Facade": ".impl"},
    deferred_members={"Facade": ("Member", "Other")},
    public_exports=("Facade",),
)
''',
        "impl.py": '''\
class Facade:
    class Member:
        value: int = 0

    class Other:
        value: int = 1
''',
    })
    try:
        dead = {row["name"] for row in find_dead_code(store, root=tmp_path)}
        assert "Member" not in dead
        assert "Other" not in dead
    finally:
        store.close()
