"""A shared Git hook must not implicitly build a graph for each worktree."""

import os
import subprocess

from code_review_graph.skills import install_git_hook

LEGACY_SCRIPT = """#!/bin/sh
# Installed by code-review-graph. Remove this file to disable pre-commit graph checks.
if command -v code-review-graph >/dev/null 2>&1; then
    code-review-graph update || true
    code-review-graph detect-changes --brief || true
fi
"""


def git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def hook_repo(tmp_path, monkeypatch):
    repo = tmp_path / "main repo"
    repo.mkdir()
    # Isolate identity and hooks from the developer's or test runner's Git config.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "3")
    for index, (key, value) in enumerate(
        (
            ("core.hooksPath", str(repo / ".git" / "hooks")),
            ("user.name", "CRG Hook Test"),
            ("user.email", "hook-tests@example.invalid"),
        )
    ):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{index}", key)
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{index}", value)
    git(repo, "init", "-b", "main")
    (repo / "initial.txt").write_text("initial\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    commands = tmp_path / "commands.log"
    monkeypatch.setenv("CRG_HOOK_LOG", str(commands))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "code-review-graph"
    binary.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CRG_HOOK_LOG"\n'
        "mkdir -p .code-review-graph\ntouch .code-review-graph/graph.db\n"
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    return repo, commands


def commit_change(repo, name="change.txt"):
    (repo / name).write_text("change\n")
    git(repo, "add", name)
    return git(repo, "commit", "-m", "change")


def test_shared_hook_skips_linked_worktree_graph_creation(tmp_path, monkeypatch):
    repo, commands = hook_repo(tmp_path, monkeypatch)
    install_git_hook(repo)
    linked = tmp_path / "linked tree"
    git(repo, "worktree", "add", "-b", "feature", str(linked))
    result = commit_change(linked)
    assert not commands.exists()
    assert not (linked / ".code-review-graph").exists()
    assert "linked worktree" in result.stderr


def test_main_tree_hook_still_runs_with_explicit_root(tmp_path, monkeypatch):
    repo, commands = hook_repo(tmp_path, monkeypatch)
    install_git_hook(repo)
    commit_change(repo)
    assert commands.read_text().splitlines() == [
        f"update --repo {repo}",
        f"detect-changes --brief --repo {repo}",
    ]


def test_upgrade_preserves_user_hook_commands_and_is_idempotent(tmp_path, monkeypatch):
    repo, commands = hook_repo(tmp_path, monkeypatch)
    hook = repo / ".git" / "hooks" / "pre-commit"
    marker = tmp_path / "user-hook.log"
    monkeypatch.setenv("USER_HOOK_LOG", str(marker))
    hook.write_text(
        '#!/bin/sh\necho before >> "$USER_HOOK_LOG"\n'
        + LEGACY_SCRIPT
        + 'echo after >> "$USER_HOOK_LOG"\n'
    )
    hook.chmod(0o755)
    install_git_hook(repo)
    before = hook.read_bytes()
    install_git_hook(repo)
    assert hook.read_bytes() == before
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-b", "feature", str(linked))
    commit_change(linked)
    assert marker.read_text().splitlines() == ["before", "after"]
    assert not commands.exists()


def test_hook_needs_no_git_options_newer_than_2_13(tmp_path, monkeypatch):
    """Git before 2.31 has no --path-format; the hook must not depend on it.

    Git prepends its own exec path when running hooks, so a PATH shim cannot
    simulate an older git here; check the generated script directly instead.
    """
    repo, _commands = hook_repo(tmp_path, monkeypatch)
    hook = install_git_hook(repo)
    script = hook.read_text()
    assert "--path-format" not in script
    assert "--git-common-dir" not in script
    assert "commondir" in script


def test_hook_opt_in_runs_in_linked_worktree(tmp_path, monkeypatch):
    repo, commands = hook_repo(tmp_path, monkeypatch)
    install_git_hook(repo)
    linked = tmp_path / "linked tree"
    git(repo, "worktree", "add", "-b", "feature", str(linked))
    monkeypatch.setenv("CRG_HOOK_WORKTREES", "1")
    commit_change(linked)
    assert commands.read_text().splitlines() == [
        f"update --repo {linked}",
        f"detect-changes --brief --repo {linked}",
    ]
