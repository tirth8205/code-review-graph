"""Entry points that must reach the local graph after a registered dir is swept.

Issue #1057, PR #1080. ``tests/test_missing_registered_data_dir.py`` and
``tests/test_registered_data_dir_recovery.py`` pin ``get_data_dir`` itself,
CLI ``status``/``build``/``update``, one MCP tool and cross-repo search. This
module covers the remaining ways a user reaches the graph: the PreToolUse
enrichment hook (runs on every Read and Grep, and on the old resolution was
the call that silently recreated the swept directory), the
``get_minimal_context`` tool every agent calls first, the graph-query CLI
commands, ``visualize`` exports written next to the graph, and a registered
path that cannot be created even for a read-only lookup.
"""

from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from code_review_graph import cli
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_data_dir
from code_review_graph.registry import Registry
from code_review_graph.search import rebuild_fts_index

WARNING_TEXT = "Registered graph directory"

_GIT = [
    "git", "-c", "user.email=test@example.com", "-c", "user.name=test",
    "-c", "commit.gpgsign=false",
]


@pytest.fixture(autouse=True)
def _no_env_overrides(monkeypatch):
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    monkeypatch.delenv("CRG_REPO_ROOT", raising=False)


def _make_repo(base: Path) -> Path:
    repo = base / "repo"
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


@pytest.fixture
def swept_repo(tmp_path):
    """A repo with a built local graph whose registered data dir is gone."""
    repo = _make_repo(tmp_path)
    db = repo / ".code-review-graph" / "graph.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = GraphStore(db)
    try:
        full_build(repo, store)
        rebuild_fts_index(store)  # a real ``build`` indexes FTS in post-processing
    finally:
        store.close()
    swept = tmp_path / "swept" / "data"
    Registry().set_data_dir(str(repo), str(swept))
    return repo, db, swept


@pytest.mark.parametrize("tool", ["Grep", "Read"])
def test_pretooluse_hook_enriches_from_local_graph(swept_repo, monkeypatch, capsys, tool):
    """The hook must neither go silent nor recreate the swept directory.

    Recreating it is what made the old bug permanent: once the empty dir
    exists, the registry wins on every later command.
    """
    from code_review_graph.enrich import run_hook

    repo, _db, swept = swept_repo
    tool_input = (
        {"pattern": "helper"} if tool == "Grep"
        else {"file_path": str(repo / "app.py")}
    )
    payload = {"tool_name": tool, "tool_input": tool_input, "cwd": str(repo)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    run_hook()

    out = capsys.readouterr().out
    assert out, "hook produced no enrichment"
    context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "helper" in context
    assert not swept.exists()
    assert not swept.parent.exists()


def test_get_minimal_context_answers_from_local_graph(swept_repo):
    from code_review_graph.tools.context import get_minimal_context

    repo, _db, swept = swept_repo

    result = get_minimal_context(task="review", repo_root=str(repo))

    assert result.get("status") != "not_ready", result
    assert "missing_graph" not in json.dumps(result)
    assert not swept.exists()


def test_graph_query_cli_command_reads_local_graph(swept_repo, capsys):
    repo, _db, swept = swept_repo

    with patch.object(sys, "argv", ["code-review-graph", "search", "helper",
                                    "--repo", str(repo)]):
        cli.main()

    captured = capsys.readouterr()
    assert "No graph found" not in captured.err
    assert "helper" in captured.out
    assert not swept.exists()


def test_visualize_export_lands_next_to_local_graph(swept_repo, capsys):
    repo, db, swept = swept_repo

    with patch.object(sys, "argv", ["code-review-graph", "visualize", "--format", "json",
                                    "--repo", str(repo)]):
        cli.main()

    exported = db.parent / "graph.json"
    assert exported.is_file()
    assert "helper" in exported.read_text(encoding="utf-8")
    assert not swept.exists()


@pytest.mark.parametrize("create", [True, False])
def test_uncreatable_registration_with_local_graph_takes_the_new_branch(
    tmp_path, caplog, create,
):
    """Read-only lookups too, and through the warning, not the mkdir failure.

    ``create=True`` alone cannot tell the new branch from the old
    ``except Exception`` fallthrough (both end at the local dir), so this
    checks ``create=False`` and the warning that only the new branch logs.
    """
    repo = tmp_path / "repo"
    local = repo / ".code-review-graph"
    local.mkdir(parents=True)
    (local / "graph.db").write_bytes(b"existing graph")
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    Registry().set_data_dir(str(repo), str(blocker / "data"))

    with caplog.at_level(logging.WARNING):
        assert get_data_dir(repo, create=create) == local
    assert WARNING_TEXT in caplog.text
    assert blocker.is_file()
