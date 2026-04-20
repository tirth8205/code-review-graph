"""Tests for CLI helpers."""

import logging
import sys
from importlib.metadata import PackageNotFoundError

from code_review_graph import cli


def test_get_version_logs_and_falls_back_to_dev(monkeypatch, caplog):
    def _raise_package_not_found(_dist_name: str) -> str:
        raise PackageNotFoundError("code-review-graph")

    monkeypatch.setattr(cli, "pkg_version", _raise_package_not_found)

    with caplog.at_level(logging.DEBUG, logger="code_review_graph.cli"):
        version = cli._get_version()

    assert version == "dev"
    assert "Package metadata unavailable" in caplog.text


class TestUpdateNoGitExitsZero:
    """Regression tests for #312: running ``update`` or ``detect-changes``
    in a directory with no git repository must exit 0 (with a warning
    to stderr) so Claude Code's PostToolUse hook does not report a
    failure on every Write / Edit / Bash tool call in monorepos where
    the workspace root has no ``.git``.

    We mock ``find_repo_root`` to return ``None`` explicitly so these
    tests do not depend on the test runner's ambient git hierarchy
    (e.g. a ``.git`` directory in the user's home, which would make the
    unbounded ancestor walk find it and skip the no-git branch we want
    to test — same hazard addressed by #241's ``stop_at`` parameter).
    """

    def _invoke(self, command: str, capsys, monkeypatch):
        """Drive ``cli.main`` through the no-git branch by forcing
        ``find_repo_root`` to return ``None``, and capture the stderr
        warning + exit code."""
        import pytest as _pytest

        monkeypatch.setattr(
            "code_review_graph.incremental.find_repo_root",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(sys, "argv", ["code-review-graph", command])
        with _pytest.raises(SystemExit) as excinfo:
            cli.main()
        captured = capsys.readouterr()
        return excinfo.value.code, captured.out, captured.err

    def test_update_exits_zero_without_git(self, capsys, monkeypatch):
        """Before #312 this exited 1, causing
        ``PostToolUse:Edit hook error`` noise on every tool call."""
        code, _out, err = self._invoke("update", capsys, monkeypatch)
        assert code == 0, f"expected exit 0, got {code}; stderr: {err!r}"

    def test_update_still_warns_about_missing_git(self, capsys, monkeypatch):
        """Exit 0 must not be silent — an interactive user still gets
        told why the update did nothing.  The warning goes to stderr so
        MCP stdio transport is not corrupted."""
        _code, out, err = self._invoke("update", capsys, monkeypatch)
        # Warning must be visible in stderr (hook/MCP-safe location).
        assert "git" in err.lower(), (
            f"expected a 'git' hint in stderr; got stdout={out!r} stderr={err!r}"
        )
        # And stdout must NOT contain the warning (would corrupt MCP stdio).
        assert "git" not in out.lower() or "not in a git" not in out.lower()

    def test_detect_changes_also_exits_zero_without_git(
        self, capsys, monkeypatch,
    ):
        """Same non-failing semantics for the sibling ``detect-changes``
        subcommand — otherwise hooks that wrap it get the same error."""
        code, _out, err = self._invoke("detect-changes", capsys, monkeypatch)
        assert code == 0, (
            f"expected exit 0, got {code}; stderr: {err!r}"
        )
