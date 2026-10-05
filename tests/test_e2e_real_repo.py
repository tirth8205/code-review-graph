"""End-to-end pipeline tests against this repository's own source tree.

Every other build test in the suite parses a handful of synthetic files
written into ``tmp_path``. Those pin individual parser rules but cannot catch
the failures that only appear at repository scale: a resolver that silently
drops every edge, a postprocess stage that crashes on a real call graph, an
incremental update that re-parses the world when nothing changed.

This module drives the public pipeline once — ``full_build`` ->
``trace_flows``/``detect_communities``/``rebuild_fts_index`` -> queries — over
the checkout the tests are running from, and asserts facts that must hold of
the real code. A second pass builds ``tests/fixtures`` as its own repository
root to prove the multi-language surface survives a real build rather than a
direct ``CodeParser`` call.

Nothing is written into the checkout: the graph lives in a temporary
directory, ``CRG_HOME`` is redirected, and the build only ever reads the
working tree.

Run just this module with ``pytest -m e2e``; the rest of the suite with
``pytest -m "not e2e"``.
"""

from __future__ import annotations

import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pytest

from code_review_graph.communities import detect_communities, store_communities
from code_review_graph.flows import store_flows, trace_flows
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import (
    collect_all_files,
    full_build,
    incremental_update,
)
from code_review_graph.search import hybrid_search, rebuild_fts_index

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"

# ``collect_all_files`` prefers ``git ls-files`` and only falls back to walking
# the tree. Outside a checkout (an sdist, a vendored copy) that fallback picks
# up whatever build output happens to be lying around, so the inventory-shaped
# assertions below would be measuring the wrong tree. Skip instead of guessing.
# ``.git`` is a directory in a clone and a file in a worktree; both exist.
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not (REPO_ROOT / ".git").exists(),
        reason="needs a git checkout of this repository to build",
    ),
]

PACKAGE = REPO_ROOT / "code_review_graph"
INCREMENTAL_PY = PACKAGE / "incremental.py"
GRAPH_PY = PACKAGE / "graph.py"
CLI_PY = PACKAGE / "cli.py"
TEST_GRAPH_PY = REPO_ROOT / "tests" / "test_graph.py"

# ``file_path`` and the path half of a qualified name are POSIX-normalised
# absolute paths (see ``parser.normalize_file_path``), so build the expected
# identifiers the same way rather than comparing ``Path`` objects.
INCREMENTAL_ID = INCREMENTAL_PY.as_posix()
GRAPH_ID = GRAPH_PY.as_posix()
CLI_ID = CLI_PY.as_posix()
TEST_GRAPH_ID = TEST_GRAPH_PY.as_posix()

# Languages whose fixtures must survive a real build with at least one
# language-specific relationship edge, mapped to the language value the parser
# stores on the File node. ``.ts`` fixtures are stored as ``typescript``;
# ``.tsx`` is a separate grammar with its own language name.
LANGUAGE_EDGE_EXPECTATIONS = [
    ("Python", "python"),
    ("TypeScript", "typescript"),
    ("Go", "go"),
    ("Java", "java"),
    ("C#", "csharp"),
    ("Rust", "rust"),
    ("Ruby", "ruby"),
    ("PHP", "php"),
    ("Kotlin", "kotlin"),
]

RELATIONSHIP_EDGE_KINDS = ("CALLS", "IMPORTS_FROM")


@dataclass
class BuiltGraph:
    """One completed pipeline run, shared by every assertion in this module."""

    store: GraphStore
    root: Path
    build: dict[str, Any]
    build_seconds: float
    flows: list[dict[str, Any]]
    communities: list[dict[str, Any]]
    fts_rows: int
    postprocess_seconds: float


def _build(root: Path, db_dir: Path) -> Iterator[BuiltGraph]:
    """Run ``full_build`` then the postprocess stages over *root*."""
    store = GraphStore(db_dir / "graph.db")
    started = time.perf_counter()
    build = full_build(root, store)
    build_seconds = time.perf_counter() - started

    started = time.perf_counter()
    flows = trace_flows(store)
    store_flows(store, flows)
    communities = detect_communities(store)
    store_communities(store, communities)
    fts_rows = rebuild_fts_index(store)
    postprocess_seconds = time.perf_counter() - started

    try:
        yield BuiltGraph(
            store=store,
            root=root,
            build=build,
            build_seconds=build_seconds,
            flows=flows,
            communities=communities,
            fts_rows=fts_rows,
            postprocess_seconds=postprocess_seconds,
        )
    finally:
        store.close()


