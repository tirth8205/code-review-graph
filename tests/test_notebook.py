"""Tests for Jupyter notebook (.ipynb) parsing."""

import json
from pathlib import Path

import pytest

from code_review_graph.parser import _SQL_TABLE_RE, CodeParser

FIXTURES = Path(__file__).parent / "fixtures"


class TestNotebookParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(
            FIXTURES / "sample_notebook.ipynb",
        )

    def test_detects_notebook(self):
        assert self.parser.detect_language(Path("analysis.ipynb")) == "notebook"

    def test_file_node_uses_python_language(self):
        file_node = [n for n in self.nodes if n.kind == "File"][0]
        assert file_node.language == "python"

    def test_parses_python_functions(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "add" in names
        assert "multiply" in names

    def test_parses_python_classes(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "DataProcessor" in names

    def test_parses_class_methods(self):
        methods = [
            n for n in self.nodes
            if n.kind == "Function" and n.parent_name == "DataProcessor"
        ]
        names = {m.name for m in methods}
        assert "__init__" in names
        assert "process" in names

    def test_cell_index_tracking(self):
        funcs = {n.name: n for n in self.nodes if n.kind == "Function"}
        # add and multiply are in cell index 2 (3rd code cell, 0-based)
        assert funcs["add"].extra.get("cell_index") == 2
        assert funcs["multiply"].extra.get("cell_index") == 2
        # DataProcessor.__init__ is in cell index 3
        assert funcs["__init__"].extra.get("cell_index") == 3

    def test_cross_cell_calls(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        targets = {e.target.split("::")[-1] for e in calls}
        # process() calls add() and multiply() from different cells
        assert "add" in targets
        assert "multiply" in targets

    def test_imports_from_cells(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = {e.target for e in imports}
        assert "os" in targets
        assert "pathlib" in targets
        assert "math" in targets

    def test_skips_magic_commands(self):
        # %pip and !ls lines should be filtered out — no parse errors
        funcs = [n for n in self.nodes if n.kind == "Function"]
        assert len(funcs) >= 4  # add, multiply, __init__, process

    def test_empty_notebook(self):
        nb = {
            "cells": [],
            "metadata": {"kernelspec": {"language": "python"}},
            "nbformat": 4,
        }
        source = json.dumps(nb).encode("utf-8")
        nodes, edges = self.parser.parse_bytes(
            Path("empty.ipynb"), source,
        )
        assert len(nodes) == 1
        assert nodes[0].kind == "File"
        assert edges == []

    def test_non_python_kernel(self):
        nb = {
            "cells": [
                {"cell_type": "code", "source": ["println(\"hello\")"], "outputs": []},
            ],
            "metadata": {"kernelspec": {"language": "scala"}},
            "nbformat": 4,
        }
        source = json.dumps(nb).encode("utf-8")
        nodes, edges = self.parser.parse_bytes(
            Path("scala_notebook.ipynb"), source,
        )
        assert nodes == []
        assert edges == []

    def test_malformed_json(self):
        source = b"not valid json {{"
        nodes, edges = self.parser.parse_bytes(
            Path("bad.ipynb"), source,
        )
        assert nodes == []
        assert edges == []


class TestSqlTableExtraction:
    def test_from_clause(self):
        matches = _SQL_TABLE_RE.findall("SELECT * FROM my_table")
        assert "my_table" in matches

    def test_qualified_table(self):
        matches = _SQL_TABLE_RE.findall("SELECT * FROM catalog.schema.table")
        assert "catalog.schema.table" in matches

    def test_join(self):
        matches = _SQL_TABLE_RE.findall(
            "SELECT * FROM a JOIN b ON a.id = b.id"
        )
        assert "a" in matches
        assert "b" in matches

    def test_insert_into(self):
        matches = _SQL_TABLE_RE.findall("INSERT INTO target_table VALUES (1)")
        assert "target_table" in matches

    def test_create_table(self):
        matches = _SQL_TABLE_RE.findall("CREATE TABLE my_db.new_table (id INT)")
        assert "my_db.new_table" in matches

    def test_create_or_replace_view(self):
        matches = _SQL_TABLE_RE.findall(
            "CREATE OR REPLACE VIEW my_view AS SELECT 1"
        )
        assert "my_view" in matches

    def test_insert_overwrite(self):
        matches = _SQL_TABLE_RE.findall(
            "INSERT OVERWRITE catalog.schema.tbl SELECT * FROM src"
        )
        assert "catalog.schema.tbl" in matches
        assert "src" in matches

    def test_backtick_quoted(self):
        matches = _SQL_TABLE_RE.findall("SELECT * FROM `my-catalog`.`schema`.`table`")
        assert any("my-catalog" in m for m in matches)

    def test_no_table_refs(self):
        matches = _SQL_TABLE_RE.findall("SELECT 1 + 1")
        assert matches == []

    def test_case_insensitive(self):
        matches = _SQL_TABLE_RE.findall("select * from My_Table")
        assert "My_Table" in matches


class TestDatabricksNotebookParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(
            FIXTURES / "sample_databricks_notebook.ipynb",
        )

    def test_parses_python_functions_from_magic(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "transform_data" in names
        assert "process_results" in names

    def test_extracts_sql_table_references(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = {e.target for e in imports}
        assert "catalog.schema.raw_data" in targets
        assert "catalog.schema.lookup" in targets
        assert "catalog.schema.output" in targets

    def test_skips_scala_cells(self):
        names = {n.name for n in self.nodes if n.kind == "Function"}
        assert "x" not in names

    def test_skips_md_cells(self):
        func_count = len([n for n in self.nodes if n.kind == "Function"])
        assert func_count == 3  # transform_data + process_results + clean_data (R cell)

    def test_default_language_for_unmagicked_cell(self):
        """Cell 6 has no magic prefix — should use kernel default (python)."""
        funcs = {n.name: n for n in self.nodes if n.kind == "Function"}
        assert "process_results" in funcs

    def test_cell_index_tracking(self):
        funcs = {n.name: n for n in self.nodes if n.kind == "Function"}
        assert funcs["transform_data"].extra.get("cell_index") == 1
        assert funcs["process_results"].extra.get("cell_index") == 6

    def test_cross_cell_python_calls(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        targets = {e.target.split("::")[-1] for e in calls}
        assert "transform_data" in targets


class TestDatabricksPyNotebook:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(
            FIXTURES / "sample_databricks_export.py",
        )

    def test_detects_databricks_header(self):
        """Should parse as notebook, not regular Python."""
        file_node = [n for n in self.nodes if n.kind == "File"][0]
        assert file_node.extra.get("notebook_format") == "databricks_py"

    def test_parses_python_functions(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "load_config" in names
        assert "process_events" in names

    def test_extracts_sql_tables(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = {e.target for e in imports}
        assert "bronze.events" in targets
        assert "silver.users" in targets
        assert "gold.summary" in targets
        assert "silver.processed" in targets

    def test_skips_magic_md_cells(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert len(names) == 3  # load_config + process_events + summarize_data (R cell)

    def test_cell_index_tracking(self):
        funcs = {n.name: n for n in self.nodes if n.kind == "Function"}
        assert funcs["load_config"].extra.get("cell_index") == 0
        assert funcs["process_events"].extra.get("cell_index") == 4

    def test_python_imports(self):
        imports = [
            e for e in self.edges
            if e.kind == "IMPORTS_FROM" and e.target in ("os", "pathlib")
        ]
        targets = {e.target for e in imports}
        assert "os" in targets
        assert "pathlib" in targets

    def test_cross_cell_calls(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        targets = {e.target.split("::")[-1] for e in calls}
        assert "load_config" in targets

    def test_regular_py_not_affected(self):
        """A regular .py file (no header) should parse normally."""
        source = b"def hello():\n    return 'hi'\n"
        nodes, edges = self.parser.parse_bytes(Path("regular.py"), source)
        funcs = [n for n in nodes if n.kind == "Function"]
        assert len(funcs) == 1
        assert funcs[0].name == "hello"
        file_node = [n for n in nodes if n.kind == "File"][0]
        assert "notebook_format" not in file_node.extra

    def test_databricks_header_crlf_line_endings(self):
        """Regression guard for #239 bug 2: the Databricks auto-detection
        must handle ``\\r\\n`` (CRLF) line endings as well as ``\\n`` (LF).

        On Windows, ``git config core.autocrlf=true`` (the default) rewrites
        text files to CRLF on checkout.  Before the fix, the detection
        ``source.startswith(b"# Databricks notebook source\\n")`` matched
        only LF, so Windows checkouts silently parsed Databricks exports
        as plain Python — missing SQL-cell table extraction, cell-index
        metadata, and the ``notebook_format`` tag.
        """
        # Exact byte sequence a Windows checkout produces.
        crlf_source = (
            b"# Databricks notebook source\r\n"
            b"# COMMAND ----------\r\n"
            b"\r\n"
            b"def crlf_fn():\r\n"
            b"    return 1\r\n"
        )
        nodes, edges = self.parser.parse_bytes(Path("nb.py"), crlf_source)
        file_nodes = [n for n in nodes if n.kind == "File"]
        assert len(file_nodes) == 1
        assert file_nodes[0].extra.get("notebook_format") == "databricks_py", (
            "Databricks header with CRLF line endings was not detected; "
            "the auto-detect check is still hard-coded to \\n only"
        )
        # The body function must still be extracted through the notebook path.
        funcs = [n for n in nodes if n.kind == "Function"]
        assert any(f.name == "crlf_fn" for f in funcs)

    def test_databricks_header_lf_line_endings_still_work(self):
        """Regression guard for #239 bug 2: ensure the CRLF fix does not
        break the existing LF path (pre-existing behavior)."""
        lf_source = (
            b"# Databricks notebook source\n"
            b"# COMMAND ----------\n"
            b"\n"
            b"def lf_fn():\n"
            b"    return 1\n"
        )
        nodes, edges = self.parser.parse_bytes(Path("nb.py"), lf_source)
        file_nodes = [n for n in nodes if n.kind == "File"]
        assert len(file_nodes) == 1
        assert file_nodes[0].extra.get("notebook_format") == "databricks_py"

    def test_databricks_header_prefix_false_positive_rejected(self):
        """Regression guard for #239 bug 2: a file whose first line only
        *starts with* the Databricks phrase but has extra characters must
        NOT be detected as a Databricks export.  Protects against the
        naive fix of using ``startswith`` without checking the line end.
        """
        false_positive = (
            b"# Databricks notebook source code examples\n"
            b"def hello(): return 1\n"
        )
        nodes, edges = self.parser.parse_bytes(Path("doc.py"), false_positive)
        file_nodes = [n for n in nodes if n.kind == "File"]
        assert len(file_nodes) == 1
        assert "notebook_format" not in file_nodes[0].extra, (
            "a regular .py file whose first comment happens to start with "
            "'# Databricks notebook source' must not trigger Databricks "
            "parsing"
        )


class TestRKernelNotebook:
    def setup_method(self):
        self.parser = CodeParser()
        nb = {
            "cells": [
                {
                    "cell_type": "code",
                    "source": [
                        "library(dplyr)\n",
                    ],
                    "outputs": [],
                },
                {
                    "cell_type": "code",
                    "source": [
                        "clean_data <- function(df) {\n",
                        "  df %>% filter(!is.na(value))\n",
                        "}\n",
                    ],
                    "outputs": [],
                },
            ],
            "metadata": {"kernelspec": {"language": "r"}},
            "nbformat": 4,
        }
        source = json.dumps(nb).encode("utf-8")
        self.nodes, self.edges = self.parser.parse_bytes(
            Path("analysis.ipynb"), source,
        )

    def test_r_kernel_not_skipped(self):
        """R-kernel notebooks should now be parsed, not skipped."""
        assert len(self.nodes) >= 1
        file_node = [n for n in self.nodes if n.kind == "File"][0]
        assert file_node.language == "r"

    @pytest.mark.xfail(reason="Requires R parser mappings from PR #43")
    def test_r_kernel_detects_functions(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "clean_data" in names

    @pytest.mark.xfail(reason="Requires R parser mappings from PR #43")
    def test_r_kernel_detects_imports(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = {e.target for e in imports}
        assert "dplyr" in targets


class TestNotebookEdgeCases:
    def setup_method(self):
        self.parser = CodeParser()

    def test_databricks_header_not_on_line_1(self):
        """Header on line 2 should be treated as regular Python."""
        source = b"# comment\n# Databricks notebook source\ndef foo(): pass\n"
        nodes, edges = self.parser.parse_bytes(Path("not_db.py"), source)
        file_node = [n for n in nodes if n.kind == "File"][0]
        assert "notebook_format" not in file_node.extra

    def test_databricks_py_no_command_delimiters(self):
        """Header present but no COMMAND delimiters — single Python cell."""
        source = b"# Databricks notebook source\ndef foo():\n    return 1\n"
        nodes, edges = self.parser.parse_bytes(Path("single_cell.py"), source)
        funcs = [n for n in nodes if n.kind == "Function"]
        assert len(funcs) == 1
        assert funcs[0].name == "foo"

    def test_empty_databricks_cells(self):
        """Cells with only magic/shell lines should be skipped."""
        nb = {
            "cells": [
                {"cell_type": "code", "source": ["%pip install foo\n"], "outputs": []},
                {"cell_type": "code", "source": ["!ls\n"], "outputs": []},
                {"cell_type": "code", "source": ["def real(): pass\n"], "outputs": []},
            ],
            "metadata": {"kernelspec": {"language": "python"}},
            "nbformat": 4,
        }
        source = json.dumps(nb).encode("utf-8")
        nodes, edges = self.parser.parse_bytes(Path("sparse.ipynb"), source)
        funcs = [n for n in nodes if n.kind == "Function"]
        assert len(funcs) == 1
        assert funcs[0].name == "real"

    def test_sql_cell_no_tables(self):
        """SQL cell with no table refs should produce no edges."""
        nb = {
            "cells": [
                {"cell_type": "code", "source": ["%sql\n", "SELECT 1 + 1\n"], "outputs": []},
            ],
            "metadata": {"kernelspec": {"language": "python"}},
            "nbformat": 4,
        }
        source = json.dumps(nb).encode("utf-8")
        nodes, edges = self.parser.parse_bytes(Path("no_tables.ipynb"), source)
        imports = [e for e in edges if e.kind == "IMPORTS_FROM"]
        assert imports == []

    def test_conflicting_kernel_metadata(self):
        """kernelspec.language takes precedence over language_info.name."""
        nb = {
            "cells": [
                {"cell_type": "code", "source": ["def foo(): pass\n"], "outputs": []},
            ],
            "metadata": {
                "kernelspec": {"language": "python"},
                "language_info": {"name": "r"},
            },
            "nbformat": 4,
        }
        source = json.dumps(nb).encode("utf-8")
        nodes, edges = self.parser.parse_bytes(Path("conflict.ipynb"), source)
        file_node = [n for n in nodes if n.kind == "File"][0]
        assert file_node.language == "python"
