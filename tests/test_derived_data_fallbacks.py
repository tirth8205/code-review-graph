"""Derived data must degrade to a bounded fallback, never to an empty result.

Each test states the contract a caller depends on: communities fall back when
the optional engine cannot be imported, incremental postprocessing receives
paths in the spelling ``nodes.file_path`` uses, and a mutation drops the
resolution context that a later pass would otherwise reuse.
"""

from __future__ import annotations

import sys

import pytest

import code_review_graph.communities as communities
import code_review_graph.incremental as incremental
from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo


def _seed(tmp_path, names=("f1", "f2", "f3")):
    store = GraphStore(str(tmp_path / "graph.db"))
    for i, name in enumerate(names):
        store.upsert_node(
            NodeInfo(
                kind="Function",
                name=name,
                file_path=str(tmp_path / "pkg" / "a.py"),
                line_start=i * 10 + 1,
                line_end=i * 10 + 9,
                language="python",
            )
        )
    store.commit()
    return store


def test_broken_igraph_import_still_yields_file_communities(tmp_path, monkeypatch):
    """A package that imports but cannot load its native library must fall back.

    ``find_spec`` only proves the directory exists, so the availability flag is
    optimistic; the import is where a broken install actually fails.
    """
    store = _seed(tmp_path)
    monkeypatch.setattr(communities, "ig", None)
    monkeypatch.setattr(communities, "IGRAPH_AVAILABLE", True)
    monkeypatch.setitem(sys.modules, "igraph", None)

    result = communities.detect_communities(store)

    assert result
    assert result[0]["description"].startswith("Directory-based community")


@pytest.mark.parametrize("failure", [ImportError, OSError])
def test_any_import_failure_disables_the_engine(monkeypatch, failure):
    """A missing native library raises OSError, which must not escape."""
    class Broken:
        def __getattr__(self, name):
            raise failure("libigraph.so: cannot open shared object file")

    monkeypatch.setattr(communities, "ig", None)
    monkeypatch.setattr(communities, "IGRAPH_AVAILABLE", True)
    monkeypatch.setitem(sys.modules, "igraph", Broken())

    assert communities._load_igraph() is False
    assert communities.IGRAPH_AVAILABLE is False


def test_edge_upsert_drops_the_bare_endpoint_context(tmp_path):
    """Resolution evidence is invalidated by the write that changes it."""
    store = _seed(tmp_path)
    store._bare_endpoint_context_cache = ({}, {}, {}, {})

    store.upsert_edge(
        EdgeInfo(
            kind="IMPORTS_FROM",
            source=str(tmp_path / "pkg" / "b.py"),
            target=str(tmp_path / "pkg" / "c.py"),
            file_path=str(tmp_path / "pkg" / "b.py"),
            line=1,
        )
    )

    assert store._bare_endpoint_context_cache is None


def test_rollback_drops_the_bare_endpoint_context(tmp_path):
    """A rolled-back write must not leave its context behind."""
    store = _seed(tmp_path)
    store._bare_endpoint_context_cache = ({}, {}, {}, {})

    store.rollback()

    assert store._bare_endpoint_context_cache is None


def test_prewarm_skips_languages_without_a_bundled_grammar(monkeypatch):
    """Probing a fallback-parser language spawns a subprocess for nothing."""
    probed: list[str] = []

    class FakeParser:
        _extension_map = {
            ".res": "rescript",
            ".vb": "vbnet",
            ".yaml": "yaml",
            ".properties": "properties",
            ".py": "python",
        }

    def fake_probe(language: str) -> bool:
        probed.append(language)
        return True

    # The helper refuses to run when its qualname was replaced; mirror it.
    fake_probe.__qualname__ = "_parser_load_probe_succeeds"
    monkeypatch.setattr(
        "code_review_graph.parser._parser_load_probe_succeeds",
        fake_probe,
        raising=False,
    )
    monkeypatch.setattr(
        "code_review_graph.parser._PARSER_PROBE_RESULTS", {}, raising=False
    )

    incremental._prewarm_parser_probes(
        FakeParser(), ["a.res", "b.vb", "c.yaml", "d.properties", "e.py"]
    )

    assert probed == ["python"]


