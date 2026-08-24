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


def _select_repos(
    repos: list[dict[str, Any]],
    names: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Pick the registry entries named by ``names``, preserving registry order.

    A name matches an entry's alias first, then the final component of its
    path, so an entry registered as ``{"path": ".../billing-api", "alias":
    "billing"}`` is selected by either ``["billing"]`` or ``["billing-api"]``.
    Registry order is preserved so the caller cannot change the merge
    tie-breaker by reordering ``names``.

    Returns the selected entries and the names that matched nothing.
    """
    by_name: dict[str, int] = {}
    for index, entry in enumerate(repos):
        folder = Path(entry["path"]).name
        by_name.setdefault(entry.get("alias") or folder, index)
        by_name.setdefault(folder, index)

    wanted = {by_name[name] for name in names if name in by_name}
    unknown = [name for name in names if name not in by_name]
    return [entry for i, entry in enumerate(repos) if i in wanted], unknown


def cross_repo_search_func(
    query: str,
    kind: str | None = None,
    limit: int = 20,
    max_results: int = 50,
    repos: list[str] | None = None,
) -> dict[str, Any]:
    """Search across registered repositories.

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
        repos: Optional aliases or folder names to search. Omitted or empty
            searches every registered repo. A registry that spans unrelated
            products otherwise contributes each repo's best local rank to the
            top of the merged list, so the caller pays for repositories that
            cannot answer the question. Names matching no entry are returned
            in ``unknown`` rather than dropped, since a silently skipped repo
            is indistinguishable from one that simply had no matches.

    Returns:
        Combined search results from the searched repos, plus ``total``
        and ``truncated``, and ``unknown`` when ``repos`` was given.
    """
    from ..registry import Registry

    _validate_positive_int(limit, "limit")
    _validate_positive_int(max_results, "max_results")

    try:
        registry = Registry()
        all_repos = registry.list_repos()
        if not all_repos:
            return {
                "status": "ok",
                "summary": (
                    "No repositories registered. "
                    "Use 'register' to add repos."
                ),
                "results": [],
            }

        unknown: list[str] = []
        if repos:
            selected, unknown = _select_repos(all_repos, repos)
            if not selected:
                return {
                    "status": "ok",
                    "summary": (
                        f"No registered repository matches {repos}. "
                        "Use 'repos' to list the registry."
                    ),
                    "results": [],
                    "total": 0,
                    "truncated": False,
                    "repos_searched": [],
                    "unknown": unknown,
                }
        else:
            selected = all_repos

        ranked_results: list[tuple[int, int, dict[str, Any]]] = []
        searched_repos: list[str] = []

        for repo_index, repo_entry in enumerate(selected):
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

        response = {
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
        if repos:
            response["unknown"] = unknown
        return response
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
