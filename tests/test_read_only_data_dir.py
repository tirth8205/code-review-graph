"""Edge-case regressions for the read-only DB path of status/detect-changes/
visualize/wiki/watch (#803, PR #809).

Covers resolution branches the PR's own tests leave untouched: registry
entries, deep CRG_DATA_DIR trees, legacy migration for the newly read-only
commands, CRG_DATA_DIR vs legacy interaction, relative and unicode
--data-dir paths, and registry side effects when a graph IS present.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from code_review_graph import cli
from code_review_graph.graph import GraphStore

READ_ONLY_COMMANDS = ["status", "detect-changes", "visualize", "wiki", "watch"]


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    """Redirect all per-user state into tmp_path and clear overrides."""
    crg_home = tmp_path / "crg-home"
    monkeypatch.setenv("CRG_HOME", str(crg_home))
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    monkeypatch.delenv("CRG_REPO_ROOT", raising=False)
    return crg_home


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


def _make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "gitrepo"
    repo.mkdir()
    subprocess.run(
        ["git", "-C", str(repo), "init", "-q"],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return repo


def _run_cli(argv: list[str]) -> pytest.ExceptionInfo:
    with patch.object(sys, "argv", ["code-review-graph", *argv]):
        with pytest.raises(SystemExit) as exc_info:
            cli.main()
    return exc_info


def _run_cli_ok(argv: list[str]) -> None:
    with patch.object(sys, "argv", ["code-review-graph", *argv]):
        cli.main()


def _build_min_graph(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = GraphStore(db_path)
    store.close()


@pytest.mark.parametrize("command", READ_ONLY_COMMANDS)
def test_registry_pointed_data_dir_is_not_created(
    command, tmp_path, isolated_env, capsys,
):
    """A registry entry naming a missing data dir must stay a no-op."""
    repo = _make_repo(tmp_path)
    if command == "detect-changes":
        repo = _make_git_repo(tmp_path)
    registry_dir = isolated_env
    registry_dir.mkdir(parents=True)
    pointed = tmp_path / "registry-pointed-data"
    registry_file = registry_dir / "registry.json"
    registry_file.write_text(
        '{"repos": [{"path": "%s", "alias": "", "data_dir": "%s"}]}'
        % (str(repo).replace("\\\\", "\\\\\\\\"), str(pointed).replace("\\\\", "\\\\\\\\")),
        encoding="utf-8",
    )
    before = registry_file.read_bytes()

    exc_info = _run_cli([command, "--repo", str(repo)])

    assert exc_info.value.code == 1
    assert "No graph found" in capsys.readouterr().err
    assert not pointed.exists()
    assert registry_file.read_bytes() == before
    assert not (repo / ".code-review-graph").exists()


@pytest.mark.parametrize("command", READ_ONLY_COMMANDS)
def test_deep_missing_crg_data_dir_tree_not_created(
    command, tmp_path, isolated_env, monkeypatch, capsys,
):
    """No level of a deeply nested CRG_DATA_DIR may be materialized."""
    repo = _make_repo(tmp_path)
    if command == "detect-changes":
        repo = _make_git_repo(tmp_path)
    deep = tmp_path / "a" / "b" / "c" / "d"
    monkeypatch.setenv("CRG_DATA_DIR", str(deep))

    exc_info = _run_cli([command, "--repo", str(repo)])

    assert exc_info.value.code == 1
    assert "No graph found" in capsys.readouterr().err
    assert not (tmp_path / "a").exists()


def test_legacy_migration_still_runs_for_wiki(tmp_path, isolated_env, capsys):
    """wiki on a legacy .code-review-graph.db migrates it instead of failing."""
    repo = _make_repo(tmp_path)
    legacy = repo / ".code-review-graph.db"
    _build_min_graph(legacy)

    _run_cli_ok(["wiki", "--repo", str(repo)])

    out = capsys.readouterr().out
    assert "Output:" in out
    assert not legacy.exists()
    assert (repo / ".code-review-graph" / "graph.db").exists()


def test_legacy_migration_still_runs_for_visualize_json(
    tmp_path, isolated_env, capsys,
):
    repo = _make_repo(tmp_path)
    legacy = repo / ".code-review-graph.db"
    _build_min_graph(legacy)

    _run_cli_ok(["visualize", "--repo", str(repo), "--format", "json"])

    assert "JSON exported" in capsys.readouterr().out
    assert not legacy.exists()
    assert (repo / ".code-review-graph" / "graph.db").exists()
    assert (repo / ".code-review-graph" / "graph.json").exists()


def test_crg_data_dir_blocks_legacy_migration(
    tmp_path, isolated_env, monkeypatch, capsys,
):
    """With CRG_DATA_DIR set, the legacy DB must be left alone and no dir made."""
    repo = _make_repo(tmp_path)
    legacy = repo / ".code-review-graph.db"
    _build_min_graph(legacy)
    external = tmp_path / "external-data"
    monkeypatch.setenv("CRG_DATA_DIR", str(external))

    exc_info = _run_cli(["visualize", "--repo", str(repo)])

    assert exc_info.value.code == 1
    assert "No graph found" in capsys.readouterr().err
    assert legacy.exists()
    assert not external.exists()


@pytest.mark.parametrize("command", ["status", "visualize", "wiki", "watch"])
def test_existing_empty_explicit_data_dir_gains_nothing(
    command, tmp_path, isolated_env, capsys,
):
    """--data-dir on an existing but graph-less dir: exit 1, dir stays empty."""
    repo = _make_repo(tmp_path)
    data_dir = tmp_path / "existing-empty"
    data_dir.mkdir()

    exc_info = _run_cli(
        [command, "--repo", str(repo), "--data-dir", str(data_dir)],
    )

    assert exc_info.value.code == 1
    assert "No graph found" in capsys.readouterr().err
    assert list(data_dir.iterdir()) == []
    assert not (isolated_env / "registry.json").exists()


def test_relative_explicit_data_dir_not_created(
    tmp_path, isolated_env, monkeypatch, capsys,
):
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    exc_info = _run_cli(
        ["status", "--repo", str(repo), "--data-dir", "rel-data"],
    )

    assert exc_info.value.code == 1
    assert "No graph found" in capsys.readouterr().err
    assert not (tmp_path / "rel-data").exists()


def test_visualize_with_graph_and_data_dir_writes_no_registry(
    tmp_path, isolated_env, capsys,
):
    """A present graph under --data-dir exports there without registry writes."""
    repo = _make_repo(tmp_path)
    data_dir = tmp_path / "présent data dir"
    _build_min_graph(data_dir / "graph.db")

    _run_cli_ok(
        ["visualize", "--repo", str(repo), "--format", "json",
         "--data-dir", str(data_dir)],
    )

    assert "JSON exported" in capsys.readouterr().out
    assert (data_dir / "graph.json").exists()
    assert not (isolated_env / "registry.json").exists()
    assert not (repo / ".code-review-graph").exists()


def test_wiki_with_graph_and_data_dir_writes_no_registry(
    tmp_path, isolated_env, capsys,
):
    repo = _make_repo(tmp_path)
    data_dir = tmp_path / "wiki-data"
    _build_min_graph(data_dir / "graph.db")

    _run_cli_ok(["wiki", "--repo", str(repo), "--data-dir", str(data_dir)])

    assert "Output:" in capsys.readouterr().out
    assert (data_dir / "wiki").is_dir()
    assert not (isolated_env / "registry.json").exists()
    assert not (repo / ".code-review-graph").exists()


def test_status_with_graph_and_data_dir_reads_in_place(
    tmp_path, isolated_env, capsys,
):
    repo = _make_repo(tmp_path)
    data_dir = tmp_path / "status-data"
    _build_min_graph(data_dir / "graph.db")

    _run_cli_ok(["status", "--repo", str(repo), "--data-dir", str(data_dir)])

    assert "Nodes: 0" in capsys.readouterr().out
    assert not (isolated_env / "registry.json").exists()
    assert not (repo / ".code-review-graph").exists()


def test_detect_changes_no_graph_real_git_repo_with_commit(
    tmp_path, isolated_env, capsys,
):
    """Even with real history, detect-changes must not materialize a graph."""
    repo = _make_git_repo(tmp_path)
    (repo / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "-A"],
        check=True, capture_output=True, timeout=30,
    )
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=t@example.com", "-c", "user.name=t",
            "commit", "-q", "-m", "init",
        ],
        check=True, capture_output=True, timeout=30,
    )

    exc_info = _run_cli(["detect-changes", "--repo", str(repo)])

    assert exc_info.value.code == 1
    assert "No graph found" in capsys.readouterr().err
    assert not (repo / ".code-review-graph").exists()
