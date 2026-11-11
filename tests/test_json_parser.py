"""JSON files: their paths as nodes, never their values, and no flood.

``.json`` files gave nothing. YAML showed what can go wrong the other way: on
sidekiq, translation files alone became thousands of nodes (#1150). So JSON
follows the same value-free shape, and data files stay out. See: #436
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from code_review_graph.changes import analyze_changes
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import (
    DEFAULT_IGNORE_PATTERNS,
    _should_ignore,
    full_build,
    get_db_path,
    incremental_update,
)
from code_review_graph.parser import CodeParser, normalize_file_path
from code_review_graph.postprocessing import run_post_processing
from code_review_graph.search import hybrid_search
from code_review_graph.tools.query import query_graph, semantic_search_nodes
from code_review_graph.tools.review import detect_changes_func

FIXTURE = Path(__file__).parent / "fixtures" / "sample_config.json"
SOURCE = FIXTURE.read_bytes()


def _parse(source: bytes, path: Path | None = None, root: Path | None = None):
    parser = CodeParser(repo_root=root) if root else CodeParser()
    return parser.parse_bytes(path or Path("/repo/config.json"), source)


def _paths(nodes):
    return {node.name: node for node in nodes if node.kind == "JsonPath"}


def test_json_extension_is_detected():
    assert CodeParser().detect_language(Path("a.json")) == "json"
    assert CodeParser().detect_language(Path("A.JSON")) == "json"


def test_file_node_says_values_are_not_indexed():
    nodes, _ = _parse(SOURCE)

    assert nodes[0].kind == "File"
    assert nodes[0].language == "json"
    assert nodes[0].extra["values_indexed"] is False
    assert nodes[0].extra["json_truncated"] is False


def test_paths_are_indexed():
    paths = _paths(_parse(SOURCE)[0])

    assert set(paths) == {
        "$",
        "$.name",
        "$.version",
        "$.scripts",
        "$.scripts.build",
        "$.scripts.test",
        "$.services",
        "$.services[*]",
        "$.services[*].id",
        "$.services[*].metadata",
        "$.services[*].metadata.tier",
        "$.services[*].secret-token",
        "$.services[*].replicas",
        '$["dotted.key"]',
        "$.empty",
    }


def test_values_never_reach_a_node_or_an_edge():
    nodes, edges = _parse(SOURCE)
    dumped = json.dumps(
        [(n.name, n.params, n.extra) for n in nodes]
        + [(e.source, e.target, e.extra) for e in edges],
    )

    for value in ("never-index-this", "frontend", "jest --ci", "1.0.0", "worker"):
        assert value not in dumped


def test_value_types_are_recorded():
    paths = _paths(_parse(SOURCE)[0])

    assert paths["$"].extra["value_type"] == "object"
    assert paths["$.services"].extra["value_type"] == "array"
    assert paths["$.name"].extra["value_type"] == "string"
    assert paths['$["dotted.key"]'].extra["value_type"] == "boolean"
    assert paths["$.empty"].extra["value_type"] == "null"
    assert paths["$.services[*].replicas"].extra["value_type"] == "number"


def test_mixed_value_types_are_named():
    paths = _paths(_parse(b'[{"a": 1}, {"a": "x"}]')[0])

    assert paths["$[*].a"].extra["value_type"] == "mixed"
    assert paths["$[*].a"].extra["value_types"] == ["number", "string"]


def test_array_items_share_one_path_and_keep_samples():
    paths = _paths(_parse(SOURCE)[0])
    tier = paths["$.services[*].metadata.tier"]

    assert tier.extra["occurrence_count"] == 2
    assert tier.extra["line_samples"] == [12, 19]
    assert tier.extra["range_samples"] == [[12, 12], [19, 19]]
    assert tier.extra["example_path"] == "$.services[0].metadata.tier"
    assert (tier.line_start, tier.line_end) == (12, 19)


def test_occurrence_samples_are_capped():
    source = json.dumps([{"a": 1}] * 500, indent=0).encode()
    node = _paths(_parse(source)[0])["$[*].a"]

    assert node.extra["occurrence_count"] == 500
    assert len(node.extra["line_samples"]) < 500
    assert node.extra["lines_truncated"] is True


def test_lines_cover_the_key_and_value():
    paths = _paths(_parse(SOURCE)[0])

    assert (paths["$.scripts"].line_start, paths["$.scripts"].line_end) == (4, 7)
    assert (paths["$"].line_start, paths["$"].line_end) == (1, 26)


def test_contains_edges_follow_the_path_tree():
    nodes, edges = _parse(SOURCE)
    contains = {(e.source, e.target) for e in edges if e.kind == "CONTAINS"}
    file_path = "/repo/config.json"

    assert (file_path, f"{file_path}::json:$") in contains
    assert (f"{file_path}::json:$", f"{file_path}::json:$.scripts") in contains
    assert (
        f"{file_path}::json:$.scripts", f"{file_path}::json:$.scripts.build",
    ) in contains
    assert all(e.kind == "CONTAINS" for e in edges)
    assert len(contains) == len(_paths(nodes))


def test_comments_and_trailing_commas_are_tolerated():
    """tsconfig.json and .vscode/settings.json are JSON with comments."""
    source = b"""// top comment
{
  /* block */
  "compilerOptions": {
    "target": "es2020", // inline
    "strict": true,
  },
}
"""

    paths = _paths(_parse(source)[0])

    assert {"$.compilerOptions.target", "$.compilerOptions.strict"} <= set(paths)


def test_escaped_keys_are_read():
    source = b'{"a\\"b": 1, "caf\\u00e9": 2}'
    paths = _paths(_parse(source)[0])

    assert '$["a\\"b"]' in paths
    assert '$["caf\xe9"]' in paths


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("config.json", b'{"\\ud800": 1, "ok": 2, "smile\\ud83d\\ude00": 3}'),
        ("config.yml", b'"\\ud800": 1\nok: 2\n"smile\\U0001F600": 3\n'),
    ],
)
def test_a_key_sqlite_cannot_store_is_skipped(tmp_path, name, source):
    """A lone surrogate such as ``\\ud800`` is not UTF-8, so SQLite refused the
    whole build. That key is left out; a real emoji stays."""
    root = tmp_path.resolve()
    (root / ".git").mkdir()
    (root / name).write_bytes(source)
    store = GraphStore(get_db_path(root))
    try:
        full_build(root, store)
        names = {node.name for node in store.get_nodes_by_file(str(root / name))}
    finally:
        store.close()

    assert "$.ok" in names
    assert '$["smile\U0001f600"]' in names
    assert not any("\ud800" in node_name for node_name in names)


@pytest.mark.parametrize(
    "source",
    [
        b"",
        b"   ",
        b"{",
        b'{"a":',
        b"not json",
        b'{"a": [1, 2',
        b"\xff\xfe{}",
        b"[" * 5000,
        b'{"a":' * 2000,
    ],
)
def test_malformed_json_does_not_raise(source):
    nodes, _ = _parse(source)

    assert nodes[0].kind == "File"


def test_scalar_and_empty_documents():
    assert set(_paths(_parse(b"3")[0])) == {"$"}
    assert set(_paths(_parse(b"{}")[0])) == {"$"}
    assert set(_paths(_parse(b"[]")[0])) == {"$"}


def test_deep_nesting_is_bounded_and_marked():
    depth = CodeParser._MAX_JSON_DEPTH + 10
    source = b'{"a":' * depth + b"1" + b"}" * depth

    nodes, _ = _parse(source)

    assert len(_paths(nodes)) <= CodeParser._MAX_JSON_DEPTH + 1
    assert nodes[0].extra["json_truncated"] is True


def test_oversized_file_keeps_only_its_file_node(monkeypatch):
    monkeypatch.setattr(CodeParser, "_MAX_JSON_BYTES", 10)

    nodes, edges = _parse(SOURCE)

    assert [node.kind for node in nodes] == ["File"]
    assert nodes[0].extra["json_truncated"] is True
    assert edges == []


def test_node_count_is_capped_shallow_paths_first(monkeypatch):
    monkeypatch.setattr(CodeParser, "_MAX_JSON_NODES", 4)
    source = b'{"a": {"x": {"deep": 1}}, "b": 1, "c": 1, "d": 1}'

    nodes, _ = _parse(source)
    paths = _paths(nodes)

    assert set(paths) == {"$", "$.a", "$.b", "$.c"}
    assert nodes[0].extra["json_truncated"] is True


def test_an_object_used_as_a_table_is_collapsed():
    """Keys that are data (ids, countries, hashes) must not each become a node."""
    table = {f"key-{index}": {"size": index} for index in range(5000)}
    source = json.dumps({"entries": table, "version": 1}, indent=1).encode()

    paths = _paths(_parse(source)[0])

    assert set(paths) == {
        "$", "$.version", "$.entries", "$.entries.*", "$.entries.*.size",
    }
    assert paths["$.entries.*"].extra["occurrence_count"] == 5000


def test_an_object_under_the_limit_keeps_its_keys():
    limit = CodeParser._MAX_JSON_OBJECT_KEYS
    source = json.dumps({f"k{index}": 1 for index in range(limit)}).encode()

    assert len(_paths(_parse(source)[0])) == limit + 1


@pytest.mark.parametrize(
    "relative",
    [
        "config/locales/devise.en.json",
        "web/locales/pt-BR.json",
        "public/locales/en/common.json",
        "src/i18n/fr.json",
        "translations/de.json",
        "_locales/en/messages.json",
    ],
)
def test_translation_files_get_a_file_node_only(tmp_path, relative):
    """The same rule as YAML: see tests/test_yaml_locale_files.py in #1150."""
    nodes, edges = _parse(b'{"a": {"b": "text"}}', tmp_path / relative, tmp_path)

    assert [node.kind for node in nodes] == ["File"]
    assert nodes[0].extra["json_locale_file"] is True
    assert nodes[0].extra["values_indexed"] is False
    assert edges == []


