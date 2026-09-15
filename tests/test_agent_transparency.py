"""Regression coverage for safe agent-facing graph transparency."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import code_review_graph.main as main_module
import code_review_graph.tools._common as common_module
import code_review_graph.tools.query as query_module
from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.tools.query import query_graph, semantic_search_nodes


def _make_repo(tmp_path: Path, name: str = "repo") -> tuple[Path, GraphStore]:
    root = tmp_path / name
    root.mkdir()
    (root / ".git").mkdir()
    graph_dir = root / ".code-review-graph"
    graph_dir.mkdir()
    return root, GraphStore(graph_dir / "graph.db")


def _set_build_metadata(store: GraphStore, sha: str) -> None:
    store.set_metadata("last_updated", "2026-07-17T12:00:00+00:00")
    store.set_metadata("git_head_sha", sha)
    store.commit()


class TestLiveHeadProvenance:
    def test_reports_commit_match_without_claiming_clean_worktree(
        self, tmp_path, monkeypatch,
    ):
        sha = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
        root, store = _make_repo(tmp_path)
        try:
            _set_build_metadata(store, sha)
        finally:
            store.close()

        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout=sha + "\n")

        monkeypatch.setattr(subprocess, "run", fake_run)
        provenance = common_module.graph_provenance(str(root))

        assert provenance["head_sha"] == sha
        assert provenance["head_matches_build"] is True
        assert "is_stale" not in provenance
        assert calls == [
            (
                ["git", "rev-parse", "--verify", "HEAD"],
                {
                    "capture_output": True,
                    "text": True,
                    "encoding": "utf-8",
                    "errors": "replace",
                    "cwd": str(root),
                    "timeout": 1.0,
                    "stdin": subprocess.DEVNULL,
                    "check": False,
                },
            ),
        ]

    def test_reports_commit_mismatch(self, tmp_path, monkeypatch):
        built_sha = "a" * 40
        head_sha = "b" * 40
        root, store = _make_repo(tmp_path)
        try:
            _set_build_metadata(store, built_sha)
        finally:
            store.close()

        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(
                returncode=0, stdout=head_sha + "\n",
            ),
        )

        provenance = common_module.graph_provenance(str(root))
        assert provenance["head_sha"] == head_sha
        assert provenance["head_matches_build"] is False

    def test_git_timeout_preserves_stored_provenance(self, tmp_path, monkeypatch):
        built_sha = "c" * 40
        root, store = _make_repo(tmp_path)
        try:
            _set_build_metadata(store, built_sha)
        finally:
            store.close()

        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired("git rev-parse", 1.0)

        monkeypatch.setattr(subprocess, "run", timeout)
        provenance = common_module.graph_provenance(str(root))

        assert provenance["built_at_sha"] == built_sha
        assert "head_sha" not in provenance
        assert "head_matches_build" not in provenance


def _seed_callers(
    store: GraphStore,
    root: Path,
    *,
    count: int,
    include_orphan: bool = False,
) -> str:
    target = str(root / "target.py") + "::target"
    store.upsert_node(NodeInfo(
        kind="Function",
        name="target",
        file_path=str(root / "target.py"),
        line_start=1,
        line_end=3,
        language="python",
    ))
    for index in range(count):
        source = str(root / f"caller_{index}.py") + f"::caller_{index}"
        store.upsert_node(NodeInfo(
            kind="Function",
            name=f"caller_{index}",
            file_path=str(root / f"caller_{index}.py"),
            line_start=1,
            line_end=3,
            language="python",
        ))
        store.upsert_edge(EdgeInfo(
            kind="CALLS",
            source=source,
            target=target,
            file_path=str(root / f"caller_{index}.py"),
            line=2,
        ))
    if include_orphan:
        store.upsert_edge(EdgeInfo(
            kind="CALLS",
            source=str(root / "missing.py") + "::missing",
            target=target,
            file_path=str(root / "missing.py"),
            line=2,
        ))
    store.commit()
    return target


class TestBoundedQueryResults:
    @pytest.mark.parametrize("invalid", [0, -1])
    def test_rejects_non_positive_max_results(self, tmp_path, invalid):
        root, store = _make_repo(tmp_path)
        try:
            target = _seed_callers(store, root, count=1)
        finally:
            store.close()

        with pytest.raises(ValueError, match="max_results"):
            query_graph(
                "callers_of", target, str(root), max_results=invalid,
            )

    def test_standard_cap_counts_only_real_results_and_keeps_edges_aligned(
        self, tmp_path,
    ):
        root, store = _make_repo(tmp_path)
        try:
            target = _seed_callers(
                store, root, count=6, include_orphan=True,
            )
        finally:
            store.close()

        result = query_graph(
            "callers_of", target, str(root), max_results=2,
        )

        assert result["result_count"] == 6
        assert result["results_omitted"] == 4
        assert len(result["results"]) == 2
        returned = {node["qualified_name"] for node in result["results"]}
        assert {edge["source"] for edge in result["edges"]} <= returned

    def test_minimal_cap_uses_smaller_of_requested_limit_and_five(self, tmp_path):
        root, store = _make_repo(tmp_path)
        try:
            target = _seed_callers(store, root, count=6)
        finally:
            store.close()

        result = query_graph(
            "callers_of",
            target,
            str(root),
            detail_level="minimal",
            max_results=2,
        )

        assert result["result_count"] == 6
        assert result["results_omitted"] == 4
        assert len(result["results"]) == 2

    def test_streams_edges_instead_of_materializing_the_full_edge_list(
        self, tmp_path, monkeypatch,
    ):
        root, store = _make_repo(tmp_path)
        target = _seed_callers(store, root, count=6)

        def materializing_lookup(*args, **kwargs):
            raise AssertionError("query_graph must use the streaming edge API")

        monkeypatch.setattr(store, "get_edges_by_target", materializing_lookup)
        monkeypatch.setattr(
            query_module, "_get_store", lambda _repo_root: (store, root),
        )

        result = query_graph(
            "callers_of", target, str(root), max_results=2,
        )
        assert result["result_count"] == 6


class TestSymbolDisambiguation:
    def test_keeps_candidates_and_adds_ranked_disambiguation(self, tmp_path):
        root, store = _make_repo(tmp_path)
        try:
            for path in (root / "a.py", root / "b.py"):
                store.upsert_node(NodeInfo(
                    kind="Function",
                    name="process",
                    file_path=str(path),
                    line_start=10,
                    line_end=20,
                    language="python",
                ))
            store.commit()
        finally:
            store.close()

        result = query_graph("callers_of", "process", str(root))

        assert result["status"] == "ambiguous"
        assert result["candidates"] == result["disambiguation"]
        assert len(result["disambiguation"]) == 2
        assert "qualified_name" in result["hint"]
        assert all(
            {"qualified_name", "name", "kind", "file_path", "line_start"}
            <= candidate.keys()
            for candidate in result["disambiguation"]
        )

    def test_qualified_tail_filter_runs_before_candidate_limit(self, tmp_path):
        root, store = _make_repo(tmp_path)
        target_path = root / "Target.cs"
        target = f"{target_path}::Details.QueryHandler"
        try:
            for index in range(100):
                store.upsert_node(NodeInfo(
                    kind="Class",
                    name="QueryHandler",
                    parent_name=f"Outer{index}",
                    file_path=str(root / f"Decoy{index}.cs"),
                    line_start=1,
                    line_end=1,
                    language="csharp",
                ))
            store.upsert_node(NodeInfo(
                kind="Class",
                name="QueryHandler",
                parent_name="Details",
                file_path=str(target_path),
                line_start=1,
                line_end=5,
                language="csharp",
            ))
            store.commit()
            assert [
                node.qualified_name
                for node in store.search_nodes_by_qualified_tail(
                    "Details.QueryHandler", limit=100,
                )
            ] == [target]
            assert store.count_nodes_by_qualified_tail(
                "Details.QueryHandler",
            ) == 1
        finally:
            store.close()

        result = query_graph(
            "children_of", "Details.QueryHandler", str(root),
        )

        assert result["status"] == "ok"
        assert result["target"] == target
        assert result["results"] == []

    def test_java_fqn_requires_matching_language_and_class(self, tmp_path):
        root, store = _make_repo(tmp_path)
        try:
            java_target = str(root / "OrderHandler.java") + "::OrderHandler.process"
            java_caller = str(root / "OrderRouter.java") + "::route"
            store.upsert_node(NodeInfo(
                kind="Function",
                name="process",
                parent_name="OrderHandler",
                file_path=str(root / "OrderHandler.java"),
                line_start=10,
                line_end=20,
                language="java",
            ))
            store.upsert_node(NodeInfo(
                kind="Function",
                name="route",
                file_path=str(root / "OrderRouter.java"),
                line_start=1,
                line_end=5,
                language="java",
            ))
            store.upsert_node(NodeInfo(
                kind="Function",
                name="process",
                file_path=str(root / "worker.py"),
                line_start=1,
                line_end=5,
                language="python",
            ))
            store.upsert_edge(EdgeInfo(
                kind="CALLS",
                source=java_caller,
                target=java_target,
                file_path=str(root / "OrderRouter.java"),
                line=3,
            ))
            store.commit()
        finally:
            store.close()

        result = query_graph(
            "callers_of",
            "com.example.orders.OrderHandler.process",
            str(root),
        )

        assert result["status"] == "ok"
        assert result["target"] == java_target
        assert [node["name"] for node in result["results"]] == ["route"]

    def test_java_fqn_never_falls_back_to_unrelated_global_name(self, tmp_path):
        root, store = _make_repo(tmp_path)
        try:
            store.upsert_node(NodeInfo(
                kind="Function",
                name="process",
                file_path=str(root / "worker.py"),
                line_start=1,
                line_end=5,
                language="python",
            ))
            store.commit()
        finally:
            store.close()

        result = query_graph(
            "callers_of",
            "com.example.MissingHandler.process",
            str(root),
        )
        assert result["status"] == "not_found"

    @pytest.mark.parametrize(
        ("language", "parent_name", "file_name", "method", "target"),
        [
            ("go", "Handler", "handler.go", "Process", "Handler.Process"),
            ("go", "pkg.Handler", "handler.go", "Process", "pkg.Handler.Process"),
            ("python", "Handler", "service.py", "process", "Handler.process"),
            ("kotlin", "Handler", "Handler.kt", "process", "Handler.process"),
            ("typescript", "Handler", "handler.ts", "process", "Handler.process"),
            (
                "csharp", "Details.QueryHandler", "Details.cs", "Handle",
                "Details.QueryHandler.Handle",
            ),
        ],
    )
    def test_dotted_target_resolves_for_every_language(
        self, tmp_path, language, parent_name, file_name, method, target,
    ):
        """A dotted target is Java-FQN-shaped in every language, so the Java
        guard must not withhold an exact qualified-tail match from Go, Python,
        Kotlin, TypeScript or C# when no Java node competes for it."""
        root, store = _make_repo(tmp_path)
        try:
            store.upsert_node(NodeInfo(
                kind="Function",
                name=method,
                parent_name=parent_name,
                file_path=str(root / file_name),
                line_start=1,
                line_end=5,
                language=language,
            ))
            store.commit()
        finally:
            store.close()

        result = query_graph("callers_of", target, str(root))

        assert result["status"] == "ok"
        assert result["target"] == f"{root / file_name}::{parent_name}.{method}"

    def test_java_fqn_not_displaced_by_same_tail_in_another_language(
        self, tmp_path,
    ):
        """The dotted-tail lookup is not language-filtered, so a non-Java node
        whose qualified tail happens to equal a Java FQN must not silently win
        over a genuine Java match."""
        root, store = _make_repo(tmp_path)
        try:
            store.upsert_node(NodeInfo(
                kind="Function",
                name="process",
                parent_name="OrderHandler",
                file_path=str(root / "com" / "example" / "OrderHandler.java"),
                line_start=10,
                line_end=20,
                language="java",
            ))
            store.upsert_node(NodeInfo(
                kind="Function",
                name="process",
                parent_name="com.example.OrderHandler",
                file_path=str(root / "Impostor.cs"),
                line_start=1,
                line_end=5,
                language="csharp",
            ))
            store.commit()
        finally:
            store.close()

        result = query_graph(
            "callers_of",
            "com.example.OrderHandler.process",
            str(root),
        )

        # The Java match wins outright: the unfiltered tail lookup must not
        # displace it, exactly as before the dotted-tail lookup existed.
        assert result["status"] == "ok"
        assert result["target"] == (
            f"{root / 'com' / 'example' / 'OrderHandler.java'}"
            "::OrderHandler.process"
        )

    def test_dotted_ambiguity_count_survives_the_candidate_cap(self, tmp_path):
        """``candidate_count`` must describe the whole matching population, not
        the capped slice, or ``candidates_truncated`` silently under-reports."""
        root, store = _make_repo(tmp_path)
        over_cap = query_module._MAX_DOTTED_TARGET_CANDIDATES + 1
        try:
            for index in range(over_cap):
                store.upsert_node(NodeInfo(
                    kind="Function",
                    name="Process",
                    parent_name="Handler",
                    file_path=str(root / f"pkg{index}" / "handler.go"),
                    line_start=1,
                    line_end=5,
                    language="go",
                ))
            store.commit()
        finally:
            store.close()

        result = query_graph("callers_of", "Handler.Process", str(root))

        assert result["status"] == "ambiguous"
        assert f"matches {over_cap} node(s)" in result["summary"]
        assert result["candidates_truncated"] is True

    def test_duplicate_java_class_method_stays_ambiguous(self, tmp_path):
        root, store = _make_repo(tmp_path)
        try:
            for directory in ("v1", "v2"):
                store.upsert_node(NodeInfo(
                    kind="Function",
                    name="process",
                    parent_name="OrderHandler",
                    file_path=str(root / directory / "OrderHandler.java"),
                    line_start=1,
                    line_end=5,
                    language="java",
                ))
            store.commit()
        finally:
            store.close()

        result = query_graph(
            "callers_of",
            "com.example.OrderHandler.process",
            str(root),
        )
        assert result["status"] == "ambiguous"
        assert len(result["disambiguation"]) == 2

    def test_file_summary_path_does_not_enter_symbol_resolution(self, tmp_path):
        root, store = _make_repo(tmp_path)
        try:
            store.upsert_node(NodeInfo(
                kind="Function",
                name="handle",
                file_path=str(root / "src" / "service.v2.py"),
                line_start=1,
                line_end=5,
                language="python",
            ))
            store.commit()
        finally:
            store.close()

        result = query_graph(
            "file_summary", "src/service.v2.py", str(root),
        )
        assert result["status"] == "ok"
        assert [node["name"] for node in result["results"]] == ["handle"]


