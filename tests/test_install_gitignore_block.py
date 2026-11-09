"""``install --gitignore``: an opt-in block for the files install created.

Issue #471 asked for the files ``install`` generates to be ignored by git. They
can be team files, so the behaviour is opt-in and off by default. With the flag
on, ``install`` adds one marked block to the repository's ``.gitignore`` that
lists only what that run created and git does not track. It never lists a file
that existed before the run, a tracked file or the ``.code-review-graph/``
entry, which keeps its own handling. ``uninstall`` removes the block and
nothing else.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from code_review_graph import install_gitignore, skills, uninstall
from code_review_graph.cli import _handle_init

START = install_gitignore.BLOCK_START
END = install_gitignore.BLOCK_END


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake home for the installers and for git, so no real config is read."""
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake)
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake / ".config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(fake / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HERMES_HOME", str(fake / ".hermes"))
    return fake


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path, home: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    return root


def _status(repo: Path) -> list[str]:
    return sorted(_git(repo, "status", "--porcelain", "--untracked-files=all").splitlines())


def _install(repo: Path, platform: str, *, gitignore: bool, dry_run: bool = False) -> None:
    _handle_init(
        argparse.Namespace(
            repo=str(repo),
            dry_run=dry_run,
            platform=platform,
            yes=True,
            no_instructions=False,
            no_skills=False,
            no_hooks=False,
            gitignore=gitignore,
        )
    )


