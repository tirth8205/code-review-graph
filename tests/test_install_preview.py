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
from code_review_graph.errors import CodeReviewGraphError

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
    dsh_home = home / ".dsh"
    for directory in (home, repo, hermes_home, dsh_home):
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
    monkeypatch.setenv("DSH_HOME", str(dsh_home))
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
    # Two writers touch this one file: the MCP entry, then the hooks. Both
    # keep its comment (#1157).
    (repo / ".gemini" / "settings.json").write_text(
        '{\n  // dark, please\n  "theme": "dark"\n}\n', encoding="utf-8"
    )
    (repo / ".claude").mkdir()
    # Commented hook settings are spliced, not rewritten (#1157), and the
    # steering hook is merged into the same file after the other hooks.
    (repo / ".claude" / "settings.json").write_text(
        '{\n  // team rule\n  "permissions": {"allow": ["Bash"]}\n}\n', encoding="utf-8"
    )
    (home / ".cursor" / "hooks.json").write_text(
        '{\n  // mine\n  "version": 1,\n  "hooks": {}\n}\n', encoding="utf-8"
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
    (home / ".dsh" / "cordis.patch.yml").write_text(
        "# DSH\n- id: model\n  config:\n    url: !!js process.env.MODEL_URL\n",
        encoding="utf-8",
    )


# The opt-in steps write files too: the steering hook (#1155) and the
# .gitignore block (#471). Each must be planned with the rest.
_OPT_INS = {"plain": {}, "opt-ins": {"steering": True, "gitignore": True}}


@pytest.mark.parametrize("opt_ins", sorted(_OPT_INS))
@pytest.mark.parametrize("lived_in", [False, True], ids=["fresh", "lived-in"])
@pytest.mark.parametrize("key", PLATFORM_KEYS)
def test_install_writes_exactly_what_the_dry_run_showed(
    sandbox, key, lived_in, opt_ins, plans
):
    if lived_in:
        _plant_user_files(sandbox)
    extra = _OPT_INS[opt_ins]
    before = _snapshot(sandbox)
    cli._handle_init(_args(sandbox, key, dry_run=True, **extra))
    assert _snapshot(sandbox) == before
    planned = {str(change.path): change for change in plans[0].changes()}

    cli._handle_init(_args(sandbox, key, **extra))

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


@pytest.mark.parametrize("opt_ins", sorted(_OPT_INS))
@pytest.mark.parametrize("key", ["claude", "all"])
def test_nothing_is_written_before_the_question_is_answered(
    sandbox, key, opt_ins, monkeypatch, capsys
):
    _plant_user_files(sandbox)
    before = _snapshot(sandbox)
    asked: list[tuple[bool, str]] = []

    def answer(prompt: str, default_yes: bool = True, **_: object) -> bool:
        asked.append((_snapshot(sandbox) == before, capsys.readouterr().out))
        return False

    monkeypatch.setattr(cli, "_confirm_yes_no", answer)
    (sandbox.repo / "CLAUDE.md").unlink()
    before = _snapshot(sandbox)
    cli._handle_init(_args(sandbox, key, yes=False, **_OPT_INS[opt_ins]))

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


_LINKS = pytest.mark.skipif(os.name == "nt", reason="symlinks need extra rights on Windows")


def test_instruction_file_deleted_at_the_question_stays_deleted(sandbox, monkeypatch, capsys):
    # Instruction files are planned before the question, so an edit made while
    # it is open is left alone like any other file (Copilot on #1173).
    notes = sandbox.repo / "CLAUDE.md"
    notes.write_text("# Team notes\n", encoding="utf-8")

    def answer(prompt: str, default_yes: bool = True, **_: object) -> bool:
        notes.unlink()
        return True

    monkeypatch.setattr(cli, "_confirm_yes_no", answer)
    with pytest.raises(CodeReviewGraphError):
        cli._handle_init(_args(sandbox, "claude", yes=False))

    assert not notes.exists()
    assert "CLAUDE.md" in capsys.readouterr().err


@_POSIX_ONLY
def test_apply_leaves_a_file_whose_mode_changed_after_the_preview(tmp_path):
    hook = tmp_path / "pre-commit"
    hook.write_text("#!/bin/sh\n", encoding="utf-8")
    hook.chmod(0o755)
    with install_plan.planning() as plan:
        install_plan.write_text(hook, "#!/bin/sh\necho graph\n")
        install_plan.chmod(hook, 0o755)
    hook.chmod(0o644)  # the user turns the hook off while the question is open

    failures = plan.apply()

    assert [path for path, _ in failures] == [hook]
    assert stat.S_IMODE(hook.stat().st_mode) == 0o644
    assert hook.read_text(encoding="utf-8") == "#!/bin/sh\n"


def test_install_removes_the_old_copilot_file_the_dry_run_showed(sandbox, plans):
    cli._handle_init(_args(sandbox, "copilot"))
    legacy = sandbox.repo / ".github" / "code-review-graph.instruction.md"
    legacy.write_text(skills._COPILOT_SECTION, encoding="utf-8")

    cli._handle_init(_args(sandbox, "copilot", dry_run=True))
    assert legacy in {change.path for change in plans[1].changes()}

    cli._handle_init(_args(sandbox, "copilot"))

    assert not legacy.exists()


def test_no_success_message_for_a_file_that_was_left_alone(sandbox, monkeypatch, capsys):
    mcp = sandbox.repo / ".mcp.json"

    def answer(prompt: str, default_yes: bool = True, **_: object) -> bool:
        mcp.write_text('{"mine": true}\n', encoding="utf-8")
        return True

    monkeypatch.setattr(cli, "_confirm_yes_no", answer)
    with pytest.raises(CodeReviewGraphError):
        cli._handle_init(_args(sandbox, "claude", yes=False))

    captured = capsys.readouterr()
    assert "Configured" not in captured.out
    assert "Could not write" in captured.err and ".mcp.json" in captured.err
    assert mcp.read_text(encoding="utf-8") == '{"mine": true}\n'
    # What did reach the disk is still named.
    assert "CLAUDE.md" in captured.out.split("Inject graph")[-1]


def test_progress_messages_follow_a_clean_install(sandbox, capsys):
    cli._handle_init(_args(sandbox, "claude"))

    out = capsys.readouterr().out
    assert "Configured 1 platform(s): Claude Code" in out
    assert "Injected graph instructions into: CLAUDE.md" in out


@_LINKS
def test_claude_md_linked_to_agents_md_gets_one_block(sandbox, capsys):
    agents = sandbox.repo / "AGENTS.md"
    agents.write_text("# Agents\n", encoding="utf-8")
    (sandbox.repo / "CLAUDE.md").symlink_to("AGENTS.md")

    cli._handle_init(_args(sandbox, "all"))

    assert (sandbox.repo / "CLAUDE.md").is_symlink()
    assert agents.read_text(encoding="utf-8").count(skills._CLAUDE_MD_SECTION_MARKER) == 1
    assert "Could not write" not in capsys.readouterr().err


@_LINKS
def test_plan_sees_one_file_through_each_of_its_names(tmp_path):
    real = tmp_path / "AGENTS.md"
    real.write_text("# Agents\n", encoding="utf-8")
    link = tmp_path / "CLAUDE.md"
    link.symlink_to("AGENTS.md")
    with install_plan.planning() as plan:
        install_plan.write_text(link, "# Agents\nmore\n")
        assert install_plan.read_text(real) == "# Agents\nmore\n"
    assert len(plan.changes()) == 1

    assert plan.apply() == []

    assert link.is_symlink()
    assert real.read_text(encoding="utf-8") == "# Agents\nmore\n"


@_LINKS
def test_unlinking_a_symlink_removes_the_link_not_its_target(tmp_path):
    real = tmp_path / "shared.md"
    real.write_text("shared\n", encoding="utf-8")
    link = tmp_path / "legacy.md"
    link.symlink_to("shared.md")
    with install_plan.planning() as plan:
        install_plan.unlink(link)
        assert not install_plan.exists(link)
        assert install_plan.read_text(real) == "shared\n"

    assert plan.apply() == []

    assert not os.path.lexists(link)
    assert real.read_text(encoding="utf-8") == "shared\n"


# -- writers that came with the 2026-11-09 merge train ------------------------


def test_dry_run_shows_the_steering_hook_and_writes_nothing(sandbox, plans, capsys):
    before = _snapshot(sandbox)

    cli._handle_init(_args(sandbox, "claude", dry_run=True, steering=True))

    out = capsys.readouterr().out
    assert _snapshot(sandbox) == before
    assert "[dry-run] Would add the steering hook" in out
    settings = sandbox.repo / ".claude" / "settings.json"
    planned = {change.path: change for change in plans[0].changes()}[settings]
    assert b"PreToolUse" in planned.after and b"enrich" in planned.after

    cli._handle_init(_args(sandbox, "claude", steering=True))

    assert settings.read_bytes() == planned.after


def test_no_steering_removes_what_install_just_planned(sandbox, plans):
    """``--no-steering`` reads the hooks planned a moment before, not the disk."""
    cli._handle_init(_args(sandbox, "claude", steering=True))
    settings = sandbox.repo / ".claude" / "settings.json"
    with_steering = settings.read_bytes()
    before = _snapshot(sandbox)

    cli._handle_init(_args(sandbox, "claude", dry_run=True, steering=False))

    assert _snapshot(sandbox) == before
    planned = {change.path: change for change in plans[1].changes()}[settings]
    assert planned.after != with_steering

    cli._handle_init(_args(sandbox, "claude", steering=False))

    assert settings.read_bytes() == planned.after
    # The other hooks the same run planned are still there.
    assert b"hook-update" in planned.after


def test_dry_run_keeps_comments_in_hook_settings(sandbox, plans, capsys):
    settings = sandbox.repo / ".claude" / "settings.json"
    settings.parent.mkdir()
    original = '{\n  // team rule\n  "permissions": {"allow": ["Bash"]}\n}\n'
    settings.write_text(original, encoding="utf-8")

    cli._handle_init(_args(sandbox, "claude", dry_run=True))

    out = capsys.readouterr().out
    assert "   // team rule" in out
    assert "-  // team rule" not in out
    assert settings.read_text(encoding="utf-8") == original
    planned = {change.path: change for change in plans[0].changes()}
    assert b"// team rule" in planned[settings].after
    # The backup holds what the user wrote, also only in the plan.
    assert planned[settings.with_name("settings.json.bak")].after == original.encode()

    cli._handle_init(_args(sandbox, "claude"))

    assert settings.read_bytes() == planned[settings].after


def test_dry_run_says_which_hook_settings_it_would_leave_alone(sandbox, capsys):
    settings = sandbox.repo / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text("{ not json\n", encoding="utf-8")
    before = _snapshot(sandbox)

    cli._handle_init(_args(sandbox, "claude", dry_run=True))

    out = capsys.readouterr().out
    assert _snapshot(sandbox) == before
    assert "left unchanged to avoid data loss" in out
    assert "+++ b/.claude/settings.json" not in out


def _git_block(repo: Path) -> list[str]:
    from code_review_graph import install_gitignore

    lines = (repo / ".gitignore").read_text(encoding="utf-8").splitlines()
    inner = lines[lines.index(install_gitignore.BLOCK_START) + 1 :]
    inner = inner[: inner.index(install_gitignore.BLOCK_END)]
    return [line for line in inner if not line.startswith("#")]


def test_dry_run_shows_the_gitignore_block_it_would_write(sandbox, plans, capsys):
    before = _snapshot(sandbox)

    cli._handle_init(_args(sandbox, "claude", dry_run=True, gitignore=True))

    out = capsys.readouterr().out
    assert _snapshot(sandbox) == before
    assert "+# >>> code-review-graph install files >>>" in out
    assert "+/.mcp.json" in out
    gitignore = sandbox.repo / ".gitignore"
    planned = {change.path: change for change in plans[0].changes()}[gitignore]

    cli._handle_init(_args(sandbox, "claude", gitignore=True))

    assert gitignore.read_bytes() == planned.after
    assert _git_block(sandbox.repo) == ["/.claude/", "/.mcp.json", "/CLAUDE.md"]


def test_saying_no_keeps_instruction_files_out_of_the_gitignore_block(
    sandbox, monkeypatch
):
    monkeypatch.setattr(cli, "_confirm_yes_no", lambda *a, **k: False)

    cli._handle_init(_args(sandbox, "claude", yes=False, gitignore=True))

    assert not (sandbox.repo / "CLAUDE.md").exists()
    assert _git_block(sandbox.repo) == ["/.claude/", "/.mcp.json"]
