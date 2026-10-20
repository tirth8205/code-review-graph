"""Tests for the TsconfigResolver class."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update
from code_review_graph.tools.query import query_graph
from code_review_graph.tsconfig_resolver import TsconfigResolver

FIXTURES = Path(__file__).parent / "fixtures"


def _write_config(root: Path, name: str, paths: dict, base_url: str = ".") -> None:
    (root / name).write_text(
        json.dumps({"compilerOptions": {"baseUrl": base_url, "paths": paths}}),
        encoding="utf-8",
    )


class TestTsconfigResolver:
    def setup_method(self):
        self.resolver = TsconfigResolver()

    def test_strip_jsonc_comments(self):
        text = '{\n  // comment\n  "key": "value" /* block */\n}'
        result = self.resolver._strip_jsonc_comments(text)
        assert "//" not in result
        assert "/*" not in result

    def test_strip_trailing_commas(self):
        text = '{"a": 1, "b": 2,}'
        result = self.resolver._strip_jsonc_comments(text)
        assert ",}" not in result

    def test_resolve_alias(self):
        importer = str(FIXTURES / "alias_importer.ts")
        result = self.resolver.resolve_alias("@/lib/utils", importer)
        assert result is not None
        assert result.endswith("utils.ts")

    def test_resolve_alias_nonexistent_returns_none(self):
        importer = str(FIXTURES / "alias_importer.ts")
        result = self.resolver.resolve_alias("@/nonexistent/module", importer)
        assert result is None

    def test_resolve_npm_package_returns_none(self):
        importer = str(FIXTURES / "alias_importer.ts")
        result = self.resolver.resolve_alias("react", importer)
        assert result is None

    def test_no_tsconfig_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = str(Path(tmp_dir) / "file.ts")
            result = self.resolver.resolve_alias("@/foo", file_path)
        assert result is None

    def test_caching(self):
        importer = str(FIXTURES / "alias_importer.ts")
        self.resolver.resolve_alias("@/lib/utils", importer)
        cache_size_after_first = len(self.resolver._cache)
        assert cache_size_after_first >= 1
        self.resolver.resolve_alias("@/lib/utils", importer)
        assert len(self.resolver._cache) == cache_size_after_first


class TestJsconfigResolution:
    """Regression tests for issue #776: jsconfig.json path aliases."""

    def setup_method(self):
        self.resolver = TsconfigResolver()

    def test_jsconfig_only_project_resolves_alias(self):
        """A plain-JS project declaring aliases only in jsconfig.json resolves them."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_config(root, "jsconfig.json", {"@/*": ["src/*"]})
            target = root / "src" / "composables" / "useThing.js"
            target.parent.mkdir(parents=True)
            target.write_text("export function useThing() {}\n", encoding="utf-8")
            importer = root / "src" / "App.vue"
            importer.write_text("import '@/composables/useThing'\n", encoding="utf-8")

            result = self.resolver.resolve_alias("@/composables/useThing", str(importer))
            assert result is not None
            assert Path(result) == target.resolve()

    def test_tsconfig_wins_over_jsconfig_in_same_dir(self):
        """When both configs exist in a directory, tsconfig.json takes precedence."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_config(root, "tsconfig.json", {"@/*": ["ts_src/*"]})
            _write_config(root, "jsconfig.json", {"@/*": ["js_src/*"]})
            ts_target = root / "ts_src" / "mod.ts"
            ts_target.parent.mkdir(parents=True)
            ts_target.write_text("export const x = 1\n", encoding="utf-8")
            js_target = root / "js_src" / "mod.js"
            js_target.parent.mkdir(parents=True)
            js_target.write_text("export const x = 1\n", encoding="utf-8")
            importer = root / "main.ts"
            importer.write_text("import { x } from '@/mod'\n", encoding="utf-8")

            result = self.resolver.resolve_alias("@/mod", str(importer))
            assert result is not None
            assert Path(result) == ts_target.resolve()

    def test_jsconfig_with_jsonc_comments_and_extends(self):
        """jsconfig files support JSONC comments and relative extends chains."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "jsconfig.base.json").write_text(
                '{\n'
                '  // shared aliases\n'
                '  "compilerOptions": {\n'
                '    "baseUrl": ".",\n'
                '    "paths": {"@/*": ["src/*"],}\n'
                '  }\n'
                '}\n',
                encoding="utf-8",
            )
            (root / "jsconfig.json").write_text(
                '{"extends": "./jsconfig.base.json", "compilerOptions": {}}\n',
                encoding="utf-8",
            )
            target = root / "src" / "util.js"
            target.parent.mkdir(parents=True)
            target.write_text("export const u = 1\n", encoding="utf-8")
            importer = root / "src" / "app.js"
            importer.write_text("import { u } from '@/util'\n", encoding="utf-8")

            result = self.resolver.resolve_alias("@/util", str(importer))
            assert result is not None
            assert Path(result) == target.resolve()


class TestExtendsAnchoring:
    """``baseUrl``/``paths`` inherited through ``extends`` stay anchored to the
    config that declared them, as tsc resolves them.

    The jsconfig test above keeps the base and the child in one directory, where
    both anchors coincide; an Nx/Turborepo layout puts the base at the root and
    each app's config two levels down.
    """

    def _monorepo(self, root: Path, base_options: dict) -> Path:
        (root / "tsconfig.base.json").write_text(
            json.dumps({"compilerOptions": base_options}), encoding="utf-8",
        )
        target = root / "libs" / "shared" / "src" / "index.ts"
        target.parent.mkdir(parents=True)
        target.write_text("export const shared = 1;\n", encoding="utf-8")
        (root / "apps" / "api" / "src").mkdir(parents=True)
        return target

    def _app_config(self, root: Path, compiler_options: dict) -> Path:
        (root / "apps" / "api" / "tsconfig.json").write_text(
            json.dumps({
                "extends": "../../tsconfig.base.json",
                "compilerOptions": compiler_options,
            }),
            encoding="utf-8",
        )
        importer = root / "apps" / "api" / "src" / "main.ts"
        importer.write_text("import { shared } from '@acme/shared';\n", encoding="utf-8")
        return importer

    def test_inherited_base_url_resolves_from_the_base_config(self, tmp_path):
        target = self._monorepo(tmp_path, {
            "baseUrl": ".",
            "paths": {"@acme/shared": ["libs/shared/src/index.ts"]},
        })
        importer = self._app_config(tmp_path, {"outDir": "../../dist/apps/api"})

        result = TsconfigResolver().resolve_alias("@acme/shared", str(importer))
        assert result is not None
        assert Path(result) == target.resolve()

    def test_inherited_paths_without_base_url_resolve_from_the_base_config(self, tmp_path):
        target = self._monorepo(tmp_path, {
            "paths": {"@acme/shared": ["./libs/shared/src/index.ts"]},
        })
        importer = self._app_config(tmp_path, {})

        result = TsconfigResolver().resolve_alias("@acme/shared", str(importer))
        assert result is not None
        assert Path(result) == target.resolve()

    def test_child_that_redeclares_paths_keeps_its_own_anchor(self, tmp_path):
        target = self._monorepo(tmp_path, {
            "baseUrl": ".",
            "paths": {"@acme/shared": ["libs/shared/src/index.ts"]},
        })
        importer = self._app_config(tmp_path, {
            "baseUrl": ".",
            "paths": {"@acme/shared": ["../../libs/shared/src/index.ts"]},
        })

        result = TsconfigResolver().resolve_alias("@acme/shared", str(importer))
        assert result is not None
        assert Path(result) == target.resolve()

    def test_child_base_url_rebases_inherited_paths(self, tmp_path):
        self._monorepo(tmp_path, {
            "baseUrl": ".",
            "paths": {"@acme/local/*": ["lib/*"]},
        })
        local = tmp_path / "apps" / "api" / "src" / "lib" / "helper.ts"
        local.parent.mkdir(parents=True)
        local.write_text("export const helper = 1;\n", encoding="utf-8")
        importer = self._app_config(tmp_path, {"baseUrl": "src"})

        result = TsconfigResolver().resolve_alias("@acme/local/helper", str(importer))
        assert result is not None
        assert Path(result) == local.resolve()


def test_resolve_alias_probes_mts_and_cts(tmp_path):
    _write_config(tmp_path, "tsconfig.json", {"@lib/*": ["src/lib/*"]})
    lib = tmp_path / "src" / "lib"
    lib.mkdir(parents=True)
    (lib / "util.mts").write_text("export const f = 1;\n", encoding="utf-8")
    (lib / "legacy.cts").write_text("module.exports = {};\n", encoding="utf-8")
    importer = tmp_path / "src" / "app.ts"
    importer.write_text("", encoding="utf-8")
    resolver = TsconfigResolver()
    util = resolver.resolve_alias("@lib/util", str(importer))
    legacy = resolver.resolve_alias("@lib/legacy", str(importer))
    assert util is not None and util.endswith("util.mts")
    assert legacy is not None and legacy.endswith("legacy.cts")


# ---------------------------------------------------------------------------
# Edge cases for anchoring inherited baseUrl/paths (PR #1111, issue #1110)
# ---------------------------------------------------------------------------


def _write_json(path: Path, data: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _write_source(path: Path, text: str = "export const value = 1;\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


_SHARED_ALIAS = {"@acme/shared": ["libs/shared/src/index.ts"]}


class TestExtendsAnchoringEdgeCases:
    """Shapes of the extends chain beyond the four in ``TestExtendsAnchoring``.

    tsc resolves ``baseUrl`` against the config that declares it, and
    ``paths`` against ``baseUrl`` when the chain sets one, otherwise against
    the config that declares ``paths`` (PR #1111, issue #1110).
    """

    def test_anchor_survives_a_middle_config_that_declares_neither(self, tmp_path):
        target = _write_source(tmp_path / "libs" / "shared" / "src" / "index.ts")
        _write_json(tmp_path / "tsconfig.base.json", {
            "compilerOptions": {"baseUrl": ".", "paths": _SHARED_ALIAS},
        })
        _write_json(tmp_path / "packages" / "config" / "lib.json", {
            "extends": "../../tsconfig.base.json",
            "compilerOptions": {"strict": True},
        })
        # Suffix-less extends: ``.json`` is appended, as tsc does.
        _write_json(tmp_path / "apps" / "api" / "tsconfig.json", {
            "extends": "../../packages/config/lib",
        })
        importer = _write_source(tmp_path / "apps" / "api" / "src" / "main.ts")

        result = TsconfigResolver().resolve_alias("@acme/shared", str(importer))
        assert result is not None
        assert Path(result) == target.resolve()

    def test_paths_without_base_url_anchor_at_the_middle_config_declaring_them(
        self, tmp_path,
    ):
        target = _write_source(tmp_path / "libs" / "shared" / "src" / "index.ts")
        _write_json(tmp_path / "tsconfig.base.json", {"compilerOptions": {"strict": True}})
        _write_json(tmp_path / "configs" / "tsconfig.paths.json", {
            "extends": "../tsconfig.base.json",
            "compilerOptions": {
                "paths": {"@acme/shared": ["../libs/shared/src/index.ts"]},
            },
        })
        _write_json(tmp_path / "apps" / "api" / "tsconfig.json", {
            "extends": "../../configs/tsconfig.paths.json",
        })
        importer = _write_source(tmp_path / "apps" / "api" / "src" / "main.ts")

        result = TsconfigResolver().resolve_alias("@acme/shared", str(importer))
        assert result is not None
        assert Path(result) == target.resolve()

    def test_base_url_pointing_out_of_a_sibling_config_directory(self, tmp_path):
        shared = _write_source(tmp_path / "libs" / "shared" / "src" / "index.ts")
        _write_json(tmp_path / "config" / "tsconfig.base.json", {
            "compilerOptions": {"baseUrl": "..", "paths": {"@acme/*": ["libs/*/src"]}},
        })
        _write_json(tmp_path / "apps" / "api" / "tsconfig.json", {
            "extends": "../../config/tsconfig.base.json",
        })
        importer = _write_source(tmp_path / "apps" / "api" / "src" / "main.ts")

        result = TsconfigResolver().resolve_alias("@acme/shared", str(importer))
        assert result is not None
        assert Path(result) == shared.resolve()

    def test_inherited_base_url_anchors_paths_the_child_declares(self, tmp_path):
        target = _write_source(tmp_path / "libs" / "shared" / "src" / "index.ts")
        _write_json(tmp_path / "tsconfig.base.json", {"compilerOptions": {"baseUrl": "."}})
        _write_json(tmp_path / "apps" / "api" / "tsconfig.json", {
            "extends": "../../tsconfig.base.json",
            "compilerOptions": {"paths": _SHARED_ALIAS},
        })
        importer = _write_source(tmp_path / "apps" / "api" / "src" / "main.ts")

        result = TsconfigResolver().resolve_alias("@acme/shared", str(importer))
        assert result is not None
        assert Path(result) == target.resolve()

    def test_child_paths_without_any_base_url_anchor_at_the_child(self, tmp_path):
        # The base's own target exists too, so resolving it would be a wrong hit.
        _write_source(tmp_path / "libs" / "shared" / "src" / "index.ts")
        _write_json(tmp_path / "tsconfig.base.json", {
            "compilerOptions": {"paths": {"@acme/shared": ["./libs/shared/src/index.ts"]}},
        })
        local = _write_source(tmp_path / "apps" / "api" / "local" / "shared.ts")
        _write_json(tmp_path / "apps" / "api" / "tsconfig.json", {
            "extends": "../../tsconfig.base.json",
            "compilerOptions": {"paths": {"@acme/shared": ["./local/shared"]}},
        })
        importer = _write_source(tmp_path / "apps" / "api" / "src" / "main.ts")

        result = TsconfigResolver().resolve_alias("@acme/shared", str(importer))
        assert result is not None
        assert Path(result) == local.resolve()

    def test_child_empty_paths_drops_inherited_aliases(self, tmp_path):
        _write_source(tmp_path / "libs" / "shared" / "src" / "index.ts")
        _write_json(tmp_path / "tsconfig.base.json", {
            "compilerOptions": {"baseUrl": ".", "paths": _SHARED_ALIAS},
        })
        _write_json(tmp_path / "apps" / "api" / "tsconfig.json", {
            "extends": "../../tsconfig.base.json",
            "compilerOptions": {"paths": {}},
        })
        importer = _write_source(tmp_path / "apps" / "api" / "src" / "main.ts")

        assert TsconfigResolver().resolve_alias("@acme/shared", str(importer)) is None

    def test_inherited_wildcards_probe_extensions_and_directory_index(self, tmp_path):
        button = _write_source(tmp_path / "libs" / "ui" / "src" / "Button.tsx")
        data = _write_source(tmp_path / "libs" / "data" / "src" / "index.ts")
        _write_json(tmp_path / "tsconfig.base.json", {
            "compilerOptions": {
                "baseUrl": ".",
                "paths": {"@acme/ui/*": ["libs/ui/src/*"], "@acme/*": ["libs/*/src"]},
            },
        })
        _write_json(tmp_path / "apps" / "web" / "tsconfig.json", {
            "extends": "../../tsconfig.base.json",
        })
        importer = _write_source(tmp_path / "apps" / "web" / "src" / "page.tsx")
        resolver = TsconfigResolver()

        assert resolver.resolve_alias("@acme/ui/Button", str(importer)) == str(button.resolve())
        assert resolver.resolve_alias("@acme/data", str(importer)) == str(data.resolve())
        assert resolver.resolve_alias("@acme/missing", str(importer)) is None

    def test_unicode_and_spaces_in_every_directory(self, tmp_path):
        root = tmp_path / "mono répo"
        target = _write_source(root / "libs" / "ünï code" / "src" / "index.ts")
        _write_json(root / "tsconfig.base.json", {
            "compilerOptions": {
                "baseUrl": ".",
                "paths": {"@acme/ünï": ["libs/ünï code/src/index.ts"]},
            },
        })
        _write_json(root / "apps" / "wéb app" / "tsconfig.json", {
            "extends": "../../tsconfig.base.json",
        })
        importer = _write_source(root / "apps" / "wéb app" / "src" / "main.ts")

        result = TsconfigResolver().resolve_alias("@acme/ünï", str(importer))
        assert result is not None
        assert Path(result) == target.resolve()

    def test_jsconfig_child_extending_a_root_base(self, tmp_path):
        target = _write_source(tmp_path / "packages" / "utils" / "index.js")
        _write_json(tmp_path / "jsconfig.base.json", {
            "compilerOptions": {"baseUrl": ".", "paths": {"~utils": ["packages/utils"]}},
        })
        _write_json(tmp_path / "apps" / "site" / "jsconfig.json", {
            "extends": "../../jsconfig.base.json",
        })
        importer = _write_source(tmp_path / "apps" / "site" / "src" / "App.vue")

        result = TsconfigResolver().resolve_alias("~utils", str(importer))
        assert result is not None
        assert Path(result) == target.resolve()

    def test_several_apps_share_one_base_and_repeat_lookups(self, tmp_path):
        target = _write_source(tmp_path / "libs" / "shared" / "src" / "index.ts")
        _write_json(tmp_path / "tsconfig.base.json", {
            "compilerOptions": {"baseUrl": ".", "paths": _SHARED_ALIAS},
        })
        importers = []
        for app, extends in (
            ("apps/api", "../../tsconfig.base.json"),
            ("apps/web", "../../tsconfig.base.json"),
            ("apps/admin/portal", "../../../tsconfig.base.json"),
        ):
            _write_json(tmp_path / app / "tsconfig.json", {"extends": extends})
            importers.append(_write_source(tmp_path / app / "src" / "main.ts"))

        resolver = TsconfigResolver()
        for _ in range(2):
            for importer in importers:
                result = resolver.resolve_alias("@acme/shared", str(importer))
                assert result is not None
                assert Path(result) == target.resolve()

    @pytest.mark.parametrize("extends", [
        "../../missing.base.json",
        ["../../tsconfig.base.json"],
        "@tsconfig/node20/tsconfig.json",
    ], ids=["missing-file", "array-form", "package"])
    def test_unfollowed_extends_keep_the_childs_own_anchor(self, tmp_path, extends):
        _write_source(tmp_path / "libs" / "shared" / "src" / "index.ts")
        _write_json(tmp_path / "tsconfig.base.json", {
            "compilerOptions": {"baseUrl": ".", "paths": _SHARED_ALIAS},
        })
        util = _write_source(tmp_path / "apps" / "api" / "src" / "util.ts")
        _write_json(tmp_path / "apps" / "api" / "tsconfig.json", {
            "extends": extends,
            "compilerOptions": {"paths": {"@app/*": ["./src/*"]}},
        })
        importer = _write_source(tmp_path / "apps" / "api" / "src" / "main.ts")
        resolver = TsconfigResolver()

        assert resolver.resolve_alias("@app/util", str(importer)) == str(util.resolve())
        # Array-form and package extends are out of scope (#1110), so the
        # base's alias stays unresolved instead of resolving somewhere wrong.
        assert resolver.resolve_alias("@acme/shared", str(importer)) is None

    def test_extends_cycle_terminates_with_the_declaring_anchor(self, tmp_path):
        target = _write_source(tmp_path / "libs" / "shared" / "src" / "index.ts")
        _write_json(tmp_path / "tsconfig.base.json", {
            "extends": "./apps/api/tsconfig.json",
            "compilerOptions": {"baseUrl": ".", "paths": _SHARED_ALIAS},
        })
        _write_json(tmp_path / "apps" / "api" / "tsconfig.json", {
            "extends": "../../tsconfig.base.json",
        })
        importer = _write_source(tmp_path / "apps" / "api" / "src" / "main.ts")

        result = TsconfigResolver().resolve_alias("@acme/shared", str(importer))
        assert result is not None
        assert Path(result) == target.resolve()

    def test_invalid_base_json_falls_back_to_the_childs_own_config(self, tmp_path):
        (tmp_path / "tsconfig.base.json").write_text("{ not json", encoding="utf-8")
        util = _write_source(tmp_path / "apps" / "api" / "src" / "util.ts")
        _write_json(tmp_path / "apps" / "api" / "tsconfig.json", {
            "extends": "../../tsconfig.base.json",
            "compilerOptions": {"paths": {"@app/*": ["./src/*"]}},
        })
        importer = _write_source(tmp_path / "apps" / "api" / "src" / "main.ts")
        resolver = TsconfigResolver()

        assert resolver.resolve_alias("@app/util", str(importer)) == str(util.resolve())
        assert resolver.resolve_alias("@acme/shared", str(importer)) is None

    def test_internal_anchor_keys_in_a_user_config_are_ignored(self, tmp_path):
        target = _write_source(tmp_path / "libs" / "shared" / "src" / "index.ts")
        decoy_root = tmp_path / "decoy"
        _write_source(decoy_root / "libs" / "shared" / "src" / "index.ts")
        _write_json(tmp_path / "tsconfig.base.json", {
            "_base_url_dir": str(decoy_root),
            "_paths_dir": str(decoy_root),
            "compilerOptions": {"paths": {"@acme/shared": ["./libs/shared/src/index.ts"]}},
        })
        _write_json(tmp_path / "apps" / "api" / "tsconfig.json", {
            "extends": "../../tsconfig.base.json",
            "_tsconfig_dir": str(decoy_root),
        })
        importer = _write_source(tmp_path / "apps" / "api" / "src" / "main.ts")

        result = TsconfigResolver().resolve_alias("@acme/shared", str(importer))
        assert result is not None
        assert Path(result) == target.resolve()

    @pytest.mark.parametrize("child_options", [
        {"baseUrl": 1},
        [],
        "not-an-object",
    ], ids=["int-baseUrl", "list-options", "string-options"])
    def test_malformed_child_options_never_raise(self, tmp_path, child_options):
        target = _write_source(tmp_path / "libs" / "shared" / "src" / "index.ts")
        _write_json(tmp_path / "tsconfig.base.json", {
            "compilerOptions": {"baseUrl": ".", "paths": _SHARED_ALIAS},
        })
        _write_json(tmp_path / "apps" / "api" / "tsconfig.json", {
            "extends": "../../tsconfig.base.json",
            "compilerOptions": child_options,
        })
        importer = _write_source(tmp_path / "apps" / "api" / "src" / "main.ts")

        result = TsconfigResolver().resolve_alias("@acme/shared", str(importer))
        # tsc rejects these options (``"baseUrl": null`` is different: it
        # unsets the inherited value, see TestExtendsAnchoringMatchesTsc). The
        # resolver must not crash and must not point anywhere but the target.
        assert result in (None, str(target.resolve()))

    def test_deep_extends_chain_keeps_the_root_anchor(self, tmp_path):
        target = _write_source(tmp_path / "libs" / "shared" / "src" / "index.ts")
        _write_json(tmp_path / "tsconfig.base.json", {
            "compilerOptions": {"baseUrl": ".", "paths": _SHARED_ALIAS},
        })
        previous = "../tsconfig.base.json"
        for level in range(40):
            _write_json(tmp_path / "configs" / f"tsconfig.{level}.json", {
                "extends": previous if level == 0 else f"./tsconfig.{level - 1}.json",
                "compilerOptions": {"strict": level % 2 == 0},
            })
        _write_json(tmp_path / "apps" / "api" / "tsconfig.json", {
            "extends": "../../configs/tsconfig.39.json",
        })
        importer = _write_source(tmp_path / "apps" / "api" / "src" / "main.ts")

        result = TsconfigResolver().resolve_alias("@acme/shared", str(importer))
        assert result is not None
        assert Path(result) == target.resolve()


