"""Tests for ``code_review_graph.uninstall``.

These tests exercise the full uninstall workflow by:

1. Building a fake repository with every artifact ``install`` could write.
2. Building a fake user home (via ``monkeypatch.setattr(Path, "home", ...)``)
   with every user-level artifact ``install`` could write.
3. Running ``uninstall.run(dry_run=True)`` and confirming nothing changes
   but every expected action is reported.
4. Running ``uninstall.run()`` and confirming the artifacts are gone or
   surgically edited.

Where surgical edits are involved we deliberately include unrelated entries
(other MCP servers, other hooks, other content in instruction files) and
assert they survive — that is the entire point of "uninstall must not
nuke the user's other tools".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_review_graph import uninstall

# ---------------------------------------------------------------------------
# Helpers — build fake artifacts that match what skills.py writes
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def fake_repo(tmp_path: Path) -> Path:
    """Build a repository with every per-repo artifact install creates."""
    repo = tmp_path / "fake-repo"
    repo.mkdir()
    # Local DB dir.
    (repo / ".code-review-graph").mkdir()
    (repo / ".code-review-graph" / "graph.db").write_bytes(b"\x00")
    # Legacy DB file.
    (repo / ".code-review-graph.db").write_bytes(b"\x00")
    # Workspace MCP configs — mixed with an unrelated server we must keep.
    _write_json(
        repo / ".mcp.json",
        {
            "mcpServers": {
                "code-review-graph": {"command": "code-review-graph", "args": ["serve"]},
                "other-tool": {"command": "other-tool"},
            }
        },
    )
    _write_json(
        repo / ".opencode.json",
        {"mcpServers": {"code-review-graph": {"command": "x"}}},
    )
    _write_json(
        repo / ".cursor" / "mcp.json",
        {"mcpServers": {"code-review-graph": {"command": "x"}, "keepme": {"command": "y"}}},
    )
    _write_json(
        repo / ".qoder" / "mcp.json",
        {"mcpServers": {"code-review-graph": {"command": "x"}}},
    )
    _write_json(
        repo / ".kiro" / "settings" / "mcp.json",
        {"mcpServers": {"code-review-graph": {"command": "x"}}},
    )
    _write_json(
        repo / ".vscode" / "mcp.json",
        {"servers": {"code-review-graph": {"command": "x"}}},
    )
    # Hooks — claude settings has user hooks too that must survive.
    _write_json(
        repo / ".claude" / "settings.json",
        {
            "model": "claude-sonnet",
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Edit|Write|Bash",
                        "hooks": [{"type": "command", "command": "code-review-graph update"}],
                    },
                    {
                        "matcher": "Edit",
                        "hooks": [{"type": "command", "command": "user-custom-hook"}],
                    },
                ]
            },
        },
    )
    _write_json(
        repo / ".gemini" / "settings.json",
        {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "",
                        "hooks": [{
                            "type": "command",
                            "command": "bash .gemini/hooks/crg-session-start.sh",
                        }],
                    }
                ]
            }
        },
    )
    # Skills — generated.
    for name in ("explore-codebase", "review-changes", "debug-issue", "refactor-safely"):
        (repo / ".claude" / "skills" / name).mkdir(parents=True, exist_ok=True)
        (repo / ".claude" / "skills" / name / "skill.md").write_text("body")
        (repo / ".gemini" / "skills" / name).mkdir(parents=True, exist_ok=True)
        (repo / ".gemini" / "skills" / name / "SKILL.md").write_text("body")
    (repo / ".qoder" / "skills" / "explore-codebase").mkdir(parents=True)
    (repo / ".qoder" / "skills" / "explore-codebase" / "SKILL.md").write_text("body")
    # Gemini hook scripts.
    (repo / ".gemini" / "hooks").mkdir(parents=True, exist_ok=True)
    (repo / ".gemini" / "hooks" / "crg-session-start.sh").write_text("#!/bin/sh")
    (repo / ".gemini" / "hooks" / "crg-update.sh").write_text("#!/bin/sh")
    # Git pre-commit hook — installed alongside a user hook.
    git_hooks = repo / ".git" / "hooks"
    git_hooks.mkdir(parents=True)
    (git_hooks / "pre-commit").write_text(
        "#!/bin/sh\n"
        "echo \"user hook still here\"\n"
        "# Installed by code-review-graph. Remove this file to disable.\n"
        "if command -v code-review-graph >/dev/null 2>&1; then\n"
        "    code-review-graph update || true\n"
        "    code-review-graph detect-changes --brief || true\n"
        "fi\n"
    )
    # Instruction files — append our marker section.
    _write_text(
        repo / "CLAUDE.md",
        "# Project instructions\n\nDo something useful.\n\n"
        "<!-- code-review-graph MCP tools -->\nstuff added by install\n",
    )
    _write_text(
        repo / "AGENTS.md",
        "<!-- code-review-graph MCP tools -->\nonly our content\n",
    )
    # .gitignore with our entry.
    _write_text(
        repo / ".gitignore",
        "node_modules/\n# Added by code-review-graph\n.code-review-graph/\n",
    )
    return repo


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a fake ``~`` and point ``Path.home()`` at it."""
    home = tmp_path / "fake-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    # User-level registry / data dir.
    (home / ".code-review-graph").mkdir()
    (home / ".code-review-graph" / "registry.json").write_text("{}")
    # User-level MCP configs.
    _write_json(
        home / ".cursor" / "mcp.json",
        {"mcpServers": {"code-review-graph": {"command": "x"}, "other": {"command": "y"}}},
    )
    _write_json(
        home / ".continue" / "config.json",
        {"mcpServers": [{"name": "code-review-graph", "command": "x"},
                        {"name": "keep-me", "command": "y"}]},
    )
    _write_json(
        home / ".qwen" / "settings.json",
        {"mcpServers": {"code-review-graph": {"command": "x"}}},
    )
    _write_json(
        home / ".copilot" / "mcp-config.json",
        {"servers": {"code-review-graph": {"command": "x"}}},
    )
    # Codex MCP config (TOML) — interleaved with another server we must keep.
    codex_dir = home / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(
        "[mcp_servers.other]\n"
        "command = \"other\"\n"
        "\n"
        "[mcp_servers.code-review-graph]\n"
        "command = \"code-review-graph\"\n"
        "args = [\"serve\"]\n"
        "\n"
        "[other_section]\n"
        "value = 1\n"
    )
    # Codex hooks.
    _write_json(
        codex_dir / "hooks.json",
        {"hooks": {"PostToolUse": [{"hooks": [
            {"type": "command", "command": "code-review-graph update"}
        ]}]}},
    )
    # Cursor hooks + scripts.
    _write_json(
        home / ".cursor" / "hooks.json",
        {"version": 1, "hooks": {"afterEdit": [{"command": "code-review-graph update"}]}},
    )
    (home / ".cursor" / "hooks").mkdir(parents=True, exist_ok=True)
    (home / ".cursor" / "hooks" / "crg.sh").write_text("#!/bin/sh")
    # OpenCode plugin.
    (home / ".config" / "opencode" / "plugins").mkdir(parents=True)
    (home / ".config" / "opencode" / "plugins" / "crg-plugin.ts").write_text("// plugin")
    return home


