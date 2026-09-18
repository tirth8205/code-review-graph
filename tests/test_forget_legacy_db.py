"""`forget` must still find a graph stored at the legacy top-level path."""

from __future__ import annotations

import sys

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build


def test_forget_migrates_legacy_database_instead_of_reporting_missing(
    tmp_path, monkeypatch, capsys
):
    from code_review_graph import cli

    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def a():\n    return 1\n")
    (repo / "b.py").write_text("def b():\n    return 2\n")
    legacy = repo / ".code-review-graph.db"
    with GraphStore(legacy) as store:
        full_build(repo, store)
    assert legacy.exists()

    monkeypatch.setattr(sys, "argv", ["code-review-graph", "forget", "--repo", str(repo), "a.py"])
    exit_code = 0
    try:
        cli.main()
    except SystemExit as exc:
        exit_code = exc.code or 0
    captured = capsys.readouterr()
    assert "No graph found" not in captured.out + captured.err
    assert exit_code == 0
    migrated = repo / ".code-review-graph" / "graph.db"
    assert migrated.exists()
    assert not legacy.exists()
    with GraphStore(migrated) as store:
        assert store.get_nodes_by_file(str(repo / "a.py")) == []
        assert store.get_nodes_by_file(str(repo / "b.py"))
