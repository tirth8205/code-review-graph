"""Tests for skills and hooks auto-install."""

import json
import os
import stat
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
    _build_server_entry,
    _cursor_hook_scripts,
    _detect_serve_command,
    _in_poetry_project,
    _in_uv_project,
    _opencode_plugin_content,
    generate_cursor_hooks_config,
    generate_hooks_config,
    generate_skills,
    inject_claude_md,
    inject_platform_instructions,
    install_gemini_cli_hooks,
    install_gemini_cli_skills,
    install_cursor_hooks,
    install_git_hook,
    install_hooks,
    install_opencode_plugin,
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
        config = generate_hooks_config(Path("/repo"))
        assert "hooks" in config

    def test_has_post_tool_use(self):
        config = generate_hooks_config(Path("/repo"))
        assert "PostToolUse" in config["hooks"]
        entry = config["hooks"]["PostToolUse"][0]
        assert entry["matcher"] == "Edit|Write|Bash"
        inner = entry["hooks"][0]
        assert inner["type"] == "command"
        assert "update" in inner["command"]
        assert 0 < inner["timeout"] <= 600

    def test_has_session_start(self):
        config = generate_hooks_config(Path("/repo"))
        assert "SessionStart" in config["hooks"]
        entry = config["hooks"]["SessionStart"][0]
        assert "matcher" in entry
        inner = entry["hooks"][0]
        assert inner["type"] == "command"
        assert "status" in inner["command"]
        assert 0 < inner["timeout"] <= 600

    def test_does_not_emit_invalid_pre_commit_hook(self):
        config = generate_hooks_config(Path("/repo"))
        assert "PreCommit" not in config["hooks"]

    def test_has_only_valid_hook_types(self):
        config = generate_hooks_config(Path("/repo"))
        hook_types = set(config["hooks"].keys())
        assert hook_types == {"PostToolUse", "SessionStart"}

    def test_hook_entries_use_nested_hooks_array(self):
        config = generate_hooks_config(Path("/repo"))
        for hook_type, entries in config["hooks"].items():
            for entry in entries:
                assert "hooks" in entry, f"{hook_type} entry missing 'hooks' array"
                assert "command" not in entry, f"{hook_type} has bare 'command' outside hooks[]"

    def test_repo_root_embedded_in_commands(self):
        config = generate_hooks_config(Path("/my/project"))
        post_cmd = config["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        session_cmd = config["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        assert "/my/project" in post_cmd
        assert "/my/project" in session_cmd

    def test_quotes_repo_paths_with_spaces(self):
        config = generate_hooks_config(Path("/repo with spaces"))
        post_cmd = config["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        assert '"' in post_cmd  # path is JSON-encoded so spaces are quoted

    def test_entries_use_claude_code_hook_schema(self):
        """Regression guard for the Claude Code hook schema.

        Claude Code rejects entries that put ``command`` directly on the
        event entry. Each entry must wrap its command(s) in a
        ``hooks: [{"type": "command", "command": ..., "timeout": ...}]``
        array — missing that wrapper causes the entire settings.json to
        fail to parse ("Expected array, but received undefined").
        """
        config = generate_hooks_config(Path("/repo"))
        for event_name, entries in config["hooks"].items():
            for entry in entries:
                assert "command" not in entry, (
                    f"{event_name} entry has a flat `command` field; "
                    "it must be wrapped in an inner `hooks` array"
                )
                assert "hooks" in entry, (
                    f"{event_name} entry is missing the inner `hooks` array"
                )
                assert isinstance(entry["hooks"], list)
                for hook in entry["hooks"]:
                    assert hook.get("type") == "command", (
                        f"{event_name} inner hook missing type=\"command\""
                    )
                    assert "command" in hook
                    assert "timeout" in hook


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
        assert "OtherHook" in data["hooks"]
        assert "PostToolUse" in data["hooks"]
        assert "SessionStart" in data["hooks"]
        assert "PreCommit" not in data["hooks"]
        assert "OtherHook" in data["hooks"]  # pre-existing hooks must not be clobbered

    def test_creates_settings_backup(self, tmp_path):
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir(parents=True)
        existing = {"hooks": {"OtherHook": []}}
        (settings_dir / "settings.json").write_text(json.dumps(existing))

        install_hooks(tmp_path)

        backup_path = settings_dir / "settings.json.bak"
        assert backup_path.exists()
        backup = json.loads(backup_path.read_text())
        assert backup == existing

    def test_creates_claude_directory(self, tmp_path):
        install_hooks(tmp_path)
        assert (tmp_path / ".claude").is_dir()

    def test_install_qoder_hooks(self, tmp_path):
        install_hooks(tmp_path, platform="qoder")
        settings_path = tmp_path / ".qoder" / "settings.json"
        assert settings_path.exists()
        data = json.loads(settings_path.read_text())
        assert "hooks" in data
        assert "PostToolUse" in data["hooks"]
        assert "SessionStart" in data["hooks"]

    def test_install_qoder_hooks_merges_existing(self, tmp_path):
        settings_dir = tmp_path / ".qoder"
        settings_dir.mkdir(parents=True)
        existing = {"customSetting": True}
        (settings_dir / "settings.json").write_text(json.dumps(existing))

        install_hooks(tmp_path, platform="qoder")

        data = json.loads((settings_dir / "settings.json").read_text())
        assert data["customSetting"] is True
        assert "hooks" in data


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
        assert set(updated) == {"AGENTS.md", "GEMINI.md", ".cursorrules", ".windsurfrules", "QODER.md", ".kiro/steering/code-review-graph.md"}

    def test_default_is_all(self, tmp_path):
        updated = inject_platform_instructions(tmp_path)
        assert set(updated) == {"AGENTS.md", "GEMINI.md", ".cursorrules", ".windsurfrules", "QODER.md", ".kiro/steering/code-review-graph.md"}

    def test_claude_writes_nothing(self, tmp_path):
        updated = inject_platform_instructions(tmp_path, target="claude")
        assert updated == []
        assert not (tmp_path / "AGENTS.md").exists()
        assert not (tmp_path / "GEMINI.md").exists()
        assert not (tmp_path / ".cursorrules").exists()
        assert not (tmp_path / ".windsurfrules").exists()
        assert not (tmp_path / "QODER.md").exists()

    def test_cursor_writes_only_cursor_files(self, tmp_path):
        updated = inject_platform_instructions(tmp_path, target="cursor")
        assert set(updated) == {"AGENTS.md", ".cursorrules"}
        assert not (tmp_path / "GEMINI.md").exists()
        assert not (tmp_path / ".windsurfrules").exists()
        assert not (tmp_path / "QODER.md").exists()

    def test_windsurf_writes_only_windsurfrules(self, tmp_path):
        updated = inject_platform_instructions(tmp_path, target="windsurf")
        assert updated == [".windsurfrules"]

    def test_antigravity_writes_agents_and_gemini(self, tmp_path):
        updated = inject_platform_instructions(tmp_path, target="antigravity")
        assert set(updated) == {"AGENTS.md", "GEMINI.md"}

    def test_gemini_cli_writes_only_gemini_md(self, tmp_path):
        updated = inject_platform_instructions(tmp_path, target="gemini-cli")
        assert updated == ["GEMINI.md"]
        assert not (tmp_path / "AGENTS.md").exists()
        assert not (tmp_path / ".cursorrules").exists()
        assert not (tmp_path / ".windsurfrules").exists()
        assert not (tmp_path / "QODER.md").exists()

    def test_opencode_writes_only_agents(self, tmp_path):
        updated = inject_platform_instructions(tmp_path, target="opencode")
        assert updated == ["AGENTS.md"]

    def test_qoder_writes_only_qoder_md(self, tmp_path):
        updated = inject_platform_instructions(tmp_path, target="qoder")
        assert updated == ["QODER.md"]
        assert not (tmp_path / "AGENTS.md").exists()
        assert not (tmp_path / "GEMINI.md").exists()
        assert not (tmp_path / ".cursorrules").exists()
        assert not (tmp_path / ".windsurfrules").exists()


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
        assert "serve" in entry["args"]

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
        expected_cmd, _ = _detect_serve_command()
        assert data["mcp_servers"]["code-review-graph"]["command"] == expected_cmd

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
        expected_cmd, _ = _detect_serve_command()
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

    def test_install_gemini_cli_config(self, tmp_path):
        gemini_config = tmp_path / ".gemini" / "settings.json"
        with patch.dict(
            PLATFORMS,
            {
                "gemini-cli": {
                    **PLATFORMS["gemini-cli"],
                    "config_path": lambda root: gemini_config,
                    "detect": lambda: True,
                },
            },
        ):
            configured = install_platform_configs(tmp_path, target="gemini-cli")
        assert "Gemini CLI" in configured
        data = json.loads(gemini_config.read_text())
        entry = data["mcpServers"]["code-review-graph"]
        assert "type" not in entry
        assert entry["args"][-1] == "serve"

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
                "gemini-cli": {**PLATFORMS["gemini-cli"], "detect": lambda: False},
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

    def test_install_qoder_config(self, tmp_path):
        qoder_config = tmp_path / ".qoder" / "mcp.json"
        with patch.dict(
            PLATFORMS,
            {
                "qoder": {
                    **PLATFORMS["qoder"],
                    "config_path": lambda root: qoder_config,
                    "detect": lambda: True,
                },
            },
        ):
            configured = install_platform_configs(tmp_path, target="qoder")
        assert "Qoder" in configured
        data = json.loads(qoder_config.read_text())
        assert "mcpServers" in data
        assert "code-review-graph" in data["mcpServers"]
        assert data["mcpServers"]["code-review-graph"]["type"] == "stdio"
        import shutil
        expected_cmd = "uvx" if shutil.which("uvx") else "code-review-graph"
        assert data["mcpServers"]["code-review-graph"]["command"] == expected_cmd


class TestGeminiCLIInstall:
    def test_install_gemini_cli_hooks_creates_settings_and_scripts(self, tmp_path):
        settings_dir = tmp_path / ".gemini"
        settings_dir.mkdir(parents=True, exist_ok=True)
        settings_path = settings_dir / "settings.json"
        settings_path.write_text(json.dumps({"customSetting": True}) + "\n", encoding="utf-8")

        out_path = install_gemini_cli_hooks(tmp_path)
        assert out_path == settings_path
        assert (settings_dir / "settings.json.bak").exists()

        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert data["customSetting"] is True
        assert "hooks" in data
        assert "SessionStart" in data["hooks"]
        assert "AfterTool" in data["hooks"]

        session_start = settings_dir / "hooks" / "crg-session-start.sh"
        update = settings_dir / "hooks" / "crg-update.sh"
        assert session_start.exists()
        assert update.exists()
        assert os.access(session_start, os.X_OK)
        assert os.access(update, os.X_OK)

    def test_install_gemini_cli_skills_writes_skill_dirs(self, tmp_path):
        skills_root = install_gemini_cli_skills(tmp_path)
        assert skills_root == tmp_path / ".gemini" / "skills"
        skill_path = skills_root / "explore-codebase" / "SKILL.md"
        assert skill_path.exists()
        text = skill_path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert "name: explore-codebase" in text
        assert "description:" in text


class TestCursorHooksConfig:
    """Tests for generate_cursor_hooks_config()."""

    def test_has_version_1(self):
        config = generate_cursor_hooks_config()
        assert config["version"] == 1

    def test_has_after_file_edit(self):
        config = generate_cursor_hooks_config()
        hooks = config["hooks"]["afterFileEdit"]
        assert len(hooks) >= 1
        assert "crg-update.sh" in hooks[0]["command"]
        assert hooks[0]["timeout"] == 5

    def test_has_session_start(self):
        config = generate_cursor_hooks_config()
        hooks = config["hooks"]["sessionStart"]
        assert len(hooks) >= 1
        assert "crg-session-start.sh" in hooks[0]["command"]
        assert hooks[0]["timeout"] == 5

    def test_has_before_shell_execution(self):
        config = generate_cursor_hooks_config()
        hooks = config["hooks"]["beforeShellExecution"]
        assert len(hooks) >= 1
        assert "crg-pre-commit.sh" in hooks[0]["command"]
        assert hooks[0]["timeout"] == 10
        assert hooks[0]["matcher"] == "^git\\s+commit"

    def test_has_all_three_hook_types(self):
        config = generate_cursor_hooks_config()
        hook_types = set(config["hooks"].keys())
        assert hook_types == {"afterFileEdit", "sessionStart", "beforeShellExecution"}

    def test_commands_point_to_home_cursor_hooks(self):
        config = generate_cursor_hooks_config()
        from pathlib import Path

        hooks_dir = str(Path.home() / ".cursor" / "hooks")
        for event, entries in config["hooks"].items():
            for entry in entries:
                assert entry["command"].startswith(hooks_dir), (
                    f"{event} command does not start with {hooks_dir}"
                )


class TestCursorHookScripts:
    """Tests for _cursor_hook_scripts()."""

    def test_returns_three_scripts(self):
        scripts = _cursor_hook_scripts()
        assert set(scripts.keys()) == {
            "crg-update.sh",
            "crg-session-start.sh",
            "crg-pre-commit.sh",
        }

    def test_scripts_start_with_shebang(self):
        scripts = _cursor_hook_scripts()
        for name, content in scripts.items():
            assert content.startswith("#!/usr/bin/env bash"), f"{name} missing shebang line"

    def test_scripts_exit_zero(self):
        """Each script must end with exit 0 for graceful failure."""
        scripts = _cursor_hook_scripts()
        for name, content in scripts.items():
            assert "exit 0" in content, f"{name} missing 'exit 0'"

    def test_scripts_consume_stdin(self):
        """Each script must consume stdin (Cursor protocol)."""
        scripts = _cursor_hook_scripts()
        for name, content in scripts.items():
            assert "cat > /dev/null" in content, f"{name} missing stdin consumption"

    def test_update_script_runs_update(self):
        scripts = _cursor_hook_scripts()
        assert "code-review-graph update --skip-flows" in scripts["crg-update.sh"]

    def test_session_start_script_runs_status(self):
        scripts = _cursor_hook_scripts()
        assert "code-review-graph status" in scripts["crg-session-start.sh"]

    def test_pre_commit_script_runs_detect_changes(self):
        scripts = _cursor_hook_scripts()
        assert "code-review-graph detect-changes --brief" in scripts["crg-pre-commit.sh"]


class TestInstallCursorHooks:
    """Tests for install_cursor_hooks()."""

    def test_creates_hooks_json(self, tmp_path):
        with patch("code_review_graph.skills.Path.home", return_value=tmp_path):
            result = install_cursor_hooks()
        hooks_json = tmp_path / ".cursor" / "hooks.json"
        assert hooks_json.exists()
        assert result == hooks_json
        data = json.loads(hooks_json.read_text())
        assert data["version"] == 1
        assert "afterFileEdit" in data["hooks"]

    def test_creates_hook_scripts(self, tmp_path):
        with patch("code_review_graph.skills.Path.home", return_value=tmp_path):
            install_cursor_hooks()
        hooks_dir = tmp_path / ".cursor" / "hooks"
        assert (hooks_dir / "crg-update.sh").exists()
        assert (hooks_dir / "crg-session-start.sh").exists()
        assert (hooks_dir / "crg-pre-commit.sh").exists()

    def test_scripts_are_executable(self, tmp_path):
        with patch("code_review_graph.skills.Path.home", return_value=tmp_path):
            install_cursor_hooks()
        hooks_dir = tmp_path / ".cursor" / "hooks"
        for script in hooks_dir.iterdir():
            mode = script.stat().st_mode
            assert mode & stat.S_IXUSR, f"{script.name} not executable by owner"
            assert mode & stat.S_IXGRP, f"{script.name} not executable by group"

    def test_merges_with_existing_hooks_json(self, tmp_path):
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir(parents=True)
        existing = {
            "version": 1,
            "hooks": {
                "afterFileEdit": [{"command": "/some/other/hook.sh", "timeout": 3}],
                "stop": [{"command": "/some/stop-hook.sh", "timeout": 2}],
            },
        }
        (cursor_dir / "hooks.json").write_text(json.dumps(existing))

        with patch("code_review_graph.skills.Path.home", return_value=tmp_path):
            install_cursor_hooks()

        data = json.loads((cursor_dir / "hooks.json").read_text())
        # Original hook preserved
        commands = [h["command"] for h in data["hooks"]["afterFileEdit"]]
        assert "/some/other/hook.sh" in commands
        # Our hook added
        assert any("crg-update.sh" in c for c in commands)
        # Unrelated hook type preserved
        assert "stop" in data["hooks"]

    def test_no_duplicate_on_reinstall(self, tmp_path):
        with patch("code_review_graph.skills.Path.home", return_value=tmp_path):
            install_cursor_hooks()
            install_cursor_hooks()

        data = json.loads((tmp_path / ".cursor" / "hooks.json").read_text())
        # Each event type should have exactly 1 crg hook
        for event, entries in data["hooks"].items():
            crg_hooks = [h for h in entries if "crg-" in h.get("command", "")]
            assert len(crg_hooks) == 1, f"{event} has {len(crg_hooks)} crg hooks after reinstall"

    def test_handles_corrupt_existing_json(self, tmp_path):
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir(parents=True)
        (cursor_dir / "hooks.json").write_text("not valid json{{{")

        with patch("code_review_graph.skills.Path.home", return_value=tmp_path):
            result = install_cursor_hooks()

        assert result.exists()
        data = json.loads(result.read_text())
        assert data["version"] == 1


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


class TestDetectServeCommand:
    """Tests for _detect_serve_command() and its helpers."""

    # ------------------------------------------------------------------
    # _in_poetry_project() unit tests
    # ------------------------------------------------------------------

    def test_in_poetry_project_via_poetry_active(self, monkeypatch):
        """POETRY_ACTIVE=1 signals a poetry shell session."""
        monkeypatch.setenv("POETRY_ACTIVE", "1")
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        assert _in_poetry_project() is True

    def test_in_poetry_project_via_virtual_env(self, monkeypatch):
        """VIRTUAL_ENV containing 'pypoetry' signals a poetry run session."""
        monkeypatch.delenv("POETRY_ACTIVE", raising=False)
        monkeypatch.setenv("VIRTUAL_ENV", "/home/user/.cache/pypoetry/virtualenvs/proj-xxx")
        assert _in_poetry_project() is True

    def test_in_poetry_project_false_for_plain_venv(self, monkeypatch):
        """A plain venv (no pypoetry in path) is not treated as poetry."""
        monkeypatch.delenv("POETRY_ACTIVE", raising=False)
        monkeypatch.setenv("VIRTUAL_ENV", "/home/user/myproject/.venv")
        assert _in_poetry_project() is False

    def test_in_poetry_project_false_when_nothing_set(self, monkeypatch):
        """No env vars → not in a poetry project."""
        monkeypatch.delenv("POETRY_ACTIVE", raising=False)
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        assert _in_poetry_project() is False

    # ------------------------------------------------------------------
    # _detect_serve_command() integration tests
    # ------------------------------------------------------------------

    def test_poetry_active_returns_poetry_run(self, monkeypatch):
        """POETRY_ACTIVE=1 (poetry shell) → 'poetry run' invocation."""
        monkeypatch.setenv("POETRY_ACTIVE", "1")
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.setattr(
            "code_review_graph.skills.shutil.which",
            lambda x: "/usr/bin/poetry" if x == "poetry" else None,
        )
        cmd, args = _detect_serve_command()
        assert cmd == "poetry"
        assert args == ["run", "code-review-graph", "serve"]

    def test_virtual_env_pypoetry_returns_poetry_run(self, monkeypatch):
        """VIRTUAL_ENV with 'pypoetry' (poetry run) → 'poetry run' invocation."""
        monkeypatch.delenv("POETRY_ACTIVE", raising=False)
        monkeypatch.setenv("VIRTUAL_ENV", "/home/user/.cache/pypoetry/virtualenvs/proj-abc123")
        monkeypatch.setattr(
            "code_review_graph.skills.shutil.which",
            lambda x: "/usr/bin/poetry" if x == "poetry" else None,
        )
        cmd, args = _detect_serve_command()
        assert cmd == "poetry"
        assert args == ["run", "code-review-graph", "serve"]

    def test_poetry_env_without_poetry_on_path_falls_through(self, monkeypatch):
        """If poetry venv is detected but poetry binary is missing, fall through."""
        monkeypatch.setenv("POETRY_ACTIVE", "1")
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
        monkeypatch.setattr("code_review_graph.skills._in_uv_project", lambda: False)
        # poetry not on PATH → should fall through to uvx
        monkeypatch.setattr(
            "code_review_graph.skills.shutil.which",
            lambda x: "/usr/bin/uvx" if x == "uvx" else None,
        )
        cmd, _ = _detect_serve_command()
        assert cmd == "uvx"

    def test_uv_project_env_returns_uv_run(self, monkeypatch):
        """UV_PROJECT_ENVIRONMENT set + uv on PATH → 'uv run' invocation."""
        monkeypatch.delenv("POETRY_ACTIVE", raising=False)
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/some/.venv")
        monkeypatch.setattr(
            "code_review_graph.skills.shutil.which",
            lambda x: "/usr/bin/uv" if x == "uv" else None,
        )
        cmd, args = _detect_serve_command()
        assert cmd == "uv"
        assert args == ["run", "code-review-graph", "serve"]

    def test_uv_lock_detection_returns_uv_run(self, monkeypatch, tmp_path):
        """uv.lock alongside sys.executable → detected as a uv project."""
        monkeypatch.delenv("POETRY_ACTIVE", raising=False)
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
        venv = tmp_path / ".venv" / "bin"
        venv.mkdir(parents=True)
        (tmp_path / "uv.lock").write_text("")
        fake_python = venv / "python"
        fake_python.write_text("")
        monkeypatch.setattr("code_review_graph.skills.sys.executable", str(fake_python))
        monkeypatch.setattr(
            "code_review_graph.skills.shutil.which",
            lambda x: "/usr/bin/uv" if x == "uv" else None,
        )
        assert _in_uv_project() is True
        cmd, args = _detect_serve_command()
        assert cmd == "uv"
        assert args == ["run", "code-review-graph", "serve"]

    def test_uvx_fallback(self, monkeypatch):
        """Not in Poetry/uv but uvx available → use uvx (original behaviour)."""
        monkeypatch.delenv("POETRY_ACTIVE", raising=False)
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
        monkeypatch.setattr("code_review_graph.skills._in_uv_project", lambda: False)
        monkeypatch.setattr(
            "code_review_graph.skills.shutil.which",
            lambda x: "/usr/bin/uvx" if x == "uvx" else None,
        )
        cmd, args = _detect_serve_command()
        assert cmd == "uvx"
        assert args == ["code-review-graph", "serve"]

    def test_sys_executable_fallback(self, monkeypatch):
        """Nothing else available → fall back to sys.executable -m."""
        monkeypatch.delenv("POETRY_ACTIVE", raising=False)
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
        monkeypatch.setattr("code_review_graph.skills._in_uv_project", lambda: False)
        monkeypatch.setattr("code_review_graph.skills.shutil.which", lambda _: None)
        cmd, args = _detect_serve_command()
        assert cmd == sys.executable
        assert args == ["-m", "code_review_graph", "serve"]

    def test_poetry_takes_priority_over_uv(self, monkeypatch):
        """Poetry detection wins even when UV_PROJECT_ENVIRONMENT is also set."""
        monkeypatch.setenv("POETRY_ACTIVE", "1")
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/some/.venv")
        monkeypatch.setattr(
            "code_review_graph.skills.shutil.which",
            lambda x: "/usr/bin/poetry" if x == "poetry" else None,
        )
        cmd, _ = _detect_serve_command()
        assert cmd == "poetry"

    def test_in_uv_project_false_without_lockfile(self, monkeypatch, tmp_path):
        """_in_uv_project returns False when no uv.lock in ancestor dirs."""
        fake_python = tmp_path / "bin" / "python"
        fake_python.parent.mkdir(parents=True)
        fake_python.write_text("")
        monkeypatch.setattr("code_review_graph.skills.sys.executable", str(fake_python))
        monkeypatch.setattr("code_review_graph.skills.Path.home", staticmethod(lambda: tmp_path))
        assert _in_uv_project() is False


class TestOpenCodePluginContent:
    """Tests for _opencode_plugin_content()."""

    def test_returns_non_empty_string(self):
        content = _opencode_plugin_content()
        assert isinstance(content, str)
        assert len(content) > 100

    def test_has_plugin_type_import(self):
        content = _opencode_plugin_content()
        assert "import type" in content
        assert "@opencode-ai/plugin" in content

    def test_has_default_export(self):
        content = _opencode_plugin_content()
        assert "export default" in content

    def test_hooks_file_edited_event(self):
        content = _opencode_plugin_content()
        assert '"file.edited"' in content
        assert "code-review-graph update --skip-flows" in content

    def test_hooks_session_created_event(self):
        content = _opencode_plugin_content()
        assert '"session.created"' in content
        assert "code-review-graph status" in content

    def test_hooks_tool_execute_before_event(self):
        content = _opencode_plugin_content()
        assert '"tool.execute.before"' in content
        assert "code-review-graph detect-changes --brief" in content

    def test_has_git_commit_detection(self):
        """Pre-commit hook should match git commit commands."""
        content = _opencode_plugin_content()
        assert "git" in content
        assert "commit" in content

    def test_all_handlers_have_try_catch(self):
        """Every event handler must use try/catch for graceful failure."""
        content = _opencode_plugin_content()
        # Count the three event registrations and ensure catch blocks
        assert content.count("} catch") >= 3


class TestInstallOpenCodePlugin:
    """Tests for install_opencode_plugin()."""

    def test_creates_plugin_file(self, tmp_path):
        with patch("code_review_graph.skills.Path.home", return_value=tmp_path):
            result = install_opencode_plugin()
        plugin_path = tmp_path / ".config" / "opencode" / "plugins" / "crg-plugin.ts"
        assert plugin_path.exists()
        assert result == plugin_path

    def test_plugin_file_has_correct_content(self, tmp_path):
        with patch("code_review_graph.skills.Path.home", return_value=tmp_path):
            result = install_opencode_plugin()
        content = result.read_text(encoding="utf-8")
        assert "export default" in content
        assert "file.edited" in content

    def test_creates_parent_directories(self, tmp_path):
        with patch("code_review_graph.skills.Path.home", return_value=tmp_path):
            install_opencode_plugin()
        plugins_dir = tmp_path / ".config" / "opencode" / "plugins"
        assert plugins_dir.is_dir()

    def test_overwrites_existing_plugin(self, tmp_path):
        plugins_dir = tmp_path / ".config" / "opencode" / "plugins"
        plugins_dir.mkdir(parents=True)
        old_plugin = plugins_dir / "crg-plugin.ts"
        old_plugin.write_text("// old version")

        with patch("code_review_graph.skills.Path.home", return_value=tmp_path):
            install_opencode_plugin()

        content = old_plugin.read_text()
        assert "// old version" not in content
        assert "export default" in content

    def test_idempotent(self, tmp_path):
        with patch("code_review_graph.skills.Path.home", return_value=tmp_path):
            install_opencode_plugin()
            result = install_opencode_plugin()
        content = result.read_text()
        assert "export default" in content
        # Only one default export in the file
        assert content.count("export default") == 1

    def test_plugin_is_typescript(self, tmp_path):
        with patch("code_review_graph.skills.Path.home", return_value=tmp_path):
            result = install_opencode_plugin()
        assert result.suffix == ".ts"

    def test_preserves_other_plugins(self, tmp_path):
        plugins_dir = tmp_path / ".config" / "opencode" / "plugins"
        plugins_dir.mkdir(parents=True)
        other_plugin = plugins_dir / "other-plugin.ts"
        other_plugin.write_text("// other plugin")

        with patch("code_review_graph.skills.Path.home", return_value=tmp_path):
            install_opencode_plugin()

        assert other_plugin.exists()
        assert other_plugin.read_text() == "// other plugin"

    def test_file_is_utf8(self, tmp_path):
        with patch("code_review_graph.skills.Path.home", return_value=tmp_path):
            result = install_opencode_plugin()
        # Should be readable as UTF-8 without errors
        content = result.read_text(encoding="utf-8")
        assert len(content) > 0
