"""MCP tool wrappers for graph analysis features."""

from __future__ import annotations

from typing import Any

from ..analysis import (
    find_bridge_nodes,
    find_hub_nodes,
    find_knowledge_gaps,
    find_surprising_connections,
    generate_suggested_questions,
)
from ._common import _bounded, _get_store, _shown_of, _validate_positive_int

# The ranking helpers already score every candidate before slicing, so asking
# for "all" costs nothing extra and lets the tool report an honest ``total``.
_FETCH_ALL = 10**9

# Hard ceilings. A caller may raise ``top_n`` above the default, but never
# past these: an unbounded top_n returned >500k tokens before #849's sweep.
_MAX_HUB_NODES = 100
_MAX_BRIDGE_NODES = 100
_MAX_SURPRISING = 100
_MAX_GAPS_PER_CATEGORY = 50

_MINIMAL_HUB_FIELDS = ("name", "kind", "total_degree")
_MINIMAL_BRIDGE_FIELDS = ("name", "kind", "betweenness")
_MINIMAL_SURPRISE_FIELDS = ("source", "target", "edge_kind", "surprise_score")
_MINIMAL_GAP_FIELDS = ("name", "qualified_name", "community_id", "size", "degree")


def _project(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    """Keep only *fields* on each row, dropping keys the row does not have."""
    return [{k: r[k] for k in fields if k in r} for r in rows]


def get_hub_nodes_func(
    repo_root: str | None = None,
    top_n: int = 10,
    detail_level: str = "standard",
) -> dict[str, Any]:
    """Find the most connected nodes in the codebase graph.

    Hub nodes have the highest total degree (in + out edges).
    These are architectural hotspots -- changes to them have
    disproportionate blast radius.

    Args:
        repo_root: Repository root (auto-detected if omitted).
        top_n: Number of top hubs to return (default 10, capped at 100).
        detail_level: "standard" (default) returns full node data;
            "minimal" returns only name, kind, and total_degree.

    Returns:
        Ranked hubs. ``total`` reports the untruncated candidate count and
        ``truncated`` marks that ``top_n`` or the ceiling cut the list.
    """
    _validate_positive_int(top_n, "top_n")

    store, _root = _get_store(repo_root or None)
    try:
        hubs, total, truncated = _bounded(
            find_hub_nodes(store, top_n=_FETCH_ALL), top_n, _MAX_HUB_NODES,
        )
        if detail_level == "minimal":
            hubs = _project(hubs, _MINIMAL_HUB_FIELDS)
        return {
            "status": "ok",
            "summary": (
                f"{total} hub node(s) ranked by degree"
                + _shown_of(len(hubs), total)
            ),
            "hub_nodes": hubs,
            "count": len(hubs),
            "total": total,
            "truncated": truncated,
            "next_tool_suggestions": [
                "get_impact_radius -- check blast radius of a hub",
                "query_graph callers_of -- see what calls a hub",
                "get_bridge_nodes -- find architectural chokepoints",
            ],
        }
    finally:
        store.close()


def get_bridge_nodes_func(
    repo_root: str | None = None,
    top_n: int = 10,
    detail_level: str = "standard",
) -> dict[str, Any]:
    """Find architectural chokepoints via betweenness centrality.

    Bridge nodes sit on the shortest paths between many node
    pairs. If they break, multiple code regions lose
    connectivity.

    Args:
        repo_root: Repository root (auto-detected if omitted).
        top_n: Number of top bridges to return (default 10, capped at 100).
        detail_level: "standard" (default) returns full node data;
            "minimal" returns only name, kind, and betweenness.

    Returns:
        Ranked bridges. ``total`` reports the untruncated candidate count and
        ``truncated`` marks that ``top_n`` or the ceiling cut the list.
    """
    _validate_positive_int(top_n, "top_n")

    store, _root = _get_store(repo_root or None)
    try:
        bridges, total, truncated = _bounded(
            find_bridge_nodes(store, top_n=_FETCH_ALL), top_n, _MAX_BRIDGE_NODES,
        )
        if detail_level == "minimal":
            bridges = _project(bridges, _MINIMAL_BRIDGE_FIELDS)
        return {
            "status": "ok",
            "summary": (
                f"{total} bridge node(s) ranked by betweenness"
                + _shown_of(len(bridges), total)
            ),
            "bridge_nodes": bridges,
            "count": len(bridges),
            "total": total,
            "truncated": truncated,
            "next_tool_suggestions": [
                "get_hub_nodes -- find most connected nodes",
                "get_impact_radius -- check blast radius",
                "detect_changes -- see if bridges are affected",
            ],
        }
    finally:
        store.close()


def get_knowledge_gaps_func(
    repo_root: str | None = None,
    max_per_category: int = 15,
    detail_level: str = "standard",
) -> dict[str, Any]:
    """Identify structural weaknesses in the codebase.

    Finds: isolated nodes (disconnected), thin communities
    (< 3 members), untested hotspots (high-degree, no tests),
    and single-file communities.

    Args:
        repo_root: Repository root (auto-detected if omitted).
        max_per_category: Maximum entries per gap category (default 15,
            capped at 50). ``summary`` and ``total_gaps`` always report the
            untruncated counts.
        detail_level: "standard" (default) returns full gap records;
            "minimal" drops file paths and keeps identifying fields only.

    Returns:
        Gaps by category. ``summary`` maps each category to its untruncated
        count and ``truncated`` marks that at least one list was cut.
    """
    _validate_positive_int(max_per_category, "max_per_category")

    store, _root = _get_store(repo_root or None)
    try:
        raw = find_knowledge_gaps(store)
        # Totals must come from the untruncated lists: the summary counts are
        # the whole point of the tool, and capping them would hide the gap.
        totals = {category: len(rows) for category, rows in raw.items()}

        gaps: dict[str, list[dict[str, Any]]] = {}
        truncated = False
        for category, rows in raw.items():
            visible, _total, cut = _bounded(
                rows, max_per_category, _MAX_GAPS_PER_CATEGORY,
            )
            if detail_level == "minimal":
                visible = _project(visible, _MINIMAL_GAP_FIELDS)
            gaps[category] = visible
            truncated = truncated or cut

        total = sum(totals.values())
        return {
            "status": "ok",
            "summary": totals,
            "gaps": gaps,
            "total_gaps": total,
            "truncated": truncated,
            "next_tool_suggestions": [
                "refactor dead_code -- find unused symbols",
                "get_hub_nodes -- find high-impact nodes",
                "get_suggested_questions -- review prompts",
            ],
        }
    finally:
        store.close()


def get_surprising_connections_func(
    repo_root: str | None = None,
    top_n: int = 15,
    detail_level: str = "standard",
) -> dict[str, Any]:
    """Find unexpected architectural coupling in the codebase.

    Scores edges by surprise factors: cross-community,
    cross-language, peripheral-to-hub, cross-test-boundary.

    Args:
        repo_root: Repository root (auto-detected if omitted).
        top_n: Number of top surprises to return (default 15, capped at 100).
        detail_level: "standard" (default) returns full edge records;
            "minimal" returns only source, target, edge_kind, and score.

    Returns:
        Ranked surprising edges. ``total`` reports the untruncated count and
        ``truncated`` marks that ``top_n`` or the ceiling cut the list.
    """
    _validate_positive_int(top_n, "top_n")

    store, _root = _get_store(repo_root or None)
    try:
        surprises, total, truncated = _bounded(
            find_surprising_connections(store, top_n=_FETCH_ALL),
            top_n,
            _MAX_SURPRISING,
        )
        if detail_level == "minimal":
            surprises = _project(surprises, _MINIMAL_SURPRISE_FIELDS)
        return {
            "status": "ok",
            "summary": (
                f"{total} surprising connection(s) ranked by surprise score"
                + _shown_of(len(surprises), total)
            ),
            "surprising_connections": surprises,
            "count": len(surprises),
            "total": total,
            "truncated": truncated,
            "next_tool_suggestions": [
                "get_architecture_overview -- community structure",
                "query_graph callers_of -- trace the coupling",
                "get_bridge_nodes -- find chokepoints",
            ],
        }
    finally:
        store.close()


def get_suggested_questions_func(
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Auto-generate review questions from graph analysis.

    Produces questions about: bridge nodes, untested hubs,
    surprising connections, thin communities, and untested
    hotspots.

    The output is bounded by construction: ``generate_suggested_questions``
    draws at most 3 bridges, 3 hubs, 3 surprises, 2 thin communities and
    2 untested hotspots, so no result cap is needed here.

    Args:
        repo_root: Repository root (auto-detected if omitted).
    """
    store, _root = _get_store(repo_root or None)
    try:
        questions = generate_suggested_questions(store)
        by_priority: dict[str, list[dict[str, Any]]] = {
            "high": [], "medium": [], "low": [],
        }
        for q in questions:
            prio = q.get("priority", "medium")
            if prio in by_priority:
                by_priority[prio].append(q)
        return {
            "questions": questions,
            "count": len(questions),
            "by_priority": {
                k: len(v) for k, v in by_priority.items()
            },
            "next_tool_suggestions": [
                "get_knowledge_gaps -- structural weaknesses",
                "detect_changes -- risk-scored review",
                "get_architecture_overview -- community map",
            ],
        }
    finally:
        store.close()
