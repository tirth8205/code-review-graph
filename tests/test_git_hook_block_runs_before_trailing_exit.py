"""A hook block added to a user's hook that ends in ``exit`` still runs.

The shell stops at a top-level ``exit``, so a block appended after it never
ran and the graph went stale. The block now goes just before that ``exit``.
When moving it would change what the hook exits with (a bare ``exit`` or
``exit $?``), the hook is left alone with a warning. Issue crg-28j.
"""

from __future__ import annotations

import logging

import pytest

from code_review_graph import skills, uninstall
from tests.test_git_sync_hooks import calls, commit_file, git, sync_repo


def _block_before_exit(text: str, block: str, exit_line: str) -> None:
    assert text.count(block) == 1
    assert text.index(block) < text.index(exit_line)
    assert text.rstrip("\n").endswith(exit_line)


def test_pre_commit_block_goes_before_a_trailing_exit(tmp_path, monkeypatch):
    _upstream, clone, log = sync_repo(tmp_path, monkeypatch)
    hook = clone / ".git" / "hooks" / "pre-commit"
    user = "#!/bin/sh\necho checked\nexit 0\n"
    hook.write_text(user)

    skills.install_git_hook(clone)

    text = hook.read_text()
    _block_before_exit(text, skills._git_hook_block(), "exit 0")
    commit_file(clone, "local.txt")
    assert any(line.startswith("update --quiet") for line in calls(log))

    report = uninstall.UninstallReport()
    uninstall._remove_git_hook(clone, report, dry_run=False)
    assert hook.read_text() == user


def test_sync_block_goes_before_a_trailing_exit(tmp_path, monkeypatch):
    upstream, clone, log = sync_repo(tmp_path, monkeypatch)
    hook = clone / ".git" / "hooks" / "post-merge"
    user = "#!/bin/sh\necho user\n# all done\nexit 0 # keep going\n\n"
    hook.write_text(user)

    skills.install_git_sync_hooks(clone)

    _block_before_exit(
        hook.read_text(), skills._git_sync_hook_block("post-merge"), "exit 0 # keep going"
    )
    commit_file(upstream, "probe.txt")
    git(clone, "pull")
    assert calls(log) == [f"update --quiet --repo {clone} probe=present"]


def test_reinstall_moves_a_block_stranded_after_exit(tmp_path, monkeypatch):
    _upstream, clone, _log = sync_repo(tmp_path, monkeypatch)
    hook = clone / ".git" / "hooks" / "post-merge"
    block = skills._git_sync_hook_block("post-merge")
    hook.write_text("#!/bin/sh\necho user\nexit 0\n" + block)

    skills.install_git_sync_hooks(clone)

    assert hook.read_text() == "#!/bin/sh\necho user\n" + block + "exit 0\n"


def test_reinstall_moves_a_pre_commit_block_stranded_after_exit(tmp_path, monkeypatch):
    _upstream, clone, _log = sync_repo(tmp_path, monkeypatch)
    hook = clone / ".git" / "hooks" / "pre-commit"
    block = skills._git_hook_block()
    hook.write_text("#!/bin/sh\necho user\nexit 0\n" + block)

    skills.install_git_hook(clone)

    assert hook.read_text() == "#!/bin/sh\necho user\n" + block + "exit 0\n"


@pytest.mark.parametrize("exit_line", ["exit", "exit $?", 'exit "$?"'])
def test_exit_that_passes_on_the_last_status_is_left_alone(
    tmp_path, monkeypatch, caplog, exit_line
):
    """Code put before ``exit`` here would change what the hook exits with."""
    _upstream, clone, _log = sync_repo(tmp_path, monkeypatch)
    hooks = clone / ".git" / "hooks"
    user = f"#!/bin/sh\nrun_checks\n{exit_line}\n"
    (hooks / "pre-commit").write_text(user)
    (hooks / "post-merge").write_text(user)

    with caplog.at_level(logging.WARNING, logger="code_review_graph.skills"):
        skills.install_git_hook(clone)
        written = skills.install_git_sync_hooks(clone)

    assert (hooks / "pre-commit").read_text() == user
    assert (hooks / "post-merge").read_text() == user
    assert hooks / "post-merge" not in written
    assert sum("exit" in record.getMessage() for record in caplog.records) == 2


@pytest.mark.parametrize(
    "tail",
    [
        "run_checks ||\n    exit 1\n",
        "run_checks || \\\nexit 1\n",
        "if run_checks; then\n    exit 0\nfi\n",
    ],
    ids=["or-newline", "backslash", "inside-if"],
)
def test_exit_that_is_not_top_level_keeps_the_block_at_the_end(
    tmp_path, monkeypatch, tail
):
    _upstream, clone, _log = sync_repo(tmp_path, monkeypatch)
    hook = clone / ".git" / "hooks" / "post-merge"
    hook.write_text("#!/bin/sh\n" + tail)

    skills.install_git_sync_hooks(clone)

    assert hook.read_text() == "#!/bin/sh\n" + tail + skills._git_sync_hook_block("post-merge")
