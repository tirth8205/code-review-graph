"""CLI tests for MCP serve command wiring."""

import sys
from unittest.mock import patch

from code_review_graph import cli


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


class TestDaemonCommand:
    def test_daemon_start_calls_helper(self):
        argv = ["code-review-graph", "daemon", "start", "--repo", "repo-root"]
        with patch.object(sys, "argv", argv):
            with patch("code_review_graph.incremental.start_watch_daemon") as mock_start:
                mock_start.return_value = {"message": "started"}
                cli.main()

        mock_start.assert_called_once()

    def test_daemon_status_calls_helper(self):
        argv = ["code-review-graph", "daemon", "status", "--repo", "repo-root"]
        with patch.object(sys, "argv", argv):
            with patch("code_review_graph.incremental.get_watch_daemon_status") as mock_status:
                mock_status.return_value = {
                    "running": False,
                    "pid": None,
                    "pid_file": "pid-file",
                    "lock_file": "lock-file",
                }
                cli.main()

        mock_status.assert_called_once()

    def test_daemon_stop_calls_helper(self):
        argv = ["code-review-graph", "daemon", "stop", "--repo", "repo-root"]
        with patch.object(sys, "argv", argv):
            with patch("code_review_graph.incremental.stop_watch_daemon") as mock_stop:
                mock_stop.return_value = {"message": "stopped"}
                cli.main()

        mock_stop.assert_called_once()
