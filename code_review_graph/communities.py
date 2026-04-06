"""Community/cluster detection for the code knowledge graph.

Detects communities of related code nodes using the Leiden algorithm (via igraph,
optional) with a file-based grouping fallback when igraph is not installed.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from typing import Any

from .graph import GraphEdge, GraphNode, GraphStore, _sanitize_name

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional igraph import
# ---------------------------------------------------------------------------

try:
    import igraph as ig  # type: ignore[import-untyped]

    IGRAPH_AVAILABLE = True
except ImportError:
    ig = None  # type: ignore[assignment]
    IGRAPH_AVAILABLE = False

# ---------------------------------------------------------------------------
# Edge weight mapping
# ---------------------------------------------------------------------------

EDGE_WEIGHTS: dict[str, float] = {
    "CALLS": 1.0,
    "IMPORTS_FROM": 0.5,
    "INHERITS": 0.8,
    "IMPLEMENTS": 0.7,
    "CONTAINS": 0.3,
    "TESTED_BY": 0.4,
    "DEPENDS_ON": 0.6,
}

# Common words to filter when generating community names
_COMMON_WORDS = frozenset({
    "get", "set", "self", "init", "new", "create", "update", "delete",
    "add", "remove", "make", "build", "from", "to", "for", "with",
    "the", "and", "test", "main", "run", "do", "is", "has", "on",
    "of", "in", "at", "by", "my", "this", "that", "all", "none",
})


# ---------------------------------------------------------------------------
# Community naming
# ---------------------------------------------------------------------------


def _generate_community_name(members: list[GraphNode]) -> str:
    """Generate a meaningful name for a community of nodes.

    Algorithm:
    1. Find most common module/file prefix among members
    2. If a dominant class exists (>40% of nodes), use its name
    3. Fallback: most frequent keyword in function/class names
    4. Format: "{prefix}-{keyword}"
    """
    if not members:
        return "empty"

    # 1. Find common file prefix
    file_paths = [m.file_path for m in members]
    prefix = _extract_file_prefix(file_paths)

    # 2. Check for dominant class
    class_names = [m.name for m in members if m.kind == "Class"]
    if class_names:
        class_counts = Counter(class_names)
        top_class, top_count = class_counts.most_common(1)[0]
        if top_count > len(members) * 0.4:
            if prefix:
                return f"{prefix}-{_to_slug(top_class)}"
            return _to_slug(top_class)

    # 3. Most frequent keyword from function/class names
    keywords = _extract_keywords(members)
    keyword = keywords[0] if keywords else ""

    if prefix and keyword:
        return f"{prefix}-{keyword}"
    if prefix:
        return prefix
    if keyword:
        return keyword
    return "cluster"


def _extract_file_prefix(file_paths: list[str]) -> str:
    """Find the most common short directory or module name from file paths."""
    if not file_paths:
        return ""
    # Extract the parent directory or file stem
    parts: list[str] = []
    for fp in file_paths:
        # Use the last directory component or file stem
        segments = fp.replace("\\", "/").split("/")
        # Take the parent dir if it exists, otherwise the file stem
        if len(segments) >= 2:
            parts.append(segments[-2])
        else:
            stem = segments[-1].rsplit(".", 1)[0]
            parts.append(stem)

    counts = Counter(parts)
    top_part, _ = counts.most_common(1)[0]
    return _to_slug(top_part)


def _extract_keywords(members: list[GraphNode]) -> list[str]:
    """Extract the most frequent meaningful keywords from member names."""
    word_counts: Counter[str] = Counter()
    for m in members:
        if m.kind in ("Function", "Class", "Test", "Type"):
            words = _split_name(m.name)
            for w in words:
                wl = w.lower()
                if wl not in _COMMON_WORDS and len(wl) > 1:
                    word_counts[wl] += 1

    if not word_counts:
        return []
    return [w for w, _ in word_counts.most_common(5)]


def _split_name(name: str) -> list[str]:
    """Split a camelCase or snake_case name into words."""
    # Insert boundary before uppercase letters for camelCase
    s = re.sub(r"([a-z])([A-Z])", r"\1_\2", name)
    # Split on underscores, hyphens, dots
    return [p for p in re.split(r"[_\-.\s]+", s) if p]


def _to_slug(s: str) -> str:
    """Convert a string to a short lowercase slug."""
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:30]


# ---------------------------------------------------------------------------
# Cohesion calculation
# ---------------------------------------------------------------------------


def _compute_cohesion(
    member_qns: set[str], all_edges: list[GraphEdge]
) -> float:
    """Compute cohesion: internal_edges / (internal_edges + external_edges)."""
    internal = 0
    external = 0
    for e in all_edges:
        src_in = e.source_qualified in member_qns
        tgt_in = e.target_qualified in member_qns
        if src_in or tgt_in:
            if src_in and tgt_in:
                internal += 1
            else:
                external += 1
    total = internal + external
    if total == 0:
        return 0.0
    return internal / total


# ---------------------------------------------------------------------------
# Leiden-based community detection (igraph)
# ---------------------------------------------------------------------------


def _detect_leiden(
    nodes: list[GraphNode], edges: list[GraphEdge], min_size: int
) -> list[dict[str, Any]]:
    """Detect communities using Leiden algorithm via igraph."""
    if ig is None:
        return []

    # Build mapping from qualified_name to index
    qn_to_idx: dict[str, int] = {}
    idx_to_node: dict[int, GraphNode] = {}
    for i, node in enumerate(nodes):
        qn_to_idx[node.qualified_name] = i
        idx_to_node[i] = node

    if not qn_to_idx:
        return []

    # Build igraph graph (undirected, weighted)
    g = ig.Graph(n=len(qn_to_idx), directed=False)
    edge_list: list[tuple[int, int]] = []
    weights: list[float] = []
    seen_edges: set[tuple[int, int]] = set()

    for e in edges:
        src_idx = qn_to_idx.get(e.source_qualified)
        tgt_idx = qn_to_idx.get(e.target_qualified)
        if src_idx is not None and tgt_idx is not None and src_idx != tgt_idx:
            pair = (min(src_idx, tgt_idx), max(src_idx, tgt_idx))
            if pair not in seen_edges:
                seen_edges.add(pair)
                edge_list.append(pair)
                weights.append(EDGE_WEIGHTS.get(e.kind, 0.5))

    if not edge_list:
        # No edges — fall back to file grouping
        return _detect_file_based(nodes, edges, min_size)

    g.add_edges(edge_list)
    g.es["weight"] = weights

    # Run Leiden
    partition = g.community_leiden(
        objective_function="modularity",
        weights="weight",
    )

    # Build communities from partition
    communities: list[dict[str, Any]] = []
    for cluster_ids in partition:
        if len(cluster_ids) < min_size:
            continue
        members = [idx_to_node[i] for i in cluster_ids if i in idx_to_node]
        if len(members) < min_size:
            continue
        member_qns = {m.qualified_name for m in members}
        cohesion = _compute_cohesion(member_qns, edges)
        lang_counts = Counter(m.language for m in members if m.language)
        dominant_lang = lang_counts.most_common(1)[0][0] if lang_counts else ""
        name = _generate_community_name(members)

        communities.append({
            "name": name,
            "level": 0,
            "size": len(members),
            "cohesion": round(cohesion, 4),
            "dominant_language": dominant_lang,
            "description": f"Community of {len(members)} nodes",
            "members": [m.qualified_name for m in members],
            "member_qns": member_qns,
        })

    # Second pass: split large communities (>50 nodes)
    final: list[dict[str, Any]] = []
    for comm in communities:
        if comm["size"] > 50:
            sub_nodes = [n for n in nodes if n.qualified_name in comm["member_qns"]]
            sub_edges = [
                e for e in edges
                if e.source_qualified in comm["member_qns"]
                and e.target_qualified in comm["member_qns"]
            ]
            subs = _detect_leiden_sub(sub_nodes, sub_edges, min_size, parent_name=comm["name"])
            if len(subs) >= 2:
                final.extend(subs)
            else:
                final.append(comm)
        else:
            final.append(comm)

    return final


def _detect_leiden_sub(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    min_size: int,
    parent_name: str,
) -> list[dict[str, Any]]:
    """Second-pass Leiden on a large community for sub-communities."""
    if ig is None:
        return []

    qn_to_idx: dict[str, int] = {}
    idx_to_node: dict[int, GraphNode] = {}
    for i, node in enumerate(nodes):
        qn_to_idx[node.qualified_name] = i
        idx_to_node[i] = node

    g = ig.Graph(n=len(qn_to_idx), directed=False)
    edge_list: list[tuple[int, int]] = []
    weights: list[float] = []
    seen_edges: set[tuple[int, int]] = set()

    for e in edges:
        src_idx = qn_to_idx.get(e.source_qualified)
        tgt_idx = qn_to_idx.get(e.target_qualified)
        if src_idx is not None and tgt_idx is not None and src_idx != tgt_idx:
            pair = (min(src_idx, tgt_idx), max(src_idx, tgt_idx))
            if pair not in seen_edges:
                seen_edges.add(pair)
                edge_list.append(pair)
                weights.append(EDGE_WEIGHTS.get(e.kind, 0.5))

    if not edge_list:
        return []

    g.add_edges(edge_list)
    g.es["weight"] = weights

    partition = g.community_leiden(
        objective_function="modularity",
        weights="weight",
    )

    subs: list[dict[str, Any]] = []
    for idx, cluster_ids in enumerate(partition):
        if len(cluster_ids) < min_size:
            continue
        members = [idx_to_node[i] for i in cluster_ids if i in idx_to_node]
        if len(members) < min_size:
            continue
        member_qns = {m.qualified_name for m in members}
        cohesion = _compute_cohesion(member_qns, edges)
        lang_counts = Counter(m.language for m in members if m.language)
        dominant_lang = lang_counts.most_common(1)[0][0] if lang_counts else ""
        name = _generate_community_name(members)

        subs.append({
            "name": f"{parent_name}/{name}",
            "level": 1,
            "size": len(members),
            "cohesion": round(cohesion, 4),
            "dominant_language": dominant_lang,
            "description": f"Sub-community of {len(members)} nodes within {parent_name}",
            "members": [m.qualified_name for m in members],
            "member_qns": member_qns,
        })

    return subs


# ---------------------------------------------------------------------------
# File-based fallback community detection
# ---------------------------------------------------------------------------


def _detect_file_based(
    nodes: list[GraphNode], edges: list[GraphEdge], min_size: int
) -> list[dict[str, Any]]:
    """Group nodes by file_path when igraph is not available."""
    by_file: dict[str, list[GraphNode]] = defaultdict(list)
    for n in nodes:
        by_file[n.file_path].append(n)

    communities: list[dict[str, Any]] = []
    for file_path, members in by_file.items():
        if len(members) < min_size:
            continue
        member_qns = {m.qualified_name for m in members}
        cohesion = _compute_cohesion(member_qns, edges)
        lang_counts = Counter(m.language for m in members if m.language)
        dominant_lang = lang_counts.most_common(1)[0][0] if lang_counts else ""
        name = _generate_community_name(members)

        communities.append({
            "name": name,
            "level": 0,
            "size": len(members),
            "cohesion": round(cohesion, 4),
            "dominant_language": dominant_lang,
            "description": f"File-based community: {file_path}",
            "members": [m.qualified_name for m in members],
            "member_qns": member_qns,
        })

    return communities


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_communities(
    store: GraphStore, min_size: int = 2
) -> list[dict[str, Any]]:
    """Detect communities in the code graph.

    Uses the Leiden algorithm via igraph if available, otherwise falls back to
    file-based grouping.

    Args:
        store: The GraphStore instance.
        min_size: Minimum number of nodes for a community to be included.

    Returns:
        List of community dicts with keys: name, level, size, cohesion,
        dominant_language, description, members, member_qns.
    """
    # Gather all nodes (exclude File nodes to focus on code entities)
    all_edges = store.get_all_edges()
    all_files = store.get_all_files()

    nodes: list[GraphNode] = []
    for fp in all_files:
        nodes.extend(store.get_nodes_by_file(fp))

    # Also gather nodes from files referenced in edges but not in all_files
    edge_files: set[str] = set()
    for e in all_edges:
        edge_files.add(e.file_path)
    for fp in edge_files - set(all_files):
        nodes.extend(store.get_nodes_by_file(fp))

    # Deduplicate by qualified_name
    seen_qns: set[str] = set()
    unique_nodes: list[GraphNode] = []
    for n in nodes:
        if n.qualified_name not in seen_qns:
            seen_qns.add(n.qualified_name)
            unique_nodes.append(n)

    if IGRAPH_AVAILABLE:
        logger.info("Detecting communities with Leiden algorithm (igraph)")
        results = _detect_leiden(unique_nodes, all_edges, min_size)
    else:
        logger.info("igraph not available, using file-based community detection")
        results = _detect_file_based(unique_nodes, all_edges, min_size)

    # Convert member_qns (internal set) to a list for serialization safety,
    # then strip it from the returned dicts to avoid leaking internal state.
    for comm in results:
        if "member_qns" in comm:
            comm["member_qns"] = list(comm["member_qns"])
            del comm["member_qns"]

    return results


def incremental_detect_communities(
    store: GraphStore,
    changed_files: list[str],
    min_size: int = 2,
) -> int:
    """Re-detect communities only if changed files affect existing communities.

    If no existing communities contain nodes from changed files, skips
    re-detection entirely (the common case for small changes). Otherwise
    re-runs full community detection.

    Args:
        store: The GraphStore instance.
        changed_files: List of file paths that have changed.
        min_size: Minimum number of nodes for a community to be included.

    Returns:
        Number of communities detected, or 0 if skipped.
    """
    if not changed_files:
        return 0

    conn = store._conn

    # Check if any communities are affected
    placeholders = ",".join("?" * len(changed_files))
    affected = conn.execute(
        f"SELECT COUNT(DISTINCT community_id) FROM nodes "  # nosec B608
        f"WHERE community_id IS NOT NULL AND file_path IN ({placeholders})",
        changed_files,
    ).fetchone()

    if not affected or affected[0] == 0:
        return 0  # No communities affected, skip

    # Re-run full community detection (correct and fast enough)
    communities = detect_communities(store, min_size=min_size)
    return store_communities(store, communities)


def store_communities(
    store: GraphStore, communities: list[dict[str, Any]]
) -> int:
    """Store detected communities in the database.

    Clears existing communities and community_id assignments, then inserts
    the new communities and updates node community_id references.

    Args:
        store: The GraphStore instance.
        communities: List of community dicts from detect_communities().

    Returns:
        Number of communities stored.
    """
    # NOTE: store_communities uses _conn directly because it performs
    # multi-statement batch writes (DELETE + INSERT loop + UPDATE loop)
    # that are tightly coupled to the DB transaction lifecycle.
    conn = store._conn

    # Clear existing data
    conn.execute("DELETE FROM communities")
    conn.execute("UPDATE nodes SET community_id = NULL")

    count = 0
    for comm in communities:
        cursor = conn.execute(
            """INSERT INTO communities (name, level, cohesion, size, dominant_language, description)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                comm["name"],
                comm.get("level", 0),
                comm.get("cohesion", 0.0),
                comm["size"],
                comm.get("dominant_language", ""),
                comm.get("description", ""),
            ),
        )
        community_id = cursor.lastrowid

        # Update community_id on member nodes
        member_qns = comm.get("members", [])
        for qn in member_qns:
            conn.execute(
                "UPDATE nodes SET community_id = ? WHERE qualified_name = ?",
                (community_id, qn),
            )
        count += 1

    conn.commit()
    return count


