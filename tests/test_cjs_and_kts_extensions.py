"""``.cjs`` and ``.kts`` files are source code like their siblings. See: #1117, #1158

``.mjs`` and ``.kt`` were in the extension map and these two were not, so every
build skipped CommonJS modules and Kotlin scripts and reported the repository
as if they did not exist. The import resolver already knew both suffixes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path
from code_review_graph.parser import CodeParser, is_test_file
from code_review_graph.tools import query_graph


@pytest.mark.parametrize(
    ("name", "language"),
    [("util.cjs", "javascript"), ("UTIL.CJS", "javascript"), ("build.gradle.kts", "kotlin")],
)
def test_extension_is_detected(name, language):
    assert CodeParser().detect_language(Path(name)) == language


def test_cjs_file_yields_its_functions_and_calls(tmp_path):
    path = tmp_path / "util.cjs"
    path.write_text(
        "function helper() { return 1; }\n"
        "function run() { return helper(); }\n"
        "module.exports = { helper, run };\n",
        encoding="utf-8",
    )

    nodes, edges = CodeParser().parse_file(path)

    assert {"helper", "run"} <= {node.name for node in nodes}
    assert any(
        edge.kind == "CALLS" and edge.source.endswith("::run") and edge.target.endswith("helper")
        for edge in edges
    )


def test_kts_file_yields_its_functions_and_calls(tmp_path):
    path = tmp_path / "tool.main.kts"
    path.write_text(
        "fun helper(): Int = 1\n"
        "fun run(): Int = helper()\n"
        "class Tool\n",
        encoding="utf-8",
    )

    nodes, edges = CodeParser().parse_file(path)

    assert {"helper", "run", "Tool"} <= {node.name for node in nodes}
    assert any(
        edge.kind == "CALLS" and edge.source.endswith("::run") and edge.target.endswith("helper")
        for edge in edges
    )


def test_build_of_the_issue_repro_links_require_and_calls(tmp_path, monkeypatch):
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    root = tmp_path.resolve()
    (root / ".git").mkdir()
    (root / "util.cjs").write_text(
        "function helper() { return 1; }\nmodule.exports = { helper };\n",
        encoding="utf-8",
    )
    (root / "main.cjs").write_text(
        "const { helper } = require('./util.cjs');\nfunction run() { return helper(); }\n",
        encoding="utf-8",
    )
    (root / "same.js").write_text("function same() { return 1; }\n", encoding="utf-8")

    with GraphStore(get_db_path(root)) as store:
        full_build(root, store)
        assert store.get_stats().files_count == 3

    importers = query_graph("importers_of", "util.cjs", str(root))
    assert [row["file"] for row in importers["results"]] == [(root / "main.cjs").as_posix()]
    callers = query_graph("callers_of", "helper", str(root))
    assert [row["name"] for row in callers["results"]] == ["run"]


def test_cjs_test_file_counts_as_test_code(tmp_path):
    assert is_test_file(tmp_path / "util.test.cjs", tmp_path)