# ---------------------------------------------------------------------------
# Dry-run behavior
# ---------------------------------------------------------------------------


def test_dry_run_does_not_mutate(fake_repo: Path, fake_home: Path) -> None:
    before_repo = sorted(p.relative_to(fake_repo).as_posix() for p in fake_repo.rglob("*"))
    before_home = sorted(p.relative_to(fake_home).as_posix() for p in fake_home.rglob("*"))

    report = uninstall.run(repo=fake_repo, dry_run=True)

    after_repo = sorted(p.relative_to(fake_repo).as_posix() for p in fake_repo.rglob("*"))
    after_home = sorted(p.relative_to(fake_home).as_posix() for p in fake_home.rglob("*"))

    assert before_repo == after_repo, "dry-run modified the repo"
    assert before_home == after_home, "dry-run modified ~"
    assert report.total_actions > 0


# ---------------------------------------------------------------------------
# Per-repo cleanup
# ---------------------------------------------------------------------------


def test_repo_data_dir_is_removed(fake_repo: Path, fake_home: Path) -> None:
    uninstall.run(repo=fake_repo, keep_user_configs=True)
    assert not (fake_repo / ".code-review-graph").exists()
    assert not (fake_repo / ".code-review-graph.db").exists()


def test_keep_data_preserves_data_dirs(fake_repo: Path, fake_home: Path) -> None:
    report = uninstall.run(repo=fake_repo, keep_data=True, keep_user_configs=True)
    assert (fake_repo / ".code-review-graph").exists()
    # And it should be reported as skipped, not removed.
    skipped = "\n".join(report.skipped_paths)
    assert ".code-review-graph (kept: --keep-data)" in skipped


def test_mcp_entry_removed_other_servers_preserved(fake_repo: Path, fake_home: Path) -> None:
    uninstall.run(repo=fake_repo, keep_user_configs=True)
    data = json.loads((fake_repo / ".mcp.json").read_text())
    assert "code-review-graph" not in data["mcpServers"]
    assert "other-tool" in data["mcpServers"], "unrelated MCP server was deleted"


def test_cursor_workspace_mcp_preserves_unrelated(fake_repo: Path, fake_home: Path) -> None:
    uninstall.run(repo=fake_repo, keep_user_configs=True)
    data = json.loads((fake_repo / ".cursor" / "mcp.json").read_text())
    assert "code-review-graph" not in data["mcpServers"]
    assert "keepme" in data["mcpServers"]