@pytest.mark.parametrize(
    "relative",
    [
        "config/en.json",
        "it.json",
        "locales/deploy.json",
        "i18n/config.json",
        "lang/en.json",
    ],
)
def test_doubtful_files_keep_their_paths(tmp_path, relative):
    """It takes both a translation folder and a language code, as for YAML."""
    nodes, _ = _parse(b'{"a": 1}', tmp_path / relative, tmp_path)

    assert "$.a" in _paths(nodes)


def test_a_checkout_folder_does_not_make_a_translation_file(tmp_path):
    root = tmp_path / "locales" / "en"
    root.mkdir(parents=True)

    nodes, _ = _parse(b'{"a": 1}', root / "settings.json", root)

    assert "$.a" in _paths(nodes)


@pytest.mark.parametrize(
    "relative",
    [
        "tests/fixtures/response.json",
        "test/data.json",
        "__tests__/snap.json",
        "src/pkg/testdata/case.json",
        "docs/examples/bar-chart.vg.json",
        "examples/app/package.json",
        "data/cars.json",
        "packages/site/demo/scene.json",
    ],
)
def test_data_files_get_a_file_node_only(tmp_path, relative):
    """Tests, docs, examples and data sets: on vega, docs/ alone was 12,725 paths."""
    nodes, edges = _parse(b'{"a": 1}', tmp_path / relative, tmp_path)

    assert [node.kind for node in nodes] == ["File"]
    assert nodes[0].extra["json_data_file"] is True
    assert edges == []


