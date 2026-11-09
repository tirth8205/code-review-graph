"""The edit hook updates the graph of the repository that holds the edited file.

Issue #311: an agent started in a folder of sibling repositories, in an outer
repository with nested ones, or in a main checkout that edits a linked
worktree, got the graph of its starting directory updated (or none at all),
never the one next to the file it changed.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from code_review_graph import hook_update
from code_review_graph.skills import generate_hooks_config

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "CRG Test",
    "GIT_AUTHOR_EMAIL": "crg-tests@example.invalid",
    "GIT_COMMITTER_NAME": "CRG Test",
    "GIT_COMMITTER_EMAIL": "crg-tests@example.invalid",
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_IDENTITY},
    )


def _repo(path: Path, *, commit: bool = False) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    if commit:
        (path / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        _git(path, "add", "mod.py")
        _git(path, "commit", "-q", "-m", "init")
    return path.resolve()


def _give_graph(repo: Path) -> None:
    data = repo / ".code-review-graph"
    data.mkdir(exist_ok=True)
    (data / "graph.db").write_bytes(b"")


def _targets(monkeypatch, payload: dict) -> list[Path]:
    calls: list[Path] = []
    monkeypatch.setattr(hook_update, "_update_repo", calls.append)
    monkeypatch.setattr(hook_update.sys, "stdin", io.StringIO(json.dumps(payload)))
    hook_update.run_update_hook()
    return calls


def _edit(cwd: Path | None, file_path: Path | str) -> dict:
    payload: dict = {"tool_name": "Edit", "tool_input": {"file_path": str(file_path)}}
    if cwd is not None:
        payload["cwd"] = str(cwd)
    return payload


def test_sibling_repos_under_a_plain_folder(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    repo_a = _repo(workspace / "repo-a")
    repo_b = _repo(workspace / "repo-b")
    unbuilt = _repo(workspace / "repo-c")
    for repo in (repo_a, repo_b):
        _give_graph(repo)

    assert _targets(monkeypatch, _edit(workspace, repo_a / "a.py")) == [repo_a]
    assert _targets(monkeypatch, _edit(workspace, repo_b / "src" / "b.py")) == [repo_b]
    # A sibling nobody built is not built by an edit made from the folder.
    assert _targets(monkeypatch, _edit(workspace, unbuilt / "c.py")) == []
    assert not (unbuilt / ".code-review-graph").exists()


def test_nested_repo_inside_the_session_repo(monkeypatch, tmp_path):
    outer = _repo(tmp_path / "outer")
    inner = _repo(outer / "libs" / "inner")
    _give_graph(outer)

    # The outer graph cannot hold the inner repository's files, so it is not
    # the graph to update; with no graph of its own the inner repo is skipped.
    assert _targets(monkeypatch, _edit(outer, inner / "x.py")) == []

    _give_graph(inner)
    assert _targets(monkeypatch, _edit(outer, inner / "x.py")) == [inner]
    assert _targets(monkeypatch, _edit(outer, outer / "top.py")) == [outer]


def test_linked_worktree_edited_from_the_main_checkout(monkeypatch, tmp_path):
    main = _repo(tmp_path / "main", commit=True)
    worktree = tmp_path / "trees" / "feature"
    _git(main, "worktree", "add", "-q", "-b", "feature", str(worktree))
    worktree = worktree.resolve()
    _give_graph(main)

    # The main checkout's graph describes another commit: never the target.
    assert _targets(monkeypatch, _edit(main, worktree / "mod.py")) == []

    _give_graph(worktree)
    assert _targets(monkeypatch, _edit(main, worktree / "mod.py")) == [worktree]
    assert _targets(monkeypatch, _edit(main, main / "mod.py")) == [main]


def test_session_started_in_a_worktree_updates_that_worktree(monkeypatch, tmp_path):
    main = _repo(tmp_path / "main", commit=True)
    worktree = main / ".claude" / "worktrees" / "agent"
    _git(main, "worktree", "add", "-q", "-b", "agent", str(worktree))
    worktree = worktree.resolve()
    _give_graph(main)

    assert _targets(monkeypatch, _edit(worktree, worktree / "mod.py")) == [worktree]


def test_single_repo_is_updated_as_before(monkeypatch, tmp_path):
    """The session's own repository is updated, and built, with no graph yet."""
    repo = _repo(tmp_path / "repo")
    (repo / "pkg").mkdir()

    assert _targets(monkeypatch, _edit(repo / "pkg", repo / "pkg" / "x.py")) == [repo]
    # A relative path is read against the payload cwd.
    assert _targets(monkeypatch, _edit(repo / "pkg", "x.py")) == [repo]
    # A file not written yet, in a directory not made yet, still has a repo.
    assert _targets(monkeypatch, _edit(repo, repo / "new" / "dir" / "x.py")) == [repo]