def test_claude_settings_keeps_user_hooks(fake_repo: Path, fake_home: Path) -> None:
    uninstall.run(repo=fake_repo, keep_user_configs=True)
    data = json.loads((fake_repo / ".claude" / "settings.json").read_text())
    # The model setting must survive.
    assert data["model"] == "claude-sonnet"
    # The user-custom hook must survive; the code-review-graph one must be gone.
    remaining = data.get("hooks", {}).get("PostToolUse", [])
    commands = [
        h["command"]
        for entry in remaining
        for h in entry.get("hooks", [])
    ]
    assert "user-custom-hook" in commands
    assert "code-review-graph update" not in commands


def test_generated_skills_dirs_are_removed(fake_repo: Path, fake_home: Path) -> None:
    uninstall.run(repo=fake_repo, keep_user_configs=True)
    for name in ("explore-codebase", "review-changes", "debug-issue", "refactor-safely"):
        assert not (fake_repo / ".claude" / "skills" / name).exists()
        assert not (fake_repo / ".gemini" / "skills" / name).exists()


def test_pre_commit_hook_preserves_user_lines(fake_repo: Path, fake_home: Path) -> None:
    uninstall.run(repo=fake_repo, keep_user_configs=True)
    hook = (fake_repo / ".git" / "hooks" / "pre-commit").read_text()
    assert "user hook still here" in hook
    assert "code-review-graph" not in hook
    assert "Installed by code-review-graph" not in hook


def test_instruction_section_removed_other_content_kept(
    fake_repo: Path, fake_home: Path,
) -> None:
    uninstall.run(repo=fake_repo, keep_user_configs=True)
    claude_md = fake_repo / "CLAUDE.md"
    assert claude_md.exists(), "CLAUDE.md should survive — it has non-CRG content"
    text = claude_md.read_text()
    assert "Project instructions" in text
    assert "<!-- code-review-graph MCP tools -->" not in text


def test_agents_md_deleted_when_only_our_content(fake_repo: Path, fake_home: Path) -> None:
    uninstall.run(repo=fake_repo, keep_user_configs=True)
    assert not (fake_repo / "AGENTS.md").exists()


def test_gitignore_entry_removed(fake_repo: Path, fake_home: Path) -> None:
    uninstall.run(repo=fake_repo, keep_user_configs=True)
    gi = (fake_repo / ".gitignore").read_text()
    assert "node_modules/" in gi
    assert ".code-review-graph" not in gi
    assert "Added by code-review-graph" not in gi


# ---------------------------------------------------------------------------
# User-level cleanup
# ---------------------------------------------------------------------------


def test_user_registry_dir_removed(fake_repo: Path, fake_home: Path) -> None:
    uninstall.run(repo=fake_repo)
    assert not (fake_home / ".code-review-graph").exists()


def test_user_cursor_mcp_only_our_entry_removed(fake_repo: Path, fake_home: Path) -> None:
    uninstall.run(repo=fake_repo)
    data = json.loads((fake_home / ".cursor" / "mcp.json").read_text())
    assert "code-review-graph" not in data["mcpServers"]
    assert "other" in data["mcpServers"]


def test_continue_array_format_keeps_unrelated(fake_repo: Path, fake_home: Path) -> None:
    uninstall.run(repo=fake_repo)
    data = json.loads((fake_home / ".continue" / "config.json").read_text())
    names = [s["name"] for s in data["mcpServers"]]
    assert "code-review-graph" not in names
    assert "keep-me" in names


def test_codex_toml_entry_removed_others_kept(fake_repo: Path, fake_home: Path) -> None:
    uninstall.run(repo=fake_repo)
    text = (fake_home / ".codex" / "config.toml").read_text()
    assert "[mcp_servers.code-review-graph]" not in text
    assert "[mcp_servers.other]" in text
    assert "[other_section]" in text


def test_opencode_plugin_file_removed(fake_repo: Path, fake_home: Path) -> None:
    uninstall.run(repo=fake_repo)
    assert not (fake_home / ".config" / "opencode" / "plugins" / "crg-plugin.ts").exists()


def test_keep_user_configs_skips_user_level(fake_repo: Path, fake_home: Path) -> None:
    uninstall.run(repo=fake_repo, keep_user_configs=True)
    # User-level files must be untouched.
    assert (fake_home / ".code-review-graph").exists()
    data = json.loads((fake_home / ".cursor" / "mcp.json").read_text())
    assert "code-review-graph" in data["mcpServers"]


# ---------------------------------------------------------------------------
# No-op behavior
# ---------------------------------------------------------------------------


def test_uninstall_in_clean_repo_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "empty-home")
    (tmp_path / "empty-home").mkdir()
    clean_repo = tmp_path / "clean"
    clean_repo.mkdir()
    report = uninstall.run(repo=clean_repo)
    assert report.removed_paths == []
    assert report.edited_paths == []
    assert report.errors == []
