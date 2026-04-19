"""Tests for the raw SQL query command (code_review_graph/query.py)."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from code_review_graph.query import (
    QueryError,
    _MAX_LIMIT,
    _validate_sql,
    format_table,
    run_query,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fixture_db(tmp_path: Path) -> Path:
    """Minimal graph DB with nodes and edges tables for query tests."""
    db_path = tmp_path / "graph.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY,
            kind TEXT,
            name TEXT,
            qualified_name TEXT,
            file_path TEXT,
            line_start INTEGER,
            line_end INTEGER,
            language TEXT,
            parent_name TEXT,
            is_test INTEGER DEFAULT 0
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY,
            kind TEXT,
            source_qualified TEXT,
            target_qualified TEXT,
            file_path TEXT,
            line INTEGER
        );
    """)
    conn.executemany(
        """INSERT INTO nodes
           (kind, name, qualified_name, file_path, line_start, line_end,
            language, parent_name, is_test)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        [
            ("Function", "foo", "/a.py::foo", "/a.py", 1, 10, "python", None, 0),
            ("Function", "bar", "/a.py::bar", "/a.py", 11, 20, "python", None, 0),
            ("Function", "test_foo", "/test_a.py::test_foo", "/test_a.py", 1, 5,
             "python", None, 1),
        ],
    )
    conn.executemany(
        """INSERT INTO edges (kind, source_qualified, target_qualified, file_path, line)
           VALUES (?,?,?,?,?)""",
        [
            ("CALLS", "/a.py::bar", "/a.py::foo", "/a.py", 15),
            ("CALLS", "/test_a.py::test_foo", "/a.py::foo", "/test_a.py", 3),
        ],
    )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# _validate_sql
# ---------------------------------------------------------------------------


class TestValidateSql:
    def test_select_allowed(self):
        _validate_sql("SELECT 1")

    def test_select_with_whitespace_allowed(self):
        _validate_sql("  \n  SELECT * FROM nodes")

    def test_cte_allowed(self):
        _validate_sql("WITH cte AS (SELECT 1) SELECT * FROM cte")

    def test_insert_rejected(self):
        with pytest.raises(QueryError) as exc:
            _validate_sql("INSERT INTO nodes VALUES (1)")
        assert exc.value.error_type == "disallowed_statement"

    def test_update_rejected(self):
        with pytest.raises(QueryError) as exc:
            _validate_sql("UPDATE nodes SET name = 'x'")
        assert exc.value.error_type == "disallowed_statement"

    def test_delete_rejected(self):
        with pytest.raises(QueryError):
            _validate_sql("DELETE FROM nodes")

    def test_create_rejected(self):
        with pytest.raises(QueryError):
            _validate_sql("CREATE TABLE t (id INTEGER)")

    def test_drop_rejected(self):
        with pytest.raises(QueryError):
            _validate_sql("DROP TABLE nodes")

    def test_alter_rejected(self):
        with pytest.raises(QueryError):
            _validate_sql("ALTER TABLE nodes ADD COLUMN x TEXT")

    def test_attach_rejected(self):
        with pytest.raises(QueryError):
            _validate_sql("ATTACH DATABASE 'other.db' AS other")

    def test_pragma_rejected(self):
        with pytest.raises(QueryError) as exc:
            _validate_sql("PRAGMA journal_mode")
        assert exc.value.error_type == "disallowed_pragma"

    def test_keyword_in_string_literal_allowed(self):
        _validate_sql("SELECT name FROM nodes WHERE name = 'CREATE TABLE'")

    def test_keyword_in_line_comment_allowed(self):
        _validate_sql("SELECT 1 -- DROP TABLE here")

    def test_keyword_in_block_comment_allowed(self):
        _validate_sql("SELECT 1 /* INSERT INTO t VALUES (1) */")

    def test_keyword_as_double_quoted_identifier_allowed(self):
        # Double-quoted identifiers are valid SQLite column aliases.
        # The regex approach would have false-positived here.
        _validate_sql('SELECT name AS "CREATE" FROM nodes')


# ---------------------------------------------------------------------------
# run_query
# ---------------------------------------------------------------------------


class TestRunQuery:
    def test_basic_select(self, fixture_db: Path):
        rows = run_query(fixture_db, "SELECT name FROM nodes ORDER BY name")
        assert len(rows) == 3
        names = [r["name"] for r in rows]
        assert "foo" in names and "bar" in names and "test_foo" in names

    def test_column_names_present(self, fixture_db: Path):
        rows = run_query(fixture_db, "SELECT name, kind FROM nodes LIMIT 1")
        assert set(rows[0].keys()) == {"name", "kind"}

    def test_param_binding(self, fixture_db: Path):
        rows = run_query(
            fixture_db,
            "SELECT name FROM nodes WHERE kind = :kind AND is_test = 0",
            params={"kind": "Function"},
        )
        names = [r["name"] for r in rows]
        assert "test_foo" not in names
        assert set(names) == {"foo", "bar"}

    def test_limit_caps_rows(self, fixture_db: Path):
        rows = run_query(fixture_db, "SELECT * FROM nodes", limit=2)
        assert len(rows) == 2

    def test_limit_max_enforced(self, fixture_db: Path):
        rows = run_query(fixture_db, "SELECT * FROM nodes", limit=_MAX_LIMIT + 500)
        assert len(rows) <= _MAX_LIMIT

    def test_default_limit_applied(self, fixture_db: Path):
        # 3 rows in fixture, all returned within default limit of 100
        rows = run_query(fixture_db, "SELECT * FROM nodes")
        assert len(rows) == 3

    def test_cte_query_allowed(self, fixture_db: Path):
        rows = run_query(
            fixture_db,
            "WITH fns AS (SELECT * FROM nodes WHERE kind = 'Function') "
            "SELECT name FROM fns ORDER BY name",
        )
        assert len(rows) == 3

    def test_join_query(self, fixture_db: Path):
        rows = run_query(
            fixture_db,
            """
            SELECT DISTINCT n.name
            FROM nodes n
            JOIN edges e ON e.target_qualified = n.qualified_name
            WHERE e.kind = 'CALLS'
            """,
        )
        assert any(r["name"] == "foo" for r in rows)

    def test_insert_rejected(self, fixture_db: Path):
        with pytest.raises(QueryError) as exc:
            run_query(fixture_db, "INSERT INTO nodes (name) VALUES ('x')")
        assert exc.value.error_type == "disallowed_statement"

    def test_pragma_rejected(self, fixture_db: Path):
        with pytest.raises(QueryError) as exc:
            run_query(fixture_db, "PRAGMA journal_mode")
        assert exc.value.error_type == "disallowed_pragma"

    def test_nonexistent_db_raises_db_error(self, tmp_path: Path):
        bad_path = tmp_path / "missing.db"
        with pytest.raises(QueryError) as exc:
            run_query(bad_path, "SELECT 1")
        assert exc.value.error_type == "db_error"

    def test_sql_syntax_error(self, fixture_db: Path):
        with pytest.raises(QueryError) as exc:
            run_query(fixture_db, "SELEKT * FROM nodes")
        assert exc.value.error_type == "sql_syntax_error"

    def test_runtime_sql_error(self, fixture_db: Path):
        # Valid SELECT start but references a column that does not exist
        with pytest.raises(QueryError) as exc:
            run_query(fixture_db, "SELECT nonexistent_column FROM nodes")
        assert exc.value.error_type == "sql_error"

    def test_keyword_in_where_clause_allowed(self, fixture_db: Path):
        rows = run_query(
            fixture_db,
            "SELECT name FROM nodes WHERE name = 'CREATE TABLE'",
        )
        assert rows == []

    def test_empty_result(self, fixture_db: Path):
        rows = run_query(fixture_db, "SELECT * FROM nodes WHERE 1 = 0")
        assert rows == []

    def test_null_value_in_row(self, fixture_db: Path):
        # parent_name is NULL for top-level functions
        rows = run_query(
            fixture_db,
            "SELECT name, parent_name FROM nodes WHERE name = 'foo'",
        )
        assert rows[0]["parent_name"] is None

    def test_query_error_to_dict(self):
        err = QueryError("oops", "sql_error")
        d = err.to_dict()
        assert d == {"error": "oops", "type": "sql_error"}


# ---------------------------------------------------------------------------
# format_table
# ---------------------------------------------------------------------------


class TestFormatTable:
    def test_empty_returns_no_results(self):
        assert format_table([]) == "(no results)"

    def test_contains_column_names(self):
        rows = [{"name": "foo", "kind": "Function"}]
        out = format_table(rows)
        assert "name" in out
        assert "kind" in out

    def test_contains_row_values(self):
        rows = [{"name": "foo", "kind": "Function"}]
        out = format_table(rows)
        assert "foo" in out
        assert "Function" in out

    def test_null_rendered_as_NULL(self):
        rows = [{"name": None, "kind": "Function"}]
        out = format_table(rows)
        assert "NULL" in out

    def test_header_and_separator_same_width(self):
        rows = [
            {"col_a": "short", "col_b": "x"},
            {"col_a": "a_much_longer_value", "col_b": "y"},
        ]
        lines = format_table(rows).split("\n")
        # header, separator, then data rows
        assert len(lines) == 4
        assert len(lines[0]) == len(lines[1])

    def test_multi_row(self):
        rows = [{"n": "a"}, {"n": "b"}, {"n": "c"}]
        lines = format_table(rows).split("\n")
        # header + separator + 3 data rows = 5 lines
        assert len(lines) == 5

    def test_output_structure(self):
        rows = [{"a": "1", "b": "hello"}]
        lines = format_table(rows).split("\n")
        # header row contains column names
        assert "a" in lines[0] and "b" in lines[0]
        # separator row contains only dashes and -+- joins, no letters
        assert all(c in "-+ " for c in lines[1])
        # data row contains the actual values
        assert "1" in lines[2] and "hello" in lines[2]


# ---------------------------------------------------------------------------
# _handle_query (CLI layer)
# ---------------------------------------------------------------------------


def _make_args(repo_root: Path, sql: str, output_format: str = "json", **kwargs) -> argparse.Namespace:
    defaults = dict(
        sql=sql,
        file=None,
        param=[],
        limit=100,
        timeout=10,
        repo=str(repo_root),
        output_format=output_format,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


@pytest.fixture
def cli_db(tmp_path: Path) -> Path:
    """Fixture DB placed where get_db_path() expects it: <root>/.code-review-graph/graph.db."""
    data_dir = tmp_path / ".code-review-graph"
    data_dir.mkdir()
    db_path = data_dir / "graph.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY,
            kind TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, line_start INTEGER, line_end INTEGER,
            language TEXT, parent_name TEXT, is_test INTEGER DEFAULT 0
        );
    """)
    conn.executemany(
        "INSERT INTO nodes (kind, name, qualified_name, file_path, line_start, line_end, "
        "language, parent_name, is_test) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("Function", "foo", "/a.py::foo", "/a.py", 1, 10, "python", None, 0),
            ("Function", "bar", "/a.py::bar", "/a.py", 11, 20, "python", None, 0),
        ],
    )
    conn.commit()
    conn.close()
    return tmp_path