@pytest.fixture(scope="module")
def _e2e_env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Isolate per-user state and pin the parse executor for the whole module.

    ``conftest.isolated_crg_home`` is function-scoped, so a module-scoped
    build can be set up before it runs; redirect ``CRG_HOME`` here too rather
    than depend on fixture ordering.

    ``CRG_PARSE_EXECUTOR=thread`` is the documented override for hosts where
    ``ProcessPoolExecutor`` workers are a problem (the same knob
    ``test_identity_retry.py`` uses). Under pytest the process pool re-imports
    the session's ``__main__`` in every spawned worker, which is both slow and
    platform-dependent; threads keep the parallel path under test and make the
    run deterministic.
    """
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("CRG_HOME", str(tmp_path_factory.mktemp("e2e-crg-home")))
    monkeypatch.setenv("CRG_PARSE_EXECUTOR", "thread")
    try:
        yield
    finally:
        monkeypatch.undo()


@pytest.fixture(scope="module")
def repo_graph(
    _e2e_env: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[BuiltGraph]:
    """This repository, built once through the public pipeline."""
    yield from _build(REPO_ROOT, tmp_path_factory.mktemp("e2e-repo-graph"))


@pytest.fixture(scope="module")
def fixtures_graph(
    _e2e_env: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[BuiltGraph]:
    """``tests/fixtures`` built as its own repository root."""
    yield from _build(FIXTURES, tmp_path_factory.mktemp("e2e-fixtures-graph"))


def _table_count(store: GraphStore, table: str) -> int:
    """Count rows in *table* directly.

    Reads ``_conn`` on purpose: the point of the assertion is to compare
    ``get_stats()`` against the tables it summarises, which no public helper
    exposes. ``table`` is a module-level literal, never caller input.
    """
    if table not in ("nodes", "edges"):  # defensive: keeps the SQL a literal
        raise ValueError(f"unsupported table: {table}")
    sql = "SELECT COUNT(*) FROM nodes" if table == "nodes" else "SELECT COUNT(*) FROM edges"
    return int(store._conn.execute(sql).fetchone()[0])


def _has_control_character(text: str) -> bool:
    """True when *text* holds a C0/C1 control character."""
    return any(unicodedata.category(char) == "Cc" for char in text)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def test_full_build_parses_the_repository_without_errors(repo_graph: BuiltGraph) -> None:
    """The real checkout parses cleanly and produces a substantial graph."""
    assert repo_graph.build["errors"] == [], (
        f"files failed to parse: {repo_graph.build['errors'][:5]}"
    )
    # The package alone is ~80 files; a collapse to a handful means the
    # ignore rules or ``git ls-files`` stopped finding the tree.
    assert repo_graph.build["files_parsed"] > 100
    assert repo_graph.build["total_nodes"] > 1000
    assert repo_graph.build["total_edges"] > 1000
    assert repo_graph.build["stale_files_removed"] == 0

    stats = repo_graph.store.get_stats()
    assert "python" in stats.languages
    assert stats.files_count > 100


# ---------------------------------------------------------------------------
# Node identity
# ---------------------------------------------------------------------------


def test_incremental_update_is_a_function_node_in_incremental_py(
    repo_graph: BuiltGraph,
) -> None:
    """``incremental.incremental_update`` exists with the right file path."""
    node = repo_graph.store.get_node(f"{INCREMENTAL_ID}::incremental_update")

    assert node is not None, "incremental_update missing from the built graph"
    assert node.kind == "Function"
    assert node.name == "incremental_update"
    assert node.file_path == INCREMENTAL_ID
    assert node.language == "python"
    assert node.line_start > 0
    assert node.line_end >= node.line_start


def test_graphstore_is_a_class_node_in_graph_py(repo_graph: BuiltGraph) -> None:
    """``graph.GraphStore`` exists with the right file path."""
    node = repo_graph.store.get_node(f"{GRAPH_ID}::GraphStore")

    assert node is not None, "GraphStore missing from the built graph"
    assert node.kind == "Class"
    assert node.name == "GraphStore"
    assert node.file_path == GRAPH_ID
    assert node.language == "python"


def test_no_node_name_contains_a_control_character(repo_graph: BuiltGraph) -> None:
    """Names reaching MCP clients must carry no control characters."""
    offenders = [
        node.qualified_name
        for node in repo_graph.store.get_all_nodes(exclude_files=False)
        if _has_control_character(node.name)
    ]

    assert offenders == [], f"control characters in node names: {offenders[:5]}"


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------


def test_incremental_update_calls_should_ignore(repo_graph: BuiltGraph) -> None:
    """The real call ``_should_ignore(rel_path, ignore_patterns)`` is an edge."""
    source = f"{INCREMENTAL_ID}::incremental_update"
    targets = {
        edge.target_qualified
        for edge in repo_graph.store.get_edges_by_source(source)
        if edge.kind == "CALLS"
    }

    assert f"{INCREMENTAL_ID}::_should_ignore" in targets, (
        "no CALLS edge from incremental_update to _should_ignore; "
        f"resolved call targets were {sorted(targets)[:10]}"
    )


def test_repository_local_python_import_resolves_to_the_imported_file(
    repo_graph: BuiltGraph,
) -> None:
    """``from code_review_graph.graph import GraphStore`` becomes a file edge.

    Only absolute repository-local imports resolve to a file today: the
    parser reads the first ``dotted_name`` child of an
    ``import_from_statement``, which for ``from .graph import GraphStore`` is
    the imported *symbol*, not the module. So the package's own relative
    imports do not produce ``incremental.py -> graph.py``; ``tests/`` imports
    the package absolutely and does. See the PR notes.
    """
    targets = {
        edge.target_qualified
        for edge in repo_graph.store.get_edges_by_source(TEST_GRAPH_ID)
        if edge.kind == "IMPORTS_FROM"
    }

    assert GRAPH_ID in targets, (
        "no IMPORTS_FROM edge from tests/test_graph.py to code_review_graph/graph.py"
    )


def test_tests_for_graphstore_includes_the_graph_test_module(
    repo_graph: BuiltGraph,
) -> None:
    """Test coverage for ``GraphStore`` reaches ``tests/test_graph.py``."""
    covering = repo_graph.store.get_transitive_tests(
        f"{GRAPH_ID}::GraphStore", max_depth=1,
    )

    assert covering, "GraphStore has no covering tests at all"
    from_test_graph = [t for t in covering if t["file_path"] == TEST_GRAPH_ID]
    assert from_test_graph, (
        "tests_for(GraphStore) returned no test from tests/test_graph.py; "
        f"covering files were {sorted({t['file_path'] for t in covering})[:5]}"
    )
    assert all(t["name"] for t in from_test_graph)


def test_impact_radius_of_graph_module_reaches_its_test_module(
    repo_graph: BuiltGraph,
) -> None:
    """Changing ``graph.py`` impacts the modules that import it.

    Impact follows ``IMPORTS_FROM`` from the dependency back to its
    dependents, so the importers are the blast radius and the modules
    ``graph.py`` itself imports are not.
    """
    # The default node cap (500) truncates on a graph this size; raise it so
    # the assertion is about reachability, not about the cap.
    radius = repo_graph.store.get_impact_radius(
        [GRAPH_ID], max_depth=1, max_nodes=20_000,
    )

    impacted = set(radius["impacted_files"])
    assert TEST_GRAPH_ID in impacted, (
        "impact radius of graph.py did not reach tests/test_graph.py"
    )
    assert GRAPH_ID not in impacted, "the changed file must not be its own dependent"
    assert radius["changed_nodes"], "no seed nodes for a file that is in the graph"
    assert radius["impact_scores"][TEST_GRAPH_ID] > 0


# ---------------------------------------------------------------------------
# Postprocess: communities, flows, search
# ---------------------------------------------------------------------------


def test_communities_partition_the_graph(repo_graph: BuiltGraph) -> None:
    """Detection yields several communities and never double-books a node."""
    communities = repo_graph.communities

    assert len(communities) > 1, f"expected >1 community, got {len(communities)}"

    seen: dict[str, str] = {}
    duplicates: list[tuple[str, str, str]] = []
    for community in communities:
        assert community["size"] > 0
        for member in community["members"]:
            if member in seen:
                duplicates.append((member, seen[member], community["name"]))
            else:
                seen[member] = community["name"]

    assert duplicates == [], f"nodes in two communities: {duplicates[:5]}"


def test_a_flow_starts_at_a_cli_entry_point(repo_graph: BuiltGraph) -> None:
    """``cli.main`` is detected as an entry point and traced into a flow."""
    cli_flows = {
        flow["entry_point"]: flow
        for flow in repo_graph.flows
        if flow["entry_point"].startswith(f"{CLI_ID}::")
    }

    assert cli_flows, "no execution flow starts in code_review_graph/cli.py"
    main_flow = cli_flows.get(f"{CLI_ID}::main")
    assert main_flow is not None, (
        f"cli.py::main is not a flow entry point; found {sorted(cli_flows)}"
    )
    assert main_flow["node_count"] > 1, "the CLI flow reached nothing"
    assert main_flow["criticality"] >= 0.0


def test_hybrid_search_ranks_impact_radius_nodes_first(repo_graph: BuiltGraph) -> None:
    """A natural-language query surfaces the impact-radius implementation."""
    assert repo_graph.fts_rows > 0

    results = hybrid_search(repo_graph.store, "impact radius", limit=10)

    assert len(results) == 10, f"expected a full page of hits, got {len(results)}"
    on_topic = [r for r in results if "impact_radius" in r["name"]]
    assert len(on_topic) >= 5, (
        f"only {len(on_topic)}/10 hits mention impact_radius: "
        f"{[r['name'] for r in results]}"
    )
    assert any(
        r["name"] == "get_impact_radius" and r["file_path"] == GRAPH_ID
        for r in results
    ), f"graph.get_impact_radius missing from the top 10: {[r['name'] for r in results]}"


# ---------------------------------------------------------------------------
# Stats and incremental re-run
# ---------------------------------------------------------------------------


def test_stats_totals_equal_the_table_counts(repo_graph: BuiltGraph) -> None:
    """``get_stats`` summarises the tables it reports on, exactly."""
    store = repo_graph.store
    stats = store.get_stats()

    assert stats.total_nodes == _table_count(store, "nodes")
    assert stats.total_edges == _table_count(store, "edges")
    assert sum(stats.nodes_by_kind.values()) == stats.total_nodes
    assert sum(stats.edges_by_kind.values()) == stats.total_edges
    assert stats.files_count == stats.nodes_by_kind["File"]
    assert stats.last_updated


def test_second_update_with_no_changes_is_a_no_op(repo_graph: BuiltGraph) -> None:
    """Re-running the update over an unchanged tree changes nothing.

    The working tree has not moved since ``full_build``, so content
    reconciliation must find no mismatch, nothing must be re-parsed, and no
    file may be reported stale.
    """
    store = repo_graph.store
    before = store.get_stats()

    result = incremental_update(repo_graph.root, store, changed_files=[])

    assert result["changed_files"] == []
    assert result["dependent_files"] == []
    assert result["files_updated"] == 0
    assert result["stale_files_removed"] == 0
    assert result["errors"] == []

    after = store.get_stats()
    assert (after.total_nodes, after.total_edges, after.files_count) == (
        before.total_nodes,
        before.total_edges,
        before.files_count,
    )
    assert after.nodes_by_kind == before.nodes_by_kind
    assert after.edges_by_kind == before.edges_by_kind


# ---------------------------------------------------------------------------
# Multi-language pass over tests/fixtures
# ---------------------------------------------------------------------------


def test_every_supported_fixture_file_produces_at_least_one_node(
    fixtures_graph: BuiltGraph,
) -> None:
    """Nothing the collector accepts may parse into an empty graph.

    ``collect_all_files`` already drops files whose extension maps to no
    language, so what it returns is exactly the supported set; every one of
    them must contribute rows.
    """
    files = collect_all_files(FIXTURES)
    assert len(files) > 40, f"fixture collection returned only {len(files)} files"

    empty = [
        rel for rel in files
        if not fixtures_graph.store.get_nodes_by_file((FIXTURES / rel).as_posix())
    ]

    assert empty == [], f"supported fixture files produced no nodes: {sorted(empty)}"
    assert fixtures_graph.build["errors"] == [], (
        f"fixture files failed to parse: {fixtures_graph.build['errors'][:5]}"
    )


@pytest.mark.parametrize(
    ("label", "language"),
    LANGUAGE_EDGE_EXPECTATIONS,
    ids=[language for _, language in LANGUAGE_EDGE_EXPECTATIONS],
)
def test_language_fixtures_produce_relationship_edges(
    fixtures_graph: BuiltGraph, label: str, language: str,
) -> None:
    """Each major language yields a CALLS or IMPORTS_FROM edge from a build."""
    store = fixtures_graph.store
    language_files = {
        node.file_path
        for node in store.get_all_nodes(exclude_files=False)
        if node.kind == "File" and node.language == language
    }
    assert language_files, f"no {label} File node in the fixtures build"

    by_kind: dict[str, int] = defaultdict(int)
    for edge in store.get_all_edges():
        if edge.file_path in language_files:
            by_kind[edge.kind] += 1

    relationship_edges = sum(by_kind[kind] for kind in RELATIONSHIP_EDGE_KINDS)
    assert relationship_edges > 0, (
        f"{label} fixtures produced no CALLS/IMPORTS_FROM edge; "
        f"edge kinds seen were {dict(by_kind)}"
    )