def test_build_and_update_link_imports_through_inherited_paths(tmp_path):
    """Full build, incremental update and rebuild all store the resolved file,
    not the raw alias, for an app that inherits ``paths`` (PR #1111)."""
    target = _write_source(
        tmp_path / "libs" / "shared" / "src" / "index.ts",
        "export function shared(): number { return 1; }\n",
    )
    _write_json(tmp_path / "tsconfig.base.json", {
        "compilerOptions": {"baseUrl": ".", "paths": _SHARED_ALIAS},
    })
    _write_json(tmp_path / "apps" / "api" / "tsconfig.json", {
        "extends": "../../tsconfig.base.json",
    })
    main = _write_source(
        tmp_path / "apps" / "api" / "src" / "main.ts",
        "import { shared } from '@acme/shared';\n"
        "export function apiMain(): number { return shared(); }\n",
    )
    root = str(tmp_path)
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()

    def importers() -> set[str]:
        found = query_graph("importers_of", str(target), repo_root=root)
        assert found["status"] == "ok"
        return {row["file"] for row in found["results"]}

    def raw_alias_edges(store: GraphStore) -> int:
        row = store._conn.execute(
            "SELECT COUNT(*) FROM edges WHERE kind = 'IMPORTS_FROM' "
            "AND target_qualified = ?",
            ("@acme/shared",),
        ).fetchone()
        return int(row[0])

    with GraphStore(graph_dir / "graph.db") as store:
        assert full_build(tmp_path, store)["errors"] == []
        assert raw_alias_edges(store) == 0
    assert importers() == {main.as_posix()}

    extra = _write_source(
        tmp_path / "apps" / "api" / "src" / "extra.ts",
        "import { shared } from '@acme/shared';\n"
        "export function extra(): number { return shared() + 1; }\n",
    )
    with GraphStore(graph_dir / "graph.db") as store:
        updated = incremental_update(
            tmp_path, store, changed_files=["apps/api/src/extra.ts"],
        )
        assert updated["errors"] == []
        assert raw_alias_edges(store) == 0
    assert importers() == {main.as_posix(), extra.as_posix()}

    with GraphStore(graph_dir / "graph.db") as store:
        assert full_build(tmp_path, store)["errors"] == []
        assert raw_alias_edges(store) == 0
    assert importers() == {main.as_posix(), extra.as_posix()}


