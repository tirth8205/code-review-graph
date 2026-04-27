"""Graph snapshot diffing -- compare graph state over time."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .graph import GraphStore

logger = logging.getLogger(__name__)


def take_snapshot(store: GraphStore) -> dict[str, Any]:
    """Take a snapshot of the current graph state.

    Returns a dict with node and edge counts, qualified names,
    and community assignments for later diffing.
    """
    stats = store.get_stats()
    nodes = store.get_all_nodes(exclude_files=False)
    community_map = store.get_all_community_ids()

    return {
        "node_count": stats.total_nodes,
        "edge_count": stats.total_edges,
        "nodes": {
            n.qualified_name: {
                "kind": n.kind,
                "file": n.file_path,
                "community_id": community_map.get(
                    n.qualified_name
                ),
            }
            for n in nodes
        },
        "edges": {
            f"{e.source_qualified}->"
            f"{e.target_qualified}:{e.kind}"
            for e in store.get_all_edges()
        },
    }


def save_snapshot(snapshot: dict, path: Path) -> None:
    """Save a snapshot to a JSON file."""
    data = dict(snapshot)
    if isinstance(data.get("edges"), set):
        data["edges"] = sorted(data["edges"])
    path.write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )


def load_snapshot(path: Path) -> dict:
    """Load a snapshot from a JSON file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data.get("edges"), list):
        data["edges"] = set(data["edges"])
    return data


def diff_snapshots(
    before: dict, after: dict,
) -> dict[str, Any]:
    """Compare two graph snapshots.

    Returns:
        Dict with new_nodes, removed_nodes, new_edges,
        removed_edges, community_changes, and summary
        statistics.
    """
    before_nodes = set(before.get("nodes", {}).keys())
    after_nodes = set(after.get("nodes", {}).keys())
    before_edges = before.get("edges", set())
    after_edges = after.get("edges", set())

    new_nodes = after_nodes - before_nodes
    removed_nodes = before_nodes - after_nodes
    new_edges = after_edges - before_edges
    removed_edges = before_edges - after_edges

    # Community changes for nodes that exist in both
    community_changes = []
    for qn in before_nodes & after_nodes:
        before_cid = before["nodes"][qn].get(
            "community_id"
        )
        after_cid = after["nodes"][qn].get(
            "community_id"
        )
        if before_cid != after_cid:
            community_changes.append({
                "node": qn,
                "before_community": before_cid,
                "after_community": after_cid,
            })

    return {
        "new_nodes": [
            {"qualified_name": qn, **after["nodes"][qn]}
            for qn in sorted(new_nodes)
        ][:100],
        "removed_nodes": sorted(removed_nodes)[:100],
        "new_edges": sorted(new_edges)[:100],
        "removed_edges": sorted(removed_edges)[:100],
        "community_changes": community_changes[:50],
        "summary": {
            "nodes_added": len(new_nodes),
            "nodes_removed": len(removed_nodes),
            "edges_added": len(new_edges),
            "edges_removed": len(removed_edges),
            "community_moves": len(community_changes),
            "before_total": before.get(
                "node_count", 0
            ),
            "after_total": after.get(
                "node_count", 0
            ),
        },
    }
