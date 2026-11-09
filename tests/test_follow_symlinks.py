"""Following symlinked folders is opt-in, never loops, is capped, and stores
each file once under a path inside the repository (issue #269).

Off by default, so nothing changes for anyone who does not ask. When on
(``--follow-symlinks`` or ``CRG_FOLLOW_SYMLINKS=1``), links to folders
outside the repository are followed too, because the user made the link and
asked for it; their files are stored under the in-repo link path, never the
outside real path. A full build, an incremental update and the watcher all
hold the same files, and every tool that reads a file back reads these
through the in-repo path while still refusing paths outside the repository
that did not come through a followed link.
"""

from __future__ import annotations

import logging
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
    _WatchSupervisor,
    collect_all_files,
    follow_symlinks_enabled,
    followed_files,
    full_build,
    incremental_update,
    path_within_repo,
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
    monkeypatch.delenv("CRG_MAX_FOLLOWED_FILES", raising=False)


@pytest.fixture()
def outside(tmp_path: Path) -> Path:
    folder = tmp_path / "outside"
    folder.mkdir()
    (folder / "secret.py").write_text("def secret():\n    pass\n")
    return folder.resolve()


@pytest.fixture()
def repo(tmp_path: Path, outside: Path) -> Path:
    """A repository whose only route to three folders is through symlinks.

    ``shared/`` and ``other/`` are ignored by Git, so today nothing in them is
    indexed, and ``app/outside`` points outside the repository. ``app/up``
    and ``shared/loop`` point at folders that hold them, and ``app/home``
    points at the folder that holds the whole repository.
    """
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "shared").mkdir()
    (root / "other").mkdir()
    (root / ".gitignore").write_text("/shared/\n/other/\n")
    (root / "app" / "main.py").write_text("def main():\n    pass\n")
    (root / "shared" / "util.py").write_text("def util():\n    pass\n")
    (root / "other" / "extra.py").write_text("def extra():\n    pass\n")
    (root / "app" / "shared").symlink_to("../shared", target_is_directory=True)
    (root / "app" / "up").symlink_to("..", target_is_directory=True)
    (root / "app" / "outside").symlink_to(outside, target_is_directory=True)
    (root / "app" / "home").symlink_to(tmp_path, target_is_directory=True)
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


ON = {"app/main.py", "shared/util.py", "other/extra.py", "app/outside/secret.py"}


def _files(store: GraphStore, root: Path) -> set[str]:
    return {Path(path).relative_to(root).as_posix() for path in store.get_all_files()}


def _build(root: Path, db: Path, **kwargs) -> tuple[dict, set[str]]:
    store = GraphStore(db)
    try:
        result = full_build(root, store, **kwargs)
        return result, _files(store, root)
    finally:
        store.close()


# -- full build ----------------------------------------------------------


def test_off_by_default_nothing_changes(repo: Path, tmp_path: Path) -> None:
    assert follow_symlinks_enabled() is False
    assert collect_all_files(repo) == ["app/main.py"]
    result, files = _build(repo, tmp_path / "off.db")
    assert files == {"app/main.py"}
    assert result["files_parsed"] == 1


def test_on_stores_every_file_under_an_in_repo_path(
    repo: Path, outside: Path, tmp_path: Path,
) -> None:
    store = GraphStore(tmp_path / "on.db")
    try:
        result = full_build(repo, store, follow_symlinks=True)
        assert result["errors"] == []
        assert _files(store, repo) == ON
        # Never the outside real path, for nodes or for edges.
        rows = store._conn.execute(
            "SELECT file_path FROM nodes UNION SELECT target_qualified FROM edges"
        ).fetchall()
        assert not any(str(outside) in row[0] for row in rows)
    finally:
        store.close()


def test_cycles_and_links_above_the_repo_are_skipped(repo: Path, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="code_review_graph.incremental"):
        files = collect_all_files(repo, follow_symlinks=True)
    assert set(files) == ON
    assert not any(
        part in {"up", "home", "loop", "back", "alias.py"}
        for path in files for part in path.split("/")
    )
    text = caplog.text
    assert "Not following app/up" in text
    assert "Not following app/home" in text
    assert "Not following shared/loop" in text


def test_env_var_turns_it_on_and_the_flag_wins(repo: Path, monkeypatch) -> None:
    monkeypatch.setenv(FOLLOW_SYMLINKS_ENV, "1")
    assert follow_symlinks_enabled() is True
    assert set(collect_all_files(repo)) == ON
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


