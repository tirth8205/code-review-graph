"""Freshness metadata follows what was stored; one failed file must not freeze it.

A persistently unparseable file previously (a) left a full build without a Git
anchor, so every later update became a full rebuild, (b) pinned incremental
freshness at the old commit, and (c) stopped watch batches from recording HEAD,
so every query carried a stale-graph caveat.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path, incremental_update
from code_review_graph.tools.build import build_or_update_graph


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=T", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, name: str, body: str) -> str:
    (repo / name).write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"edit {name}")
    return _git(repo, "rev-parse", "HEAD")


def _fail_parsing(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    from code_review_graph.parser import CodeParser

    original = CodeParser.parse_bytes

    def flaky(self, path, source):
        if Path(path).name == name:
            raise RuntimeError("simulated grammar failure")
        return original(self, path, source)

    monkeypatch.setattr(CodeParser, "parse_bytes", flaky)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.py").write_text("def a():\n    return 1\n")
    (root / "b.py").write_text("def b():\n    return 2\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def test_full_build_with_one_failing_file_still_records_git_anchor(repo, monkeypatch):
    head = _commit(repo, "bad.py", "def bad():\n    pass\n")
    _fail_parsing(monkeypatch, "bad.py")
    with GraphStore(get_db_path(repo)) as store:
        result = full_build(repo, store)
        assert [e["file"] for e in result["errors"]] == ["bad.py"]
        assert store.get_metadata("git_head_sha") == head


def test_update_after_failed_file_stays_incremental(repo, monkeypatch):
    _commit(repo, "bad.py", "def bad():\n    pass\n")
    _fail_parsing(monkeypatch, "bad.py")
    build_or_update_graph(full_rebuild=True, repo_root=str(repo), postprocess="none")
    _commit(repo, "c.py", "def c():\n    return 3\n")
    result = build_or_update_graph(full_rebuild=False, repo_root=str(repo), postprocess="none")
    assert result["build_type"] == "incremental"
    assert result["changed_files"] == ["c.py"]


def test_watch_batch_after_commit_records_head(repo):
    with GraphStore(get_db_path(repo)) as store:
        full_build(repo, store)
        head = _commit(repo, "a.py", "def a():\n    return 11\n")
        result = incremental_update(repo, store, changed_files=["a.py"], reconcile_stale=False)
        assert result["files_updated"] == 1
        assert store.get_metadata("git_head_sha") == head


def test_watch_batch_that_stores_nothing_keeps_old_anchor(repo):
    with GraphStore(get_db_path(repo)) as store:
        full_build(repo, store)
        anchor = store.get_metadata("git_head_sha")
        _commit(repo, "notes.txt", "not source\n")
        result = incremental_update(repo, store, changed_files=["a.py"], reconcile_stale=False)
        assert result["files_updated"] == 0
        assert store.get_metadata("git_head_sha") == anchor


def test_failing_file_does_not_pin_freshness_for_stored_files(repo, monkeypatch):
    with GraphStore(get_db_path(repo)) as store:
        full_build(repo, store)
        anchor = store.get_metadata("git_head_sha")
        (repo / "bad.py").write_text("def bad():\n    pass\n")
        head = _commit(repo, "c.py", "def c():\n    return 3\n")
        _fail_parsing(monkeypatch, "bad.py")

        first = incremental_update(repo, store, base=anchor)
        assert first["files_updated"] == 1
        assert [e["file"] for e in first["errors"]] == ["bad.py"]
        assert first["freshness_advanced"] is True
        assert store.get_metadata("git_head_sha") == head
        assert store.get_nodes_by_file(str(repo / "c.py"))

        head2 = _commit(repo, "d.py", "def d():\n    return 4\n")
        second = incremental_update(repo, store, base=head)
        assert second["changed_files"] == ["d.py"]
        assert second["errors"] == []
        assert store.get_metadata("git_head_sha") == head2


def test_build_tool_reports_failed_files_and_still_postprocesses(repo, monkeypatch):
    build_or_update_graph(full_rebuild=True, repo_root=str(repo), postprocess="none")
    (repo / "bad.py").write_text("def bad():\n    pass\n")
    head = _commit(repo, "c.py", "def c():\n    return 3\n")
    _fail_parsing(monkeypatch, "bad.py")
    result = build_or_update_graph(full_rebuild=False, repo_root=str(repo), postprocess="minimal")
    assert result["build_type"] == "incremental"
    assert [e["file"] for e in result["errors"]] == ["bad.py"]
    assert "bad.py" in result["summary"]
    assert "up to date" not in result["summary"].lower()
    assert "bare_edges_resolved" in result
    with GraphStore(get_db_path(repo)) as store:
        assert store.get_metadata("git_head_sha") == head


def test_build_tool_with_only_a_failed_file_does_not_claim_up_to_date(repo, monkeypatch):
    build_or_update_graph(full_rebuild=True, repo_root=str(repo), postprocess="none")
    _commit(repo, "a.py", "def a():\n    return 11\n")
    _fail_parsing(monkeypatch, "a.py")
    result = build_or_update_graph(full_rebuild=False, repo_root=str(repo), postprocess="none")
    assert result["files_updated"] == 0
    assert [e["file"] for e in result["errors"]] == ["a.py"]
    assert "up to date" not in result["summary"].lower()
    assert "a.py" in result["summary"]


def test_cli_update_warns_about_failed_files(repo, monkeypatch, capsys):
    from code_review_graph import cli

    build_or_update_graph(full_rebuild=True, repo_root=str(repo), postprocess="none")
    (repo / "bad.py").write_text("def bad():\n    pass\n")
    _commit(repo, "c.py", "def c():\n    return 3\n")
    _fail_parsing(monkeypatch, "bad.py")
    monkeypatch.setattr(
        sys,
        "argv",
        ["code-review-graph", "update", "--repo", str(repo), "--skip-postprocess"],
    )
    cli.main()
    captured = capsys.readouterr()
    assert "Incremental: 1 files updated" in captured.out
    assert "bad.py" in captured.err
