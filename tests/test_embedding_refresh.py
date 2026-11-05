"""Explicit, provider-scoped embedding refresh and orphan cleanup."""

import asyncio
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_review_graph.embeddings import (
    REFRESH_SKIPPED_WARNING,
    EmbeddingStore,
    embed_all_nodes,
    refresh_embeddings,
)
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.parser import NodeInfo
from code_review_graph.postprocessing import run_post_processing
from code_review_graph.tools.build import _run_postprocess, build_or_update_graph, run_postprocess
from code_review_graph.tools.docs import embed_graph


class _StubProvider:
    dimension = 2

    def __init__(self, name: str = "local:test-model") -> None:
        self.name = name
        self.embedded: list[str] = []

    def embed(self, texts):
        self.embedded.extend(texts)
        return [[float(len(text)), 1.0] for text in texts]

    def embed_query(self, text):
        return [1.0, 0.0]


def _graph_with_function(tmp_path):
    db = tmp_path / "graph.db"
    store = GraphStore(db)
    file_path = str(tmp_path / "module.py")
    store.upsert_node(
        NodeInfo(
            kind="File",
            name=file_path,
            file_path=file_path,
            line_start=1,
            line_end=20,
            language="python",
        )
    )
    store.upsert_node(
        NodeInfo(
            kind="Function",
            name="keep",
            file_path=file_path,
            line_start=1,
            line_end=2,
            language="python",
        )
    )
    store.commit()
    return store, file_path


class TestOrphanCleanup:
    def test_purge_removes_only_vectors_without_graph_nodes(self, tmp_path):
        graph, _ = _graph_with_function(tmp_path)
        provider = _StubProvider()
        with patch("code_review_graph.embeddings.get_provider", return_value=provider):
            embeddings = EmbeddingStore(graph.db_path, provider="local", model="test-model")
        embeddings.embed_nodes(graph.get_all_nodes(exclude_files=False))
        embeddings._conn.execute(
            "INSERT INTO embeddings (qualified_name, vector, text_hash, provider) "
            "VALUES (?, ?, ?, ?)",
            ("deleted.py::ghost", b"\x00" * 8, "old", provider.name),
        )
        embeddings._conn.commit()

        try:
            assert embeddings.purge_orphans() == 1
            remaining = embeddings._conn.execute(
                "SELECT qualified_name FROM embeddings ORDER BY qualified_name",
            ).fetchall()
            assert [row["qualified_name"] for row in remaining] == [
                f"{(tmp_path / 'module.py').as_posix()}::keep",
            ]
        finally:
            embeddings.close()
            graph.close()

    def test_purge_is_safe_without_a_nodes_table(self, tmp_path):
        with patch("code_review_graph.embeddings.get_provider", return_value=None):
            embeddings = EmbeddingStore(tmp_path / "standalone.db")
        try:
            assert embeddings.purge_orphans() == 0
        finally:
            embeddings.close()

    def test_manual_embed_purges_even_when_provider_is_unavailable(self, tmp_path):
        graph, _ = _graph_with_function(tmp_path)
        with patch("code_review_graph.embeddings.get_provider", return_value=None):
            embeddings = EmbeddingStore(graph.db_path)
        embeddings._conn.execute(
            "INSERT INTO embeddings (qualified_name, vector, text_hash, provider) "
            "VALUES ('deleted.py::ghost', ?, 'old', 'unknown')",
            (b"\x00" * 8,),
        )
        embeddings._conn.commit()

        try:
            assert embed_all_nodes(graph, embeddings) == 0
            assert embeddings.count() == 0
        finally:
            embeddings.close()
            graph.close()


