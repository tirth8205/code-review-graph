"""Integration tests exercising git-dependent code with real temporary repos.

Tests cover:
- get_changed_files with real git history
- parse_git_diff_ranges with real diffs
- incremental_update detecting real file modifications
- base ref injection rejection
- wiki page path traversal protection
"""

from __future__ import annotations

import inspect
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from code_review_graph.changes import parse_git_diff_ranges
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import (
    _commit_object_exists,
    collect_all_files,
    full_build,
    get_all_tracked_files,
    get_changed_files,
    get_staged_and_unstaged,
    incremental_update,
    resolve_incremental_base,
)
from code_review_graph.tools.build import build_or_update_graph
from code_review_graph.wiki import get_wiki_page


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command inside *repo* and return the result."""
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=str(repo),
        timeout=10,
    )


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """Create a real git repo with two commits.

    Commit 1 adds ``hello.py`` with a single function.
    Commit 2 modifies ``hello.py`` (adds a second function).
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    _git(repo, "init")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")

    # First commit
    py_file = repo / "hello.py"
    py_file.write_text("def greet():\n    return 'hello'\n")
    _git(repo, "add", "hello.py")
    _git(repo, "commit", "-m", "initial commit")

    # Second commit — modify the file
    py_file.write_text(
        "def greet():\n    return 'hello'\n\n"
        "def farewell():\n    return 'goodbye'\n"
    )
    _git(repo, "add", "hello.py")
    _git(repo, "commit", "-m", "add farewell function")

    return repo


# ------------------------------------------------------------------
# 1. get_changed_files with a real git repo
# ------------------------------------------------------------------


def test_get_changed_files_real_git(git_repo: Path) -> None:
    """get_changed_files should list hello.py as changed between HEAD~1..HEAD."""
    changed = get_changed_files(git_repo, base="HEAD~1")
    assert "hello.py" in changed


@pytest.fixture()
def git_repo_with_unicode_path(tmp_path: Path) -> Path:
    """Create a real repository with a committed non-ASCII Python path."""
    repo = tmp_path / "unicode-repo"
    repo.mkdir()
    assert _git(repo, "init").returncode == 0
    assert _git(repo, "config", "user.email", "test@test.com").returncode == 0
    assert _git(repo, "config", "user.name", "Test").returncode == 0
    (repo / "café.py").write_text("value = 1\n", encoding="utf-8")
    assert _git(repo, "add", "--", "café.py").returncode == 0
    assert _git(repo, "commit", "-m", "add unicode path").returncode == 0
    return repo


def test_get_changed_files_preserves_unicode_path(
    git_repo_with_unicode_path: Path,
) -> None:
    """Committed Git changes preserve a literal path on every platform."""
    source = git_repo_with_unicode_path / "café.py"
    source.write_text("value = 2\n", encoding="utf-8")
    assert _git(git_repo_with_unicode_path, "add", "--", "café.py").returncode == 0
    assert (
        _git(git_repo_with_unicode_path, "commit", "-m", "modify unicode path").returncode
        == 0
    )

    assert get_changed_files(git_repo_with_unicode_path, base="HEAD~1") == [
        "café.py"
    ]


def test_get_staged_and_unstaged_preserves_unicode_path(
    git_repo_with_unicode_path: Path,
) -> None:
    """Working-tree Git changes preserve a literal path on every platform."""
    source = git_repo_with_unicode_path / "café.py"
    source.write_text("value = 2\n", encoding="utf-8")

    assert get_staged_and_unstaged(git_repo_with_unicode_path) == ["café.py"]


def test_get_staged_and_unstaged_expands_new_untracked_directories(
    git_repo_with_unicode_path: Path,
) -> None:
    """A new directory reports its files, not an unusable directory placeholder."""
    nested = git_repo_with_unicode_path / "new" / "nested.py"
    nested.parent.mkdir()
    nested.write_text("value = 1\n", encoding="utf-8")

    assert get_staged_and_unstaged(git_repo_with_unicode_path) == [
        "new/nested.py",
    ]


