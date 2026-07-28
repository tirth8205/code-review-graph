"""Tests for the TsconfigResolver class."""

from __future__ import annotations

import tempfile
from pathlib import Path

from code_review_graph.tsconfig_resolver import TsconfigResolver

FIXTURES = Path(__file__).parent / "fixtures"


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

    def test_resolve_alias_from_jsconfig(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "jsconfig.json").write_text(
                '{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./src/*"]}}}',
                encoding="utf-8",
            )
            (root / "src").mkdir()
            (root / "src" / "utils.js").write_text("export const a = 1;\n", encoding="utf-8")

            result = self.resolver.resolve_alias("@/utils", str(root / "main.js"))

        assert result is not None
        assert result.endswith("utils.js")

    def test_tsconfig_takes_precedence_over_jsconfig(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "tsconfig.json").write_text(
                '{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./ts-src/*"]}}}',
                encoding="utf-8",
            )
            (root / "jsconfig.json").write_text(
                '{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./js-src/*"]}}}',
                encoding="utf-8",
            )
            for sub, ext in (("ts-src", ".ts"), ("js-src", ".js")):
                (root / sub).mkdir()
                (root / sub / f"utils{ext}").write_text("export const a = 1;\n", encoding="utf-8")

            result = self.resolver.resolve_alias("@/utils", str(root / "main.ts"))

        assert result is not None
        assert result.endswith(str(Path("ts-src") / "utils.ts"))

    def test_caching(self):
        importer = str(FIXTURES / "alias_importer.ts")
        self.resolver.resolve_alias("@/lib/utils", importer)
        cache_size_after_first = len(self.resolver._cache)
        assert cache_size_after_first >= 1
        self.resolver.resolve_alias("@/lib/utils", importer)
        assert len(self.resolver._cache) == cache_size_after_first
