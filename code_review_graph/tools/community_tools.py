"""Tools 13, 14, 15: community listing, detail, architecture overview."""

from __future__ import annotations

from typing import Any

from ..communities import get_architecture_overview, get_communities
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
    detail_level: str = "standard",
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
        detail_level: "standard" returns full community metadata; "minimal"
            returns name, size, cohesion, and dominant_language only.

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

        summary_str = (
            f"Community '{community['name']}': "
            f"{community['size']} nodes, "
            f"cohesion {community['cohesion']:.4f}"
        )

        if detail_level == "minimal":
            minimal_community = {
                k: community[k]
                for k in ("name", "size", "cohesion", "dominant_language")
                if k in community
            }
            result = {
                "status": "ok",
                "summary": summary_str,
                "community": minimal_community,
            }
            result["_hints"] = generate_hints(
                "get_community", result, get_session()
            )
            return result

        result = {
            "status": "ok",
            "summary": summary_str,
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


def get_architecture_overview_func(
    repo_root: str | None = None,
    detail_level: str = "standard",
) -> dict[str, Any]:
    """Generate an architecture overview based on community structure.

    [EXPLORE] Builds a high-level view of the codebase architecture by
    analyzing community boundaries and cross-community coupling.
    Includes warnings for high coupling between communities.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
        detail_level: "standard" returns full overview with all communities
            and cross-community edges; "minimal" returns community names,
            counts, and warnings only (no edge details).

    Returns:
        Architecture overview with communities, cross-community edges,
        and warnings.
    """
    store, root = _get_store(repo_root)
    try:
        overview = get_architecture_overview(store)
        n_communities = len(overview["communities"])
        n_cross = len(overview["cross_community_edges"])
        n_warnings = len(overview["warnings"])
        summary_str = (
            f"Architecture: {n_communities} communities, "
            f"{n_cross} cross-community edges, "
            f"{n_warnings} warning(s)"
        )
        if detail_level == "minimal":
            minimal_communities = [
                {"name": c["name"], "size": c.get("size", 0)}
                for c in overview["communities"]
            ]
            result: dict[str, Any] = {
                "status": "ok",
                "summary": summary_str,
                "communities": minimal_communities,
                "cross_community_edge_count": n_cross,
                "warnings": overview["warnings"],
            }
            result["_hints"] = generate_hints(
                "get_architecture_overview", result, get_session()
            )
            return result
        result = {
            "status": "ok",
            "summary": summary_str,
            **overview,
        }
        result["_hints"] = generate_hints(
            "get_architecture_overview", result, get_session()
        )
        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()
