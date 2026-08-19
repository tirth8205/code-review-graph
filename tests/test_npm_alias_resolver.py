"""npm dependency aliases resolved to local workspace packages."""

from __future__ import annotations

import json
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path
from code_review_graph.parser import CodeParser
from code_review_graph.tools.query import query_graph


def _write_monorepo(root: Path) -> tuple[Path, Path]:
    """Build the locally-vendored npm-alias shape from issue #343."""
    shared_root = root / "packages" / "shared"
    shared_root.mkdir(parents=True)
    (shared_root / "package.json").write_text(
        json.dumps({"name": "@scope/shared", "version": "1.0.0"}),
        encoding="utf-8",
    )
    module = shared_root / "some" / "module.ts"
    module.parent.mkdir(parents=True)
    module.write_text("export function shared() {}\n", encoding="utf-8")

    app_root = root / "apps" / "consumer"
    (app_root / "src").mkdir(parents=True)
    (app_root / "package.json").write_text(
        json.dumps(
            {
                "name": "@scope/consumer",
                "dependencies": {"sharedLib": "npm:@scope/shared@^1.0.0"},
            },
        ),
        encoding="utf-8",
    )
    importer = app_root / "src" / "entry.ts"
    importer.write_text(
        "import { shared } from 'sharedLib/some/module';\n"
        "export function entry() { return shared(); }\n",
        encoding="utf-8",
    )
    return importer, module


def test_npm_alias_import_resolves_to_local_workspace_module(tmp_path: Path) -> None:
    importer, module = _write_monorepo(tmp_path)

    parser = CodeParser(repo_root=tmp_path)
    _nodes, edges = parser.parse_file(importer)
    imports = [edge for edge in edges if edge.kind == "IMPORTS_FROM"]

    assert imports, "expected the aliased import to be indexed"
    assert imports[0].target == module.resolve().as_posix()


def test_npm_alias_importers_survive_a_full_build_and_query(tmp_path: Path) -> None:
    importer, module = _write_monorepo(tmp_path)
    (tmp_path / ".code-review-graph").mkdir()
    db_path = get_db_path(tmp_path)

    with GraphStore(db_path) as store:
        built = full_build(tmp_path, store)
        assert built["errors"] == []

    result = query_graph(
        "importers_of",
        "packages/shared/some/module.ts",
        repo_root=str(tmp_path),
    )

    assert result["result_count"] == 1
    assert result["results"][0]["importer"] == importer.resolve().as_posix()
    assert result["results"][0]["file"] == importer.resolve().as_posix()
    assert module.is_file()


def test_root_npm_alias_resolves_package_entrypoint(tmp_path: Path) -> None:
    package_root = tmp_path / "packages" / "shared"
    package_root.mkdir(parents=True)
    (package_root / "package.json").write_text(
        json.dumps(
            {
                "name": "@scope/shared",
                "main": "./source/public.js",
            },
        ),
        encoding="utf-8",
    )
    source = package_root / "source" / "public.ts"
    source.parent.mkdir()
    source.write_text("export const value = 1;\n", encoding="utf-8")
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / "package.json").write_text(
        json.dumps(
            {"devDependencies": {"sharedLib": "npm:@scope/shared@2.0.0"}},
        ),
        encoding="utf-8",
    )
    importer = app_root / "entry.ts"
    importer.write_text("import 'sharedLib';\n", encoding="utf-8")

    _nodes, edges = CodeParser(repo_root=tmp_path).parse_file(importer)

    imports = [edge for edge in edges if edge.kind == "IMPORTS_FROM"]
    assert imports[0].target == source.resolve().as_posix()


def test_missing_and_ambiguous_local_packages_remain_external(tmp_path: Path) -> None:
    for directory, name in (("first", "@scope/shared"), ("second", "@scope/shared")):
        package_root = tmp_path / directory
        package_root.mkdir()
        (package_root / "package.json").write_text(
            json.dumps({"name": name}), encoding="utf-8",
        )
    app_root = tmp_path / "apps" / "missing"
    app_root.mkdir(parents=True)
    (app_root / "package.json").write_text(
        json.dumps({"dependencies": {"missingLib": "npm:@scope/missing@1.0.0"}}),
        encoding="utf-8",
    )
    missing_importer = app_root / "missing.ts"
    missing_importer.write_text("import 'missingLib';\n", encoding="utf-8")
    ambiguous_importer = tmp_path / "ambiguous.ts"
    ambiguous_importer.write_text("import 'sharedLib';\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"sharedLib": "npm:@scope/shared@1.0.0"}}),
        encoding="utf-8",
    )

    parser = CodeParser(repo_root=tmp_path)
    missing_edges = parser.parse_file(missing_importer)[1]
    ambiguous_edges = parser.parse_file(ambiguous_importer)[1]

    assert [edge.target for edge in missing_edges if edge.kind == "IMPORTS_FROM"] == [
        "missingLib",
    ]
    assert [
        edge.target for edge in ambiguous_edges if edge.kind == "IMPORTS_FROM"
    ] == ["sharedLib"]


def test_npm_alias_cannot_escape_its_local_package(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    package_root.mkdir()
    (package_root / "package.json").write_text(
        json.dumps({"name": "shared"}), encoding="utf-8",
    )
    secret = tmp_path / "secret.ts"
    secret.write_text("export const secret = 1;\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"sharedLib": "npm:shared@1.0.0"}}),
        encoding="utf-8",
    )
    importer = tmp_path / "entry.ts"
    importer.write_text("import 'sharedLib/../secret';\n", encoding="utf-8")

    _nodes, edges = CodeParser(repo_root=tmp_path).parse_file(importer)

    assert [edge.target for edge in edges if edge.kind == "IMPORTS_FROM"] == [
        "sharedLib/../secret",
    ]


def test_explicit_tsconfig_alias_takes_precedence_over_npm_alias(tmp_path: Path) -> None:
    package_root = tmp_path / "packages" / "shared"
    package_root.mkdir(parents=True)
    (package_root / "package.json").write_text(
        json.dumps({"name": "@scope/shared"}), encoding="utf-8",
    )
    npm_target = package_root / "npm.ts"
    npm_target.write_text("export const a = 1;\n", encoding="utf-8")
    tsconfig_target = tmp_path / "explicit.ts"
    tsconfig_target.write_text("export const b = 2;\n", encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text(
        json.dumps(
            {
                "compilerOptions": {
                    "baseUrl": ".",
                    "paths": {"sharedLib": ["./explicit.ts"]},
                },
            },
        ),
        encoding="utf-8",
    )
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"sharedLib": "npm:@scope/shared@1.0.0"}}),
        encoding="utf-8",
    )
    importer = tmp_path / "entry.ts"
    importer.write_text("import 'sharedLib';\n", encoding="utf-8")

    _nodes, edges = CodeParser(repo_root=tmp_path).parse_file(importer)

    assert [edge.target for edge in edges if edge.kind == "IMPORTS_FROM"] == [
        tsconfig_target.resolve().as_posix(),
    ]
