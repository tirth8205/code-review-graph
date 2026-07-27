"""Tests for install CLI platform-specific behavior."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from code_review_graph import skills, uninstall
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


def test_copilot_cli_install_reinstall_uninstall_lifecycle(
    monkeypatch, tmp_path
):
    """The public lifecycle migrates safely and is repeatable without a client."""
    repo = tmp_path / "repo"
    (repo / ".git" / "hooks").mkdir(parents=True)
    home = tmp_path / "home"
    config = home / ".copilot" / "mcp-config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "current-server": {"command": "keep-current"},
                },
                "servers": {
                    "code-review-graph": {},
                    "legacy-server": {"command": "keep-legacy"},
                },
                "theme": "dark",
            }
        ),
        encoding="utf-8",
    )
    legacy_instruction = repo / ".github" / "code-review-graph.instruction.md"
    legacy_instruction.parent.mkdir(parents=True)
    legacy_instruction.write_text(
        "# User notes\n\n" + skills._COPILOT_SECTION,
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: home)
    args = _args(repo, "copilot-cli")
    args.no_instructions = False
    args.no_skills = True
    args.no_hooks = True

    _handle_init(args)
    first_config = config.read_bytes()
    current_instruction = (
        repo
        / ".github"
        / "instructions"
        / "code-review-graph.instructions.md"
    )
    first_instruction = current_instruction.read_bytes()
    _handle_init(args)

    assert config.read_bytes() == first_config
    assert current_instruction.read_bytes() == first_instruction
    installed = json.loads(config.read_text(encoding="utf-8"))
    assert installed["mcpServers"]["current-server"] == {
        "command": "keep-current",
    }
    assert installed["mcpServers"]["code-review-graph"]["type"] == "local"
    assert installed["mcpServers"]["code-review-graph"]["tools"] == ["*"]
    assert installed["servers"] == {
        "legacy-server": {"command": "keep-legacy"},
    }
    assert legacy_instruction.read_text(encoding="utf-8") == "# User notes\n"

    report = uninstall.run(repo=repo, keep_data=True)

    assert report.errors == []
    assert json.loads(config.read_text(encoding="utf-8")) == {
        "mcpServers": {
            "current-server": {"command": "keep-current"},
        },
        "servers": {
            "legacy-server": {"command": "keep-legacy"},
        },
        "theme": "dark",
    }
    assert legacy_instruction.read_text(encoding="utf-8") == "# User notes\n"
    assert not current_instruction.exists()


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


def test_handle_init_codebuddy_installs_only_codebuddy_native_files(
    monkeypatch, tmp_path, capsys
):
    import code_review_graph.skills as skills_module

    assert "codebuddy" in __import__(
        "code_review_graph.cli", fromlist=["_PLATFORM_CHOICES"]
    )._PLATFORM_CHOICES

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
        lambda repo_root, target, dry_run=False: ["CodeBuddy Code"],
    )

    called = {
        "claude_skills": False,
        "codebuddy_skills": False,
        "codebuddy_hooks": False,
        "codebuddy_instructions": False,
    }

    def _generate_skills(repo_root):
        called["claude_skills"] = True
        return repo_root / ".claude" / "skills"

    def _install_codebuddy_skills(repo_root):
        called["codebuddy_skills"] = True
        return repo_root / ".codebuddy" / "skills"

    def _install_codebuddy_hooks(repo_root):
        called["codebuddy_hooks"] = True
        return repo_root / ".codebuddy" / "settings.json"

    def _inject_platform_instructions(repo_root, target="all"):
        called["codebuddy_instructions"] = target == "codebuddy"
        return ["CODEBUDDY.md"]

    monkeypatch.setattr(skills_module, "generate_skills", _generate_skills)
    monkeypatch.setattr(
        skills_module,
        "install_codebuddy_skills",
        _install_codebuddy_skills,
        raising=False,
    )
    monkeypatch.setattr(
        skills_module,
        "install_codebuddy_hooks",
        _install_codebuddy_hooks,
        raising=False,
    )
    monkeypatch.setattr(
        skills_module,
        "inject_platform_instructions",
        _inject_platform_instructions,
    )

    args = _args(tmp_path, "codebuddy")
    args.no_instructions = False
    _handle_init(args)
    out = capsys.readouterr().out

    assert called == {
        "claude_skills": False,
        "codebuddy_skills": True,
        "codebuddy_hooks": True,
        "codebuddy_instructions": True,
    }
    assert "Installed CodeBuddy skills" in out
    assert "Installed CodeBuddy hooks" in out
