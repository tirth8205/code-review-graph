"""Tests for the ``forget`` command and its file-matching helper.

``forget`` drops already-parsed files from the graph without a full rebuild,
so the two things worth guarding are (1) the path/glob matching that decides
*which* stored files to drop and (2) the end-to-end command keeping the graph
and its FTS index consistent afterwards.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from code_review_graph import cli
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.parser import NodeInfo
from code_review_graph.search import rebuild_fts_index


class TestMatchFilesToForget:
    """Unit tests for the pure path/glob matcher."""

    stored = [
        "/repo/pkg/auth.py",
        "/repo/pkg/views.py",
        "/repo/main.py",
    ]

    def test_exact_relative_path(self):
        matched = cli._match_files_to_forget(self.stored, ["pkg/auth.py"], Path("/repo"))
        assert matched == ["/repo/pkg/auth.py"]

    def test_exact_absolute_path(self):
        matched = cli._match_files_to_forget(self.stored, ["/repo/main.py"], Path("/repo"))
        assert matched == ["/repo/main.py"]

    def test_directory_prefix_matches_everything_underneath(self):
        matched = cli._match_files_to_forget(self.stored, ["pkg"], Path("/repo"))
        assert matched == ["/repo/pkg/auth.py", "/repo/pkg/views.py"]

    def test_relative_glob(self):
        matched = cli._match_files_to_forget(self.stored, ["pkg/*.py"], Path("/repo"))
        assert matched == ["/repo/pkg/auth.py", "/repo/pkg/views.py"]

    def test_no_match_returns_empty(self):
        assert cli._match_files_to_forget(self.stored, ["missing.py"], Path("/repo")) == []

    def test_multiple_patterns_are_unioned_and_deduplicated(self):
        matched = cli._match_files_to_forget(
            self.stored, ["pkg/auth.py", "pkg"], Path("/repo")
        )
        assert matched == ["/repo/pkg/auth.py", "/repo/pkg/views.py"]

    def test_blank_pattern_is_ignored(self):
        assert cli._match_files_to_forget(self.stored, ["   "], Path("/repo")) == []


def _seed_file(store: GraphStore, abs_path: str, symbol: str) -> None:
    """Store one File node and one Function node for a parsed file."""
    store.store_file_nodes_edges(
        abs_path,
        [
            NodeInfo(
                kind="File", name=abs_path, file_path=abs_path,
                line_start=1, line_end=40, language="python",
            ),
            NodeInfo(
                kind="Function", name=symbol, file_path=abs_path,
                line_start=5, line_end=20, language="python",
            ),
        ],
        [],
    )


def _fts_hits(store: GraphStore, symbol: str) -> int:
    row = store._conn.execute(
        "SELECT COUNT(*) FROM nodes_fts WHERE nodes_fts MATCH ?", (symbol,)
    ).fetchone()
    return row[0]


@pytest.fixture
def seeded_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A repo whose graph tracks three parsed files across two packages."""
    repo_root = tmp_path.resolve()
    files = {
        "auth": str(repo_root / "pkg" / "auth.py"),
        "views": str(repo_root / "pkg" / "views.py"),
        "main": str(repo_root / "main.py"),
    }
    store = GraphStore(get_db_path(repo_root))
    _seed_file(store, files["auth"], "authenticate")
    _seed_file(store, files["views"], "render_home")
    _seed_file(store, files["main"], "entrypoint")
    rebuild_fts_index(store)
    store.close()
    return repo_root, files


def _run_forget(repo_root: Path, *patterns: str, dry_run: bool = False) -> None:
    argv = ["code-review-graph", "forget", *patterns, "--repo", str(repo_root)]
    if dry_run:
        argv.append("--dry-run")
    with patch.object(sys, "argv", argv):
        cli.main()


def test_forget_removes_matching_file_and_keeps_the_rest(seeded_repo, capsys):
    repo_root, files = seeded_repo

    _run_forget(repo_root, "pkg/auth.py")

    store = GraphStore(get_db_path(repo_root))
    try:
        remaining = set(store.get_all_files())
        assert files["auth"] not in remaining
        assert files["views"] in remaining
        assert files["main"] in remaining
        # The FTS index must not keep phantom entries for the dropped file.
        assert _fts_hits(store, "authenticate") == 0
        assert _fts_hits(store, "render_home") == 1
    finally:
        store.close()

    out = capsys.readouterr().out
    assert "Forgot 1 file(s)" in out


def test_forget_directory_drops_every_file_underneath(seeded_repo):
    repo_root, files = seeded_repo

    _run_forget(repo_root, "pkg")

    store = GraphStore(get_db_path(repo_root))
    try:
        remaining = set(store.get_all_files())
        assert remaining == {files["main"]}
    finally:
        store.close()


def test_forget_dry_run_changes_nothing(seeded_repo, capsys):
    repo_root, files = seeded_repo

    _run_forget(repo_root, "pkg/auth.py", dry_run=True)

    store = GraphStore(get_db_path(repo_root))
    try:
        remaining = set(store.get_all_files())
        assert remaining == set(files.values())
    finally:
        store.close()

    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "No changes made." in out


def test_forget_reports_when_nothing_matches(seeded_repo, capsys):
    repo_root, files = seeded_repo

    _run_forget(repo_root, "does/not/exist.py")

    store = GraphStore(get_db_path(repo_root))
    try:
        assert set(store.get_all_files()) == set(files.values())
    finally:
        store.close()

    assert "No parsed files matched" in capsys.readouterr().out


def test_forget_without_a_graph_exits_nonzero(tmp_path, capsys):
    repo_root = tmp_path.resolve()
    with pytest.raises(SystemExit) as excinfo:
        _run_forget(repo_root, "anything.py")
    assert excinfo.value.code == 1
    assert "No graph found" in capsys.readouterr().err
