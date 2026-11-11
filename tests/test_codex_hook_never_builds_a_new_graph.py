"""The Codex edit hook updates a graph that exists and never builds a new one.

``hook-update`` builds a graph on first use in the session's repository. The
Codex skill says building one needs the user to ask, so the Codex hook passes
``--existing-only`` and a repository with no graph is left alone.
Issue crg-cjw.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from code_review_graph import hook_update, skills, uninstall

# The Codex edit hook up to 2.4.0, which ran the copy on PATH.
_V2_4_0_CODEX_UPDATE = (
    "cat | { if command -v code-review-graph >/dev/null 2>&1; "
    "then code-review-graph hook-update; else cat >/dev/null; fi; }"
)


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def _run(monkeypatch, payload: dict, *, existing_only: bool) -> list[Path]:
    calls: list[Path] = []
    monkeypatch.setattr(hook_update, "_update_repo", calls.append)
    monkeypatch.setattr(hook_update.sys, "stdin", io.StringIO(json.dumps(payload)))
    hook_update.run_update_hook(existing_only=existing_only)
    return calls


@pytest.mark.parametrize("with_file", [True, False], ids=["edit", "shell"])
def test_existing_only_skips_a_repo_with_no_graph(tmp_path, monkeypatch, with_file):
    repo = _repo(tmp_path)
    payload: dict = {"cwd": str(repo)}
    if with_file:
        payload["tool_input"] = {"file_path": str(repo / "a.py")}

    assert _run(monkeypatch, payload, existing_only=True) == []
    # Without the flag the session repository is still built on first use.
    assert _run(monkeypatch, payload, existing_only=False) == [repo]


@pytest.mark.parametrize("with_file", [True, False], ids=["edit", "shell"])
def test_existing_only_updates_a_repo_with_a_graph(tmp_path, monkeypatch, with_file):
    repo = _repo(tmp_path)
    (repo / ".code-review-graph").mkdir()
    (repo / ".code-review-graph" / "graph.db").write_bytes(b"")
    payload: dict = {"cwd": str(repo)}
    if with_file:
        payload["tool_input"] = {"file_path": str(repo / "a.py")}

    assert _run(monkeypatch, payload, existing_only=True) == [repo]


def test_cli_passes_existing_only(tmp_path, monkeypatch):
    from code_review_graph import cli

    seen: list[bool] = []
    monkeypatch.setattr(
        hook_update, "run_update_hook", lambda existing_only=False: seen.append(existing_only)
    )
    for argv in (["hook-update", "--existing-only"], ["hook-update"]):
        monkeypatch.setattr("sys.argv", ["code-review-graph", *argv])
        cli.main()
    assert seen == [True, False]


def test_codex_hook_passes_existing_only():
    for program in (None, skills._PATH_HOOK_PROGRAM):
        config = skills.generate_codex_hooks_config(Path("."), program)
        command = config["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        assert "hook-update --existing-only" in command
        assert skills._is_generated_hook_command(command), command
    # Claude Code still builds on first use.
    claude = skills.generate_hooks_config(Path("."))
    assert "--existing-only" not in claude["hooks"]["PostToolUse"][0]["hooks"][0]["command"]


def test_old_codex_hook_is_still_ours(tmp_path, monkeypatch):
    pinned_old = skills._codex_hook_command("hook-update", skills._hook_program())
    for command in (_V2_4_0_CODEX_UPDATE, pinned_old):
        assert skills._is_generated_hook_command(command), command

    home = tmp_path / "home"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    repo = _repo(tmp_path)
    for command in (_V2_4_0_CODEX_UPDATE, pinned_old):
        (codex / "hooks.json").write_text(
            json.dumps(
                {
                    "note": "kept",
                    "hooks": {
                        "PostToolUse": [
                            {
                                "matcher": "Write|Edit|Bash",
                                "hooks": [{"type": "command", "command": command}],
                            }
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        uninstall.run(repo=repo, keep_data=True)
        assert json.loads((codex / "hooks.json").read_text(encoding="utf-8")) == {
            "note": "kept"
        }, command
