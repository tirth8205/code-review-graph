"""``query_graph("path_to", A, to=B)``: how A reaches B, and how sure the route is.

Each hop says how it was bound: to the very node (``direct``), through a bare
name only one symbol has (``unique_name``), or through one several share
(``shared_name``). Stored edge confidence only breaks ties within a level: graphs
built before #984 was fixed say 1.0 even on an edge that names no node.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from code_review_graph import cli
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.tools import query as query_module
from code_review_graph.tools import query_graph


def _repo(tmp_path: Path) -> tuple[Path, GraphStore]:
    root = tmp_path.resolve()
    (root / ".git").mkdir(exist_ok=True)
    return root, GraphStore(get_db_path(root))


def _node(
    store: GraphStore,
    path: Path,
    name: str,
    kind: str = "Function",
    language: str = "python",
    is_test: bool = False,
) -> str:
    store.upsert_node(NodeInfo(
        kind=kind, name=name, file_path=str(path), line_start=1, line_end=5,
        language=language, is_test=is_test,
    ))
    return f"{path}::{name}"


def _edge(
    store: GraphStore,
    source: str,
    target: str,
    kind: str = "CALLS",
    line: int = 2,
    extra: dict | None = None,
) -> None:
    store.upsert_edge(EdgeInfo(
        kind=kind, source=source, target=target,
        file_path=source.split("::", 1)[0], line=line, extra=extra or {},
    ))


def _hops(result: dict) -> list[tuple[str, str]]:
    return [(hop["from"].rsplit("::", 1)[-1], hop["to"].rsplit("::", 1)[-1])
            for hop in result["results"]]


class TestRoute:
    def test_direct_route(self, tmp_path):
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            b = _node(store, root / "b.py", "middle")
            c = _node(store, root / "c.py", "finish")
            _edge(store, a, b, line=3)
            _edge(store, b, c, line=7)
            store.commit()
        finally:
            store.close()

        result = query_graph("path_to", "start", str(root), to="finish")

        assert result["status"] == "ok"
        assert result["pattern"] == "path_to"
        assert result["target"] == a
        assert result["to"] == c
        assert result["result_count"] == 2
        assert _hops(result) == [("start", "middle"), ("middle", "finish")]
        first = result["results"][0]
        assert first["kind"] == "CALLS"
        assert first["line"] == 3
        assert first["file_path"] == str(root / "a.py")
        assert first["certainty"] == "direct"
        assert result["certainty"] == "direct"

    def test_shortest_route_wins(self, tmp_path):
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            b = _node(store, root / "a.py", "long_way")
            c = _node(store, root / "a.py", "finish")
            _edge(store, a, b)
            _edge(store, b, c)
            _edge(store, a, c)
            store.commit()
        finally:
            store.close()

        result = query_graph("path_to", "start", str(root), to="finish")

        assert _hops(result) == [("start", "finish")]

    def test_a_cycle_does_not_hang(self, tmp_path):
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            b = _node(store, root / "a.py", "loop")
            c = _node(store, root / "a.py", "finish")
            _edge(store, a, b)
            _edge(store, b, a)
            _edge(store, b, c)
            store.commit()
        finally:
            store.close()

        assert _hops(query_graph("path_to", "start", str(root), to="finish")) == [
            ("start", "loop"), ("loop", "finish"),
        ]

    def test_same_node_needs_no_hops(self, tmp_path):
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            store.commit()
        finally:
            store.close()

        result = query_graph("path_to", a, str(root), to=a)

        assert result["status"] == "ok"
        assert result["result_count"] == 0
        assert result["results"] == []
        assert "same" in result["summary"]

    @pytest.mark.parametrize("kind", ["REFERENCES", "INHERITS", "IMPLEMENTS", "CONTAINS"])
    def test_other_edges_carry_a_route(self, tmp_path, kind):
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start", kind="Class")
            b = _node(store, root / "b.py", "finish", kind="Class")
            _edge(store, a, b, kind=kind)
            store.commit()
        finally:
            store.close()

        result = query_graph("path_to", "start", str(root), to="finish")

        assert result["results"][0]["kind"] == kind

    def test_tested_by_is_not_a_route(self, tmp_path):
        """A -> test -> B says nothing about how A reaches B."""
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            test = _node(store, root / "a.py", "check", kind="Test", is_test=True)
            b = _node(store, root / "b.py", "finish")
            _edge(store, a, test, kind="TESTED_BY")
            _edge(store, test, b)
            store.commit()
        finally:
            store.close()

        result = query_graph("path_to", "start", str(root), to="finish")

        assert result["result_count"] == 0


    def test_reads_only_what_it_walks(self, tmp_path, monkeypatch):
        """A short route, a missing end or the same node never loads the whole graph."""
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            b = _node(store, root / "b.py", "finish")
            _edge(store, a, b)
            store.commit()
        finally:
            store.close()

        def whole_graph(*_args, **_kwargs):
            raise AssertionError("path_to read the whole graph")

        monkeypatch.setattr(GraphStore, "get_all_edges", whole_graph)
        monkeypatch.setattr(GraphStore, "get_all_nodes", whole_graph)

        assert query_graph("path_to", "start", str(root), to="finish")["result_count"] == 1
        assert query_graph("path_to", a, str(root), to=a)["result_count"] == 0
        assert query_graph("path_to", a, str(root), to="nothing")["status"] == "not_found"


class TestSearchLimit:
    def _graph(self, tmp_path: Path, weaker_edge: bool) -> tuple[str, str]:
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            for index in range(5):
                _edge(store, a, _node(store, root / f"d{index}.py", f"dead_end_{index}"))
            middle = _node(store, root / "z.py", "middle")
            finish = _node(store, root / "z.py", "finish")
            _edge(store, a, middle)
            _edge(store, middle, finish)
            if weaker_edge:
                _edge(store, a, "finish")
            store.commit()
        finally:
            store.close()
        return a, finish

    @pytest.mark.parametrize("weaker_edge", [True, False])
    def test_hitting_the_limit_is_not_no_route(self, tmp_path, monkeypatch, weaker_edge):
        a, finish = self._graph(tmp_path, weaker_edge)
        monkeypatch.setattr(query_module, "_MAX_ROUTE_VISITS", 3)

        result = query_graph("path_to", a, str(tmp_path.resolve()), to=finish)

        # Neither a weaker route nor "no route": the search stopped short.
        assert result["status"] == "ok"
        assert result["result_count"] == 0
        assert result["search_incomplete"] is True
        assert "certainty" not in result
        assert "No route" not in result["summary"]
        assert "stopped" in result["summary"]

    def test_within_the_limit_finds_the_route(self, tmp_path):
        a, finish = self._graph(tmp_path, weaker_edge=True)

        result = query_graph("path_to", a, str(tmp_path.resolve()), to=finish)

        assert result["certainty"] == "direct"
        assert "search_incomplete" not in result


class TestCertainty:
    def test_bare_name_one_symbol_has(self, tmp_path):
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            c = _node(store, root / "b.py", "finish")
            _edge(store, a, "finish")
            store.commit()
        finally:
            store.close()

        result = query_graph("path_to", "start", str(root), to="finish")

        assert result["to"] == c
        assert result["results"][0]["certainty"] == "unique_name"
        assert result["certainty"] == "unique_name"
        assert result["weakest_hop"]["certainty"] == "unique_name"

    def test_bare_name_several_symbols_share(self, tmp_path):
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            wanted = _node(store, root / "b.py", "finish")
            _node(store, root / "c.py", "finish")
            _edge(store, a, "finish")
            store.commit()
        finally:
            store.close()

        result = query_graph("path_to", "start", str(root), to=wanted)

        assert result["results"][0]["certainty"] == "shared_name"
        assert "shared_name" in result["summary"]

    def test_ambiguous_targets_are_the_candidates(self, tmp_path):
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            first = _node(store, root / "b.py", "finish")
            second = _node(store, root / "c.py", "finish")
            third = _node(store, root / "d.py", "finish")
            _edge(store, a, "finish", extra={"ambiguous_targets": [first, second]})
            store.commit()
        finally:
            store.close()

        assert query_graph(
            "path_to", "start", str(root), to=first,
        )["results"][0]["certainty"] == "shared_name"
        assert query_graph("path_to", "start", str(root), to=third)["result_count"] == 0

    @pytest.mark.parametrize("listed, recorded", [(11, 11), (2, 25)])
    def test_resolver_candidates_obey_the_ten_symbol_cutoff(self, tmp_path, listed, recorded):
        """The recorded total counts too: a list cut to 20 still names 25."""
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            named = [_node(store, root / f"m{i}.py", "finish") for i in range(listed)]
            _edge(store, a, "finish", extra={
                "ambiguous_targets": named, "ambiguous_target_count": recorded,
            })
            store.commit()
        finally:
            store.close()

        assert query_graph("path_to", a, str(root), to=named[0])["result_count"] == 0

    def test_resolver_candidates_skip_test_code(self, tmp_path):
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            test = _node(store, root / "t.py", "check", kind="Test", is_test=True)
            helper = _node(store, root / "tests" / "helpers.py", "check")
            real = _node(store, root / "b.py", "check")
            finish = _node(store, root / "c.py", "finish")
            _edge(store, a, "check", extra={"ambiguous_targets": [test, helper, real]})
            _edge(store, test, finish)
            _edge(store, helper, finish)
            store.commit()
        finally:
            store.close()

        assert query_graph("path_to", a, str(root), to=finish)["result_count"] == 0
        result = query_graph("path_to", a, str(root), to=real)
        assert _hops(result) == [("start", "check")]

    def test_name_match_skips_helpers_in_test_files(self, tmp_path):
        """An older graph marks a test-file helper is_test=False; its path still says test."""
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            helper = _node(store, root / "tests" / "helpers.py", "make_cart")
            finish = _node(store, root / "b.py", "finish")
            _edge(store, a, "make_cart")
            _edge(store, helper, finish)
            store.commit()
        finally:
            store.close()

        assert query_graph("path_to", a, str(root), to=finish)["result_count"] == 0

    def test_longer_certain_route_beats_a_shorter_guess(self, tmp_path):
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            b = _node(store, root / "a.py", "middle")
            c = _node(store, root / "b.py", "finish")
            _edge(store, a, "finish")
            _edge(store, a, b)
            _edge(store, b, c)
            store.commit()
        finally:
            store.close()

        result = query_graph("path_to", "start", str(root), to="finish")

        assert _hops(result) == [("start", "middle"), ("middle", "finish")]
        assert result["certainty"] == "direct"

    def test_weakest_hop_names_the_hop(self, tmp_path):
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            b = _node(store, root / "a.py", "middle")
            c = _node(store, root / "b.py", "finish")
            _edge(store, a, b)
            _edge(store, b, "finish", line=9)
            store.commit()
        finally:
            store.close()

        result = query_graph("path_to", "start", str(root), to="finish")

        assert result["results"][0]["certainty"] == "direct"
        assert result["weakest_hop"]["certainty"] == "unique_name"
        assert result["weakest_hop"]["from"] == b
        assert result["weakest_hop"]["to"] == c
        assert result["weakest_hop"]["line"] == 9

    @pytest.mark.parametrize("guess", [
        {},  # an older graph: a bare name still says EXTRACTED and 1.0
        {"confidence": 0.2, "confidence_tier": "UNRESOLVED"},
    ])
    def test_a_name_guess_is_weaker_than_any_bound_hop(self, tmp_path, guess):
        """How a hop was bound ranks first, whatever score the edge stores."""
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            b = _node(store, root / "a.py", "middle")
            c = _node(store, root / "b.py", "finish")
            _edge(store, a, b, line=3, extra={"confidence": 0.7, "confidence_tier": "INFERRED"})
            _edge(store, b, "finish", line=9, extra=guess)
            store.commit()
        finally:
            store.close()

        result = query_graph("path_to", "start", str(root), to="finish")

        assert result["certainty"] == "unique_name"
        assert result["weakest_hop"]["to"] == c
        assert result["weakest_hop"]["line"] == 9

    def test_a_lower_stored_score_breaks_a_tie(self, tmp_path):
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            b = _node(store, root / "a.py", "middle")
            c = _node(store, root / "b.py", "finish")
            _edge(store, a, b, line=3)
            _edge(store, b, c, line=9, extra={"confidence": 0.7, "confidence_tier": "INFERRED"})
            store.commit()
        finally:
            store.close()

        result = query_graph("path_to", "start", str(root), to="finish")

        assert result["certainty"] == "direct"
        assert result["weakest_hop"]["line"] == 9
        assert result["weakest_hop"]["confidence"] == 0.7
        # A hop at the full score does not spell it out.
        assert "confidence" not in result["results"][0]

    def test_receiver_evidence_blocks_a_name_match(self, tmp_path):
        """``some_dict.get(...)`` does not mean ``ConnectionPool.get``."""
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            pool = root / "pool.py"
            store.upsert_node(NodeInfo(
                kind="Function", name="fetch_row", parent_name="ConnectionPool",
                file_path=str(pool), line_start=1, line_end=5, language="python",
            ))
            _edge(store, a, "fetch_row", extra={
                "receiver": "some_dict", "receiver_binding": "local",
            })
            store.commit()
        finally:
            store.close()

        result = query_graph(
            "path_to", "start", str(root), to=f"{pool}::ConnectionPool.fetch_row",
        )

        assert result["result_count"] == 0

    def test_builtin_name_is_not_followed(self, tmp_path):
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            wanted = _node(store, root / "b.py", "get")
            _edge(store, a, "get")
            store.commit()
        finally:
            store.close()

        assert query_graph("path_to", a, str(root), to=wanted)["result_count"] == 0

    def test_other_language_is_not_a_name_match(self, tmp_path):
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            wanted = _node(store, root / "b.go", "finish", language="go")
            _edge(store, a, "finish")
            store.commit()
        finally:
            store.close()

        assert query_graph("path_to", a, str(root), to=wanted)["result_count"] == 0

    def test_test_symbol_is_not_a_name_match(self, tmp_path):
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            test = _node(store, root / "t.py", "check", kind="Test", is_test=True)
            b = _node(store, root / "b.py", "finish")
            _edge(store, a, "check")
            _edge(store, test, b)
            store.commit()
        finally:
            store.close()

        assert query_graph("path_to", a, str(root), to=b)["result_count"] == 0


class TestNoRoute:
    def test_says_so_and_names_what_it_cannot_see(self, tmp_path):
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            b = _node(store, root / "b.py", "finish")
            store.commit()
        finally:
            store.close()

        result = query_graph("path_to", a, str(root), to=b)

        assert result["status"] == "ok"
        assert result["result_count"] == 0
        assert result["results"] == []
        assert "No route" in result["summary"]
        assert "certainty" not in result
        assert "reverse_route_exists" not in result

    def test_points_out_the_route_the_other_way(self, tmp_path):
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            b = _node(store, root / "b.py", "finish")
            _edge(store, b, a)
            store.commit()
        finally:
            store.close()

        result = query_graph("path_to", a, str(root), to=b)

        assert result["result_count"] == 0
        assert result["reverse_route_exists"] is True
        assert "other way" in result["summary"]


class TestEndpoints:
    def test_missing_to_is_an_error(self, tmp_path):
        root, store = _repo(tmp_path)
        store.close()

        result = query_graph("path_to", "start", str(root))

        assert result["status"] == "error"
        assert "to" in result["error"]

    @pytest.mark.parametrize("which", ["target", "to"])
    def test_unknown_endpoint_says_which(self, tmp_path, which):
        root, store = _repo(tmp_path)
        try:
            _node(store, root / "a.py", "start")
            store.commit()
        finally:
            store.close()
        names = {"target": "nothing_here", "to": "start"}
        if which == "to":
            names = {"target": "start", "to": "nothing_here"}

        result = query_graph("path_to", names["target"], str(root), to=names["to"])

        assert result["status"] == "not_found"
        assert result["endpoint"] == which
        assert "nothing_here" in result["summary"]

    @pytest.mark.parametrize("which", ["target", "to"])
    def test_ambiguous_endpoint_says_which(self, tmp_path, which):
        root, store = _repo(tmp_path)
        try:
            first = _node(store, root / "a.py", "shared")
            second = _node(store, root / "b.py", "shared")
            _node(store, root / "c.py", "other")
            store.commit()
        finally:
            store.close()
        names = {"target": "shared", "to": "other"}
        if which == "to":
            names = {"target": "other", "to": "shared"}

        result = query_graph("path_to", names["target"], str(root), to=names["to"])

        assert result["status"] == "ambiguous"
        assert result["endpoint"] == which
        assert result["candidate_count"] == 2
        assert {c["qualified_name"] for c in result["candidates"]} == {first, second}

    def test_exact_name_beats_tokenised_matches(self, tmp_path):
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            _node(store, root / "tests" / "test_a.py", "test_start", kind="Test", is_test=True)
            _node(store, root / "a.py", "start_helper")
            b = _node(store, root / "b.py", "finish")
            _edge(store, a, b)
            store.commit()
        finally:
            store.close()

        assert query_graph("path_to", "start", str(root), to="finish")["target"] == a

    def test_real_symbol_beats_a_same_named_test_helper(self, tmp_path):
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            _node(store, root / "tests" / "test_a.py", "start")
            b = _node(store, root / "b.py", "finish")
            _edge(store, a, b)
            store.commit()
        finally:
            store.close()

        assert query_graph("path_to", "start", str(root), to="finish")["target"] == a

    def test_file_paths_work_as_endpoints(self, tmp_path):
        root, store = _repo(tmp_path)
        try:
            a = str(root / "a.py")
            b = str(root / "b.py")
            store.upsert_node(NodeInfo(
                kind="File", name=a, file_path=a, line_start=1, line_end=1, language="python",
            ))
            store.upsert_node(NodeInfo(
                kind="File", name=b, file_path=b, line_start=1, line_end=1, language="python",
            ))
            store.upsert_edge(EdgeInfo(
                kind="IMPORTS_FROM", source=a, target=b, file_path=a, line=1,
            ))
            store.commit()
        finally:
            store.close()

        result = query_graph("path_to", "a.py", str(root), to="b.py")

        assert result["results"][0]["kind"] == "IMPORTS_FROM"
        assert result["certainty"] == "direct"
        assert "'a.py' reaches 'b.py'" in result["summary"]

        minimal = query_graph("path_to", "a.py", str(root), to="b.py", detail_level="minimal")

        assert minimal["results"][0]["from"] == "a.py"
        assert minimal["results"][0]["to"] == "b.py"

    def test_type_only_import_is_not_a_route(self, tmp_path):
        """``if TYPE_CHECKING:`` imports never run, as import cycles already know."""
        root = tmp_path.resolve()
        (root / ".git").mkdir()
        (root / "a.py").write_text(
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from b import Thing\n"
            "x = 1\n",
            encoding="utf-8",
        )
        (root / "b.py").write_text("class Thing:\n    pass\n", encoding="utf-8")
        (root / "c.py").write_text("from b import Thing\n", encoding="utf-8")
        store = GraphStore(get_db_path(root))
        try:
            full_build(root, store)
        finally:
            store.close()

        assert query_graph("path_to", "a.py", str(root), to="b.py")["result_count"] == 0
        # A plain import still is one.
        result = query_graph("path_to", "c.py", str(root), to="b.py")
        assert result["results"][0]["kind"] == "IMPORTS_FROM"


class TestMinimal:
    def test_uses_short_names_and_stays_small(self, tmp_path):
        root, store = _repo(tmp_path)
        try:
            names = [f"step_{index}" for index in range(8)]
            nodes = [_node(store, root / "pkg" / f"m{i}.py", n) for i, n in enumerate(names)]
            for source, target in zip(nodes, nodes[1:]):
                _edge(store, source, target)
            store.commit()
        finally:
            store.close()

        result = query_graph(
            "path_to", "step_0", str(root), to="step_7", detail_level="minimal",
        )

        # A route cut short is no route, so the five-row cap does not apply.
        assert result["result_count"] == 7
        assert len(result["results"]) == 7
        assert result["results"][0] == {
            "from": "pkg/m0.py::step_0", "to": "pkg/m1.py::step_1", "kind": "CALLS",
            "line": 2, "certainty": "direct",
        }
        assert result["weakest_hop"]["from"] == "pkg/m0.py::step_0"
        assert len(json.dumps(result)) // 4 < 800


    def test_same_named_methods_stay_apart(self, tmp_path):
        """``First.run`` and ``Second.run`` must not both read as ``run``."""
        root, store = _repo(tmp_path)
        try:
            for path, parent in ((root / "a.py", "First"), (root / "b.py", "Second")):
                store.upsert_node(NodeInfo(
                    kind="Function", name="run", parent_name=parent, file_path=str(path),
                    line_start=1, line_end=5, language="python",
                ))
            first = f"{root / 'a.py'}::First.run"
            second = f"{root / 'b.py'}::Second.run"
            _edge(store, first, second, line=4)
            store.commit()
        finally:
            store.close()

        result = query_graph("path_to", first, str(root), to=second, detail_level="minimal")

        hop = result["results"][0]
        assert hop["from"] == "a.py::First.run"
        assert hop["to"] == "b.py::Second.run"
        assert "file" not in hop
        assert result["weakest_hop"] == hop

    def test_names_the_call_site_file_when_it_is_elsewhere(self, tmp_path):
        root, store = _repo(tmp_path)
        try:
            a = _node(store, root / "a.py", "start")
            b = _node(store, root / "b.py", "finish")
            store.upsert_edge(EdgeInfo(
                kind="CALLS", source=a, target=b, file_path=str(root / "inc" / "a.h"), line=7,
            ))
            store.commit()
        finally:
            store.close()

        result = query_graph("path_to", a, str(root), to=b, detail_level="minimal")

        assert result["results"][0]["file"] == "inc/a.h"


class TestEndToEnd:
    def _build(self, tmp_path: Path) -> Path:
        root = tmp_path.resolve()
        (root / "pkg").mkdir()
        (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (root / "pkg" / "a.py").write_text(
            "from pkg.b import middle\n\n\ndef start():\n    return middle()\n",
            encoding="utf-8",
        )
        (root / "pkg" / "b.py").write_text(
            "from pkg.c import finish\n\n\ndef middle():\n    return finish()\n",
            encoding="utf-8",
        )
        (root / "pkg" / "c.py").write_text("def finish():\n    return 1\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        store = GraphStore(get_db_path(root))
        try:
            full_build(root, store)
        finally:
            store.close()
        return root

    def test_built_graph(self, tmp_path):
        root = self._build(tmp_path)

        result = query_graph("path_to", "start", str(root), to="finish")

        assert _hops(result) == [("start", "middle"), ("middle", "finish")]
        assert result["certainty"] == "direct"
        assert result["results"][0]["line"] == 5

    def test_cli(self, tmp_path, monkeypatch, capsys):
        root = self._build(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            [
                "code-review-graph", "query", "path_to", "start", "--to", "finish",
                "--repo", str(root),
            ],
        )

        cli.main()

        result = json.loads(capsys.readouterr().out)
        assert result["result_count"] == 2
        assert result["certainty"] == "direct"
