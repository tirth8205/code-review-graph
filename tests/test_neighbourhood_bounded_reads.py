"""A seeded view reads around its seed, and answers exactly as a full read would.

``export_graph_data`` used to load every node, file by file, and the whole edge
table before cutting down to a two-node neighbourhood (#1145, fixed in #1178).
Bare-name edges make this easy to get subtly wrong: an edge from an unrelated
file can resolve onto the seed, so the comparisons here are byte for byte
against the full read.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from code_review_graph import visualization
from code_review_graph.graph import GraphStore
from code_review_graph.neighbourhood import NeighbourhoodSpec, SeedResolutionError, extract
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.visualization import export_graph_data, generate_html

SHARED_NAMES = ("get", "run", "helper")
# Every name in this file is cut to the same 256 characters on export.
DEEP_FILE = "deep/" + "d" * 241 + ".py"


def _node(name: str, file_path: str, kind: str = "Function", **extra_fields) -> NodeInfo:
    return NodeInfo(
        kind=kind,
        name=name,
        file_path=file_path,
        line_start=1,
        line_end=5,
        language="python",
        **extra_fields,
    )


def _edge(kind: str, source: str, target: str, file_path: str, line: int = 1) -> EdgeInfo:
    return EdgeInfo(kind=kind, source=source, target=target, file_path=file_path, line=line)


def _full_read(store: GraphStore, **seed_args) -> dict:
    """What the view was before: everything read, then cut down."""
    spec = NeighbourhoodSpec.from_args(**seed_args).validate()
    return extract(export_graph_data(store), spec)


def _assert_same_view(store: GraphStore, **seed_args) -> None:
    assert json.dumps(export_graph_data(store, **seed_args)) == json.dumps(
        _full_read(store, **seed_args)
    )


def _random_graph(path: Path, seed: int) -> GraphStore:
    """A graph holding every way an edge can name a node."""
    rng = random.Random(seed)
    store = GraphStore(path)
    files = [f"pkg/mod{i}.py" for i in range(8)] + [
        "src/lib/util.ts",
        "include/a/b.hpp",
        "tests/test_mod.py",
        DEEP_FILE,
    ]
    symbols: dict[str, list[str]] = {}
    for file_path in files:
        store.upsert_node(_node(file_path, file_path, kind="File"))
        names = [f"{Path(file_path).stem}_{i}" for i in range(rng.randint(1, 4))]
        names += rng.sample(SHARED_NAMES, rng.randint(0, 2))
        symbols[file_path] = []
        for name in names:
            store.upsert_node(_node(name, file_path))
            symbols[file_path].append(name)
            store.upsert_edge(_edge("CONTAINS", file_path, f"{file_path}::{name}", file_path))
        store.upsert_node(_node("Box", file_path, kind="Class"))
        store.upsert_node(_node("open", file_path, parent_name="Box"))
        store.upsert_edge(_edge("CONTAINS", file_path, f"{file_path}::Box", file_path))
        store.upsert_edge(
            _edge("CONTAINS", f"{file_path}::Box", f"{file_path}::Box.open", file_path)
        )

    # A control character the export strips from the name it shows.
    store.upsert_node(_node("ctl\x01name", "pkg/mod1.py"))
    store.upsert_edge(_edge("CONTAINS", "pkg/mod1.py", "pkg/mod1.py::ctl\x01name", "pkg/mod1.py"))

    # A marker the file inventory hides unless an edge names its file.
    store.upsert_node(_node("evt_hidden", "__events__/hidden", extra={"virtual": True}))
    store.upsert_node(_node("evt_shown", "__events__/shown", extra={"virtual": True}))
    store.upsert_edge(
        _edge("REFERENCES", "pkg/mod0.py", "evt_shown", "__events__/shown", line=2)
    )

    def spell(file_path: str, name: str) -> str:
        qualified = f"{file_path}::{name}"
        module = file_path.replace("/", ".").replace(".py", "")
        return rng.choice([
            qualified,
            name,
            f"{Path(file_path).name}::{name}",
            f"{module}.{name}",
        ])

    def any_end() -> str:
        file_path = rng.choice(files)
        roll = rng.random()
        if roll < 0.1:
            return file_path
        if roll < 0.2:
            return rng.choice(["b.hpp", "a/b.hpp", "lib/util.ts", "pkg.mod3"])
        if roll < 0.3:
            return rng.choice(["os.path.join", "print", "no_such_thing"])
        return spell(file_path, rng.choice(symbols[file_path]))

    kinds = ("CALLS", "IMPORTS_FROM", "INHERITS", "REFERENCES", "TESTED_BY")
    for index in range(rng.randint(20, 80)):
        file_path = rng.choice(files)
        source = any_end() if rng.random() < 0.1 else (
            file_path if rng.random() < 0.2
            else f"{file_path}::{rng.choice(symbols[file_path])}"
        )
        target = source if rng.random() < 0.05 else any_end()
        store.upsert_edge(_edge(rng.choice(kinds), source, target, file_path, line=index + 10))
    # Two parents for one member: the older edge is the one that counts.
    store.upsert_edge(_edge("CONTAINS", "pkg/mod1.py", "pkg/mod0.py::Box", "pkg/mod1.py"))

    node_ids = [row[0] for row in store._conn.execute("SELECT id FROM nodes")]
    for index in range(3):
        store._conn.execute(
            "INSERT INTO flows (name, entry_point_id, depth, node_count, file_count, "
            "criticality, path_json) VALUES (?, ?, 1, 2, 1, ?, ?)",
            (f"flow_{index}", node_ids[0], index / 10,
             json.dumps(rng.sample(node_ids, 3))),
        )
    for index in range(3):
        cursor = store._conn.execute(
            "INSERT INTO communities (name, cohesion, size, dominant_language, description) "
            "VALUES (?, 0.5, ?, 'python', 'd')",
            (f"community_{index}", index),
        )
        members = rng.sample(node_ids, rng.randint(1, len(node_ids) // 3))
        store._conn.executemany(
            "UPDATE nodes SET community_id = ? WHERE id = ?",
            [(cursor.lastrowid, node_id) for node_id in members],
        )
    store.commit()
    return store


SEED_ARGS = [
    {"seed_symbols": ["pkg/mod0.py::mod0_0"]},
    {"seed_symbols": ["pkg/mod2.py::Box.open"], "depth": 3},
    {"seed_symbols": ["helper"], "depth": 1},
    {"seed_symbols": ["mod1.py::mod1_0"], "depth": 2},
    {"seed_symbols": ["pkg/mod0.py"], "depth": 1},
    {"seed_symbols": ["pkg/mod3.py::mod3_0"], "depth": 0},
    {"seed_symbols": ["pkg/mod0.py::mod0_0"], "depth": 2, "max_nodes": 3},
    {"seed_symbols": ["pkg/mod4.py::mod4_0"], "depth": 3, "max_nodes": 1},
    {"seed_symbols": ["pkg/mod4.py::mod4_0"], "depth": 2, "render_depth": 2, "max_nodes": 10},
    {"seed_files": ["pkg/mod5.py"], "depth": 1},
    {"seed_files": ["pkg/mod*"], "depth": 1, "max_nodes": 12},
    {"seed_files": ["b.hpp"], "seed_symbols": ["tests/test_mod.py"], "depth": 2},
    {"seed_flow": "flow_1", "depth": 1},
    {"seed_flow": "flow", "seed_symbols": ["run"], "depth": 2, "max_nodes": 20},
    {"seed_symbols": ["__events__/shown::evt_shown"], "depth": 1},
    {"seed_files": ["deep/*"], "depth": 2},
    {"seed_symbols": ["Box"], "depth": 1, "max_nodes": 8},
    {"seed_symbols": ["ctlname"], "depth": 1},
]


@pytest.mark.parametrize("graph_seed", range(30))
def test_seeded_view_is_byte_identical_to_the_full_read(tmp_path, graph_seed):
    store = _random_graph(tmp_path / "graph.db", graph_seed)
    try:
        for seed_args in SEED_ARGS:
            spec = NeighbourhoodSpec.from_args(**seed_args).validate()
            try:
                expected = _full_read(store, **seed_args)
            except SeedResolutionError as exc:
                # Not every random graph has this name; it must fail the same way.
                with pytest.raises(SeedResolutionError) as raised:
                    visualization._export_seeded(store, spec)
                assert str(raised.value) == str(exc)
                continue
            # Called directly: a fallback to the full read would compare it to itself.
            view = visualization._export_seeded(store, spec)
            assert view is not None, seed_args
            assert json.dumps(view) == json.dumps(expected), seed_args
    finally:
        store.close()


def test_a_seeded_view_does_not_read_the_whole_edge_table(tmp_path, monkeypatch):
    store = _random_graph(tmp_path / "graph.db", 1)
    monkeypatch.setattr(
        GraphStore,
        "get_all_edges",
        lambda _self: pytest.fail("the seeded view read the whole edge table"),
    )
    try:
        view = export_graph_data(store, seed_symbols=["pkg/mod0.py::mod0_0"], depth=2)
    finally:
        store.close()
    assert view["mode"] == "neighbourhood"


def test_an_unrelated_bare_name_edge_still_reaches_the_seed(tmp_path):
    """The caller's file is nowhere near the seed; only the name connects them."""
    store = GraphStore(tmp_path / "graph.db")
    try:
        store.upsert_node(_node("seed_fn", "a/seed.py"))
        store.upsert_node(_node("far_fn", "z/far.py"))
        store.upsert_edge(_edge("CALLS", "z/far.py::far_fn", "seed_fn", "z/far.py"))
        store.commit()

        view = export_graph_data(store, seed_symbols=["a/seed.py::seed_fn"], depth=1)
    finally:
        store.close()

    assert view["neighbourhood"]["hops"]["z/far.py::far_fn"] == 1
    assert [(e["source"], e["target"]) for e in view["edges"]] == [
        ("z/far.py::far_fn", "a/seed.py::seed_fn"),
    ]


