"""The session and worktree hooks act only in a Git checkout, and say so truly.

The session hook ran ``git ... && status || echo 'Not a git repo'``, so a
failing ``status`` (a Git repo with no graph yet) also printed "Not a git
repo". The plugin's EnterWorktree hook ran ``build`` with no Git check and made
``.code-review-graph`` in any folder. Issue crg-2v0.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from code_review_graph import skills, uninstall

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="runs the POSIX hook commands"
)

HOOKS_JSON = Path(__file__).resolve().parent.parent / "hooks" / "hooks.json"

# The session hook up to 2.4.0, which ran the copy on PATH.
_V2_4_0_SESSION_COMMAND = (
    "cat >/dev/null || true; command -v code-review-graph >/dev/null 2>&1 || exit 0; "
    "git rev-parse --git-dir >/dev/null 2>&1 && code-review-graph status --repo "
    "\"$(git rev-parse --show-toplevel 2>/dev/null)\" || echo 'Not a git repo, skipping'"
)


def _shipped() -> dict:
    return json.loads(HOOKS_JSON.read_text(encoding="utf-8"))


def _cli_env(tmp_path: Path, status_exit: int) -> tuple[dict[str, str], Path]:
    """A PATH holding git, cat and a fake CLI that logs each call."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("git", "cat"):
        found = shutil.which(tool)
        assert found is not None
        (bin_dir / tool).symlink_to(found)
    log = tmp_path / "cli.log"
    fake = bin_dir / "code-review-graph"
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{log}"\n'
        'if [ "$1" = status ]; then echo "No graph found."; '
        f"exit {status_exit}; fi\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env = {
        "PATH": str(bin_dir),
        "HOME": str(tmp_path),
        "GIT_CEILING_DIRECTORIES": str(tmp_path),
    }
    return env, log


def _run(command: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/sh", "-c", command],
        input="{}",
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=30,
    )


def _session_commands() -> list[str]:
    path_copy = skills.generate_hooks_config(Path("."), program=skills._PATH_HOOK_PROGRAM)
    pinned = skills.generate_hooks_config(Path("."))
    return [
        _shipped()["hooks"]["SessionStart"][0]["hooks"][0]["command"],
        path_copy["hooks"]["SessionStart"][0]["hooks"][0]["command"],
        pinned["hooks"]["SessionStart"][0]["hooks"][0]["command"],
    ]


@pytest.mark.parametrize("which", [0, 1, 2], ids=["shipped", "path-copy", "pinned"])
def test_session_hook_in_a_git_repo_with_no_graph_says_nothing_about_git(
    tmp_path, monkeypatch, which
):
    env, log = _cli_env(tmp_path, status_exit=1)
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    # The pinned hook names the copy that ran install; point it at the fake.
    fake = Path(env["PATH"]) / "code-review-graph"
    program = skills._HookProgram(
        f"[ -x {skills._sh_single_quote(str(fake))} ]", skills._sh_single_quote(str(fake))
    )
    monkeypatch.setattr(skills, "_hook_program", lambda: program)

    proc = _run(_session_commands()[which], work, env)

    assert proc.returncode == 0, proc.stderr
    assert "Not a git repo" not in proc.stdout
    assert "No graph found." in proc.stdout
    assert log.read_text(encoding="utf-8").startswith("status")


@pytest.mark.parametrize("which", [0, 1, 2], ids=["shipped", "path-copy", "pinned"])
def test_session_hook_outside_git_skips(tmp_path, which):
    env, log = _cli_env(tmp_path, status_exit=0)
    work = tmp_path / "work"
    work.mkdir()

    proc = _run(_session_commands()[which], work, env)

    assert proc.returncode == 0, proc.stderr
    assert "Not a git repo, skipping" in proc.stdout
    assert not log.exists()


def _worktree_command() -> str:
    groups = _shipped()["hooks"]["PostToolUse"]
    (group,) = [group for group in groups if group["matcher"] == "EnterWorktree"]
    return group["hooks"][0]["command"]


def test_worktree_hook_builds_nothing_outside_git(tmp_path):
    env, log = _cli_env(tmp_path, status_exit=0)
    work = tmp_path / "work"
    work.mkdir()

    proc = _run(_worktree_command(), work, env)
    time.sleep(1)

    assert proc.returncode == 0, proc.stderr
    assert not log.exists()
    assert not (work / ".code-review-graph").exists()


def test_worktree_hook_builds_in_a_git_checkout(tmp_path):
    env, log = _cli_env(tmp_path, status_exit=0)
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)

    proc = _run(_worktree_command(), work, env)
    deadline = time.monotonic() + 10
    while not log.exists() and time.monotonic() < deadline:
        time.sleep(0.05)

    assert proc.returncode == 0, proc.stderr
    assert log.read_text(encoding="utf-8").startswith("build")


def test_hooks_json_is_the_generated_plugin_config():
    assert _shipped() == skills.generate_plugin_hooks_config()


def test_old_session_hooks_are_still_ours(tmp_path, monkeypatch):
    """Install replaces, and uninstall removes, the session hook earlier code wrote."""
    pinned_old = skills._claude_session_hook_command(
        skills._hook_program(), skills._legacy_session_status_body
    )
    assert pinned_old != skills._claude_session_hook_command(skills._hook_program())
    for command in (_V2_4_0_SESSION_COMMAND, pinned_old):
        assert skills._is_generated_hook_command(command), command

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir()
    for command in (_V2_4_0_SESSION_COMMAND, pinned_old):
        settings.write_text(
            json.dumps(
                {
                    "model": "kept",
                    "hooks": {
                        "SessionStart": [
                            {"matcher": "", "hooks": [{"type": "command", "command": command}]}
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        uninstall.run(repo=repo, keep_data=True)
        assert json.loads(settings.read_text(encoding="utf-8")) == {"model": "kept"}, command
