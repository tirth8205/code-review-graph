"""PHP include/require import edge regressions (PR #819)."""

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


def test_absolute_include_is_not_rebased_inside_repository(tmp_path: Path) -> None:
    fake = tmp_path / "__crg_missing_absolute_root__" / "helper.php"
    fake.parent.mkdir()
    fake.write_text("<?php function helper() {}", encoding="utf-8")
    source = tmp_path / "index.php"
    source.write_text(
        "<?php include '/__crg_missing_absolute_root__/helper.php';",
        encoding="utf-8",
    )
    _, edges = CodeParser(tmp_path).parse_file(source)
    assert [edge.target for edge in edges if edge.kind == "IMPORTS_FROM"] == [
        "/__crg_missing_absolute_root__/helper.php",
    ]


def test_absolute_include_inside_repository_resolves(tmp_path: Path) -> None:
    dependency = tmp_path / "helper.php"
    dependency.write_text("<?php function helper() {}", encoding="utf-8")
    source = tmp_path / "index.php"
    source.write_text(f"<?php include '{dependency.as_posix()}';", encoding="utf-8")
    _, edges = CodeParser(tmp_path).parse_file(source)
    assert [edge.target for edge in edges if edge.kind == "IMPORTS_FROM"] == [
        dependency.resolve().as_posix(),
    ]