def test_get_staged_and_unstaged_uses_rename_destination(
    git_repo_with_unicode_path: Path,
) -> None:
    """NUL porcelain returns only the destination record for a staged rename."""
    destination = "renamed café.py"
    assert (
        _git(
            git_repo_with_unicode_path,
            "mv",
            "--",
            "café.py",
            destination,
        ).returncode
        == 0
    )

    assert get_staged_and_unstaged(git_repo_with_unicode_path) == [destination]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows filenames cannot contain '>' or newline characters",
)
def test_git_discovery_preserves_literal_separator_characters(
    git_repo_with_unicode_path: Path,
) -> None:
    """NUL output preserves arrows and newlines as filename characters."""
    names = ["path -> literal.py", "line\nbreak.py"]
    for name in names:
        (git_repo_with_unicode_path / name).write_text("value = 1\n")
    assert _git(git_repo_with_unicode_path, "add", "--", *names).returncode == 0
    assert (
        _git(git_repo_with_unicode_path, "commit", "-m", "add literal paths").returncode
        == 0
    )

    assert set(
        get_changed_files(git_repo_with_unicode_path, base="HEAD~1")
    ) == set(names)

    for name in names:
        (git_repo_with_unicode_path / name).write_text("value = 2\n")
    assert set(get_staged_and_unstaged(git_repo_with_unicode_path)) == set(names)


# ------------------------------------------------------------------
# 2. parse_git_diff_ranges with a real git repo
# ------------------------------------------------------------------


def test_parse_git_diff_ranges_real_git(git_repo: Path) -> None:
    """parse_git_diff_ranges should return non-empty line ranges for hello.py."""
    ranges = parse_git_diff_ranges(str(git_repo), base="HEAD~1")
    assert "hello.py" in ranges
    assert len(ranges["hello.py"]) > 0
    # Each entry is a (start, end) tuple with positive line numbers
    for start, end in ranges["hello.py"]:
        assert start >= 1
        assert end >= start


# ------------------------------------------------------------------
# 3. incremental_update detects real modifications
# ------------------------------------------------------------------


def test_incremental_update_real_git(git_repo: Path) -> None:
    """Full build then incremental update should detect the second commit."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        store = GraphStore(db_path)

        # Reset to first commit, do a full build
        _git(git_repo, "checkout", "HEAD~1", "--detach")
        full_build(git_repo, store)
        initial_nodes = store.get_stats().total_nodes
        assert initial_nodes > 0, "full_build should create at least one node"

        # Move back to tip (second commit) and do incremental update
        _git(git_repo, "checkout", "-")
        result = incremental_update(
            git_repo, store, changed_files=["hello.py"]
        )
        assert result["files_updated"] >= 1
        assert "hello.py" in result["changed_files"]

        # The graph should now contain more nodes (farewell function added)
        assert store.get_stats().total_nodes >= initial_nodes

        store.close()
    finally:
        Path(db_path).unlink(missing_ok=True)


# ------------------------------------------------------------------
# 4. base ref injection is rejected
# ------------------------------------------------------------------


def test_base_validation_rejects_injection(git_repo: Path) -> None:
    """Passing a malicious --flag as base should be rejected (empty list)."""
    result = get_changed_files(git_repo, base="--output=/tmp/evil")
    assert result == []


# ------------------------------------------------------------------
# 5. wiki page path traversal is blocked
# ------------------------------------------------------------------


@pytest.fixture()
def git_repo_with_submodule(tmp_path: Path) -> Path:
    """Create a parent repo containing a git submodule with a Python file."""
    # Create the "library" repo that will become a submodule
    lib_repo = tmp_path / "lib"
    lib_repo.mkdir()
    _git(lib_repo, "init")
    _git(lib_repo, "config", "user.email", "test@test.com")
    _git(lib_repo, "config", "user.name", "Test")
    (lib_repo / "util.py").write_text("def helper():\n    pass\n")
    _git(lib_repo, "add", "util.py")
    _git(lib_repo, "commit", "-m", "lib initial")

    # Create the parent repo and add lib as a submodule
    parent = tmp_path / "parent"
    parent.mkdir()
    _git(parent, "init")
    _git(parent, "config", "user.email", "test@test.com")
    _git(parent, "config", "user.name", "Test")
    (parent / "main.py").write_text("def main():\n    pass\n")
    _git(parent, "add", "main.py")
    _git(parent, "commit", "-m", "parent initial")
    _git(
        parent, "-c", "protocol.file.allow=always",
        "submodule", "add", str(lib_repo), "lib",
    )
    _git(parent, "commit", "-m", "add lib submodule")

    return parent


def test_get_all_tracked_files_without_recurse(
    git_repo_with_submodule: Path,
) -> None:
    """Without recurse_submodules, submodule files are NOT listed."""
    files = get_all_tracked_files(
        git_repo_with_submodule, recurse_submodules=False
    )
    assert "main.py" in files
    # Submodule entry appears as a gitlink, not as individual files
    assert not any(f.startswith("lib/") for f in files)


def test_get_all_tracked_files_with_recurse(
    git_repo_with_submodule: Path,
) -> None:
    """With recurse_submodules=True, submodule files ARE listed."""
    files = get_all_tracked_files(
        git_repo_with_submodule, recurse_submodules=True
    )
    assert "main.py" in files
    assert "lib/util.py" in files


def test_collect_all_files_with_recurse(
    git_repo_with_submodule: Path,
) -> None:
    """collect_all_files with recurse_submodules includes submodule code."""
    files = collect_all_files(
        git_repo_with_submodule, recurse_submodules=True
    )
    assert "main.py" in files
    assert "lib/util.py" in files


def test_full_build_with_recurse_submodules(
    git_repo_with_submodule: Path,
) -> None:
    """full_build with recurse_submodules parses submodule files."""
    db_path = git_repo_with_submodule / ".code-review-graph" / "graph.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = GraphStore(db_path)
    try:
        result = full_build(
            git_repo_with_submodule, store, recurse_submodules=True
        )
        assert result["files_parsed"] >= 2  # main.py + lib/util.py
        assert result["errors"] == []

        # Verify both parent and submodule nodes exist
        parent_nodes = store.get_nodes_by_file(
            str(git_repo_with_submodule / "main.py")
        )
        sub_nodes = store.get_nodes_by_file(
            str(git_repo_with_submodule / "lib" / "util.py")
        )
        assert len(parent_nodes) > 0
        assert len(sub_nodes) > 0
    finally:
        store.close()


def test_wiki_page_path_traversal_blocked(tmp_path: Path) -> None:
    """get_wiki_page must not serve files outside the wiki directory."""
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()

    # Create a legitimate page
    (wiki_dir / "my-module.md").write_text("# My Module\n")

    # Attempt a path traversal — should return None
    result = get_wiki_page(str(wiki_dir), "../../etc/passwd")
    assert result is None


# ------------------------------------------------------------------
# 6. Auto-resolved incremental base (last-synced commit, not HEAD~1)
# ------------------------------------------------------------------


def _init_repo(tmp_path: Path) -> Path:
    """A git repo with one commit adding ``a.py``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.py").write_text("def alpha():\n    return 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "c0")
    return repo