def test_an_edit_outside_every_repository_updates_nothing(monkeypatch, tmp_path):
    repo = _repo(tmp_path / "repo")
    loose = tmp_path / "loose"
    loose.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))

    assert _targets(monkeypatch, _edit(repo, loose / "notes.py")) == []


def test_an_edit_in_another_unbuilt_repository_starts_no_build(monkeypatch, tmp_path):
    """Editing a dotfiles repository from a project must not index it."""
    project = _repo(tmp_path / "project")
    elsewhere = _repo(tmp_path / "dotfiles")

    assert _targets(monkeypatch, _edit(project, elsewhere / ".zshrc")) == []

    _give_graph(elsewhere)
    assert _targets(monkeypatch, _edit(project, elsewhere / ".zshrc")) == [elsewhere]


def test_without_a_file_path_the_cwd_repo_is_used(monkeypatch, tmp_path):
    repo = _repo(tmp_path / "repo")
    payload = {"cwd": str(repo), "tool_name": "Bash", "tool_input": {"command": "ls"}}

    assert _targets(monkeypatch, payload) == [repo]


def test_without_a_cwd_only_a_built_repo_is_updated(monkeypatch, tmp_path):
    repo = _repo(tmp_path / "repo")

    assert _targets(monkeypatch, _edit(None, repo / "x.py")) == []
    # A relative path with no cwd to read it against picks nothing.
    assert _targets(monkeypatch, _edit(None, "x.py")) == []

    _give_graph(repo)
    assert _targets(monkeypatch, _edit(None, repo / "x.py")) == [repo]


