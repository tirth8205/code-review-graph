"""Git hooks keep the graph in sync after checkout, merge and rebase.

Every test drives real git commands against a throwaway clone, with a stub
``code-review-graph`` on PATH that logs each call and whether the file named by
``CRG_PROBE_FILE`` existed at that moment, so a test can tell an update that
saw the final working tree from one that ran too early.
"""

import os
import subprocess

from code_review_graph import skills, uninstall
from code_review_graph.skills import install_git_sync_hooks


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
        timeout=30,
    )


def set_git_config(monkeypatch, hooks_path):
    # Isolate identity and hooks from the developer's or test runner's Git config.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "4")
    for index, (key, value) in enumerate(
        (
            ("core.hooksPath", str(hooks_path)),
            ("user.name", "CRG Hook Test"),
            ("user.email", "hook-tests@example.invalid"),
            ("pull.rebase", "false"),
        )
    ):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{index}", key)
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{index}", value)


def commit_file(repo, name, content="change\n"):
    (repo / name).write_text(content)
    git(repo, "add", name)
    git(repo, "commit", "-m", f"add {name}")


def sync_repo(tmp_path, monkeypatch):
    """Return ``(upstream, clone, log)`` with sync hooks installed in the clone."""
    set_git_config(monkeypatch, tmp_path / "no-hooks")
    upstream = tmp_path / "upstream repo"
    upstream.mkdir()
    git(upstream, "init", "-b", "main")
    commit_file(upstream, "initial.txt", "initial\n")
    clone = tmp_path / "clone repo"
    git(tmp_path, "clone", str(upstream), str(clone))
    set_git_config(monkeypatch, clone / ".git" / "hooks")

    log = tmp_path / "commands.log"
    monkeypatch.setenv("CRG_HOOK_LOG", str(log))
    monkeypatch.setenv("CRG_PROBE_FILE", "probe.txt")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "code-review-graph"
    binary.write_text(
        "#!/bin/sh\n"
        'for last; do :; done\n'
        'if [ -f "$last/$CRG_PROBE_FILE" ]; then probe=present; else probe=missing; fi\n'
        'printf "%s probe=%s\\n" "$*" "$probe" >> "$CRG_HOOK_LOG"\n'
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])

    install_git_sync_hooks(clone)
    return upstream, clone, log


def calls(log):
    return log.read_text().splitlines() if log.exists() else []


def test_branch_checkout_updates_but_file_checkout_does_not(tmp_path, monkeypatch):
    _upstream, clone, log = sync_repo(tmp_path, monkeypatch)
    git(clone, "checkout", "-b", "feature")
    assert calls(log) == [f"update --quiet --repo {clone} probe=missing"]

    (clone / "initial.txt").write_text("edited\n")
    git(clone, "checkout", "--", "initial.txt")
    assert len(calls(log)) == 1


def test_fast_forward_pull_updates_once(tmp_path, monkeypatch):
    upstream, clone, log = sync_repo(tmp_path, monkeypatch)
    commit_file(upstream, "probe.txt")
    git(clone, "pull")
    assert calls(log) == [f"update --quiet --repo {clone} probe=present"]


def test_merge_pull_updates_once(tmp_path, monkeypatch):
    upstream, clone, log = sync_repo(tmp_path, monkeypatch)
    commit_file(upstream, "probe.txt")
    commit_file(clone, "local.txt")
    git(clone, "pull", "--no-edit")
    assert calls(log) == [f"update --quiet --repo {clone} probe=present"]


def test_rebase_pull_updates_once_after_local_commits_are_replayed(tmp_path, monkeypatch):
    """A rebase checks out the new base before replaying local commits.

    Updating at that checkout would drop the user's own commits from the graph,
    so the only update must come from post-rewrite, once the rebase finished.
    """
    upstream, clone, log = sync_repo(tmp_path, monkeypatch)
    commit_file(upstream, "upstream.txt")
    commit_file(clone, "probe.txt")
    git(clone, "pull", "--rebase")
    assert calls(log) == [f"update --quiet --repo {clone} probe=present"]


def test_amend_does_not_update(tmp_path, monkeypatch):
    _upstream, clone, log = sync_repo(tmp_path, monkeypatch)
    commit_file(clone, "local.txt")
    git(clone, "commit", "--amend", "-m", "reworded")
    assert calls(log) == []