class TestHandleQuery:
    def test_json_output_is_valid_json(self, cli_db: Path, capsys):
        from code_review_graph.cli import _handle_query

        _handle_query(_make_args(cli_db, "SELECT name FROM nodes ORDER BY id"))
        out = capsys.readouterr().out
        data = json.loads(out)
        assert isinstance(data, list)

    def test_json_output_contains_row_values(self, cli_db: Path, capsys):
        from code_review_graph.cli import _handle_query

        _handle_query(_make_args(cli_db, "SELECT name FROM nodes WHERE name = 'foo'"))
        data = json.loads(capsys.readouterr().out)
        assert data == [{"name": "foo"}]

    def test_json_output_all_rows(self, cli_db: Path, capsys):
        from code_review_graph.cli import _handle_query

        _handle_query(_make_args(cli_db, "SELECT name FROM nodes ORDER BY id"))
        data = json.loads(capsys.readouterr().out)
        assert [r["name"] for r in data] == ["foo", "bar"]

    def test_table_output_renders_values(self, cli_db: Path, capsys):
        from code_review_graph.cli import _handle_query

        _handle_query(_make_args(cli_db, "SELECT name FROM nodes WHERE name = 'foo'",
                                 output_format="table"))
        out = capsys.readouterr().out
        assert "name" in out and "foo" in out