def get_communities(
    store: GraphStore, sort_by: str = "size", min_size: int = 0
) -> list[dict[str, Any]]:
    """Retrieve stored communities from the database.

    Args:
        store: The GraphStore instance.
        sort_by: Column to sort by ("size", "cohesion", "name").
        min_size: Minimum community size to include.

    Returns:
        List of community dicts.
    """
    valid_sorts = {"size", "cohesion", "name"}
    if sort_by not in valid_sorts:
        sort_by = "size"

    order = "DESC" if sort_by in ("size", "cohesion") else "ASC"

    # NOTE: get_communities reads the communities table which has no
    # dedicated GraphStore method (it's a domain-specific table managed
    # entirely by the communities module).  We use _conn for this query.
    rows = store._conn.execute(
        f"SELECT * FROM communities WHERE size >= ? ORDER BY {sort_by} {order}",  # nosec B608
        (min_size,),
    ).fetchall()

    communities: list[dict[str, Any]] = []
    for row in rows:
        # Fetch member qualified names for this community
        member_qns = [
            _sanitize_name(qn)
            for qn in store.get_community_member_qns(row["id"])
        ]

        communities.append({
            "id": row["id"],
            "name": _sanitize_name(row["name"]),
            "level": row["level"],
            "cohesion": row["cohesion"],
            "size": row["size"],
            "dominant_language": row["dominant_language"] or "",
            "description": _sanitize_name(row["description"] or ""),
            "members": member_qns,
        })

    return communities


