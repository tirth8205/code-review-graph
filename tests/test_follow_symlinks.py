"""Following symlinked folders is opt-in, stays inside the repository, never
loops, and stores each file once under its real path (issue #269).

Off by default, so nothing changes for anyone who does not ask. When on
(``--follow-symlinks`` or ``CRG_FOLLOW_SYMLINKS=1``), a full build, an
incremental update and the watcher all hold the same files.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_review_graph import cli
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import (
    FOLLOW_SYMLINKS_ENV,
    _create_watch_handler,
    collect_all_files,
    follow_symlinks_enabled,
    full_build,
    incremental_update,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="symlink creation needs privileges"
)


def _git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=str(repo), timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture(autouse=True)
def _option_unset(monkeypatch):
    monkeypatch.delenv(FOLLOW_SYMLINKS_ENV, raising=False)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A repository whose only route to two folders is through symlinks.

    ``shared/`` and ``other/`` are ignored by Git, so today nothing in them is
    indexed. ``app/shared`` reaches ``shared/``, which reaches ``other/``,
    which points back at ``shared/``. ``app/up`` and ``shared/loop`` point at
    folders that hold them, and ``app/outside`` leaves the repository.
    """
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    (root / "app").mkdir(parents=True)
    (root / "shared").mkdir()
    (root / "other").mkdir()
    outside.mkdir()
    (root / ".gitignore").write_text("/shared/\n/other/\n")
    (root / "app" / "main.py").write_text("def main():\n    pass\n")
    (root / "shared" / "util.py").write_text("def util():\n    pass\n")
    (root / "other" / "extra.py").write_text("def extra():\n    pass\n")
    (outside / "secret.py").write_text("def secret():\n    pass\n")
    (root / "app" / "shared").symlink_to("../shared", target_is_directory=True)
    (root / "app" / "up").symlink_to("..", target_is_directory=True)
    (root / "app" / "outside").symlink_to(outside, target_is_directory=True)
    (root / "shared" / "loop").symlink_to(".", target_is_directory=True)
    (root / "shared" / "to_other").symlink_to("../other", target_is_directory=True)
    (root / "other" / "back").symlink_to("../shared", target_is_directory=True)
    (root / "shared" / "alias.py").symlink_to("util.py")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root.resolve()


def _files(store: GraphStore, root: Path) -> set[str]:
    return {Path(path).relative_to(root).as_posix() for path in store.get_all_files()}


def _build(root: Path, db: Path, **kwargs) -> tuple[dict, set[str]]:
    store = GraphStore(db)
    try:
        result = full_build(root, store, **kwargs)
        return result, _files(store, root)
    finally:
        store.close()


def test_off_by_default_nothing_changes(repo: Path, tmp_path: Path) -> None:
    assert follow_symlinks_enabled() is False
    assert collect_all_files(repo) == ["app/main.py"]
    result, files = _build(repo, tmp_path / "off.db")
    assert files == {"app/main.py"}
    assert result["files_parsed"] == 1


def test_on_follows_inside_links_once_under_the_real_path(
    repo: Path, tmp_path: Path,
) -> None:
    result, files = _build(repo, tmp_path / "on.db", follow_symlinks=True)

    assert files == {"app/main.py", "shared/util.py", "other/extra.py"}
    assert result["errors"] == []
    # The outside link, the cycles and the symlinked file add nothing.
    assert not any("secret" in path for path in files)
    assert not any(part in {"up", "loop", "back", "alias.py"} for path in files
                   for part in path.split("/"))


def test_env_var_turns_it_on_and_the_flag_wins(repo: Path, monkeypatch) -> None:
    monkeypatch.setenv(FOLLOW_SYMLINKS_ENV, "1")
    assert follow_symlinks_enabled() is True
    assert "shared/util.py" in collect_all_files(repo)
    assert collect_all_files(repo, follow_symlinks=False) == ["app/main.py"]


def test_link_to_an_indexed_folder_adds_no_duplicates(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "lib").mkdir(parents=True)
    (root / "app").mkdir()
    (root / "lib" / "core.py").write_text("def core():\n    pass\n")
    (root / "app" / "lib").symlink_to("../lib", target_is_directory=True)
    (root / "app" / "LIB2").symlink_to("../lib", target_is_directory=True)
    _git(root, "init", "-q")
    _git(root, "add", ".")
    root = root.resolve()

    assert collect_all_files(root, follow_symlinks=True) == collect_all_files(root)


def test_link_spelling_another_case_adds_no_duplicates(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "lib").mkdir(parents=True)
    if not (root / "LIB").exists():
        pytest.skip("needs a case-insensitive disk")
    (root / "app").mkdir()
    (root / "lib" / "core.py").write_text("def core():\n    pass\n")
    (root / "app" / "shared").symlink_to("../LIB", target_is_directory=True)
    _git(root, "init", "-q")
    _git(root, "add", ".")
    root = root.resolve()

    assert collect_all_files(root, follow_symlinks=True) == ["lib/core.py"]


def test_ignored_link_and_ignored_target_are_not_followed(repo: Path) -> None:
    (repo / ".code-review-graphignore").write_text("app/shared/\n")
    assert collect_all_files(repo, follow_symlinks=True) == ["app/main.py"]

    (repo / ".code-review-graphignore").write_text("/other/\n")
    files = collect_all_files(repo, follow_symlinks=True)
    assert "shared/util.py" in files
    assert "other/extra.py" not in files


