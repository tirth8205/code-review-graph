"""Tests for seeded k-hop neighbourhood views and the path-between query.

The whole-repo view stops being useful well before a real repository stops
growing: the renderer falls back to one bubble per community past 3000 nodes
or 9000 edges.  These tests pin the alternative — a payload that carries only
the k-hop neighbourhood of a seed, and nothing else.

The hop assertions here deliberately recompute the expected node set with a
breadth-first search written inside the test, over the *exported* edge list,
rather than reusing anything from ``code_review_graph.neighbourhood``.  A test
that called the implementation to compute its own expectation would pass for
any implementation.
"""

from __future__ import annotations

import json

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo

# ---------------------------------------------------------------------------
# Fixture graph
# ---------------------------------------------------------------------------
#
#   a.py::f_a --CALLS--> b.py::f_b --CALLS--> c.py::f_c --CALLS--> d.py::f_d
#                            |                                        |
#                            +--CALLS--> b.py::f_b2                   |
#                                                                     v
#                                                            e.py::f_e
#   c.py::C_child --INHERITS--> d.py::C_base
#   b.py --IMPORTS_FROM--> c.py
#   z.py::f_z                       (island: reachable from nothing)
#
# Every file CONTAINS its own symbols.  CONTAINS is structural, not a semantic
# hop: a File must not act as a shortcut that drags in every sibling symbol.


def _fn(name: str, file_path: str, kind: str = "Function") -> NodeInfo:
    return NodeInfo(
        kind=kind,
        name=name,
        file_path=file_path,
        line_start=1,
        line_end=5,
        language="python",
        parent_name=None,
        params=None,
        return_type=None,
        modifiers=None,
        is_test=kind == "Test",
        extra={},
    )


def _file(file_path: str) -> NodeInfo:
    return NodeInfo(
        kind="File",
        name=file_path.rsplit("/", 1)[-1],
        file_path=file_path,
        line_start=1,
        line_end=100,
        language="python",
        parent_name=None,
        params=None,
        return_type=None,
        modifiers=None,
        is_test=False,
        extra={},
    )


def _edge(kind: str, source: str, target: str, file_path: str) -> EdgeInfo:
    return EdgeInfo(
        kind=kind,
        source=source,
        target=target,
        file_path=file_path,
        line=1,
        extra={},
    )


SYMBOLS = {
    "src/a.py": ["f_a"],
    "src/b.py": ["f_b", "f_b2"],
    "src/c.py": ["f_c", "C_child"],
    "src/d.py": ["f_d", "C_base"],
    "src/e.py": ["f_e"],
    "src/z.py": ["f_z"],
}

CALL_EDGES = [
    ("src/a.py::f_a", "src/b.py::f_b"),
    ("src/b.py::f_b", "src/c.py::f_c"),
    ("src/b.py::f_b", "src/b.py::f_b2"),
    ("src/c.py::f_c", "src/d.py::f_d"),
    ("src/d.py::f_d", "src/e.py::f_e"),
]


@pytest.fixture
def chain_store(tmp_path) -> GraphStore:
    store = GraphStore(tmp_path / "chain.db")
    for file_path, symbols in SYMBOLS.items():
        store.upsert_node(_file(file_path))
        for sym in symbols:
            kind = "Class" if sym.startswith("C_") else "Function"
            store.upsert_node(_fn(sym, file_path, kind=kind))
            store.upsert_edge(
                _edge("CONTAINS", file_path, f"{file_path}::{sym}", file_path)
            )
    for source, target in CALL_EDGES:
        store.upsert_edge(_edge("CALLS", source, target, source.split("::")[0]))
    store.upsert_edge(
        _edge("INHERITS", "src/c.py::C_child", "src/d.py::C_base", "src/c.py")
    )
    store.upsert_edge(_edge("IMPORTS_FROM", "src/b.py", "src/c.py", "src/b.py"))
    store.commit()
    return store