class TestExtendsAnchoringMatchesTsc:
    """More extends-chain shapes, each checked against tsc 5.9 with
    ``--traceResolution`` (PR #1111, issue #1110)."""

    def test_inherited_catch_all_paths_resolve_from_the_base(self, tmp_path):
        # TypeScript 6 deprecates ``baseUrl`` and 7 removes it; the suggested
        # replacement is a catch-all ``"*": ["./*"]`` in the root base config.
        target = _write_source(tmp_path / "libs" / "shared" / "src" / "index.ts")
        _write_json(tmp_path / "tsconfig.base.json", {
            "compilerOptions": {"paths": {"*": ["./*"]}},
        })
        _write_json(tmp_path / "apps" / "api" / "tsconfig.json", {
            "extends": "../../tsconfig.base.json",
        })
        importer = _write_source(tmp_path / "apps" / "api" / "src" / "main.ts")
        resolver = TsconfigResolver()

        assert resolver.resolve_alias("libs/shared/src", str(importer)) == str(
            target.resolve()
        )
        assert resolver.resolve_alias("react", str(importer)) is None

    def test_child_base_url_rebases_paths_the_base_declared_without_base_url(
        self, tmp_path,
    ):
        # A baseUrl anywhere in the chain beats the directory of the config
        # that declared ``paths``, even when that config set no baseUrl.
        _write_source(tmp_path / "libs" / "shared" / "index.ts")
        local = _write_source(
            tmp_path / "apps" / "api" / "src" / "libs" / "shared" / "index.ts",
        )
        _write_json(tmp_path / "tsconfig.base.json", {
            "compilerOptions": {"paths": {"@acme/shared": ["./libs/shared/index.ts"]}},
        })
        _write_json(tmp_path / "apps" / "api" / "tsconfig.json", {
            "extends": "../../tsconfig.base.json",
            "compilerOptions": {"baseUrl": "./src"},
        })
        importer = _write_source(tmp_path / "apps" / "api" / "src" / "main.ts")

        result = TsconfigResolver().resolve_alias("@acme/shared", str(importer))
        assert result == str(local.resolve())

    def test_two_bases_with_the_same_alias_stay_separate_in_one_resolver(
        self, tmp_path,
    ):
        root_target = _write_source(tmp_path / "libs" / "shared" / "src" / "index.ts")
        legacy_target = _write_source(tmp_path / "legacy" / "vendor" / "shared.ts")
        _write_json(tmp_path / "tsconfig.base.json", {
            "compilerOptions": {"baseUrl": ".", "paths": _SHARED_ALIAS},
        })
        _write_json(tmp_path / "legacy" / "tsconfig.base.json", {
            "compilerOptions": {
                "baseUrl": ".", "paths": {"@acme/shared": ["vendor/shared.ts"]},
            },
        })
        _write_json(tmp_path / "apps" / "api" / "tsconfig.json", {
            "extends": "../../tsconfig.base.json",
        })
        _write_json(tmp_path / "legacy" / "apps" / "old" / "tsconfig.json", {
            "extends": "../../tsconfig.base.json",
        })
        api = _write_source(tmp_path / "apps" / "api" / "src" / "main.ts")
        old = _write_source(tmp_path / "legacy" / "apps" / "old" / "src" / "main.ts")
        resolver = TsconfigResolver()

        for _ in range(2):
            assert resolver.resolve_alias("@acme/shared", str(api)) == str(
                root_target.resolve()
            )
            assert resolver.resolve_alias("@acme/shared", str(old)) == str(
                legacy_target.resolve()
            )

    @pytest.mark.parametrize("shared_options", [
        {"baseUrl": ".", "paths": {"@app/*": ["src/*"]}},
        {"paths": {"@app/*": ["./src/*"]}},
    ], ids=["baseUrl", "paths-only"])
    def test_symlinked_base_config_anchors_at_the_link_not_its_target(
        self, tmp_path, shared_options,
    ):
        # tsc anchors baseUrl/paths at the path it opened the extended config
        # through; it does not follow the symlink to the file's real directory.
        _write_json(tmp_path / "tools" / "tsconfig.shared.json", {
            "compilerOptions": shared_options,
        })
        _write_source(tmp_path / "tools" / "src" / "util.ts")  # beside the real file
        link = tmp_path / "apps" / "api" / "tsconfig.shared.json"
        link.parent.mkdir(parents=True)
        try:
            os.symlink(os.path.join("..", "..", "tools", "tsconfig.shared.json"), link)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available")
        _write_json(tmp_path / "apps" / "api" / "tsconfig.json", {
            "extends": "./tsconfig.shared.json",
        })
        util = _write_source(tmp_path / "apps" / "api" / "src" / "util.ts")
        importer = _write_source(tmp_path / "apps" / "api" / "src" / "main.ts")

        result = TsconfigResolver().resolve_alias("@app/util", str(importer))
        assert result == str(util.resolve())

    @pytest.mark.parametrize("shared_options", [
        {"baseUrl": "..", "paths": {"@app/*": ["src/*"]}},
        {"paths": {"@app/*": ["../src/*"]}},
    ], ids=["baseUrl", "paths-only"])
    def test_base_config_in_a_symlinked_directory_climbs_from_the_link(
        self, tmp_path, shared_options,
    ):
        # tsc normalizes ``..`` lexically, so it climbs out of the directory the
        # config was opened through (apps/api/shared), not out of the one the
        # symlink points to (tools/shared). Checked with tsc 5.9.3 and 6.0.3.
        _write_json(tmp_path / "tools" / "shared" / "tsconfig.json", {
            "compilerOptions": shared_options,
        })
        _write_source(tmp_path / "tools" / "src" / "util.ts")  # where ``..`` lands physically
        link = tmp_path / "apps" / "api" / "shared"
        link.parent.mkdir(parents=True)
        try:
            os.symlink(os.path.join("..", "..", "tools", "shared"), link, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available")
        _write_json(tmp_path / "apps" / "api" / "tsconfig.json", {
            "extends": "./shared/tsconfig.json",
        })
        util = _write_source(tmp_path / "apps" / "api" / "src" / "util.ts")
        importer = _write_source(tmp_path / "apps" / "api" / "src" / "main.ts")

        result = TsconfigResolver().resolve_alias("@app/util", str(importer))
        assert result == str(util.resolve())

    def test_child_null_base_url_unsets_the_inherited_base_url(self, tmp_path):
        # tsc treats ``null`` in an extending config as "unset the inherited
        # value": paths then resolve against the config that declared them.
        _write_source(tmp_path / "base" / "foo.ts")  # where the inherited baseUrl points
        target = _write_source(tmp_path / "foo.ts")
        _write_json(tmp_path / "tsconfig.base.json", {
            "compilerOptions": {"baseUrl": "./base", "paths": {"@x/*": ["./*"]}},
        })
        _write_json(tmp_path / "apps" / "api" / "tsconfig.json", {
            "extends": "../../tsconfig.base.json",
            "compilerOptions": {"baseUrl": None},
        })
        importer = _write_source(tmp_path / "apps" / "api" / "src" / "main.ts")

        result = TsconfigResolver().resolve_alias("@x/foo", str(importer))
        assert result == str(target.resolve())
