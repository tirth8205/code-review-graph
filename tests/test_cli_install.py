"""Tests for install CLI platform-specific behavior."""

from __future__ import annotations

import argparse
from pathlib import Path

from code_review_graph.cli import _handle_init


def _args(tmp_path: Path, platform: str) -> argparse.Namespace:
    return argparse.Namespace(
        repo=str(tmp_path),
        dry_run=False,
        platform=platform,
        yes=True,
        no_instructions=True,
        no_skills=False,
        no_hooks=False,
    )


def test_handle_init_codex_skips_claude_skills(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        "code_review_graph.incremental.find_repo_root",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "code_review_graph.incremental.ensure_repo_gitignore_excludes_crg",
        lambda repo_root: "created",
    )
    monkeypatch.setattr(
        "code_review_graph.skills.install_platform_configs",
        lambda repo_root, target, dry_run=False: ["Codex"],
    )

    called = {"generate_skills": False, "codex_hooks": False, "git_hook": False}

    def _generate_skills(repo_root):
        called["generate_skills"] = True
        return repo_root / ".claude" / "skills"

    def _install_codex_hooks(repo_root):
        called["codex_hooks"] = True
        return Path("/tmp/fake-codex-hooks.json")

    def _install_git_hook(repo_root):
        called["git_hook"] = True
        return repo_root / ".git" / "hooks" / "pre-commit"

    monkeypatch.setattr("code_review_graph.skills.generate_skills", _generate_skills)
    monkeypatch.setattr("code_review_graph.skills.install_codex_hooks", _install_codex_hooks)
    monkeypatch.setattr("code_review_graph.skills.install_git_hook", _install_git_hook)

    _handle_init(_args(tmp_path, "codex"))
    out = capsys.readouterr().out

    assert called["generate_skills"] is False
    assert called["codex_hooks"] is True
    assert called["git_hook"] is True
    assert "Installed Codex hooks" in out


def test_handle_init_cursor_installs_cursor_hooks(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        "code_review_graph.incremental.find_repo_root",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "code_review_graph.incremental.ensure_repo_gitignore_excludes_crg",
        lambda repo_root: "created",
    )
    monkeypatch.setattr(
        "code_review_graph.skills.install_platform_configs",
        lambda repo_root, target, dry_run=False: ["Cursor"],
    )
    monkeypatch.setitem(
        __import__("code_review_graph.skills", fromlist=["PLATFORMS"]).PLATFORMS,
        "cursor",
        {
            **__import__("code_review_graph.skills", fromlist=["PLATFORMS"]).PLATFORMS["cursor"],
            "detect": lambda: True,
        },
    )

    called = {"cursor_hooks": False}

    def _install_cursor_hooks():
        called["cursor_hooks"] = True
        return Path("/tmp/fake-cursor-hooks.json")

    monkeypatch.setattr("code_review_graph.skills.install_cursor_hooks", _install_cursor_hooks)

    _handle_init(_args(tmp_path, "cursor"))
    out = capsys.readouterr().out

    assert called["cursor_hooks"] is True
    assert "Installed Cursor hooks" in out