@pytest.mark.parametrize(
    "relative",
    ["package.json", "packages/vega/package.json", "src/config/app.json", ".vscode/launch.json"],
)
def test_project_config_keeps_its_paths(tmp_path, relative):
    nodes, _ = _parse(b'{"a": 1}', tmp_path / relative, tmp_path)

    assert "$.a" in _paths(nodes)
    assert "json_data_file" not in nodes[0].extra


@pytest.mark.parametrize(
    "path",
    [
        "package-lock.json",
        "examples/app/package-lock.json",
        "npm-shrinkwrap.json",
        "a/npm-shrinkwrap.json",
        "packages.lock.json",
        "src/App/packages.lock.json",
        "obj/project.assets.json",
        "src/App/obj/project.assets.json",
        "data/big.min.json",
    ],
)
def test_lock_and_minified_files_are_ignored(path):
    assert _should_ignore(path, DEFAULT_IGNORE_PATTERNS)


@pytest.mark.parametrize(
    "path",
    ["package.json", "tsconfig.json", "a/composer.json", "lock.json", ".vscode/settings.json"],
)
def test_ordinary_config_is_not_ignored(path):
    assert not _should_ignore(path, DEFAULT_IGNORE_PATTERNS)


def _build(tmp_path: Path) -> Path:
    root = tmp_path.resolve()
    (root / "config.json").write_bytes(SOURCE)
    (root / "app.js").write_text('import data from "./config.json";\nexport const v = data;\n')
    (root / "locales").mkdir()
    (root / "locales" / "en.json").write_text('{"greeting": "hello"}')
    (root / "docs").mkdir()
    (root / "docs" / "example.json").write_text('{"replicas": 2}')
    (root / "package-lock.json").write_text('{"lockfileVersion": 3}')
    (root / ".git").mkdir()
    store = GraphStore(get_db_path(root))
    try:
        full_build(root, store)
        run_post_processing(store)
    finally:
        store.close()
    return root


