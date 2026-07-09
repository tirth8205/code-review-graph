"""Tests for CLI helpers and MCP serve command wiring."""

import json
import logging
import sys
from importlib.metadata import PackageNotFoundError
from unittest.mock import MagicMock, patch

from code_review_graph import cli


def test_get_version_falls_back_to_package_attr_when_metadata_missing(
    monkeypatch, caplog,
):
    """When importlib.metadata can't find the dist, fall back to __version__.

    This matters on filesystems where iCloud / OneDrive leave orphan
    dist-info dirs that confuse the metadata lookup. Before v2.3.5 the
    fallback returned the literal string "dev", which produced confusing
    output for installed users whose lookup happened to fail.
    """
    def _raise_package_not_found(_dist_name: str) -> str:
        raise PackageNotFoundError("code-review-graph")

    monkeypatch.setattr(cli, "pkg_version", _raise_package_not_found)

    with caplog.at_level(logging.DEBUG, logger="code_review_graph.cli"):
        version = cli._get_version()

    # Falls back to the package's __version__, not "dev"
    from code_review_graph import __version__ as expected
    assert version == expected
    assert "Package metadata unavailable" in caplog.text


def test_get_version_returns_dev_when_both_sources_fail(monkeypatch, caplog):
    """The literal "dev" fallback still fires when __version__ also fails."""
    def _raise_package_not_found(_dist_name: str) -> str:
        raise PackageNotFoundError("code-review-graph")

    monkeypatch.setattr(cli, "pkg_version", _raise_package_not_found)

    import code_review_graph
    monkeypatch.delattr(code_review_graph, "__version__", raising=False)

    with caplog.at_level(logging.DEBUG, logger="code_review_graph.cli"):
        version = cli._get_version()

    assert version == "dev"


class TestServeCommand:
    def test_serve_passes_auto_watch_flag(self):
        argv = [
            "code-review-graph",
            "serve",
            "--repo",
            "repo-root",
            "--auto-watch",
        ]
        with patch.object(sys, "argv", argv):
            with patch("code_review_graph.main.main") as mock_serve:
                cli.main()

        mock_serve.assert_called_once_with(
            repo_root="repo-root",
            auto_watch=True,
            tools=None,
        )

    def test_mcp_alias_maps_to_serve(self):
        argv = [
            "code-review-graph",
            "mcp",
            "--repo",
            "repo-root",
        ]
        with patch.object(sys, "argv", argv):
            with patch("code_review_graph.main.main") as mock_serve:
                cli.main()

        mock_serve.assert_called_once_with(
            repo_root="repo-root",
            auto_watch=False,
        )


class TestWatchInteraction:
    def test_watch_exits_when_lock_is_held(self):
        argv = ["code-review-graph", "watch", "--repo", "repo-root"]
        with patch.object(sys, "argv", argv):
            with patch("code_review_graph.graph.GraphStore") as mock_store:
                mock_store.return_value = MagicMock()
                with patch("code_review_graph.incremental.get_db_path") as mock_db:
                    mock_db.return_value = MagicMock()
                    with patch("code_review_graph.incremental.watch") as mock_watch:
                        mock_watch.side_effect = RuntimeError("watcher already running")
                        try:
                            cli.main()
                            assert False, "Expected SystemExit"
                        except SystemExit as exc:
                            assert exc.code == 1


class TestBuildUpdateCommands:
    def test_build_skip_postprocess_does_not_run_extra_cli_postprocess(self):
        argv = [
            "code-review-graph",
            "build",
            "--skip-postprocess",
            "--repo",
            "repo-root",
        ]
        result = {
            "files_parsed": 1,
            "total_nodes": 2,
            "total_edges": 1,
            "postprocess_level": "none",
        }

        with patch.object(sys, "argv", argv):
            with patch("code_review_graph.graph.GraphStore") as mock_store:
                mock_store.return_value = MagicMock()
                with patch("code_review_graph.incremental.get_db_path") as mock_db:
                    mock_db.return_value = MagicMock()
                    with patch(
                        "code_review_graph.tools.build.build_or_update_graph",
                        return_value=result,
                    ) as mock_build:
                        with patch(
                            "code_review_graph.postprocessing.run_post_processing",
                        ) as mock_postprocess:
                            cli.main()

        mock_build.assert_called_once_with(
            full_rebuild=True,
            repo_root="repo-root",
            postprocess="none",
        )
        mock_postprocess.assert_not_called()

    def test_update_skip_flows_does_not_run_extra_cli_postprocess(self):
        argv = [
            "code-review-graph",
            "update",
            "--skip-flows",
            "--repo",
            "repo-root",
        ]
        result = {
            "files_updated": 1,
            "total_nodes": 2,
            "total_edges": 1,
            "postprocess_level": "minimal",
        }

        with patch.object(sys, "argv", argv):
            with patch("code_review_graph.graph.GraphStore") as mock_store:
                mock_store.return_value = MagicMock()
                with patch("code_review_graph.incremental.get_db_path") as mock_db:
                    mock_db.return_value = MagicMock()
                    with patch(
                        "code_review_graph.tools.build.build_or_update_graph",
                        return_value=result,
                    ) as mock_build:
                        with patch(
                            "code_review_graph.postprocessing.run_post_processing",
                        ) as mock_postprocess:
                            cli.main()

        mock_build.assert_called_once_with(
            full_rebuild=False,
            repo_root="repo-root",
            base="HEAD~1",
            postprocess="minimal",
        )
        mock_postprocess.assert_not_called()


