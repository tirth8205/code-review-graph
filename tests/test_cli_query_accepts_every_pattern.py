"""The CLI ``query`` command takes every pattern the MCP query tool takes.

It took 9 of the 19, so ``references_to``, the Spring and event patterns
and the rest were only reachable over MCP. Found in PR #1191. The choices
now come from the same list as the tool, so a new pattern reaches both.
"""

from __future__ import annotations

import argparse
import json
import sys
from unittest.mock import patch

import pytest

import code_review_graph.tools  # noqa: F401 - exposes lazy patch targets
from code_review_graph import cli
from code_review_graph.tools.query import _QUERY_PATTERNS


def _pattern_choices() -> set[str]:
    parser = cli.build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            query = action.choices["query"]
            for query_action in query._actions:
                if query_action.dest == "pattern":
                    return set(query_action.choices)
    raise AssertionError("query has no pattern argument")


def test_cli_query_takes_every_tool_pattern():
    assert _pattern_choices() == set(_QUERY_PATTERNS)


@pytest.mark.parametrize("pattern", sorted(_QUERY_PATTERNS))
def test_each_pattern_reaches_the_tool(pattern, tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "graph.db").touch()
    monkeypatch.setenv("CRG_DATA_DIR", str(data_dir))
    extra = ["--to", "other"] if pattern == "path_to" else []
    argv = ["code-review-graph", "query", pattern, "target", *extra, "--repo", str(repo)]
    result = {"status": "ok", "pattern": pattern}

    with patch.object(sys, "argv", argv):
        with patch("code_review_graph.tools.query_graph", return_value=result) as tool:
            cli.main()

    assert json.loads(capsys.readouterr().out) == result
    expected = {"to": "other"} if pattern == "path_to" else {}
    tool.assert_called_once_with(
        pattern=pattern, target="target", repo_root=str(repo), **expected
    )


def test_help_names_every_pattern(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["query", "--help"])

    out = " ".join(capsys.readouterr().out.split())
    for pattern in _QUERY_PATTERNS:
        assert pattern in out
