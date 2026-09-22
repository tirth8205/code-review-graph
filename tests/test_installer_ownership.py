"""What the installer may rewrite, and what belongs to the user.

An installer that quietly deletes a hook, an MCP entry or a comment the user
wrote is worse than the bug it was fixing. Every test here plants something
user-owned in the path of a merge and demands it comes back out intact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_review_graph import skills, uninstall

# The two losses that motivated this file, quoted from the reports.
TEAM_HOOK_COMMAND = "code-review-graph build --repo /srv/mono && notify-team"
TEAM_HOOK_NAME = "team nightly graph"
ACRG_TOOLS_COMMAND = "bash /opt/acrg-tools/run.sh"


def _read_jsonc(path: Path) -> dict:
    return json.loads(skills._strip_jsonc(path.read_text(encoding="utf-8")))


def _hook_commands(settings: dict, event: str) -> list[str]:
    return [
        hook.get("command", "")
        for group in settings.get("hooks", {}).get(event, [])
        for hook in group.get("hooks", [])
    ]


# ---------------------------------------------------------------------------
# Hook ownership
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # A user's own hook that legitimately calls this CLI, with a
        # subcommand and a chained command no release has ever written.
        TEAM_HOOK_COMMAND,
        # Four characters of an unrelated directory name are not ownership.
        ACRG_TOOLS_COMMAND,
        "bash /opt/acrg-tools/crg-helper.sh",
        "code-review-graph build",
        "code-review-graph update --skip-flows && deploy",
        "code-review-graph detect-changes --brief; rm -rf /tmp/scratch",
        "/usr/local/bin/crg-wrapper",
        # No release ever wrote a bare subcommand, so this is someone's own.
        "code-review-graph update",
        "code-review-graph detect-changes",
        "code-review-graph update --skip-flows --verbose",
        # One of our scripts handed to someone else's script as an argument is
        # their command, not ours: the program being run is ``run.sh``.
        "bash /opt/acrg-tools/run.sh /home/u/.cursor/hooks/crg-update.sh",
        # A second command chained onto ours makes the whole line theirs, and
        # that stays true when the path has a space in it.
        "/home/u/.cursor/hooks/crg-update.sh && notify-team",
        "/Users/jo smith/.cursor/hooks/crg-update.sh; rm -rf /tmp/scratch",
        # An unterminated quote is a line no shell can parse, so nothing about
        # it can be claimed with certainty.
        '"/Users/jo smith/.cursor/hooks/crg-update.sh',
    ],
)
def test_user_hook_commands_are_not_claimed(command: str) -> None:
    assert skills._is_generated_hook_command(command) is False


@pytest.mark.parametrize(
    "command",
    [
        # Every shape a released version has written, oldest first.
        "code-review-graph update --quiet",
        "code-review-graph update --quiet --skip-flows",
        "code-review-graph update --skip-flows",
        "code-review-graph status",
        "code-review-graph status --json",
        "code-review-graph detect-changes --brief",
        "git rev-parse --git-dir >/dev/null 2>&1"
        " && code-review-graph update --skip-flows || true",
        "cat >/dev/null || true; git rev-parse --git-dir >/dev/null 2>&1"
        " && code-review-graph status || echo 'Not a git repo, skipping'",
        'cat >/dev/null || true; command -v code-review-graph >/dev/null 2>&1 || exit 0;'
        ' git rev-parse --git-dir >/dev/null 2>&1 && code-review-graph update'
        ' --skip-flows --repo "$(git rev-parse --show-toplevel 2>/dev/null)" || true',
        "bash .gemini/hooks/crg-update.sh",
        "/home/u/.cursor/hooks/crg-session-start.sh",
        "bash /previous/checkout/.gemini/hooks/crg-update.sh",
        # The script's own file name decides ownership, so none of the ways a
        # path can be spelled may change the answer: a space in the home
        # directory, a quoted command, a trailing argument, Windows
        # separators, a relative path.
        "/Users/jo smith/.cursor/hooks/crg-update.sh",
        '"/Users/jo smith/.cursor/hooks/crg-session-start.sh"',
        "'/Users/jo smith/.cursor/hooks/crg-pre-commit.sh'",
        "bash /Users/jo smith/.gemini/hooks/crg-update.sh",
        'bash "/Users/jo smith/.gemini/hooks/crg-update.sh"',
        "C:\\Users\\jo\\.cursor\\hooks\\crg-update.sh",
        "C:\\Users\\jo smith\\.cursor\\hooks\\crg-pre-commit.sh",
        ".cursor/hooks/crg-update.sh",
        "/home/u/.cursor/hooks/crg-update.sh --repo /srv/mono",
        # v2.3.3-v2.3.6 pinned the repo with ``json.dumps``, so the path
        # arrives quoted and a space in it is still our own command.
        "cat >/dev/null || true; git rev-parse --git-dir >/dev/null 2>&1"
        " && code-review-graph update --skip-flows"
        ' --repo "/Users/jo smith/mono repo" || true',
        "cat >/dev/null || true; git rev-parse --git-dir >/dev/null 2>&1"
        ' && code-review-graph status --repo "/Users/jo smith/mono repo"'
        " || echo 'Not a git repo, skipping'",
    ],
)
def test_generated_hook_commands_are_claimed(command: str) -> None:
    assert skills._is_generated_hook_command(command) is True


def test_install_twice_under_a_home_with_a_space_leaves_one_cursor_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A space in the home directory must not stack a second hook beside ours.

    Cursor's command is the absolute path of the script, so on a machine whose
    home directory has a space in it the previous release's entry only looks
    like a foreign command if ownership is decided by whitespace.
    """
    home = tmp_path / "jo smith"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    skills.install_cursor_hooks()
    skills.install_cursor_hooks()

    config = json.loads((home / ".cursor" / "hooks.json").read_text(encoding="utf-8"))
    for event, entries in config["hooks"].items():
        assert len(entries) == 1, f"{event} kept a duplicate hook: {entries}"