def _commit_file(repo: Path, name: str) -> None:
    (repo / f"{name}.py").write_text(f"def {name}():\n    return 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", name)


def test_commit_object_exists(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert _commit_object_exists(repo, head) is True
    assert _commit_object_exists(repo, "0" * 40) is False
    # Injection-shaped refs are rejected before ever reaching git.
    assert _commit_object_exists(repo, "--output=/tmp/evil") is False


def test_resolve_incremental_base(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    store = GraphStore(str(repo / "graph.db"))
    try:
        # Usable anchor -> the stored SHA.
        store.set_metadata("git_head_sha", head)
        assert resolve_incremental_base(repo, store) == head
        # Unreachable anchor (rewrite / shallow clone) -> None (full rebuild).
        store.set_metadata("git_head_sha", "0" * 40)
        assert resolve_incremental_base(repo, store) is None
    finally:
        store.close()

    # No anchor at all (fresh / legacy database) -> None.
    store2 = GraphStore(str(repo / "graph2.db"))
    try:
        assert resolve_incremental_base(repo, store2) is None
    finally:
        store2.close()


def test_resolve_incremental_base_non_git(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    store = GraphStore(str(plain / "graph.db"))
    try:
        # Non-git working copies keep the concrete "HEAD~1" default so the
        # SVN/plain change-discovery path never receives None.
        assert resolve_incremental_base(plain, store) == "HEAD~1"
    finally:
        store.close()


def test_update_auto_base_catches_commits_missed_by_head1(tmp_path: Path) -> None:
    """The headline bug: after several out-of-band commits, a default update
    must reconcile all of them, not only the newest."""
    repo = _init_repo(tmp_path)
    c0 = _git(repo, "rev-parse", "HEAD").stdout.strip()
    build_or_update_graph(full_rebuild=True, repo_root=str(repo), postprocess="none")

    for name in ("beta", "gamma", "delta"):
        _commit_file(repo, name)

    # Old behaviour, reproduced explicitly: HEAD~1 sees only the last commit.
    head1 = get_changed_files(repo, "HEAD~1")
    assert head1 == ["delta.py"]

    # New behaviour: auto base resolves to c0 and catches every commit since.
    res = build_or_update_graph(
        full_rebuild=False, repo_root=str(repo), base=None, postprocess="none"
    )
    assert res["base_resolved"] == c0
    assert set(res["changed_files"]) == {"beta.py", "gamma.py", "delta.py"}


def test_update_auto_base_across_divergent_branch_switch(tmp_path: Path) -> None:
    """Build on one branch, then switch to a divergent branch whose HEAD~1 is
    NOT the anchor. A fixed HEAD~1 base would miss the difference and leave the
    other branch's files stale; the resolved anchor reconciles it.
    """
    repo = _init_repo(tmp_path)  # main @ c0 with a.py

    # A sibling branch off c0 that adds its own file, then back to main.
    _git(repo, "checkout", "-b", "sibling")
    _commit_file(repo, "sibling_only")
    _git(repo, "checkout", "main")

    # Advance main by two commits and build the graph at main's tip.
    _commit_file(repo, "main_one")
    _commit_file(repo, "main_two")
    main_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()
    build_or_update_graph(full_rebuild=True, repo_root=str(repo), postprocess="none")

    # Switch to the divergent sibling. Its HEAD~1 is c0, not main's tip, so a
    # fixed-HEAD~1 update could never reconcile against where the graph is.
    _git(repo, "checkout", "sibling")
    res = build_or_update_graph(
        full_rebuild=False, repo_root=str(repo), base=None, postprocess="none"
    )
    # The resolved base is the commit the graph was actually built at.
    assert res["base_resolved"] == main_tip
    assert res["build_type"] == "incremental"
    # The diff main_tip..sibling adds sibling's file and drops main's files.
    changed = set(res["changed_files"])
    assert {"sibling_only.py", "main_one.py", "main_two.py"} <= changed


def test_update_without_usable_anchor_falls_back_to_full_rebuild(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    build_or_update_graph(full_rebuild=True, repo_root=str(repo), postprocess="none")

    # Corrupt the anchor to an unreachable SHA (as a history rewrite would).
    from code_review_graph.incremental import get_db_path

    store = GraphStore(str(get_db_path(repo)))
    try:
        store.set_metadata("git_head_sha", "0" * 40)
    finally:
        store.close()

    _commit_file(repo, "epsilon")
    res = build_or_update_graph(
        full_rebuild=False, repo_root=str(repo), base=None, postprocess="none"
    )
    assert res["build_type"] == "full"
    assert res["base_resolved"] is None


def test_update_explicit_base_bypasses_auto_resolution(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    build_or_update_graph(full_rebuild=True, repo_root=str(repo), postprocess="none")
    for name in ("beta", "gamma"):
        _commit_file(repo, name)

    # An explicit base is honoured verbatim and stays incremental.
    res = build_or_update_graph(
        full_rebuild=False, repo_root=str(repo), base="HEAD~1", postprocess="none"
    )
    assert res["build_type"] == "incremental"
    assert res["base_resolved"] == "HEAD~1"
    assert res["changed_files"] == ["gamma.py"]


def test_mcp_tool_base_defaults_to_none() -> None:
    """The MCP wrapper must default base to None so omitted-base calls reach
    the auto-resolution path instead of a hardcoded HEAD~1."""
    from code_review_graph.main import build_or_update_graph_tool

    # FastMCP may wrap the tool; the underlying callable is stored on ``.fn``.
    fn = getattr(build_or_update_graph_tool, "fn", build_or_update_graph_tool)
    assert inspect.signature(fn).parameters["base"].default is None


def test_cli_update_brief_default_base_does_not_crash(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """`update --brief` with no explicit --base must not crash. The base now
    defaults to None, which the brief impact path cannot pass to git directly;
    it has to reuse the resolved base."""
    from code_review_graph import cli

    repo = _init_repo(tmp_path)
    build_or_update_graph(full_rebuild=True, repo_root=str(repo), postprocess="none")
    _commit_file(repo, "brief_new")

    monkeypatch.setattr(
        sys,
        "argv",
        ["code-review-graph", "update", "--brief", "--repo", str(repo)],
    )
    cli.main()  # would raise AttributeError/TypeError on a None base before the fix

    out = capsys.readouterr().out
    # It ran the incremental update and the brief impact summary without error.
    assert "Incremental:" in out
    assert "changed file" in out
