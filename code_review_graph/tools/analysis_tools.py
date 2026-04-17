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
from ._common import _get_store


def get_hub_nodes_func(
    repo_root: str = "",
    top_n: int = 10,
) -> dict[str, Any]:
    """Find the most connected nodes in the codebase graph.

    Hub nodes have the highest total degree (in + out edges).
    These are architectural hotspots -- changes to them have
    disproportionate blast radius.

    Args:
        repo_root: Repository root (auto-detected if empty).
        top_n: Number of top hubs to return (default 10).
    """
    store, _root = _get_store(repo_root or None)
    hubs = find_hub_nodes(store, top_n=top_n)
    return {
        "hub_nodes": hubs,
        "count": len(hubs),
        "next_tool_suggestions": [
            "get_impact_radius -- check blast radius of a hub",
            "query_graph callers_of -- see what calls a hub",
            "get_bridge_nodes -- find architectural chokepoints",
        ],
    }


def get_bridge_nodes_func(
    repo_root: str = "",
    top_n: int = 10,
) -> dict[str, Any]:
    """Find architectural chokepoints via betweenness centrality.

    Bridge nodes sit on the shortest paths between many node
    pairs. If they break, multiple code regions lose
    connectivity.

    Args:
        repo_root: Repository root (auto-detected if empty).
        top_n: Number of top bridges to return (default 10).
    """
    store, _root = _get_store(repo_root or None)
    bridges = find_bridge_nodes(store, top_n=top_n)
    return {
        "bridge_nodes": bridges,
        "count": len(bridges),
        "next_tool_suggestions": [
            "get_hub_nodes -- find most connected nodes",
            "get_impact_radius -- check blast radius",
            "detect_changes -- see if bridges are affected",
        ],
    }


def get_knowledge_gaps_func(
    repo_root: str = "",
) -> dict[str, Any]:
    """Identify structural weaknesses in the codebase.

    Finds: isolated nodes (disconnected), thin communities
    (< 3 members), untested hotspots (high-degree, no tests),
    and single-file communities.

    Args:
        repo_root: Repository root (auto-detected if empty).
    """
    store, _root = _get_store(repo_root or None)
    gaps = find_knowledge_gaps(store)
    total = sum(len(v) for v in gaps.values())
    return {
        "gaps": gaps,
        "total_gaps": total,
        "summary": {
            "isolated_nodes": len(gaps["isolated_nodes"]),
            "thin_communities": len(
                gaps["thin_communities"]
            ),
            "untested_hotspots": len(
                gaps["untested_hotspots"]
            ),
            "single_file_communities": len(
                gaps["single_file_communities"]
            ),
        },
        "next_tool_suggestions": [
            "refactor dead_code -- find unused symbols",
            "get_hub_nodes -- find high-impact nodes",
            "get_suggested_questions -- review prompts",
        ],
    }


def get_surprising_connections_func(
    repo_root: str = "",
    top_n: int = 15,
) -> dict[str, Any]:
    """Find unexpected architectural coupling in the codebase.

    Scores edges by surprise factors: cross-community,
    cross-language, peripheral-to-hub, cross-test-boundary.

    Args:
        repo_root: Repository root (auto-detected if empty).
        top_n: Number of top surprises to return (default 15).
    """
    store, _root = _get_store(repo_root or None)
    surprises = find_surprising_connections(
        store, top_n=top_n
    )
    return {
        "surprising_connections": surprises,
        "count": len(surprises),
        "next_tool_suggestions": [
            "get_architecture_overview -- community structure",
            "query_graph callers_of -- trace the coupling",
            "get_bridge_nodes -- find chokepoints",
        ],
    }


def get_suggested_questions_func(
    repo_root: str = "",
) -> dict[str, Any]:
    """Auto-generate review questions from graph analysis.

    Produces questions about: bridge nodes, untested hubs,
    surprising connections, thin communities, and untested
    hotspots.

    Args:
        repo_root: Repository root (auto-detected if empty).
    """
    store, _root = _get_store(repo_root or None)
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
