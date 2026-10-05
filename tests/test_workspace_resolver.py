"""Tests for the WorkspaceResolver class (workspace package names, #1030)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from code_review_graph.workspace_resolver import WorkspaceResolver


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _touch(path: Path, text: str = "export const x = 1\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _monorepo(root: Path, workspaces: object = ("packages/*", "apps/*")) -> Path:
    """Create an npm-workspaces root plus one importer; return the importer."""
    if isinstance(workspaces, tuple):
        workspaces = list(workspaces)
    _write_json(root / "package.json", {"name": "root", "private": True, "workspaces": workspaces})
    return _touch(root / "apps" / "web" / "src" / "page.ts", "import { x } from '@acme/shared'\n")


def _package(root: Path, rel_dir: str, manifest: dict) -> Path:
    pkg_dir = root / rel_dir
    _write_json(pkg_dir / "package.json", manifest)
    return pkg_dir


def _same(result: Optional[str], expected: Path) -> bool:
    return result is not None and Path(result).resolve() == expected.resolve()


# The entry-point shape of a package that ships compiled output: every field
# points into dist/, which is gitignored and usually absent from a checkout.
_DIST_ONLY = {
    "main": "./dist/index.cjs",
    "module": "./dist/index.js",
    "types": "./dist/index.d.ts",
    "exports": {
        ".": {
            "types": "./dist/index.d.ts",
            "import": "./dist/index.js",
            "require": "./dist/index.cjs",
        }
    },
}


class TestPackageEntry:
    def setup_method(self):
        self.resolver = WorkspaceResolver()

    def test_dist_entry_maps_to_src_when_dist_absent(self, tmp_path):
        importer = _monorepo(tmp_path)
        pkg = _package(tmp_path, "packages/shared", {"name": "@acme/shared", **_DIST_ONLY})
        index = _touch(pkg / "src" / "index.ts")

        assert _same(self.resolver.resolve("@acme/shared", str(importer)), index)

    def test_dist_present_still_resolves_to_source(self, tmp_path):
        importer = _monorepo(tmp_path)
        pkg = _package(tmp_path, "packages/shared", {"name": "@acme/shared", **_DIST_ONLY})
        _touch(pkg / "dist" / "index.js")
        _touch(pkg / "dist" / "index.d.ts")
        index = _touch(pkg / "src" / "index.ts")

        assert _same(self.resolver.resolve("@acme/shared", str(importer)), index)

    def test_subpath_export_maps_to_src(self, tmp_path):
        importer = _monorepo(tmp_path)
        pkg = _package(tmp_path, "packages/client", {
            "name": "@acme/sdk",
            "exports": {
                ".": {"types": "./dist/index.d.ts", "import": "./dist/index.js"},
                "./browser": {
                    "types": "./dist/browser-client.d.ts",
                    "import": "./dist/browser-client.js",
                    "require": "./dist/browser-client.cjs",
                },
            },
        })
        _touch(pkg / "src" / "index.ts")
        target = _touch(pkg / "src" / "browser-client.ts")

        assert _same(self.resolver.resolve("@acme/sdk/browser", str(importer)), target)

    def test_exports_pointing_at_source(self, tmp_path):
        importer = _monorepo(tmp_path)
        pkg = _package(tmp_path, "packages/video", {
            "name": "@acme/video",
            "main": "src/index.ts",
            "exports": {
                ".": "./src/index.ts",
                "./player": "./src/player/index.ts",
                "./package.json": "./package.json",
            },
        })
        index = _touch(pkg / "src" / "index.ts")
        player = _touch(pkg / "src" / "player" / "index.ts")

        assert _same(self.resolver.resolve("@acme/video", str(importer)), index)
        assert _same(self.resolver.resolve("@acme/video/player", str(importer)), player)

    def test_string_exports(self, tmp_path):
        importer = _monorepo(tmp_path)
        pkg = _package(
            tmp_path, "packages/core", {"name": "@acme/core", "exports": "./lib/main.ts"},
        )
        main = _touch(pkg / "lib" / "main.ts")

        assert _same(self.resolver.resolve("@acme/core", str(importer)), main)

    def test_main_only_pointing_at_source(self, tmp_path):
        importer = _monorepo(tmp_path)
        pkg = _package(tmp_path, "packages/core", {"name": "@acme/core", "main": "src/entry.tsx"})
        entry = _touch(pkg / "src" / "entry.tsx")

        assert _same(self.resolver.resolve("@acme/core", str(importer)), entry)

    def test_no_entry_fields_falls_back_to_src_index(self, tmp_path):
        importer = _monorepo(tmp_path)
        pkg = _package(tmp_path, "packages/core", {"name": "@acme/core"})
        index = _touch(pkg / "src" / "index.ts")

        assert _same(self.resolver.resolve("@acme/core", str(importer)), index)

    def test_root_index_fallback(self, tmp_path):
        importer = _monorepo(tmp_path)
        pkg = _package(tmp_path, "packages/core", {"name": "@acme/core"})
        index = _touch(pkg / "index.js")

        assert _same(self.resolver.resolve("@acme/core", str(importer)), index)

    def test_subpath_without_exports_probes_src(self, tmp_path):
        importer = _monorepo(tmp_path)
        pkg = _package(
            tmp_path, "packages/shared", {"name": "@acme/shared", "main": "./dist/index.js"},
        )
        _touch(pkg / "src" / "index.ts")
        date = _touch(pkg / "src" / "utils" / "date.ts")
        utils_index = _touch(pkg / "src" / "utils" / "index.ts")

        assert _same(self.resolver.resolve("@acme/shared/utils/date", str(importer)), date)
        assert _same(self.resolver.resolve("@acme/shared/utils", str(importer)), utils_index)

    def test_exports_wildcard_pattern(self, tmp_path):
        importer = _monorepo(tmp_path)
        pkg = _package(tmp_path, "packages/ui", {
            "name": "@acme/ui",
            "exports": {"./*": "./src/components/*.tsx"},
        })
        button = _touch(pkg / "src" / "components" / "button.tsx")

        assert _same(self.resolver.resolve("@acme/ui/button", str(importer)), button)

    def test_exports_array_fallback(self, tmp_path):
        importer = _monorepo(tmp_path)
        pkg = _package(tmp_path, "packages/core", {
            "name": "@acme/core",
            "exports": {".": ["./missing/entry.js", "./src/index.ts"]},
        })
        index = _touch(pkg / "src" / "index.ts")

        assert _same(self.resolver.resolve("@acme/core", str(importer)), index)

    def test_nested_conditions(self, tmp_path):
        importer = _monorepo(tmp_path)
        pkg = _package(tmp_path, "packages/core", {
            "name": "@acme/core",
            "exports": {
                ".": {
                    "node": {"import": "./dist/node/index.mjs", "require": "./dist/node/index.cjs"},
                    "default": "./dist/index.js",
                }
            },
        })
        node_entry = _touch(pkg / "src" / "node" / "index.ts")

        assert _same(self.resolver.resolve("@acme/core", str(importer)), node_entry)

    def test_non_code_export_target_is_returned_when_present(self, tmp_path):
        importer = _monorepo(tmp_path)
        pkg = _package(tmp_path, "packages/ui", {
            "name": "@acme/ui",
            "exports": {".": "./src/index.ts", "./styles.css": "./src/index.css"},
        })
        _touch(pkg / "src" / "index.ts")
        css = _touch(pkg / "src" / "index.css", "body {}\n")

        assert _same(self.resolver.resolve("@acme/ui/styles.css", str(importer)), css)

    def test_unknown_subpath_returns_none(self, tmp_path):
        importer = _monorepo(tmp_path)
        pkg = _package(tmp_path, "packages/shared", {"name": "@acme/shared", **_DIST_ONLY})
        _touch(pkg / "src" / "index.ts")

        assert self.resolver.resolve("@acme/shared/does-not-exist", str(importer)) is None

    def test_types_directory_layout_resolves_the_package_root(self, tmp_path):
        # dist/types/ mirrors src/, so dist/types/index.d.ts is src/index.ts,
        # never the src/types/ barrel that happens to share the segment.
        importer = _monorepo(tmp_path)
        for manifest in (
            {"types": "./dist/types/index.d.ts", "main": "./dist/index.js"},
            {"exports": {".": {"types": "./dist/types/index.d.ts"}}},
            {"types": "./dist/types/index.d.ts"},
        ):
            resolver = WorkspaceResolver()
            pkg = _package(tmp_path, "packages/shared", {"name": "@acme/shared", **manifest})
            index = _touch(pkg / "src" / "index.ts")
            _touch(pkg / "src" / "types" / "index.ts")

            assert _same(resolver.resolve("@acme/shared", str(importer)), index), manifest

    def test_subpath_naming_the_format_directory_keeps_it(self, tmp_path):
        importer = _monorepo(tmp_path)
        pkg = _package(tmp_path, "packages/shared", {
            "name": "@acme/shared",
            "exports": {".": "./dist/index.js", "./types": "./dist/types/index.js"},
        })
        _touch(pkg / "src" / "index.ts")
        types_index = _touch(pkg / "src" / "types" / "index.ts")

        assert _same(self.resolver.resolve("@acme/shared/types", str(importer)), types_index)

    def test_pattern_priority_follows_node(self, tmp_path):
        # Node compares the prefix first, then the longer key wins.
        importer = _monorepo(tmp_path)
        pkg = _package(tmp_path, "packages/ui", {
            "name": "@acme/ui",
            "exports": {"./*": "./src/*.ts", "./*.css": "./styles/*.css"},
        })
        _touch(pkg / "src" / "button.css")
        css = _touch(pkg / "styles" / "button.css", "button {}\n")

        assert _same(self.resolver.resolve("@acme/ui/button.css", str(importer)), css)

    def test_null_export_blocks_the_subpath(self, tmp_path):
        importer = _monorepo(tmp_path)
        pkg = _package(tmp_path, "packages/core", {
            "name": "@acme/core",
            "exports": {".": "./src/index.ts", "./internal/*": None},
        })
        _touch(pkg / "src" / "index.ts")
        _touch(pkg / "src" / "internal" / "secret.ts")

        assert self.resolver.resolve("@acme/core/internal/secret", str(importer)) is None

    def test_case_mismatch_does_not_resolve(self, tmp_path):
        # NTFS and APFS answer is_file() for Utils.ts when only utils.ts
        # exists; Linux does not. The edge must not depend on the platform.
        importer = _monorepo(tmp_path)
        pkg = _package(tmp_path, "packages/shared", {"name": "@acme/shared"})
        _touch(pkg / "src" / "index.ts")
        _touch(pkg / "src" / "utils.ts")

        assert self.resolver.resolve("@acme/shared/Utils", str(importer)) is None

    def test_directory_case_mismatch_does_not_resolve(self, tmp_path):
        importer = _monorepo(tmp_path)
        pkg = _package(tmp_path, "packages/shared", {"name": "@acme/shared"})
        _touch(pkg / "src" / "index.ts")
        _touch(pkg / "src" / "utils" / "date.ts")
        _touch(pkg / "src" / "utils" / "index.ts")

        assert self.resolver.resolve("@acme/shared/Utils/date", str(importer)) is None
        assert self.resolver.resolve("@acme/shared/Utils", str(importer)) is None

    def test_null_only_targets_block_the_subpath(self, tmp_path):
        importer = _monorepo(tmp_path)
        for blocked in ({"import": None, "default": None}, [None], []):
            resolver = WorkspaceResolver()
            pkg = _package(tmp_path, "packages/core", {
                "name": "@acme/core",
                "exports": {".": "./src/index.ts", "./internal/*": blocked},
            })
            _touch(pkg / "src" / "index.ts")
            _touch(pkg / "src" / "internal" / "secret.ts")

            assert resolver.resolve("@acme/core/internal/secret", str(importer)) is None, blocked


class TestWorkspaceDiscovery:
    def setup_method(self):
        self.resolver = WorkspaceResolver()

    def test_explicit_directory_entries(self, tmp_path):
        importer = _monorepo(tmp_path, workspaces=["packages/*", "backend", "frontend"])
        backend = _package(tmp_path, "backend", {"name": "@acme/backend", "main": "src/server.ts"})
        server = _touch(backend / "src" / "server.ts")

        assert _same(self.resolver.resolve("@acme/backend", str(importer)), server)

    def test_yarn_object_form(self, tmp_path):
        importer = _monorepo(tmp_path, workspaces={"packages": ["libs/*"], "nohoist": ["**/x"]})
        pkg = _package(tmp_path, "libs/shared", {"name": "@acme/shared"})
        index = _touch(pkg / "src" / "index.ts")

        assert _same(self.resolver.resolve("@acme/shared", str(importer)), index)

    def test_pnpm_workspace_yaml(self, tmp_path):
        (tmp_path / "pnpm-workspace.yaml").write_text(
            "packages:\n  - 'packages/*'\n  - \"apps/*\"\n", encoding="utf-8",
        )
        _write_json(tmp_path / "package.json", {"name": "root", "private": True})
        importer = _touch(tmp_path / "apps" / "web" / "src" / "page.ts")
        pkg = _package(tmp_path, "packages/policies", {"name": "@core/policies", **_DIST_ONLY})
        index = _touch(pkg / "src" / "index.ts")

        assert _same(self.resolver.resolve("@core/policies", str(importer)), index)

    def test_negated_pattern_excludes_package(self, tmp_path):
        importer = _monorepo(tmp_path, workspaces=["packages/*", "!packages/legacy"])
        legacy = _package(tmp_path, "packages/legacy", {"name": "@acme/legacy"})
        _touch(legacy / "src" / "index.ts")

        assert self.resolver.resolve("@acme/legacy", str(importer)) is None

    def test_globstar_does_not_enter_node_modules(self, tmp_path):
        importer = _monorepo(tmp_path, workspaces=["packages/**"])
        real = _package(tmp_path, "packages/a", {"name": "@acme/a"})
        index = _touch(real / "src" / "index.ts")
        vendored = _package(tmp_path, "packages/a/node_modules/@acme/fake", {"name": "@acme/fake"})
        _touch(vendored / "src" / "index.ts")

        assert _same(self.resolver.resolve("@acme/a", str(importer)), index)
        assert self.resolver.resolve("@acme/fake", str(importer)) is None

    def test_no_workspace_root_returns_none(self, tmp_path):
        _write_json(tmp_path / "package.json", {"name": "single-app"})
        importer = _touch(tmp_path / "src" / "page.ts")

        assert self.resolver.resolve("@acme/shared", str(importer)) is None

    def test_invalid_member_manifest_is_skipped(self, tmp_path):
        importer = _monorepo(tmp_path)
        broken = tmp_path / "packages" / "broken"
        broken.mkdir(parents=True)
        (broken / "package.json").write_text("{ not json", encoding="utf-8")
        pkg = _package(tmp_path, "packages/shared", {"name": "@acme/shared"})
        index = _touch(pkg / "src" / "index.ts")

        assert _same(self.resolver.resolve("@acme/shared", str(importer)), index)

    def test_member_without_name_is_skipped(self, tmp_path):
        importer = _monorepo(tmp_path)
        nameless = _package(tmp_path, "packages/nameless", {"private": True})
        _touch(nameless / "src" / "index.ts")

        assert self.resolver.resolve("nameless", str(importer)) is None

    def test_invalid_root_manifest_returns_none(self, tmp_path):
        (tmp_path / "package.json").write_text("{ broken", encoding="utf-8")
        importer = _touch(tmp_path / "apps" / "web" / "src" / "page.ts")

        assert self.resolver.resolve("@acme/shared", str(importer)) is None

    def test_walk_stops_at_the_repo_root(self, tmp_path):
        outer = tmp_path / "outer"
        _write_json(outer / "package.json", {"name": "outer", "workspaces": ["*"]})
        sibling = _package(outer, "sibling", {"name": "@acme/sibling"})
        _touch(sibling / "src" / "index.ts")
        repo = outer / "repo"
        _write_json(repo / "package.json", {"name": "repo"})
        importer = _touch(repo / "src" / "page.ts")

        resolver = WorkspaceResolver(repo_root=repo)
        assert resolver.resolve("@acme/sibling", str(importer)) is None

    def test_patterns_cannot_escape_the_workspace_root(self, tmp_path):
        outside = _package(tmp_path, "outside/pkg", {"name": "@acme/outside"})
        _touch(outside / "src" / "index.ts")
        repo = tmp_path / "repo"
        importer = _monorepo(repo, workspaces=[
            "..\\outside\\*", "../outside/*", "/outside/*", "C:/outside/*", "packages/*",
        ])

        assert WorkspaceResolver(repo_root=repo).resolve("@acme/outside", str(importer)) is None
        assert WorkspaceResolver().resolve("@acme/outside", str(importer)) is None

    def test_crafted_manifests_do_not_raise(self, tmp_path):
        importer = _monorepo(tmp_path)
        nested = tmp_path / "packages" / "deep"
        nested.mkdir(parents=True)
        (nested / "package.json").write_text("[" * 200_000, encoding="utf-8")
        pkg = _package(tmp_path, "packages/shared", {"name": "@acme/shared"})
        index = _touch(pkg / "src" / "index.ts")

        assert _same(self.resolver.resolve("@acme/shared", str(importer)), index)

    def test_unreadable_pnpm_file_does_not_raise(self, tmp_path):
        (tmp_path / "pnpm-workspace.yaml").write_bytes(b"packages:\n  - '\xff\xfe\xfa'\n")
        importer = _touch(tmp_path / "apps" / "web" / "src" / "page.ts")

        assert self.resolver.resolve("@acme/shared", str(importer)) is None

    def test_a_failed_lookup_is_cached(self, tmp_path, monkeypatch):
        from code_review_graph import workspace_resolver as module

        importer = _monorepo(tmp_path)
        calls = []

        def boom(directory):
            calls.append(directory)
            raise RecursionError("crafted manifest")

        monkeypatch.setattr(module, "_workspace_patterns", boom)
        assert self.resolver.resolve("@acme/shared", str(importer)) is None
        assert self.resolver.resolve("@acme/other", str(importer)) is None
        assert len(calls) == 1


class TestSpecifiers:
    def setup_method(self):
        self.resolver = WorkspaceResolver()

    def test_external_packages_return_none(self, tmp_path):
        importer = _monorepo(tmp_path)
        pkg = _package(tmp_path, "packages/shared", {"name": "@acme/shared"})
        _touch(pkg / "src" / "index.ts")

        for spec in ("react", "@types/node", "lodash/fp", "node:fs"):
            assert self.resolver.resolve(spec, str(importer)) is None, spec

    def test_scoped_prefix_is_not_a_match(self, tmp_path):
        importer = _monorepo(tmp_path)
        pkg = _package(tmp_path, "packages/ui", {"name": "@acme/ui"})
        _touch(pkg / "src" / "index.ts")

        assert self.resolver.resolve("@acme/ui-kit", str(importer)) is None

    def test_unscoped_package_name(self, tmp_path):
        importer = _monorepo(tmp_path)
        _write_json(
            tmp_path / "apps" / "web" / "package.json",
            {"name": "web", "dependencies": {"utils": "workspace:*"}},
        )
        pkg = _package(tmp_path, "packages/utils", {"name": "utils"})
        index = _touch(pkg / "src" / "index.ts")
        strings = _touch(pkg / "src" / "strings.ts")

        assert _same(self.resolver.resolve("utils", str(importer)), index)
        assert _same(self.resolver.resolve("utils/strings", str(importer)), strings)

    def test_unscoped_name_needs_a_declared_dependency(self, tmp_path):
        # 'components/Button' is just as likely a baseUrl-relative import of
        # the importer's own src/components/, so an undeclared unscoped name
        # must not become an edge into a sibling package.
        importer = _monorepo(tmp_path)
        _write_json(tmp_path / "apps" / "web" / "package.json", {"name": "web"})
        pkg = _package(tmp_path, "packages/components", {"name": "components"})
        _touch(pkg / "src" / "Button.tsx")

        assert self.resolver.resolve("components/Button", str(importer)) is None

    def test_marker_manifest_does_not_hide_declared_dependencies(self, tmp_path):
        # {"type": "module"} marker files are not packages; the dependency
        # list that counts is the nearest manifest with a name.
        _monorepo(tmp_path)
        _write_json(
            tmp_path / "apps" / "web" / "package.json",
            {"name": "web", "dependencies": {"utils": "workspace:*"}},
        )
        _write_json(tmp_path / "apps" / "web" / "src" / "esm" / "package.json", {"type": "module"})
        importer = _touch(tmp_path / "apps" / "web" / "src" / "esm" / "page.ts")
        pkg = _package(tmp_path, "packages/utils", {"name": "utils"})
        index = _touch(pkg / "src" / "index.ts")

        assert _same(self.resolver.resolve("utils", str(importer)), index)

    def test_node_builtin_names_are_never_workspace_packages(self, tmp_path):
        importer = _monorepo(tmp_path)
        _write_json(
            tmp_path / "apps" / "web" / "package.json",
            {"name": "web", "dependencies": {"events": "*", "path": "*"}},
        )
        for name in ("events", "path"):
            pkg = _package(tmp_path, f"packages/{name}", {"name": name})
            _touch(pkg / "src" / "index.ts")

        for spec in ("events", "path", "path/posix"):
            assert self.resolver.resolve(spec, str(importer)) is None, spec

    def test_relative_and_alias_specifiers_are_ignored(self, tmp_path):
        importer = _monorepo(tmp_path)
        pkg = _package(tmp_path, "packages/shared", {"name": "@acme/shared"})
        _touch(pkg / "src" / "index.ts")

        for spec in ("./local", "../up", "@/lib/utils", "@", "", "/abs/path"):
            assert self.resolver.resolve(spec, str(importer)) is None, spec


class TestCaching:
    def test_workspace_map_is_cached(self, tmp_path):
        resolver = WorkspaceResolver()
        importer = _monorepo(tmp_path)
        pkg = _package(tmp_path, "packages/shared", {"name": "@acme/shared"})
        index = _touch(pkg / "src" / "index.ts")

        assert _same(resolver.resolve("@acme/shared", str(importer)), index)
        (pkg / "package.json").unlink()
        assert _same(resolver.resolve("@acme/shared", str(importer)), index)