def test_notebook_edits_are_routed_too(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = _repo(workspace / "repo")
    _give_graph(repo)
    payload = {
        "cwd": str(workspace),
        "tool_name": "NotebookEdit",
        "tool_input": {"notebook_path": str(repo / "nb.ipynb")},
    }

    assert _targets(monkeypatch, payload) == [repo]


def test_a_submodule_without_a_graph_hands_the_edit_to_its_superproject(
    monkeypatch, tmp_path
):
    """A superproject graph can hold submodule files (CRG_RECURSE_SUBMODULES)."""
    library = _repo(tmp_path / "library", commit=True)
    superproject = _repo(tmp_path / "super", commit=True)
    _git(
        superproject,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(library),
        "vendor/library",
    )
    submodule = (superproject / "vendor" / "library").resolve()
    _give_graph(superproject)
    workspace = tmp_path

    assert _targets(monkeypatch, _edit(workspace, submodule / "mod.py")) == [superproject]

    _give_graph(submodule)
    assert _targets(monkeypatch, _edit(workspace, submodule / "mod.py")) == [submodule]


def _shared_graph(data_dir: Path, built_for: Path) -> None:
    """A ``CRG_DATA_DIR`` graph, recording the repository it was built for."""
    from code_review_graph.graph import GraphStore

    data_dir.mkdir(parents=True, exist_ok=True)
    store = GraphStore(data_dir / "graph.db")
    try:
        store.set_metadata("repo_root", str(built_for))
        store.commit()
    finally:
        store.close()


def test_a_shared_data_dir_graph_belongs_only_to_the_repo_it_was_built_for(
    monkeypatch, tmp_path
):
    """Every repo resolves to one ``CRG_DATA_DIR`` database; only one owns it."""
    workspace = tmp_path / "workspace"
    repo_a = _repo(workspace / "repo-a")
    repo_b = _repo(workspace / "repo-b")
    shared = tmp_path / "shared"
    _shared_graph(shared, repo_a)
    monkeypatch.setenv("CRG_DATA_DIR", str(shared))

    assert _targets(monkeypatch, _edit(workspace, repo_a / "a.py")) == [repo_a]
    # Updating repo-b would rebuild repo-a's graph with repo-b's files.
    assert _targets(monkeypatch, _edit(workspace, repo_b / "b.py")) == []
    assert _targets(monkeypatch, _edit(repo_a, repo_b / "b.py")) == []
    assert _targets(monkeypatch, _edit(repo_a, repo_a / "a.py")) == [repo_a]


def test_a_submodule_reaches_its_superproject_through_a_shared_data_dir(
    monkeypatch, tmp_path
):
    library = _repo(tmp_path / "library", commit=True)
    superproject = _repo(tmp_path / "super", commit=True)
    _git(
        superproject,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(library),
        "vendor/library",
    )
    submodule = (superproject / "vendor" / "library").resolve()
    shared = tmp_path / "shared"
    _shared_graph(shared, superproject)
    monkeypatch.setenv("CRG_DATA_DIR", str(shared))

    assert _targets(monkeypatch, _edit(tmp_path, submodule / "mod.py")) == [superproject]


def test_a_graph_at_the_legacy_location_counts_as_built(monkeypatch, tmp_path):
    """The update migrates ``.code-review-graph.db``; detection must not skip it."""
    workspace = tmp_path / "workspace"
    repo = _repo(workspace / "repo")
    legacy = repo / ".code-review-graph.db"
    legacy.write_bytes(b"")

    assert _targets(monkeypatch, _edit(workspace, repo / "x.py")) == [repo]
    # Detection itself moves nothing.
    assert legacy.is_file()
    assert not (repo / ".code-review-graph").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="runs the POSIX hook command")
def test_installed_claude_hook_updates_the_sibling_that_was_edited(tmp_path):
    """The command ``install`` writes, end to end, from a folder of siblings."""
    workspace = tmp_path / "workspace"
    repos = {}
    for name in ("repo-a", "repo-b"):
        repo = _repo(workspace / name, commit=True)
        repos[name] = repo
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "code-review-graph"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m code_review_graph "$@"\n')
    shim.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
    }
    for repo in repos.values():
        built = subprocess.run(
            [sys.executable, "-m", "code_review_graph", "build", "--repo", str(repo), "-q"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert built.returncode == 0, built.stderr

    command = generate_hooks_config(workspace)["hooks"]["PostToolUse"][0]["hooks"][0][
        "command"
    ]
    edited = repos["repo-b"] / "mod.py"
    edited.write_text("def f():\n    return 1\n\n\ndef added():\n    return 2\n")
    proc = subprocess.run(
        ["/bin/sh", "-c", command],
        cwd=workspace,
        input=json.dumps(_edit(workspace, edited)),
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr

    def _functions(repo: Path) -> set[str]:
        import sqlite3

        db = repo / ".code-review-graph" / "graph.db"
        with sqlite3.connect(db) as conn:
            return {
                row[0]
                for row in conn.execute("SELECT name FROM nodes WHERE kind = 'Function'")
            }

    assert "added" in _functions(repos["repo-b"])
    assert "added" not in _functions(repos["repo-a"])
    assert not (workspace / ".code-review-graph").exists()