def test_a_quoted_cursor_hook_from_an_earlier_release_is_replaced() -> None:
    """A release that quoted the command wrote the same hook, not a new one."""
    quoted = '"/Users/jo smith/.cursor/hooks/crg-update.sh"'
    merged = skills._merge_flat_hook_entries(
        [{"command": quoted, "timeout": 5}, {"command": ACRG_TOOLS_COMMAND, "timeout": 9}],
        [{"command": "/Users/jo smith/.cursor/hooks/crg-update.sh", "timeout": 5}],
    )
    assert merged == [
        {"command": "/Users/jo smith/.cursor/hooks/crg-update.sh", "timeout": 5},
        {"command": ACRG_TOOLS_COMMAND, "timeout": 9},
    ]


def test_install_keeps_a_users_own_hook_on_another_matcher(tmp_path: Path) -> None:
    """A PostToolUse hook on matcher ``Write`` is the user's, not ours."""
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Write",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": TEAM_HOOK_COMMAND,
                                    "name": TEAM_HOOK_NAME,
                                    "timeout": 600,
                                }
                            ],
                        }
                    ]
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    skills._merge_hooks_into_settings(
        settings_dir, skills.generate_hooks_config(tmp_path)
    )

    settings = json.loads((settings_dir / "settings.json").read_text(encoding="utf-8"))
    commands = _hook_commands(settings, "PostToolUse")
    assert TEAM_HOOK_COMMAND in commands
    survivor = next(
        hook
        for group in settings["hooks"]["PostToolUse"]
        for hook in group["hooks"]
        if hook["command"] == TEAM_HOOK_COMMAND
    )
    assert survivor["name"] == TEAM_HOOK_NAME
    assert survivor["timeout"] == 600


def test_install_keeps_an_unrelated_script_under_our_own_matcher(tmp_path: Path) -> None:
    """``acrg-tools`` shares four characters with us and nothing else."""
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Edit|Write",
                            "hooks": [
                                {"type": "command", "command": ACRG_TOOLS_COMMAND}
                            ],
                        }
                    ]
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    skills._merge_hooks_into_settings(
        settings_dir, skills.generate_hooks_config(tmp_path)
    )

    settings = json.loads((settings_dir / "settings.json").read_text(encoding="utf-8"))
    assert ACRG_TOOLS_COMMAND in _hook_commands(settings, "PostToolUse")


