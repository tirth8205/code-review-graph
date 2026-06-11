"""Tools 13, 14, 15: community listing, detail, architecture overview."""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..communities import get_architecture_overview, get_communities
from ..context_savings import attach_context_savings
from ..graph import node_to_dict
from ..hints import generate_hints, get_session
from ._common import _get_store

# ---------------------------------------------------------------------------
# Tool 13: list_communities  [EXPLORE]
# ---------------------------------------------------------------------------


def list_communities_func(
    repo_root: str | None = None,
    sort_by: str = "size",
    min_size: int = 0,
    detail_level: str = "standard",
) -> dict[str, Any]:
    """List detected code communities in the codebase.

    [EXPLORE] Retrieves stored communities from the knowledge graph.
    Each community represents a cluster of related code entities
    (functions, classes) detected via the Leiden algorithm or
    file-based grouping.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
        sort_by: Sort column: size, cohesion, or name.
        min_size: Minimum community size to include (default: 0).
        detail_level: "standard" (default) returns full community data;
                      "minimal" returns only name, size, and cohesion
                      per community.

    Returns:
        List of communities with size and cohesion scores.
    """
    store, root = _get_store(repo_root)
    try:
        communities = get_communities(
            store, sort_by=sort_by, min_size=min_size
        )
        if detail_level == "minimal":
            communities = [
                {"name": c["name"], "size": c["size"], "cohesion": c["cohesion"]}
                for c in communities
            ]
        result: dict[str, object] = {
            "status": "ok",
            "summary": f"Found {len(communities)} communities",
            "communities": communities,
        }
        result["_hints"] = generate_hints(
            "list_communities", result, get_session()
        )
        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 14: get_community  [EXPLORE]
# ---------------------------------------------------------------------------


def get_community_func(
    community_name: str | None = None,
    community_id: int | None = None,
    include_members: bool = False,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Get details of a single code community.

    [EXPLORE] Retrieves a community by its database ID or by name match.
    Optionally includes the full list of member nodes.

    Args:
        community_name: Name to search for (partial match). Ignored if
                        community_id given.
        community_id: Database ID of the community.
        include_members: If True, include full member node details.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Community details, or not_found status.
    """
    store, root = _get_store(repo_root)
    try:
        community: dict | None = None
        all_communities = get_communities(store)

        if community_id is not None:
            for c in all_communities:
                if c.get("id") == community_id:
                    community = c
                    break
        elif community_name is not None:
            for c in all_communities:
                if community_name.lower() in c["name"].lower():
                    community = c
                    break

        if community is None:
            return {
                "status": "not_found",
                "summary": (
                    "No community found matching the given criteria."
                ),
            }

        if include_members:
            cid = community.get("id")
            if cid is not None:
                member_nodes = store.get_nodes_by_community_id(cid)
                members = [node_to_dict(n) for n in member_nodes]
                community["member_details"] = members

        result = {
            "status": "ok",
            "summary": (
                f"Community '{community['name']}': "
                f"{community['size']} nodes, "
                f"cohesion {community['cohesion']:.4f}"
            ),
            "community": community,
        }
        result["_hints"] = generate_hints(
            "get_community", result, get_session()
        )
        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 15: get_architecture_overview  [EXPLORE]
# ---------------------------------------------------------------------------


_MINIMAL_COMMUNITY_FIELDS = ("id", "name", "size", "cohesion", "dominant_language")


def _minimal_overview(overview: dict[str, Any]) -> dict[str, Any]:
    """Compress overview for ``detail_level="minimal"``.

    The full overview can exceed 600KB on medium repos because it embeds
    every community's member list and every individual cross-community
    edge. Minimal mode drops member lists and aggregates the edge list
    to one row per community pair with a count and the top edge kinds —
    enough to spot coupling smells without exploding token budgets.
    """
    communities = [
        {k: c[k] for k in _MINIMAL_COMMUNITY_FIELDS if k in c}
        for c in overview.get("communities", [])
    ]
    id_to_name = {c["id"]: c["name"] for c in communities if "id" in c}

    edge_pair_counts: Counter[tuple[int, int]] = Counter()
    edge_pair_kinds: dict[tuple[int, int], Counter[str]] = {}
    for e in overview.get("cross_community_edges", []):
        # Use canonical (low, high) ordering so A↔B and B↔A aggregate together.
        a, b = e["source_community"], e["target_community"]
        pair = (a, b) if a <= b else (b, a)
        edge_pair_counts[pair] += 1
        edge_pair_kinds.setdefault(pair, Counter())[e["edge_kind"]] += 1

    cross_pairs = [
        {
            "source_community": id_to_name.get(a, f"community-{a}"),
            "target_community": id_to_name.get(b, f"community-{b}"),
            "edge_count": count,
            "top_kinds": [k for k, _ in edge_pair_kinds[(a, b)].most_common(3)],
        }
        for (a, b), count in edge_pair_counts.most_common()
    ]
    return {
        "communities": communities,
        "cross_community_edges": cross_pairs,
        "warnings": overview.get("warnings", []),
    }


def get_architecture_overview_func(
    repo_root: str | None = None,
    detail_level: str = "minimal",
) -> dict[str, Any]:
    """Generate an architecture overview based on community structure.

    [EXPLORE] Builds a high-level view of the codebase architecture by
    analyzing community boundaries and cross-community coupling.
    Includes warnings for high coupling between communities.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
        detail_level: "minimal" (default) drops community member lists
                      and aggregates edges to one row per community pair
                      (typical reduction: 600KB -> <5KB);
                      "standard" returns the full overview including
                      per-edge cross-community detail.

    Returns:
        Architecture overview with communities, cross-community edges,
        and warnings.
    """
    store, root = _get_store(repo_root)
    try:
        full_overview = get_architecture_overview(store)
        overview = full_overview
        if detail_level == "minimal":
            overview = _minimal_overview(full_overview)
        n_communities = len(overview["communities"])
        n_cross = len(overview["cross_community_edges"])
        n_warnings = len(overview["warnings"])
        cross_label = (
            "community pairs"
            if detail_level == "minimal"
            else "cross-community edges"
        )
        result = {
            "status": "ok",
            "summary": (
                f"Architecture: {n_communities} communities, "
                f"{n_cross} {cross_label}, "
                f"{n_warnings} warning(s)"
            ),
            **overview,
        }
        result["_hints"] = generate_hints(
            "get_architecture_overview", result, get_session()
        )
        if detail_level == "minimal":
            attach_context_savings(result, original_context=full_overview)
        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()
