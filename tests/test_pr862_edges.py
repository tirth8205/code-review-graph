"""Per-name Kotlin import attribution after folded trailing comments (#862)."""

from pathlib import Path

from code_review_graph.parser import CodeParser


def test_import_name_with_trailing_kdoc_still_attributes_calls(tmp_path: Path) -> None:
    dep = tmp_path / "dep"
    dep.mkdir()
    (dep / "Helper.kt").write_text(
        "package dep\n\nfun helper(): Int = 42\n",
        encoding="utf-8",
    )
    source = tmp_path / "Main.kt"
    source.write_text(
        "package app\n\n"
        "import dep.helper\n\n"
        "/** document the consumer below the import */\n"
        "class Main {\n"
        "    fun run(): Int = helper()\n"
        "}\n",
        encoding="utf-8",
    )

    _, edges = CodeParser(tmp_path).parse_file(source)
    calls = [edge for edge in edges if edge.kind == "CALLS"]

    assert len(calls) == 1
    assert calls[0].target.endswith("Helper.kt::helper")
