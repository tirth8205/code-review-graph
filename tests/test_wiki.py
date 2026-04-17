"""Tests for wiki generation."""

import tempfile
from pathlib import Path

from code_review_graph.communities import detect_communities, store_communities
from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.wiki import (
    _generate_community_page,
    _slugify,
    generate_wiki,
    get_wiki_page,
)


class TestWiki:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)
        self.wiki_dir = tempfile.mkdtemp()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)
        # Clean up wiki dir
        wiki_path = Path(self.wiki_dir)
        if wiki_path.exists():
            for f in wiki_path.iterdir():
                f.unlink(missing_ok=True)
            wiki_path.rmdir()

    def _seed_communities(self):
        """Seed graph data and detect/store communities."""
        # Auth cluster
        self.store.upsert_node(
            NodeInfo(
                kind="File", name="auth.py", file_path="auth.py",
                line_start=1, line_end=100, language="python",
            ), file_hash="a1"
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function", name="login", file_path="auth.py",
                line_start=5, line_end=20, language="python",
            ), file_hash="a1"
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function", name="logout", file_path="auth.py",
                line_start=25, line_end=40, language="python",
            ), file_hash="a1"
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function", name="check_token", file_path="auth.py",
                line_start=45, line_end=60, language="python",
            ), file_hash="a1"
        )
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="auth.py::login",
            target="auth.py::check_token", file_path="auth.py", line=10,
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="auth.py::logout",
            target="auth.py::check_token", file_path="auth.py", line=30,
        ))

        # DB cluster
        self.store.upsert_node(
            NodeInfo(
                kind="File", name="db.py", file_path="db.py",
                line_start=1, line_end=100, language="python",
            ), file_hash="b1"
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function", name="connect", file_path="db.py",
                line_start=5, line_end=20, language="python",
            ), file_hash="b1"
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function", name="query", file_path="db.py",
                line_start=25, line_end=40, language="python",
            ), file_hash="b1"
        )
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="db.py::query",
            target="db.py::connect", file_path="db.py", line=30,
        ))
        self.store.commit()

        communities = detect_communities(self.store, min_size=2)
        store_communities(self.store, communities)
        return communities

    def test_generate_wiki_creates_files(self):
        """generate_wiki creates markdown files including index.md."""
        self._seed_communities()
        result = generate_wiki(self.store, self.wiki_dir)

        wiki_path = Path(self.wiki_dir)
        assert (wiki_path / "index.md").exists()
        # At least one community page should be generated
        md_files = list(wiki_path.glob("*.md"))
        assert len(md_files) >= 2  # index + at least 1 community page

        assert result["pages_generated"] >= 2
        assert isinstance(result["pages_updated"], int)
        assert isinstance(result["pages_unchanged"], int)

    def test_generate_wiki_index_has_links(self):
        """index.md contains links to community pages."""
        self._seed_communities()
        generate_wiki(self.store, self.wiki_dir)

        index_content = (Path(self.wiki_dir) / "index.md").read_text()
        assert "# Code Wiki" in index_content
        assert "Communities" in index_content
        assert ".md" in index_content  # contains links to .md files

    def test_get_wiki_page_returns_content(self):
        """get_wiki_page returns content for an existing page."""
        self._seed_communities()
        generate_wiki(self.store, self.wiki_dir)

        # Find any generated page
        wiki_path = Path(self.wiki_dir)
        pages = [f for f in wiki_path.glob("*.md") if f.name != "index.md"]
        assert len(pages) > 0

        # Get page by its stem (slug)
        page_name = pages[0].stem
        content = get_wiki_page(self.wiki_dir, page_name)
        assert content is not None
        assert len(content) > 0

    def test_get_wiki_page_returns_none_for_missing(self):
        """get_wiki_page returns None for non-existent page."""
        content = get_wiki_page(self.wiki_dir, "nonexistent-page")
        assert content is None

    def test_community_page_has_expected_sections(self):
        """Generated community pages contain expected sections."""
        communities = self._seed_communities()
        assert len(communities) > 0

        from code_review_graph.communities import get_communities
        stored = get_communities(self.store)
        assert len(stored) > 0

        page = _generate_community_page(self.store, stored[0])
        assert "## Overview" in page
        assert "## Members" in page
        assert "## Execution Flows" in page
        assert "## Dependencies" in page

    def test_slugify(self):
        """_slugify converts names to safe filenames."""
        assert _slugify("auth-login") == "auth-login"
        assert _slugify("My Community Name") == "my-community-name"
        assert _slugify("") == "unnamed"
        assert _slugify("auth/sub-cluster") == "auth-sub-cluster"

    def test_generate_wiki_force_regenerates(self):
        """generate_wiki with force=True regenerates all pages."""
        self._seed_communities()

        # First generation
        result1 = generate_wiki(self.store, self.wiki_dir)
        assert result1["pages_generated"] >= 2

        # Second generation without force - should be unchanged
        result2 = generate_wiki(self.store, self.wiki_dir)
        assert result2["pages_unchanged"] >= 1

        # Third generation with force - should update all
        result3 = generate_wiki(self.store, self.wiki_dir, force=True)
        assert result3["pages_generated"] + result3["pages_updated"] >= 1

    def test_generate_wiki_empty_graph(self):
        """generate_wiki on empty graph creates index with no communities."""
        result = generate_wiki(self.store, self.wiki_dir)
        assert result["pages_generated"] >= 1  # at least index.md

        index_content = (Path(self.wiki_dir) / "index.md").read_text()
        assert "Total communities" in index_content
        assert "0" in index_content  # 0 communities

    def test_generate_wiki_handles_slug_collisions(self, monkeypatch):
        """Communities whose names slugify to the same string must each get
        their own page — earlier behaviour silently overwrote the first
        community's file with the second's content and counted the second
        as an 'updated' page (see #222 follow-up).
        """
        # Fake three communities whose _slugify outputs collide:
        #   "Data Processing"  -> data-processing
        #   "data processing"  -> data-processing
        #   "Data  Processing" -> data-processing
        colliding_communities = [
            {
                "name": "Data Processing", "size": 5, "cohesion": 0.9,
                "dominant_language": "python", "description": "first",
                "members": [], "member_qns": set(),
            },
            {
                "name": "data processing", "size": 4, "cohesion": 0.8,
                "dominant_language": "python", "description": "second",
                "members": [], "member_qns": set(),
            },
            {
                "name": "Data  Processing", "size": 3, "cohesion": 0.7,
                "dominant_language": "python", "description": "third",
                "members": [], "member_qns": set(),
            },
        ]

        import code_review_graph.wiki as wiki_mod
        monkeypatch.setattr(
            wiki_mod, "get_communities", lambda store: colliding_communities,
        )

        result = generate_wiki(self.store, self.wiki_dir)

        # 3 unique .md pages + 1 index.md should land on disk.
        wiki_files = sorted(p.name for p in Path(self.wiki_dir).glob("*.md"))
        expected = {
            "data-processing.md",
            "data-processing-2.md",
            "data-processing-3.md",
            "index.md",
        }
        assert set(wiki_files) == expected, wiki_files

        # Counter must match what actually hit the disk.
        page_total = (
            result["pages_generated"]
            + result["pages_updated"]
            + result["pages_unchanged"]
        )
        assert page_total == len(wiki_files), (
            f"counter {result} but {len(wiki_files)} files on disk"
        )

        # Every community's description must survive (no data loss).
        content_first = (Path(self.wiki_dir) / "data-processing.md").read_text()
        content_second = (Path(self.wiki_dir) / "data-processing-2.md").read_text()
        content_third = (Path(self.wiki_dir) / "data-processing-3.md").read_text()
        # Descriptions are rendered in the page body; make sure all three
        # communities produced distinct content.
        assert content_first != content_second
        assert content_first != content_third
        assert content_second != content_third