def test_graph_paths_are_reachable_from_the_file(tmp_path):
    root = _build(tmp_path)

    file_children = query_graph("children_of", "config.json", str(root))
    children = query_graph("children_of", "config.json::json:$", str(root))

    assert [row["name"] for row in file_children["results"]] == ["$"]
    assert "$.scripts" in {row["name"] for row in children["results"]}


def test_graph_rows_expose_metadata_without_values(tmp_path):
    root = _build(tmp_path)

    summary = query_graph("file_summary", "config.json", str(root))
    rows = {row["name"]: row for row in summary["results"]}

    tier = rows["$.services[*].metadata.tier"]["json"]
    assert tier["schema_path"] == "$.services[*].metadata.tier"
    assert tier["occurrence_count"] == 2
    assert "yaml" not in rows["$.services"]
    file_row = next(row for row in summary["results"] if row["kind"] == "File")
    assert file_row["json"]["values_indexed"] is False
    assert "never-index-this" not in json.dumps(summary)


def test_translation_file_row_says_so(tmp_path):
    root = _build(tmp_path)

    summary = query_graph("file_summary", "locales/en.json", str(root))

    assert [row["kind"] for row in summary["results"]] == ["File"]
    assert summary["results"][0]["json"]["locale_file"] is True


def test_data_file_row_says_so(tmp_path):
    root = _build(tmp_path)

    summary = query_graph("file_summary", "docs/example.json", str(root))

    assert [row["kind"] for row in summary["results"]] == ["File"]
    assert summary["results"][0]["json"]["data_file"] is True
    assert "locale_file" not in summary["results"][0]["json"]


def test_keys_are_searchable_and_values_are_not(tmp_path):
    root = _build(tmp_path)

    by_key = semantic_search_nodes("replicas", kind="JsonPath", repo_root=str(root))
    by_value = semantic_search_nodes("frontend", kind="JsonPath", repo_root=str(root))

    assert [row["name"] for row in by_key["results"]] == ["$.services[*].replicas"]
    assert by_value["results"] == []


def test_a_script_import_now_reaches_the_json_file(tmp_path):
    root = _build(tmp_path)

    importers = query_graph("importers_of", "config.json", str(root))

    assert any(row["importer"].endswith("app.js") for row in importers["results"])


def test_lock_file_is_not_in_the_graph(tmp_path):
    root = _build(tmp_path)

    summary = query_graph("file_summary", "package-lock.json", str(root))

    assert summary["results"] == []


def test_changed_lines_map_to_json_paths(tmp_path):
    root = _build(tmp_path)
    store = GraphStore(get_db_path(root))
    file_path = normalize_file_path(root / "config.json")
    try:
        result = analyze_changes(
            store,
            changed_files=[file_path],
            changed_ranges={file_path: [(19, 19)]},
            repo_root=str(root),
        )
    finally:
        store.close()

    names = {row["name"] for row in result["changed_json_paths"]}
    assert "$.services[*].metadata.tier" in names
    assert "$.scripts" not in names
    assert result["changed_yaml_paths"] == []
    assert result["json_paths_truncated"] is False
    assert "changed JSON path(s)" in result["summary"]


def test_kind_filter_accepts_jsonpath_on_the_cli():
    from code_review_graph.cli import build_parser

    args = build_parser().parse_args(["search", "replicas", "--kind", "JsonPath"])

    assert args.kind == "JsonPath"


# ---------------------------------------------------------------------------
# Search: JSON shares words with code, so code must stay on top
# ---------------------------------------------------------------------------


def _build_search_repo(tmp_path: Path, locales: int = 0) -> Path:
    root = tmp_path.resolve()
    (root / ".git").mkdir()
    (root / "src").mkdir()
    (root / "src" / "response.js").write_text(
        "var res = {};\n"
        "res.cookie = function (name, value) {\n  return name + value;\n};\n"
        "module.exports = res;\n"
    )
    (root / "src" / "i18n.js").write_text(
        "export function appWithTranslation(app) {\n  return app;\n}\n"
    )
    (root / "package.json").write_text(json.dumps(
        {"name": "app", "dependencies": {"cookie": "1.0.0", "cookie-signature": "1.0.0"}},
        indent=2,
    ))
    languages = [
        "de", "en", "fr", "it", "ja", "nl", "es", "pt", "ru", "zh",
        "ko", "pl", "sv", "da", "fi", "nb", "cs", "tr", "uk", "el",
    ]
    for language in languages[:locales]:
        folder = root / "public" / "locales" / language
        folder.mkdir(parents=True)
        (folder / "translation.json").write_text('{"title": "x"}')
    store = GraphStore(get_db_path(root))
    try:
        full_build(root, store)
        run_post_processing(store)
    finally:
        store.close()
    return root