# ---------------------------------------------------------------------------
# Independent expectation helpers (no implementation code reused)
# ---------------------------------------------------------------------------


def _reference_hops(edges: list[dict], seeds: list[str], depth: int) -> dict[str, int]:
    """Breadth-first hop distances over non-CONTAINS edges, undirected."""
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        if edge["kind"] == "CONTAINS":
            continue
        adjacency.setdefault(edge["source"], set()).add(edge["target"])
        adjacency.setdefault(edge["target"], set()).add(edge["source"])
    hops = {seed: 0 for seed in seeds}
    frontier = list(seeds)
    for distance in range(1, depth + 1):
        nxt: list[str] = []
        for node in frontier:
            for neighbour in adjacency.get(node, ()):
                if neighbour not in hops:
                    hops[neighbour] = distance
                    nxt.append(neighbour)
        frontier = nxt
    return hops


def _reference_parent_files(edges: list[dict], members: set[str]) -> set[str]:
    """Files that CONTAIN any member, i.e. the structural attachment set."""
    parents = set()
    for edge in edges:
        if edge["kind"] == "CONTAINS" and edge["target"] in members:
            parents.add(edge["source"])
    return parents


# ---------------------------------------------------------------------------
# Hop-exactness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("depth", [0, 1, 2, 3])
def test_neighbourhood_contains_exactly_the_nodes_within_k_hops(chain_store, depth):
    from code_review_graph.visualization import export_graph_data

    full = export_graph_data(chain_store)
    seed = "src/a.py::f_a"

    expected_symbols = set(_reference_hops(full["edges"], [seed], depth))
    expected = expected_symbols | _reference_parent_files(
        full["edges"], expected_symbols
    )

    view = export_graph_data(chain_store, seed_symbols=[seed], depth=depth)
    actual = {node["qualified_name"] for node in view["nodes"]}

    assert actual == expected


def test_hop_labels_match_an_independent_bfs(chain_store):
    from code_review_graph.visualization import export_graph_data

    full = export_graph_data(chain_store)
    seed = "src/a.py::f_a"
    expected = _reference_hops(full["edges"], [seed], 3)

    view = export_graph_data(chain_store, seed_symbols=[seed], depth=3)
    hops = view["neighbourhood"]["hops"]

    for qualified_name, distance in expected.items():
        assert hops[qualified_name] == distance


def test_contains_edges_are_not_a_hop_shortcut(chain_store):
    """f_b2 shares a file with f_b but is 2 CALLS hops from the seed."""
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(
        chain_store, seed_symbols=["src/a.py::f_a"], depth=1
    )
    names = {node["qualified_name"] for node in view["nodes"]}

    assert "src/b.py::f_b" in names
    assert "src/b.py" in names, "the containing file is attached for clustering"
    assert "src/b.py::f_b2" not in names, "CONTAINS must not shortcut to siblings"


def test_rest_of_the_graph_is_absent_from_the_payload(chain_store):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(
        chain_store, seed_symbols=["src/a.py::f_a"], depth=2
    )
    serialized = json.dumps(view)

    assert "f_z" not in serialized, "unreachable island must not be shipped"
    assert "f_e" not in serialized, "hop 4 must not be shipped at depth 2"
    assert len(view["nodes"]) < len(export_graph_data(chain_store)["nodes"])


def test_every_payload_edge_has_both_endpoints_in_the_payload(chain_store):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(
        chain_store, seed_symbols=["src/a.py::f_a"], depth=2
    )
    names = {node["qualified_name"] for node in view["nodes"]}
    for edge in view["edges"]:
        assert edge["source"] in names
        assert edge["target"] in names


def test_unseeded_export_is_unchanged(chain_store):
    from code_review_graph.visualization import export_graph_data

    data = export_graph_data(chain_store)

    assert "neighbourhood" not in data
    expected = len(SYMBOLS) + sum(len(v) for v in SYMBOLS.values())
    assert len(data["nodes"]) == expected  # files + symbols