def _block(repo: Path) -> list[str]:
    lines = (repo / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines.count(START) == 1 and lines.count(END) == 1
    inner = lines[lines.index(START) + 1 : lines.index(END)]
    return [line for line in inner if not line.startswith("#")]


def test_without_the_flag_install_leaves_its_files_visible(repo: Path) -> None:
    _install(repo, "claude", gitignore=False)

    gitignore = (repo / ".gitignore").read_text(encoding="utf-8")
    assert START not in gitignore
    assert gitignore == "# Added by code-review-graph\n.code-review-graph/\n"
    assert "?? .mcp.json" in _status(repo)


def test_the_flag_lists_exactly_what_a_claude_install_created(repo: Path) -> None:
    _install(repo, "claude", gitignore=True)

    assert _block(repo) == ["/.claude/", "/.mcp.json", "/CLAUDE.md"]
    assert _status(repo) == ["?? .gitignore"]
    # The existing handling of the data folder is untouched.
    assert "# Added by code-review-graph\n.code-review-graph/\n" in (
        repo / ".gitignore"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize("platform", ("all", *skills.PLATFORMS))
def test_every_platform_leaves_only_gitignore_visible_and_dry_run_agrees(
    repo: Path, platform: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing install writes in the repository may be missed by the block.

    If a new installer starts writing a new file in the repository, this test
    fails until the block knows about it.
    """
    _install(repo, platform, gitignore=True, dry_run=True)
    preview = capsys.readouterr().out
    assert not (repo / ".gitignore").exists()

    _install(repo, platform, gitignore=True)

    assert _status(repo) == ["?? .gitignore"]
    written = _block(repo) if START in (repo / ".gitignore").read_text("utf-8") else []
    previewed = [
        line.strip()
        for line in preview.splitlines()
        if line.startswith("    /")
    ]
    assert previewed == written


def test_tracked_and_pre_existing_files_are_never_listed(repo: Path) -> None:
    (repo / "CLAUDE.md").write_text("# Team rules\n", encoding="utf-8")
    _git(repo, "add", "CLAUDE.md")
    _git(repo, "commit", "-qm", "team rules")
    notes = repo / ".claude" / "notes.md"
    notes.parent.mkdir()
    notes.write_text("mine\n", encoding="utf-8")

    _install(repo, "claude", gitignore=True)

    assert _block(repo) == ["/.claude/settings.json", "/.claude/skills/", "/.mcp.json"]
    status = _status(repo)
    assert " M CLAUDE.md" in status
    assert "?? .claude/notes.md" in status


def test_a_path_the_user_already_ignores_is_not_repeated(repo: Path) -> None:
    (repo / ".gitignore").write_text(".claude/\n", encoding="utf-8")

    _install(repo, "claude", gitignore=True)

    assert _block(repo) == ["/.mcp.json", "/CLAUDE.md"]


def test_rerun_keeps_one_block_and_merges_new_platforms(repo: Path) -> None:
    _install(repo, "claude", gitignore=True)
    first = (repo / ".gitignore").read_bytes()

    _install(repo, "claude", gitignore=True)
    assert (repo / ".gitignore").read_bytes() == first

    _install(repo, "gemini-cli", gitignore=True)
    assert _block(repo) == ["/.claude/", "/.gemini/", "/.mcp.json", "/CLAUDE.md", "/GEMINI.md"]
    assert _status(repo) == ["?? .gitignore"]


def test_rerun_without_the_flag_keeps_the_block(repo: Path) -> None:
    _install(repo, "claude", gitignore=True)
    first = (repo / ".gitignore").read_bytes()

    _install(repo, "claude", gitignore=False)

    assert (repo / ".gitignore").read_bytes() == first


def test_uninstall_restores_the_users_gitignore_byte_for_byte(repo: Path) -> None:
    original = b"node_modules/\n*.log\n\n# build output\ndist/\n"
    (repo / ".gitignore").write_bytes(original)

    _install(repo, "claude", gitignore=True)
    _install(repo, "gemini-cli", gitignore=True)
    assert (repo / ".gitignore").read_bytes() != original

    report = uninstall.run(repo=repo, keep_data=True)

    assert report.errors == []
    assert (repo / ".gitignore").read_bytes() == original


def test_uninstall_removes_a_gitignore_install_created(repo: Path) -> None:
    _install(repo, "claude", gitignore=True)

    uninstall.run(repo=repo, keep_data=True)

    assert not (repo / ".gitignore").exists()


def test_uninstall_keeps_user_lines_on_both_sides_of_the_block() -> None:
    text = (
        "dist/\n"
        f"{START}\n{install_gitignore.BLOCK_NOTE}\n/.mcp.json\n{END}\n"
        "coverage/\n"
    )

    assert install_gitignore.remove_block(text) == ("dist/\ncoverage/\n", "removed")


@pytest.mark.parametrize(
    "inner",
    [
        "*.log",  # a glob the installer never writes
        "!/.mcp.json",  # a negation
        "/.mcp.json ",  # trailing space
        "# my own note",
    ],
)
def test_a_hand_edited_block_is_left_alone(repo: Path, inner: str) -> None:
    text = f"dist/\n{START}\n/.mcp.json\n{inner}\n{END}\n"
    (repo / ".gitignore").write_text(text, encoding="utf-8")

    report = uninstall.run(repo=repo, keep_data=True)

    assert (repo / ".gitignore").read_text(encoding="utf-8") == text
    assert any("edited by hand" in item for item in report.skipped_paths)

    _install(repo, "claude", gitignore=True)
    assert f"{START}\n/.mcp.json\n{inner}\n{END}\n" in (repo / ".gitignore").read_text(
        encoding="utf-8"
    )


def test_a_block_without_its_end_marker_is_left_alone() -> None:
    text = f"dist/\n{START}\n/.mcp.json\n"

    assert install_gitignore.remove_block(text) == (text, "edited")


def test_dry_run_writes_nothing(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / ".gitignore").write_text("dist/\n", encoding="utf-8")

    _install(repo, "claude", gitignore=True, dry_run=True)

    out = capsys.readouterr().out
    assert "[dry-run] Would add to .gitignore" in out
    assert "    /.mcp.json" in out
    assert (repo / ".gitignore").read_text(encoding="utf-8") == "dist/\n"
    assert not (repo / ".mcp.json").exists()


def test_outside_a_git_work_tree_the_flag_does_nothing(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "plain"
    (folder / ".code-review-graph").mkdir(parents=True)

    _install(folder, "claude", gitignore=True)

    assert START not in (folder / ".gitignore").read_text(encoding="utf-8")
    assert "not a Git work tree" in capsys.readouterr().out


def test_crlf_gitignore_keeps_its_line_endings(repo: Path) -> None:
    original = b"dist/\r\n# Added by code-review-graph\r\n.code-review-graph/\r\n"
    (repo / ".gitignore").write_bytes(original)

    _install(repo, "claude", gitignore=True)

    data = (repo / ".gitignore").read_bytes()
    assert data.startswith(original)
    assert b"\n" not in data.replace(b"\r\n", b"")
    assert _status(repo) == ["?? .gitignore"]

    uninstall.run(repo=repo, keep_data=True)
    assert (repo / ".gitignore").read_bytes() == b"dist/\r\n"


def test_new_hooks_in_a_work_tree_hooks_folder_are_listed_one_by_one(repo: Path) -> None:
    """``core.hooksPath`` can point inside the repository, at a team folder."""
    hooks = repo / ".githooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\necho team\n", encoding="utf-8")
    _git(repo, "add", ".githooks/pre-commit")
    _git(repo, "commit", "-qm", "team hook")
    _git(repo, "config", "core.hooksPath", ".githooks")

    _install(repo, "claude", gitignore=True)

    assert _block(repo) == [
        "/.claude/",
        "/.githooks/post-checkout",
        "/.githooks/post-merge",
        "/.githooks/post-rewrite",
        "/.mcp.json",
        "/CLAUDE.md",
    ]
    assert _status(repo) == [" M .githooks/pre-commit", "?? .gitignore"]


def test_a_repository_subfolder_gets_its_own_anchored_block(
    tmp_path: Path, home: Path
) -> None:
    top = tmp_path / "mono"
    project = top / "services" / "api"
    project.mkdir(parents=True)
    _git(top, "init", "-q")

    _install(project, "claude", gitignore=True)

    assert _block(project) == ["/.claude/", "/.mcp.json", "/CLAUDE.md"]
    assert _status(top) == ["?? services/api/.gitignore"]


def test_a_line_the_user_removed_is_not_added_back(repo: Path) -> None:
    _install(repo, "claude", gitignore=True)
    gitignore = repo / ".gitignore"
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8").replace("/CLAUDE.md\n", ""), encoding="utf-8"
    )

    _install(repo, "claude", gitignore=True)

    assert _block(repo) == ["/.claude/", "/.mcp.json"]
    assert "?? CLAUDE.md" in _status(repo)


def test_an_entry_whose_path_is_gone_is_dropped(repo: Path) -> None:
    _install(repo, "claude", gitignore=True)
    (repo / "CLAUDE.md").unlink()

    _handle_init(
        argparse.Namespace(
            repo=str(repo), dry_run=False, platform="claude", yes=True,
            no_instructions=True, no_skills=False, no_hooks=False, gitignore=True,
        )
    )

    assert _block(repo) == ["/.claude/", "/.mcp.json"]


def test_cli_parser_accepts_the_flag_and_it_defaults_off(repo: Path) -> None:
    import sys
    from unittest.mock import patch

    from code_review_graph import cli

    seen: list[bool] = []
    with patch.object(cli, "_handle_init", lambda args: seen.append(args.gitignore)):
        for argv in (["install"], ["install", "--gitignore"], ["init", "--gitignore"]):
            with patch.object(sys, "argv", ["code-review-graph", *argv, "--repo", str(repo)]):
                cli.main()

    assert seen == [False, True, True]


def test_codex_files_in_a_codex_home_inside_the_repo_are_listed(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``CODEX_HOME`` can name a folder in the work tree that is already there."""
    codex = repo / ".codex"
    codex.mkdir()
    (codex / "notes.md").write_text("mine\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex))

    _install(repo, "codex", gitignore=True, dry_run=True)
    preview = [
        line.strip() for line in capsys.readouterr().out.splitlines() if line.startswith("    /")
    ]
    _install(repo, "codex", gitignore=True)

    assert _status(repo) == ["?? .codex/notes.md", "?? .gitignore"]
    assert preview == _block(repo)


def test_a_settings_backup_made_after_the_mcp_step_is_listed(repo: Path) -> None:
    """The MCP step writes ``.gemini/settings.json``; the hook step then backs it up."""
    (repo / ".gemini").mkdir()
    (repo / ".gemini" / "notes.md").write_text("mine\n", encoding="utf-8")

    _install(repo, "gemini-cli", gitignore=True)

    assert (repo / ".gemini" / "settings.json.bak").exists()
    assert _status(repo) == ["?? .gemini/notes.md", "?? .gitignore"]


def test_dry_run_keeps_the_entry_for_a_file_install_will_write_again(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(repo, "claude", gitignore=True)
    before = (repo / ".gitignore").read_bytes()
    (repo / ".mcp.json").unlink()
    capsys.readouterr()

    _install(repo, "claude", gitignore=True, dry_run=True)
    out = capsys.readouterr().out

    assert "Would drop" not in out
    assert "already lists every file install created" in out
    _install(repo, "claude", gitignore=True)
    assert (repo / ".gitignore").read_bytes() == before


@pytest.mark.parametrize(
    "original",
    [b".code-review-graph/", b"dist/\r\n.code-review-graph/"],
    ids=["lf", "crlf"],
)
def test_a_gitignore_without_a_final_newline_comes_back_byte_for_byte(
    repo: Path, original: bytes
) -> None:
    (repo / ".gitignore").write_bytes(original)

    _install(repo, "claude", gitignore=True)
    first = (repo / ".gitignore").read_bytes()
    assert _status(repo) == ["?? .gitignore"]
    _install(repo, "claude", gitignore=True)
    assert (repo / ".gitignore").read_bytes() == first
    _install(repo, "gemini-cli", gitignore=True)
    assert _status(repo) == ["?? .gitignore"]

    uninstall.run(repo=repo, keep_data=True)

    assert (repo / ".gitignore").read_bytes() == original
