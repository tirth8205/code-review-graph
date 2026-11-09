"""The opt-in steering hook: graph context beside Claude Code's searches and reads.

``enrich.py`` existed, but ``install`` never wrote the hook that runs it. The hook
changes what the model sees on every search and read, so it stays off unless asked
for, and it must never get in the way of the tool call it sits in front of.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from code_review_graph import cli, enrich, skills, uninstall


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git" / "hooks").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    return repo


def _run(repo: Path, *args: str) -> None:
    with patch.object(sys, "argv", [
        "code-review-graph", *args, "--repo", str(repo), "--no-instructions", "--yes",
    ]):
        cli.main()


def _settings(repo: Path) -> dict:
    path = repo / ".claude" / "settings.json"
    if not path.exists():
        return {}
    return json.loads(skills._strip_jsonc(path.read_text(encoding="utf-8")))


def _pre_tool_commands(repo: Path) -> list[str]:
    return [
        hook.get("command", "")
        for group in _settings(repo).get("hooks", {}).get("PreToolUse", [])
        for hook in group.get("hooks", [])
    ]


STEERING_COMMAND = skills.generate_steering_hooks_config()["hooks"]["PreToolUse"][0][
    "hooks"
][0]["command"]


class TestOptIn:
    def test_plain_install_does_not_add_it(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path, monkeypatch)
        _run(repo, "install", "--platform", "claude")

        assert _settings(repo)["hooks"]["PostToolUse"]
        assert _pre_tool_commands(repo) == []

    @pytest.mark.parametrize("command", ["install", "init"])
    def test_flag_adds_it(self, tmp_path, monkeypatch, capsys, command):
        repo = _repo(tmp_path, monkeypatch)
        _run(repo, command, "--platform", "claude", "--steering")

        group = _settings(repo)["hooks"]["PreToolUse"][0]
        assert group["matcher"] == "Grep|Glob|Bash|Read"
        assert group["hooks"][0]["command"] == STEERING_COMMAND
        # A slow graph must not hold up the tool call.
        assert group["hooks"][0]["timeout"] <= 5
        assert "steering hook" in capsys.readouterr().out

    def test_all_platforms_counts_as_claude(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path, monkeypatch)
        _run(repo, "install", "--steering")

        assert _pre_tool_commands(repo) == [STEERING_COMMAND]
        # Qoder reads its own settings file and does not take this hook's output.
        qoder = repo / ".qoder" / "settings.json"
        if qoder.exists():
            assert "PreToolUse" not in json.loads(qoder.read_text(encoding="utf-8"))["hooks"]

    def test_other_platform_says_it_skipped(self, tmp_path, monkeypatch, capsys):
        repo = _repo(tmp_path, monkeypatch)
        _run(repo, "install", "--platform", "codex", "--steering")

        assert _pre_tool_commands(repo) == []
        assert "only for Claude Code" in capsys.readouterr().out

    def test_no_hooks_wins(self, tmp_path, monkeypatch, capsys):
        repo = _repo(tmp_path, monkeypatch)
        _run(repo, "install", "--platform", "claude", "--steering", "--no-hooks")

        assert _pre_tool_commands(repo) == []
        assert "--no-hooks" in capsys.readouterr().out

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch, capsys):
        repo = _repo(tmp_path, monkeypatch)
        _run(repo, "install", "--platform", "claude", "--steering", "--dry-run")

        assert not (repo / ".claude" / "settings.json").exists()
        assert "steering hook" in capsys.readouterr().out

    def test_reinstall_does_not_repeat_it(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path, monkeypatch)
        _run(repo, "install", "--platform", "claude", "--steering")
        _run(repo, "install", "--platform", "claude", "--steering")

        assert _pre_tool_commands(repo) == [STEERING_COMMAND]

    def test_plain_reinstall_keeps_the_choice(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path, monkeypatch)
        _run(repo, "install", "--platform", "claude", "--steering")
        _run(repo, "install", "--platform", "claude")

        assert _pre_tool_commands(repo) == [STEERING_COMMAND]


class TestSwitchingOff:
    def test_no_steering_removes_only_it(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path, monkeypatch)
        _run(repo, "install", "--platform", "claude", "--steering")
        before = _settings(repo)["hooks"]

        _run(repo, "install", "--platform", "claude", "--no-steering")

        after = _settings(repo)["hooks"]
        assert "PreToolUse" not in after
        assert after["PostToolUse"] == before["PostToolUse"]
        assert after["SessionStart"] == before["SessionStart"]

    def test_no_steering_with_nothing_there_is_fine(self, tmp_path, monkeypatch, capsys):
        repo = _repo(tmp_path, monkeypatch)
        _run(repo, "install", "--platform", "claude", "--no-steering")

        assert _pre_tool_commands(repo) == []
        assert "no steering hook" in capsys.readouterr().out.lower()

    def test_no_steering_dry_run_keeps_it(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path, monkeypatch)
        _run(repo, "install", "--platform", "claude", "--steering")

        _run(repo, "install", "--platform", "claude", "--no-steering", "--dry-run")

        assert _pre_tool_commands(repo) == [STEERING_COMMAND]

    def test_no_steering_for_another_platform_leaves_it(self, tmp_path, monkeypatch, capsys):
        repo = _repo(tmp_path, monkeypatch)
        _run(repo, "install", "--platform", "claude", "--steering")
        capsys.readouterr()

        _run(repo, "install", "--platform", "codex", "--no-steering")

        assert _pre_tool_commands(repo) == [STEERING_COMMAND]
        assert "only for Claude Code" in capsys.readouterr().out

    def test_uninstall_removes_it(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path, monkeypatch)
        _run(repo, "install", "--platform", "claude", "--steering")

        with patch.object(sys, "argv", [
            "code-review-graph", "uninstall", "--repo", str(repo), "--yes",
        ]):
            cli.main()

        assert _pre_tool_commands(repo) == []
        assert "code-review-graph enrich" not in (
            (repo / ".claude" / "settings.json").read_text(encoding="utf-8")
            if (repo / ".claude" / "settings.json").exists()
            else ""
        )


class TestUserHooks:
    USER_GROUP = {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "my-audit-log"}],
    }

    def _plant(self, repo: Path, settings: dict) -> None:
        path = repo / ".claude" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings), encoding="utf-8")

    def test_install_keeps_a_user_hook_on_the_same_event(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path, monkeypatch)
        self._plant(repo, {"hooks": {"PreToolUse": [self.USER_GROUP]}})

        _run(repo, "install", "--platform", "claude", "--steering")
        _run(repo, "install", "--platform", "claude", "--steering")

        assert _pre_tool_commands(repo) == ["my-audit-log", STEERING_COMMAND]

    def test_install_keeps_a_user_hook_in_the_same_group(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path, monkeypatch)
        mine = {"type": "command", "command": "my-audit-log"}
        group = {"matcher": "Grep|Glob|Bash|Read", "hooks": [mine]}
        self._plant(repo, {"hooks": {"PreToolUse": [group]}})

        _run(repo, "install", "--platform", "claude", "--steering")
        _run(repo, "install", "--platform", "claude", "--steering")

        assert sorted(_pre_tool_commands(repo)) == sorted(["my-audit-log", STEERING_COMMAND])

    @pytest.mark.parametrize("off", ["no-steering", "uninstall"])
    def test_removal_keeps_a_user_hook(self, tmp_path, monkeypatch, off):
        repo = _repo(tmp_path, monkeypatch)
        self._plant(repo, {"hooks": {"PreToolUse": [self.USER_GROUP]}})
        _run(repo, "install", "--platform", "claude", "--steering")

        if off == "no-steering":
            _run(repo, "install", "--platform", "claude", "--no-steering")
        else:
            with patch.object(sys, "argv", [
                "code-review-graph", "uninstall", "--repo", str(repo), "--yes",
            ]):
                cli.main()

        assert _pre_tool_commands(repo) == ["my-audit-log"]

    @pytest.mark.parametrize("command", [
        "code-review-graph status",
        "code-review-graph update --quiet",
    ])
    def test_install_keeps_other_project_commands_under_its_matcher(
        self, tmp_path, monkeypatch, command,
    ):
        """The installer writes these on other events, so here they are the user's."""
        repo = _repo(tmp_path, monkeypatch)
        group = {
            "matcher": "Grep|Glob|Bash|Read",
            "hooks": [{"type": "command", "command": command}],
        }
        self._plant(repo, {"hooks": {"PreToolUse": [group]}})

        _run(repo, "install", "--platform", "claude", "--steering")

        assert sorted(_pre_tool_commands(repo)) == sorted([command, STEERING_COMMAND])

    @pytest.mark.parametrize("off", ["no-steering", "uninstall"])
    def test_removal_keeps_a_copy_filed_elsewhere(self, tmp_path, monkeypatch, off):
        """Only the hook under the steering event and matcher is ours to remove."""
        repo = _repo(tmp_path, monkeypatch)
        _run(repo, "install", "--platform", "claude", "--steering")
        settings = _settings(repo)
        copy = {"type": "command", "command": STEERING_COMMAND}
        settings["hooks"]["PreToolUse"].append({"matcher": "Bash", "hooks": [copy]})
        settings["hooks"]["Stop"] = [{"hooks": [copy]}]
        self._plant(repo, settings)

        if off == "no-steering":
            _run(repo, "install", "--platform", "claude", "--no-steering")
        else:
            with patch.object(sys, "argv", [
                "code-review-graph", "uninstall", "--repo", str(repo), "--yes",
            ]):
                cli.main()

        after = _settings(repo)["hooks"]
        assert after["PreToolUse"] == [{"matcher": "Bash", "hooks": [copy]}]
        assert after["Stop"] == [{"hooks": [copy]}]

    @pytest.mark.parametrize("folder", [".qoder", ".codebuddy"])
    def test_uninstall_leaves_it_in_other_platforms(self, tmp_path, monkeypatch, folder):
        """Install writes the steering hook for Claude Code only."""
        repo = _repo(tmp_path, monkeypatch)
        path = repo / folder / "settings.json"
        path.parent.mkdir(parents=True)
        group = {
            "matcher": "Grep|Glob|Bash|Read",
            "hooks": [{"type": "command", "command": STEERING_COMMAND}],
        }
        settings = {"hooks": {"PreToolUse": [group]}}
        path.write_text(json.dumps(settings), encoding="utf-8")

        with patch.object(sys, "argv", [
            "code-review-graph", "uninstall", "--repo", str(repo), "--yes",
        ]):
            cli.main()

        assert json.loads(path.read_text(encoding="utf-8")) == settings

    def test_removal_keeps_comments_in_the_file(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path, monkeypatch)
        _run(repo, "install", "--platform", "claude", "--steering")
        path = repo / ".claude" / "settings.json"
        raw = path.read_text(encoding="utf-8")
        path.write_text("// my note\n" + raw, encoding="utf-8")

        report = uninstall.remove_steering_hook(repo)

        assert report.total_actions == 1
        text = path.read_text(encoding="utf-8")
        assert "// my note" in text
        assert "PreToolUse" not in text

    @pytest.mark.parametrize("command", [
        "code-review-graph enrich",
        "code-review-graph enrich && notify-team",
        "bash /opt/my-tools/enrich.sh",
    ])
    def test_a_hand_written_enrich_hook_is_the_users(self, tmp_path, monkeypatch, command):
        repo = _repo(tmp_path, monkeypatch)
        group = {
            "matcher": "Grep|Glob|Bash|Read",
            "hooks": [{"type": "command", "command": command}],
        }
        self._plant(repo, {"hooks": {"PreToolUse": [group]}})

        assert not skills._is_steering_hook_command(command)
        _run(repo, "install", "--platform", "claude", "--steering")
        assert command in _pre_tool_commands(repo)

        _run(repo, "install", "--platform", "claude", "--no-steering")
        assert _pre_tool_commands(repo) == [command]

    def test_our_command_is_recognised(self):
        assert skills._is_steering_hook_command(STEERING_COMMAND)
        # Only the steering install and removal claim it.
        assert not skills._is_generated_hook_command(STEERING_COMMAND)


