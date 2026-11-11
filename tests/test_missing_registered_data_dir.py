"""Missing registered graph directory recovery, issue #1057."""

import pytest

from code_review_graph.incremental import get_data_dir
from code_review_graph.registry import Registry


@pytest.mark.parametrize("create", [True, False])
def test_missing_registered_dir_uses_existing_local_graph(tmp_path, monkeypatch, caplog, create):
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    repo = tmp_path / "repo"
    local = repo / ".code-review-graph"
    local.mkdir(parents=True)
    (local / "graph.db").write_bytes(b"existing graph")
    missing = tmp_path / "swept" / "data"
    Registry().set_data_dir(str(repo), str(missing))
    assert get_data_dir(repo, create=create) == local
    assert not missing.exists()
    assert str(missing) in caplog.text
    assert str(local) in caplog.text


@pytest.mark.parametrize("create", [True, False])
def test_fresh_relocation_without_local_graph_is_respected(tmp_path, monkeypatch, create):
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "external"
    Registry().set_data_dir(str(repo), str(external))
    assert get_data_dir(repo, create=create) == external
    assert external.exists() == create


def test_existing_registered_directory_keeps_priority(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    local = repo / ".code-review-graph"
    local.mkdir(parents=True)
    (local / "graph.db").touch()
    external = tmp_path / "external"
    external.mkdir()
    Registry().set_data_dir(str(repo), str(external))
    monkeypatch.setenv("CRG_DATA_DIR", str(tmp_path / "env"))
    assert get_data_dir(repo) == external


def test_missing_registry_falls_through_to_explicit_environment(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    local = repo / ".code-review-graph"
    local.mkdir(parents=True)
    (local / "graph.db").touch()
    Registry().set_data_dir(str(repo), str(tmp_path / "missing"))
    override = tmp_path / "env"
    monkeypatch.setenv("CRG_DATA_DIR", str(override))
    assert get_data_dir(repo) == override


@pytest.mark.parametrize("exists", [True, False])
def test_repos_displays_registered_data_directory(tmp_path, monkeypatch, capsys, exists):
    import sys

    from code_review_graph.cli import main

    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "external"
    if exists:
        external.mkdir()
    Registry().set_data_dir(str(repo), str(external))
    monkeypatch.setattr(sys, "argv", ["code-review-graph", "repos"])
    main()
    output = capsys.readouterr().out
    assert f"data_dir: {external}" in output
    assert ("[MISSING]" in output) == (not exists)