def test_two_links_to_one_outside_folder_store_it_once(
    tmp_path: Path, outside: Path,
) -> None:
    root = tmp_path / "plain"
    (root / ".code-review-graph").mkdir(parents=True)
    (root / "a").mkdir()
    (root / "b").mkdir()
    (root / "a" / "x").symlink_to(outside, target_is_directory=True)
    (root / "b" / "y").symlink_to(outside, target_is_directory=True)
    root = root.resolve()

    assert collect_all_files(root, follow_symlinks=True) == ["a/x/secret.py"]


def test_cap_stops_the_walk_and_says_so(
    tmp_path: Path, outside: Path, monkeypatch, caplog,
) -> None:
    for index in range(4):
        (outside / f"m{index}.py").write_text(f"def m{index}():\n    pass\n")
    root = tmp_path / "plain"
    (root / ".code-review-graph").mkdir(parents=True)
    (root / "inputs").symlink_to(outside, target_is_directory=True)
    root = root.resolve()
    monkeypatch.setenv("CRG_MAX_FOLLOWED_FILES", "2")

    with caplog.at_level(logging.WARNING, logger="code_review_graph.incremental"):
        files = collect_all_files(root, follow_symlinks=True)

    assert len(files) == 2
    assert all(path.startswith("inputs/") for path in files)
    assert "CRG_MAX_FOLLOWED_FILES" in caplog.text
    # The same files every time, so update and watch agree with the build.
    assert collect_all_files(root, follow_symlinks=True) == files


def test_ignored_link_and_ignored_target_are_not_followed(repo: Path) -> None:
    (repo / ".code-review-graphignore").write_text("app/shared/\napp/outside/\n")
    assert collect_all_files(repo, follow_symlinks=True) == ["app/main.py"]

    (repo / ".code-review-graphignore").write_text("/other/\n")
    files = collect_all_files(repo, follow_symlinks=True)
    assert "shared/util.py" in files
    assert "other/extra.py" not in files


def test_edges_into_an_outside_folder_use_the_in_repo_path(
    tmp_path: Path, outside: Path,
) -> None:
    (outside / "c.ts").write_text(
        "import { x } from './d';\nexport function y() { return x(); }\n"
    )
    (outside / "d.ts").write_text("export function x() { return 1; }\n")
    root = tmp_path / "plain"
    (root / ".code-review-graph").mkdir(parents=True)
    (root / "web").symlink_to(outside, target_is_directory=True)
    root = root.resolve()
    store = GraphStore(tmp_path / "graph.db")
    try:
        full_build(root, store, follow_symlinks=True)
        targets = {
            row[0] for row in store._conn.execute(
                "SELECT target_qualified FROM edges WHERE file_path = ?",
                (str(root / "web" / "c.ts"),),
            )
        }
        assert str(root / "web" / "d.ts") in targets
        assert not any(str(outside) in target for target in targets)
    finally:
        store.close()


# -- incremental update --------------------------------------------------


def test_update_agrees_with_full_build(repo: Path, outside: Path, tmp_path: Path) -> None:
    store = GraphStore(tmp_path / "graph.db")
    try:
        full_build(repo, store, follow_symlinks=True)

        # New files behind links: Git never reports them.
        (repo / "shared" / "new.py").write_text("def new():\n    pass\n")
        (outside / "more.py").write_text("def more():\n    pass\n")
        # An edit behind the outside link.
        (outside / "secret.py").write_text("def renamed():\n    pass\n")
        incremental_update(repo, store, base="HEAD", follow_symlinks=True)
        assert {"shared/new.py", "app/outside/more.py"} <= _files(store, repo)
        names = {n.name for n in store.get_nodes_by_file(str(repo / "app/outside/secret.py"))}
        assert "renamed" in names
        assert _files(store, repo) == _build(
            repo, tmp_path / "check1.db", follow_symlinks=True
        )[1]

        # The links go: what only they reached goes too.
        (repo / "app" / "shared").unlink()
        (repo / "app" / "outside").unlink()
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
        assert _files(store, repo) == ON

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