def test_replacing_our_hook_keeps_its_position(tmp_path: Path) -> None:
    """A hook that ran first goes on running first after a reinstall."""
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Edit|Write|Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "code-review-graph update --skip-flows",
                                }
                            ],
                        },
                        {
                            "matcher": "Write",
                            "hooks": [
                                {"type": "command", "command": TEAM_HOOK_COMMAND}
                            ],
                        },
                    ]
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    skills._merge_hooks_into_settings(
        settings_dir, skills.generate_hooks_config(tmp_path)
    )

    settings = json.loads((settings_dir / "settings.json").read_text(encoding="utf-8"))
    groups = settings["hooks"]["PostToolUse"]
    assert len(groups) == 2
    assert skills._is_generated_hook_command(groups[0]["hooks"][0]["command"])
    assert groups[1]["hooks"][0]["command"] == TEAM_HOOK_COMMAND


def test_cursor_hook_replacement_keeps_its_position() -> None:
    entries = [
        {"command": "/home/u/.cursor/hooks/crg-update.sh", "timeout": 5},
        {"command": ACRG_TOOLS_COMMAND, "timeout": 9},
    ]
    merged = skills._merge_flat_hook_entries(entries, [{"command": "new", "timeout": 5}])
    assert merged == [
        {"command": "new", "timeout": 5},
        {"command": ACRG_TOOLS_COMMAND, "timeout": 9},
    ]


# ---------------------------------------------------------------------------
# MCP entry ownership
# ---------------------------------------------------------------------------


HAND_TUNED_ENTRY = {
    "command": "uv",
    "args": ["run", "--project", "/home/me/crg", "code-review-graph", "serve"],
    "type": "stdio",
}


@pytest.mark.parametrize(
    "entry",
    [
        # The exact line the troubleshooting guide asks people to write.
        HAND_TUNED_ENTRY,
        {"command": "uvx", "args": ["code-review-graph", "serve", "--log-level", "debug"]},
        {"command": "docker", "args": ["run", "crg", "code-review-graph", "serve"]},
        {"command": "uvx", "args": ["code-review-graph", "serve"], "type": "sse"},
        {
            "command": "uvx",
            "args": ["code-review-graph", "serve"],
            "env": {"CRG_TOKEN": "secret"},
        },
        {"command": "uvx", "args": ["code-review-graph", "serve"], "tools": ["query_graph"]},
    ],
)
def test_hand_written_server_entries_are_not_claimed(entry: dict) -> None:
    assert skills._is_generated_server_entry(entry) is False


@pytest.mark.parametrize(
    "entry",
    [
        {"command": "uvx", "args": ["code-review-graph", "serve"]},
        {"command": "uvx", "args": ["code-review-graph", "serve"], "type": "stdio"},
        {"command": "code-review-graph", "args": ["serve"]},
        {"command": "poetry", "args": ["run", "code-review-graph", "serve"]},
        {"command": "uv", "args": ["run", "code-review-graph", "serve"], "cwd": "/repo"},
        {
            "command": "/opt/previous-release/bin/python3.9",
            "args": ["-m", "code_review_graph", "serve"],
            "cwd": "/previous/checkout",
        },
        {"type": "local", "command": ["uvx", "code-review-graph", "serve", "--repo", "/r"]},
        {"command": "uvx", "args": ["code-review-graph", "serve"], "env": []},
    ],
)
def test_generated_server_entries_are_claimed(entry: dict) -> None:
    assert skills._is_generated_server_entry(entry) is True


def test_install_leaves_a_hand_tuned_entry_alone(tmp_path: Path, capsys) -> None:
    """The ``--project`` flag someone added survives a reinstall."""
    config = tmp_path / ".mcp.json"
    original = json.dumps({"mcpServers": {"code-review-graph": HAND_TUNED_ENTRY}}, indent=2)
    config.write_text(original + "\n", encoding="utf-8")

    configured = skills.install_platform_configs(tmp_path, target="claude")

    assert config.read_text(encoding="utf-8") == original + "\n"
    assert "--project" in config.read_text(encoding="utf-8")
    # A hand-written entry is not something this installer configured.
    assert configured == []
    assert "hand-written" in capsys.readouterr().out