def test_update_agrees_with_full_build(repo: Path, tmp_path: Path) -> None:
    store = GraphStore(tmp_path / "graph.db")
    try:
        full_build(repo, store, follow_symlinks=True)

        # A new file behind a link: Git never reports it.
        (repo / "shared" / "new.py").write_text("def new():\n    pass\n")
        incremental_update(repo, store, base="HEAD", follow_symlinks=True)
        assert "shared/new.py" in _files(store, repo)
        assert _files(store, repo) == _build(
            repo, tmp_path / "check1.db", follow_symlinks=True
        )[1]

        # The link goes: what only it reached goes too.
        (repo / "app" / "shared").unlink()
        incremental_update(repo, store, base="HEAD", follow_symlinks=True)
        assert _files(store, repo) == {"app/main.py"}
        assert _files(store, repo) == _build(
            repo, tmp_path / "check2.db", follow_symlinks=True
        )[1]
    finally:
        store.close()


def test_update_with_option_off_drops_followed_files(repo: Path, tmp_path: Path) -> None:
    store = GraphStore(tmp_path / "graph.db")
    try:
        full_build(repo, store, follow_symlinks=True)
        assert "shared/util.py" in _files(store, repo)

        incremental_update(repo, store, base="HEAD")
        assert _files(store, repo) == {"app/main.py"}
    finally:
        store.close()


def test_update_off_never_touches_a_graph_that_did_not_use_it(
    repo: Path, tmp_path: Path,
) -> None:
    store = GraphStore(tmp_path / "graph.db")
    try:
        full_build(repo, store)
        with patch(
            "code_review_graph.incremental._inventory_candidates",
            side_effect=AssertionError("update listed the repository"),
        ):
            incremental_update(repo, store, base="HEAD")
        assert store.get_metadata("symlink_followed_files") is None
    finally:
        store.close()


def test_watch_follows_a_new_link_and_drops_a_removed_one(
    repo: Path, tmp_path: Path,
) -> None:
    from watchdog.events import FileCreatedEvent, FileDeletedEvent

    link = repo / "app" / "shared"
    link.unlink()
    store = GraphStore(tmp_path / "graph.db")
    try:
        full_build(repo, store, follow_symlinks=True)
        assert _files(store, repo) == {"app/main.py"}
        handler = _create_watch_handler(repo, store, None, follow_symlinks=True)

        link.symlink_to("../shared", target_is_directory=True)
        handler.process([FileCreatedEvent(str(link))])
        handler.raise_if_failed()
        assert _files(store, repo) == {"app/main.py", "shared/util.py", "other/extra.py"}
        assert _files(store, repo) == _build(
            repo, tmp_path / "check1.db", follow_symlinks=True
        )[1]

        link.unlink()
        handler.process([FileDeletedEvent(str(link))])
        handler.raise_if_failed()
        assert _files(store, repo) == {"app/main.py"}
    finally:
        store.close()


def test_watch_off_ignores_a_new_link(repo: Path, tmp_path: Path) -> None:
    from watchdog.events import FileCreatedEvent

    link = repo / "app" / "shared"
    link.unlink()
    store = GraphStore(tmp_path / "graph.db")
    try:
        full_build(repo, store)
        handler = _create_watch_handler(repo, store, None)
        link.symlink_to("../shared", target_is_directory=True)
        handler.process([FileCreatedEvent(str(link))])
        handler.raise_if_failed()
        assert _files(store, repo) == {"app/main.py"}
    finally:
        store.close()


@pytest.mark.parametrize(
    ("flag", "expected"),
    [([], {}), (["--follow-symlinks"], {"follow_symlinks": True}),
     (["--no-follow-symlinks"], {"follow_symlinks": False})],
)
@pytest.mark.parametrize("command", ["build", "update"])
def test_cli_flag_reaches_the_build(command: str, flag: list[str], expected: dict) -> None:
    result = {"files_parsed": 0, "files_updated": 0, "total_nodes": 0, "total_edges": 0}
    argv = ["code-review-graph", command, "--repo", "repo-root", "-q", *flag]
    with patch.object(sys, "argv", argv), \
            patch("code_review_graph.cli._ensure_project_marker"), \
            patch(
                "code_review_graph.tools.build.build_or_update_graph", return_value=result,
            ) as build:
        cli.main()
    kwargs = build.call_args.kwargs
    assert {k: v for k, v in kwargs.items() if k == "follow_symlinks"} == expected


def test_cli_watch_flag_reaches_watch(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    monkeypatch.chdir(root)
    argv = ["code-review-graph", "watch", "--repo", ".", "--follow-symlinks"]
    with patch.object(sys, "argv", argv), \
            patch("code_review_graph.graph.GraphStore", return_value=MagicMock()), \
            patch("code_review_graph.incremental.get_db_path", return_value=MagicMock()), \
            patch("code_review_graph.incremental.watch") as watch:
        cli.main()
    assert watch.call_args.kwargs["follow_symlinks"] is True


def test_build_tool_passes_the_option(repo: Path) -> None:
    from code_review_graph.tools.build import build_or_update_graph

    result = build_or_update_graph(
        full_rebuild=True, repo_root=str(repo), postprocess="none", follow_symlinks=True,
    )
    assert result["files_parsed"] == 3
    assert os.path.isfile(repo / ".code-review-graph" / "graph.db")