def test_edges_between_two_attached_files_are_kept(tmp_path):
    """Neither file is within the hops, but both are drawn, so their edge is too."""
    store = GraphStore(tmp_path / "graph.db")
    try:
        for file_path, name in (("a.py", "fn_a"), ("b.py", "fn_b")):
            store.upsert_node(_node(file_path, file_path, kind="File"))
            store.upsert_node(_node(name, file_path))
            store.upsert_edge(_edge("CONTAINS", file_path, f"{file_path}::{name}", file_path))
        store.upsert_edge(_edge("CALLS", "a.py::fn_a", "b.py::fn_b", "a.py"))
        store.upsert_edge(_edge("IMPORTS_FROM", "a.py", "b.py", "a.py"))
        store.commit()

        view = export_graph_data(store, seed_symbols=["a.py::fn_a"], depth=1)
    finally:
        store.close()

    assert ("a.py", "b.py") in {(e["source"], e["target"]) for e in view["edges"]}


def _store_with_unrelated_files(path: Path, count: int, communities: int) -> tuple[GraphStore, str]:
    store = GraphStore(path)
    names = [("seed.py", "seed")] + [(f"unrelated_{i}.py", f"unrelated_{i}") for i in range(count)]
    for file_path, symbol in names:
        store.upsert_node(_node(file_path, file_path, kind="File"))
        store.upsert_node(_node(symbol, file_path))
        store.upsert_edge(_edge("CONTAINS", file_path, f"{file_path}::{symbol}", file_path))
    for index in range(communities):
        store._conn.execute(
            "INSERT INTO communities (name, size) VALUES (?, 1)", (f"community_{index}",),
        )
    store.commit()
    return store, "seed.py::seed"


