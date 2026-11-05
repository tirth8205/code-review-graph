"""An explicit refresh on a graph with no index says so, on every path (#1071).

#1071 made an explicit embedding refresh (provider and model both passed) on a
never-embedded graph report ``embeddings_refresh_skipped`` and
``REFRESH_SKIPPED_WARNING`` instead of a silent ``ok``. These cases sit next
to the PR's own tests in ``test_embedding_refresh.py`` and
``test_watch_refresh_without_index.py``: an incremental update that does
re-parse files, a repository path with spaces and non-ASCII characters,
repeated requests, the interrupted-build repair, the MCP tools and the CLI
driven for real, an index written by an earlier release, and the full cycle
from skip to embed to refresh. Each one also checks that no provider is
resolved, which is the #653 guarantee the skip exists to keep.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from code_review_graph.build_state import BUILD_IN_PROGRESS, BUILD_STATE_KEY
from code_review_graph.embeddings import (
    REFRESH_SKIPPED_WARNING,
    EmbeddingStore,
    embed_all_nodes,
    has_embeddings,
)
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import _raise_watch_postprocess_warnings, get_db_path
from code_review_graph.postprocessing import run_post_processing
from code_review_graph.tools.build import build_or_update_graph, run_postprocess

PAIR = {"embedding_provider": "local", "embedding_model": "test-model"}


class _StubProvider:
    dimension = 2

    def __init__(self, name: str = "local:test-model") -> None:
        self.name = name

    def embed(self, texts):
        return [[float(len(text)), 1.0] for text in texts]

    def embed_query(self, text):
        return [1.0, 0.0]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=T", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _git_repo(tmp_path: Path, monkeypatch, name: str = "repo") -> Path:
    """A committed one-file git repository, parsed serially."""
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "app.py").write_text("def hello():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _commit_change(repo: Path) -> None:
    (repo / "app.py").write_text(
        "def hello():\n    return 2\n\n\ndef bye():\n    return hello()\n",
    )
    _git(repo, "commit", "-qam", "change")


def _embeddings_table_exists(repo: Path) -> bool:
    conn = sqlite3.connect(get_db_path(repo))
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'embeddings'",
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def _embed(repo: Path) -> int:
    """Create a real index with a stub provider, the way ``embed`` would."""
    graph = GraphStore(get_db_path(repo))
    try:
        with patch(
            "code_review_graph.embeddings.get_provider",
            return_value=_StubProvider(),
        ):
            with EmbeddingStore(graph.db_path, provider="local", model="test-model") as store:
                return embed_all_nodes(graph, store)
    finally:
        graph.close()


def _assert_skip_reported(result: dict) -> None:
    assert result["status"] == "ok"
    assert result["embeddings_refresh_skipped"] is True
    assert "embeddings_refreshed" not in result
    assert result["warnings"] == [REFRESH_SKIPPED_WARNING]


class TestBuildPaths:
    @pytest.mark.parametrize("repo_name", ["repo", "my repo üñî"])
    def test_incremental_update_that_reparses_files_reports_the_skip(
        self, tmp_path, monkeypatch, repo_name,
    ):
        repo = _git_repo(tmp_path, monkeypatch, repo_name)
        build_or_update_graph(full_rebuild=True, repo_root=str(repo))
        _commit_change(repo)

        with patch("code_review_graph.embeddings.get_provider") as get_provider:
            result = build_or_update_graph(repo_root=str(repo), **PAIR)
        get_provider.assert_not_called()
        assert result["build_type"] == "incremental"
        assert result["files_updated"] >= 1
        _assert_skip_reported(result)

    def test_repeated_requests_report_every_time_and_never_create_the_index(
        self, tmp_path, monkeypatch,
    ):
        repo = _git_repo(tmp_path, monkeypatch)
        with patch("code_review_graph.embeddings.get_provider") as get_provider:
            results = [
                build_or_update_graph(full_rebuild=True, repo_root=str(repo), **PAIR),
                build_or_update_graph(full_rebuild=True, repo_root=str(repo), **PAIR),
                build_or_update_graph(repo_root=str(repo), **PAIR),
                build_or_update_graph(repo_root=str(repo), **PAIR),
            ]
        get_provider.assert_not_called()
        for result in results:
            _assert_skip_reported(result)
        # The check reads the schema; it must not leave an empty table behind
        # that a later reader would mistake for an initialised index.
        assert not _embeddings_table_exists(repo)

    @pytest.mark.parametrize("postprocess", ["none", "minimal", "full"])
    def test_no_change_update_reports_the_skip_at_every_level(
        self, tmp_path, monkeypatch, postprocess,
    ):
        repo = _git_repo(tmp_path, monkeypatch)
        build_or_update_graph(full_rebuild=True, repo_root=str(repo))

        with patch("code_review_graph.embeddings.get_provider") as get_provider:
            result = build_or_update_graph(
                repo_root=str(repo), postprocess=postprocess, **PAIR,
            )
        get_provider.assert_not_called()
        assert result["files_updated"] == 0
        _assert_skip_reported(result)

    def test_interrupted_build_repair_reports_the_skip(self, tmp_path, monkeypatch):
        repo = _git_repo(tmp_path, monkeypatch)
        build_or_update_graph(full_rebuild=True, repo_root=str(repo))
        graph = GraphStore(get_db_path(repo))
        try:
            graph.set_metadata(BUILD_STATE_KEY, BUILD_IN_PROGRESS)
        finally:
            graph.close()

        with patch("code_review_graph.embeddings.get_provider") as get_provider:
            result = build_or_update_graph(repo_root=str(repo), **PAIR)
        get_provider.assert_not_called()
        assert result["build_type"] == "full"
        assert result["repaired_interrupted_build"] is True
        _assert_skip_reported(result)

    def test_postprocess_command_keeps_ok_status_with_an_empty_table(
        self, tmp_path, monkeypatch,
    ):
        repo = _git_repo(tmp_path, monkeypatch)
        build_or_update_graph(full_rebuild=True, repo_root=str(repo))
        with patch("code_review_graph.embeddings.get_provider", return_value=None):
            EmbeddingStore(get_db_path(repo)).close()
        assert _embeddings_table_exists(repo)

        with patch("code_review_graph.embeddings.get_provider") as get_provider:
            result = run_postprocess(repo_root=str(repo), **PAIR)
        get_provider.assert_not_called()
        assert result["summary"] == "Post-processing complete."
        _assert_skip_reported(result)


class TestFromSkipToRefresh:
    def test_skip_then_embed_then_refresh(self, tmp_path, monkeypatch):
        repo = _git_repo(tmp_path, monkeypatch)
        with patch("code_review_graph.embeddings.get_provider") as get_provider:
            first = build_or_update_graph(full_rebuild=True, repo_root=str(repo), **PAIR)
        get_provider.assert_not_called()
        _assert_skip_reported(first)

        assert _embed(repo) > 0

        _commit_change(repo)
        with patch(
            "code_review_graph.embeddings.get_provider",
            return_value=_StubProvider(),
        ):
            refreshed = build_or_update_graph(repo_root=str(repo), **PAIR)
            noop = build_or_update_graph(repo_root=str(repo), **PAIR)
        assert refreshed["status"] == "ok"
        assert refreshed["embeddings_refreshed"] >= 1
        assert "embeddings_refresh_skipped" not in refreshed
        assert "warnings" not in refreshed
        assert noop["files_updated"] == 0
        assert "embeddings_refresh_skipped" not in noop
        assert "warnings" not in noop


class TestHasEmbeddings:
    def _graph(self, tmp_path: Path) -> GraphStore:
        return GraphStore(tmp_path / "graph.db")

    def test_no_table_is_false_and_creates_nothing(self, tmp_path):
        graph = self._graph(tmp_path)
        try:
            assert has_embeddings(graph) is False
            assert has_embeddings(graph) is False
            assert graph._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'embeddings'",
            ).fetchone() is None
        finally:
            graph.close()

    def test_empty_table_is_false(self, tmp_path):
        graph = self._graph(tmp_path)
        try:
            with patch("code_review_graph.embeddings.get_provider", return_value=None):
                EmbeddingStore(graph.db_path).close()
            assert has_embeddings(graph) is False
        finally:
            graph.close()

    @pytest.mark.parametrize("legacy", [False, True])
    def test_any_stored_row_is_true(self, tmp_path, legacy):
        graph = self._graph(tmp_path)
        try:
            if legacy:
                # The shape earlier releases wrote: no provider column.
                graph._conn.executescript(
                    "CREATE TABLE embeddings (qualified_name TEXT PRIMARY KEY, "
                    "vector BLOB NOT NULL, text_hash TEXT NOT NULL);",
                )
                graph._conn.execute(
                    "INSERT INTO embeddings VALUES (?, ?, ?)", ("m.py::f", b"\x00" * 8, "h"),
                )
                graph.commit()
            else:
                with patch("code_review_graph.embeddings.get_provider", return_value=None):
                    store = EmbeddingStore(graph.db_path)
                store._conn.execute(
                    "INSERT INTO embeddings (qualified_name, vector, text_hash, provider) "
                    "VALUES (?, ?, ?, ?)",
                    ("m.py::f", b"\x00" * 8, "h", "local:test-model"),
                )
                store._conn.commit()
                store.close()
            assert has_embeddings(graph) is True
        finally:
            graph.close()


class TestIndexFromAnEarlierRelease:
    """An index written before provider identity existed is not a skip."""

    def _legacy_graph(self, tmp_path: Path, *, rows: bool) -> GraphStore:
        graph = GraphStore(tmp_path / "graph.db")
        graph._conn.executescript(
            "CREATE TABLE embeddings (qualified_name TEXT PRIMARY KEY, "
            "vector BLOB NOT NULL, text_hash TEXT NOT NULL);",
        )
        if rows:
            graph._conn.execute(
                "INSERT INTO embeddings VALUES (?, ?, ?)", ("m.py::f", b"\x00" * 8, "h"),
            )
        graph.commit()
        return graph

    def test_legacy_rows_fail_the_refresh_and_still_stop_watch(self, tmp_path):
        graph = self._legacy_graph(tmp_path, rows=True)
        try:
            with patch("code_review_graph.embeddings.get_provider") as get_provider:
                result = run_post_processing(graph, **PAIR)
            get_provider.assert_not_called()
            assert "embeddings_refresh_skipped" not in result
            assert REFRESH_SKIPPED_WARNING not in result["warnings"]
            assert any("provider identity" in w for w in result["warnings"])
            with pytest.raises(RuntimeError, match="provider identity"):
                _raise_watch_postprocess_warnings(result)
        finally:
            graph.close()

    def test_empty_legacy_table_is_a_skip(self, tmp_path):
        graph = self._legacy_graph(tmp_path, rows=False)
        try:
            with patch("code_review_graph.embeddings.get_provider") as get_provider:
                result = run_post_processing(graph, **PAIR)
            get_provider.assert_not_called()
            assert result["embeddings_refresh_skipped"] is True
            assert result["warnings"] == [REFRESH_SKIPPED_WARNING]
            _raise_watch_postprocess_warnings(result)
        finally:
            graph.close()


class TestWatchExemption:
    @pytest.mark.parametrize(
        "result",
        [
            {"warnings": [REFRESH_SKIPPED_WARNING, REFRESH_SKIPPED_WARNING]},
            {"warnings": []},
            {},
            None,
            "not a dict",
        ],
        ids=["notice-twice", "empty-list", "no-key", "none", "non-dict"],
    )
    def test_results_without_a_real_failure_keep_watch_running(self, result):
        _raise_watch_postprocess_warnings(result)

    @pytest.mark.parametrize(
        "warning",
        [
            "Embedding refresh requires both an explicit provider and model.",
            REFRESH_SKIPPED_WARNING + " ",
            REFRESH_SKIPPED_WARNING.upper(),
        ],
        ids=["half-pair", "trailing-space", "different-case"],
    )
    def test_only_the_exact_notice_is_exempt(self, warning):
        with pytest.raises(RuntimeError, match="post-processing reported warnings"):
            _raise_watch_postprocess_warnings({"warnings": [warning]})


class TestRealEntryPoints:
    def test_mcp_tools_return_the_skip(self, tmp_path, monkeypatch):
        from code_review_graph import main as crg_main

        build_tool = getattr(
            crg_main.build_or_update_graph_tool, "fn", crg_main.build_or_update_graph_tool,
        )
        postprocess_tool = getattr(
            crg_main.run_postprocess_tool, "fn", crg_main.run_postprocess_tool,
        )
        repo = _git_repo(tmp_path, monkeypatch)
        with patch("code_review_graph.embeddings.get_provider") as get_provider:
            built = asyncio.run(build_tool(full_rebuild=True, repo_root=str(repo), **PAIR))
            noop = asyncio.run(build_tool(repo_root=str(repo), **PAIR))
            post = asyncio.run(postprocess_tool(repo_root=str(repo), **PAIR))
            default = asyncio.run(build_tool(repo_root=str(repo)))
        get_provider.assert_not_called()
        for result in (built, noop, post):
            _assert_skip_reported(result)
        assert "embeddings_refresh_skipped" not in default
        assert "warnings" not in default

    @pytest.mark.parametrize(
        "command",
        [["build"], ["update"], ["postprocess"], ["build", "--quiet"]],
        ids=["build", "noop-update", "postprocess", "quiet-build"],
    )
    def test_cli_exits_cleanly_and_logs_the_notice(
        self, tmp_path, monkeypatch, caplog, command,
    ):
        from code_review_graph import cli

        repo = _git_repo(tmp_path, monkeypatch)
        build_or_update_graph(full_rebuild=True, repo_root=str(repo))
        argv = [
            "code-review-graph",
            *command,
            "--repo",
            str(repo),
            "--embedding-provider",
            "local",
            "--embedding-model",
            "test-model",
        ]
        caplog.set_level(logging.WARNING)
        with (
            patch.object(sys, "argv", argv),
            patch("code_review_graph.embeddings.get_provider") as get_provider,
        ):
            cli.main()
        get_provider.assert_not_called()
        notices = [r for r in caplog.records if r.getMessage() == REFRESH_SKIPPED_WARNING]
        assert len(notices) == 1

    def test_notice_names_a_real_command_and_tool(self):
        from code_review_graph import cli
        from code_review_graph import main as crg_main

        assert "`code-review-graph embed`" in REFRESH_SKIPPED_WARNING
        assert "embed_graph_tool" in REFRESH_SKIPPED_WARNING
        assert hasattr(crg_main, "embed_graph_tool")
        args = cli.build_parser().parse_args(["embed"])
        assert args.command == "embed"


def _notices(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == REFRESH_SKIPPED_WARNING]


class TestWatchTellsTheUser:
    """Watch drops the notice from its failure check, so the log is all the user sees."""

    def test_every_watch_update_logs_the_notice_once_and_keeps_running(
        self, tmp_path, caplog,
    ):
        from functools import partial

        from watchdog.events import FileCreatedEvent

        from code_review_graph.incremental import _create_watch_handler

        repo = tmp_path / "repo"
        repo.mkdir()
        store = GraphStore(tmp_path / "graph.db")
        # The exact callback ``code-review-graph watch`` builds for the pair.
        handler = _create_watch_handler(repo, store, partial(run_post_processing, **PAIR))
        caplog.set_level(logging.WARNING)
        try:
            with patch("code_review_graph.embeddings.get_provider") as get_provider:
                for name in ("first.py", "second.py"):
                    source = repo / name
                    source.write_text(f"def {source.stem}():\n    pass\n")
                    caplog.clear()
                    handler.process([FileCreatedEvent(str(source))])
                    handler.raise_if_failed()
                    assert len(_notices(caplog)) == 1, name
            get_provider.assert_not_called()
            assert len(store.get_all_files()) == 2
            assert not has_embeddings(store)
        finally:
            store.close()

    def test_cli_watch_with_the_pair_keeps_running_on_a_never_embedded_graph(
        self, tmp_path, monkeypatch, caplog,
    ):
        from code_review_graph import cli

        repo = _git_repo(tmp_path, monkeypatch)
        build_or_update_graph(full_rebuild=True, repo_root=str(repo))
        # Changed while no watcher ran: startup reconciliation re-parses it and
        # hands the store to the CLI's post-processing callback.
        (repo / "app.py").unlink()
        argv = [
            "code-review-graph", "watch", "--repo", str(repo),
            "--embedding-provider", "local", "--embedding-model", "test-model",
        ]
        caplog.set_level(logging.WARNING)
        with (
            patch.object(sys, "argv", argv),
            patch("code_review_graph.embeddings.get_provider") as get_provider,
            patch("watchdog.observers.Observer") as observer,
            patch("time.sleep", side_effect=KeyboardInterrupt),
        ):
            cli.main()  # a fatal post-processing warning would sys.exit(1)
        get_provider.assert_not_called()
        observer.return_value.start.assert_called_once()
        assert len(_notices(caplog)) == 1
        assert not _embeddings_table_exists(repo)


class TestNoChangeUpdateEdgeCases:
    def test_an_empty_table_left_by_graph_stats_is_still_a_skip(self, tmp_path, monkeypatch):
        """``list_graph_stats`` opens an EmbeddingStore, which creates an empty table."""
        from code_review_graph.tools.query import list_graph_stats

        repo = _git_repo(tmp_path, monkeypatch)
        build_or_update_graph(full_rebuild=True, repo_root=str(repo))
        with patch("code_review_graph.embeddings.get_provider", return_value=None):
            stats = list_graph_stats(repo_root=str(repo))
        assert stats["status"] == "ok"
        assert _embeddings_table_exists(repo)

        with patch("code_review_graph.embeddings.get_provider") as get_provider:
            result = build_or_update_graph(repo_root=str(repo), **PAIR)
        get_provider.assert_not_called()
        assert result["files_updated"] == 0
        _assert_skip_reported(result)

    @pytest.mark.parametrize("changed", [False, True], ids=["no-op", "reparse"])
    @pytest.mark.parametrize(
        "half",
        [{"embedding_provider": "local"}, {"embedding_model": "test-model"}],
        ids=["provider-only", "model-only"],
    )
    def test_half_a_pair_is_never_reported_as_a_skip(
        self, tmp_path, monkeypatch, changed, half,
    ):
        """MCP callers can send half a pair; the CLI rejects it before this point."""
        repo = _git_repo(tmp_path, monkeypatch)
        build_or_update_graph(full_rebuild=True, repo_root=str(repo))
        if changed:
            _commit_change(repo)

        with patch("code_review_graph.embeddings.get_provider") as get_provider:
            result = build_or_update_graph(repo_root=str(repo), **half)
        get_provider.assert_not_called()
        assert result["status"] == "ok"
        assert "embeddings_refresh_skipped" not in result
        assert REFRESH_SKIPPED_WARNING not in result.get("warnings", [])


class TestCliProcess:
    def test_build_prints_the_notice_once_and_exits_zero(self, tmp_path, monkeypatch):
        """The user-facing stream, from a real process, with no provider mocked."""
        import os

        repo = _git_repo(tmp_path, monkeypatch)
        home = tmp_path / "home"
        home.mkdir()
        env = {
            **os.environ,
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "APPDATA": str(home / "AppData"),
            "CRG_HOME": str(home / ".code-review-graph"),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "CRG_SERIAL_PARSE": "1",
        }
        script = (
            "import sys\n"
            "from code_review_graph.cli import main\n"
            "try:\n"
            "    main()\n"
            "finally:\n"
            "    print('LOADED', 'sentence_transformers' in sys.modules, file=sys.stderr)\n"
        )
        proc = subprocess.run(
            [
                sys.executable, "-c", script, "build", "--repo", str(repo),
                "--embedding-provider", "local", "--embedding-model", "all-MiniLM-L6-v2",
            ],
            capture_output=True, text=True, encoding="utf-8", env=env, timeout=120,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stderr.count(REFRESH_SKIPPED_WARNING) == 1, proc.stderr
        assert "LOADED False" in proc.stderr
        assert "Full build:" in proc.stdout
        assert not _embeddings_table_exists(repo)