def test_install_leaves_a_hand_tuned_codex_table_alone(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    original = (
        "[mcp_servers.code-review-graph]\n"
        'command = "uv"\n'
        'args = ["run", "--project", "/home/me/crg", "code-review-graph", "serve"]\n'
    )
    config.write_text(original, encoding="utf-8")

    changed = skills._merge_toml_mcp_server(
        config, "code-review-graph", {"command": "uvx", "args": ["x", "serve"]}
    )

    # None means refused, so the caller does not report it as configured.
    assert changed is None
    assert config.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# TOML splice boundaries
# ---------------------------------------------------------------------------


def _codex_config(tail: str) -> str:
    return (
        "[mcp_servers.code-review-graph]\n"
        'command = "uvx"\n'
        'args = ["code-review-graph", "serve"]\n'
        + tail
    )


def test_toml_replacement_keeps_the_comment_belonging_to_the_next_table(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        _codex_config(
            "\n"
            "# IMPORTANT: on-call paging bridge. DO NOT REMOVE.\n"
            "[mcp_servers.pager]\n"
            'command = "pagerd"\n'
        ),
        encoding="utf-8",
    )

    changed = skills._merge_toml_mcp_server(
        config,
        "code-review-graph",
        {"command": "uvx", "args": ["code-review-graph", "serve"], "cwd": str(tmp_path)},
    )

    text = config.read_text(encoding="utf-8")
    assert changed is True
    assert "# IMPORTANT: on-call paging bridge. DO NOT REMOVE." in text
    assert "[mcp_servers.pager]" in text
    assert f'cwd = "{tmp_path}"' in text


def test_toml_replacement_keeps_a_free_standing_comment_at_end_of_file(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        _codex_config("\n# remember to add the staging server here\n"),
        encoding="utf-8",
    )

    changed = skills._merge_toml_mcp_server(
        config,
        "code-review-graph",
        {"command": "uvx", "args": ["code-review-graph", "serve"], "cwd": str(tmp_path)},
    )

    text = config.read_text(encoding="utf-8")
    assert changed is True
    assert "# remember to add the staging server here" in text


# ---------------------------------------------------------------------------
# JSONC comments
# ---------------------------------------------------------------------------


def test_opencode_install_keeps_comments(tmp_path: Path) -> None:
    config = tmp_path / "opencode.jsonc"
    config.write_text(
        "{\n"
        "  // the whole team reads this file\n"
        '  "theme": "system",\n'
        '  "mcp": {\n'
        "    // DO NOT REMOVE: the deploy bot depends on this one\n"
        '    "deploy-bot": {"type": "local", "command": ["deploy-bot"]}\n'
        "  }\n"
        "}\n",
        encoding="utf-8",
    )

    skills.install_platform_configs(tmp_path, target="opencode")

    text = config.read_text(encoding="utf-8")
    assert "// the whole team reads this file" in text
    assert "// DO NOT REMOVE: the deploy bot depends on this one" in text
    data = _read_jsonc(config)
    assert data["theme"] == "system"
    assert "deploy-bot" in data["mcp"]
    assert "code-review-graph" in data["mcp"]


def test_opencode_reinstall_keeps_comments_when_replacing_a_stale_entry(
    tmp_path: Path,
) -> None:
    config = tmp_path / "opencode.jsonc"
    config.write_text(
        "{\n"
        '  "mcp": {\n'
        "    // installed by an older release\n"
        '    "code-review-graph": {\n'
        '      "type": "local",\n'
        '      "command": ["uvx", "code-review-graph", "serve", "--repo", "/gone"]\n'
        "    }\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )

    skills.install_platform_configs(tmp_path, target="opencode")

    text = config.read_text(encoding="utf-8")
    assert "// installed by an older release" in text
    data = _read_jsonc(config)
    assert data["mcp"]["code-review-graph"]["command"][-1] == str(tmp_path)


# ---------------------------------------------------------------------------
# Git hook markers
# ---------------------------------------------------------------------------


ORPHAN_HOOK = (
    "#!/bin/sh\n"
    "echo user-hook\n"
    f"{skills._GIT_HOOK_BEGIN_MARKER}\n"
    f"{skills._GIT_HOOK_NOTE}\n"
    f"{skills._GIT_HOOK_BODY}"
    "echo more-user-hook\n"
)


def test_install_refuses_a_block_whose_end_marker_was_deleted(tmp_path: Path) -> None:
    hooks = tmp_path / ".git" / "hooks"
    hooks.mkdir(parents=True)
    hook = hooks / "pre-commit"
    hook.write_text(ORPHAN_HOOK, encoding="utf-8")

    skills.install_git_hook(tmp_path)

    assert hook.read_text(encoding="utf-8") == ORPHAN_HOOK


def test_uninstall_refuses_a_block_whose_end_marker_was_deleted(tmp_path: Path) -> None:
    """The documented behaviour: refused, reported once, nothing written."""
    repo = tmp_path / "repo"
    (repo / ".git" / "hooks").mkdir(parents=True)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(ORPHAN_HOOK, encoding="utf-8")

    report = uninstall.UninstallReport()
    uninstall._remove_git_hook(repo, report, dry_run=False)

    assert hook.read_text(encoding="utf-8") == ORPHAN_HOOK
    assert report.edited_paths == []
    assert report.removed_paths == []
    named = [entry for entry in report.skipped_paths if str(hook) in entry]
    assert len(named) == 1
    assert "end marker" in named[0]


def test_uninstall_names_a_hand_edited_block_once(tmp_path: Path) -> None:
    """A file that was edited is reported as edited, and only as edited."""
    repo = tmp_path / "repo"
    (repo / ".git" / "hooks").mkdir(parents=True)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        + skills._GIT_HOOK_BLOCK
        + f"{skills._GIT_HOOK_NOTE}\n"
        + "if command -v code-review-graph >/dev/null 2>&1; then\n"
        + "    code-review-graph update --hand-edited || true\n"
        + "fi\n",
        encoding="utf-8",
    )

    report = uninstall.UninstallReport()
    uninstall._remove_git_hook(repo, report, dry_run=False)

    edited = [entry for entry in report.edited_paths if str(hook) in entry]
    skipped = [entry for entry in report.skipped_paths if str(hook) in entry]
    assert len(edited) == 1
    assert skipped == []
    assert "--hand-edited" in hook.read_text(encoding="utf-8")


def test_comment_only_config_keeps_its_comments(tmp_path: Path) -> None:
    """A JSONC file that is nothing but comments still gets a config, and
    still has its comments afterwards (#344 covers the empty-file case)."""
    config = tmp_path / "opencode.jsonc"
    config.write_text("// team config, fill me in\n", encoding="utf-8")

    configured = skills.install_platform_configs(tmp_path, target="opencode")

    text = config.read_text(encoding="utf-8")
    assert "// team config, fill me in" in text
    assert configured == ["OpenCode"]
    assert "code-review-graph" in _read_jsonc(config)["mcp"]


def test_a_kept_hook_that_mentions_this_project_is_reported(
    tmp_path: Path, capsys
) -> None:
    """Certainty is the condition for touching someone's config; when it is
    missing the hook stays and the user hears about it."""
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Write",
                            "hooks": [
                                {"type": "command", "command": TEAM_HOOK_COMMAND}
                            ],
                        }
                    ]
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    skills._merge_hooks_into_settings(
        settings_dir, skills.generate_hooks_config(tmp_path)
    )

    out = capsys.readouterr().out
    assert "kept a hook command this installer did not write" in out
    assert TEAM_HOOK_COMMAND in out


