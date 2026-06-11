"""Tests for config-driven custom language support (languages.toml, #320).

Erlang is used as the end-to-end grammar: tree_sitter_language_pack ships
it, but code-review-graph has no built-in ``.erl`` support (only Elixir on
the BEAM side), so it exercises the full bring-your-own-language path.
"""

import logging
from pathlib import Path

import pytest

from code_review_graph import custom_languages
from code_review_graph.custom_languages import (
    CONFIG_RELATIVE_PATH,
    MAX_CUSTOM_LANGUAGES,
    load_custom_languages,
)
from code_review_graph.parser import (
    EXTENSION_TO_LANGUAGE,
    CodeParser,
    _builtin_language_names,
)

BUILTIN_LANGUAGES = _builtin_language_names()

ERLANG_TOML = """\
[languages.erlang]
extensions = [".erl"]
grammar = "erlang"
function_node_types = ["function_clause"]
class_node_types = ["record_decl"]
import_node_types = ["import_attribute"]
call_node_types = ["call"]
comment = "Erlang via the bundled tree-sitter-erlang grammar"
"""

ERLANG_SOURCE = """\
-module(math_utils).
-export([add/2, scale/2]).
-import(lists, [map/2]).

-record(point, {x, y}).

add(A, B) ->
    helper(A) + B.

helper(X) -> X * 2.

scale(Points, F) ->
    lists:map(fun(P) -> add(P, F) end, Points).
"""


def write_config(repo_root: Path, text: str) -> Path:
    config_path = repo_root / CONFIG_RELATIVE_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(text, encoding="utf-8")
    return config_path


def load(repo_root: Path):
    return load_custom_languages(
        repo_root,
        builtin_extensions=EXTENSION_TO_LANGUAGE,
        builtin_languages=BUILTIN_LANGUAGES,
    )


@pytest.fixture(autouse=True)
def _clear_loader_cache():
    custom_languages.clear_cache()
    yield
    custom_languages.clear_cache()


class TestLoader:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load(tmp_path) == {}

    def test_malformed_toml_warns_and_returns_empty(self, tmp_path, caplog):
        write_config(tmp_path, "[languages.broken\nnot toml at all")
        with caplog.at_level(logging.WARNING):
            assert load(tmp_path) == {}
        assert "Malformed TOML" in caplog.text

    def test_valid_language_loaded(self, tmp_path):
        write_config(tmp_path, ERLANG_TOML)
        result = load(tmp_path)
        assert set(result) == {"erlang"}
        lang = result["erlang"]
        assert lang.grammar == "erlang"
        assert lang.extensions == (".erl",)
        assert lang.function_node_types == ("function_clause",)
        assert lang.class_node_types == ("record_decl",)
        assert lang.import_node_types == ("import_attribute",)
        assert lang.call_node_types == ("call",)
        assert "tree-sitter-erlang" in lang.comment

    def test_extensions_normalised_to_lowercase(self, tmp_path):
        write_config(tmp_path, """\
[languages.erlang]
extensions = [".ERL"]
grammar = "erlang"
function_node_types = ["function_clause"]
""")
        result = load(tmp_path)
        assert result["erlang"].extensions == (".erl",)

    def test_bad_grammar_skipped(self, tmp_path, caplog):
        write_config(tmp_path, """\
[languages.mylang]
extensions = [".myl"]
grammar = "not_a_real_grammar"
function_node_types = ["function_definition"]
""")
        with caplog.at_level(logging.WARNING):
            assert load(tmp_path) == {}
        assert "not_a_real_grammar" in caplog.text
        assert "tree_sitter_language_pack" in caplog.text

    def test_builtin_extension_collision_skipped(self, tmp_path, caplog):
        write_config(tmp_path, """\
[languages.notpython]
extensions = [".py"]
grammar = "erlang"
function_node_types = ["function_clause"]
""")
        with caplog.at_level(logging.WARNING):
            assert load(tmp_path) == {}
        assert "built-in" in caplog.text

    def test_extension_without_dot_skipped(self, tmp_path, caplog):
        write_config(tmp_path, """\
[languages.erlang]
extensions = ["erl"]
grammar = "erlang"
function_node_types = ["function_clause"]
""")
        with caplog.at_level(logging.WARNING):
            assert load(tmp_path) == {}
        assert "must start with a dot" in caplog.text

    def test_builtin_language_name_shadowing_skipped(self, tmp_path, caplog):
        write_config(tmp_path, """\
[languages.python]
extensions = [".pyq"]
grammar = "python"
function_node_types = ["function_definition"]
""")
        with caplog.at_level(logging.WARNING):
            assert load(tmp_path) == {}
        assert "shadows a built-in language" in caplog.text

    def test_duplicate_extension_across_custom_languages_skipped(
        self, tmp_path, caplog,
    ):
        write_config(tmp_path, """\
[languages.first]
extensions = [".dup"]
grammar = "erlang"
function_node_types = ["function_clause"]

[languages.second]
extensions = [".dup"]
grammar = "erlang"
function_node_types = ["function_clause"]
""")
        with caplog.at_level(logging.WARNING):
            result = load(tmp_path)
        assert set(result) == {"first"}
        assert "already claimed" in caplog.text

    def test_missing_grammar_key_skipped(self, tmp_path, caplog):
        write_config(tmp_path, """\
[languages.nogramma]
extensions = [".ng"]
function_node_types = ["function_definition"]
""")
        with caplog.at_level(logging.WARNING):
            assert load(tmp_path) == {}
        assert "grammar" in caplog.text

    def test_no_node_types_skipped(self, tmp_path, caplog):
        write_config(tmp_path, """\
[languages.empty]
extensions = [".emp"]
grammar = "erlang"
""")
        with caplog.at_level(logging.WARNING):
            assert load(tmp_path) == {}
        assert "no node types" in caplog.text

    def test_invalid_node_type_list_skipped(self, tmp_path, caplog):
        write_config(tmp_path, """\
[languages.badtypes]
extensions = [".bt"]
grammar = "erlang"
function_node_types = "function_clause"
""")
        with caplog.at_level(logging.WARNING):
            assert load(tmp_path) == {}
        assert "list of non-empty strings" in caplog.text

    def test_cap_at_max_custom_languages(self, tmp_path, caplog):
        blocks = [
            f"""\
[languages.lang{i:02d}]
extensions = [".l{i:02d}"]
grammar = "erlang"
function_node_types = ["function_clause"]
"""
            for i in range(MAX_CUSTOM_LANGUAGES + 2)
        ]
        write_config(tmp_path, "\n".join(blocks))
        with caplog.at_level(logging.WARNING):
            result = load(tmp_path)
        assert len(result) == MAX_CUSTOM_LANGUAGES
        assert "ignoring the rest" in caplog.text

    def test_cache_reused_and_isolated(self, tmp_path):
        write_config(tmp_path, ERLANG_TOML)
        first = load(tmp_path)
        first.pop("erlang")  # Mutating the returned dict must not poison the cache
        second = load(tmp_path)
        assert set(second) == {"erlang"}