class TestDetectChangesCommand:
    def test_brief_output_includes_token_savings_panel(self, tmp_path, capsys):
        """v2.3.5: --brief output renders a boxed Token Savings panel.

        Replaces the v2.3.4 one-line `Estimated context saved: …` format.
        The panel must include the title, the saved-tokens line, the
        percent suffix, and box borders.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / "app.py").write_text("x" * 2000, encoding="utf-8")
        argv = [
            "code-review-graph",
            "detect-changes",
            "--repo",
            str(repo),
            "--brief",
        ]

        with patch.object(sys, "argv", argv):
            with patch("code_review_graph.graph.GraphStore") as mock_store:
                mock_store.return_value = MagicMock()
                with patch("code_review_graph.incremental.get_db_path") as mock_db:
                    mock_db.return_value = MagicMock()
                    with patch(
                        "code_review_graph.incremental.get_changed_files",
                        return_value=["app.py"],
                    ):
                        with patch(
                            "code_review_graph.changes.analyze_changes",
                            return_value={"summary": "summary only"},
                        ):
                            cli.main()

        output = capsys.readouterr().out
        assert "summary only" in output
        # Panel structure: title, the three core rows, and box borders.
        assert "Token Savings" in output
        assert "Full context would be:" in output
        assert "Graph context used:" in output
        assert "Saved:" in output
        # Box drawing characters from format_context_savings_panel
        assert "┌" in output and "┘" in output

    def test_json_output_includes_compact_savings_metadata(self, tmp_path, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / "app.py").write_text("x" * 2000, encoding="utf-8")
        argv = [
            "code-review-graph",
            "detect-changes",
            "--repo",
            str(repo),
        ]

        with patch.object(sys, "argv", argv):
            with patch("code_review_graph.graph.GraphStore") as mock_store:
                mock_store.return_value = MagicMock()
                with patch("code_review_graph.incremental.get_db_path") as mock_db:
                    mock_db.return_value = MagicMock()
                    with patch(
                        "code_review_graph.incremental.get_changed_files",
                        return_value=["app.py"],
                    ):
                        with patch(
                            "code_review_graph.changes.analyze_changes",
                            return_value={"summary": "json summary"},
                        ):
                            cli.main()

        result = json.loads(capsys.readouterr().out)
        assert set(result["context_savings"]) == {
            "estimated",
            "saved_tokens",
            "saved_percent",
        }


class TestDetectChangesEndToEnd:
    """Regression test for #528: CLI detect-changes mapped 0 functions.

    The graph stores absolute native paths, but the CLI path let
    analyze_changes parse the diff internally, producing forward-slash
    relative keys that never matched on Windows.  This exercises the full
    pipeline on a real tmp git repo with a committed change.
    """

    @staticmethod
    def _git(repo, *args):
        import subprocess

        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@example.com",
             "-c", "user.name=Test", "-c", "commit.gpgsign=false", *args],
            check=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )

    def test_detect_changes_maps_committed_change_to_functions(
        self, tmp_path, capsys, monkeypatch,
    ):
        monkeypatch.delenv("CRG_DATA_DIR", raising=False)
        monkeypatch.delenv("CRG_REPO_ROOT", raising=False)

        repo = tmp_path / "repo"
        src = repo / "src"
        src.mkdir(parents=True)
        app = src / "app.py"
        app.write_text(
            "def greet(name):\n"
            "    message = 'hello ' + name\n"
            "    return message\n"
            "\n"
            "def farewell(name):\n"
            "    return 'bye ' + name\n",
            encoding="utf-8",
        )

        self._git(repo, "init", "-q")
        self._git(repo, "add", ".")
        self._git(repo, "commit", "-q", "-m", "initial")

        # Commit a change inside greet() so HEAD~1..HEAD touches lines 1-3.
        app.write_text(
            "def greet(name):\n"
            "    message = 'hi there ' + name\n"
            "    return message.upper()\n"
            "\n"
            "def farewell(name):\n"
            "    return 'bye ' + name\n",
            encoding="utf-8",
        )
        self._git(repo, "add", ".")
        self._git(repo, "commit", "-q", "-m", "change greet")

        # Build the graph after the change (stores absolute native paths).
        from code_review_graph.graph import GraphStore
        from code_review_graph.incremental import full_build, get_db_path

        store = GraphStore(get_db_path(repo))
        try:
            full_build(repo, store)
        finally:
            store.close()

        argv = ["code-review-graph", "detect-changes", "--repo", str(repo)]
        with patch.object(sys, "argv", argv):
            cli.main()

        out = capsys.readouterr().out
        result = json.loads(out[out.index("{"):])

        # The diff must map to >0 functions — not silently come up empty.
        names = {f["name"] for f in result["changed_functions"]}
        assert "greet" in names

        # The token-savings metadata must not be the misleading
        # 100%-on-empty case (tiny response because nothing was mapped).
        savings = result["context_savings"]
        assert result["changed_functions"]
        assert savings["saved_percent"] < 100