def _traced_statements(store: GraphStore, seed: str, tmp_path: Path) -> list[str]:
    statements: list[str] = []
    store._conn.set_trace_callback(statements.append)
    try:
        report: dict = {}
        generate_html(
            store, tmp_path / "graph.html", seed_symbols=[seed], depth=1, max_nodes=50,
            report=report,
        )
    finally:
        store._conn.set_trace_callback(None)
    assert report["node_count"] == 2
    return statements


def test_reads_do_not_grow_with_unrelated_files_or_communities(tmp_path):
    counts = []
    for count, communities in ((100, 5), (400, 200)):
        folder = tmp_path / str(count)
        folder.mkdir()
        store, seed = _store_with_unrelated_files(folder / "graph.db", count, communities)
        try:
            statements = _traced_statements(store, seed, folder)
        finally:
            store.close()
        counts.append(len(statements))
        assert not [s for s in statements if s.startswith("SELECT * FROM nodes WHERE file_path")]
        assert not [s for s in statements if s.strip() == "SELECT * FROM edges"]
        assert not [s for s in statements if "WHERE community_id = " in s]
    assert counts[0] == counts[1]


def test_rows_for_unrelated_edges_are_never_loaded(tmp_path, monkeypatch):
    store, seed = _store_with_unrelated_files(tmp_path / "graph.db", 50, 0)
    loaded = []
    real = GraphStore._row_to_edge
    monkeypatch.setattr(
        GraphStore, "_row_to_edge", lambda self, row: loaded.append(row) or real(self, row),
    )
    try:
        export_graph_data(store, seed_symbols=[seed], depth=1)
    finally:
        store.close()

    # Just the seed's own CONTAINS edge, read once for the seed and once for its file.
    assert 1 <= len(loaded) <= 2


