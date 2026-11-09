"""Recovering the local graph when a registered data directory disappears.

Edge cases for issue #1057 (PR #1080) beyond the PR's own module,
``tests/test_missing_registered_data_dir.py``: the whole resolution chain
(``get_db_path``, CLI ``status``/``build``/``update``, an MCP tool and
cross-repo search), what counts as an existing local graph, relative,
symlinked and non-ASCII repository paths, repeated resolution, a registered
path that cannot be created, a database left at the repository root by early
releases, explicit relocation with ``--data-dir``, and the ``repos`` listing
across mixed registry entries.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from code_review_graph import cli
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_data_dir, get_db_path
from code_review_graph.registry import Registry, default_registry_path

WARNING_TEXT = "Registered graph directory"

_GIT = [
    "git", "-c", "user.email=test@example.com", "-c", "user.name=test",
    "-c", "commit.gpgsign=false",
]


@pytest.fixture(autouse=True)
def _no_env_overrides(monkeypatch):
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    monkeypatch.delenv("CRG_REPO_ROOT", raising=False)


def _make_repo(base: Path, name: str = "repo") -> Path:
    """A committed one-file Git repository."""
    repo = base / name
    repo.mkdir(parents=True)
    (repo / "app.py").write_text(
        "def helper(x):\n    return x + 1\n\n\ndef main():\n    return helper(2)\n",
        encoding="utf-8",
    )
    for args in (["init", "-q"], ["add", "app.py"], ["commit", "-qm", "init"]):
        subprocess.run(
            [*_GIT, "-C", str(repo), *args],
            check=True, capture_output=True, timeout=30,
        )
    return repo


def _build_local_graph(repo: Path) -> Path:
    db = repo / ".code-review-graph" / "graph.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = GraphStore(db)
    try:
        full_build(repo, store)
    finally:
        store.close()
    return db


def _node_count(db: Path) -> int:
    store = GraphStore(db)
    try:
        return store.get_stats().total_nodes
    finally:
        store.close()


def _register_swept_dir(repo: Path, swept: Path) -> bytes:
    """Register ``swept`` for ``repo`` and return the registry file bytes."""
    Registry().set_data_dir(str(repo), str(swept))
    return default_registry_path().read_bytes()


def _run_cli(argv: list[str]) -> None:
    with patch.object(sys, "argv", ["code-review-graph", *argv]):
        cli.main()


# --- what counts as an existing local graph ---------------------------------


@pytest.mark.parametrize("layout", ["empty_dir", "wiki_only", "graph_db_is_a_directory"])
def test_local_dir_without_a_graph_db_file_keeps_registered_dir(tmp_path, caplog, layout):
    """Only a real ``graph.db`` file opts out of the registered directory."""
    repo = tmp_path / "repo"
    local = repo / ".code-review-graph"
    local.mkdir(parents=True)
    (local / ".gitignore").write_text("*\n", encoding="utf-8")
    if layout == "wiki_only":
        (local / "wiki").mkdir()
        (local / "wiki" / "index.md").write_text("# wiki\n", encoding="utf-8")
    elif layout == "graph_db_is_a_directory":
        (local / "graph.db").mkdir()
    registered = tmp_path / "fresh" / "data"
    Registry().set_data_dir(str(repo), str(registered))

    with caplog.at_level(logging.WARNING):
        assert get_data_dir(repo) == registered.resolve()
    assert registered.is_dir()
    assert WARNING_TEXT not in caplog.text


# --- the whole resolution chain ---------------------------------------------


@pytest.mark.parametrize("read_only", [True, False])
def test_get_db_path_resolves_to_local_graph_without_side_effects(tmp_path, read_only):
    repo = _make_repo(tmp_path)
    local_db = _build_local_graph(repo)
    graph_bytes = local_db.read_bytes()
    swept = tmp_path / "swept" / "data"
    registry_bytes = _register_swept_dir(repo, swept)

    assert get_db_path(repo, read_only=read_only) == local_db
    assert not swept.exists()
    assert not swept.parent.exists()
    assert local_db.read_bytes() == graph_bytes
    assert default_registry_path().read_bytes() == registry_bytes


def test_repeated_resolution_is_stable_and_never_rewrites_registry(tmp_path, caplog):
    repo = tmp_path / "repo"
    local = repo / ".code-review-graph"
    local.mkdir(parents=True)
    (local / "graph.db").write_bytes(b"existing graph")
    swept = tmp_path / "swept" / "data"
    registry_bytes = _register_swept_dir(repo, swept)

    with caplog.at_level(logging.WARNING):
        results = [get_data_dir(repo, create=create) for create in (True, False, True)]

    assert results == [local, local, local]
    assert not swept.exists()
    assert caplog.text.count(WARNING_TEXT) == 3
    # The registration is reported, not repaired: the entry survives so the
    # user can see it in ``repos`` and decide what to do with it.
    assert default_registry_path().read_bytes() == registry_bytes
    assert Registry().get_data_dir_for_repo(str(repo)) == str(swept.resolve())


def test_registered_dir_regains_priority_once_it_exists_again(tmp_path):
    repo = tmp_path / "repo"
    local = repo / ".code-review-graph"
    local.mkdir(parents=True)
    (local / "graph.db").write_bytes(b"existing graph")
    swept = tmp_path / "swept" / "data"
    Registry().set_data_dir(str(repo), str(swept))

    assert get_data_dir(repo) == local
    swept.mkdir(parents=True)
    assert get_data_dir(repo) == swept.resolve()


def test_no_warning_when_registration_is_usable_or_absent(tmp_path, caplog):
    repo = tmp_path / "repo"
    local = repo / ".code-review-graph"
    local.mkdir(parents=True)
    (local / "graph.db").write_bytes(b"existing graph")
    other = tmp_path / "other"
    (other / ".code-review-graph").mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    Registry().set_data_dir(str(repo), str(external))

    with caplog.at_level(logging.WARNING):
        assert get_data_dir(repo) == external.resolve()
        assert get_data_dir(other) == other / ".code-review-graph"
    assert WARNING_TEXT not in caplog.text


def test_explicit_environment_does_not_override_a_fresh_relocation(tmp_path, monkeypatch):
    """With no local graph the registry still wins over CRG_DATA_DIR."""
    repo = tmp_path / "repo"
    repo.mkdir()
    registered = tmp_path / "fresh"
    Registry().set_data_dir(str(repo), str(registered))
    monkeypatch.setenv("CRG_DATA_DIR", str(tmp_path / "env"))

    assert get_data_dir(repo) == registered.resolve()
    assert registered.is_dir()
    assert not (tmp_path / "env").exists()


# --- repository path shapes -------------------------------------------------


def test_relative_repo_root_falls_back_to_local_graph(tmp_path, monkeypatch):
    """``--repo .`` reaches get_data_dir as the relative path ``.``."""
    repo = tmp_path / "repo"
    local = repo / ".code-review-graph"
    local.mkdir(parents=True)
    (local / "graph.db").write_bytes(b"existing graph")
    swept = tmp_path / "swept"
    Registry().set_data_dir(str(repo), str(swept))
    monkeypatch.chdir(repo)

    assert get_data_dir(Path(".")).resolve() == local.resolve()
    assert get_db_path(Path(".")).resolve() == (local / "graph.db").resolve()
    assert not swept.exists()


def test_unicode_and_spaces_in_repo_and_registered_paths(tmp_path, caplog):
    repo = tmp_path / "répo 日本 space"
    local = repo / ".code-review-graph"
    local.mkdir(parents=True)
    (local / "graph.db").write_bytes(b"existing graph")
    swept = tmp_path / "swept tmp" / "dätä"
    Registry().set_data_dir(str(repo), str(swept))

    with caplog.at_level(logging.WARNING):
        assert get_data_dir(repo) == local
    assert not swept.exists()
    assert str(swept.resolve()) in caplog.text
    assert str(local) in caplog.text


def test_symlinked_repo_root_falls_back_to_local_graph(tmp_path):
    repo = tmp_path / "repo"
    local = repo / ".code-review-graph"
    local.mkdir(parents=True)
    (local / "graph.db").write_bytes(b"existing graph")
    link = tmp_path / "link"
    try:
        link.symlink_to(repo, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    swept = tmp_path / "swept"
    Registry().set_data_dir(str(repo), str(swept))

    assert get_data_dir(link).resolve() == local.resolve()
    assert not swept.exists()


# --- error paths ------------------------------------------------------------


def test_uncreatable_registered_dir_without_local_graph_falls_through(tmp_path):
    """Unchanged by #1080: a registration that cannot be created is a failed
    registry lookup, which falls through to normal resolution."""
    repo = tmp_path / "repo"
    repo.mkdir()
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    Registry().set_data_dir(str(repo), str(blocker / "data"))

    assert get_data_dir(repo) == repo / ".code-review-graph"
    assert (repo / ".code-review-graph").is_dir()
    assert blocker.is_file()


# --- graphs written by earlier releases -------------------------------------


def test_root_level_legacy_database_is_not_lost(tmp_path):
    """A pre-directory ``.code-review-graph.db`` ends up where resolution points."""
    repo = tmp_path / "repo"
    repo.mkdir()
    legacy = repo / ".code-review-graph.db"
    legacy.write_bytes(b"legacy graph")
    swept = tmp_path / "swept" / "data"
    Registry().set_data_dir(str(repo), str(swept))

    db_path = get_db_path(repo)

    assert db_path.read_bytes() == b"legacy graph"
    assert not legacy.exists()


# --- real user paths: CLI, MCP tool, cross-repo search -----------------------


def test_cli_status_reads_local_graph(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    _build_local_graph(repo)
    swept = tmp_path / "swept" / "data"
    _register_swept_dir(repo, swept)

    _run_cli(["status", "--repo", str(repo)])

    captured = capsys.readouterr()
    assert "Nodes:" in captured.out
    assert "No graph found" not in captured.err
    assert not swept.exists()


@pytest.mark.parametrize("command", ["build", "update"])
def test_cli_writes_go_to_local_graph_not_recreated_dir(tmp_path, capsys, command):
    repo = _make_repo(tmp_path)
    local_db = _build_local_graph(repo)
    (repo / "extra.py").write_text("def added():\n    return 1\n", encoding="utf-8")
    subprocess.run(
        [*_GIT, "-C", str(repo), "add", "extra.py"],
        check=True, capture_output=True, timeout=30,
    )
    subprocess.run(
        [*_GIT, "-C", str(repo), "commit", "-qm", "extra"],
        check=True, capture_output=True, timeout=30,
    )
    before = _node_count(local_db)
    swept = tmp_path / "swept" / "data"
    _register_swept_dir(repo, swept)

    _run_cli([command, "--repo", str(repo)])

    assert not swept.exists()
    assert _node_count(local_db) > before


def test_explicit_data_dir_still_relocates_a_repo_with_a_local_graph(tmp_path, capsys):
    """``build --data-dir NEW`` creates NEW first, so the registration wins."""
    repo = _make_repo(tmp_path)
    local_db = _build_local_graph(repo)
    local_bytes = local_db.read_bytes()
    new = tmp_path / "new home"

    _run_cli(["build", "--repo", str(repo), "--data-dir", str(new)])

    assert (new / "graph.db").is_file()
    assert _node_count(new / "graph.db") > 0
    assert Registry().get_data_dir_for_repo(str(repo)) == str(new.resolve())
    assert get_data_dir(repo) == new.resolve()
    assert local_db.read_bytes() == local_bytes


def test_mcp_tool_answers_from_local_graph(tmp_path):
    from code_review_graph.tools.query import list_graph_stats

    repo = _make_repo(tmp_path)
    local_db = _build_local_graph(repo)
    swept = tmp_path / "swept" / "data"
    _register_swept_dir(repo, swept)

    result = list_graph_stats(repo_root=str(repo))

    assert result["status"] == "ok"
    assert result["total_nodes"] == _node_count(local_db) > 0
    assert not swept.exists()


def test_cross_repo_search_reaches_local_graph_of_swept_registration(tmp_path):
    from code_review_graph.tools.registry_tools import cross_repo_search_func

    repo = _make_repo(tmp_path)
    _build_local_graph(repo)
    swept = tmp_path / "swept" / "data"
    Registry().register(str(repo), alias="swept-repo")
    _register_swept_dir(repo, swept)

    result = cross_repo_search_func(query="helper")

    assert result["status"] == "ok"
    assert result["repos_searched"] == ["swept-repo"]
    assert any(item["name"] == "helper" for item in result["results"])
    assert not swept.exists()


# --- the repos listing ------------------------------------------------------


def test_repos_marks_only_the_missing_entry(tmp_path, capsys):
    plain = tmp_path / "plain"
    (plain / ".git").mkdir(parents=True)
    present = tmp_path / "present"
    present.mkdir()
    missing = tmp_path / "missing"
    missing.mkdir()
    present_dir = tmp_path / "present data"
    present_dir.mkdir()
    missing_dir = tmp_path / "gone data"
    registry = Registry()
    registry.register(str(plain), alias="plainalias")
    registry.set_data_dir(str(present), str(present_dir))
    registry.set_data_dir(str(missing), str(missing_dir))

    _run_cli(["repos"])

    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        f"  {plain.resolve()}  (plainalias)",
        f"  {present.resolve()}",
        f"    data_dir: {present_dir.resolve()}",
        f"  {missing.resolve()}",
        f"    data_dir: {missing_dir.resolve()} [MISSING]",
    ]
    assert not missing_dir.exists()


def test_repos_reads_registry_written_without_data_dir_keys(tmp_path, capsys):
    """A registry file from a release before --data-dir lists unchanged."""
    repo = tmp_path / "repo"
    repo.mkdir()
    path = default_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"repos": [{"path": str(repo), "alias": "old"}]}),
        encoding="utf-8",
    )

    _run_cli(["repos"])

    out = capsys.readouterr().out
    assert out.splitlines() == [f"  {repo}  (old)"]
    assert "data_dir" not in out
