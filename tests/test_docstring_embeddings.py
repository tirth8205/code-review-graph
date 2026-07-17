"""Documentation summaries used by semantic embeddings."""

from pathlib import Path
from unittest.mock import patch

from code_review_graph.embeddings import _node_to_text
from code_review_graph.graph import GraphNode, GraphStore
from code_review_graph.incremental import full_build, incremental_update
from code_review_graph.parser import CodeParser


def _parsed_node(path: str, source: bytes, name: str):
    nodes, _ = CodeParser().parse_bytes(Path(path), source)
    return next(node for node in nodes if node.name == name)


class TestDocumentationSummaryExtraction:
    def test_python_uses_runtime_string_value_and_first_paragraph(self):
        node = _parsed_node(
            "module.py",
            (
                b"def parse_rates():\n"
                b'    (r"Parse\\n" " uploaded \\u20ac rate sheets.\\n\\nDetails.")\n'
                b"    return []\n"
            ),
            "parse_rates",
        )

        assert node.extra["docstring"] == (r"Parse\n uploaded " + "\N{EURO SIGN} rate sheets.")

    def test_python_rejects_bytes_and_fstrings(self):
        for literal in (b'b"not docs"', b'f"not {1} docs"'):
            node = _parsed_node(
                "module.py",
                b"def f():\n    " + literal + b"\n    return 1\n",
                "f",
            )
            assert "docstring" not in node.extra

    def test_python_class_and_method_docstrings_are_independent(self):
        nodes, _ = CodeParser().parse_bytes(
            Path("module.py"),
            (
                b"class Parser:\n"
                b'    """Parses uploaded files."""\n'
                b"\n"
                b"    def run(self):\n"
                b'        """Run one parse."""\n'
                b"        return None\n"
            ),
        )
        by_name = {node.name: node for node in nodes}

        assert by_name["Parser"].extra["docstring"] == "Parses uploaded files."
        assert by_name["run"].extra["docstring"] == "Run one parse."

    def test_jsdoc_on_exported_function_and_blank_line_boundary(self):
        documented = _parsed_node(
            "module.ts",
            b"/** Parse uploaded sheets. */\nexport function parse() {}\n",
            "parse",
        )
        detached = _parsed_node(
            "module.ts",
            b"/** Module banner. */\n\nexport function parse() {}\n",
            "parse",
        )

        assert documented.extra["docstring"] == "Parse uploaded sheets."
        assert "docstring" not in detached.extra

    def test_plain_javascript_comment_is_not_documentation(self):
        node = _parsed_node(
            "module.js",
            b"// implementation note\nfunction parse() {}\n",
            "parse",
        )

        assert "docstring" not in node.extra

    def test_go_plain_comment_block_is_documentation(self):
        node = _parsed_node(
            "module.go",
            (
                b"package parser\n\n"
                b"// Parse reads an uploaded sheet\n"
                b"// and returns normalized rows.\n"
                b"func Parse() {}\n"
            ),
            "Parse",
        )

        assert node.extra["docstring"] == (
            "Parse reads an uploaded sheet and returns normalized rows."
        )

    def test_go_compiler_directive_is_not_embedding_text(self):
        node = _parsed_node(
            "module.go",
            (
                b"package parser\n\n"
                b"// Parse reads an uploaded sheet.\n"
                b"//go:noinline\n"
                b"func Parse() {}\n"
            ),
            "Parse",
        )

        assert node.extra["docstring"] == "Parse reads an uploaded sheet."

    def test_rust_outer_docs_cross_attributes_but_inner_docs_do_not_attach(self):
        documented = _parsed_node(
            "module.rs",
            b"/// Parse a sheet.\n#[inline]\nfn parse() {}\n",
            "parse",
        )
        inner = _parsed_node(
            "module.rs",
            b"//! Module documentation.\nfn parse() {}\n",
            "parse",
        )

        assert documented.extra["docstring"] == "Parse a sheet."
        assert "docstring" not in inner.extra

    def test_javadoc_keeps_only_the_summary_paragraph(self):
        node = _parsed_node(
            "Parser.java",
            (
                b"class Parser {\n"
                b"  /**\n"
                b"   * Parse a rate sheet.\n"
                b"   *\n"
                b"   * @param path uploaded file\n"
                b"   */\n"
                b"  void parse(String path) {}\n"
                b"}\n"
            ),
            "parse",
        )

        assert node.extra["docstring"] == "Parse a rate sheet."

    def test_javadoc_html_paragraph_boundary_excludes_details(self):
        node = _parsed_node(
            "Parser.java",
            (
                b"class Parser {\n"
                b"  /** Parse a {@code RateSheet}.\n"
                b"   * <p>Implementation details must not be embedded.\n"
                b"   */\n"
                b"  void parse() {}\n"
                b"}\n"
            ),
            "parse",
        )

        assert node.extra["docstring"] == "Parse a RateSheet."

    def test_csharp_xml_summary_crosses_attribute(self):
        node = _parsed_node(
            "Parser.cs",
            (
                b"class Parser {\n"
                b"  /// <summary>\n"
                b"  /// Parse a rate sheet.\n"
                b"  /// </summary>\n"
                b"  [Obsolete]\n"
                b"  public void Parse() {}\n"
                b"}\n"
            ),
            "Parse",
        )

        assert node.extra["docstring"] == "Parse a rate sheet."

    def test_doxygen_comment_attaches_to_cpp_template_function(self):
        node = _parsed_node(
            "parser.cpp",
            (
                b"/** Parse a typed sheet. */\n"
                b"template <typename T>\n"
                b"T parse(T value) { return value; }\n"
            ),
            "parse",
        )

        assert node.extra["docstring"] == "Parse a typed sheet."

    def test_doxygen_brief_marker_is_not_embedded_as_prose(self):
        node = _parsed_node(
            "parser.cpp",
            b"/** @brief Parse a typed sheet. */\nint parse() { return 0; }\n",
            "parse",
        )

        assert node.extra["docstring"] == "Parse a typed sheet."

    def test_summary_is_bounded_to_four_hundred_characters(self):
        node = _parsed_node(
            "module.py",
            ('def f():\n    """' + ("word " * 200) + '"""\n').encode(),
            "f",
        )

        assert len(node.extra["docstring"]) == 400


