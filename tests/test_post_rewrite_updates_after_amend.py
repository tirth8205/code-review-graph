"""``commit --amend`` updates the graph through the post-rewrite hook.

``commit --amend --no-verify`` skips pre-commit, and post-rewrite only ran
for a rebase, so the graph went stale. post-rewrite now updates for both.
Issue crg-wwz.
"""

from __future__ import annotations

from code_review_graph import skills, uninstall
from tests.test_git_sync_hooks import calls, commit_file, git, sync_repo

# The post-rewrite condition v2.4.0 shipped, before amend counted.
_V2_4_0_CONDITION = '[ "$1" = "rebase" ] && '


def test_amend_without_pre_commit_updates_once(tmp_path, monkeypatch):
    _upstream, clone, log = sync_repo(tmp_path, monkeypatch)
    commit_file(clone, "local.txt")
    (clone / "probe.txt").write_text("late\n")
    git(clone, "add", "probe.txt")
    git(clone, "commit", "--amend", "--no-verify", "-m", "reworded")
    assert calls(log) == [f"update --quiet --repo {clone} probe=present"]


def test_rebase_still_updates_once(tmp_path, monkeypatch):
    upstream, clone, log = sync_repo(tmp_path, monkeypatch)
    commit_file(upstream, "upstream.txt")
    commit_file(clone, "probe.txt")
    git(clone, "pull", "--rebase")
    assert calls(log) == [f"update --quiet --repo {clone} probe=present"]


def _v2_4_0_block(monkeypatch) -> str:
    with monkeypatch.context() as patch:
        patch.setitem(skills._GIT_SYNC_HOOK_CONDITIONS, "post-rewrite", _V2_4_0_CONDITION)
        return skills._git_sync_hook_block("post-rewrite")


def test_reinstall_upgrades_the_rebase_only_block(tmp_path, monkeypatch):
    _upstream, clone, log = sync_repo(tmp_path, monkeypatch)
    hook = clone / ".git" / "hooks" / "post-rewrite"
    old = _v2_4_0_block(monkeypatch)
    assert _V2_4_0_CONDITION in old
    hook.write_text("#!/bin/sh\necho user\n" + old)

    skills.install_git_sync_hooks(clone)

    assert hook.read_text() == (
        "#!/bin/sh\necho user\n" + skills._git_sync_hook_block("post-rewrite")
    )
    commit_file(clone, "probe.txt")
    git(clone, "commit", "--amend", "--no-verify", "-m", "reworded")
    assert calls(log) == [f"update --quiet --repo {clone} probe=present"]


def test_uninstall_removes_the_rebase_only_block(tmp_path, monkeypatch):
    _upstream, clone, _log = sync_repo(tmp_path, monkeypatch)
    hook = clone / ".git" / "hooks" / "post-rewrite"
    hook.write_text("#!/bin/sh\necho user\n" + _v2_4_0_block(monkeypatch))

    uninstall._remove_git_sync_hooks(clone, uninstall.UninstallReport(), dry_run=False)

    assert hook.read_text() == "#!/bin/sh\necho user\n"