# ---------------------------------------------------------------------------
# Seed resolution
# ---------------------------------------------------------------------------


def test_seed_accepts_a_bare_symbol_name(chain_store):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(chain_store, seed_symbols=["f_a"], depth=1)

    assert view["neighbourhood"]["seeds"] == ["src/a.py::f_a"]


def test_unresolvable_seed_raises(chain_store):
    from code_review_graph.neighbourhood import SeedResolutionError
    from code_review_graph.visualization import export_graph_data

    with pytest.raises(SeedResolutionError) as excinfo:
        export_graph_data(chain_store, seed_symbols=["no_such_symbol"])

    assert "no_such_symbol" in str(excinfo.value)


def test_seed_from_changed_files_includes_the_files_symbols(chain_store):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(chain_store, seed_files=["src/b.py"], depth=0)
    names = {node["qualified_name"] for node in view["nodes"]}

    assert names == {"src/b.py", "src/b.py::f_b", "src/b.py::f_b2"}
    assert view["neighbourhood"]["seed_kind"] == "file"


def test_seed_file_matching_tolerates_a_repo_relative_prefix(chain_store):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(chain_store, seed_files=["./src/b.py"], depth=0)
    names = {node["qualified_name"] for node in view["nodes"]}

    assert "src/b.py::f_b" in names


def test_seed_from_flow(chain_store):
    from code_review_graph.flows import store_flows
    from code_review_graph.visualization import export_graph_data

    path_qns = ["src/a.py::f_a", "src/b.py::f_b", "src/c.py::f_c"]
    ids = [chain_store.get_node(qn).id for qn in path_qns]
    store_flows(
        chain_store,
        [
            {
                "name": "a to c",
                "entry_point_id": ids[0],
                "path": ids,
                "depth": 3,
                "node_count": 3,
                "file_count": 3,
                "criticality": 1.0,
            }
        ],
    )

    view = export_graph_data(chain_store, seed_flow="a to c", depth=0)
    names = {node["qualified_name"] for node in view["nodes"]}

    assert set(path_qns) <= names
    assert view["neighbourhood"]["seed_kind"] == "flow"


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def test_max_nodes_trims_the_outermost_hop_first(chain_store):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(
        chain_store, seed_symbols=["src/a.py::f_a"], depth=3, max_nodes=4
    )
    hops = view["neighbourhood"]["hops"]

    assert len(view["nodes"]) <= 4
    assert view["neighbourhood"]["truncated"] is True
    assert hops["src/a.py::f_a"] == 0
    assert max(hops[n["qualified_name"]] for n in view["nodes"]) < 3


# ---------------------------------------------------------------------------
# Path between two symbols
# ---------------------------------------------------------------------------


def test_path_between_two_symbols_follows_calls(chain_store):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(
        chain_store, path_from="f_a", path_to="f_e", depth=0
    )
    neighbourhood = view["neighbourhood"]

    assert neighbourhood["path"] == [
        "src/a.py::f_a",
        "src/b.py::f_b",
        "src/c.py::f_c",
        "src/d.py::f_d",
        "src/e.py::f_e",
    ]
    assert neighbourhood["path_directed"] is True
    assert neighbourhood["seed_kind"] == "path"


def test_path_can_traverse_inherits_and_imports(chain_store):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(
        chain_store, path_from="C_child", path_to="C_base", depth=0
    )

    assert view["neighbourhood"]["path"] == [
        "src/c.py::C_child",
        "src/d.py::C_base",
    ]