def test_update_refuses_a_path_through_a_link_it_did_not_follow(
    repo: Path, tmp_path: Path,
) -> None:
    store = GraphStore(tmp_path / "graph.db")
    try:
        full_build(repo, store)
        incremental_update(
            repo, store, changed_files=["app/outside/secret.py"], reconcile_stale=False,
        )
        assert _files(store, repo) == {"app/main.py"}

        # On, but the path goes through a link the walk skips (a cycle).
        full_build(repo, store, follow_symlinks=True)
        incremental_update(
            repo, store, changed_files=["app/home/outside/secret.py"],
            reconcile_stale=False, follow_symlinks=True,
        )
        assert _files(store, repo) == ON
    finally:
        store.close()


# -- reading files back --------------------------------------------------


def test_read_back_guard(repo: Path, outside: Path, tmp_path: Path) -> None:
    store = GraphStore(tmp_path / "graph.db")
    try:
        full_build(repo, store, follow_symlinks=True)
        followed = followed_files(store)
    finally:
        store.close()
    assert path_within_repo(repo, repo / "app" / "main.py", followed)
    assert path_within_repo(repo, "shared/util.py", followed)
    assert path_within_repo(repo, repo / "app" / "outside" / "secret.py", followed)
    assert path_within_repo(repo, "app/outside/secret.py", followed)
    # Outside paths that did not come through a followed link.
    assert not path_within_repo(repo, repo / "app" / "outside" / "secret.py", set())
    assert not path_within_repo(repo, repo / "app" / "home" / "outside" / "secret.py", followed)
    assert not path_within_repo(repo, outside / "secret.py", followed)
    assert not path_within_repo(repo, "../outside/secret.py", followed)


def _built_tool_repo(repo: Path) -> None:
    from code_review_graph.tools.build import build_or_update_graph

    result = build_or_update_graph(
        full_rebuild=True, repo_root=str(repo), postprocess="minimal", follow_symlinks=True,
    )
    assert result["files_parsed"] == len(ON)


def test_search_source_reads_followed_files_only(repo: Path) -> None:
    from code_review_graph.tools.query import semantic_search_nodes

    _built_tool_repo(repo)
    hits = semantic_search_nodes("secret", repo_root=str(repo), include_source=True)
    hit = next(r for r in hits["results"] if r["name"] == "secret")
    assert "def secret" in hit["source"]

    # The same row without the record of the followed link is refused.
    store = GraphStore(repo / ".code-review-graph" / "graph.db")
    try:
        store.set_metadata("symlink_followed_files", "[]")
    finally:
        store.close()
    hits = semantic_search_nodes("secret", repo_root=str(repo), include_source=True)
    hit = next(r for r in hits["results"] if r["name"] == "secret")
    assert "source" not in hit


def test_review_context_reads_followed_files_and_refuses_others(repo: Path) -> None:
    from code_review_graph.tools.review import get_review_context

    _built_tool_repo(repo)
    review = get_review_context(
        changed_files=["app/outside/secret.py"], repo_root=str(repo), include_source=True,
    )
    assert "def secret" in str(review["context"]["source_snippets"])

    refused = get_review_context(
        changed_files=["app/home/outside/secret.py", "../outside/secret.py"],
        repo_root=str(repo), include_source=True,
    )
    assert "def secret" not in str(refused)


def test_forget_works_through_the_link_path(repo: Path) -> None:
    from code_review_graph.cli import _match_files_to_forget
    from code_review_graph.forget import forget_files

    _built_tool_repo(repo)
    store = GraphStore(repo / ".code-review-graph" / "graph.db")
    try:
        targets = _match_files_to_forget(store.get_all_files(), ["app/outside"], repo)
        assert targets == [str(repo / "app" / "outside" / "secret.py")]
        forget_files(store, repo, targets)
        assert _files(store, repo) == ON - {"app/outside/secret.py"}
    finally:
        store.close()


def test_rename_applies_to_a_followed_file(repo: Path, outside: Path) -> None:
    from code_review_graph.tools.refactor_tools import apply_refactor_func, refactor_func

    _built_tool_repo(repo)
    preview = refactor_func(
        mode="rename", old_name="secret", new_name="hidden", repo_root=str(repo),
    )
    assert preview["status"] == "ok", preview
    applied = apply_refactor_func(preview["refactor_id"], repo_root=str(repo))
    assert applied["status"] == "ok", applied
    assert "def hidden" in (outside / "secret.py").read_text()


# -- watch ---------------------------------------------------------------