def test_semantic_search_minimal_reports_hidden_returned_results(tmp_path):
    root, store = _make_repo(tmp_path)
    try:
        for index in range(10):
            store.upsert_node(NodeInfo(
                kind="Function",
                name=f"do_thing_{index}",
                file_path=str(root / f"module_{index}.py"),
                line_start=1,
                line_end=5,
                language="python",
            ))
        store.commit()
    finally:
        store.close()

    result = semantic_search_nodes(
        "do_thing", limit=10, repo_root=str(root), detail_level="minimal",
    )
    assert len(result["results"]) == 5
    assert result["results_omitted"] == 5


def test_impact_minimal_reports_nodes_omitted(tmp_path, monkeypatch):
    store = GraphStore(tmp_path / "impact.db")
    seed = "/seed.py::seed"
    store.upsert_node(NodeInfo(
        kind="Function", name="seed", file_path="/seed.py",
        line_start=1, line_end=3, language="python",
    ))
    for index in range(5):
        impacted = f"/impacted_{index}.py::impacted_{index}"
        store.upsert_node(NodeInfo(
            kind="Function", name=f"impacted_{index}",
            file_path=f"/impacted_{index}.py", line_start=1,
            line_end=3, language="python",
        ))
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=impacted, target=seed,
            file_path=f"/impacted_{index}.py", line=1,
        ))
    store.commit()

    monkeypatch.setattr(
        query_module, "_get_store", lambda _repo_root: (store, tmp_path),
    )
    monkeypatch.setattr(
        query_module,
        "_resolve_graph_file_paths",
        lambda _store, _root, _files: ["/seed.py"],
    )

    result = query_module.get_impact_radius(
        changed_files=["seed.py"],
        max_results=2,
        repo_root=str(tmp_path),
        detail_level="minimal",
    )

    assert result["truncated"] is True
    assert result["nodes_omitted"] == 3


