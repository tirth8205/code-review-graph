"""npm dependency aliases resolved to local workspace packages."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path
from code_review_graph.npm_alias_resolver import version_satisfies
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
                "version": "2.0.0",
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
        json.dumps({"name": "@scope/shared", "version": "1.0.0", "main": "npm.ts"}),
        encoding="utf-8",
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


def _imports(root: Path, importer: Path) -> list[str]:
    _nodes, edges = CodeParser(repo_root=root).parse_file(importer)
    return [edge.target for edge in edges if edge.kind == "IMPORTS_FROM"]


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_nearer_ordinary_dependency_hides_an_ancestor_alias(tmp_path: Path) -> None:
    """The maintainer's reproducer from the review of #880."""
    _write_json(tmp_path / "package.json", {"dependencies": {"alias": "npm:shared@1.0.0"}})
    _write_json(tmp_path / "app" / "package.json", {"dependencies": {"alias": "2.0.0"}})
    _write_json(
        tmp_path / "shared" / "package.json",
        {"name": "shared", "version": "2.0.0", "main": "index.ts"},
    )
    (tmp_path / "shared" / "index.ts").write_text(
        "export function help() {}\n", encoding="utf-8",
    )
    caller = tmp_path / "app" / "caller.ts"
    caller.write_text("import {help} from 'alias'; help();\n", encoding="utf-8")

    _nodes, edges = CodeParser(repo_root=tmp_path).parse_file(caller)

    assert [e.target for e in edges if e.kind == "IMPORTS_FROM"] == ["alias"]
    assert not [e for e in edges if "shared" in e.target]


def test_nearer_alias_wins_over_an_ancestor_ordinary_dependency(tmp_path: Path) -> None:
    _write_json(tmp_path / "package.json", {"dependencies": {"alias": "2.0.0"}})
    _write_json(
        tmp_path / "app" / "package.json",
        {"dependencies": {"alias": "npm:shared@^1.0.0"}},
    )
    _write_json(
        tmp_path / "shared" / "package.json",
        {"name": "shared", "version": "1.4.0", "main": "index.ts"},
    )
    target = tmp_path / "shared" / "index.ts"
    target.write_text("export function help() {}\n", encoding="utf-8")
    caller = tmp_path / "app" / "caller.ts"
    caller.write_text("import {help} from 'alias';\n", encoding="utf-8")

    assert _imports(tmp_path, caller) == [target.resolve().as_posix()]


@pytest.mark.parametrize(
    ("local_version", "spec", "resolves"),
    [
        ("2.0.0", "npm:shared@1.0.0", False),
        ("1.0.0", "npm:shared@1.0.0", True),
        ("1.4.2", "npm:shared@^1.0.0", True),
        ("2.0.0", "npm:shared@^1.0.0", False),
        ("1.2.9", "npm:shared@~1.2.3", True),
        ("1.3.0", "npm:shared@~1.2.3", False),
        ("1.0.0-beta.1", "npm:shared@^1.0.0", False),
        ("7.0.0", "npm:shared", True),
        ("7.0.0", "npm:shared@latest", True),
        ("7.0.0", "npm:shared@next", False),
        (None, "npm:shared@1.0.0", False),
        (None, "npm:shared@*", True),
        ("not-a-version", "npm:shared@^1.0.0", False),
    ],
)
def test_alias_range_must_accept_the_local_version(
    tmp_path: Path, local_version: object, spec: str, resolves: bool,
) -> None:
    manifest: dict[str, object] = {"name": "shared", "main": "index.ts"}
    if local_version is not None:
        manifest["version"] = local_version
    _write_json(tmp_path / "shared" / "package.json", manifest)
    target = tmp_path / "shared" / "index.ts"
    target.write_text("export const a = 1;\n", encoding="utf-8")
    _write_json(tmp_path / "app" / "package.json", {"dependencies": {"alias": spec}})
    caller = tmp_path / "app" / "caller.ts"
    caller.write_text("import 'alias';\n", encoding="utf-8")

    expected = target.resolve().as_posix() if resolves else "alias"
    assert _imports(tmp_path, caller) == [expected]


@pytest.mark.parametrize(
    ("version", "version_range", "expected"),
    [
        ("1.2.3", "1.2.3", True),
        ("1.2.4", "=1.2.3", False),
        ("0.2.9", "^0.2.3", True),
        ("0.3.0", "^0.2.3", False),
        ("0.0.4", "^0.0.3", False),
        ("1.9.9", "1.x", True),
        ("2.0.0", "1.x", False),
        ("2.3.4", "1.2.3 - 2.3.4", True),
        ("2.3.5", "1.2.3 - 2.3.4", False),
        ("2.4.0", "1.2.3 - 2.3", False),
        ("5.0.0", "1.x || >=2.5.0 || 5.0.0 - 7.2.3", True),
        ("1.3.0", ">= 1.2.3 < 2", True),
        ("1.2.3-beta.3", "^1.2.3-beta.2", True),
        ("1.3.0-beta", "^1.2.3-beta.2", False),
        ("1.2.3", "next", False),
        ("1.2.3", ">*", False),
    ],
)
def test_version_ranges_follow_npm(version: str, version_range: str, expected: bool) -> None:
    # Checked against node-semver's satisfies() on 1,700 range and version pairs.
    assert version_satisfies(version, version_range) is expected
