"""Binding forms the receiver-evidence reader has to understand.

The receiver-evidence filter is only as good as the reader that feeds it: a
binding form the reader cannot spell leaves the receiver ``unknown``, and an
``unknown`` receiver attributes nothing. That is the right default when the
file really says nothing, and a silent recall hole when the file says it
plainly in a shape the reader skipped.

Four shapes were skipped, and each one is a common way to write Python:

``store, root = _get_store()``
    the target is a pattern, not a name, so the binding was dropped whole —
    even though ``_get_store`` is annotated ``-> tuple[GraphStore, Path]``
    and position 0 of that annotation is a ``GraphStore``.
``code_review_graph.graph.GraphStore(...)``
    a constructor reached through a dotted module path, which the reader
    only recognised when the class was spelled as a bare name.
``CodeParser().parse_file(...)``
    a call on a constructor expression: no receiver NAME exists, but the
    class is written right there in the expression.
``s = _local_factory()``
    a module-local ``def`` with a return annotation. The annotation is the
    file speaking; only a return type read out of a function BODY would be
    inference, and none is read here.

Every assertion goes through ``query_graph`` — the entry point the MCP tool
calls — and names exact targets, because the point of the change is which
node a query answers with, not what the store happens to hold.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import CodeParser
from code_review_graph.postprocessing import run_post_processing
from code_review_graph.tools import query_graph

_LIB = '''\
from pathlib import Path


class GraphStore:
    """The class every shape in this fixture has to reach."""

    def __init__(self, db_path):
        self.db_path = db_path

    def close(self):
        return self.db_path

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Rival:
    """A decoy: same method name, different class."""

    def close(self):
        return None


class CodeParser:
    def parse_file(self, path):
        return path
'''

_COMMON = '''\
from pathlib import Path

from .lib import GraphStore


def get_store(repo_root=None) -> tuple[GraphStore, Path]:
    return GraphStore(repo_root), Path(".")


def get_pair() -> tuple[GraphStore, tuple[GraphStore, Path]]:
    return GraphStore(None), (GraphStore(None), Path("."))


def get_many() -> tuple[GraphStore, ...]:
    return (GraphStore(None),)


def unannotated_factory():
    return GraphStore(None)
'''

_USE = '''\
import pkg.lib
from pkg import lib

from ._common import get_many, get_pair, get_store, unannotated_factory
from .lib import CodeParser, GraphStore


def tuple_unpack(repo_root):
    store, root = get_store(repo_root)
    store.close()


def nested_unpack():
    first, (second, third) = get_pair()
    first.close()
    second.close()


def starred_unpack():
    head, *rest = get_many()
    head.close()


def starred_tail_is_given_up(repo_root):
    _first, *tail_pair = get_store(repo_root)
    for item in tail_pair:
        item.close()


def dotted_constructor(path):
    store = pkg.lib.GraphStore(path)
    store.close()


def module_attribute_constructor(path):
    store = lib.GraphStore(path)
    store.close()


def dotted_constructor_in_with(path):
    with pkg.lib.GraphStore(path) as store:
        store.close()


def expression_receiver(path):
    CodeParser().parse_file(path)


def constructor_expression_receiver(path):
    GraphStore(path).close()


def with_tuple_alias(repo_root):
    with get_store(repo_root) as (store, root):
        store.close()


def local_annotated_factory(path):
    store = _local_factory(path)
    store.close()


def _local_factory(path) -> GraphStore:
    return GraphStore(path)


def unannotated_factory_says_nothing():
    store = unannotated_factory()
    store.close()


def no_evidence_at_all(anything):
    anything.close()
'''


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("shapes")
    package = root / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "lib.py").write_text(_LIB, encoding="utf-8")
    (package / "_common.py").write_text(_COMMON, encoding="utf-8")
    (package / "use.py").write_text(_USE, encoding="utf-8")
    db_path = root / ".code-review-graph" / "graph.db"
    store = GraphStore(db_path)
    try:
        full_build(root, store)
        run_post_processing(store)
    finally:
        store.close()
    return root


def _qn(repo_root: Path, relative: str, symbol: str) -> str:
    return f"{(repo_root / relative).resolve().as_posix()}::{symbol}"


def _callers(repo_root: Path, qualified_name: str) -> set[str]:
    """``callers_of`` through the tool, as the set of caller names."""
    minimal = query_graph(
        pattern="callers_of", target=qualified_name,
        repo_root=str(repo_root), detail_level="minimal", max_results=500,
    )
    assert minimal["status"] == "ok", minimal
    full = query_graph(
        pattern="callers_of", target=qualified_name,
        repo_root=str(repo_root), detail_level="standard", max_results=500,
    )
    # Minimal caps the visible rows at five but counts every result, so the
    # two have to agree before the identities can be trusted.
    assert full["result_count"] == minimal["result_count"]
    return {result["name"] for result in full["results"]}


@pytest.fixture(scope="module")
def store_close_callers(repo) -> set[str]:
    return _callers(repo, _qn(repo, "pkg/lib.py", "GraphStore.close"))


# ---------------------------------------------------------------------------
# (a) tuple and list unpacking from an annotated call
# ---------------------------------------------------------------------------


def test_tuple_unpacking_binds_by_position(store_close_callers):
    """``store, root = get_store()`` binds ``store``, not ``root``."""
    assert "tuple_unpack" in store_close_callers


def test_tuple_unpacking_reaches_the_right_class(repo):
    """The decoy with the same method name gets none of these callers."""
    assert _callers(repo, _qn(repo, "pkg/lib.py", "Rival.close")) == set()


def test_nested_unpacking_follows_the_annotation_into_the_inner_tuple(
    store_close_callers,
):
    """``first, (second, third) = get_pair()`` types both, at both depths."""
    assert "nested_unpack" in store_close_callers


def test_a_starred_target_keeps_the_names_before_it(store_close_callers):
    """``head, *rest = get_many()`` still types ``head``."""
    assert "starred_unpack" in store_close_callers


def test_a_starred_target_gives_up_on_itself_not_the_statement(
    repo, store_close_callers,
):
    """Nothing after the star is numbered, so nothing after it is typed."""
    assert "starred_tail_is_given_up" not in store_close_callers


def test_with_as_tuple_alias_binds_the_same_way(store_close_callers):
    """``with get_store() as (store, root)`` is an assignment in disguise."""
    assert "with_tuple_alias" in store_close_callers


def test_the_second_position_is_not_given_the_first_position_type(repo):
    """``root`` is a ``Path``; it must not answer for ``GraphStore``."""
    parser = CodeParser()
    _nodes, edges = parser.parse_file(repo / "pkg" / "use.py")
    bound = {
        (edge.line, edge.extra.get("receiver")): edge.extra.get("receiver_class")
        for edge in edges
        if edge.kind == "CALLS" and edge.extra.get("receiver_binding") == "class"
    }
    assert all(receiver != "root" for _line, receiver in bound)


# ---------------------------------------------------------------------------
# (b) dotted-attribute constructors
# ---------------------------------------------------------------------------


def test_dotted_module_constructor_binds_the_receiver(store_close_callers):
    """``pkg.lib.GraphStore(path)`` types ``store`` like ``GraphStore(path)``."""
    assert "dotted_constructor" in store_close_callers


def test_module_attribute_constructor_binds_the_receiver(store_close_callers):
    """``lib.GraphStore(path)`` after ``from pkg import lib``."""
    assert "module_attribute_constructor" in store_close_callers


def test_dotted_constructor_inside_with_binds_the_alias(store_close_callers):
    assert "dotted_constructor_in_with" in store_close_callers


def test_the_dotted_constructor_call_itself_names_the_class(repo):
    """``pkg.lib.GraphStore(...)`` is a call INTO the class, and says so."""
    assert "dotted_constructor" in _callers(
        repo, _qn(repo, "pkg/lib.py", "GraphStore"),
    )


def test_an_unimported_dotted_root_is_not_invented(repo):
    """The import map is the authority; a bare attribute walk is not."""
    parser = CodeParser()
    source = repo / "pkg" / "unimported.py"
    source.write_text(
        "def f(holder, path):\n"
        "    store = holder.GraphStore(path)\n"
        "    store.close()\n",
        encoding="utf-8",
    )
    try:
        _nodes, edges = parser.parse_file(source)
        bindings = {
            edge.extra.get("receiver_binding")
            for edge in edges
            if edge.kind == "CALLS" and edge.extra.get("receiver") == "store"
        }
        assert bindings == {"unknown"}
    finally:
        source.unlink()


# ---------------------------------------------------------------------------
# (c) expression receivers
# ---------------------------------------------------------------------------


def test_a_constructor_expression_receiver_names_its_class(repo):
    """``CodeParser().parse_file(path)`` calls ``CodeParser.parse_file``."""
    assert _callers(repo, _qn(repo, "pkg/lib.py", "CodeParser.parse_file")) == {
        "expression_receiver",
    }


def test_a_constructor_expression_receiver_reaches_a_shared_method_name(
    store_close_callers,
):
    """``GraphStore(path).close()`` picks ``GraphStore`` over ``Rival``."""
    assert "constructor_expression_receiver" in store_close_callers


# ---------------------------------------------------------------------------
# (d) annotated factories, and the honest default that survives them
# ---------------------------------------------------------------------------


def test_a_module_local_annotated_factory_types_its_result(
    store_close_callers,
):
    """``_local_factory(path) -> GraphStore`` is the file's own claim."""
    assert "local_annotated_factory" in store_close_callers