class TestEmbeddingNodeEnumeration:
    def test_embeds_non_file_nodes_outside_the_file_inventory(self, tmp_path):
        graph, _ = _graph_with_function(tmp_path)
        graph.upsert_node(NodeInfo(
            kind="Event", name="OrderCreated", file_path="spring:event:OrderCreated",
            line_start=0, line_end=0, language="java", extra={"virtual": True},
        ))
        graph.commit()
        provider = _StubProvider()
        try:
            assert "spring:event:OrderCreated" not in graph.get_all_files()
            with patch("code_review_graph.embeddings.get_provider", return_value=provider):
                with EmbeddingStore(graph.db_path) as embeddings:
                    assert embed_all_nodes(graph, embeddings) == 2
                    assert embeddings.count() == 2
            assert any("OrderCreated" in text for text in provider.embedded)
        finally:
            graph.close()

    @pytest.mark.parametrize("legacy_paths", [False, True])
    def test_manual_embed_reaches_nodes_regardless_of_stored_path(self, tmp_path, legacy_paths):
        graph, _ = _graph_with_function(tmp_path)
        if legacy_paths:
            graph._conn.execute("UPDATE nodes SET file_path = replace(file_path, '/', ?)", ("\\",))
            graph.commit()
        provider = _StubProvider()
        try:
            with patch("code_review_graph.embeddings.get_provider", return_value=provider):
                with EmbeddingStore(graph.db_path) as embeddings:
                    assert embed_all_nodes(graph, embeddings) == 1
                    assert embeddings.count() == 1
                    assert embeddings.search("keep")[0][0].endswith("::keep")
                    assert embed_all_nodes(graph, embeddings) == 0
            assert len(provider.embedded) == 1
        finally:
            graph.close()

    @pytest.mark.parametrize("legacy_paths", [False, True])
    def test_refresh_reaches_changed_nodes_regardless_of_stored_path(self, tmp_path, legacy_paths):
        graph, _ = _graph_with_function(tmp_path)
        provider = _StubProvider()
        try:
            with patch("code_review_graph.embeddings.get_provider", return_value=provider):
                with EmbeddingStore(graph.db_path) as embeddings:
                    embeddings.embed_nodes(graph.get_all_nodes())
                graph._conn.execute("UPDATE nodes SET params = '(value)' WHERE kind = 'Function'")
                if legacy_paths:
                    graph._conn.execute(
                        "UPDATE nodes SET file_path = replace(file_path, '/', ?)", ("\\",),
                    )
                graph.commit()
                result = refresh_embeddings(graph, provider="local", model="test-model")
            assert result == {"embedded": 1, "purged": 0}
            assert len(provider.embedded) == 2
            assert "value" in provider.embedded[-1]
        finally:
            graph.close()

    @pytest.mark.parametrize("node_kind", [None, "File", "Function"])
    def test_tool_summary_reflects_whether_any_vectors_exist(self, tmp_path, node_kind):
        graph, _ = _graph_with_function(tmp_path)
        if node_kind != "Function":
            graph._conn.execute("DELETE FROM nodes WHERE kind != 'File'")
        if node_kind is None:
            graph._conn.execute("DELETE FROM nodes")
        graph.commit()
        db_path = graph.db_path
        graph.close()
        provider = _StubProvider()
        with (
            patch("code_review_graph.embeddings.get_provider", return_value=provider),
            patch("code_review_graph.tools.docs.get_db_path", return_value=db_path),
            patch(
                "code_review_graph.tools.docs._get_store",
                side_effect=lambda root: (GraphStore(db_path), tmp_path),
            ),
        ):
            first = embed_graph(str(tmp_path))
            second = embed_graph(str(tmp_path))
        has_vectors = node_kind == "Function"
        assert first["status"] == second["status"] == "ok"
        assert first["newly_embedded"] == int(has_vectors)
        assert second["newly_embedded"] == 0
        for result in (first, second):
            assert result["total_embeddings"] == int(has_vectors)
            assert ("Semantic search is now active." in result["summary"]) == has_vectors