def test_code_outranks_a_json_key_with_the_same_name(tmp_path):
    """On express, ``$.dependencies.cookie`` came above ``res.cookie``."""
    root = _build_search_repo(tmp_path)

    rows = semantic_search_nodes("cookie", repo_root=str(root))["results"]

    assert rows[0]["kind"] == "Function"
    assert rows[0]["name"] == "res.cookie"
    json_ranks = [i for i, row in enumerate(rows) if row["kind"] == "JsonPath"]
    assert json_ranks and min(json_ranks) > 0


@pytest.mark.parametrize(
    ("query", "kind"),
    [("cookie", "JsonPath"), ("$.dependencies.cookie", None), ("package.json cookie", None)],
)
def test_json_still_comes_first_when_asked_for(tmp_path, query, kind):
    root = _build_search_repo(tmp_path)

    rows = semantic_search_nodes(query, kind=kind, repo_root=str(root))["results"]

    assert rows[0]["language"] == "json"


def test_json_files_do_not_use_up_the_search_window(tmp_path):
    """On next-i18next, six translation.json files hid ``appWithTranslation``."""
    root = _build_search_repo(tmp_path, locales=6)
    store = GraphStore(get_db_path(root))
    try:
        rows = hybrid_search(store, "translation", limit=1, repo_root=root)
    finally:
        store.close()

    assert [row["name"] for row in rows] == ["appWithTranslation"]


def test_many_json_files_do_not_use_up_the_search_window(tmp_path):
    """Widening the window a few times still stopped at 15 rows for limit=1."""
    root = _build_search_repo(tmp_path, locales=20)
    store = GraphStore(get_db_path(root))
    try:
        rows = hybrid_search(store, "translation", limit=1, repo_root=root)
    finally:
        store.close()

    assert [row["name"] for row in rows] == ["appWithTranslation"]


def test_a_file_search_looks_past_json_files(tmp_path):
    """``kind="File"`` is not a request for JSON, so JSON files must not fill it."""
    root = _build_search_repo(tmp_path, locales=20)
    code = root / "src" / "lib" / "client" / "runtime" / "translation.js"
    code.parent.mkdir(parents=True)
    code.write_text("export const translation = 1;\n")
    store = GraphStore(get_db_path(root))
    try:
        full_build(root, store)
        run_post_processing(store)
        rows = hybrid_search(store, "translation", kind="File", limit=1, repo_root=root)
    finally:
        store.close()

    assert [row["file_path"] for row in rows] == [normalize_file_path(code)]


# ---------------------------------------------------------------------------
# detect_changes_tool bounds JSON paths like YAML paths
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_detect_changes_reports_and_bounds_json_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / ".gitignore").write_text(".code-review-graph/\n")
    config = repo / "settings.json"
    config.write_text('{\n  "server": {\n    "port": 80\n  },\n  "log": "info"\n}\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    with GraphStore(get_db_path(repo)) as store:
        full_build(repo, store)

    config.write_text('{\n  "server": {\n    "port": 8080\n  },\n  "log": "info"\n}\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "bump port")
    with GraphStore(get_db_path(repo)) as store:
        incremental_update(repo, store)

    standard = detect_changes_func(base="HEAD~1", repo_root=str(repo))
    minimal = detect_changes_func(base="HEAD~1", repo_root=str(repo), detail_level="minimal")
    bounded = detect_changes_func(base="HEAD~1", repo_root=str(repo), max_results=1)

    assert {row["name"] for row in standard["changed_json_paths"]} == {
        "$", "$.server", "$.server.port",
    }
    assert standard["changed_yaml_paths"] == []
    assert minimal["changed_json_path_count"] == 3
    assert len(bounded["changed_json_paths"]) == 1
    assert bounded["changed_json_paths_total"] == 3
    assert bounded["truncated"] is True
    assert "1 of 3 changed JSON path(s)" in bounded["summary"]