class TestDocumentationEmbeddingText:
    @staticmethod
    def _node(extra: dict) -> GraphNode:
        return GraphNode(
            id=1,
            kind="Function",
            name="parse_rates",
            qualified_name="module.py::parse_rates",
            file_path="module.py",
            line_start=1,
            line_end=2,
            language="python",
            parent_name=None,
            params=None,
            return_type=None,
            is_test=False,
            file_hash=None,
            extra=extra,
        )

    def test_text_includes_normalized_bounded_summary_deterministically(self):
        summary = "  Parse\n uploaded\t rate sheets.  " + ("x" * 500)

        first = _node_to_text(self._node({"docstring": summary}))
        second = _node_to_text(self._node({"docstring": summary}))

        assert first == second
        assert "Parse uploaded rate sheets." in first
        assert ("x" * 401) not in first

    def test_non_string_legacy_metadata_is_ignored(self):
        plain = _node_to_text(self._node({}))
        malformed = _node_to_text(self._node({"docstring": {"unexpected": "shape"}}))

        assert malformed == plain


def test_docstring_metadata_survives_full_and_incremental_persistence(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "module.py"
    source.write_text('def parse():\n    """Old summary."""\n', encoding="utf-8")
    store = GraphStore(tmp_path / "graph.db")
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")

    try:
        with patch(
            "code_review_graph.incremental.collect_all_files",
            return_value=["module.py"],
        ):
            full_build(repo, store)
        full_node = next(
            node for node in store.get_nodes_by_file(str(source)) if node.name == "parse"
        )
        assert full_node.extra["docstring"] == "Old summary."

        source.write_text('def parse():\n    """New summary."""\n', encoding="utf-8")
        incremental_update(repo, store, changed_files=["module.py"])
        changed_node = next(
            node for node in store.get_nodes_by_file(str(source)) if node.name == "parse"
        )
        assert changed_node.extra["docstring"] == "New summary."

        source.write_text("def parse():\n    return None\n", encoding="utf-8")
        incremental_update(repo, store, changed_files=["module.py"])
        removed_node = next(
            node for node in store.get_nodes_by_file(str(source)) if node.name == "parse"
        )
        assert "docstring" not in removed_node.extra
    finally:
        store.close()
