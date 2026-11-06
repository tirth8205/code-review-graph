"""A multi-clause Elixir function is one node that spans every clause.

Each clause is its own ``def``, but they share one qualified name and the graph
keeps one node per name, so the last clause used to overwrite the others: the
earlier lines belonged to no function and a change there was missed. See: #979
"""

from __future__ import annotations

from pathlib import Path

from code_review_graph.changes import map_changes_to_nodes
from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser

SOURCE = b"""\
defmodule T do
  def plain(x), do: x

  def guarded(x) when is_binary(x), do: first(x)
  def guarded(x), do: second(x)

  @doc "zero"
  def multi(0), do: zero()

  @doc "other"
  def multi(n) do
    other(n)
  end

  defp first(x), do: x
  defp second(x), do: x
  defp zero, do: 0
  defp other(n), do: n
end
"""


def _parse(tmp_path: Path, source: bytes = SOURCE):
    path = tmp_path / "t.ex"
    path.write_bytes(source)
    nodes, edges = CodeParser().parse_bytes(path, source)
    return path, nodes, edges


def _functions(nodes) -> dict[str, list[tuple[int, int]]]:
    found: dict[str, list[tuple[int, int]]] = {}
    for node in nodes:
        if node.kind in ("Function", "Test"):
            found.setdefault(node.name, []).append((node.line_start, node.line_end))
    return found


def test_clauses_merge_into_one_node_spanning_all_of_them(tmp_path):
    _, nodes, _ = _parse(tmp_path)

    functions = _functions(nodes)

    assert functions["multi"] == [(8, 13)]
    assert functions["plain"] == [(2, 2)]


def test_guarded_clause_still_counts(tmp_path):
    """#980 made guarded heads visible; they must merge like any other clause."""
    _, nodes, _ = _parse(tmp_path)

    assert _functions(nodes)["guarded"] == [(4, 5)]


def test_edges_from_every_clause_are_kept(tmp_path):
    path, nodes, edges = _parse(tmp_path)

    calls = {
        edge.target.rsplit(".", 1)[-1]: edge
        for edge in edges
        if edge.kind == "CALLS" and edge.source.endswith("::T.guarded")
    }
    assert {name: calls[name].line for name in ("first", "second")} == {
        "first": 4, "second": 5,
    }

    calls = {
        edge.target.rsplit(".", 1)[-1]: edge
        for edge in edges
        if edge.kind == "CALLS" and edge.source.endswith("::T.multi")
    }
    assert {name: calls[name].line for name in ("zero", "other")} == {
        "zero": 8, "other": 12,
    }


def test_module_contains_the_function_once(tmp_path):
    path, _, edges = _parse(tmp_path)

    contains = [
        edge for edge in edges
        if edge.kind == "CONTAINS" and edge.target.endswith("::T.multi")
    ]

    assert len(contains) == 1
    assert contains[0].line == 8


def test_first_clause_keeps_its_parameters(tmp_path):
    source = b"""\
defmodule T do
  def with_default(a, b \\\\ 1)
  def with_default(a, 0), do: a
  def with_default(_a, _b), do: nil
end
"""
    _, nodes, _ = _parse(tmp_path, source)

    function = next(node for node in nodes if node.name == "with_default")

    assert function.params == "(a, b \\\\ 1)"
    assert (function.line_start, function.line_end) == (2, 4)


def test_arities_with_another_function_between_share_one_span(tmp_path):
    """The graph has one node per name, so ``get/1`` and ``get/2`` are one."""
    source = b"""\
defmodule T do
  def get(a), do: a
  def fetch(a), do: a
  def get(a, b), do: {a, b}
end
"""
    _, nodes, _ = _parse(tmp_path, source)

    functions = _functions(nodes)

    assert functions["get"] == [(2, 4)]
    assert functions["fetch"] == [(3, 3)]


def test_same_name_in_two_modules_stays_two_functions(tmp_path):
    source = b"""\
defmodule A do
  def run(0), do: 0
  def run(n), do: n
end

defmodule B do
  def run(0), do: 0
  def run(n), do: n
end
"""
    _, nodes, _ = _parse(tmp_path, source)

    spans = {
        node.parent_name: (node.line_start, node.line_end)
        for node in nodes
        if node.name == "run"
    }

    assert len([node for node in nodes if node.name == "run"]) == 2
    assert spans == {"A": (2, 3), "B": (7, 8)}


def test_stored_node_keeps_the_span_and_a_change_in_any_clause_finds_it(tmp_path):
    path, nodes, edges = _parse(tmp_path)
    with GraphStore(tmp_path / "graph.db") as store:
        store.store_file_nodes_edges(str(path), nodes, edges)
        qualified = f"{path}::T.multi"

        node = store.get_node(qualified)
        assert (node.line_start, node.line_end) == (8, 13)

        for line in (8, 12):
            changed = map_changes_to_nodes(store, {str(path): [(line, line)]})
            assert qualified in {found.qualified_name for found in changed}

        stored_calls = {
            edge.target_qualified.rsplit(".", 1)[-1]
            for edge in store.get_edges_by_source(qualified)
            if edge.kind == "CALLS"
        }
        assert {"zero", "other"} <= stored_calls


def test_other_languages_are_untouched(tmp_path):
    """Only Elixir clauses merge: a repeated name elsewhere is a different case."""
    path = tmp_path / "t.py"
    source = b"def f():\n    pass\n\ndef f():\n    pass\n"
    path.write_bytes(source)

    nodes, _ = CodeParser().parse_bytes(path, source)

    assert _functions(nodes)["f"] == [(1, 2), (4, 5)]
