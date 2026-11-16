"""C3 decides which ancestor a call runs through a diamond or a mixin.

The index used to give up on any class with more than one parent and answer only
when exactly one ancestor declared the method. A diamond with two declarations
returned nothing, so a call made through the subclass lost its target and the
method behind it looked dead.
"""

from __future__ import annotations

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.inheritance import InheritanceIndex
from code_review_graph.tools.build import build_or_update_graph

DIAMOND = '''\
class A:
    def run(self) -> str:
        return "a"


class B(A):
    pass


class C(A):
    pass


class D(B, C):
    pass
'''

PRECEDENCE = '''\
class A:
    def run(self) -> str:
        return "a"


class B(A):
    def run(self) -> str:
        return "b"


class C(A):
    def run(self) -> str:
        return "c"


class D(B, C):
    pass
'''

EXTERNAL = '''\
from somewhere import External


class A:
    def run(self) -> str:
        return "a"


class D(External, A):
    pass
'''

CYCLE = '''\
class A(B):
    pass


class B(A):
    pass
'''


def _index(tmp_path: Path, source: str) -> tuple[GraphStore, InheritanceIndex]:
    root = tmp_path.resolve()
    (root / ".code-review-graph").mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text(source, encoding="utf-8")
    result = build_or_update_graph(full_rebuild=True, repo_root=str(root))
    assert result["status"] == "ok", result
    store = GraphStore(get_db_path(root))
    return store, InheritanceIndex(store._conn, {}, {})


def _class(index: InheritanceIndex, root: Path, name: str) -> str:
    return f"{root / 'app.py'}::{name}"


def test_a_diamond_resolves_through_c3(tmp_path):
    store, index = _index(tmp_path, DIAMOND)
    try:
        root = tmp_path.resolve()
        d, b, c, a = (
            _class(index, root, name) for name in ("D", "B", "C", "A")
        )
        assert index.linearization(d) == (d, b, c, a)
        assert index.inherited_method(d, "run") == index.method_of(a, "run")
    finally:
        store.close()


def test_the_first_parent_wins_when_both_define_the_method(tmp_path):
    store, index = _index(tmp_path, PRECEDENCE)
    try:
        root = tmp_path.resolve()
        d, b = _class(index, root, "D"), _class(index, root, "B")
        assert index.inherited_method(d, "run") == index.method_of(b, "run")
    finally:
        store.close()


def test_an_external_parent_keeps_the_conservative_answer(tmp_path):
    store, index = _index(tmp_path, EXTERNAL)
    try:
        root = tmp_path.resolve()
        assert index.linearization(_class(index, root, "D")) == ()
    finally:
        store.close()


def test_an_inheritance_cycle_terminates(tmp_path):
    store, index = _index(tmp_path, CYCLE)
    try:
        root = tmp_path.resolve()
        assert index.linearization(_class(index, root, "A")) == ()
    finally:
        store.close()
