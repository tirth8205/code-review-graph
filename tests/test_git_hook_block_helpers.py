"""Direct coverage for the helpers that find, resolve and rewrite marked hook blocks.

Install and uninstall reach these only through their callers, whose tests never
take the refusal and fallback branches. Added with PR #1125, which generalised
them to take the sync hooks' markers and hook names.
"""

import subprocess

import pytest

from code_review_graph import skills, uninstall

BEGIN = skills._GIT_HOOK_BEGIN_MARKER
END = skills._GIT_HOOK_END_MARKER
SYNC_BEGIN = skills._GIT_SYNC_HOOK_BEGIN_MARKER
SYNC_END = skills._GIT_SYNC_HOOK_END_MARKER


def git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    # Keep the developer's own core.hooksPath out of the resolution under test.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    return root


def git_unavailable(*_args, **_kwargs):
    raise OSError("git not found")


# --- _git_hook_markers_balanced ---------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "#!/bin/sh\necho user\n",
        f"#!/bin/sh\n{BEGIN}\nbody\n{END}\n",
        f"{BEGIN}\none\n{END}\necho between\n{BEGIN}\ntwo\n{END}\n",
    ],
    ids=["no-markers", "one-block", "two-blocks"],
)
def test_markers_balanced_accepts_well_formed_blocks(text):
    assert skills._git_hook_markers_balanced(text)


@pytest.mark.parametrize(
    "text",
    [
        f"{BEGIN}\nbody\n",
        f"body\n{END}\n",
        f"{END}\nbody\n{BEGIN}\n",
        f"{BEGIN}\n{BEGIN}\nbody\n{END}\n{END}\n",
    ],
    ids=["missing-end", "missing-begin", "end-before-begin", "nested"],
)
def test_markers_balanced_rejects_hand_edited_blocks(text):
    assert not skills._git_hook_markers_balanced(text)


def test_markers_balanced_only_counts_the_markers_it_is_given():
    # An orphan pre-commit marker says nothing about the sync block, and back.
    text = f"{BEGIN}\n{SYNC_BEGIN}\nbody\n{SYNC_END}\n"
    assert skills._git_hook_markers_balanced(text, SYNC_BEGIN, SYNC_END)
    assert not skills._git_hook_markers_balanced(text)


# --- _git_hook_block_span ---------------------------------------------------


def test_block_span_includes_the_trailing_newline():
    text = f"#!/bin/sh\n{BEGIN}\nbody\n{END}\necho after\n"
    begin, end = skills._git_hook_block_span(text)
    assert text[:begin] + text[end:] == "#!/bin/sh\necho after\n"


def test_block_span_at_end_of_file_without_newline():
    text = f"#!/bin/sh\n{BEGIN}\nbody\n{END}"
    begin, end = skills._git_hook_block_span(text)
    assert end == len(text)
    assert text[:begin] == "#!/bin/sh\n"


@pytest.mark.parametrize(
    "text",
    ["#!/bin/sh\necho user\n", f"#!/bin/sh\n{BEGIN}\nbody\n"],
    ids=["no-block", "missing-end"],
)
def test_block_span_is_none_without_a_closed_block(text):
    assert skills._git_hook_block_span(text) is None


def test_block_span_starts_searching_at_the_given_offset():
    text = f"{BEGIN}\none\n{END}\n{BEGIN}\ntwo\n{END}\n"
    _first_begin, first_end = skills._git_hook_block_span(text)
    begin, end = skills._git_hook_block_span(text, first_end)
    assert text[begin:end] == f"{BEGIN}\ntwo\n{END}\n"


def test_block_span_uses_the_markers_it_is_given():
    text = f"{BEGIN}\npre-commit\n{END}\n{SYNC_BEGIN}\nsync\n{SYNC_END}\n"
    begin, end = skills._git_hook_block_span(text, 0, SYNC_BEGIN, SYNC_END)
    assert text[begin:end] == f"{SYNC_BEGIN}\nsync\n{SYNC_END}\n"


