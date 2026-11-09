"""A bare name written in full means that name, and the answer stays small.

``query_graph`` used to send every bare target through the tokenising full-text
index, so ``hybrid_search`` also matched each ``test_hybrid_search_*`` and came
back "ambiguous" with thousands of tokens and no answer, even when only one
symbol had that name. See: #1149
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.tools import query_graph


def _repo(tmp_path: Path) -> tuple[Path, GraphStore]:
    root = tmp_path.resolve()
    (root / ".git").mkdir()
    return root, GraphStore(get_db_path(root))


def _add(
    store: GraphStore,
    path: Path,
    name: str,
    kind: str = "Function",
    is_test: bool = False,
) -> str:
    store.upsert_node(NodeInfo(
        kind=kind,
        name=name,
        file_path=str(path),
        line_start=1,
        line_end=5,
        language="python",
        is_test=is_test,
    ))
    return f"{path}::{name}"


def test_exact_name_beats_tokenised_matches(tmp_path):
    root, store = _repo(tmp_path)
    try:
        for suffix in ("a", "b", "c"):
            _add(
                store, root / "tests" / "test_search.py",
                f"test_hybrid_search_{suffix}", kind="Test", is_test=True,
            )
        _add(store, root / "search.py", "hybrid_search_helper")
        wanted = _add(store, root / "search.py", "hybrid_search")
        store.commit()
    finally:
        store.close()

    result = query_graph("callers_of", "hybrid_search", str(root))

    assert result["status"] == "ok"
    assert result["target"] == wanted


def test_exact_name_survives_the_search_cap(tmp_path):
    """The exact node must not depend on landing inside the fuzzy LIMIT."""
    root, store = _repo(tmp_path)
    try:
        for index in range(40):
            _add(store, root / "noise.py", f"process_{index}")
        wanted = _add(store, root / "zzz.py", "process")
        store.commit()
    finally:
        store.close()

    result = query_graph("callers_of", "process", str(root))

    assert result["status"] == "ok"
    assert result["target"] == wanted


def test_exact_name_is_case_sensitive(tmp_path):
    root, store = _repo(tmp_path)
    try:
        _add(store, root / "a.py", "Process", kind="Class")
        wanted = _add(store, root / "b.py", "process")
        store.commit()
    finally:
        store.close()

    result = query_graph("callers_of", "process", str(root))

    assert result["status"] == "ok"
    assert result["target"] == wanted


def test_same_named_test_helper_does_not_block_the_real_symbol(tmp_path):
    root, store = _repo(tmp_path)
    try:
        wanted = _add(store, root / "app.py", "load_config")
        _add(store, root / "tests" / "test_app.py", "load_config")
        _add(store, root / "tests" / "test_other.py", "load_config")
        store.commit()
    finally:
        store.close()

    result = query_graph("callers_of", "load_config", str(root))

    assert result["status"] == "ok"
    assert result["target"] == wanted
    # The pick must be visible: the reader asked about a name two tests share.
    assert "2 same-named test symbol(s)" in result["summary"]


def test_two_real_symbols_stay_ambiguous_without_the_fuzzy_noise(tmp_path):
    root, store = _repo(tmp_path)
    try:
        first = _add(store, root / "a.py", "process")
        second = _add(store, root / "b.py", "process")
        _add(
            store, root / "tests" / "test_a.py", "test_process",
            kind="Test", is_test=True,
        )
        _add(store, root / "c.py", "process_all")
        store.commit()
    finally:
        store.close()

    result = query_graph("callers_of", "process", str(root))

    assert result["status"] == "ambiguous"
    assert result["candidate_count"] == 2
    assert result["candidates_truncated"] is False
    assert {c["qualified_name"] for c in result["disambiguation"]} == {first, second}


def test_exact_count_survives_the_candidate_cap(tmp_path):
    root, store = _repo(tmp_path)
    try:
        for index in range(25):
            _add(store, root / f"m{index}.py", "process")
        store.commit()
    finally:
        store.close()

    result = query_graph("callers_of", "process", str(root))

    assert result["status"] == "ambiguous"
    assert result["candidate_count"] == 25
    assert len(result["disambiguation"]) == 20
    assert result["candidates_truncated"] is True


def test_test_symbols_can_still_be_asked_about_by_name(tmp_path):
    root, store = _repo(tmp_path)
    try:
        wanted = _add(
            store, root / "tests" / "test_a.py", "test_load",
            kind="Test", is_test=True,
        )
        _add(
            store, root / "tests" / "test_a.py", "test_load_twice",
            kind="Test", is_test=True,
        )
        store.commit()
    finally:
        store.close()

    result = query_graph("callees_of", "test_load", str(root))

    assert result["status"] == "ok"
    assert result["target"] == wanted


def test_partial_name_keeps_the_fuzzy_answer(tmp_path):
    root, store = _repo(tmp_path)
    try:
        _add(store, root / "a.py", "hybrid_search")
        _add(store, root / "a.py", "hybrid_rerank")
        store.commit()
    finally:
        store.close()

    result = query_graph("callers_of", "hybrid", str(root))

    assert result["status"] == "ambiguous"
    assert result["candidate_count"] == 2


def test_minimal_ambiguous_answer_is_small_and_sent_once(tmp_path):
    root, store = _repo(tmp_path)
    try:
        for index in range(30):
            _add(store, root / "pkg" / f"mod{index}.py", f"hybrid_{index}")
        store.commit()
    finally:
        store.close()

    result = query_graph("callers_of", "hybrid", str(root), detail_level="minimal")

    assert result["status"] == "ambiguous"
    assert len(result["candidates"]) == 5
    assert "disambiguation" not in result
    assert all(set(row) == {"qualified_name", "kind"} for row in result["candidates"])
    assert result["candidate_count"] == 30
    assert result["candidates_truncated"] is True
    assert "candidates" in result["hint"]
    assert "disambiguation" not in result["summary"]
    # The project's stated budget for graph context on a whole task.
    assert len(json.dumps(result)) // 4 < 800


def test_standard_ambiguous_answer_keeps_both_keys(tmp_path):
    root, store = _repo(tmp_path)
    try:
        for index in range(3):
            _add(store, root / f"m{index}.py", "process")
        store.commit()
    finally:
        store.close()

    result = query_graph("callers_of", "process", str(root))

    assert result["candidates"] == result["disambiguation"]
    assert {"qualified_name", "name", "kind", "file_path", "line_start"} <= set(
        result["disambiguation"][0],
    )


@pytest.mark.parametrize("detail", ["minimal", "standard"])
def test_long_checkout_path_candidates_can_still_be_selected(tmp_path, detail):
    """Names past the 256 character cut must stay distinct and usable."""
    root, store = _repo(tmp_path)
    deep = root / ("d" * 120) / ("e" * 120)
    try:
        first = _add(store, deep / "a.py", "process")
        second = _add(store, deep / "b.py", "process")
        store.commit()
    finally:
        store.close()
    assert len(first) > 256

    ambiguous = query_graph("callers_of", "process", str(root), detail_level=detail)

    names = [row["qualified_name"] for row in ambiguous["candidates"]]
    assert len(set(names)) == 2
    for name, stored in zip(sorted(names), sorted([first, second])):
        assert stored.endswith(name)
        assert query_graph("children_of", name, str(root))["status"] == "ok"


def test_exact_name_lookup_uses_an_index(tmp_path):
    """Every bare-name query runs this, so it must not scan the whole graph."""
    _root, store = _repo(tmp_path)
    try:
        plan = store._conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT * FROM nodes WHERE name = ? ORDER BY qualified_name",
            ("process",),
        ).fetchall()
    finally:
        store.close()

    assert "idx_nodes_name" in " ".join(row[3] for row in plan)


@pytest.mark.parametrize("detail", ["minimal", "standard"])
def test_resolved_callees_come_before_guesses(tmp_path, detail):
    """Five bare builtins must not fill the window and hide the real callee."""
    root, store = _repo(tmp_path)
    source = root / "a.py"
    try:
        caller = _add(store, source, "caller")
        real = _add(store, source, "real_callee")
        for line, name in enumerate(["len", "str", "int", "max", "min", "sum"], start=2):
            store.upsert_edge(EdgeInfo(
                kind="CALLS", source=caller, target=name,
                file_path=str(source), line=line,
            ))
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=caller, target=real,
            file_path=str(source), line=20,
        ))
        store.commit()
    finally:
        store.close()

    result = query_graph("callees_of", caller, str(root), detail_level=detail)

    assert result["results"][0]["name"] == "real_callee"
    assert "target_resolution" not in result["results"][0]
    assert result["results"][1]["target_resolution"] == "unresolved"
    assert result["result_count"] == 7


def test_every_callee_is_seen_before_a_repeat(tmp_path):
    """Certain-first must not undo first-sightings-before-repeats."""
    root, store = _repo(tmp_path)
    source = root / "a.py"
    try:
        caller = _add(store, source, "caller")
        real = _add(store, source, "real_callee")
        for line in (2, 3):
            store.upsert_edge(EdgeInfo(
                kind="CALLS", source=caller, target=real,
                file_path=str(source), line=line,
            ))
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=caller, target="len",
            file_path=str(source), line=4,
        ))
        store.commit()
    finally:
        store.close()

    result = query_graph("callees_of", caller, str(root))

    assert [row["name"] for row in result["results"]] == [
        "real_callee", "len", "real_callee",
    ]
