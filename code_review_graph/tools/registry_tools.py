"""Tools 21, 22: list_repos_func, cross_repo_search_func."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..graph import GraphStore, _sanitize_name
from ..incremental import get_db_path
from ..search import hybrid_search
from ._common import _bounded, _shown_of, _validate_positive_int

logger = logging.getLogger(__name__)

# Hard ceiling on the merged result set. ``limit`` is per repo, so a
# registry with 40 repos returned 40x the caller's expectation.
_MAX_CROSS_REPO_RESULTS = 100

# Hard ceilings on the name-echo lists. ``repos`` is caller-supplied and
# unbounded, so echoing it back verbatim let one call carry far more tokens
# than the tool's own budget allows while reporting nothing was truncated.
# Names are also sanitised on the way out, like every other name in a
# response, which caps each entry at 256 characters.
_MAX_ECHOED_NAMES = 20


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
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Pick the registry entries named by ``names``, preserving registry order.

    A name matches an entry's alias first, and only if no alias matches does
    it match the final component of an entry's path, so an entry registered
    as ``{"path": ".../billing-api", "alias": "billing"}`` is selected by
    either ``["billing"]`` or ``["billing-api"]``. Aliases are resolved
    before folder names across the WHOLE registry, so an explicit alias
    always beats another entry's incidental folder name.

    A name that matches several entries selects ALL of them. Selecting only
    the first would search fewer repositories than the caller asked for
    while reporting nothing missing, which is the failure ``unknown`` exists
    to prevent; sibling checkouts sharing a folder name are the normal case
    this parameter was added for.

    Registry order is preserved so the caller cannot change the merge
    tie-breaker by reordering ``names``.

    Returns the selected entries, the names that matched nothing, and the
    names that matched more than one entry.
    """
    by_alias: dict[str, list[int]] = {}
    by_folder: dict[str, list[int]] = {}
    for index, entry in enumerate(repos):
        folder = Path(entry["path"]).name
        alias = entry.get("alias")
        # Only an EXPLICIT alias goes in the alias namespace. An entry
        # registered without one is addressable by its folder name alone,
        # so it cannot outrank another entry's deliberate alias.
        if alias:
            by_alias.setdefault(alias, []).append(index)
        by_folder.setdefault(folder, []).append(index)

    wanted: set[int] = set()
    unknown: list[str] = []
    ambiguous: list[str] = []
    for name in names:
        matches = by_alias.get(name) or by_folder.get(name)
        if not matches:
            unknown.append(name)
            continue
        wanted.update(matches)
        if len(matches) > 1:
            ambiguous.append(name)

    selected = [entry for i, entry in enumerate(repos) if i in wanted]
    return selected, unknown, ambiguous


def _echoed(names: list[str]) -> tuple[list[str], int, bool]:
    """Bound and sanitise a caller-supplied name list before echoing it back.

    ``repos`` is the only unbounded input this tool echoes. Returning it
    verbatim let a single call carry many times the tool's own token budget
    while reporting ``truncated: False``. Sanitising matches what every
    other name in a response gets and caps each entry at 256 characters.
    """
    cleaned = [_sanitize_name(name) for name in names]
    return _bounded(cleaned, _MAX_ECHOED_NAMES, _MAX_ECHOED_NAMES)


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
            cannot answer the question. Matching is exact and case-sensitive
            and paths are not accepted, unlike ``Registry.resolve_repo``:
            this parameter only ever narrows an already-trusted registry, and
            a name that does not match is reported rather than guessed at.
            Names matching no entry are returned in ``unknown`` rather than
            dropped, since a silently skipped repo is indistinguishable from
            one that simply had no matches; names matching several entries
            select all of them and are listed in ``ambiguous``.

    Returns:
        Combined search results from the searched repos, plus ``total``
        and ``truncated``. When ``repos`` was given, also ``unknown`` and
        ``ambiguous`` with their ``*_total`` counts, both bounded like every
        other list in a response.
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
        ambiguous: list[str] = []
        unknown_shown: list[str] = []
        unknown_total = 0
        unknown_truncated = False
        ambiguous_shown: list[str] = []
        ambiguous_total = 0
        ambiguous_truncated = False
        if repos:
            selected, unknown, ambiguous = _select_repos(all_repos, repos)
            unknown_shown, unknown_total, unknown_truncated = _echoed(unknown)
            ambiguous_shown, ambiguous_total, ambiguous_truncated = _echoed(
                ambiguous
            )
            if not selected:
                # The count, never the names: the summary is the field agents
                # read first, and interpolating a caller-supplied list here
                # was the second copy of an unbounded echo.
                return {
                    "status": "ok",
                    "summary": (
                        f"No registered repository matches the "
                        f"{unknown_total} name(s) given. "
                        "Use 'repos' to list the registry."
                    ),
                    "results": [],
                    "total": 0,
                    "truncated": False,
                    "repos_searched": [],
                    "unknown": unknown_shown,
                    "unknown_total": unknown_total,
                    "unknown_truncated": unknown_truncated,
                    "ambiguous": ambiguous_shown,
                    "ambiguous_total": ambiguous_total,
                    "ambiguous_truncated": ambiguous_truncated,
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

        # A dropped or duplicated name reaches the summary too. The
        # all-unknown branch already said so there, and CLAUDE.md trains
        # agents to read the summary first, so both branches must report it.
        notes = ""
        if unknown_total:
            notes += f"; {unknown_total} name(s) matched no repository"
        if ambiguous_total:
            notes += f"; {ambiguous_total} name(s) matched several"

        response = {
            "status": "ok",
            "summary": (
                f"Found {total} result(s) across "
                f"{len(searched_repos)} repo(s) for '{query}'"
                + _shown_of(len(all_results), total)
                + notes
            ),
            "results": all_results,
            "total": total,
            "truncated": truncated,
            "repos_searched": searched_repos,
        }
        if repos:
            response["unknown"] = unknown_shown
            response["unknown_total"] = unknown_total
            response["unknown_truncated"] = unknown_truncated
            response["ambiguous"] = ambiguous_shown
            response["ambiguous_total"] = ambiguous_total
            response["ambiguous_truncated"] = ambiguous_truncated
        return response
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
