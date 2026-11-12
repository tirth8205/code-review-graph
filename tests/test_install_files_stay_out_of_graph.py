"""Files ``install`` wrote stay out of the graph and the review until Git tracks them.

``install`` lists every file it writes inside the repository in
``.code-review-graph/install-manifest.json``. Build and change discovery read
that list and leave out the paths Git does not track, so a new user's first
``build`` does not index ``.mcp.json`` and hook scripts, and their first
``detect-changes`` reviews their own change rather than forty install files.

A path Git tracks is kept: once the user commits an install file it is theirs.
No manifest means nothing changes.

Found by the developer-experience review of 2.4.0 (product item 3).
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from code_review_graph import cli, install_manifest, uninstall
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import (
    INSTALL_MANIFEST,
    collect_all_files,
    discover_review_changes,
    full_build,
    get_changed_files,
    get_staged_and_unstaged,
    install_manifest_paths,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

INSTALL_FILES = (".mcp.json", ".claude/settings.json", "hooks/crg-update.sh")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, timeout=30,
    )


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_manifest(repo: Path, files: list[str], version: int = 1) -> None:
    _write(repo, str(INSTALL_MANIFEST), json.dumps({"version": version, "files": files}))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with two commits and the files an install leaves behind."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _write(repo, ".gitignore", ".code-review-graph/\n")
    _write(repo, "app.py", "def handler():\n    return 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "first")
    _write(repo, "app.py", "def handler():\n    return 2\n")
    _git(repo, "commit", "-q", "-am", "second")

    _write(repo, ".mcp.json", '{"mcpServers": {"code-review-graph": {}}}\n')
    _write(repo, ".claude/settings.json", '{"hooks": {}}\n')
    _write(repo, "hooks/crg-update.sh", "#!/bin/sh\ncode-review-graph update --quiet\n")
    # The user's own new file, which must stay in.
    _write(repo, "feature.py", "def feature():\n    return 3\n")
    return repo


def test_without_a_manifest_nothing_changes(repo: Path) -> None:
    files = set(collect_all_files(repo))
    changed, _ = discover_review_changes(repo)

    assert set(INSTALL_FILES) <= files
    assert set(INSTALL_FILES) <= set(changed)


def test_build_leaves_out_untracked_install_files(repo: Path) -> None:
    _write_manifest(repo, list(INSTALL_FILES))

    files = set(collect_all_files(repo))

    assert not files & set(INSTALL_FILES)
    assert {"app.py", "feature.py"} <= files


def test_full_build_does_not_index_install_files(repo: Path, tmp_path: Path) -> None:
    _write_manifest(repo, list(INSTALL_FILES))

    with GraphStore(tmp_path / "graph.db") as store:
        full_build(repo, store)
        stored = {Path(path).relative_to(repo.resolve()).as_posix()
                  for path in store.get_all_files()}

    assert not stored & set(INSTALL_FILES)
    assert "feature.py" in stored


def test_change_discovery_leaves_out_untracked_install_files(repo: Path) -> None:
    _write_manifest(repo, list(INSTALL_FILES))

    changed, _ = discover_review_changes(repo)
    working_tree = get_staged_and_unstaged(repo)
    since_parent = get_changed_files(repo, "HEAD~1")

    for listed in (changed, working_tree, since_parent):
        assert not set(listed) & set(INSTALL_FILES)
        assert "feature.py" in listed


def test_status_fallback_leaves_out_untracked_install_files(repo: Path) -> None:
    """The working-tree path: nothing differs from the base, so git status answers."""
    _write_manifest(repo, list(INSTALL_FILES))

    changed, _ = discover_review_changes(repo, "HEAD")

    assert not set(changed) & set(INSTALL_FILES)
    assert "feature.py" in changed


def test_a_tracked_install_file_is_treated_like_any_other(repo: Path) -> None:
    _write_manifest(repo, list(INSTALL_FILES))
    _git(repo, "add", ".mcp.json")
    _git(repo, "commit", "-q", "-m", "keep the MCP config")
    _write(repo, ".mcp.json", '{"mcpServers": {}}\n')

    files = set(collect_all_files(repo))
    changed, _ = discover_review_changes(repo, "HEAD")

    assert ".mcp.json" in files
    assert ".mcp.json" in changed
    assert "hooks/crg-update.sh" not in files


def test_a_staged_install_file_is_tracked(repo: Path) -> None:
    _write_manifest(repo, list(INSTALL_FILES))
    _git(repo, "add", ".claude/settings.json")

    assert ".claude/settings.json" in collect_all_files(repo)
    assert ".claude/settings.json" in get_staged_and_unstaged(repo)


def test_walk_without_vcs_leaves_out_install_files(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    _write(root, "app.py", "def handler():\n    return 1\n")
    _write(root, ".mcp.json", "{}\n")
    _write_manifest(root, [".mcp.json"])

    files = set(collect_all_files(root))

    assert "app.py" in files
    assert ".mcp.json" not in files


@pytest.mark.parametrize(
    "content",
    ["not json", json.dumps(["a"]), json.dumps({"version": 2, "files": [".mcp.json"]}),
     json.dumps({"version": 1, "files": "x"})],
    ids=["not-json", "a-list", "unknown-version", "files-not-a-list"],
)
def test_a_manifest_this_release_cannot_read_leaves_nothing_out(
    repo: Path, content: str, caplog: pytest.LogCaptureFixture,
) -> None:
    _write(repo, str(INSTALL_MANIFEST), content)

    with caplog.at_level(logging.WARNING, logger="code_review_graph.incremental"):
        assert install_manifest_paths(repo) == frozenset()
        files = set(collect_all_files(repo))

    assert set(INSTALL_FILES) <= files
    assert "install-manifest.json" in caplog.text


def test_manifest_paths_are_normalised(tmp_path: Path) -> None:
    _write_manifest(tmp_path, ["./.mcp.json", "hooks//a.sh", "", 3])  # type: ignore[list-item]

    assert install_manifest_paths(tmp_path) == frozenset({".mcp.json", "hooks/a.sh"})


def test_the_record_install_writes_is_the_one_build_and_review_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real install, then a real build and review: the two sides agree on the record."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.setenv("DSH_HOME", str(home / ".dsh"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _write(repo, ".gitignore", "*.pyc\n")
    _write(repo, "app.py", "def handler():\n    return 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "first")
    _write(repo, "app.py", "def handler():\n    return 2\n")
    _git(repo, "commit", "-q", "-am", "second")

    cli._handle_init(argparse.Namespace(
        repo=str(repo), dry_run=False, platform="claude", yes=True,
        no_instructions=False, no_skills=False, no_hooks=False,
    ))
    _write(repo, "feature.py", "def feature():\n    return 3\n")

    written = set(install_manifest.read(repo))
    assert {".mcp.json", ".claude/settings.json", "CLAUDE.md"} <= written
    # Both sides read the same file the same way.
    assert install_manifest_paths(repo) == frozenset(written)

    tracked = {".gitignore"}  # committed before install edited it
    left_out = written - tracked
    files = set(collect_all_files(repo))
    changed, _ = discover_review_changes(repo)
    for listed in (files, set(changed)):
        assert not listed & left_out
        assert "feature.py" in listed
        assert not [path for path in listed if path.startswith(".code-review-graph/")]
    assert ".mcp.json" not in files

    # Once the user commits an install file, it is theirs.
    _git(repo, "add", ".mcp.json")
    _git(repo, "commit", "-q", "-m", "share the MCP config")
    assert ".mcp.json" in collect_all_files(repo)

    uninstall.run(repo=repo, keep_data=True)

    assert not install_manifest.manifest_path(repo).exists()
    assert install_manifest_paths(repo) == frozenset()