class TestExplicitRefresh:
    def test_never_embedded_graph_skips_without_resolving_provider(self, tmp_path):
        from code_review_graph.embeddings import refresh_embeddings

        graph, _ = _graph_with_function(tmp_path)
        try:
            with patch("code_review_graph.embeddings.get_provider") as get_provider:
                assert (
                    refresh_embeddings(
                        graph,
                        provider="openai",
                        model="costly-model",
                    )
                    is None
                )
            get_provider.assert_not_called()
        finally:
            graph.close()

    def test_exact_provider_refreshes_changed_nodes_and_purges_orphans(self, tmp_path):
        from code_review_graph.embeddings import refresh_embeddings

        graph, file_path = _graph_with_function(tmp_path)
        provider = _StubProvider()
        with patch("code_review_graph.embeddings.get_provider", return_value=provider):
            embeddings = EmbeddingStore(graph.db_path, provider="local", model="test-model")
            embeddings.embed_nodes(graph.get_all_nodes(exclude_files=False))
            embeddings._conn.execute(
                "INSERT INTO embeddings (qualified_name, vector, text_hash, provider) "
                "VALUES ('deleted.py::ghost', ?, 'old', ?)",
                (b"\x00" * 8, provider.name),
            )
            embeddings._conn.commit()
            embeddings.close()

            graph.upsert_node(
                NodeInfo(
                    kind="Function",
                    name="added",
                    file_path=file_path,
                    line_start=4,
                    line_end=5,
                    language="python",
                )
            )
            graph.commit()
            result = refresh_embeddings(
                graph,
                provider="local",
                model="test-model",
            )

        try:
            assert result == {"embedded": 1, "purged": 1}
        finally:
            graph.close()

    def test_provider_identity_mismatch_refuses_migration(self, tmp_path):
        from code_review_graph.embeddings import refresh_embeddings

        graph, _ = _graph_with_function(tmp_path)
        original = _StubProvider("local:original-model")
        with patch("code_review_graph.embeddings.get_provider", return_value=original):
            embeddings = EmbeddingStore(graph.db_path)
            embeddings.embed_nodes(graph.get_all_nodes(exclude_files=False))
            embeddings.close()

        requested = _StubProvider("local:new-model")
        try:
            with patch(
                "code_review_graph.embeddings.get_provider",
                return_value=requested,
            ):
                with pytest.raises(ValueError, match="existing embeddings use"):
                    refresh_embeddings(
                        graph,
                        provider="local",
                        model="new-model",
                    )
            assert requested.embedded == []
        finally:
            graph.close()

    def test_legacy_rows_without_provider_identity_are_refused_precisely(self, tmp_path):
        from code_review_graph.embeddings import refresh_embeddings

        graph, _ = _graph_with_function(tmp_path)
        graph._conn.executescript(
            "CREATE TABLE embeddings ("
            "qualified_name TEXT PRIMARY KEY, vector BLOB NOT NULL, "
            "text_hash TEXT NOT NULL"
            ");"
        )
        graph._conn.execute(
            "INSERT INTO embeddings (qualified_name, vector, text_hash) "
            "VALUES (?, ?, ?)",
            (f"{(tmp_path / 'module.py').as_posix()}::keep", b"\x00" * 8, "old"),
        )
        graph.commit()

        try:
            with patch("code_review_graph.embeddings.get_provider") as get_provider:
                with pytest.raises(ValueError, match="provider identity"):
                    refresh_embeddings(
                        graph,
                        provider="local",
                        model="test-model",
                    )
            get_provider.assert_not_called()
        finally:
            graph.close()


