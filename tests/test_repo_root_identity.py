"""Repository-root identity regressions for graph reconciliation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

import pytest

from code_review_graph import cli
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update


def test_incremental_update_survives_mixed_repo_root_spellings(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "app.py").write_text("def main() -> None:\n    pass\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    store = GraphStore(repo / ".code-review-graph" / "graph.db")
    try:
        full_build(repo.resolve(), store)
        before = store.get_all_files()

        result = incremental_update(Path("."), store, changed_files=[])

        assert result["stale_files_removed"] == 0
        assert store.get_all_files() == before
    finally:
        store.close()


def test_incremental_update_refuses_total_root_mismatch_without_purging(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "app.py").write_text("def main() -> None:\n    pass\n", encoding="utf-8")
    wrong_root = tmp_path / "wrong-root"
    wrong_root.mkdir()
    store = GraphStore(repo / ".code-review-graph" / "graph.db")
    try:
        full_build(repo, store)
        before = store.get_all_files()

        with pytest.raises(RuntimeError, match="different repository root"):
            incremental_update(wrong_root, store, changed_files=[])

        assert store.get_all_files() == before
    finally:
        store.close()


@pytest.mark.parametrize(
    "command",
    sorted(cli._PATH_REPO_COMMANDS),
)
def test_path_repo_commands_use_one_absolute_root_spelling(
    command: str, tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    args = SimpleNamespace(command=command, repo=".")

    cli._canonicalize_repo_argument(args)

    assert args.repo == str(repo.resolve())


@pytest.mark.parametrize("command", ["eval", "daemon"])
def test_name_valued_repo_arguments_are_not_treated_as_paths(
    command: str,
) -> None:
    args = SimpleNamespace(command=command, repo="repo-config-name")

    cli._canonicalize_repo_argument(args)

    assert args.repo == "repo-config-name"


@pytest.mark.parametrize("command", ["build", "update"])
def test_build_and_update_cli_pass_a_canonical_root(
    command: str, tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.chdir(repo)
    result = {
        "files_parsed": 1,
        "files_updated": 0,
        "total_nodes": 2,
        "total_edges": 1,
        "errors": [],
    }

    with patch.object(cli.sys, "argv", ["code-review-graph", command, "--repo", ".", "--quiet"]):
        with patch("code_review_graph.graph.GraphStore", return_value=MagicMock()):
            with patch(
                "code_review_graph.incremental.get_db_path",
                return_value=MagicMock(),
            ):
                with patch(
                    "code_review_graph.tools.build.build_or_update_graph",
                    return_value=result,
                ) as build_or_update:
                    cli.main()

    assert build_or_update.call_args.kwargs["repo_root"] == str(repo.resolve())


def test_watch_cli_passes_a_canonical_root(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.chdir(repo)
    store = MagicMock()

    with patch.object(cli.sys, "argv", ["code-review-graph", "watch", "--repo", "."]):
        with patch("code_review_graph.graph.GraphStore", return_value=store):
            with patch(
                "code_review_graph.incremental.get_db_path",
                return_value=MagicMock(),
            ):
                with patch("code_review_graph.incremental.watch") as watch:
                    cli.main()

    watch.assert_called_once_with(repo.resolve(), store, on_files_updated=ANY)