def test_an_unrelated_hook_is_not_reported(tmp_path: Path, capsys) -> None:
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Write",
                            "hooks": [{"type": "command", "command": "make lint"}],
                        }
                    ]
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    skills._merge_hooks_into_settings(
        settings_dir, skills.generate_hooks_config(tmp_path)
    )

    assert "kept a hook command" not in capsys.readouterr().out


def test_uninstall_keeps_the_comment_belonging_to_the_next_toml_table(
    tmp_path: Path,
) -> None:
    """Removal stops where replacement does: short of the next table's note."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    config = tmp_path / "config.toml"
    config.write_text(
        'theme = "dark"\n\n'
        "[mcp_servers.code-review-graph]\n"
        'command = "uvx"\n'
        'args = ["code-review-graph", "serve"]\n\n'
        "# IMPORTANT: on-call paging bridge. DO NOT REMOVE.\n"
        "[mcp_servers.pager]\n"
        'command = "pagerd"\n',
        encoding="utf-8",
    )

    report = uninstall.UninstallReport()
    uninstall._remove_toml_entry(
        config, "mcp_servers", tmp_path, report, dry_run=False
    )

    text = config.read_text(encoding="utf-8")
    assert "[mcp_servers.code-review-graph]" not in text
    assert "# IMPORTANT: on-call paging bridge. DO NOT REMOVE." in text
    assert "[mcp_servers.pager]" in text
    assert 'theme = "dark"' in text