def test_path_ignores_contains_edges(chain_store):
    """f_b and f_b2 share a file; the only legal link is the CALLS edge."""
    from code_review_graph.neighbourhood import PATH_EDGE_KINDS
    from code_review_graph.visualization import export_graph_data

    assert "CONTAINS" not in PATH_EDGE_KINDS

    view = export_graph_data(
        chain_store, path_from="f_b2", path_to="f_c", depth=0
    )

    assert view["neighbourhood"]["path"] == [
        "src/b.py::f_b2",
        "src/b.py::f_b",
        "src/c.py::f_c",
    ]
    assert view["neighbourhood"]["path_directed"] is False


def test_path_with_no_connection_reports_it(chain_store):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(
        chain_store, path_from="f_a", path_to="f_z", depth=0
    )

    assert view["neighbourhood"]["path"] == []
    assert view["neighbourhood"]["path_error"]


def test_path_nodes_are_seeds_so_context_expands_around_them(chain_store):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(
        chain_store, path_from="f_a", path_to="f_c", depth=1
    )
    hops = view["neighbourhood"]["hops"]

    assert hops["src/b.py::f_b"] == 0
    assert hops["src/b.py::f_b2"] == 1


# ---------------------------------------------------------------------------
# generate_html wiring
# ---------------------------------------------------------------------------


def test_generate_html_neighbourhood_stays_one_file(chain_store, tmp_path):
    from code_review_graph.visualization import generate_html

    out = tmp_path / "graph.html"
    generate_html(chain_store, out, seed_symbols=["f_a"], depth=2)
    content = out.read_text(encoding="utf-8")

    assert '"neighbourhood"' in content
    assert "f_z" not in content
    assert not (tmp_path / "graph.data.js").exists()
    assert "window.__CRG_GRAPH_DATA__" not in content


def test_generate_html_sidecar_is_opt_in(chain_store, tmp_path):
    from code_review_graph.visualization import generate_html

    out = tmp_path / "graph.html"
    generate_html(chain_store, out, seed_symbols=["f_a"], depth=2, sidecar=True)
    content = out.read_text(encoding="utf-8")
    sidecar = tmp_path / "graph.data.js"

    assert sidecar.exists()
    assert '<script src="graph.data.js"></script>' in content
    assert "window.__CRG_GRAPH_DATA__" in content
    assert "src/a.py::f_a" not in content, "payload moved out of the page"
    payload = sidecar.read_text(encoding="utf-8")
    assert payload.startswith("window.__CRG_GRAPH_DATA__ =")
    assert "src/a.py::f_a" in payload


def test_neighbourhood_never_falls_back_to_bubble_aggregation(chain_store, tmp_path):
    """A seeded view is small by construction; auto must not aggregate it."""
    from code_review_graph.visualization import generate_html

    out = tmp_path / "graph.html"
    generate_html(
        chain_store,
        out,
        mode="auto",
        seed_symbols=["f_a"],
        depth=2,
        max_full_nodes=1,
        max_full_edges=1,
    )
    content = out.read_text(encoding="utf-8")

    assert "Drill down" not in content
    assert 'id="filter-panel"' in content


def test_neighbourhood_page_keeps_the_existing_interaction_surface(
    chain_store, tmp_path
):
    from code_review_graph.visualization import generate_html

    out = tmp_path / "graph.html"
    generate_html(chain_store, out, seed_symbols=["f_a"], depth=2)
    content = out.read_text(encoding="utf-8")

    for marker in (
        'id="search"',
        'id="flow-select"',
        'id="btn-community"',
        'id="detail-panel"',
        'id="help-overlay"',
        'data-edge-kind="CALLS"',
        'data-kind="Function"',
    ):
        assert marker in content, marker


def test_neighbourhood_page_has_expand_on_click(chain_store, tmp_path):
    from code_review_graph.visualization import generate_html

    out = tmp_path / "graph.html"
    generate_html(chain_store, out, seed_symbols=["f_a"], depth=2)
    content = out.read_text(encoding="utf-8")

    assert "nbExpand" in content
    assert "render_depth" in content


