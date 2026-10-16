"""CommonJS `.cjs` files are parsed as JavaScript, like `.js` and `.mjs`."""

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import CodeParser
from code_review_graph.tools.query import query_graph


def test_cjs_is_detected_as_javascript():
    assert CodeParser().detect_language(Path("hooks/sync.cjs")) == "javascript"


def test_cjs_file_yields_functions_and_calls(tmp_path):
    src = tmp_path / "sync.cjs"
    src.write_text(
        '"use strict";\n'
        "function merge(a, b) { return { ...a, ...b }; }\n"
        "function install(settings) { return merge(settings, {}); }\n"
        "module.exports = { install };\n",
        encoding="utf-8",
    )

    nodes, edges = CodeParser().parse_file(src)

    assert {n.name for n in nodes if n.kind == "Function"} == {"merge", "install"}
    calls = {
        (e.source.split("::")[-1], e.target.split("::")[-1]) for e in edges if e.kind == "CALLS"
    }
    assert ("install", "merge") in calls


def test_full_build_indexes_cjs_and_resolves_require(tmp_path):
    (tmp_path / "util.cjs").write_text(
        "function helper() { return 1; }\nmodule.exports = { helper };\n", encoding="utf-8"
    )
    (tmp_path / "main.cjs").write_text(
        "const { helper } = require('./util.cjs');\nfunction run() { return helper(); }\n",
        encoding="utf-8",
    )
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()
    with GraphStore(graph_dir / "graph.db") as store:
        built = full_build(tmp_path, store)
        assert built["errors"] == []
        assert store.get_node(f"{(tmp_path / 'util.cjs').as_posix()}::helper") is not None
        assert store.get_node(f"{(tmp_path / 'main.cjs').as_posix()}::run") is not None

    root = str(tmp_path)
    util = (tmp_path / "util.cjs").as_posix()
    importers = query_graph("importers_of", util, repo_root=root)
    assert importers["status"] == "ok"
    assert {node["file"] for node in importers["results"]} == {(tmp_path / "main.cjs").as_posix()}
    callers = query_graph("callers_of", f"{util}::helper", repo_root=root)
    assert callers["status"] == "ok"
    assert {node["name"] for node in callers["results"]} == {"run"}
