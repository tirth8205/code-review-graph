"""Tests for the Markdown and YAML knowledge extractors.

These formats have no bundled tree-sitter grammar, so they are parsed with
regex-based extractors (same approach as ReScript / SQL CREATE PROCEDURE).
Markdown headings and YAML registry entries become graph nodes, letting the
graph link documentation/config to code via the shared CONTAINS/REFERENCES
edge kinds.
"""
import tempfile
from pathlib import Path

from code_review_graph.parser import CodeParser


class TestKnowledgeExtractors:
    def setup_method(self):
        self.parser = CodeParser()

    def _write(self, suffix: str, content: str) -> Path:
        d = Path(tempfile.mkdtemp())
        p = d / f"sample{suffix}"
        p.write_text(content, encoding="utf-8")
        return p

    def test_markdown_headings_become_nodes(self):
        p = self._write(".md", "# Title\n## Section A\n### Sub\n## Section B\n")
        nodes, edges = self.parser.parse_file(p)
        kinds = {n.kind for n in nodes}
        assert "File" in kinds
        assert "Section" in kinds
        # 1 File + 4 headings
        assert sum(1 for n in nodes if n.kind == "Section") == 4
        contains = [e for e in edges if e.kind == "CONTAINS"]
        assert len(contains) == 4

    def test_markdown_nesting_and_code_fence(self):
        p = self._write(".md", "# Top\n## Child\n```\n# not a heading\n```\n")
        nodes, edges = self.parser.parse_file(p)
        labels = {n.name for n in nodes if n.kind == "Section"}
        # fenced "# not a heading" must NOT become a node
        assert not any("not a heading" in lbl for lbl in labels)
        # Child nests under Top, not the file
        nest = [
            e for e in edges
            if e.kind == "CONTAINS" and "Top" in e.source and "Child" in e.target
        ]
        assert nest

    def test_markdown_references(self):
        p = self._write(".md", "# Doc\nSee `other_doc.md` and [link](target.md).\n")
        _, edges = self.parser.parse_file(p)
        refs = {e.target for e in edges if e.kind == "REFERENCES"}
        assert "other_doc" in refs
        assert "target" in refs

    def test_yaml_top_keys_and_entries(self):
        p = self._write(".yaml", "crons:\n  - id: CRON-1\n    name: alpha\n  - id: CRON-2\n")
        nodes, edges = self.parser.parse_file(p)
        names = {n.name for n in nodes}
        assert any(n.kind == "File" for n in nodes)
        assert any("crons" in nm for nm in names)          # top-level key node
        assert any("CRON-1" in nm for nm in names)         # registry entry node
        assert [e for e in edges if e.kind == "CONTAINS"]
