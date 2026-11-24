"""Recover line ranges for PostgreSQL's dollar-quoted procedures (#744)."""

from pathlib import Path

import pytest

from code_review_graph.changes import analyze_changes
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import CodeParser


@pytest.mark.parametrize("tag", ["$$", "$procedure_body$"])
@pytest.mark.parametrize("language_after", [False, True])
def test_dollar_quoted_procedure_covers_its_body(tag, language_after):
    language_before = "" if language_after else "LANGUAGE plpgsql "
    trailer = "\nLANGUAGE plpgsql" if language_after else ""
    statement = (
        f"CREATE OR REPLACE PROCEDURE app.refresh() {language_before}AS {tag}\n"
        "BEGIN\n"
        "  PERFORM app.lookup(1);\n"
        "  PERFORM 'CREATE TABLE not_a_statement';\n"
        "END;\n"
        f"{tag}{trailer};"
    )
    source = (statement + "\nCREATE TABLE next_table(id int);\n").encode()
    nodes, _ = CodeParser().parse_bytes(Path("migration.sql"), source)
    [proc] = [node for node in nodes if node.extra.get("sql_kind") == "procedure"]

    assert (proc.name, proc.line_start, proc.line_end) == (
        "refresh", 1, statement.count("\n") + 1,
    )
    assert next(node for node in nodes if node.name == "next_table").line_start == (
        proc.line_end + 1
    )


def test_change_inside_postgres_procedure_maps_to_its_symbol(tmp_path):
    migration = tmp_path / "migration.sql"
    migration.write_text(
        "CREATE PROCEDURE refresh() AS $$\n"
        "BEGIN\n"
        "  PERFORM app.lookup(1);\n"
        "END;\n"
        "$$ LANGUAGE plpgsql;\n"
        "CREATE PROCEDURE clear() AS $$ SELECT 1; $$ LANGUAGE sql;\n",
        encoding="utf-8",
    )
    with GraphStore(tmp_path / ".code-review-graph" / "graph.db") as store:
        full_build(tmp_path, store)
        path = migration.as_posix()
        analysis = analyze_changes(store, [path], {path: [(3, 3)]})

    assert [symbol["name"] for symbol in analysis["changed_functions"]] == ["refresh"]
