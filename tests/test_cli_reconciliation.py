"""Current-main regressions for the reconciled CLI contribution stack."""

from __future__ import annotations

import io
import json
import logging
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import code_review_graph.graph  # noqa: F401 - imported so unittest.mock can patch it
from code_review_graph import cli


@pytest.mark.parametrize(
    ("command", "result"),
    [
        (
            "build",
            {"files_parsed": 1, "total_nodes": 2, "total_edges": 1},
        ),
        (
            "update",
            {"files_updated": 1, "total_nodes": 2, "total_edges": 1},
        ),
    ],
)
def test_quiet_build_and_update_suppress_summary_and_info_logs(
    command, result, capsys, caplog,
):
    """``--quiet`` must silence progress logs as well as the final summary."""

    def _run_with_progress(**_kwargs):
        logging.getLogger("code_review_graph.test_progress").info("parsing progress")
        return result

    argv = ["code-review-graph", command, "--repo", "repo-root", "--quiet"]
    with caplog.at_level(logging.INFO):
        with patch.object(sys, "argv", argv):
            with patch("code_review_graph.graph.GraphStore", return_value=MagicMock()):
                with patch(
                    "code_review_graph.incremental.get_db_path",
                    return_value=MagicMock(),
                ):
                    with patch(
                        "code_review_graph.tools.build.build_or_update_graph",
                        side_effect=_run_with_progress,
                    ):
                        cli.main()

    assert capsys.readouterr().out == ""
    assert "parsing progress" not in caplog.text


def test_status_json_is_the_only_stdout_and_includes_current_sha(capsys):
    store = MagicMock()
    store.get_stats.return_value = SimpleNamespace(
        total_nodes=3,
        total_edges=4,
        files_count=2,
        languages=["Python"],
        last_updated="2026-07-17T12:00:00Z",
    )
    store.get_metadata.side_effect = {
        "git_branch": "main",
        "git_head_sha": "old-sha",
        "svn_revision": None,
        "svn_branch": None,
    }.get
    argv = ["code-review-graph", "status", "--repo", "repo-root", "--json"]

    with patch.object(sys, "argv", argv):
        with patch("code_review_graph.graph.GraphStore", return_value=store):
            with patch(
                "code_review_graph.incremental.get_db_path",
                return_value=MagicMock(),
            ):
                with patch(
                    "code_review_graph.incremental.detect_vcs",
                    return_value="git",
                ):
                    with patch(
                        "code_review_graph.incremental._git_branch_info",
                        return_value=("feature", "current-sha"),
                    ):
                        cli.main()

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload == {
        "nodes": 3,
        "edges": 4,
        "files": 2,
        "languages": ["Python"],
        "last_updated": "2026-07-17T12:00:00Z",
        "vcs": "git",
        "built_on_branch": "main",
        "built_at_commit": "old-sha",
        "current_branch": "feature",
        "current_sha": "current-sha",
        "svn_branch": None,
        "svn_revision": None,
    }
    assert output.count("\n") == 1


def test_status_quiet_prints_nothing(capsys):
    store = MagicMock()
    store.get_stats.return_value = SimpleNamespace(
        total_nodes=0,
        total_edges=0,
        files_count=0,
        languages=[],
        last_updated=None,
    )
    store.get_metadata.return_value = None
    argv = ["code-review-graph", "status", "--repo", "repo-root", "--quiet"]

    with patch.object(sys, "argv", argv):
        with patch("code_review_graph.graph.GraphStore", return_value=store):
            with patch(
                "code_review_graph.incremental.get_db_path",
                return_value=MagicMock(),
            ):
                with patch("code_review_graph.incremental.detect_vcs", return_value="none"):
                    cli.main()

    assert capsys.readouterr().out == ""


