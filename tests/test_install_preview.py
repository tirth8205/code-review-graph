"""``install --dry-run`` shows each change, and nothing is written early.

Issue #173: ``install`` rewrote CLAUDE.md and other files with no preview.
``--dry-run`` only listed file names, and a real install wrote the MCP config
before it asked about instruction files. Every install write now goes into a
plan first. A dry run prints that plan as diffs and writes nothing; a real
install lists it, asks its question, and then writes exactly that plan.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from code_review_graph import cli, install_plan, skills

PLATFORM_KEYS = sorted(skills.PLATFORMS)


@dataclass
class Sandbox:
    repo: Path
    home: Path

    def roots(self) -> tuple[Path, ...]:
        return (self.repo, self.home)


@pytest.fixture
def sandbox(tmp_path_factory, monkeypatch) -> Sandbox:
    """A scratch git repository and a throwaway HOME."""
    base = tmp_path_factory.mktemp("preview").resolve()
    home = base / "home"
    repo = base / "repo"
    hermes_home = home / ".hermes"
    for directory in (home, repo, hermes_home):
        directory.mkdir(parents=True, exist_ok=True)
    # Make every home-scoped platform look installed.
    for relative in (
        ".codex", ".cursor", ".codeium/windsurf", ".continue", ".gemini/antigravity",
        ".qwen", ".kiro", ".copilot", ".config/opencode",
        "Library/Application Support/Zed", ".config/zed",
    ):
        (home / relative).mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: home)
    return Sandbox(repo=repo, home=home)


def _args(sandbox: Sandbox, platform: str, **overrides) -> argparse.Namespace:
    values = {
        "repo": str(sandbox.repo),
        "dry_run": False,
        "platform": platform,
        "yes": True,
        "no_instructions": False,
        "no_skills": False,
        "no_hooks": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _snapshot(sandbox: Sandbox) -> dict[str, tuple[str, int] | str]:
    """Every directory and file under the sandbox, with content hash and mode."""
    tree: dict[str, tuple[str, int] | str] = {}
    for root in sandbox.roots():
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames:
                tree[os.path.join(dirpath, name)] = "dir"
            for name in filenames:
                path = os.path.join(dirpath, name)
                data = Path(path).read_bytes()
                tree[path] = (
                    hashlib.sha256(data).hexdigest(),
                    stat.S_IMODE(os.lstat(path).st_mode),
                )
    return tree


@pytest.fixture
def plans(monkeypatch) -> list[install_plan.Plan]:
    """Collect every plan the installer builds, so a test can read it back."""
    seen: list[install_plan.Plan] = []
    real = install_plan.planning

    @contextlib.contextmanager
    def spy():
        with real() as plan:
            seen.append(plan)
            yield plan

    monkeypatch.setattr(install_plan, "planning", spy)
    return seen


@pytest.mark.parametrize("key", PLATFORM_KEYS)
def test_dry_run_writes_nothing_and_shows_each_change(sandbox, key, plans, capsys):
    before = _snapshot(sandbox)

    cli._handle_init(_args(sandbox, key, dry_run=True))

    out = capsys.readouterr().out
    assert _snapshot(sandbox) == before
    assert "No files were modified" in out
    changes = plans[0].changes()
    # Every platform has at least an MCP config to write.
    assert changes
    for change in changes:
        shown = install_plan.display_path(change.path, sandbox.repo)
        assert f"+++ {install_plan.diff_label(shown, 'b')}" in out, shown
        # Home files are shown from ~, never as a repository path.
        assert "/~/" not in out
    assert "code-review-graph" in out


def _plant_user_files(sandbox: Sandbox) -> None:
    """Files a user already has, so installs merge rather than create."""
    repo, home = sandbox.repo, sandbox.home
    (repo / "CLAUDE.md").write_text("# Team notes\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("# Agents\n\nBe brief.\n", encoding="utf-8")
    (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    (repo / ".gemini").mkdir()
    # Two writers touch this one file: the MCP entry, then the hooks.
    (repo / ".gemini" / "settings.json").write_text('{"theme": "dark"}\n', encoding="utf-8")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(
        '{"permissions": {"allow": ["Bash"]}}\n', encoding="utf-8"
    )
    (repo / "opencode.jsonc").write_text(
        '{\n  // keep this note\n  "theme": "dark"\n}\n', encoding="utf-8"
    )
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    hook.chmod(0o644)  # install makes it executable: a mode change to show
    (home / ".codex" / "config.toml").write_text(
        '[mcp_servers.other]\ncommand = "other"\n', encoding="utf-8"
    )
    (home / ".hermes" / "config.yaml").write_text(
        "# Hermes\nmodel:\n  default: x\n", encoding="utf-8"
    )


@pytest.mark.parametrize("lived_in", [False, True], ids=["fresh", "lived-in"])
@pytest.mark.parametrize("key", PLATFORM_KEYS)
def test_install_writes_exactly_what_the_dry_run_showed(sandbox, key, lived_in, plans):
    if lived_in:
        _plant_user_files(sandbox)
    before = _snapshot(sandbox)
    cli._handle_init(_args(sandbox, key, dry_run=True))
    planned = {str(change.path): change for change in plans[0].changes()}

    cli._handle_init(_args(sandbox, key))

    after = _snapshot(sandbox)
    changed = {
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path) and after.get(path) != "dir"
    }
    assert changed == set(planned)
    for path, change in planned.items():
        assert Path(path).read_bytes() == change.after
        if change.mode is not None:
            assert stat.S_IMODE(os.stat(path).st_mode) == change.mode


@pytest.mark.parametrize("key", ["claude", "all"])
def test_nothing_is_written_before_the_question_is_answered(
    sandbox, key, monkeypatch, capsys
):
    before = _snapshot(sandbox)
    asked: list[tuple[bool, str]] = []

    def answer(prompt: str, default_yes: bool = True, **_: object) -> bool:
        asked.append((_snapshot(sandbox) == before, capsys.readouterr().out))
        return False

    monkeypatch.setattr(cli, "_confirm_yes_no", answer)
    cli._handle_init(_args(sandbox, key, yes=False))

    assert len(asked) == 1
    untouched, shown = asked[0]
    assert untouched, "a file was written before the question was answered"
    # The question comes after the list of files install will change.
    assert ".mcp.json" in shown
    assert "CLAUDE.md" in shown
    # Saying no skips the instructions only.
    assert (sandbox.repo / ".mcp.json").is_file()
    assert not (sandbox.repo / "CLAUDE.md").exists()


def test_ctrl_c_at_the_question_writes_nothing(sandbox, monkeypatch, capsys):
    class _Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    def interrupt(prompt: str = "") -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(sys, "stdin", _Terminal())
    monkeypatch.setattr("builtins.input", interrupt)
    before = _snapshot(sandbox)

    with pytest.raises(SystemExit) as exc_info:
        cli._handle_init(_args(sandbox, "claude", yes=False))

    assert exc_info.value.code == 130
    assert _snapshot(sandbox) == before
    assert "No files were written" in capsys.readouterr().out


def test_a_step_that_fails_while_planning_writes_nothing(sandbox, monkeypatch, capsys):
    def broken(*args, **kwargs):
        raise RuntimeError("hook writer broke")

    # The MCP config and skills are planned before the hooks step fails.
    monkeypatch.setattr(skills, "install_hooks", broken)
    before = _snapshot(sandbox)

    with pytest.raises(RuntimeError):
        cli._handle_init(_args(sandbox, "claude"))

    assert _snapshot(sandbox) == before
    assert "Install stopped before writing any file" in capsys.readouterr().err


def test_dry_run_diffs_an_existing_claude_md(sandbox, capsys):
    (sandbox.repo / "CLAUDE.md").write_text(
        "# Team notes\n\nKeep it short.\n", encoding="utf-8"
    )

    cli._handle_init(_args(sandbox, "claude", dry_run=True))

    out = capsys.readouterr().out
    assert "--- a/CLAUDE.md" in out
    assert "+++ b/CLAUDE.md" in out
    assert "\n Keep it short.\n" in out
    assert "\n+<!-- code-review-graph MCP tools -->\n" in out
    assert (sandbox.repo / "CLAUDE.md").read_text(encoding="utf-8") == (
        "# Team notes\n\nKeep it short.\n"
    )


def test_dry_run_keeps_jsonc_comments(sandbox, plans, capsys):
    config = sandbox.repo / "opencode.jsonc"
    original = '{\n  // keep this note\n  "theme": "dark"\n}\n'
    config.write_text(original, encoding="utf-8")

    cli._handle_init(_args(sandbox, "opencode", dry_run=True))

    out = capsys.readouterr().out
    assert "  // keep this note" in out
    assert "-  // keep this note" not in out
    planned = {change.path: change for change in plans[0].changes()}[config]
    assert b"// keep this note" in planned.after
    assert config.read_text(encoding="utf-8") == original

    cli._handle_init(_args(sandbox, "opencode"))

    assert config.read_bytes() == planned.after


def test_long_new_file_is_cut_with_a_note(sandbox, capsys):
    cli._handle_init(_args(sandbox, "opencode", dry_run=True))

    out = capsys.readouterr().out
    # The OpenCode plugin is far longer than the per-file limit.
    assert "+++ ~/.config/opencode/plugins/crg-plugin.ts" in out
    assert "more lines not shown" in out


def test_render_caps_each_file_and_says_how_much_was_cut(tmp_path):
    change = install_plan.FileChange(
        path=tmp_path / "big.txt",
        before=None,
        after=b"".join(b"line %d\n" % n for n in range(100)),
        before_mode=None,
        mode=None,
    )

    text = install_plan.render([change], tmp_path, limit=10)

    assert "--- /dev/null" in text
    assert "+++ b/big.txt" in text
    assert "+line 0\n" in text
    assert "+line 20\n" not in text
    assert "more lines not shown" in text


_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="Windows keeps no mode bits")


def test_render_shows_a_file_only_a_few_lines_over_the_limit_whole(tmp_path):
    change = install_plan.FileChange(
        path=tmp_path / "short.txt",
        before=None,
        after=b"".join(b"line %d\n" % n for n in range(13)),
        before_mode=None,
        mode=None,
    )

    text = install_plan.render([change], tmp_path, limit=10)

    assert "+line 12\n" in text
    assert "not shown" not in text


def test_render_says_when_a_new_file_is_empty(tmp_path):
    change = install_plan.FileChange(
        path=tmp_path / "empty.txt", before=None, after=b"", before_mode=None, mode=None
    )

    assert "new empty file: empty.txt" in install_plan.render([change], tmp_path)


@_POSIX_ONLY
def test_render_reports_a_mode_change(tmp_path):
    hook = tmp_path / "post-merge"
    change = install_plan.FileChange(
        path=hook, before=b"#!/bin/sh\n", after=b"#!/bin/sh\n",
        before_mode=0o644, mode=0o755,
    )

    text = install_plan.render([change], tmp_path)

    assert "post-merge" in text
    assert "644" in text and "755" in text


def test_plan_reads_its_own_writes_and_touches_no_disk(tmp_path):
    settings = tmp_path / ".gemini" / "settings.json"
    with install_plan.planning() as plan:
        install_plan.write_text(settings, '{"a": 1}\n')
        assert install_plan.exists(settings)
        assert install_plan.read_text(settings) == '{"a": 1}\n'
        install_plan.write_text(settings, '{"a": 2}\n')
        install_plan.chmod(settings, 0o600)
    assert not (tmp_path / ".gemini").exists()
    [change] = plan.changes()
    assert change.after == '{"a": 2}\n'.encode().replace(b"\n", os.linesep.encode())
    assert change.mode == 0o600

    assert plan.apply() == []

    assert settings.read_text(encoding="utf-8") == '{"a": 2}\n'


def test_apply_leaves_a_file_that_changed_after_the_preview(tmp_path):
    notes = tmp_path / "CLAUDE.md"
    notes.write_text("mine\n", encoding="utf-8")
    with install_plan.planning() as plan:
        install_plan.write_text(notes, "mine\nours\n")
    notes.write_text("mine, edited since\n", encoding="utf-8")

    failures = plan.apply()

    assert [path for path, _ in failures] == [notes]
    assert notes.read_text(encoding="utf-8") == "mine, edited since\n"


def test_plan_refuses_a_path_under_a_file(tmp_path):
    (tmp_path / "skills").write_text("occupied\n", encoding="utf-8")
    with install_plan.planning(), pytest.raises(NotADirectoryError):
        install_plan.write_text(tmp_path / "skills" / "x" / "SKILL.md", "body\n")


@_POSIX_ONLY
def test_helpers_write_straight_to_disk_without_a_plan(tmp_path):
    target = tmp_path / "a" / "b.txt"
    install_plan.write_text(target, "hello\n")
    assert target.read_text(encoding="utf-8") == "hello\n"
    install_plan.chmod(target, 0o700)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    install_plan.unlink(target)
    assert not target.exists()
