"""The Codex fallback only forwards supported, read-only CLI commands."""

import argparse
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from code_review_graph import cli
from code_review_graph.graph import GraphStore
from code_review_graph.parser import NodeInfo
from code_review_graph.search import rebuild_fts_index
from code_review_graph.tools.query import _QUERY_PATTERNS

PROJECT_ROOT = Path(__file__).parents[1]
WRAPPER = (
    PROJECT_ROOT
    / "code_review_graph/assets/code-review-graph/scripts/crg_readonly.py"
)


_NOT_IN_CLI = "the CLI query command does not accept it, and the wrapper only runs the CLI"

#: Query patterns the MCP tool has but the wrapper leaves out, each with why.
#: A new pattern in tools/query.py must go in the wrapper or here.
LEFT_OUT = {
    "references_to": _NOT_IN_CLI,
    "triggers_of": _NOT_IN_CLI,
    "triggered_by": _NOT_IN_CLI,
    "publishers_of": _NOT_IN_CLI,
    "listeners_of": _NOT_IN_CLI,
    "advises": _NOT_IN_CLI,
    "advised_by": _NOT_IN_CLI,
    "handlers_of": _NOT_IN_CLI,
    "endpoints_for": _NOT_IN_CLI,
    "consumers_of": _NOT_IN_CLI,
}


def _load_wrapper():
    spec = importlib.util.spec_from_file_location("crg_readonly", WRAPPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _query_subparser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices["query"]
    raise AssertionError("no query subcommand")


def _pattern_choices(query: argparse.ArgumentParser) -> set[str]:
    for action in query._actions:
        if action.dest == "pattern":
            return set(action.choices)
    raise AssertionError("query has no pattern argument")


def _options(query: argparse.ArgumentParser) -> set[str]:
    return {
        option
        for action in query._actions
        for option in action.option_strings
        if option not in ("-h", "--help")
    }


def _capture_cli(tmp_path: Path) -> tuple[Path, Path]:
    captured = tmp_path / "argv.json"
    fake_cli = tmp_path / "fake crg"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['CAPTURE_ARGV'], 'w') as out:\n"
        "    json.dump(sys.argv[1:], out)\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    return fake_cli, captured


def test_wrapper_query_patterns_match_the_tool_and_the_cli():
    """The wrapper's list must not drift from tools/query.py or the CLI again."""
    wrapper = _load_wrapper()
    offered = set(wrapper.QUERY_PATTERNS)
    tool = set(_QUERY_PATTERNS)
    cli_query = _query_subparser(cli.build_parser())
    accepted = _pattern_choices(cli_query)

    assert len(offered) == len(wrapper.QUERY_PATTERNS), "a pattern is listed twice"
    assert offered <= tool, f"not tool patterns: {sorted(offered - tool)}"
    assert offered <= accepted, f"the CLI rejects: {sorted(offered - accepted)}"
    assert not offered & set(LEFT_OUT), f"offered and left out: {sorted(offered & set(LEFT_OUT))}"
    assert tool - offered == set(LEFT_OUT), (
        "add each new pattern to QUERY_PATTERNS in crg_readonly.py, or to LEFT_OUT "
        f"with a reason. Missing from both: {sorted(tool - offered - set(LEFT_OUT))}. "
        f"Stale in LEFT_OUT: {sorted(set(LEFT_OUT) - tool)}"
    )
    now_in_cli = {
        pattern
        for pattern, reason in LEFT_OUT.items()
        if reason == _NOT_IN_CLI and pattern in accepted
    }
    assert not now_in_cli, f"the CLI accepts these now, so offer them: {sorted(now_in_cli)}"
    assert _options(_query_subparser(wrapper._build_parser())) == _options(cli_query)