def get_architecture_overview(store: GraphStore) -> dict[str, Any]:
    """Generate an architecture overview based on community structure.

    Builds a node-to-community mapping, counts cross-community edges,
    and generates warnings for high coupling.

    Args:
        store: The GraphStore instance.

    Returns:
        Dict with keys: communities, cross_community_edges, warnings.
    """
    communities = get_communities(store)

    # Build node -> community_id mapping
    node_to_community: dict[str, int] = {}
    for comm in communities:
        comm_id = comm.get("id", 0)
        for qn in comm.get("members", []):
            node_to_community[qn] = comm_id

    # Count cross-community edges
    all_edges = store.get_all_edges()
    cross_edges: list[dict[str, Any]] = []
    cross_counts: Counter[tuple[int, int]] = Counter()

    for e in all_edges:
        src_comm = node_to_community.get(e.source_qualified)
        tgt_comm = node_to_community.get(e.target_qualified)
        if (
            src_comm is not None
            and tgt_comm is not None
            and src_comm != tgt_comm
        ):
            pair = (min(src_comm, tgt_comm), max(src_comm, tgt_comm))
            cross_counts[pair] += 1
            cross_edges.append({
                "source_community": src_comm,
                "target_community": tgt_comm,
                "edge_kind": e.kind,
                "source": _sanitize_name(e.source_qualified),
                "target": _sanitize_name(e.target_qualified),
            })

    # Generate warnings for high coupling
    warnings: list[str] = []
    comm_name_map = {c.get("id", 0): c["name"] for c in communities}
    for (c1, c2), count in cross_counts.most_common():
        if count > 10:
            name1 = comm_name_map.get(c1, f"community-{c1}")
            name2 = comm_name_map.get(c2, f"community-{c2}")
            warnings.append(
                f"High coupling ({count} edges) between "
                f"'{name1}' and '{name2}'"
            )

    return {
        "communities": communities,
        "cross_community_edges": cross_edges,
        "warnings": warnings,
    }