@pytest.mark.skipif(os.name == "nt", reason="the hook command needs a POSIX shell")
class TestHookNeverBlocksTheToolCall:
    """Exit code 2 from a PreToolUse hook would stop the tool call in Claude Code."""

    def _shell(self, tmp_path: Path, script: str | None) -> subprocess.CompletedProcess:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        if script is not None:
            stub = bin_dir / "code-review-graph"
            stub.write_text(f"#!/bin/sh\ncat >/dev/null\n{script}\n", encoding="utf-8")
            stub.chmod(0o755)
        env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"}
        return subprocess.run(
            ["sh", "-c", STEERING_COMMAND],
            input='{"tool_name": "Grep"}', capture_output=True, text=True, env=env,
        )

    def test_output_reaches_the_client(self, tmp_path):
        done = self._shell(tmp_path, "echo '{\"ok\": true}'")

        assert done.returncode == 0
        assert done.stdout.strip() == '{"ok": true}'

    @pytest.mark.parametrize("code", [1, 2, 127])
    def test_failure_exits_zero(self, tmp_path, code):
        assert self._shell(tmp_path, f"exit {code}").returncode == 0

    def test_program_missing_exits_zero_and_says_nothing(self, tmp_path):
        done = self._shell(tmp_path, None)

        assert done.returncode == 0
        assert done.stdout == ""


class TestRunHookStaysQuiet:
    def _hook(self, monkeypatch, capsys, stdin: str) -> str:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        enrich.run_hook()
        return capsys.readouterr().out

    @pytest.mark.parametrize("stdin", ["", "not json", "[1, 2]", '"text"', "null"])
    def test_bad_input_is_ignored(self, monkeypatch, capsys, stdin):
        assert self._hook(monkeypatch, capsys, stdin) == ""

    def test_tool_input_of_the_wrong_shape_is_ignored(self, monkeypatch, capsys, tmp_path):
        payload = json.dumps({"tool_name": "Grep", "tool_input": "x", "cwd": str(tmp_path)})
        assert self._hook(monkeypatch, capsys, payload) == ""

    def test_broken_database_is_ignored(self, monkeypatch, capsys, tmp_path):
        (tmp_path / ".git").mkdir()
        data = tmp_path / ".code-review-graph"
        data.mkdir()
        (data / "graph.db").write_bytes(b"this is not a database at all" * 100)
        payload = json.dumps({
            "tool_name": "Grep", "tool_input": {"pattern": "parse_file"}, "cwd": str(tmp_path),
        })

        assert self._hook(monkeypatch, capsys, payload) == ""
