"""Extreme edge cases for Kotlin import extraction (PR #824).

tree-sitter-kotlin folds comments that trail an import into the preceding
``import_header`` node. PR #824 reads the ``identifier`` child instead of the
node text so IMPORTS_FROM targets stay clean module names. These tests push
beyond the PR's own coverage: comments between imports, same-line comments,
comments that themselves look like imports or aliases, unicode identifiers,
CRLF endings, malformed imports, scale, and end-to-end file resolution.
"""

from pathlib import Path

from code_review_graph.parser import CodeParser


def _import_targets(repo_root: Path, source_file: Path) -> set[str]:
    _, edges = CodeParser(repo_root).parse_file(source_file)
    return {edge.target for edge in edges if edge.kind == "IMPORTS_FROM"}


def _write(tmp_path: Path, body: str, name: str = "Main.kt") -> Path:
    source = tmp_path / name
    source.write_text(body, encoding="utf-8")
    return source


def test_block_comment_between_imports_folds_into_first(tmp_path: Path) -> None:
    # The grammar folds "/* mid */" into the FIRST import_header, not just
    # comments after the last import. Both targets must stay clean.
    source = _write(
        tmp_path,
        "package app\n\n"
        "import aaa.First\n"
        "/* mid-list comment */\n"
        "import bbb.Second\n\n"
        "class Main\n",
    )
    targets = _import_targets(tmp_path, source)
    assert {"aaa.First", "bbb.Second"} <= targets
    assert not any("/*" in t or "\n" in t for t in targets)


def test_same_line_trailing_comment(tmp_path: Path) -> None:
    source = _write(
        tmp_path,
        "package app\n\n"
        "import aaa.First // same-line note\n"
        "import bbb.Second /* inline block */\n\n"
        "class Main\n",
    )
    targets = _import_targets(tmp_path, source)
    assert {"aaa.First", "bbb.Second"} <= targets
    assert not any("//" in t or "/*" in t for t in targets)


def test_comment_that_looks_like_an_import_is_not_recorded(tmp_path: Path) -> None:
    source = _write(
        tmp_path,
        "package app\n\n"
        "import real.Thing\n\n"
        "/** import fake.Ghost */\n"
        "class Main\n",
    )
    targets = _import_targets(tmp_path, source)
    assert "real.Thing" in targets
    assert not any("fake.Ghost" in t for t in targets)


def test_comment_containing_as_keyword_does_not_alias(tmp_path: Path) -> None:
    # " as " inside the folded comment must not be mistaken for an alias.
    source = _write(
        tmp_path,
        "package app\n\n"
        "import real.Thing\n\n"
        "/** used as helper as needed */\n"
        "class Main\n",
    )
    targets = _import_targets(tmp_path, source)
    assert "real.Thing" in targets
    assert all(" as " not in t for t in targets)


def test_kdoc_adjacent_without_blank_line(tmp_path: Path) -> None:
    source = _write(
        tmp_path,
        "package app\n\n"
        "import real.Thing\n"
        "/** doc right below the import */\n"
        "class Main\n",
    )
    targets = _import_targets(tmp_path, source)
    assert "real.Thing" in targets
    assert not any("/**" in t for t in targets)


def test_aliased_import_with_same_line_comment(tmp_path: Path) -> None:
    source = _write(
        tmp_path,
        "package app\n\n"
        "import real.Thing as Alias // why we alias\n\n"
        "class Main\n",
    )
    targets = _import_targets(tmp_path, source)
    assert "real.Thing" in targets
    assert not any("Alias" in t or "//" in t for t in targets)


def test_semicolon_terminated_import(tmp_path: Path) -> None:
    source = _write(
        tmp_path,
        "package app\n\nimport real.Thing;\n\nclass Main\n",
    )
    assert "real.Thing" in _import_targets(tmp_path, source)


def test_unicode_identifier_import(tmp_path: Path) -> None:
    source = _write(
        tmp_path,
        "package app\n\n"
        "import pkg.日本語クラス\n\n"
        "/** doc */\n"
        "class Main\n",
    )
    targets = _import_targets(tmp_path, source)
    assert "pkg.日本語クラス" in targets


def test_crlf_line_endings(tmp_path: Path) -> None:
    source = tmp_path / "Main.kt"
    source.write_bytes(
        b"package app\r\n\r\n"
        b"import real.Thing\r\n\r\n"
        b"/** doc */\r\n"
        b"class Main\r\n"
    )
    targets = _import_targets(tmp_path, source)
    assert "real.Thing" in targets
    assert not any("/**" in t for t in targets)


def test_deep_wildcard_with_trailing_line_comment(tmp_path: Path) -> None:
    source = _write(
        tmp_path,
        "package app\n\n"
        "import a.b.c.d.e.*\n\n"
        "// trailing note\n"
        "class Main\n",
    )
    targets = _import_targets(tmp_path, source)
    assert "a.b.c.d.e.*" in targets
    assert not any("//" in t for t in targets)


def test_import_only_file_with_trailing_comment(tmp_path: Path) -> None:
    # No declaration after the comment at all.
    source = _write(
        tmp_path,
        "package app\n\nimport real.Thing\n\n/** dangling doc */\n",
    )
    targets = _import_targets(tmp_path, source)
    assert "real.Thing" in targets


def test_bare_import_keyword_does_not_crash(tmp_path: Path) -> None:
    # Malformed: "import" with no target must not emit a garbage edge.
    source = _write(
        tmp_path,
        "package app\n\nimport\n\nclass Main\n",
    )
    targets = _import_targets(tmp_path, source)
    assert not any(t.strip() == "import" for t in targets)
    assert not any("class" in t for t in targets)


def test_many_imports_with_trailing_kdoc_all_clean(tmp_path: Path) -> None:
    imports = "".join(f"import pkg.mod{i}.Cls{i}\n" for i in range(300))
    source = _write(
        tmp_path,
        "package app\n\n" + imports + "\n/** " + "x " * 5000 + "*/\nclass Main\n",
    )
    targets = _import_targets(tmp_path, source)
    expected = {f"pkg.mod{i}.Cls{i}" for i in range(300)}
    assert expected <= targets
    assert all(len(t) < 200 for t in targets)


def test_trailing_comment_import_resolves_to_local_file(tmp_path: Path) -> None:
    # End to end: the cleaned module name must now resolve to a real file,
    # which the folded text never could.
    dep_dir = tmp_path / "dep"
    dep_dir.mkdir()
    dep = dep_dir / "Helper.kt"
    dep.write_text("package dep\n\nclass Helper\n", encoding="utf-8")
    source = _write(
        tmp_path,
        "package app\n\n"
        "import dep.Helper\n\n"
        "/** doc after last import */\n"
        "class Main\n",
    )
    targets = _import_targets(tmp_path, source)
    assert str(dep.resolve()) in {str(Path(t)) for t in targets}