@pytest.mark.parametrize(
    "seed_args",
    [
        {"path_from": "pkg/mod0.py::mod0_0", "path_to": "pkg/mod1.py::mod1_0"},
        {"path_from": "pkg/mod0.py::mod0_0", "path_to": "pkg/mod1.py::mod1_0",
         "seed_symbols": ["pkg/mod2.py::mod2_0"]},
    ],
)
def test_path_queries_still_answer_from_the_whole_graph(tmp_path, seed_args):
    store = _random_graph(tmp_path / "graph.db", 3)
    try:
        _assert_same_view(store, **seed_args)
    finally:
        store.close()


def test_names_past_the_cut_are_read_around_the_seed(tmp_path):
    """Both names are cut to the same text, so the export shows them as one node."""
    store = GraphStore(tmp_path / "graph.db")
    deep = "/".join(["d" * 60] * 5)
    try:
        for name in ("first", "second"):
            store.upsert_node(_node(name, f"{deep}/a.py"))
        store.upsert_node(_node("seed", "seed.py"))
        store.upsert_edge(_edge("CALLS", "seed.py::seed", "first", "seed.py"))
        store.upsert_edge(_edge("CALLS", "second", "seed.py::seed", "seed.py", line=2))
        store.commit()

        seed_args = {"seed_symbols": ["seed.py::seed"], "depth": 1}
        spec = NeighbourhoodSpec.from_args(**seed_args).validate()
        view = visualization._export_seeded(store, spec)
        assert view is not None
        assert json.dumps(view) == json.dumps(_full_read(store, **seed_args))
    finally:
        store.close()

    assert len(view["nodes"]) == 2
    assert len(view["edges"]) == 2


def test_an_edge_end_that_only_resolves_once_cut_takes_the_full_read(tmp_path):
    """Cut to 256 characters, the call's target is the file's name."""
    store = GraphStore(tmp_path / "graph.db")
    deep = "/".join(["d" * 60] * 5) + ".py"
    try:
        store.upsert_node(_node(deep, deep, kind="File"))
        store.upsert_node(_node("inner", deep))
        store.upsert_node(_node("seed", "seed.py"))
        store.upsert_edge(_edge("CALLS", "seed.py::seed", f"{deep}::inner", "seed.py"))
        store.commit()

        seed_args = {"seed_symbols": ["seed.py::seed"], "depth": 1}
        spec = NeighbourhoodSpec.from_args(**seed_args).validate()
        assert visualization._export_seeded(store, spec) is None
        view = export_graph_data(store, **seed_args)
        _assert_same_view(store, **seed_args)
    finally:
        store.close()

    assert len(view["edges"]) == 1


