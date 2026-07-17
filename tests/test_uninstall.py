"""Destructive-regression tests for the safe uninstall workflow.

Every test uses a fake home and repository.  The real user configuration must
never be reachable from this suite.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from code_review_graph import skills, uninstall


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, value: object) -> None:
    _write(path, json.dumps(value, indent=2) + "\n")


def _read_jsonc(path: Path) -> object:
    return json.loads(skills._strip_jsonc(path.read_text(encoding="utf-8")))


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git" / "hooks").mkdir(parents=True)
    return repo


@pytest.mark.parametrize("platform_name", tuple(skills.PLATFORMS))
def test_uninstall_removes_mcp_entry_for_every_current_platform_spec(
    platform_name: str,
    fake_repo: Path,
    fake_home: Path,
) -> None:
    """The uninstall inventory follows PLATFORMS, including future path changes."""
    spec = skills.PLATFORMS[platform_name]
    config_path = spec["config_path"](fake_repo)
    if spec["format"] == "toml":
        _write(
            config_path,
            "theme = \"dark\"\n\n"
            "[mcp_servers.code-review-graph]\n"
            "command = \"code-review-graph\"\n\n"
            "[mcp_servers.other]\ncommand = \"other\"\n",
        )
    else:
        if spec["format"] == "array":
            container: object = [
                {"name": "code-review-graph", "command": "code-review-graph"},
                {"name": "other", "url": "https://example.test/mcp"},
            ]
        else:
            container = {
                "code-review-graph": {"command": "code-review-graph"},
                "other": {"url": "https://example.test/mcp"},
            }
        _write_json(config_path, {spec["key"]: container, "theme": "dark"})

    report = uninstall.run(repo=fake_repo, keep_data=True)

    assert report.errors == []
    if spec["format"] == "toml":
        text = config_path.read_text(encoding="utf-8")
        assert "[mcp_servers.code-review-graph]" not in text
        assert "[mcp_servers.other]" in text
        assert 'theme = "dark"' in text
    else:
        data = _read_jsonc(config_path)
        container = data[spec["key"]]
        if spec["format"] == "array":
            assert [entry["name"] for entry in container] == ["other"]
        else:
            assert set(container) == {"other"}
        assert data["theme"] == "dark"


def test_platform_inventory_is_derived_not_hard_coded(
    fake_repo: Path,
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = fake_repo / ".future-editor" / "mcp.json"
    monkeypatch.setitem(
        skills.PLATFORMS,
        "future-editor",
        {
            "name": "Future Editor",
            "config_path": lambda root: root / ".future-editor" / "mcp.json",
            "key": "servers",
            "detect": lambda: True,
            "format": "object",
            "needs_type": False,
        },
    )
    _write_json(config, {"servers": {"code-review-graph": {}, "mine": {}}})

    uninstall.run(repo=fake_repo, keep_data=True)

    assert _read_jsonc(config) == {"servers": {"mine": {}}}


def test_source_pr_legacy_mcp_paths_remain_supported(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    repo_legacy = fake_repo / ".opencode.json"
    user_legacy = fake_home / ".cursor" / "mcp.json"
    for path in (repo_legacy, user_legacy):
        _write_json(
            path,
            {"mcpServers": {"code-review-graph": {}, "other": {}}},
        )

    uninstall.run(repo=fake_repo, keep_data=True)

    for path in (repo_legacy, user_legacy):
        assert _read_jsonc(path) == {"mcpServers": {"other": {}}}


@pytest.mark.parametrize("platform_name", ["zed", "opencode"])
def test_jsonc_comments_trailing_commas_and_https_survive(
    platform_name: str,
    fake_repo: Path,
    fake_home: Path,
) -> None:
    spec = skills.PLATFORMS[platform_name]
    path = spec["config_path"](fake_repo)
    _write(
        path,
        "{\n"
        "  // keep top-level comment\n"
        f'  "{spec["key"]}": {{\n'
        "    // remove only the next member\n"
        '    "code-review-graph": {"command": "code-review-graph"},\n'
        "    // keep server comment\n"
        '    "other": {"url": "https://example.test/a//b"},\n'
        "  },\n"
        "  // keep trailing comment\n"
        '  "theme": "dark",\n'
        "}\n",
    )

    uninstall.run(repo=fake_repo, keep_data=True)

    raw = path.read_text(encoding="utf-8")
    assert "keep top-level comment" in raw
    assert "remove only the next member" in raw
    assert "keep server comment" in raw
    assert "keep trailing comment" in raw
    assert "https://example.test/a//b" in raw
    assert "code-review-graph" not in raw
    assert _read_jsonc(path)[spec["key"]]["other"]["url"].startswith("https://")


def test_gemini_shared_settings_removes_mcp_and_owned_hooks(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    settings = fake_repo / ".gemini" / "settings.json"
    _write_json(
        settings,
        {
            "mcpServers": {
                "code-review-graph": {"command": "code-review-graph"},
                "other": {"command": "other"},
            },
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "bash .gemini/hooks/crg-session-start.sh",
                            }
                        ],
                    },
                    {"matcher": "", "hooks": [{"command": "user-session-hook"}]},
                ],
                "AfterTool": [
                    {
                        "matcher": "write_file|replace",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "bash .gemini/hooks/crg-update.sh",
                            }
                        ],
                    }
                ],
            },
            "theme": "dark",
        },
    )
    for filename in ("crg-session-start.sh", "crg-update.sh"):
        _write(fake_repo / ".gemini" / "hooks" / filename, "#!/bin/sh\n")

    uninstall.run(repo=fake_repo, keep_data=True)

    data = _read_jsonc(settings)
    assert set(data["mcpServers"]) == {"other"}
    assert data["hooks"]["SessionStart"] == [
        {"matcher": "", "hooks": [{"command": "user-session-hook"}]}
    ]
    assert "AfterTool" not in data["hooks"]
    assert data["theme"] == "dark"
    assert not (fake_repo / ".gemini" / "hooks" / "crg-session-start.sh").exists()
    assert not (fake_repo / ".gemini" / "hooks" / "crg-update.sh").exists()


def test_cursor_shared_hooks_directory_keeps_unrelated_scripts(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    cursor_dir = fake_home / ".cursor"
    config = skills.generate_cursor_hooks_config()
    config["hooks"]["sessionStart"].append({"command": "user-session-hook"})
    _write_json(cursor_dir / "hooks.json", config)
    for filename in skills._cursor_hook_scripts():
        _write(cursor_dir / "hooks" / filename, "#!/bin/sh\n")
    _write(cursor_dir / "hooks" / "my-company-hook.sh", "#!/bin/sh\n")

    uninstall.run(repo=fake_repo, keep_data=True)

    assert (cursor_dir / "hooks").is_dir()
    assert (cursor_dir / "hooks" / "my-company-hook.sh").exists()
    for filename in skills._cursor_hook_scripts():
        assert not (cursor_dir / "hooks" / filename).exists()
    data = _read_jsonc(cursor_dir / "hooks.json")
    assert data["hooks"]["sessionStart"] == [{"command": "user-session-hook"}]


def test_hook_cleanup_handles_owned_entries_and_mixed_nested_groups(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    owned = skills.generate_cursor_hooks_config()["hooks"]["sessionStart"][0]["command"]
    hooks_path = fake_home / ".cursor" / "hooks.json"
    _write_json(
        hooks_path,
        {
            "hooks": {
                "sessionStart": [
                    {"command": owned},
                    {
                        "matcher": "",
                        "hooks": [
                            {"command": owned},
                            {"command": "user-session-hook"},
                        ],
                    },
                ]
            }
        },
    )

    uninstall.run(repo=fake_repo, keep_data=True)

    assert _read_jsonc(hooks_path) == {
        "hooks": {
            "sessionStart": [
                {"matcher": "", "hooks": [{"command": "user-session-hook"}]}
            ]
        }
    }


def test_source_pr_legacy_hook_commands_are_removed_exactly(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    repo_arg = json.dumps(fake_repo.resolve().as_posix())
    legacy_repo_command = (
        "git rev-parse --git-dir >/dev/null 2>&1"
        " && code-review-graph update --skip-flows"
        f" --repo {repo_arg}"
        " || true"
    )
    legacy_codex_command = (
        "git rev-parse --git-dir >/dev/null 2>&1"
        " && code-review-graph status"
        " || echo 'Not a git repo, skipping'"
    )
    _write_json(
        fake_repo / ".claude" / "settings.json",
        {"hooks": {"PostToolUse": [{"hooks": [{"command": legacy_repo_command}]}]}},
    )
    _write_json(
        fake_home / ".codex" / "hooks.json",
        {"hooks": {"SessionStart": [{"hooks": [{"command": legacy_codex_command}]}]}},
    )

    uninstall.run(repo=fake_repo, keep_data=True)

    assert _read_jsonc(fake_repo / ".claude" / "settings.json") == {}
    assert _read_jsonc(fake_home / ".codex" / "hooks.json") == {}


def test_shared_skill_directories_keep_user_files_and_unrelated_skills(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    generated_roots = [
        fake_repo / ".claude" / "skills",
        fake_repo / ".gemini" / "skills",
        fake_repo / ".codebuddy" / "skills",
    ]
    generated_slug = next(iter(skills._SKILLS)).removesuffix(".md")
    for root in generated_roots:
        _write(root / generated_slug / "SKILL.md", "generated\n")
        _write(root / generated_slug / "notes.txt", "keep\n")
        _write(root / "user-skill" / "SKILL.md", "keep\n")

    _write(fake_repo / "skills" / "project-skill" / "SKILL.md", "source\n")
    _write(fake_repo / ".qoder" / "skills" / "project-skill" / "SKILL.md", "copy\n")
    _write(fake_repo / ".qoder" / "skills" / "project-skill" / "notes.txt", "keep\n")
    _write(fake_repo / ".qoder" / "skills" / "user-skill" / "SKILL.md", "keep\n")

    uninstall.run(repo=fake_repo, keep_data=True)

    for root in generated_roots:
        assert not (root / generated_slug / "SKILL.md").exists()
        assert (root / generated_slug / "notes.txt").exists()
        assert (root / "user-skill" / "SKILL.md").exists()
    assert not (fake_repo / ".qoder" / "skills" / "project-skill" / "SKILL.md").exists()
    assert (fake_repo / ".qoder" / "skills" / "project-skill" / "notes.txt").exists()
    assert (fake_repo / ".qoder" / "skills" / "user-skill" / "SKILL.md").exists()


def test_instruction_inventory_and_git_hook_are_surgical(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    instruction_paths = ["CLAUDE.md", *skills._PLATFORM_INSTRUCTION_FILES]
    for relative in instruction_paths:
        section = skills._PLATFORM_INSTRUCTION_CUSTOM_SECTIONS.get(
            relative,
            (skills._CLAUDE_MD_SECTION_MARKER, skills._CLAUDE_MD_SECTION),
        )[1]
        _write(
            fake_repo / relative,
            "user instructions\n\n" + section,
        )
    hook = fake_repo / ".git" / "hooks" / "pre-commit"
    _write(
        hook,
        "#!/bin/sh\necho user-hook\n"
        "# Installed by code-review-graph. Remove this file to disable pre-commit graph checks.\n"
        "if command -v code-review-graph >/dev/null 2>&1; then\n"
        "    code-review-graph update || true\n"
        "    code-review-graph detect-changes --brief || true\n"
        "fi\n",
    )

    uninstall.run(repo=fake_repo, keep_data=True)

    for relative in instruction_paths:
        assert (fake_repo / relative).read_text(encoding="utf-8") == "user instructions\n"
    assert hook.read_text(encoding="utf-8") == "#!/bin/sh\necho user-hook\n"


def test_modified_instruction_section_is_not_guessed_or_truncated(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    path = fake_repo / "CLAUDE.md"
    content = (
        "user prefix\n"
        f"{skills._CLAUDE_MD_SECTION_MARKER}\n"
        "user modified this formerly generated section\n"
        "user suffix that must not be truncated\n"
    )
    _write(path, content)

    report = uninstall.run(repo=fake_repo, keep_data=True)

    assert path.read_text(encoding="utf-8") == content
    assert any(str(path) in item and "left unchanged" in item for item in report.skipped_paths)


def test_only_installer_owned_gitignore_block_is_removed(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    gitignore = fake_repo / ".gitignore"
    _write(
        gitignore,
        "dist/\n# Added by code-review-graph\n.code-review-graph/\ncoverage/\n",
    )

    uninstall.run(repo=fake_repo, keep_data=True)

    assert gitignore.read_text(encoding="utf-8") == "dist/\ncoverage/\n"

    # An unmarked entry may have been written by the user before install.
    _write(gitignore, "dist/\n.code-review-graph/\n")
    uninstall.run(repo=fake_repo, keep_data=True)
    assert gitignore.read_text(encoding="utf-8") == "dist/\n.code-review-graph/\n"


def test_dry_run_is_meaningful_and_byte_for_byte_read_only(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    config = fake_repo / ".mcp.json"
    _write_json(config, {"mcpServers": {"code-review-graph": {}}})
    data = fake_repo / ".code-review-graph" / "graph.db"
    data.parent.mkdir()
    data.write_bytes(b"graph")
    plugin = fake_home / ".config" / "opencode" / "plugins" / "crg-plugin.ts"
    _write(plugin, "plugin")
    before = {
        path: path.read_bytes()
        for path in (config, data, plugin)
    }

    report = uninstall.run(repo=fake_repo, dry_run=True)

    assert report.total_actions >= 3
    assert any(str(config) in action for action in report.edited_paths)
    assert any(str(data.parent) in action for action in report.removed_paths)
    for path, content in before.items():
        assert path.read_bytes() == content


@pytest.mark.parametrize("failure_point", ("fsync", "replace"))
def test_failed_atomic_config_write_preserves_original_bytes(
    failure_point: str,
    fake_repo: Path,
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = fake_repo / ".mcp.json"
    _write_json(
        config,
        {"mcpServers": {"code-review-graph": {}, "other": {"command": "mine"}}},
    )
    original = config.read_bytes()

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError(f"simulated {failure_point} failure")

    monkeypatch.setattr(uninstall.os, failure_point, fail)

    report = uninstall.run(
        repo=fake_repo,
        keep_data=True,
        keep_user_configs=True,
    )

    assert config.read_bytes() == original
    assert not list(config.parent.glob(f".{config.name}.*.tmp"))
    assert any(
        str(config) in error and f"simulated {failure_point} failure" in error
        for error in report.errors
    )


def test_atomic_config_replace_preserves_file_mode(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    config = fake_repo / ".mcp.json"
    _write_json(config, {"mcpServers": {"code-review-graph": {}, "other": {}}})
    config.chmod(0o640)

    report = uninstall.run(
        repo=fake_repo,
        keep_data=True,
        keep_user_configs=True,
    )

    assert report.errors == []
    assert stat.S_IMODE(config.stat().st_mode) == 0o640
    assert _read_jsonc(config) == {"mcpServers": {"other": {}}}


def test_non_repository_directory_is_refused_without_deleting_data(
    tmp_path: Path,
    fake_home: Path,
) -> None:
    ordinary_directory = tmp_path / "ordinary-directory"
    data = ordinary_directory / ".code-review-graph" / "unrelated.txt"
    config = ordinary_directory / ".mcp.json"
    _write(data, "not owned by CRG")
    _write_json(config, {"mcpServers": {"code-review-graph": {}}})

    report = uninstall.run(
        repo=ordinary_directory,
        keep_user_configs=True,
    )

    assert data.read_text(encoding="utf-8") == "not owned by CRG"
    assert _read_jsonc(config) == {"mcpServers": {"code-review-graph": {}}}
    assert report.total_actions == 0
    assert any(
        str(ordinary_directory) in item and "Git or SVN repository" in item
        for item in report.skipped_paths
    )


def test_repository_subdirectory_normalises_to_vcs_root(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    nested = fake_repo / "src" / "package"
    nested.mkdir(parents=True)
    data = fake_repo / ".code-review-graph" / "graph.db"
    data.parent.mkdir()
    data.write_bytes(b"graph")

    report = uninstall.run(
        repo=nested,
        keep_user_configs=True,
    )

    assert report.errors == []
    assert not data.parent.exists()


def test_symlink_and_out_of_boundary_paths_are_skipped(
    fake_repo: Path,
    fake_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside_data = tmp_path / "outside-data"
    outside_data.mkdir()
    _write(outside_data / "keep.txt", "keep")
    os.symlink(outside_data, fake_repo / ".code-review-graph", target_is_directory=True)

    outside_config = tmp_path / "outside-config.json"
    _write_json(outside_config, {"servers": {"code-review-graph": {}, "other": {}}})
    monkeypatch.setitem(
        skills.PLATFORMS,
        "malicious-path",
        {
            "name": "Malicious",
            "config_path": lambda root: outside_config,
            "key": "servers",
            "detect": lambda: True,
            "format": "object",
            "needs_type": False,
        },
    )

    report = uninstall.run(repo=fake_repo)

    assert (outside_data / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert (fake_repo / ".code-review-graph").is_symlink()
    assert _read_jsonc(outside_config)["servers"] == {
        "code-review-graph": {},
        "other": {},
    }
    assert any("boundary" in item or "symlink" in item for item in report.skipped_paths)


def test_malformed_config_is_unchanged_and_other_cleanup_continues(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    malformed = fake_repo / ".cursor" / "mcp.json"
    _write(malformed, '{"mcpServers": { this is not JSON')
    malformed_toml = fake_home / ".codex" / "config.toml"
    _write(
        malformed_toml,
        'broken = "unterminated\n[mcp_servers.code-review-graph]\ncommand = "crg"\n',
    )
    valid = fake_repo / ".mcp.json"
    _write_json(valid, {"mcpServers": {"code-review-graph": {}, "other": {}}})

    report = uninstall.run(repo=fake_repo, keep_data=True)

    assert malformed.read_text(encoding="utf-8") == '{"mcpServers": { this is not JSON'
    assert malformed_toml.read_text(encoding="utf-8").startswith('broken = "unterminated')
    assert _read_jsonc(valid) == {"mcpServers": {"other": {}}}
    assert any(str(malformed) in item and "parse" in item for item in report.skipped_paths)
    assert any(str(malformed_toml) in item and "parse" in item for item in report.skipped_paths)


def test_partial_filesystem_failure_is_reported_and_does_not_stop_cleanup(
    fake_repo: Path,
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = fake_repo / ".code-review-graph.db"
    blocked.write_bytes(b"db")
    removable = fake_repo / ".code-review-graph.db-wal"
    removable.write_bytes(b"wal")
    original_unlink = Path.unlink

    def fail_one(path: Path, *args: object, **kwargs: object) -> None:
        if path == blocked:
            raise PermissionError("simulated denial")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_one)

    report = uninstall.run(repo=fake_repo)

    assert blocked.exists()
    assert not removable.exists()
    assert any(str(blocked) in error and "simulated denial" in error for error in report.errors)


def test_second_run_is_idempotent(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    config = fake_repo / ".mcp.json"
    _write_json(config, {"mcpServers": {"code-review-graph": {}, "other": {}}})

    first = uninstall.run(repo=fake_repo, keep_data=True)
    second = uninstall.run(repo=fake_repo, keep_data=True)

    assert first.total_actions == 1
    assert second.total_actions == 0
    assert second.errors == []
    assert _read_jsonc(config) == {"mcpServers": {"other": {}}}


def test_keep_flags_preserve_data_and_user_configuration(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    repo_data = fake_repo / ".code-review-graph"
    repo_data.mkdir()
    (repo_data / "graph.db").write_bytes(b"db")
    legacy = fake_repo / ".code-review-graph.db"
    legacy.write_bytes(b"db")
    user_data = fake_home / ".code-review-graph"
    user_data.mkdir()
    (user_data / "registry.json").write_text("{}", encoding="utf-8")
    user_config = fake_home / ".qwen" / "settings.json"
    _write_json(user_config, {"mcpServers": {"code-review-graph": {}}})

    uninstall.run(
        repo=fake_repo,
        keep_data=True,
        keep_user_configs=True,
    )

    assert repo_data.exists()
    assert legacy.exists()
    assert user_data.exists()
    assert "code-review-graph" in _read_jsonc(user_config)["mcpServers"]


def test_all_repos_reads_registry_before_removing_user_data(
    fake_repo: Path,
    fake_home: Path,
    tmp_path: Path,
) -> None:
    registered = tmp_path / "registered"
    (registered / ".git").mkdir(parents=True)
    registered_config = registered / ".mcp.json"
    _write_json(registered_config, {"mcpServers": {"code-review-graph": {}}})
    external_data = tmp_path / "external-data"
    external_data.mkdir()
    (external_data / "graph.db").write_bytes(b"keep")
    registry_dir = fake_home / ".code-review-graph"
    registry_dir.mkdir()
    _write_json(
        registry_dir / "registry.json",
        {"repos": [{"path": str(registered), "data_dir": str(external_data)}]},
    )

    report = uninstall.run(repo=fake_repo, all_repos=True)

    assert _read_jsonc(registered_config) == {"mcpServers": {}}
    assert not registry_dir.exists()
    assert (external_data / "graph.db").read_bytes() == b"keep"
    assert any(str(external_data) in item and "retained" in item for item in report.skipped_paths)


def test_cli_dry_run_and_confirmation_are_safe(
    fake_repo: Path,
    fake_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from code_review_graph import cli

    config = fake_repo / ".mcp.json"
    _write_json(config, {"mcpServers": {"code-review-graph": {}}})

    with patch.object(
        sys,
        "argv",
        ["code-review-graph", "uninstall", "--repo", str(fake_repo), "--dry-run"],
    ):
        cli.main()
    assert "dry-run" in capsys.readouterr().out.lower()
    assert "code-review-graph" in config.read_text(encoding="utf-8")

    with (
        patch.object(
            sys,
            "argv",
            ["code-review-graph", "uninstall", "--repo", str(fake_repo)],
        ),
        patch.object(cli, "_confirm_yes_no", return_value=False),
    ):
        cli.main()
    assert "aborted" in capsys.readouterr().out.lower()
    assert "code-review-graph" in config.read_text(encoding="utf-8")
