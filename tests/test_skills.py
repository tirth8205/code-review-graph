"""Tests for skills and hooks auto-install."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10 backport
    import tomli as tomllib

from code_review_graph.skills import (
    _CLAUDE_MD_SECTION_MARKER,
    PLATFORMS,
    generate_hooks_config,
    generate_skills,
    inject_claude_md,
    inject_platform_instructions,
    install_git_hook,
    install_hooks,
    install_platform_configs,
)

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None  # type: ignore[assignment]

_needs_tomllib = pytest.mark.skipif(
    tomllib is None, reason="tomllib requires Python 3.11+",
)


class TestGenerateSkills:
    def test_creates_skills_directory(self, tmp_path):
        result = generate_skills(tmp_path)
        assert result.is_dir()
        assert result == tmp_path / ".claude" / "skills"

    def test_creates_four_skill_files(self, tmp_path):
        skills_dir = generate_skills(tmp_path)
        files = sorted(f.name for f in skills_dir.iterdir())
        assert files == [
            "debug-issue.md",
            "explore-codebase.md",
            "refactor-safely.md",
            "review-changes.md",
        ]

    def test_skill_files_have_frontmatter(self, tmp_path):
        skills_dir = generate_skills(tmp_path)
        for path in skills_dir.iterdir():
            content = path.read_text()
            assert content.startswith("---\n")
            assert "name:" in content
            assert "description:" in content
            # Frontmatter closes
            lines = content.split("\n")
            assert lines[0] == "---"
            closing_idx = content.index("---", 4)
            assert closing_idx > 0

    def test_custom_skills_dir(self, tmp_path):
        custom = tmp_path / "my-skills"
        result = generate_skills(tmp_path, skills_dir=custom)
        assert result == custom
        assert result.is_dir()
        assert len(list(result.iterdir())) == 4

    def test_skill_content_includes_get_minimal_context(self, tmp_path):
        """Every skill template must reference get_minimal_context."""
        skills_dir = generate_skills(tmp_path)
        for path in skills_dir.iterdir():
            content = path.read_text()
            assert "get_minimal_context" in content, (
                f"{path.name} missing get_minimal_context reference"
            )

    def test_skill_content_includes_detail_level(self, tmp_path):
        """Every skill template must reference detail_level."""
        skills_dir = generate_skills(tmp_path)
        for path in skills_dir.iterdir():
            content = path.read_text()
            assert "detail_level" in content, f"{path.name} missing detail_level reference"

    def test_idempotent(self, tmp_path):
        """Running twice should not fail and files should still be valid."""
        generate_skills(tmp_path)
        generate_skills(tmp_path)
        skills_dir = tmp_path / ".claude" / "skills"
        assert len(list(skills_dir.iterdir())) == 4


class TestGenerateHooksConfig:
    def test_returns_dict_with_hooks(self):
        config = generate_hooks_config()
        assert "hooks" in config

    def test_has_post_tool_use(self):
        config = generate_hooks_config()
        assert "PostToolUse" in config["hooks"]
        entry = config["hooks"]["PostToolUse"][0]
        assert entry["matcher"] == "Edit|Write|Bash"
        inner = entry["hooks"][0]
        assert inner["type"] == "command"
        assert "update" in inner["command"]
        assert 0 < inner["timeout"] <= 600

    def test_has_session_start(self):
        config = generate_hooks_config()
        assert "SessionStart" in config["hooks"]
        entry = config["hooks"]["SessionStart"][0]
        assert "matcher" in entry
        inner = entry["hooks"][0]
        assert inner["type"] == "command"
        assert "status" in inner["command"]
        assert 0 < inner["timeout"] <= 600

    def test_no_pre_commit(self):
        config = generate_hooks_config()
        assert "PreCommit" not in config["hooks"]

    def test_hook_entries_use_nested_hooks_array(self):
        config = generate_hooks_config()
        for hook_type, entries in config["hooks"].items():
            for entry in entries:
                assert "hooks" in entry, f"{hook_type} entry missing 'hooks' array"
                assert "command" not in entry, f"{hook_type} has bare 'command' outside hooks[]"


class TestInstallGitHook:
    def _make_git_repo(self, tmp_path: Path) -> Path:
        (tmp_path / ".git" / "hooks").mkdir(parents=True)
        return tmp_path

    def test_creates_executable_pre_commit_hook(self, tmp_path):
        hook_path = install_git_hook(self._make_git_repo(tmp_path))
        assert hook_path is not None and hook_path.name == "pre-commit"
        assert os.access(hook_path, os.X_OK)
        content = hook_path.read_text()
        assert content.startswith("#!/")
        assert "code-review-graph detect-changes" in content

    def test_appends_to_existing_hook(self, tmp_path):
        repo = self._make_git_repo(tmp_path)
        hook_path = repo / ".git" / "hooks" / "pre-commit"
        hook_path.write_text("#!/bin/sh\nexisting-command\n", encoding="utf-8")
        hook_path.chmod(0o755)
        install_git_hook(repo)
        content = hook_path.read_text()
        assert "existing-command" in content
        assert "code-review-graph detect-changes" in content

    def test_idempotent(self, tmp_path):
        repo = self._make_git_repo(tmp_path)
        install_git_hook(repo)
        install_git_hook(repo)
        content = (repo / ".git" / "hooks" / "pre-commit").read_text()
        assert content.count("code-review-graph detect-changes") == 1

    def test_no_git_dir_returns_none(self, tmp_path):
        assert install_git_hook(tmp_path) is None


class TestInstallHooks:
    def test_creates_settings_file(self, tmp_path):
        install_hooks(tmp_path)
        settings_path = tmp_path / ".claude" / "settings.json"
        assert settings_path.exists()
        data = json.loads(settings_path.read_text())
        assert "hooks" in data

    def test_merges_with_existing(self, tmp_path):
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir(parents=True)
        existing = {"customSetting": True, "hooks": {"OtherHook": []}}
        (settings_dir / "settings.json").write_text(json.dumps(existing))

        install_hooks(tmp_path)

        data = json.loads((settings_dir / "settings.json").read_text())
        assert data["customSetting"] is True
        assert "PostToolUse" in data["hooks"]
        assert "SessionStart" in data["hooks"]
        assert "PreCommit" not in data["hooks"]

    def test_creates_claude_directory(self, tmp_path):
        install_hooks(tmp_path)
        assert (tmp_path / ".claude").is_dir()


class TestInjectClaudeMd:
    def test_creates_section_in_new_file(self, tmp_path):
        inject_claude_md(tmp_path)
        content = (tmp_path / "CLAUDE.md").read_text()
        assert _CLAUDE_MD_SECTION_MARKER in content
        assert "MCP Tools" in content

    def test_appends_to_existing_file(self, tmp_path):
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# My Project\n\nExisting content.\n")

        inject_claude_md(tmp_path)

        content = claude_md.read_text()
        assert "# My Project" in content
        assert "Existing content." in content
        assert _CLAUDE_MD_SECTION_MARKER in content

    def test_idempotent(self, tmp_path):
        """Running twice should not duplicate the section."""
        inject_claude_md(tmp_path)
        first_content = (tmp_path / "CLAUDE.md").read_text()

        inject_claude_md(tmp_path)
        second_content = (tmp_path / "CLAUDE.md").read_text()

        assert first_content == second_content
        assert second_content.count(_CLAUDE_MD_SECTION_MARKER) == 1

    def test_idempotent_with_existing_content(self, tmp_path):
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# Existing\n")

        inject_claude_md(tmp_path)
        first_content = claude_md.read_text()

        inject_claude_md(tmp_path)
        second_content = claude_md.read_text()

        assert first_content == second_content
        assert second_content.count(_CLAUDE_MD_SECTION_MARKER) == 1


class TestInjectPlatformInstructionsFiltering:
    def test_all_writes_every_file(self, tmp_path):
        updated = inject_platform_instructions(tmp_path, target="all")
        assert set(updated) == {"AGENTS.md", "GEMINI.md", ".cursorrules", ".windsurfrules", ".kiro/steering/code-review-graph.md"}

    def test_default_is_all(self, tmp_path):
        updated = inject_platform_instructions(tmp_path)
        assert set(updated) == {"AGENTS.md", "GEMINI.md", ".cursorrules", ".windsurfrules", ".kiro/steering/code-review-graph.md"}

    def test_claude_writes_nothing(self, tmp_path):
        updated = inject_platform_instructions(tmp_path, target="claude")
        assert updated == []
        assert not (tmp_path / "AGENTS.md").exists()
        assert not (tmp_path / "GEMINI.md").exists()
        assert not (tmp_path / ".cursorrules").exists()
        assert not (tmp_path / ".windsurfrules").exists()

    def test_cursor_writes_only_cursor_files(self, tmp_path):
        updated = inject_platform_instructions(tmp_path, target="cursor")
        assert set(updated) == {"AGENTS.md", ".cursorrules"}
        assert not (tmp_path / "GEMINI.md").exists()
        assert not (tmp_path / ".windsurfrules").exists()

    def test_windsurf_writes_only_windsurfrules(self, tmp_path):
        updated = inject_platform_instructions(tmp_path, target="windsurf")
        assert updated == [".windsurfrules"]

    def test_antigravity_writes_agents_and_gemini(self, tmp_path):
        updated = inject_platform_instructions(tmp_path, target="antigravity")
        assert set(updated) == {"AGENTS.md", "GEMINI.md"}

    def test_opencode_writes_only_agents(self, tmp_path):
        updated = inject_platform_instructions(tmp_path, target="opencode")
        assert updated == ["AGENTS.md"]


class TestInstallPlatformConfigs:
    @_needs_tomllib
    def test_install_codex_config(self, tmp_path):
        codex_config = tmp_path / ".codex" / "config.toml"
        with patch.dict(
            PLATFORMS,
            {
                "codex": {
                    **PLATFORMS["codex"],
                    "config_path": lambda root: codex_config,
                    "detect": lambda: True,
                },
            },
        ):
            configured = install_platform_configs(tmp_path, target="codex")
        assert "Codex" in configured
        data = tomllib.loads(codex_config.read_text())
        entry = data["mcp_servers"]["code-review-graph"]
        assert entry["type"] == "stdio"
        assert entry["args"] == ["code-review-graph", "serve"] or entry["args"] == ["serve"]

    @_needs_tomllib
    def test_install_codex_preserves_existing_toml(self, tmp_path):
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            'model = "gpt-5.4"\n\n[mcp_servers.other]\ncommand = "other"\n',
            encoding="utf-8",
        )
        with patch.dict(
            PLATFORMS,
            {
                "codex": {
                    **PLATFORMS["codex"],
                    "config_path": lambda root: codex_config,
                    "detect": lambda: True,
                },
            },
        ):
            install_platform_configs(tmp_path, target="codex")
        data = tomllib.loads(codex_config.read_text())
        assert data["model"] == "gpt-5.4"
        assert data["mcp_servers"]["other"]["command"] == "other"
        assert data["mcp_servers"]["code-review-graph"]["command"] in {
            "uvx",
            "code-review-graph",
        }

    def test_install_codex_no_duplicate(self, tmp_path):
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "[mcp_servers.code-review-graph]",
                    'command = "uvx"',
                    'args = ["code-review-graph", "serve"]',
                    'type = "stdio"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        with patch.dict(
            PLATFORMS,
            {
                "codex": {
                    **PLATFORMS["codex"],
                    "config_path": lambda root: codex_config,
                    "detect": lambda: True,
                },
            },
        ):
            install_platform_configs(tmp_path, target="codex")
        assert codex_config.read_text().count("[mcp_servers.code-review-graph]") == 1

    def test_install_cursor_config(self, tmp_path):
        with patch.dict(
            PLATFORMS,
            {
                "cursor": {**PLATFORMS["cursor"], "detect": lambda: True},
            },
        ):
            configured = install_platform_configs(tmp_path, target="cursor")
        assert "Cursor" in configured
        config_path = tmp_path / ".cursor" / "mcp.json"
        assert config_path.exists()
        data = json.loads(config_path.read_text())
        assert "code-review-graph" in data["mcpServers"]
        assert data["mcpServers"]["code-review-graph"]["type"] == "stdio"

    def test_install_windsurf_config(self, tmp_path):
        windsurf_dir = tmp_path / ".codeium" / "windsurf"
        windsurf_dir.mkdir(parents=True)
        config_path = windsurf_dir / "mcp_config.json"
        with patch.dict(
            PLATFORMS,
            {
                "windsurf": {
                    **PLATFORMS["windsurf"],
                    "config_path": lambda root: config_path,
                    "detect": lambda: True,
                },
            },
        ):
            configured = install_platform_configs(tmp_path, target="windsurf")
        assert "Windsurf" in configured
        data = json.loads(config_path.read_text())
        entry = data["mcpServers"]["code-review-graph"]
        assert "type" not in entry
        import shutil

        expected_cmd = "uvx" if shutil.which("uvx") else "code-review-graph"
        assert entry["command"] == expected_cmd

    def test_install_zed_config(self, tmp_path):
        zed_settings = tmp_path / "zed" / "settings.json"
        zed_settings.parent.mkdir(parents=True)
        with patch.dict(
            PLATFORMS,
            {
                "zed": {
                    **PLATFORMS["zed"],
                    "config_path": lambda root: zed_settings,
                    "detect": lambda: True,
                },
            },
        ):
            configured = install_platform_configs(tmp_path, target="zed")
        assert "Zed" in configured
        data = json.loads(zed_settings.read_text())
        assert "context_servers" in data
        assert "code-review-graph" in data["context_servers"]

    def test_install_continue_config(self, tmp_path):
        continue_dir = tmp_path / ".continue"
        continue_dir.mkdir()
        config_path = continue_dir / "config.json"
        with patch.dict(
            PLATFORMS,
            {
                "continue": {
                    **PLATFORMS["continue"],
                    "config_path": lambda root: config_path,
                    "detect": lambda: True,
                },
            },
        ):
            configured = install_platform_configs(tmp_path, target="continue")
        assert "Continue" in configured
        data = json.loads(config_path.read_text())
        assert isinstance(data["mcpServers"], list)
        assert data["mcpServers"][0]["name"] == "code-review-graph"
        assert data["mcpServers"][0]["type"] == "stdio"

    def test_install_opencode_config(self, tmp_path):
        configured = install_platform_configs(tmp_path, target="opencode")
        assert "OpenCode" in configured
        config_path = tmp_path / ".opencode.json"
        data = json.loads(config_path.read_text())
        entry = data["mcpServers"]["code-review-graph"]
        assert entry["type"] == "stdio"
        assert entry["env"] == []

    def test_install_qwen_config(self, tmp_path):
        """Qwen Code uses ~/.qwen/settings.json with mcpServers (see #83)."""
        qwen_config = tmp_path / ".qwen" / "settings.json"
        with patch.dict(
            PLATFORMS,
            {
                "qwen": {
                    **PLATFORMS["qwen"],
                    "config_path": lambda root: qwen_config,
                    "detect": lambda: True,
                },
            },
        ):
            configured = install_platform_configs(tmp_path, target="qwen")
        assert "Qwen Code" in configured
        data = json.loads(qwen_config.read_text())
        entry = data["mcpServers"]["code-review-graph"]
        assert entry["type"] == "stdio"
        assert entry["args"][-1] == "serve"

    def test_install_qwen_preserves_existing_servers(self, tmp_path):
        """Adding qwen should merge with, not clobber, existing mcpServers."""
        qwen_config = tmp_path / ".qwen" / "settings.json"
        qwen_config.parent.mkdir(parents=True)
        qwen_config.write_text(
            json.dumps({"mcpServers": {"other-server": {"command": "other"}}}),
            encoding="utf-8",
        )
        with patch.dict(
            PLATFORMS,
            {
                "qwen": {
                    **PLATFORMS["qwen"],
                    "config_path": lambda root: qwen_config,
                    "detect": lambda: True,
                },
            },
        ):
            install_platform_configs(tmp_path, target="qwen")
        data = json.loads(qwen_config.read_text())
        assert "other-server" in data["mcpServers"]
        assert "code-review-graph" in data["mcpServers"]

    def test_install_all_detected(self, tmp_path):
        """Installing 'all' configures auto-detected platforms."""
        codex_config = tmp_path / ".codex" / "config.toml"
        with patch.dict(
            PLATFORMS,
            {
                "codex": {
                    **PLATFORMS["codex"],
                    "config_path": lambda root: codex_config,
                    "detect": lambda: True,
                },
                "claude": {**PLATFORMS["claude"], "detect": lambda: True},
                "opencode": {**PLATFORMS["opencode"], "detect": lambda: True},
                "cursor": {**PLATFORMS["cursor"], "detect": lambda: False},
                "windsurf": {**PLATFORMS["windsurf"], "detect": lambda: False},
                "zed": {**PLATFORMS["zed"], "detect": lambda: False},
                "continue": {**PLATFORMS["continue"], "detect": lambda: False},
                "antigravity": {**PLATFORMS["antigravity"], "detect": lambda: False},
            },
        ):
            configured = install_platform_configs(tmp_path, target="all")
        assert "Codex" in configured
        assert "Claude Code" in configured
        assert "OpenCode" in configured
        assert codex_config.exists()
        assert (tmp_path / ".mcp.json").exists()
        assert (tmp_path / ".opencode.json").exists()

    def test_merge_existing_servers(self, tmp_path):
        """Should not overwrite existing MCP servers."""
        mcp_path = tmp_path / ".mcp.json"
        existing = {"mcpServers": {"other-server": {"command": "other"}}}
        mcp_path.write_text(json.dumps(existing))
        install_platform_configs(tmp_path, target="claude")
        data = json.loads(mcp_path.read_text())
        assert "other-server" in data["mcpServers"]
        assert "code-review-graph" in data["mcpServers"]

    def test_dry_run_no_write(self, tmp_path):
        configured = install_platform_configs(tmp_path, target="claude", dry_run=True)
        assert "Claude Code" in configured
        assert not (tmp_path / ".mcp.json").exists()

    def test_already_configured_skips(self, tmp_path):
        install_platform_configs(tmp_path, target="claude")
        configured = install_platform_configs(tmp_path, target="claude")
        assert "Claude Code" in configured

    def test_continue_array_no_duplicate(self, tmp_path):
        config_path = tmp_path / ".continue" / "config.json"
        config_path.parent.mkdir(parents=True)
        existing = {
            "mcpServers": [{"name": "code-review-graph", "command": "uvx", "args": ["serve"]}]
        }
        config_path.write_text(json.dumps(existing))
        with patch.dict(
            PLATFORMS,
            {
                "continue": {
                    **PLATFORMS["continue"],
                    "config_path": lambda root: config_path,
                    "detect": lambda: True,
                },
            },
        ):
            install_platform_configs(tmp_path, target="continue")
        data = json.loads(config_path.read_text())
        assert len(data["mcpServers"]) == 1


class TestKiroPlatform:
    """Tests for Kiro platform support."""

    def test_kiro_platform_entry_exists(self):
        """PLATFORMS dict has a 'kiro' key with correct metadata."""
        assert "kiro" in PLATFORMS
        kiro = PLATFORMS["kiro"]
        assert kiro["name"] == "Kiro"
        assert kiro["key"] == "mcpServers"
        assert kiro["format"] == "object"
        assert kiro["needs_type"] is True

    def test_install_kiro_config(self, tmp_path):
        """install_platform_configs creates .kiro/settings/mcp.json."""
        configured = install_platform_configs(tmp_path, target="kiro")
        assert "Kiro" in configured
        config_path = tmp_path / ".kiro" / "settings" / "mcp.json"
        assert config_path.exists()
        data = json.loads(config_path.read_text())
        assert "code-review-graph" in data["mcpServers"]
        entry = data["mcpServers"]["code-review-graph"]
        assert entry["type"] == "stdio"

    def test_install_kiro_preserves_existing_servers(self, tmp_path):
        """Existing mcpServers entries are preserved when adding code-review-graph."""
        config_path = tmp_path / ".kiro" / "settings" / "mcp.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps({"mcpServers": {"other-server": {"command": "other"}}}),
            encoding="utf-8",
        )
        install_platform_configs(tmp_path, target="kiro")
        data = json.loads(config_path.read_text())
        assert "other-server" in data["mcpServers"]
        assert "code-review-graph" in data["mcpServers"]

    def test_install_kiro_no_duplicate(self, tmp_path):
        """Second install skips when code-review-graph already exists."""
        install_platform_configs(tmp_path, target="kiro")
        config_path = tmp_path / ".kiro" / "settings" / "mcp.json"
        first_content = config_path.read_text()
        install_platform_configs(tmp_path, target="kiro")
        second_content = config_path.read_text()
        assert first_content == second_content
        data = json.loads(second_content)
        assert list(data["mcpServers"].keys()).count("code-review-graph") == 1

    def test_kiro_steering_file_written(self, tmp_path):
        """inject_platform_instructions creates .kiro/steering/code-review-graph.md."""
        updated = inject_platform_instructions(tmp_path, target="kiro")
        assert ".kiro/steering/code-review-graph.md" in updated
        steering = tmp_path / ".kiro" / "steering" / "code-review-graph.md"
        assert steering.exists()
        content = steering.read_text()
        assert _CLAUDE_MD_SECTION_MARKER in content

    def test_kiro_steering_idempotent(self, tmp_path):
        """Running inject twice produces identical content."""
        inject_platform_instructions(tmp_path, target="kiro")
        first = (tmp_path / ".kiro" / "steering" / "code-review-graph.md").read_text()
        inject_platform_instructions(tmp_path, target="kiro")
        second = (tmp_path / ".kiro" / "steering" / "code-review-graph.md").read_text()
        assert first == second

    def test_kiro_included_in_all_when_detected(self, tmp_path):
        """install_platform_configs with target='all' includes Kiro when .kiro exists."""
        (tmp_path / ".kiro").mkdir()
        # Mock Path.home() to a dir without .kiro so only workspace detection fires
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        with patch("code_review_graph.skills.Path.home", return_value=fake_home):
            configured = install_platform_configs(tmp_path, target="all")
        assert "Kiro" in configured

    def test_kiro_workspace_detection(self, tmp_path):
        """Kiro detected when repo_root/.kiro exists even if ~/.kiro does not."""
        (tmp_path / ".kiro").mkdir()
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        with patch("code_review_graph.skills.Path.home", return_value=fake_home):
            configured = install_platform_configs(tmp_path, target="all")
        assert "Kiro" in configured
        config_path = tmp_path / ".kiro" / "settings" / "mcp.json"
        assert config_path.exists()

    def test_kiro_dry_run(self, tmp_path):
        """dry_run=True does not create any files."""
        configured = install_platform_configs(tmp_path, target="kiro", dry_run=True)
        assert "Kiro" in configured
        config_path = tmp_path / ".kiro" / "settings" / "mcp.json"
        assert not config_path.exists()
