"""Tests for the TsconfigResolver class."""

from __future__ import annotations

import unittest
from pathlib import Path

from code_review_graph.tsconfig_resolver import TsconfigResolver

FIXTURES = Path(__file__).parent / "fixtures"


class TestTsconfigResolver(unittest.TestCase):
    def setUp(self):
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
        result = self.resolver.resolve_alias("@/foo", "/tmp/no_tsconfig/file.ts")
        assert result is None

    def test_caching(self):
        importer = str(FIXTURES / "alias_importer.ts")
        self.resolver.resolve_alias("@/lib/utils", importer)
        # Second call should use cache
        self.resolver.resolve_alias("@/lib/utils", importer)
        assert len(self.resolver._cache) >= 1
