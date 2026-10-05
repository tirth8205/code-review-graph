"""Imports of workspace package names resolve across package boundaries.

Regression for #1030: in an npm, yarn or pnpm workspace an import such as
``from '@acme/policies'`` names a sibling package, not a path alias, so
``TsconfigResolver`` left it as a bare specifier. ``importers_of`` and the
impact radius then stopped at every package boundary.
"""

from __future__ import annotations

import json
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _workspace(root: Path) -> tuple[Path, Path]:
    """Build a two-package workspace; return (importer, package entry file)."""
    (root / ".git").mkdir()
    (root / ".code-review-graph").mkdir()
    _write(root / "package.json", json.dumps({
        "name": "root", "private": True, "workspaces": ["packages/*", "apps/*"],
    }))
    # Every entry point targets dist/, which a checkout does not contain.
    _write(root / "packages" / "policies" / "package.json", json.dumps({
        "name": "@acme/policies",
        "main": "./dist/index.cjs",
        "types": "./dist/index.d.ts",
        "exports": {".": {
            "types": "./dist/index.d.ts",
            "import": "./dist/index.js",
            "require": "./dist/index.cjs",
        }},
    }))
    _write(
        root / "packages" / "policies" / "src" / "can.ts",
        "export function can(action: string): boolean {\n  return action.length > 0\n}\n",
    )
    entry = _write(
        root / "packages" / "policies" / "src" / "index.ts", "export { can } from './can'\n",
    )
    _write(root / "apps" / "web" / "package.json", json.dumps({"name": "@acme/web"}))
    importer = _write(
        root / "apps" / "web" / "src" / "route.ts",
        "import { can } from '@acme/policies'\n\n"
        "export function handler(): boolean {\n  return can('read')\n}\n",
    )
    return importer, entry


def _build(root: Path) -> None:
    store = GraphStore(root / ".code-review-graph" / "graph.db")
    parser = CodeParser(root)
    for path in sorted(root.rglob("*.ts")):
        nodes, edges = parser.parse_file(path)
        for node in nodes:
            store.upsert_node(node)
        for edge in edges:
            store.upsert_edge(edge)
    store.commit()
    store.close()


def test_import_edge_targets_the_package_source_entry(tmp_path):
    importer, entry = _workspace(tmp_path)

    _, edges = CodeParser(tmp_path).parse_file(importer)

    targets = [e.target for e in edges if e.kind == "IMPORTS_FROM"]
    assert "@acme/policies" not in targets
    assert entry.resolve() in {Path(t).resolve() for t in targets}


def test_calls_through_the_package_entry_reach_the_definition(tmp_path):
    importer, _ = _workspace(tmp_path)
    definition = tmp_path / "packages" / "policies" / "src" / "can.ts"

    _, edges = CodeParser(tmp_path).parse_file(importer)

    call_targets = [e.target.rpartition("::") for e in edges if e.kind == "CALLS"]
    assert any(
        name == "can" and sep and Path(file).resolve() == definition.resolve()
        for file, sep, name in call_targets
    ), call_targets


def test_tsconfig_paths_win_over_a_workspace_name(tmp_path):
    importer, entry = _workspace(tmp_path)
    local = _write(tmp_path / "apps" / "web" / "src" / "local-policies.ts", "export {}\n")
    _write(tmp_path / "apps" / "web" / "tsconfig.json", json.dumps({
        "compilerOptions": {
            "baseUrl": ".", "paths": {"@acme/policies": ["./src/local-policies.ts"]},
        },
    }))

    _, edges = CodeParser(tmp_path).parse_file(importer)

    targets = {Path(e.target).resolve() for e in edges if e.kind == "IMPORTS_FROM"}
    assert local.resolve() in targets
    assert entry.resolve() not in targets


def test_javascript_importers_resolve_too(tmp_path):
    _, entry = _workspace(tmp_path)
    for name in ("legacy.js", "view.jsx"):
        importer = _write(
            tmp_path / "apps" / "web" / "src" / name,
            "import { can } from '@acme/policies'\nexport const ok = can('x')\n",
        )

        _, edges = CodeParser(tmp_path).parse_file(importer)

        targets = {Path(e.target).resolve() for e in edges if e.kind == "IMPORTS_FROM"}
        assert entry.resolve() in targets, name


def test_full_build_survives_a_crafted_member_manifest(tmp_path):
    import sqlite3

    from code_review_graph.incremental import full_build

    importer, entry = _workspace(tmp_path)
    (tmp_path / "packages" / "deep").mkdir()
    (tmp_path / "packages" / "deep" / "package.json").write_text("[" * 200_000, encoding="utf-8")
    db_path = tmp_path / ".code-review-graph" / "graph.db"

    with GraphStore(db_path) as store:
        full_build(tmp_path, store)

    with sqlite3.connect(db_path) as conn:
        files = {Path(row[0]).resolve() for row in conn.execute(
            "SELECT file_path FROM nodes WHERE kind = 'File'"
        )}
        targets = {Path(row[0]).resolve() for row in conn.execute(
            "SELECT target_qualified FROM edges WHERE kind = 'IMPORTS_FROM' AND file_path LIKE ?",
            ("%route.ts",),
        )}
    assert importer.resolve() in files
    assert entry.resolve() in targets


def test_importers_of_and_impact_cross_the_package_boundary(tmp_path):
    from code_review_graph.tools.query import get_impact_radius, query_graph

    importer, entry = _workspace(tmp_path)
    _build(tmp_path)

    result = query_graph("importers_of", str(entry), repo_root=str(tmp_path))
    assert result.get("status") == "ok"
    importers = {item["file"] for item in result.get("results", [])}
    assert importer.as_posix() in importers

    impact = get_impact_radius(
        changed_files=[str(entry)], repo_root=str(tmp_path), max_depth=1
    )
    assert impact["status"] == "ok"
    assert importer.as_posix() in impact["impacted_files"]
