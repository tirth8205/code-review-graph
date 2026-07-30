"""Safe CommonJS/dynamic-import subset reconciled from PR #95."""

from pathlib import Path

from code_review_graph.parser import CodeParser


def _parse(tmp_path: Path, source: str, suffix: str = ".js"):
    path = tmp_path / f"app{suffix}"
    path.write_text(source, encoding="utf-8")
    return path, CodeParser().parse_file(path)


def _imports(edges):
    return [edge for edge in edges if edge.kind == "IMPORTS_FROM"]


def test_static_require_resolves_relative_file_and_deduplicates(tmp_path):
    dependency = tmp_path / "dependency.js"
    dependency.write_text("export function run() {}\n", encoding="utf-8")
    path, (_nodes, edges) = _parse(
        tmp_path,
        "const first = require('./dependency');\n"
        "const second = require('./dependency');\n",
    )

    imports = _imports(edges)
    assert len(imports) == 1
    assert imports[0].source == path.as_posix()
    assert imports[0].target == dependency.resolve().as_posix()


def test_destructured_require_populates_import_map_for_call_resolution(tmp_path):
    dependency = tmp_path / "dependency.js"
    dependency.write_text("export function run() {}\n", encoding="utf-8")
    _path, (_nodes, edges) = _parse(
        tmp_path,
        "const { run } = require('./dependency');\n"
        "run();\n",
    )

    calls = [edge for edge in edges if edge.kind == "CALLS"]
    assert any(edge.target == f"{dependency.resolve().as_posix()}::run" for edge in calls)


def test_static_dynamic_import_is_recorded(tmp_path):
    dependency = tmp_path / "dependency.js"
    dependency.write_text("export const value = 1;\n", encoding="utf-8")
    _path, (_nodes, edges) = _parse(
        tmp_path,
        "async function load() { return import('./dependency'); }\n",
    )

    assert [edge.target for edge in _imports(edges)] == [dependency.resolve().as_posix()]


def test_package_require_remains_an_unresolved_package_edge(tmp_path):
    _path, (_nodes, edges) = _parse(tmp_path, "const express = require('express');\n")
    assert [edge.target for edge in _imports(edges)] == ["express"]


def test_dynamic_template_require_is_not_misrepresented_as_a_file(tmp_path):
    _path, (_nodes, edges) = _parse(
        tmp_path,
        "const command = require(`./commands/${name}`);\n"
        "const helper = import(`./utils/${name}.js`);\n",
    )
    assert _imports(edges) == []


def test_path_join_require_is_not_reduced_to_the_last_segment(tmp_path):
    _path, (_nodes, edges) = _parse(
        tmp_path,
        "const command = require(path.join(__dirname, group, 'handler'));\n",
    )
    assert _imports(edges) == []


def test_empty_and_argumentless_require_are_ignored(tmp_path):
    _path, (_nodes, edges) = _parse(
        tmp_path,
        "const empty = require('');\nconst missing = require();\n",
    )
    assert _imports(edges) == []