def test_render_depth_defaults_below_the_payload_depth(chain_store, tmp_path):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(chain_store, seed_symbols=["f_a"], depth=3)

    assert view["neighbourhood"]["render_depth"] == 1
    assert view["neighbourhood"]["depth"] == 3


def test_payload_reports_the_whole_graph_size_for_comparison(chain_store):
    from code_review_graph.visualization import export_graph_data

    full = export_graph_data(chain_store)
    view = export_graph_data(chain_store, seed_symbols=["f_a"], depth=1)

    assert view["neighbourhood"]["total_nodes"] == len(full["nodes"])
    assert view["neighbourhood"]["total_edges"] == len(full["edges"])


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_cli_exposes_the_neighbourhood_flags():
    from code_review_graph.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "visualize",
            "--seed-symbol",
            "f_a",
            "--depth",
            "3",
            "--render-depth",
            "2",
            "--max-nodes",
            "500",
            "--sidecar",
        ]
    )

    assert args.seed_symbol == ["f_a"]
    assert args.depth == 3
    assert args.render_depth == 2
    assert args.max_nodes == 500
    assert args.sidecar is True


def test_cli_exposes_file_flow_and_path_seeds():
    from code_review_graph.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "visualize",
            "--seed-file",
            "src/a.py",
            "--seed-file",
            "src/b.py",
            "--seed-changed",
            "--seed-flow",
            "a to c",
            "--path-from",
            "f_a",
            "--path-to",
            "f_e",
        ]
    )

    assert args.seed_file == ["src/a.py", "src/b.py"]
    assert args.seed_changed is True
    assert args.seed_flow == "a to c"
    assert args.path_from == "f_a"
    assert args.path_to == "f_e"


def test_cli_path_flags_must_come_in_pairs():
    from code_review_graph.neighbourhood import NeighbourhoodSpec

    with pytest.raises(ValueError):
        NeighbourhoodSpec(path_from="f_a").validate()
    with pytest.raises(ValueError):
        NeighbourhoodSpec(path_to="f_e").validate()
    NeighbourhoodSpec(path_from="f_a", path_to="f_e").validate()


def test_negative_depth_is_rejected():
    from code_review_graph.neighbourhood import NeighbourhoodSpec

    with pytest.raises(ValueError):
        NeighbourhoodSpec(symbols=("f_a",), depth=-1).validate()


def test_payload_carries_one_record_per_qualified_name(chain_store):
    """A duplicate key would be double-counted and bound twice by D3.

    ``export_graph_data`` de-duplicates on the raw qualified name but emits
    the ``_sanitize_name``-truncated one, so two real nodes whose paths differ
    only past 256 characters arrive here sharing a key.
    """
    from code_review_graph.neighbourhood import NeighbourhoodSpec, extract

    duplicate = {
        "id": 99,
        "kind": "Function",
        "name": "f_a",
        "qualified_name": "src/a.py::f_a",
        "file_path": "src/a.py",
    }
    data = {
        "nodes": [duplicate, dict(duplicate, id=100)],
        "edges": [],
        "stats": {},
        "flows": [],
        "communities": [],
    }

    view = extract(data, NeighbourhoodSpec(symbols=("src/a.py::f_a",), depth=1))

    assert len(view["nodes"]) == 1
    assert len(view["nodes"]) == len(view["neighbourhood"]["hops"])


def test_truncated_payload_never_exceeds_max_nodes(chain_store):
    from code_review_graph.visualization import export_graph_data

    for cap in (1, 2, 3, 5, 8):
        view = export_graph_data(
            chain_store, seed_symbols=["src/a.py::f_a"], depth=4, max_nodes=cap
        )
        names = [n["qualified_name"] for n in view["nodes"]]
        assert len(names) == len(set(names)), "duplicate node keys"
        assert len(names) <= cap, f"cap {cap} overshot by {len(names) - cap}"
        assert len(names) == len(view["neighbourhood"]["hops"])


