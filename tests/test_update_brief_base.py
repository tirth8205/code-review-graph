"""`update --brief --base <branch>` summarises the same scope as detect-changes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from code_review_graph.tools.build import build_or_update_graph


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=T", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(f"def {name[:-3]}():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"add {name}")


def test_update_brief_with_branch_base_uses_merge_base(tmp_path, monkeypatch, capsys):
    from code_review_graph import cli

    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "base.py")
    build_or_update_graph(full_rebuild=True, repo_root=str(repo), postprocess="none")

    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "feature_only.py")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "main_only.py")
    _git(repo, "checkout", "-q", "feature")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "code-review-graph",
            "update",
            "--brief",
            "--base",
            "main",
            "--repo",
            str(repo),
            "--skip-postprocess",
        ],
    )
    cli.main()
    out = capsys.readouterr().out
    # Only the feature branch's own change is in review scope; main's later
    # commit must not be counted, matching `detect-changes --base main`.
    assert "Analyzed 1 changed file(s)" in out
