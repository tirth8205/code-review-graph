"""The Token Savings panel needs a changed function to measure against.

A change with no changed functions, such as an edit to a Markdown file,
gets an almost empty analysis. ``detect-changes --brief`` and
``update --brief`` then printed "Saved ~98%" for telling the reader nothing.
Without a changed function they now print one plain line instead.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from code_review_graph import cli
from code_review_graph.tools.build import build_or_update_graph

NOTE = "Token Savings: none to show, as no functions changed."


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=T", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _commit(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"change {name}")


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "app.py", "def f():\n    return 1\n")
    build_or_update_graph(full_rebuild=True, repo_root=str(root), postprocess="none")
    return root


def _run(monkeypatch, capsys, *argv: str) -> str:
    monkeypatch.setattr(sys, "argv", ["code-review-graph", *argv])
    cli.main()
    return capsys.readouterr().out


COMMANDS = [
    pytest.param(("detect-changes", "--brief"), id="detect-changes"),
    pytest.param(("update", "--brief", "--skip-postprocess"), id="update"),
]


@pytest.mark.parametrize("command", COMMANDS)
def test_no_panel_when_no_function_changed(repo, monkeypatch, capsys, command):
    _commit(repo, "notes.md", "# Notes\n\n" + "words here. " * 400 + "\n")

    out = _run(monkeypatch, capsys, *command, "--repo", str(repo))

    assert "0 changed function(s)" in out
    assert "Saved:" not in out
    assert "┌" not in out
    assert NOTE in out


@pytest.mark.parametrize("command", COMMANDS)
def test_panel_still_shows_when_a_function_changed(repo, monkeypatch, capsys, command):
    _commit(repo, "app.py", "def f():\n    return 2\n\n\ndef g():\n    return 3\n")

    out = _run(monkeypatch, capsys, *command, "--repo", str(repo))

    assert "Token Savings" in out
    assert "Saved:" in out
    assert NOTE not in out
