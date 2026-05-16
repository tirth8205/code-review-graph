"""Tests for CLI helpers and MCP serve command wiring."""

import logging
import sys
from importlib.metadata import PackageNotFoundError
from unittest.mock import MagicMock, patch

from code_review_graph import cli


def test_get_version_logs_and_falls_back_to_dev(monkeypatch, caplog):
    def _raise_package_not_found(_dist_name: str) -> str:
        raise PackageNotFoundError("code-review-graph")

    monkeypatch.setattr(cli, "pkg_version", _raise_package_not_found)

    with caplog.at_level(logging.DEBUG, logger="code_review_graph.cli"):
        version = cli._get_version()

    assert version == "dev"
    assert "Package metadata unavailable" in caplog.text


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