def test_an_unannotated_factory_still_attributes_nothing(store_close_callers):
    """No annotation, no inference: the honest default is unchanged."""
    assert "unannotated_factory_says_nothing" not in store_close_callers


def test_a_receiver_the_file_never_describes_attributes_nothing(
    store_close_callers,
):
    assert "no_evidence_at_all" not in store_close_callers


def test_the_whole_answer_is_exactly_the_shapes_the_file_states(
    store_close_callers,
):
    """The full set, so a future widening cannot slip past unnoticed."""
    assert store_close_callers == {
        "tuple_unpack",
        "nested_unpack",
        "starred_unpack",
        "dotted_constructor",
        "module_attribute_constructor",
        "dotted_constructor_in_with",
        "constructor_expression_receiver",
        "with_tuple_alias",
        "local_annotated_factory",
        # ``GraphStore.__exit__`` calls ``self.close()`` — the established
        # same-class path, kept as proof the filter costs it nothing.
        "__exit__",
    }


# ---------------------------------------------------------------------------
# Annotation reading, pinned directly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("annotation", "path", "expected"),
    [
        ("tuple[GraphStore, Path]", (0,), "GraphStore"),
        ("tuple[GraphStore, Path]", (1,), "Path"),
        ("tuple[GraphStore, Path]", (2,), None),
        ("tuple[A, tuple[B, C]]", (1, 0), "B"),
        ("tuple[GraphStore, ...]", (7,), "GraphStore"),
        ('"tuple[GraphStore, Path]"', (0,), "GraphStore"),
        ("tuple[dict[str, int], Path]", (0,), "dict[str, int]"),
        ("GraphStore", (0,), None),
        ("list[GraphStore]", (0,), None),
        ("GraphStore", (), "GraphStore"),
    ],
)
def test_tuple_annotation_positions(annotation, path, expected):
    assert CodeParser._python_annotation_at_path(annotation, path) == expected


def test_a_return_annotation_is_read_but_a_return_statement_is_not(tmp_path):
    """Only what the module WROTE counts; a body is never inspected."""
    module = tmp_path / "factories.py"
    module.write_text(
        "class Thing:\n"
        "    pass\n"
        "\n"
        "\n"
        "def annotated() -> Thing:\n"
        "    return Thing()\n"
        "\n"
        "\n"
        "def bare():\n"
        "    return Thing()\n",
        encoding="utf-8",
    )
    annotations = CodeParser()._python_module_return_annotations(str(module))
    assert annotations == {"annotated": "Thing"}
