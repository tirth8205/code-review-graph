"""PHP include/require import edge regressions (#819)."""

from pathlib import Path

from code_review_graph.parser import CodeParser


def test_require_once_resolves_relative_php_file(tmp_path: Path) -> None:
    includes = tmp_path / "includes"
    includes.mkdir()
    dependency = includes / "conexao.php"
    dependency.write_text("<?php\nfunction connect() {}\n", encoding="utf-8")
    source = tmp_path / "index.php"
    source.write_text(
        "<?php\nrequire_once 'includes/conexao.php';\nconnect();\n",
        encoding="utf-8",
    )

    _, edges = CodeParser(tmp_path).parse_file(source)
    imports = [edge for edge in edges if edge.kind == "IMPORTS_FROM"]

    assert len(imports) == 1
    assert imports[0].target == dependency.resolve().as_posix()


def test_include_once_resolves_relative_php_file(tmp_path: Path) -> None:
    dependency = tmp_path / "helper.php"
    dependency.write_text("<?php\nfunction helper() {}\n", encoding="utf-8")
    source = tmp_path / "index.php"
    source.write_text(
        "<?php\ninclude_once 'helper.php';\nhelper();\n",
        encoding="utf-8",
    )

    _, edges = CodeParser(tmp_path).parse_file(source)
    imports = [edge for edge in edges if edge.kind == "IMPORTS_FROM"]

    assert len(imports) == 1
    assert imports[0].target == dependency.resolve().as_posix()