def test_mcp_query_wrapper_forwards_max_results(monkeypatch):
    captured = {}

    def fake_query_graph(**kwargs):
        captured.update(kwargs)
        return {"status": "ok", "results": []}

    monkeypatch.setattr(main_module, "query_graph", fake_query_graph)
    monkeypatch.setattr(
        main_module, "_resolve_repo_root", lambda repo_root=None: "/repo",
    )
    monkeypatch.setattr(
        main_module, "with_provenance", lambda result, repo_root=None: result,
    )

    tool = getattr(main_module.query_graph_tool, "fn", None)
    underlying = tool or main_module.query_graph_tool
    result = underlying("callers_of", "target", max_results=7)

    assert result["status"] == "ok"
    assert captured["max_results"] == 7


def test_dotted_lookup_survives_batched_file_replacement(tmp_path):
    """#942 and #858: production batch writes must maintain the lookup index."""
    path = str(tmp_path / "handler.py")
    with GraphStore(tmp_path / "graph.db") as store:
        for end in (3, 7):
            node = NodeInfo(
                kind="Function", name="process", parent_name="Handler",
                file_path=path, line_start=1, line_end=end, language="python",
            )
            store.store_file_batch([(path, [node], [], "hash")])
            matches = store.search_nodes_by_qualified_tail("Handler.process")
            assert len(matches) == 1
            assert matches[0].line_end == end
            assert store.count_nodes_by_qualified_tail("Handler.process") == 1