def test_any_non_containment_edge_kind_counts_as_a_hop(chain_store):
    """The hop rule is an exclusion, not an allow-list.

    REFERENCES is the second-most-common non-containment edge kind in a real
    graph after CALLS and TESTED_BY. An allow-list that forgot it would drop
    a fifth of the reachable nodes without any test noticing.
    """
    from code_review_graph.visualization import export_graph_data

    chain_store.upsert_edge(
        _edge("REFERENCES", "src/a.py::f_a", "src/z.py::f_z", "src/a.py")
    )
    chain_store.commit()

    view = export_graph_data(
        chain_store, seed_symbols=["src/a.py::f_a"], depth=1
    )
    names = {node["qualified_name"] for node in view["nodes"]}

    assert "src/z.py::f_z" in names


# ---------------------------------------------------------------------------
# --max-nodes is a cap, seeds included
# ---------------------------------------------------------------------------


@pytest.fixture
def wide_store(tmp_path) -> GraphStore:
    """40 files, 4 symbols each, every symbol calling the one file below it.

    Seeding every file (what ``--seed-changed`` does on a large review) gives
    200 seed nodes, so a cap below that has to bite into the seed set itself.
    The call chain gives each file a different degree, which is what the
    ranking is supposed to order by.
    """
    store = GraphStore(tmp_path / "wide.db")
    files = [f"src/m{index:02d}.py" for index in range(40)]
    for file_path in files:
        store.upsert_node(_file(file_path))
        for slot in range(4):
            name = f"fn{slot}"
            store.upsert_node(_fn(name, file_path))
            store.upsert_edge(
                _edge("CONTAINS", file_path, f"{file_path}::{name}", file_path)
            )
    for index, file_path in enumerate(files[:-1]):
        # File 0 calls into everything, so its degree dominates; each later
        # file calls only its successor.
        targets = files[1:] if index == 0 else [files[index + 1]]
        for target in targets:
            store.upsert_edge(
                _edge(
                    "CALLS",
                    f"{file_path}::fn0",
                    f"{target}::fn0",
                    file_path,
                )
            )
    store.commit()
    return store


def _seed_every_file() -> list[str]:
    return [f"src/m{index:02d}.py" for index in range(40)]


def test_max_nodes_caps_a_seed_set_larger_than_the_cap(wide_store):
    """The regression: a seed set bigger than the cap used to ship in full.

    ``--seed-changed`` is the command the flag exists for, and there the seed
    set *is* the large thing.
    """
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(
        wide_store, seed_files=_seed_every_file(), depth=2, max_nodes=50
    )
    block = view["neighbourhood"]

    assert block["seeds_requested"] == 200, "40 files x (1 file + 4 symbols)"
    assert len(view["nodes"]) == 50
    assert len(view["nodes"]) <= block["max_nodes"]
    assert block["seeds_dropped"] == 150
    assert len(block["seeds"]) == 50


@pytest.mark.parametrize("cap", [1, 7, 33, 50, 199, 200, 201, 400])
def test_no_cap_is_ever_exceeded_by_a_large_seed_set(wide_store, cap):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(
        wide_store, seed_files=_seed_every_file(), depth=2, max_nodes=cap
    )
    block = view["neighbourhood"]
    names = [node["qualified_name"] for node in view["nodes"]]

    assert len(names) <= cap, f"cap {cap} overshot by {len(names) - cap}"
    assert len(names) == len(set(names))
    assert block["seeds_dropped"] == 200 - len(block["seeds"])
    assert set(block["seeds"]) <= set(names), "a kept seed is in the payload"


def test_dropped_seeds_are_the_least_connected_ones(wide_store):
    """Ranking, not arbitrary truncation: the busiest seeds survive."""
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(
        wide_store, seed_files=_seed_every_file(), depth=0, max_nodes=10
    )
    kept = set(view["neighbourhood"]["seeds"])

    # m00.py calls all 39 other files, and m00.py::fn0 is the caller, so both
    # outrank the leaf symbols that carry only a CONTAINS edge.
    assert "src/m00.py" in kept
    assert "src/m00.py::fn0" in kept
    assert "src/m39.py::fn3" not in kept, "a degree-1 leaf is not kept over them"