def _graph_without_vectors(tmp_path, *, empty_table: bool):
    """A graph with nodes and no vectors: no embeddings table, or an empty one."""
    graph, _ = _graph_with_function(tmp_path)
    if empty_table:
        # Any EmbeddingStore creates the table; list_graph_stats does this too.
        with patch("code_review_graph.embeddings.get_provider", return_value=None):
            EmbeddingStore(graph.db_path).close()
    return graph


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=T", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _git_repo(tmp_path: Path, monkeypatch) -> Path:
    """A committed one-file git repository, parsed serially."""
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "app.py").write_text("def hello():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


class TestRefreshWithoutVectors:
    """An explicit refresh on a never-embedded graph reports that it embedded nothing."""

    @pytest.mark.parametrize("empty_table", [False, True])
    @pytest.mark.parametrize("level", ["none", "minimal", "full"])
    def test_build_postprocess_reports_skip_at_every_level(self, tmp_path, level, empty_table):
        graph = _graph_without_vectors(tmp_path, empty_table=empty_table)
        try:
            with patch("code_review_graph.embeddings.get_provider") as get_provider:
                result: dict = {}
                warnings = _run_postprocess(
                    graph,
                    result,
                    level,
                    embedding_provider="local",
                    embedding_model="test-model",
                )
            get_provider.assert_not_called()
            assert result["embeddings_refresh_skipped"] is True
            assert "embeddings_refreshed" not in result
            assert REFRESH_SKIPPED_WARNING in warnings
        finally:
            graph.close()

    @pytest.mark.parametrize("empty_table", [False, True])
    def test_shared_postprocessing_reports_skip(self, tmp_path, empty_table):
        graph = _graph_without_vectors(tmp_path, empty_table=empty_table)
        try:
            with patch("code_review_graph.embeddings.get_provider") as get_provider:
                result = run_post_processing(
                    graph,
                    embedding_provider="local",
                    embedding_model="test-model",
                )
            get_provider.assert_not_called()
            assert result["embeddings_refresh_skipped"] is True
            assert "embeddings_refreshed" not in result
            assert REFRESH_SKIPPED_WARNING in result["warnings"]
        finally:
            graph.close()

    def test_run_postprocess_reports_skip(self, tmp_path):
        graph = _graph_without_vectors(tmp_path, empty_table=False)
        db_path = graph.db_path
        graph.close()
        with (
            patch("code_review_graph.embeddings.get_provider") as get_provider,
            patch(
                "code_review_graph.tools.build._get_store",
                side_effect=lambda root: (GraphStore(db_path), tmp_path),
            ),
        ):
            result = run_postprocess(
                repo_root=str(tmp_path),
                embedding_provider="local",
                embedding_model="test-model",
            )
        get_provider.assert_not_called()
        assert result["embeddings_refresh_skipped"] is True
        assert REFRESH_SKIPPED_WARNING in result["warnings"]

    def test_build_result_tells_the_caller_nothing_was_embedded(self, tmp_path, monkeypatch):
        repo = _git_repo(tmp_path, monkeypatch)

        with patch("code_review_graph.embeddings.get_provider") as get_provider:
            result = build_or_update_graph(
                full_rebuild=True,
                repo_root=str(repo),
                embedding_provider="local",
                embedding_model="test-model",
            )
        get_provider.assert_not_called()
        assert result["status"] == "ok"
        assert result["embeddings_refresh_skipped"] is True
        assert "embeddings_refreshed" not in result
        assert REFRESH_SKIPPED_WARNING in result["warnings"]

    def test_update_with_no_changes_still_reports_the_missing_index(self, tmp_path, monkeypatch):
        repo = _git_repo(tmp_path, monkeypatch)

        with patch("code_review_graph.embeddings.get_provider") as get_provider:
            build_or_update_graph(full_rebuild=True, repo_root=str(repo))
            default = build_or_update_graph(repo_root=str(repo))
            explicit = build_or_update_graph(
                repo_root=str(repo),
                embedding_provider="local",
                embedding_model="test-model",
            )
        get_provider.assert_not_called()
        assert default["build_type"] == explicit["build_type"] == "incremental"
        assert default["files_updated"] == explicit["files_updated"] == 0
        assert "embeddings_refresh_skipped" not in default
        assert "warnings" not in default
        assert explicit["status"] == "ok"
        assert explicit["embeddings_refresh_skipped"] is True
        assert explicit["warnings"] == [REFRESH_SKIPPED_WARNING]

    def test_update_with_no_changes_on_an_embedded_graph_reports_nothing(
        self, tmp_path, monkeypatch,
    ):
        repo = _git_repo(tmp_path, monkeypatch)
        build_or_update_graph(full_rebuild=True, repo_root=str(repo))
        graph = GraphStore(get_db_path(repo))
        try:
            with patch(
                "code_review_graph.embeddings.get_provider",
                return_value=_StubProvider(),
            ):
                with EmbeddingStore(graph.db_path, provider="local", model="test-model") as store:
                    assert embed_all_nodes(graph, store) > 0
        finally:
            graph.close()

        with patch("code_review_graph.embeddings.get_provider") as get_provider:
            result = build_or_update_graph(
                repo_root=str(repo),
                embedding_provider="local",
                embedding_model="test-model",
            )
        get_provider.assert_not_called()
        assert result["files_updated"] == 0
        assert "embeddings_refresh_skipped" not in result
        assert "warnings" not in result


class TestRefreshWiring:
    def test_shared_postprocessing_is_default_off(self, tmp_path):
        graph, _ = _graph_with_function(tmp_path)
        try:
            with patch(
                "code_review_graph.embeddings.refresh_embeddings",
            ) as refresh:
                result = run_post_processing(graph)
            refresh.assert_not_called()
            assert "embeddings_refresh_skipped" not in result
            assert "warnings" not in result
        finally:
            graph.close()

    def test_shared_postprocessing_refresh_is_explicit_and_fail_soft(self, tmp_path):
        graph, _ = _graph_with_function(tmp_path)
        try:
            with patch(
                "code_review_graph.embeddings.refresh_embeddings",
                return_value={"embedded": 3, "purged": 2},
            ) as refresh:
                result = run_post_processing(
                    graph,
                    embedding_provider="local",
                    embedding_model="test-model",
                )
            refresh.assert_called_once_with(
                graph,
                provider="local",
                model="test-model",
            )
            assert result["embeddings_refreshed"] == 3
            assert result["embeddings_purged"] == 2
            assert "embeddings_refresh_skipped" not in result

            with patch(
                "code_review_graph.embeddings.refresh_embeddings",
                side_effect=RuntimeError("provider unavailable offline"),
            ):
                failed = run_post_processing(
                    graph,
                    embedding_provider="local",
                    embedding_model="test-model",
                )
            assert any("provider unavailable offline" in warning for warning in failed["warnings"])
        finally:
            graph.close()

    def test_build_postprocess_is_default_off_and_explicit_at_every_level(self, tmp_path):
        graph, _ = _graph_with_function(tmp_path)
        try:
            with patch(
                "code_review_graph.embeddings.refresh_embeddings",
                return_value={"embedded": 1, "purged": 1},
            ) as refresh:
                default_result: dict = {}
                assert _run_postprocess(graph, default_result, "none") == []
                refresh.assert_not_called()
                assert "embeddings_refresh_skipped" not in default_result

                explicit_result: dict = {}
                _run_postprocess(
                    graph,
                    explicit_result,
                    "none",
                    embedding_provider="local",
                    embedding_model="test-model",
                )
            refresh.assert_called_once_with(
                graph,
                provider="local",
                model="test-model",
            )
            assert explicit_result["embeddings_refreshed"] == 1
            assert explicit_result["embeddings_purged"] == 1
            assert "embeddings_refresh_skipped" not in explicit_result
        finally:
            graph.close()

    def test_partial_provider_scope_warns_without_attempting_refresh(self, tmp_path):
        graph, _ = _graph_with_function(tmp_path)
        try:
            with patch(
                "code_review_graph.embeddings.refresh_embeddings",
            ) as refresh:
                result = run_post_processing(
                    graph,
                    embedding_provider="local",
                )
            refresh.assert_not_called()
            assert any("provider and model" in warning.lower() for warning in result["warnings"])
        finally:
            graph.close()

    def test_missing_cloud_credentials_are_a_warning_not_a_build_failure(
        self,
        tmp_path,
        monkeypatch,
    ):
        graph, _ = _graph_with_function(tmp_path)
        with patch("code_review_graph.embeddings.get_provider", return_value=None):
            embeddings = EmbeddingStore(graph.db_path)
        embeddings._conn.execute(
            "INSERT INTO embeddings (qualified_name, vector, text_hash, provider) "
            "VALUES (?, ?, ?, ?)",
            (
                f"{(tmp_path / 'module.py').as_posix()}::keep",
                b"\x00" * 8,
                "old",
                "openai:test-model@https://api.example.test/v1",
            ),
        )
        embeddings._conn.commit()
        embeddings.close()
        for variable in (
            "CRG_OPENAI_API_KEY",
            "CRG_OPENAI_BASE_URL",
            "CRG_OPENAI_MODEL",
        ):
            monkeypatch.delenv(variable, raising=False)

        try:
            result = run_post_processing(
                graph,
                embedding_provider="openai",
                embedding_model="test-model",
            )
            assert result["signatures_computed"] == 2
            assert any(
                "Missing required environment" in warning
                for warning in result["warnings"]
            )
        finally:
            graph.close()

    def test_mcp_build_and_postprocess_forward_exact_scope(self):
        from code_review_graph import main as crg_main

        build_tool = getattr(
            crg_main.build_or_update_graph_tool,
            "fn",
            crg_main.build_or_update_graph_tool,
        )
        postprocess_tool = getattr(
            crg_main.run_postprocess_tool,
            "fn",
            crg_main.run_postprocess_tool,
        )
        with (
            patch.object(
                crg_main,
                "with_provenance",
                side_effect=lambda result, _root: result,
            ),
            patch.object(
                crg_main,
                "build_or_update_graph",
                return_value={"status": "ok"},
            ) as build,
            patch.object(
                crg_main,
                "run_postprocess",
                return_value={"status": "ok"},
            ) as postprocess,
        ):
            asyncio.run(
                build_tool(
                    repo_root="/repo",
                    embedding_provider="local",
                    embedding_model="test-model",
                )
            )
            asyncio.run(
                postprocess_tool(
                    repo_root="/repo",
                    embedding_provider="local",
                    embedding_model="test-model",
                )
            )

        assert build.call_args.kwargs["embedding_provider"] == "local"
        assert build.call_args.kwargs["embedding_model"] == "test-model"
        assert postprocess.call_args.kwargs["embedding_provider"] == "local"
        assert postprocess.call_args.kwargs["embedding_model"] == "test-model"

    def test_cli_build_forwards_exact_scope(self):
        from code_review_graph import cli

        argv = [
            "code-review-graph",
            "build",
            "--repo",
            "repo-root",
            "--embedding-provider",
            "local",
            "--embedding-model",
            "test-model",
        ]
        result = {"files_parsed": 1, "total_nodes": 2, "total_edges": 1}
        with (
            patch.object(sys, "argv", argv),
            patch(
                "code_review_graph.graph.GraphStore",
            ) as graph_store,
            patch(
                "code_review_graph.incremental.get_db_path",
                return_value=MagicMock(),
            ),
            patch(
                "code_review_graph.tools.build.build_or_update_graph",
                return_value=result,
            ) as build,
        ):
            graph_store.return_value = MagicMock()
            cli.main()

        build.assert_called_once_with(
            full_rebuild=True,
            repo_root=str(Path("repo-root").resolve()),
            postprocess="full",
            embedding_provider="local",
            embedding_model="test-model",
        )