def test_path_to_forwards_its_to_argument(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    fake_cli, captured = _capture_cli(tmp_path)

    result = subprocess.run(
        [
            sys.executable, str(WRAPPER), "query", "path_to", "start",
            "--to", "finish", "--repo", str(repo),
        ],
        env={**os.environ, "CRG_BIN": str(fake_cli), "CAPTURE_ARGV": str(captured)},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(captured.read_text()) == [
        "query", "path_to", "start", "--to", "finish", "--repo", str(repo.resolve())
    ]


def test_path_to_without_to_stops_before_the_cli_runs(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    fake_cli, captured = _capture_cli(tmp_path)

    result = subprocess.run(
        [sys.executable, str(WRAPPER), "query", "path_to", "start", "--repo", str(repo)],
        env={**os.environ, "CRG_BIN": str(fake_cli), "CAPTURE_ARGV": str(captured)},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "--to" in result.stderr
    assert not captured.exists()


def _wrapper_cli(tmp_path: Path) -> Path:
    cli = tmp_path / "real crg"
    cli.write_text(
        f"#!{sys.executable}\n"
        "from code_review_graph.cli import main\n"
        "main()\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    return cli


def _snapshot(db: Path) -> tuple[bytes, int, set[str]]:
    return (
        db.read_bytes(),
        db.stat().st_mtime_ns,
        {
            path.name
            for path in db.parent.iterdir()
            if path.name.startswith(db.name + "-")
        },
    )


def test_fallback_routes_query_and_rejects_mutation(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    captured = tmp_path / "argv.json"
    fake_cli = tmp_path / "fake crg"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['CAPTURE_ARGV'], 'w') as out:\n"
        "    json.dump(sys.argv[1:], out)\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    env = {**os.environ, "CRG_BIN": str(fake_cli), "CAPTURE_ARGV": str(captured)}

    result = subprocess.run(
        [sys.executable, str(WRAPPER), "query", "callers_of", "symbol", "--repo", str(repo)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(captured.read_text()) == [
        "query", "callers_of", "symbol", "--repo", str(repo)
    ]

    captured.unlink()
    result = subprocess.run(
        [sys.executable, str(WRAPPER), "build", "--repo", str(repo)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert not captured.exists()

    not_repo = tmp_path / "not-repo"
    not_repo.mkdir()
    result = subprocess.run(
        [sys.executable, str(WRAPPER), "status", "--repo", str(not_repo)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 127
    assert "No repository marker" in result.stderr
    assert not captured.exists()


def test_subdirectory_repo_resolves_to_its_repository_root(tmp_path):
    """The CLI reads the graph from --repo, so a subdirectory would look unbuilt."""
    repo = tmp_path / "repo"
    nested = repo / "pkg" / "sub"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()
    captured = tmp_path / "argv.json"
    fake_cli = tmp_path / "fake crg"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['CAPTURE_ARGV'], 'w') as out:\n"
        "    json.dump(sys.argv[1:], out)\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)

    result = subprocess.run(
        [sys.executable, str(WRAPPER), "status", "--repo", str(nested)],
        env={**os.environ, "CRG_BIN": str(fake_cli), "CAPTURE_ARGV": str(captured)},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(captured.read_text()) == ["status", "--json", "--repo", str(repo.resolve())]


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores the read-only bit",
)
def test_fallback_uses_a_read_only_connection_without_sidecars(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    db = repo / ".code-review-graph" / "graph.db"
    db.parent.mkdir()

    store = GraphStore(db)
    store.upsert_node(
        NodeInfo(
            kind="Function",
            name="read_only_probe",
            file_path=str(repo / "probe.py"),
            line_start=1,
            line_end=1,
            language="python",
        )
    )
    rebuild_fts_index(store)
    store.set_metadata("repo_root", str(repo))
    store.close()

    # A clean WAL database has no sidecars; opening it must not create them.
    connection = sqlite3.connect(db)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.commit()
    finally:
        connection.close()
    for suffix in ("-wal", "-shm"):
        db.with_name(db.name + suffix).unlink(missing_ok=True)

    before = _snapshot(db)
    env = {
        **os.environ,
        "CRG_BIN": str(_wrapper_cli(tmp_path)),
        "CRG_HOME": str(tmp_path / "crg-home"),
        "PYTHONPATH": str(PROJECT_ROOT),
    }

    data_dir = db.parent
    original_mode = data_dir.stat().st_mode
    data_dir.chmod(0o500)
    try:
        result = subprocess.run(
            [sys.executable, str(WRAPPER), "status", "--repo", str(repo)],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

        result = subprocess.run(
            [
                sys.executable,
                str(WRAPPER),
                "search",
                "read_only_probe",
                "--repo",
                str(repo),
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "read_only_probe" in result.stdout

        assert _snapshot(db) == before
    finally:
        data_dir.chmod(original_mode)


def test_fallback_does_not_create_a_missing_database(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    env = {
        **os.environ,
        "CRG_BIN": str(_wrapper_cli(tmp_path)),
        "CRG_HOME": str(tmp_path / "crg-home"),
        "PYTHONPATH": str(PROJECT_ROOT),
    }

    result = subprocess.run(
        [sys.executable, str(WRAPPER), "status", "--repo", str(repo)],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "No graph found" in result.stderr
    assert not (repo / ".code-review-graph").exists()


def test_fallback_does_not_initialize_an_empty_database(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    db = repo / ".code-review-graph" / "graph.db"
    db.parent.mkdir()
    db.touch()
    before = _snapshot(db)
    env = {
        **os.environ,
        "CRG_BIN": str(_wrapper_cli(tmp_path)),
        "CRG_HOME": str(tmp_path / "crg-home"),
        "PYTHONPATH": str(PROJECT_ROOT),
    }

    result = subprocess.run(
        [sys.executable, str(WRAPPER), "status", "--repo", str(repo)],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "No graph found" in result.stderr
    assert _snapshot(db) == before


@pytest.mark.parametrize("command", [("status",), ("detect-changes",), ("flows",)])
def test_fallback_refuses_a_graph_it_cannot_migrate(tmp_path, command):
    """A read-only open cannot migrate, so an old graph gets a clear error."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    db = repo / ".code-review-graph" / "graph.db"
    db.parent.mkdir()
    GraphStore(db).close()
    connection = sqlite3.connect(db)
    try:
        connection.execute("DROP TABLE flow_memberships")
        connection.execute("DROP TABLE flows")
        connection.execute("UPDATE metadata SET value = '1' WHERE key = 'schema_version'")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    before = _snapshot(db)
    env = {
        **os.environ,
        "CRG_BIN": str(_wrapper_cli(tmp_path)),
        "CRG_HOME": str(tmp_path / "crg-home"),
        "PYTHONPATH": str(PROJECT_ROOT),
    }

    result = subprocess.run(
        [sys.executable, str(WRAPPER), *command, "--repo", str(repo)],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert "code-review-graph update" in result.stderr
    assert _snapshot(db) == before
