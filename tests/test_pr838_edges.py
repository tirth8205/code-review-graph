"""Edge-case regressions for SQL ``IF NOT EXISTS`` handling (PR #838 / issue #820).

Stresses ``_SQL_TABLE_RE`` and the ``.sql`` table-reference pass beyond the
PR's own coverage: case and whitespace variants, quoted and qualified names,
identifiers that merely start with clause keywords, keyword-filter precision,
unicode names, and scale.
"""

from pathlib import Path

from code_review_graph.parser import _SQL_KEYWORDS, _SQL_TABLE_RE, CodeParser


class TestIfNotExistsRegex:
    def test_lowercase_clause(self):
        assert _SQL_TABLE_RE.findall(
            "create table if not exists foo (id INT);"
        ) == ["foo"]

    def test_mixed_case_clause(self):
        assert _SQL_TABLE_RE.findall(
            "Create Table If Not Exists Foo (id INT);"
        ) == ["Foo"]

    def test_newlines_and_tabs_inside_clause(self):
        assert _SQL_TABLE_RE.findall(
            "CREATE TABLE\n  IF\tNOT\nEXISTS bar (id INT);"
        ) == ["bar"]

    def test_backtick_quoted_name_after_clause(self):
        assert _SQL_TABLE_RE.findall(
            "CREATE TABLE IF NOT EXISTS `my table` (id INT);"
        ) == ["`my table`"]

    def test_schema_qualified_name_after_clause(self):
        assert _SQL_TABLE_RE.findall(
            "CREATE TABLE IF NOT EXISTS db.schema.tbl (id INT);"
        ) == ["db.schema.tbl"]

    def test_or_replace_combined_with_if_not_exists(self):
        # BigQuery-style CREATE OR REPLACE TABLE IF NOT EXISTS.
        assert _SQL_TABLE_RE.findall(
            "CREATE OR REPLACE TABLE IF NOT EXISTS bq_tbl (id INT);"
        ) == ["bq_tbl"]

    def test_view_lowercase_clause(self):
        assert _SQL_TABLE_RE.findall(
            "create view if not exists v1 as select 1;"
        ) == ["v1"]

    def test_names_starting_with_clause_keywords_are_not_swallowed(self):
        # The optional clause must not eat identifiers that merely start
        # with IF / NOT / EXISTS.
        for name in ("ifnotexists_log", "if_not_exists", "ifs", "nothing", "existsq"):
            sql = f"CREATE TABLE {name} (id INT);"
            assert _SQL_TABLE_RE.findall(sql) == [name], sql

    def test_unicode_table_name(self):
        assert _SQL_TABLE_RE.findall(
            "CREATE TABLE IF NOT EXISTS façade_übersicht (id INT);"
        ) == ["façade_übersicht"]

    def test_plain_statements_unchanged(self):
        assert _SQL_TABLE_RE.findall("CREATE TABLE t (id INT);") == ["t"]
        assert _SQL_TABLE_RE.findall(
            "INSERT OVERWRITE cat.sch.tbl SELECT * FROM src"
        ) == ["cat.sch.tbl", "src"]

    def test_new_keywords_registered(self):
        assert {"IF", "NOT", "EXISTS"} <= _SQL_KEYWORDS


class TestIfNotExistsEndToEnd:
    def setup_method(self):
        self.parser = CodeParser()

    def _imports(self, sql: bytes) -> list:
        _, edges = self.parser.parse_bytes(Path("pr838_schema.sql"), sql)
        return [e for e in edges if e.kind == "IMPORTS_FROM"]

    def test_many_idempotent_creates_all_recorded_with_lines(self):
        n = 200
        sql = b"".join(
            b"CREATE TABLE IF NOT EXISTS tbl_%d (id INT);\n" % i for i in range(n)
        )
        imports = self._imports(sql)
        assert [e.target for e in imports] == [f"tbl_{i}" for i in range(n)]
        assert [e.line for e in imports] == list(range(1, n + 1))

    def test_create_then_read_dedups_to_single_edge(self):
        imports = self._imports(
            b"CREATE TABLE IF NOT EXISTS a1 (id INT);\n"
            b"CREATE TABLE IF NOT EXISTS a2 (id INT);\n"
            b"SELECT * FROM a1 JOIN a2 ON a1.id = a2.id;\n"
        )
        assert [e.target for e in imports] == ["a1", "a2"]

    def test_keyword_filter_is_exact_match_not_prefix(self):
        # Names that contain filter keywords as a prefix must survive.
        imports = self._imports(
            b"SELECT * FROM notifications;\n"
            b"SELECT * FROM if_config;\n"
            b"SELECT * FROM exists_flags;\n"
        )
        assert [e.target for e in imports] == [
            "notifications", "if_config", "exists_flags",
        ]

    def test_where_not_exists_subquery_adds_no_keyword_edges(self):
        imports = self._imports(
            b"SELECT * FROM orders o WHERE NOT EXISTS "
            b"(SELECT 1 FROM refunds r WHERE r.oid = o.id);\n"
        )
        assert [e.target for e in imports] == ["orders", "refunds"]

    def test_no_space_before_backtick_never_emits_if_edge(self):
        # MySQL allows the quoted name to abut the clause. The regex cannot
        # see past the missing space, but the keyword fallback must ensure
        # no bogus "IF" edge is emitted (missing edge, not a wrong one).
        imports = self._imports(b"CREATE TABLE IF NOT EXISTS`t`(id INT);\n")
        assert all(e.target.upper() != "IF" for e in imports)

    def test_qualified_create_strips_schema_prefix(self):
        imports = self._imports(
            b"CREATE TABLE IF NOT EXISTS warehouse.public.facts (id INT);\n"
        )
        assert [e.target for e in imports] == ["facts"]