def test_status_missing_graph_exits_without_creating_data_tree(
    tmp_path, monkeypatch, capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    data_dir = tmp_path / "missing-data"
    monkeypatch.setenv("CRG_DATA_DIR", str(data_dir))
    argv = ["code-review-graph", "status", "--repo", str(repo)]

    with patch.object(sys, "argv", argv):
        with pytest.raises(SystemExit) as exc_info:
            cli.main()

    assert exc_info.value.code == 1
    assert "No graph found" in capsys.readouterr().err
    assert not data_dir.exists()


@pytest.mark.parametrize(
    "command",
    ["status", "detect-changes", "visualize", "wiki", "watch"],
)
def test_read_only_commands_missing_graph_do_not_create_empty_db(
    command, tmp_path, monkeypatch, capsys,
):
    """#803: read-only consumers must not poison a repo with empty graph.db."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    # detect-changes requires a real git worktree for root discovery.
    if command == "detect-changes":
        import subprocess

        subprocess.run(
            ["git", "-C", str(repo), "init", "-q"],
            check=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )
    data_dir = tmp_path / "missing-data"
    monkeypatch.setenv("CRG_DATA_DIR", str(data_dir))
    monkeypatch.delenv("CRG_HOME", raising=False)
    argv = ["code-review-graph", command, "--repo", str(repo)]

    with patch(
        "code_review_graph.registry.default_registry_path",
        return_value=tmp_path / "missing-registry.json",
    ):
        with patch.object(sys, "argv", argv):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "No graph found" in err
    assert not data_dir.exists()
    assert not (repo / ".code-review-graph").exists()


@pytest.mark.parametrize("command", ["visualize", "wiki", "watch", "status"])
def test_read_only_commands_data_dir_option_is_read_only(
    command, tmp_path, monkeypatch, capsys,
):
    """Explicit --data-dir must not create dirs/registry when the graph is missing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    data_dir = tmp_path / "explicit-data"
    registry_path = tmp_path / "registry" / "registry.json"
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    argv = [
        "code-review-graph",
        command,
        "--repo",
        str(repo),
        "--data-dir",
        str(data_dir),
    ]

    with patch(
        "code_review_graph.registry.default_registry_path",
        return_value=registry_path,
    ):
        with patch.object(sys, "argv", argv):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()

    assert exc_info.value.code == 1
    assert "No graph found" in capsys.readouterr().err
    assert not data_dir.exists()
    assert not registry_path.exists()


def test_status_preserves_legacy_graph_migration(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    legacy_db = repo / ".code-review-graph.db"
    with code_review_graph.graph.GraphStore(legacy_db):
        pass
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    argv = ["code-review-graph", "status", "--repo", str(repo)]

    with patch(
        "code_review_graph.registry.default_registry_path",
        return_value=tmp_path / "missing-registry.json",
    ):
        with patch.object(sys, "argv", argv):
            cli.main()

    assert "Nodes: 0" in capsys.readouterr().out
    assert not legacy_db.exists()
    assert (repo / ".code-review-graph" / "graph.db").exists()


def test_status_external_data_dir_does_not_migrate_unrelated_legacy_graph(
    tmp_path, monkeypatch, capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    legacy_db = repo / ".code-review-graph.db"
    with code_review_graph.graph.GraphStore(legacy_db):
        pass
    data_dir = tmp_path / "external-data"
    monkeypatch.setenv("CRG_DATA_DIR", str(data_dir))
    argv = ["code-review-graph", "status", "--repo", str(repo)]

    with patch.object(sys, "argv", argv):
        with pytest.raises(SystemExit) as exc_info:
            cli.main()

    assert exc_info.value.code == 1
    assert "No graph found" in capsys.readouterr().err
    assert legacy_db.exists()
    assert not data_dir.exists()


def test_status_data_dir_option_is_read_only(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    data_dir = tmp_path / "explicit-data"
    registry_path = tmp_path / "registry" / "registry.json"
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    argv = [
        "code-review-graph",
        "status",
        "--repo",
        str(repo),
        "--data-dir",
        str(data_dir),
    ]

    with patch(
        "code_review_graph.registry.default_registry_path",
        return_value=registry_path,
    ):
        with patch.object(sys, "argv", argv):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()

    assert exc_info.value.code == 1
    assert "No graph found" in capsys.readouterr().err
    assert not data_dir.exists()
    assert not registry_path.exists()

    with code_review_graph.graph.GraphStore(data_dir / "graph.db"):
        pass
    with patch(
        "code_review_graph.registry.default_registry_path",
        return_value=registry_path,
    ):
        with patch.object(sys, "argv", argv):
            cli.main()

    assert "Nodes: 0" in capsys.readouterr().out
    assert not registry_path.exists()


def test_status_default_data_dir_override_does_not_migrate_legacy_graph(
    tmp_path, monkeypatch, capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    legacy_db = repo / ".code-review-graph.db"
    with code_review_graph.graph.GraphStore(legacy_db):
        pass
    data_dir = repo / ".code-review-graph"
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    argv = [
        "code-review-graph",
        "status",
        "--repo",
        str(repo),
        "--data-dir",
        str(data_dir),
    ]

    with patch.object(sys, "argv", argv):
        with pytest.raises(SystemExit) as exc_info:
            cli.main()

    assert exc_info.value.code == 1
    assert "No graph found" in capsys.readouterr().err
    assert legacy_db.exists()
    assert not data_dir.exists()


def test_enrich_command_reads_stdin_and_respects_external_data_dir(
    tmp_path, monkeypatch, capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    data_dir = tmp_path / "external-data"
    data_dir.mkdir()
    (data_dir / "graph.db").touch()
    monkeypatch.setenv("CRG_DATA_DIR", str(data_dir))
    hook_input = {
        "tool_name": "Grep",
        "tool_input": {"pattern": "target_name"},
        "cwd": str(repo),
    }
    argv = ["code-review-graph", "enrich"]

    with patch.object(sys, "argv", argv):
        with patch.object(sys, "stdin", io.StringIO(json.dumps(hook_input))):
            with patch(
                "code_review_graph.enrich.enrich_search",
                return_value="graph context",
            ) as enrich_search:
                cli.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["hookSpecificOutput"]["additionalContext"] == "graph context"
    enrich_search.assert_called_once_with("target_name", str(repo))


@pytest.mark.parametrize("stdin", ["", "{not-json"])
def test_enrich_command_fails_open_for_invalid_stdin(stdin, capsys):
    argv = ["code-review-graph", "enrich"]
    with patch.object(sys, "argv", argv):
        with patch.object(sys, "stdin", io.StringIO(stdin)):
            cli.main()
    assert capsys.readouterr().out == ""


def _dead_items():
    return [
        {
            "name": name,
            "qualified_name": f"src/app.py::{name}",
            "kind": "Function",
            "file": "src/app.py",
            "file_path": "src/app.py",
            "relative_path": "src/app.py",
            "line": line,
            "language": "python",
        }
        for line, name in enumerate(("one", "two", "three"), start=1)
    ]


def test_dead_code_uses_project_root_external_data_and_reports_total(
    tmp_path, monkeypatch, capsys,
):
    repo = tmp_path / "repo"
    subdir = repo / "src" / "nested"
    subdir.mkdir(parents=True)
    (repo / ".git").mkdir()
    data_dir = tmp_path / "external-data"
    data_dir.mkdir()
    db_path = data_dir / "graph.db"
    db_path.touch()
    monkeypatch.setenv("CRG_DATA_DIR", str(data_dir))
    store = MagicMock()
    argv = [
        "code-review-graph",
        "dead-code",
        "--repo",
        str(subdir),
        "--limit",
        "2",
    ]

    with patch.object(sys, "argv", argv):
        with patch("code_review_graph.graph.GraphStore", return_value=store) as graph_store:
            with patch(
                "code_review_graph.refactor.find_dead_code",
                return_value=_dead_items(),
            ) as find_dead:
                cli.main()

    output = capsys.readouterr().out
    graph_store.assert_called_once_with(db_path)
    find_dead.assert_called_once_with(store, kind=None, file_pattern=None, root=repo)
    assert "Dead code: 3 item(s); showing 2" in output
    assert "one" in output and "two" in output and "three" not in output


def test_dead_code_json_limit_is_machine_readable(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "graph.db").touch()
    monkeypatch.setenv("CRG_DATA_DIR", str(data_dir))
    argv = [
        "code-review-graph",
        "dead-code",
        "--repo",
        str(repo),
        "--json",
        "--limit",
        "1",
    ]

    with patch.object(sys, "argv", argv):
        with patch("code_review_graph.graph.GraphStore", return_value=MagicMock()):
            with patch(
                "code_review_graph.refactor.find_dead_code",
                return_value=_dead_items(),
            ):
                cli.main()

    assert json.loads(capsys.readouterr().out) == _dead_items()[:1]


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--kind", "Module"],
        ["--limit", "-1"],
    ],
)
def test_dead_code_rejects_invalid_filters(extra_args):
    argv = ["code-review-graph", "dead-code", *extra_args]
    with patch.object(sys, "argv", argv):
        with pytest.raises(SystemExit) as exc_info:
            cli.main()
    assert exc_info.value.code == 2


def test_dead_code_missing_graph_exits_nonzero(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setenv("CRG_DATA_DIR", str(tmp_path / "missing-data"))
    argv = ["code-review-graph", "dead-code", "--repo", str(repo)]

    with patch.object(sys, "argv", argv):
        with pytest.raises(SystemExit) as exc_info:
            cli.main()

    assert exc_info.value.code == 1
    assert "No graph found" in capsys.readouterr().err
