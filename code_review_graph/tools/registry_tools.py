"""Tools 21, 22: list_repos_func, cross_repo_search_func."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..graph import GraphStore
from ..incremental import get_db_path
from ..search import hybrid_search
from ._common import _bounded, _shown_of, _validate_positive_int

logger = logging.getLogger(__name__)

# Hard ceiling on the merged result set. ``limit`` is per repo, so a
# registry with 40 repos returned 40x the caller's expectation.
_MAX_CROSS_REPO_RESULTS = 100


# ---------------------------------------------------------------------------
# Tool 21: list_repos  [REGISTRY]
# ---------------------------------------------------------------------------


def list_repos_func() -> dict[str, Any]:
    """List all registered repositories.

    [REGISTRY] Returns the list of repositories registered in the global
    multi-repo registry at ``~/.code-review-graph/registry.json``.

    Returns:
        List of registered repos with paths and aliases.
    """
    from ..registry import Registry

    try:
        registry = Registry()
        repos = registry.list_repos()
        return {
            "status": "ok",
            "summary": f"{len(repos)} registered repository(ies)",
            "repos": repos,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool 22: cross_repo_search  [REGISTRY]
# ---------------------------------------------------------------------------


def cross_repo_search_func(
    query: str,
    kind: str | None = None,
    limit: int = 20,
    max_results: int = 50,
) -> dict[str, Any]:
    """Search across all registered repositories.

    [REGISTRY] Runs hybrid_search on each registered repo's graph database
    and merges the results.

    Args:
        query: Search query string.
        kind: Optional node kind filter (e.g. "Function", "Class").
        limit: Maximum results per repo (default: 20).
        max_results: Maximum merged results to return across all repos
            (default 50, capped at 100). ``total`` reports the untruncated
            merged count; without it the response grew with the number of
            registered repos rather than with the caller's ``limit``.

    Returns:
        Combined search results from all registered repos, plus ``total``
        and ``truncated``.
    """
    from ..registry import Registry

    _validate_positive_int(limit, "limit")
    _validate_positive_int(max_results, "max_results")

    try:
        registry = Registry()
        repos = registry.list_repos()
        if not repos:
            return {
                "status": "ok",
                "summary": (
                    "No repositories registered. "
                    "Use 'register' to add repos."
                ),
                "results": [],
            }

        ranked_results: list[tuple[int, int, dict[str, Any]]] = []
        searched_repos: list[str] = []

        for repo_index, repo_entry in enumerate(repos):
            repo_path = Path(repo_entry["path"])
            db_path = get_db_path(repo_path)
            if not db_path.exists():
                continue

            try:
                store = GraphStore(str(db_path))
                try:
                    results = hybrid_search(
                        store, query, kind=kind, limit=limit
                    )
                    alias = repo_entry.get("alias", repo_path.name)
                    for local_rank, r in enumerate(results):
                        r["repo"] = alias
                        r["repo_path"] = str(repo_path)
                        ranked_results.append((local_rank, repo_index, r))
                    searched_repos.append(alias)
                finally:
                    store.close()
            except Exception as exc:
                logger.warning(
                    "Search failed for %s: %s", repo_path, exc
                )

        # Scores from different search paths are not comparable across repos.
        # Merge by each repo's local rank and use registry order as a stable tie-breaker.
        ranked_results.sort(key=lambda item: (item[0], item[1]))
        all_results, total, truncated = _bounded(
            [result for _, _, result in ranked_results],
            max_results,
            _MAX_CROSS_REPO_RESULTS,
        )

        return {
            "status": "ok",
            "summary": (
                f"Found {total} result(s) across "
                f"{len(searched_repos)} repo(s) for '{query}'"
                + _shown_of(len(all_results), total)
            ),
            "results": all_results,
            "total": total,
            "truncated": truncated,
            "repos_searched": searched_repos,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