# --- _git_hooks_dir ---------------------------------------------------------


def test_hooks_dir_is_dot_git_hooks_by_default(repo):
    assert skills._git_hooks_dir(repo).resolve() == (repo / ".git" / "hooks").resolve()


def test_hooks_dir_honours_core_hooks_path(repo):
    git(repo, "config", "core.hooksPath", ".husky")
    assert skills._git_hooks_dir(repo) == repo / ".husky"


def test_hooks_dir_in_a_linked_worktree_is_the_main_repositorys(repo, tmp_path):
    (repo / "file.txt").write_text("x\n")
    git(repo, "add", "file.txt")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-m", "x")
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-b", "feature", str(linked))
    assert skills._git_hooks_dir(linked).resolve() == (repo / ".git" / "hooks").resolve()


def test_hooks_dir_falls_back_to_dot_git_when_git_is_unavailable(repo, monkeypatch):
    monkeypatch.setattr(skills.subprocess, "run", git_unavailable)
    assert skills._git_hooks_dir(repo) == repo / ".git" / "hooks"


def test_hooks_dir_is_none_outside_a_repository(tmp_path, monkeypatch):
    monkeypatch.setattr(skills.subprocess, "run", git_unavailable)
    assert skills._git_hooks_dir(tmp_path) is None


# --- _resolve_git_hook ------------------------------------------------------


def test_resolve_git_hook_names_the_hook_inside_core_hooks_path(repo):
    git(repo, "config", "core.hooksPath", ".husky")
    assert uninstall._resolve_git_hook(repo, "post-merge") == repo / ".husky" / "post-merge"


def test_resolve_git_hook_defaults_to_pre_commit(repo):
    path = uninstall._resolve_git_hook(repo)
    assert path.resolve() == (repo / ".git" / "hooks" / "pre-commit").resolve()


def test_resolve_git_hook_falls_back_when_git_is_unavailable(repo, monkeypatch):
    monkeypatch.setattr(uninstall.subprocess, "run", git_unavailable)
    assert (
        uninstall._resolve_git_hook(repo, "post-checkout")
        == repo / ".git" / "hooks" / "post-checkout"
    )


# --- _rewrite_or_remove_hook ------------------------------------------------


@pytest.fixture
def hook(repo):
    path = repo / ".git" / "hooks" / "post-merge"
    path.write_text("#!/bin/sh\nold\n")
    return path


def test_rewrite_keeps_user_commands_with_one_trailing_newline(repo, hook):
    report = uninstall.UninstallReport()
    uninstall._rewrite_or_remove_hook(
        hook, "#!/bin/sh\necho user\n\n\n", repo, report, detail="edited", dry_run=False
    )
    assert hook.read_text() == "#!/bin/sh\necho user\n"
    assert report.edited_paths == [f"{hook} (edited)"]
    assert report.removed_paths == []


def test_rewrite_removes_a_hook_left_with_only_a_shebang(repo, hook):
    report = uninstall.UninstallReport()
    uninstall._rewrite_or_remove_hook(
        hook, "#!/bin/sh\n\n  \n", repo, report, detail="edited", dry_run=False
    )
    assert not hook.exists()
    assert len(report.removed_paths) == 1
    assert report.edited_paths == []


@pytest.mark.parametrize("new_text", ["#!/bin/sh\necho user\n", "#!/bin/sh\n"])
def test_rewrite_dry_run_touches_nothing(repo, hook, new_text):
    report = uninstall.UninstallReport()
    uninstall._rewrite_or_remove_hook(
        hook, new_text, repo, report, detail="edited", dry_run=True
    )
    assert hook.read_text() == "#!/bin/sh\nold\n"
    assert report.total_actions == 1
    assert all("would " in entry for entry in report.edited_paths + report.removed_paths)