def test_conflicted_merge_does_not_update(tmp_path, monkeypatch):
    """post-merge never runs for a merge that stops on a conflict.

    The commit that resolves it goes through pre-commit, which updates the
    graph itself.
    """
    upstream, clone, log = sync_repo(tmp_path, monkeypatch)
    commit_file(upstream, "initial.txt", "upstream\n")
    commit_file(clone, "initial.txt", "local\n")
    result = git(clone, "pull", "--no-edit", check=False)
    assert result.returncode != 0
    assert calls(log) == []


def test_linked_worktree_is_skipped_unless_opted_in(tmp_path, monkeypatch):
    _upstream, clone, log = sync_repo(tmp_path, monkeypatch)
    linked = tmp_path / "linked tree"
    result = git(clone, "worktree", "add", "-b", "feature", str(linked))
    assert calls(log) == []
    assert "linked worktree" in result.stderr

    monkeypatch.setenv("CRG_HOOK_WORKTREES", "1")
    git(linked, "checkout", "-b", "other")
    assert calls(log) == [f"update --quiet --repo {linked} probe=missing"]


def test_install_appends_to_user_hook_and_is_idempotent(tmp_path, monkeypatch):
    upstream, clone, log = sync_repo(tmp_path, monkeypatch)
    hook = clone / ".git" / "hooks" / "post-merge"
    marker = tmp_path / "user-hook.log"
    monkeypatch.setenv("USER_HOOK_LOG", str(marker))
    hook.write_text('#!/bin/sh\necho user >> "$USER_HOOK_LOG"\n')
    install_git_sync_hooks(clone)
    before = hook.read_bytes()
    install_git_sync_hooks(clone)
    assert hook.read_bytes() == before
    assert before.decode().count(skills._GIT_SYNC_HOOK_BEGIN_MARKER) == 1

    commit_file(upstream, "probe.txt")
    git(clone, "pull")
    assert marker.read_text().splitlines() == ["user"]
    assert calls(log) == [f"update --quiet --repo {clone} probe=present"]


def test_reinstall_replaces_an_older_block_in_place(tmp_path, monkeypatch):
    _upstream, clone, _log = sync_repo(tmp_path, monkeypatch)
    hook = clone / ".git" / "hooks" / "post-merge"
    older = (
        f"{skills._GIT_SYNC_HOOK_BEGIN_MARKER}\necho old\n{skills._GIT_SYNC_HOOK_END_MARKER}\n"
    )
    hook.write_text(f"#!/bin/sh\necho before\n{older}echo after\n")
    install_git_sync_hooks(clone)
    text = hook.read_text()
    assert "echo old" not in text
    assert text == (
        "#!/bin/sh\necho before\n" + skills._git_sync_hook_block("post-merge") + "echo after\n"
    )


def test_hand_edited_block_with_missing_end_marker_is_left_alone(tmp_path, monkeypatch):
    _upstream, clone, _log = sync_repo(tmp_path, monkeypatch)
    hook = clone / ".git" / "hooks" / "post-merge"
    broken = f"#!/bin/sh\n{skills._GIT_SYNC_HOOK_BEGIN_MARKER}\necho mine\n"
    hook.write_text(broken)
    install_git_sync_hooks(clone)
    assert hook.read_text() == broken


def test_uninstall_keeps_user_commands_and_removes_hooks_that_were_only_ours(
    tmp_path, monkeypatch
):
    _upstream, clone, _log = sync_repo(tmp_path, monkeypatch)
    hooks = clone / ".git" / "hooks"
    user_hook = hooks / "post-merge"
    user_hook.write_text(
        '#!/bin/sh\necho user\n' + skills._git_sync_hook_block("post-merge")
    )
    report = uninstall.UninstallReport()
    uninstall._remove_git_sync_hooks(clone, report, dry_run=False)

    assert user_hook.read_text() == "#!/bin/sh\necho user\n"
    assert not (hooks / "post-checkout").exists()
    assert not (hooks / "post-rewrite").exists()


def test_uninstall_leaves_unbalanced_block_unchanged(tmp_path, monkeypatch):
    _upstream, clone, _log = sync_repo(tmp_path, monkeypatch)
    hook = clone / ".git" / "hooks" / "post-checkout"
    broken = f"#!/bin/sh\n{skills._GIT_SYNC_HOOK_BEGIN_MARKER}\necho mine\n"
    hook.write_text(broken)
    report = uninstall.UninstallReport()
    uninstall._remove_git_sync_hooks(clone, report, dry_run=False)
    assert hook.read_text() == broken
    assert any("post-checkout" in path for path in report.skipped_paths)
