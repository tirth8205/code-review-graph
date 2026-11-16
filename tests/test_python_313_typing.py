"""Python 3.13 typing constructs name the types they use.

A PEP 695 alias, a generic parameter bound and a forward-reference string are
type uses: the model or class they name must not look dead. FLEXT writes models,
protocols and facades with exactly these shapes.
"""

from __future__ import annotations

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.refactor import find_dead_code
from code_review_graph.tools.build import build_or_update_graph

SOURCE = '''\
from typing import TypeVar

T = TypeVar("T")


class Base:
    pass


class Model:
    value: int = 0


type Alias = dict[str, Model]


class Box[T: Base]:
    item: Model


def forward() -> "Model":
    return Model()


def generic_bound(value: T) -> None:
    ...
'''


def test_python_313_typing_constructs_count_as_uses(tmp_path):
    root = tmp_path.resolve()
    (root / ".code-review-graph").mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text(SOURCE, encoding="utf-8")
    result = build_or_update_graph(full_rebuild=True, repo_root=str(root))
    assert result["status"] == "ok", result
    with GraphStore(get_db_path(root)) as store:
        dead = {row["name"] for row in find_dead_code(store, root=root)}
    assert "Model" not in dead
    assert "Base" not in dead


def test_literal_strings_are_values_not_type_uses(tmp_path):
    root = tmp_path.resolve()
    (root / ".code-review-graph").mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text(
        '''\
from typing import Literal


class Open:
    pass


class State:
    mode: Literal["Open"] = "Open"
''',
        encoding="utf-8",
    )
    result = build_or_update_graph(full_rebuild=True, repo_root=str(root))
    assert result["status"] == "ok", result
    with GraphStore(get_db_path(root)) as store:
        dead = {row["name"] for row in find_dead_code(store, root=root)}
    assert "Open" in dead
