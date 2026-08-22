"""Regression tests for Kotlin import extraction.

tree-sitter-kotlin folds any comment following the *last* import into that
``import_header`` node. Without a dedicated Kotlin branch in
``_extract_import`` the node text was recorded verbatim as the module name,
so an import trailed by a long KDoc block produced a module name containing
the whole comment. ``_do_resolve_module`` then joined it into a filesystem
path and called ``os.stat``, raising ``OSError: [Errno 63] File name too
long`` and failing the whole file.
"""

from pathlib import Path

from code_review_graph.parser import CodeParser


def _import_targets(repo_root: Path, source_file: Path) -> set[str]:
    _, edges = CodeParser(repo_root).parse_file(source_file)
    return {edge.target for edge in edges if edge.kind == "IMPORTS_FROM"}


def _write_kotlin(tmp_path: Path, body: str) -> Path:
    source = tmp_path / "Main.kt"
    source.write_text(body, encoding="utf-8")
    return source


def test_plain_import_records_module_name(tmp_path: Path) -> None:
    source = _write_kotlin(
        tmp_path,
        "package app\n\nimport kotlinx.coroutines.delay\n\nclass Main\n",
    )

    assert "kotlinx.coroutines.delay" in _import_targets(tmp_path, source)


def test_last_import_ignores_trailing_block_comment(tmp_path: Path) -> None:
    source = _write_kotlin(
        tmp_path,
        "package app\n\n"
        "import javax.inject.Inject\n\n"
        "/** Doc comment attached to the class below. */\n"
        "class Main\n",
    )

    targets = _import_targets(tmp_path, source)

    assert "javax.inject.Inject" in targets
    assert not any("/**" in target for target in targets)


def test_last_import_ignores_trailing_line_comment(tmp_path: Path) -> None:
    source = _write_kotlin(
        tmp_path,
        "package app\n\n"
        "import javax.inject.Inject\n\n"
        "// Explanatory note\n"
        "class Main\n",
    )

    targets = _import_targets(tmp_path, source)

    assert "javax.inject.Inject" in targets
    assert not any("//" in target for target in targets)


def test_only_trailing_comment_of_last_import_is_dropped(tmp_path: Path) -> None:
    source = _write_kotlin(
        tmp_path,
        "package app\n\n"
        "import kotlinx.coroutines.delay\n"
        "import javax.inject.Inject\n\n"
        "/** Doc comment. */\n"
        "class Main\n",
    )

    targets = _import_targets(tmp_path, source)

    assert {"kotlinx.coroutines.delay", "javax.inject.Inject"} <= targets


def test_aliased_import_records_original_module(tmp_path: Path) -> None:
    source = _write_kotlin(
        tmp_path,
        "package app\n\nimport kotlinx.coroutines.delay as pause\n\nclass Main\n",
    )

    assert "kotlinx.coroutines.delay" in _import_targets(tmp_path, source)


def test_wildcard_import_keeps_star_suffix(tmp_path: Path) -> None:
    source = _write_kotlin(
        tmp_path,
        "package app\n\nimport kotlinx.coroutines.*\n\nclass Main\n",
    )

    assert "kotlinx.coroutines.*" in _import_targets(tmp_path, source)


def test_wildcard_import_with_trailing_comment_keeps_star_suffix(
    tmp_path: Path,
) -> None:
    source = _write_kotlin(
        tmp_path,
        "package app\n\n"
        "import kotlinx.coroutines.*\n\n"
        "/** Doc comment. */\n"
        "class Main\n",
    )

    targets = _import_targets(tmp_path, source)

    assert "kotlinx.coroutines.*" in targets
    assert not any("/**" in target for target in targets)


def test_long_trailing_kdoc_does_not_break_parsing(tmp_path: Path) -> None:
    """The original crash: a KDoc long enough to overflow the path limit."""
    source = _write_kotlin(
        tmp_path,
        "package app\n\n"
        "import javax.inject.Inject\n\n"
        "/**\n"
        + "".join(f" * {'detail ' * 12}\n" for _ in range(40))
        + " */\n"
        "class Main\n",
    )

    targets = _import_targets(tmp_path, source)

    assert "javax.inject.Inject" in targets
    assert all(len(target) < 200 for target in targets)