def test_watch_follows_a_new_link_and_drops_a_removed_one(
    repo: Path, tmp_path: Path,
) -> None:
    from watchdog.events import FileCreatedEvent, FileDeletedEvent

    link = repo / "app" / "shared"
    link.unlink()
    store = GraphStore(tmp_path / "graph.db")
    try:
        full_build(repo, store, follow_symlinks=True)
        assert _files(store, repo) == {"app/main.py", "app/outside/secret.py"}
        handler = _create_watch_handler(repo, store, None, follow_symlinks=True)

        link.symlink_to("../shared", target_is_directory=True)
        handler.process([FileCreatedEvent(str(link))])
        handler.raise_if_failed()
        assert _files(store, repo) == ON
        assert _files(store, repo) == _build(
            repo, tmp_path / "check1.db", follow_symlinks=True
        )[1]

        link.unlink()
        handler.process([FileDeletedEvent(str(link))])
        handler.raise_if_failed()
        assert _files(store, repo) == {"app/main.py", "app/outside/secret.py"}
    finally:
        store.close()


def test_watch_sees_edits_in_an_outside_folder(
    repo: Path, outside: Path, tmp_path: Path,
) -> None:
    from watchdog.events import FileCreatedEvent, FileModifiedEvent

    store = GraphStore(tmp_path / "graph.db")
    try:
        full_build(repo, store, follow_symlinks=True)
        handler = _create_watch_handler(repo, store, None, follow_symlinks=True)
        assert handler.outside_roots == {str(outside)}

        (outside / "secret.py").write_text("def changed():\n    pass\n")
        (outside / "fresh.py").write_text("def fresh():\n    pass\n")
        # The OS reports these under the real outside path.
        handler.process([
            FileModifiedEvent(str(outside / "secret.py")),
            FileCreatedEvent(str(outside / "fresh.py")),
        ])
        handler.raise_if_failed()
        names = {n.name for n in store.get_nodes_by_file(str(repo / "app/outside/secret.py"))}
        assert "changed" in names
        assert "app/outside/fresh.py" in _files(store, repo)
        assert _files(store, repo) == _build(
            repo, tmp_path / "check.db", follow_symlinks=True
        )[1]
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
        assert handler.outside_roots == set()
        link.symlink_to("../shared", target_is_directory=True)
        handler.process([FileCreatedEvent(str(link))])
        handler.raise_if_failed()
        assert _files(store, repo) == {"app/main.py"}
    finally:
        store.close()


def test_supervisor_keeps_one_watch_per_outside_folder(tmp_path: Path) -> None:
    observer = MagicMock()
    supervisor = _WatchSupervisor(observer, tmp_path, [])
    supervisor._handler = object()
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()

    supervisor.sync_extra_roots({str(first), str(second)})
    supervisor.sync_extra_roots({str(first), str(second)})
    assert observer.schedule.call_count == 2
    assert all(call.kwargs["recursive"] for call in observer.schedule.call_args_list)

    supervisor.sync_extra_roots({str(first)})
    assert observer.unschedule.call_count == 1


# -- the reporter's case (#269) ------------------------------------------


def test_project_folder_of_links_to_input_folders(tmp_path: Path) -> None:
    """A plain folder whose code is all links to folders kept elsewhere."""
    from code_review_graph.tools.build import build_or_update_graph
    from code_review_graph.tools.query import query_graph

    for name in ("projA", "projB"):
        folder = tmp_path / "inputs" / name
        folder.mkdir(parents=True)
        (folder / f"{name.lower()}.py").write_text(f"def {name.lower()}_main():\n    pass\n")
    project = tmp_path / "project"
    (project / ".code-review-graph").mkdir(parents=True)
    (project / "inputs").mkdir()
    for name in ("projA", "projB"):
        (project / "inputs" / name).symlink_to(
            tmp_path / "inputs" / name, target_is_directory=True,
        )
    project = project.resolve()

    off = build_or_update_graph(full_rebuild=True, repo_root=str(project), postprocess="none")
    assert off["files_parsed"] == 0
    on = build_or_update_graph(
        full_rebuild=True, repo_root=str(project), postprocess="minimal", follow_symlinks=True,
    )
    assert on["files_parsed"] == 2
    summary = query_graph("file_summary", "inputs/projA/proja.py", repo_root=str(project))
    assert summary["status"] == "ok"
    assert any(r["name"] == "proja_main" for r in summary["results"])


# -- CLI -----------------------------------------------------------------


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
    _built_tool_repo(repo)
    assert os.path.isfile(repo / ".code-review-graph" / "graph.db")
