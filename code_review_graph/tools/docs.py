"""Tools 7, 8, 19, 20: embed_graph, get_docs_section, wiki tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..embeddings import EmbeddingStore, embed_all_nodes
from ..incremental import get_db_path
from ._common import _get_store

# ---------------------------------------------------------------------------
# Tool 7: embed_graph
# ---------------------------------------------------------------------------


def embed_graph(
    repo_root: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Compute vector embeddings for all graph nodes to enable semantic search.

    Requires: ``pip install code-review-graph[embeddings]``
    Default model: all-MiniLM-L6-v2. Override via ``model`` param or
    CRG_EMBEDDING_MODEL env var.
    Changing the model re-embeds all nodes automatically.

    Only embeds nodes that don't already have up-to-date embeddings.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
        model: Embedding model name (HuggingFace ID or local path).
               Falls back to CRG_EMBEDDING_MODEL env var, then
               all-MiniLM-L6-v2.

    Returns:
        Number of nodes embedded and total embedding count.
    """
    store, root = _get_store(repo_root)
    db_path = get_db_path(root)
    emb_store = EmbeddingStore(db_path, model=model)
    try:
        if not emb_store.available:
            return {
                "status": "error",
                "error": (
                    "sentence-transformers is not installed. "
                    "Install with: pip install code-review-graph[embeddings]"
                ),
            }

        newly_embedded = embed_all_nodes(store, emb_store)
        total = emb_store.count()

        return {
            "status": "ok",
            "summary": (
                f"Embedded {newly_embedded} new node(s). "
                f"Total embeddings: {total}. "
                "Semantic search is now active."
            ),
            "newly_embedded": newly_embedded,
            "total_embeddings": total,
        }
    finally:
        emb_store.close()
        store.close()


# ---------------------------------------------------------------------------
# Tool 8: get_docs_section
# ---------------------------------------------------------------------------


def get_docs_section(
    section_name: str, repo_root: str | None = None
) -> dict[str, Any]:
    """Return a specific section from the LLM-optimized reference.

    Used by skills and Claude Code to load only the exact documentation
    section needed, keeping token usage minimal (90%+ savings).

    Args:
        section_name: Exact section name. One of: usage, review-delta,
                      review-pr, commands, legal, watch, embeddings,
                      languages, troubleshooting.
        repo_root: Repository root path. Auto-detected from current
                   directory if omitted.

    Returns:
        The section content, or an error if not found.
    """
    import re as _re

    search_roots: list[Path] = []

    if repo_root:
        search_roots.append(Path(repo_root))

    try:
        _, root = _get_store(repo_root)
        if root not in search_roots:
            search_roots.append(root)
    except (RuntimeError, ValueError):
        pass

    # Fallback: package directory (for uvx/pip installs)
    pkg_docs = (
        Path(__file__).parent.parent.parent
        / "docs"
        / "LLM-OPTIMIZED-REFERENCE.md"
    )
    if pkg_docs.exists():
        pkg_root = pkg_docs.parent.parent
        if pkg_root not in search_roots:
            search_roots.append(pkg_root)

    for search_root in search_roots:
        candidate = search_root / "docs" / "LLM-OPTIMIZED-REFERENCE.md"
        if candidate.exists():
            content = candidate.read_text(encoding="utf-8")
            match = _re.search(
                rf'<section name="{_re.escape(section_name)}">'
                r"(.*?)</section>",
                content,
                _re.DOTALL | _re.IGNORECASE,
            )
            if match:
                return {
                    "status": "ok",
                    "section": section_name,
                    "content": match.group(1).strip(),
                }

    available = [
        "usage", "review-delta", "review-pr", "commands",
        "legal", "watch", "embeddings", "languages", "troubleshooting",
    ]
    return {
        "status": "not_found",
        "error": (
            f"Section '{section_name}' not found. "
            f"Available: {', '.join(available)}"
        ),
    }


# ---------------------------------------------------------------------------
# Tool 19: generate_wiki  [DOCS]
# ---------------------------------------------------------------------------


def generate_wiki_func(
    repo_root: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Generate a markdown wiki from the community structure.

    [DOCS] Creates a wiki page for each detected community and an index
    page. Pages are written to ``.code-review-graph/wiki/`` inside the
    repository. Only regenerates pages whose content has changed unless
    force=True.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
        force: If True, regenerate all pages even if content is unchanged.

    Returns:
        Status with pages_generated, pages_updated, pages_unchanged counts.
    """
    from ..incremental import get_data_dir
    from ..wiki import generate_wiki

    store, root = _get_store(repo_root)
    try:
        wiki_dir = get_data_dir(root) / "wiki"
        result = generate_wiki(store, wiki_dir, force=force)
        total = (
            result["pages_generated"]
            + result["pages_updated"]
            + result["pages_unchanged"]
        )
        return {
            "status": "ok",
            "summary": (
                f"Wiki generated: {result['pages_generated']} new, "
                f"{result['pages_updated']} updated, "
                f"{result['pages_unchanged']} unchanged "
                f"({total} total pages)"
            ),
            "wiki_dir": str(wiki_dir),
            **result,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 20: get_wiki_page  [DOCS]
# ---------------------------------------------------------------------------


def get_wiki_page_func(
    community_name: str,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Retrieve a specific wiki page by community name.

    [DOCS] Returns the markdown content of the wiki page for the given
    community. The wiki must have been generated first via generate_wiki.

    Args:
        community_name: Community name to look up (slugified for filename).
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Page content or not_found status.
    """
    from ..incremental import get_data_dir
    from ..wiki import get_wiki_page

    _, root = _get_store(repo_root)
    wiki_dir = get_data_dir(root) / "wiki"
    content = get_wiki_page(wiki_dir, community_name)
    if content is None:
        return {
            "status": "not_found",
            "summary": f"No wiki page found for '{community_name}'.",
        }
    return {
        "status": "ok",
        "summary": (
            f"Wiki page for '{community_name}' ({len(content)} chars)"
        ),
        "content": content,
    }