def test_seed_drop_is_deterministic(wide_store):
    from code_review_graph.visualization import export_graph_data

    first, second = (
        export_graph_data(
            wide_store, seed_files=_seed_every_file(), depth=2, max_nodes=37
        )["neighbourhood"]["seeds"]
        for _ in range(2)
    )

    assert first == second


def test_an_uncapped_seed_set_drops_nothing(wide_store):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(
        wide_store, seed_files=_seed_every_file(), depth=2, max_nodes=10_000
    )
    block = view["neighbourhood"]

    assert block["seeds_dropped"] == 0
    assert block["truncated"] is False
    assert len(block["seeds"]) == block["seeds_requested"] == 200


def test_a_symbol_beats_its_containing_file_for_the_last_slot(chain_store):
    """The File is a rendering nicety; the symbol is the answer."""
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(
        chain_store, seed_symbols=["src/a.py::f_a"], depth=0, max_nodes=1
    )
    names = [node["qualified_name"] for node in view["nodes"]]

    assert names == ["src/a.py::f_a"]


def test_path_nodes_outrank_other_seeds_under_a_tight_budget(chain_store):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(
        chain_store,
        path_from="f_a",
        path_to="f_e",
        seed_files=["src/z.py"],
        depth=0,
        max_nodes=5,
    )
    names = {node["qualified_name"] for node in view["nodes"]}

    assert set(view["neighbourhood"]["path"]) <= names, "the answer survives"
    assert len(names) == 5


def test_max_nodes_below_the_path_length_is_rejected(chain_store):
    from code_review_graph.visualization import export_graph_data

    with pytest.raises(ValueError) as excinfo:
        export_graph_data(
            chain_store, path_from="f_a", path_to="f_e", depth=0, max_nodes=3
        )

    assert "max-nodes 3" in str(excinfo.value)
    assert "5" in str(excinfo.value), "says what it would take"


def test_generate_html_reports_the_seed_budget(wide_store, tmp_path):
    from code_review_graph.visualization import generate_html

    report: dict = {}
    generate_html(
        wide_store,
        tmp_path / "graph.html",
        seed_files=_seed_every_file(),
        depth=2,
        max_nodes=50,
        report=report,
    )

    assert report["seeds_requested"] == 200
    assert report["seeds_dropped"] == 150
    assert report["node_count"] == 50
    assert report["truncated"] is True


# ---------------------------------------------------------------------------
# The on-page seed list
# ---------------------------------------------------------------------------


def test_page_does_not_paint_every_seed(wide_store, tmp_path):
    """A --seed-changed run must not spray 40 paths across the graph."""
    from code_review_graph.visualization import generate_html

    out = tmp_path / "graph.html"
    generate_html(wide_store, out, seed_files=_seed_every_file(), depth=1)
    content = out.read_text(encoding="utf-8")

    assert "NB_SEEDS_SHOWN = 3" in content
    assert 'id="nb-seed-toggle"' in content
    assert "more</span>" not in content
    # The full list exists, starts hidden, and is built by the toggle handler.
    assert 'id="nb-seed-list"' in content
    assert 'aria-expanded' in content
    assert "nbSeedListOpen" in content


def test_payload_still_carries_every_seed_for_the_disclosure(wide_store, tmp_path):
    from code_review_graph.visualization import export_graph_data

    view = export_graph_data(wide_store, seed_files=_seed_every_file(), depth=1)

    assert len(view["neighbourhood"]["seed_query"]) == 40


# ---------------------------------------------------------------------------
# Flag combinations that cannot mean anything
# ---------------------------------------------------------------------------