class TestParserIntegration:
    def _repo(self, tmp_path: Path) -> tuple[Path, Path]:
        write_config(tmp_path, ERLANG_TOML)
        src = tmp_path / "src" / "math_utils.erl"
        src.parent.mkdir(parents=True)
        src.write_text(ERLANG_SOURCE, encoding="utf-8")
        return tmp_path, src

    def test_detect_language_with_and_without_config(self, tmp_path):
        repo, src = self._repo(tmp_path)
        assert CodeParser(repo).detect_language(src) == "erlang"
        # Without repo_root the custom extension stays unknown.
        assert CodeParser().detect_language(src) is None

    def test_builtin_extensions_unaffected(self, tmp_path):
        repo, _src = self._repo(tmp_path)
        parser = CodeParser(repo)
        assert parser.detect_language(Path("main.py")) == "python"
        assert parser.detect_language(Path("app.ex")) == "elixir"

    def test_e2e_nodes_and_edges(self, tmp_path):
        repo, src = self._repo(tmp_path)
        parser = CodeParser(repo)
        nodes, edges = parser.parse_file(src)

        files = [n for n in nodes if n.kind == "File"]
        assert len(files) == 1
        assert files[0].language == "erlang"

        funcs = {n.name: n for n in nodes if n.kind == "Function"}
        assert {"add", "helper", "scale"} <= set(funcs)
        assert funcs["add"].language == "erlang"
        assert funcs["add"].line_start == 7

        classes = {n.name: n for n in nodes if n.kind == "Class"}
        assert "point" in classes
        assert classes["point"].language == "erlang"

        file_path = str(src)
        calls = {(e.source, e.target) for e in edges if e.kind == "CALLS"}
        # helper(A) inside add/2 resolves to the same-file definition.
        assert (f"{file_path}::add", f"{file_path}::helper") in calls
        # add(P, F) inside the anonymous fun passed to lists:map.
        assert (f"{file_path}::scale", f"{file_path}::add") in calls
        # Remote call keeps its qualified module:function form.
        assert (f"{file_path}::scale", "lists:map") in calls

        imports = {e.target for e in edges if e.kind == "IMPORTS_FROM"}
        assert "lists" in imports

        contains = {e.target for e in edges if e.kind == "CONTAINS"}
        assert f"{file_path}::add" in contains
        assert f"{file_path}::point" in contains

    def test_e2e_parse_without_config_yields_nothing(self, tmp_path):
        _repo, src = self._repo(tmp_path)
        nodes, edges = CodeParser().parse_file(src)
        assert nodes == []
        assert edges == []

    def test_full_build_includes_custom_language(self, tmp_path):
        from code_review_graph.graph import GraphStore
        from code_review_graph.incremental import full_build

        repo, src = self._repo(tmp_path)
        db_path = repo / ".code-review-graph" / "graph.db"
        store = GraphStore(db_path)
        try:
            stats = full_build(repo, store)
            assert stats["files_parsed"] >= 1
            row = store._conn.execute(
                "SELECT language FROM nodes WHERE kind = 'Function' AND name = ?",
                ("helper",),
            ).fetchone()
            assert row is not None
            assert row[0] == "erlang"
        finally:
            store.close()
