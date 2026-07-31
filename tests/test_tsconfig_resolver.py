"""Tests for the TsconfigResolver class."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

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