def test_custom_language_probes_its_grammar_not_its_alias(monkeypatch):
    """A custom language is a name→grammar mapping; probing the alias fails."""
    from types import SimpleNamespace

    probed: list[str] = []

    class FakeParser:
        _extension_map = {".ml": "mylang", ".py": "python"}
        _custom_languages = {"mylang": SimpleNamespace(grammar="python")}

    def fake_probe(language: str) -> bool:
        probed.append(language)
        return True

    fake_probe.__qualname__ = "_parser_load_probe_succeeds"
    monkeypatch.setattr(
        "code_review_graph.parser._parser_load_probe_succeeds",
        fake_probe,
        raising=False,
    )
    monkeypatch.setattr(
        "code_review_graph.parser._PARSER_PROBE_RESULTS", {}, raising=False
    )

    incremental._prewarm_parser_probes(FakeParser(), ["a.ml"])

    assert probed == ["python"]


def _bare_call_graph(tmp_path):
    """b.py calls a bare ``helper`` that a.py defines, with no import yet."""
    a, b = str(tmp_path / "a.py"), str(tmp_path / "b.py")
    store = GraphStore(str(tmp_path / "graph.db"))
    for path, name in ((a, "helper"), (b, "caller")):
        store.upsert_node(NodeInfo(
            kind="File", name=path, file_path=path,
            line_start=1, line_end=9, language="python",
        ))
        store.upsert_node(NodeInfo(
            kind="Function", name=name, file_path=path,
            line_start=2, line_end=5, language="python",
        ))
    store.upsert_edge(EdgeInfo(
        kind="CALLS", source=f"{b}::caller", target="helper", file_path=b, line=3,
    ))
    store.commit()
    return store, a, b


def _call_target(store):
    return store._conn.execute(
        "SELECT target_qualified FROM edges WHERE kind = 'CALLS'"
    ).fetchone()[0]


def test_a_raw_connection_write_reaches_the_next_resolution_pass(tmp_path):
    """Post-build resolvers write through ``store._conn``, which skips the
    store's cache invalidation. The next pass must still see the new import
    (review of #1139: a cached context resolved nothing here).
    """
    store, a, b = _bare_call_graph(tmp_path)
    try:
        store.resolve_bare_call_targets()
        assert _call_target(store) == "helper"  # no evidence yet

        store._conn.execute(
            "INSERT INTO edges (kind, source_qualified, target_qualified, "
            "file_path, line, extra, updated_at) "
            "VALUES ('IMPORTS_FROM', ?, ?, ?, 1, '{}', 0)",
            (b, a, b),
        )
        store.resolve_bare_call_targets()

        assert _call_target(store) == f"{a}::helper"
    finally:
        store.close()


def test_the_resolution_context_does_not_outlive_its_pass(tmp_path):
    """The context holds every node name; a long-lived store must not keep it."""
    store, _, _ = _bare_call_graph(tmp_path)
    try:
        store.resolve_bare_call_targets()
        store.resolve_bare_tested_by_sources()

        assert store._bare_endpoint_context_cache is None
    finally:
        store.close()


def test_closing_a_store_does_not_run_a_full_collection(tmp_path, monkeypatch):
    """Every MCP tool call closes a store; a gc pass there costs each call."""
    import code_review_graph.graph as graph_module

    def collect():
        raise AssertionError("close() must stay cheap")

    monkeypatch.setattr(graph_module, "release_os_memory", collect)
    store = _seed(tmp_path)

    store.close()


def test_direct_dependents_follow_imports_and_node_edges_only(tmp_path):
    """The one-query dependents lookup keeps the per-node lookup's rules."""
    target = str(tmp_path / "core.py")
    store = GraphStore(str(tmp_path / "graph.db"))
    try:
        store.upsert_node(NodeInfo(
            kind="Class", name="Core", file_path=target,
            line_start=1, line_end=9, language="python",
        ))
        core = f"{target}::Core"
        for name, kind, edge_target in (
            ("importer", "IMPORTS_FROM", target),
            ("caller", "CALLS", core),
            ("child", "INHERITS", core),
            ("impl", "IMPLEMENTS", core),
            ("referrer", "REFERENCES", core),
            ("tester", "TESTED_BY", core),
            ("core", "CALLS", core),
        ):
            path = target if name == "core" else str(tmp_path / f"{name}.py")
            store.upsert_edge(EdgeInfo(
                kind=kind, source=path, target=edge_target, file_path=path, line=1,
            ))
        store.commit()

        assert store.get_direct_dependent_files(target) == {
            str(tmp_path / f"{name}.py")
            for name in ("importer", "caller", "child", "impl")
        }
        assert incremental._single_hop_dependents(store, target) == (
            store.get_direct_dependent_files(target)
        )
    finally:
        store.close()