def test_a_control_character_in_an_edge_end_takes_the_full_read(tmp_path):
    """Sanitising turns this edge's target into the seed's name."""
    store = GraphStore(tmp_path / "graph.db")
    try:
        store.upsert_node(_node("seed", "seed.py"))
        store.upsert_node(_node("far", "far.py"))
        store.upsert_edge(_edge("CALLS", "far.py::far", "se\x01ed", "far.py"))
        store.commit()

        view = export_graph_data(store, seed_symbols=["seed.py::seed"], depth=1)
        _assert_same_view(store, seed_symbols=["seed.py::seed"], depth=1)
    finally:
        store.close()

    assert "far.py::far" in view["neighbourhood"]["hops"]


def test_a_legacy_backslash_edge_path_takes_the_full_read(tmp_path):
    """A full read looks this edge's file up as ``__events__/shown`` and finds the marker."""
    store = GraphStore(tmp_path / "graph.db")
    try:
        store.upsert_node(_node("seed", "seed.py"))
        store.upsert_node(_node("evt_shown", "__events__/shown", extra={"virtual": True}))
        store.upsert_edge(_edge("REFERENCES", "seed.py::seed", "evt_shown", "__events__/shown"))
        # Written before paths were stored with forward slashes (#774).
        store._conn.execute("UPDATE edges SET file_path = ?", ("__events__\\shown",))
        store.commit()

        seed_args = {"seed_symbols": ["__events__/shown::evt_shown"], "depth": 1}
        view = export_graph_data(store, **seed_args)
        _assert_same_view(store, **seed_args)
    finally:
        store.close()

    assert "seed.py::seed" in view["neighbourhood"]["hops"]


def test_an_edge_with_an_unresolvable_source_is_not_counted(tmp_path):
    store = GraphStore(tmp_path / "graph.db")
    try:
        store.upsert_node(_node("seed", "seed.py"))
        store.upsert_edge(_edge("CALLS", "ghost_source", "seed", "seed.py"))
        store.upsert_edge(_edge("CALLS", "seed.py::seed", "seed", "seed.py", line=2))
        store.commit()

        view = export_graph_data(store, seed_symbols=["seed.py::seed"], depth=1)
        _assert_same_view(store, seed_symbols=["seed.py::seed"], depth=1)
    finally:
        store.close()

    assert view["neighbourhood"]["total_edges"] == 1


def test_a_neighbourhood_covering_most_of_a_big_graph_takes_the_full_read(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(visualization, "_SEEDED_READ_FALLBACK_NODES", 2)
    store = _random_graph(tmp_path / "graph.db", 5)
    full_reads = []
    real = GraphStore.get_all_edges
    monkeypatch.setattr(
        GraphStore, "get_all_edges", lambda self: full_reads.append(1) or real(self),
    )
    try:
        seed_args = {"seed_files": ["pkg/mod*"], "depth": 3}
        view = export_graph_data(store, **seed_args)
        reads_for_view = len(full_reads)
        assert json.dumps(view) == json.dumps(_full_read(store, **seed_args))
    finally:
        store.close()

    assert reads_for_view == 1


@pytest.mark.parametrize("seed", range(20))
def test_prefix_search_matches_checking_every_candidate(seed):
    """Includes duplicates, names that are prefixes of each other, and an empty prefix."""
    rng = random.Random(seed)
    alphabet = ["a", "b", "/", ".", "::", "é", "\U0010ffff"]
    candidates = ["".join(rng.choices(alphabet, k=rng.randint(0, 5))) for _ in range(60)]
    index = visualization._NameIndex()
    index["key"] = candidates
    prefixes = ["", *candidates, "zz", "a/", "a::", "é"]

    for prefix in prefixes:
        matching = [c for c in candidates if c.startswith(prefix)]
        expected = matching[0] if len(matching) == 1 else None
        assert index.only_under_prefix("key", prefix) == expected, prefix


def test_a_seed_that_matches_nothing_reads_no_edges(tmp_path, monkeypatch):
    store, _seed = _store_with_unrelated_files(tmp_path / "graph.db", 3, 0)
    monkeypatch.setattr(
        GraphStore, "_row_to_edge", lambda *_args: pytest.fail("an edge row was loaded"),
    )
    try:
        with pytest.raises(SeedResolutionError):
            export_graph_data(store, seed_symbols=["no_such_symbol"])
    finally:
        store.close()
