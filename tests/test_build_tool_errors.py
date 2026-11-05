"""Change-discovery failures come back as a structured build result."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from code_review_graph.tools.build import build_or_update_graph


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=T", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def built_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.py").write_text("def a():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    build_or_update_graph(full_rebuild=True, repo_root=str(repo), postprocess="none")
    (repo / "a.py").write_text("def a():\n    return 2\n")
    _git(repo, "commit", "-qam", "edit")
    return repo


def _break_change_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(*_args, **_kwargs):
        raise RuntimeError("git diff failed while discovering changed files (rc=128)")

    monkeypatch.setattr("code_review_graph.incremental.get_changed_files", failing)


def test_build_tool_reports_discovery_failure_as_error_status(built_repo, monkeypatch):
    _break_change_discovery(monkeypatch)
    result = build_or_update_graph(
        full_rebuild=False, repo_root=str(built_repo), postprocess="none"
    )
    assert result["status"] == "error"
    assert result["build_type"] == "incremental"
    assert "git diff failed" in result["summary"]


def test_cli_update_exits_nonzero_on_discovery_failure(built_repo, monkeypatch, capsys):
    from code_review_graph import cli

    _break_change_discovery(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["code-review-graph", "update", "--repo", str(built_repo)])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1
    assert "git diff failed" in capsys.readouterr().err