def test_render_depth_above_depth_is_rejected():
    from code_review_graph.neighbourhood import NeighbourhoodSpec

    with pytest.raises(ValueError) as excinfo:
        NeighbourhoodSpec(symbols=("f_a",), depth=2, render_depth=3).validate()

    assert "render-depth 3" in str(excinfo.value)
    NeighbourhoodSpec(symbols=("f_a",), depth=2, render_depth=2).validate()


def test_aggregating_mode_with_a_seed_is_rejected(chain_store, tmp_path):
    from code_review_graph.visualization import generate_html

    for mode in ("community", "file"):
        with pytest.raises(ValueError) as excinfo:
            generate_html(
                chain_store, tmp_path / "g.html", mode=mode, seed_symbols=["f_a"]
            )
        assert mode in str(excinfo.value)
    generate_html(
        chain_store, tmp_path / "g.html", mode="full", seed_symbols=["f_a"]
    )


@pytest.mark.parametrize(
    "argv, message",
    [
        (["visualize", "--seed-changed-base", "main"], "--seed-changed"),
        (["visualize", "--depth", "3"], "seeded view"),
        (["visualize", "--max-nodes", "50"], "seeded view"),
        (["visualize", "--render-depth", "0"], "seeded view"),
        (
            ["visualize", "--format", "json", "--seed-symbol", "f_a"],
            "no effect on --format json",
        ),
        (["visualize", "--format", "graphml", "--depth", "2"], "no effect"),
        (["visualize", "--format", "svg", "--sidecar"], "--sidecar"),
        (
            ["visualize", "--mode", "community", "--seed-symbol", "f_a"],
            "opposite of a seeded neighbourhood",
        ),
    ],
)
def test_meaningless_flag_combinations_are_rejected(argv, message, capsys):
    from code_review_graph.cli import _check_visualize_flags, build_parser

    args = build_parser().parse_args(argv)
    fmt = getattr(args, "format", "html") or "html"

    with pytest.raises(SystemExit) as excinfo:
        _check_visualize_flags(args, fmt)

    assert excinfo.value.code == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["visualize"],
        ["visualize", "--sidecar"],
        ["visualize", "--format", "json"],
        ["visualize", "--seed-symbol", "f_a", "--depth", "3"],
        ["visualize", "--seed-changed", "--seed-changed-base", "main"],
        ["visualize", "--mode", "full", "--seed-file", "a.py"],
        ["visualize", "--path-from", "a", "--path-to", "b", "--max-nodes", "9"],
    ],
)
def test_meaningful_flag_combinations_are_accepted(argv):
    from code_review_graph.cli import _check_visualize_flags, build_parser

    args = build_parser().parse_args(argv)
    _check_visualize_flags(args, getattr(args, "format", "html") or "html")


def test_tuning_flags_default_to_none_so_a_no_op_is_detectable():
    from code_review_graph.cli import build_parser

    args = build_parser().parse_args(["visualize"])

    assert args.depth is None
    assert args.render_depth is None
    assert args.max_nodes is None
    assert args.seed_changed_base is None


def test_the_command_says_how_many_seeds_it_dropped(capsys):
    from code_review_graph.cli import _print_seed_budget

    _print_seed_budget({
        "seeds": 50,
        "seeds_requested": 7614,
        "seeds_dropped": 7564,
        "truncated": True,
        "max_nodes": 50,
        "node_count": 50,
    })
    out = capsys.readouterr().out

    assert "50" in out and "7614" in out and "7564" in out
    assert "dropped" in out


def test_an_untrimmed_run_says_nothing_about_the_budget(capsys):
    from code_review_graph.cli import _print_seed_budget

    _print_seed_budget({
        "seeds": 12,
        "seeds_requested": 12,
        "seeds_dropped": 0,
        "truncated": False,
        "max_nodes": 1500,
        "node_count": 300,
    })

    assert capsys.readouterr().out == ""
