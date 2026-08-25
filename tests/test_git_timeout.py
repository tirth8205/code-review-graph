"""#913 — git/svn timeout must not masquerade as 'no changes'."""
import subprocess
from unittest.mock import patch

import pytest

from code_review_graph.changes import GitTimeoutError, parse_git_diff_ranges


class TestGitTimeoutError:
    def test_is_runtime_error(self):
        assert issubclass(GitTimeoutError, RuntimeError)

    def test_message_names_command_and_timeout(self):
        exc = GitTimeoutError("git diff", 30)
        assert "git diff" in str(exc)
        assert "30" in str(exc)
        assert "timed out" in str(exc)

    def test_carries_command_and_timeout_attributes(self):
        exc = GitTimeoutError("svn diff", 45)
        assert exc.command == "svn diff"
        assert exc.timeout == 45


class TestParseGitDiffRangesTimeout:
    def test_timeout_raises_git_timeout_error(self, tmp_path):
        with patch(
            "code_review_graph.changes.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git diff", timeout=30),
        ):
            with pytest.raises(GitTimeoutError, match="git diff.*timed out.*30"):
                parse_git_diff_ranges(str(tmp_path), "HEAD")

    def test_non_timeout_subprocess_error_still_returns_empty(self, tmp_path):
        with patch(
            "code_review_graph.changes.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "git diff"),
        ):
            result = parse_git_diff_ranges(str(tmp_path), "HEAD")
            assert result == {}

    def test_oserror_still_returns_empty(self, tmp_path):
        with patch(
            "code_review_graph.changes.subprocess.run",
            side_effect=OSError("git not found"),
        ):
            result = parse_git_diff_ranges(str(tmp_path), "HEAD")
            assert result == {}
