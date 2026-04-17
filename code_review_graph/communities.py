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


def _compute_cohesion_batch(
    community_member_qns: list[set[str]],
    all_edges: list[GraphEdge],
) -> list[float]:
    """Compute cohesion for multiple communities in a single O(edges) pass.

    Builds a ``qualified_name -> community_index`` reverse map (each node
    appears in at most one community since all callers produce partitions),
    then walks every edge exactly once, bucketing it into internal/external
    counters per community.

    Total work: O(edges + sum(|members|)) instead of
    O(edges * communities) for naive per-community cohesion.

    Returns a list of cohesion scores aligned with ``community_member_qns``.
    """
    qn_to_idx: dict[str, int] = {}
    for idx, members in enumerate(community_member_qns):
        for qn in members:
            qn_to_idx[qn] = idx

    n = len(community_member_qns)
    internal = [0] * n
    external = [0] * n

    for e in all_edges:
        sc = qn_to_idx.get(e.source_qualified)
        tc = qn_to_idx.get(e.target_qualified)
        if sc is None and tc is None:
            continue
        if sc == tc:
            # Safe: sc is not None here (sc == tc and not both None).
            assert sc is not None
            internal[sc] += 1
        else:
            if sc is not None:
                external[sc] += 1
            if tc is not None:
                external[tc] += 1

    results: list[float] = []
    for i in range(n):
        total = internal[i] + external[i]
        results.append(internal[i] / total if total > 0 else 0.0)
    return results


def _build_adjacency(edges: list[GraphEdge]) -> dict[str, list[str]]:
    """Build adjacency list from edges (one pass over all edges)."""
    adj: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        adj[e.source_qualified].append(e.target_qualified)
        adj[e.target_qualified].append(e.source_qualified)
    return adj


def _compute_cohesion(
    member_qns: set[str],
    all_edges: list[GraphEdge],
    adj: dict[str, list[str]] | None = None,
) -> float:
    """Compute cohesion: internal_edges / (internal_edges + external_edges).

    For multiple communities, prefer :func:`_compute_cohesion_batch`, which
    runs in O(edges) total instead of O(edges) per community.
    """
    return _compute_cohesion_batch([member_qns], all_edges)[0]


# ---------------------------------------------------------------------------
# Leiden-based community detection (igraph)
# ---------------------------------------------------------------------------


def _detect_leiden(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    min_size: int,
    adj: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Detect communities using Leiden algorithm via igraph.

    Caps Leiden at ``n_iterations=2`` (sufficient for code dependency graphs)
    and skips the recursive sub-community splitting pass that caused
    exponential blow-up on large repos (>100k nodes).
    """
    if ig is None:
        return []

    qn_to_idx: dict[str, int] = {}
    idx_to_node: dict[int, GraphNode] = {}
    for i, node in enumerate(nodes):
        qn_to_idx[node.qualified_name] = i
        idx_to_node[i] = node

    if not qn_to_idx:
        return []

    logger.info("Building igraph with %d nodes...", len(qn_to_idx))

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
        return _detect_file_based(nodes, edges, min_size, adj=adj)

    g.add_edges(edge_list)
    g.es["weight"] = weights

    # Run Leiden -- scale resolution inversely with graph size to get
    # coarser clusters on large repos.  Default resolution=1.0 produces
    # thousands of tiny communities for 30k+ node graphs.
    import math
    n_nodes = g.vcount()
    resolution = max(0.05, 1.0 / math.log10(max(n_nodes, 10)))

    logger.info(
        "Running Leiden on %d nodes, %d edges...",
        g.vcount(), g.ecount(),
    )

    partition = g.community_leiden(
        objective_function="modularity",
        weights="weight",
        resolution=resolution,
        n_iterations=2,
    )

    logger.info(
        "Leiden complete, found %d partitions. Computing cohesion...",
        len(partition),
    )

    pending: list[tuple[list[GraphNode], set[str]]] = []
    for cluster_ids in partition:
        if len(cluster_ids) < min_size:
            continue
        members = [idx_to_node[i] for i in cluster_ids if i in idx_to_node]
        if len(members) < min_size:
            continue
        member_qns = {m.qualified_name for m in members}
        pending.append((members, member_qns))

    cohesions = _compute_cohesion_batch([p[1] for p in pending], edges)

    communities: list[dict[str, Any]] = []
    for (members, member_qns), cohesion in zip(pending, cohesions):
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

    logger.info("Community detection complete: %d communities", len(communities))
    return communities


# ---------------------------------------------------------------------------
# File-based fallback community detection
# ---------------------------------------------------------------------------


def _detect_file_based(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    min_size: int,
    adj: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Group nodes by directory when Leiden is unavailable or over-fragments.

    Strips the longest common directory prefix from all file paths, then
    adaptively picks a grouping depth that yields 10-200 communities.
    """
    # Collect all directory paths (normalized, without filename)
    all_dir_parts: list[list[str]] = []
    for n in nodes:
        parts = n.file_path.replace("\\", "/").split("/")
        all_dir_parts.append([p for p in parts[:-1] if p])

    # Find the longest common prefix among directory parts
    prefix_len = 0
    if all_dir_parts:
        shortest = min(len(p) for p in all_dir_parts)
        for i in range(shortest):
            seg = all_dir_parts[0][i]
            if all(p[i] == seg for p in all_dir_parts):
                prefix_len = i + 1
            else:
                break

    def _group_at_depth(depth: int) -> dict[str, list[GraphNode]]:
        groups: dict[str, list[GraphNode]] = defaultdict(list)
        for n in nodes:
            parts = n.file_path.replace("\\", "/").split("/")
            dir_parts = [p for p in parts[:-1] if p]
            remainder = dir_parts[prefix_len:]
            if remainder:
                key = "/".join(remainder[:depth])
            else:
                key = parts[-1].rsplit(".", 1)[0] if parts else "root"
            groups[key].append(n)
        return groups

    # Try increasing depths until we get 10-200 qualifying groups
    max_depth = max((len(p) - prefix_len for p in all_dir_parts), default=0)
    best_groups = _group_at_depth(1)  # depth=1 always works (file stem fallback)
    for depth in range(1, max_depth + 1):
        groups = _group_at_depth(depth)
        qualifying = sum(1 for v in groups.values() if len(v) >= min_size)
        best_groups = groups
        if qualifying >= 10:
            break

    by_dir = best_groups

    # Pre-filter to communities meeting min_size and collect their member
    # sets so we can batch-compute all cohesions in a single O(edges) pass.
    # Without this, per-community cohesion is O(edges * files), which makes
    # community detection effectively hang on large repos.
    pending: list[tuple[str, list[GraphNode], set[str]]] = []
    for dir_path, members in by_dir.items():
        if len(members) < min_size:
            continue
        member_qns = {m.qualified_name for m in members}
        pending.append((dir_path, members, member_qns))

    cohesions = _compute_cohesion_batch([p[2] for p in pending], edges)

    communities: list[dict[str, Any]] = []
    for (dir_path, members, member_qns), cohesion in zip(pending, cohesions):
        lang_counts = Counter(m.language for m in members if m.language)
        dominant_lang = lang_counts.most_common(1)[0][0] if lang_counts else ""
        name = _generate_community_name(members)

        communities.append({
            "name": name,
            "level": 0,
            "size": len(members),
            "cohesion": round(cohesion, 4),
            "dominant_language": dominant_lang,
            "description": f"Directory-based community: {dir_path}",
            "members": [m.qualified_name for m in members],
            "member_qns": member_qns,
        })

    return communities


# ---------------------------------------------------------------------------
# Oversized community splitting
# ---------------------------------------------------------------------------


def _split_oversized(
    communities: list[dict],
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    threshold_pct: float = 0.25,
    min_split_size: int = 10,
) -> list[dict]:
    """Recursively split communities that exceed threshold_pct of total.

    Uses Leiden on the subgraph of oversized communities. If igraph is
    not available, returns communities unchanged.
    """
    if not IGRAPH_AVAILABLE:
        return communities

    total = sum(
        c.get("size", len(c.get("members", [])))
        for c in communities
    )
    if total == 0:
        return communities

    threshold = max(int(total * threshold_pct), min_split_size)
    result: list[dict] = []
    next_id = max(
        (c.get("id", 0) for c in communities), default=0
    ) + 1

    for comm in communities:
        members = set(comm.get("members", []))
        if len(members) <= threshold:
            result.append(comm)
            continue

        # Build subgraph for this community
        member_nodes = [
            n for n in nodes
            if n.qualified_name in members
        ]
        member_edges = [
            e for e in edges
            if (
                e.source_qualified in members
                and e.target_qualified in members
            )
        ]

        if len(member_nodes) < min_split_size:
            result.append(comm)
            continue

        # Run Leiden on subgraph
        qn_to_idx = {
            n.qualified_name: i
            for i, n in enumerate(member_nodes)
        }
        ig_edges: list[tuple[int, int]] = []
        ig_weights: list[float] = []
        for e in member_edges:
            si = qn_to_idx.get(e.source_qualified)
            ti = qn_to_idx.get(e.target_qualified)
            if si is not None and ti is not None and si != ti:
                ig_edges.append((si, ti))
                ig_weights.append(
                    EDGE_WEIGHTS.get(e.kind, 0.5)
                )

        if not ig_edges:
            result.append(comm)
            continue

        try:
            g = ig.Graph(
                n=len(member_nodes),
                edges=ig_edges,
                directed=False,
            )
            g.es["weight"] = ig_weights
            partition = g.community_leiden(
                objective_function="modularity",
                weights="weight",
                resolution=0.5,
            )

            sub_communities: dict[int, list[str]] = {}
            for idx, cid in enumerate(partition.membership):
                sub_communities.setdefault(cid, []).append(
                    member_nodes[idx].qualified_name
                )

            if len(sub_communities) <= 1:
                result.append(comm)
                continue

            parent_id = comm.get("id", 0)
            comm_name = comm.get("name", "")
            for sub_members in sub_communities.values():
                sub_comm = {
                    "id": next_id,
                    "name": comm_name + f"-sub{next_id}",
                    "level": comm.get("level", 0) + 1,
                    "parent_id": parent_id,
                    "members": sub_members,
                    "size": len(sub_members),
                    "cohesion": 0.0,
                    "dominant_language": comm.get(
                        "dominant_language"
                    ),
                    "description": (
                        f"Split from {comm_name}"
                    ),
                }
                result.append(sub_comm)
                next_id += 1

            logger.info(
                "Split oversized community '%s' "
                "(%d members) into %d",
                comm_name,
                len(members),
                len(sub_communities),
            )
        except Exception:
            logger.warning(
                "Failed to split community '%s', "
                "keeping as-is",
                comm.get("name", ""),
                exc_info=True,
            )
            result.append(comm)

    return result


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
    unique_nodes = store.get_all_nodes(exclude_files=True)

    # Build adjacency index once for fast cohesion computation
    adj = _build_adjacency(all_edges)

    logger.info(
        "Loaded %d unique nodes, %d edges",
        len(unique_nodes), len(all_edges),
    )

    if IGRAPH_AVAILABLE:
        logger.info("Detecting communities with Leiden algorithm (igraph)")
        results = _detect_leiden(unique_nodes, all_edges, min_size, adj=adj)
    else:
        logger.info("igraph not available, using file-based community detection")
        results = _detect_file_based(unique_nodes, all_edges, min_size, adj=adj)

    # Split oversized communities
    results = _split_oversized(
        results, unique_nodes, all_edges,
    )

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

    # Wrap in explicit transaction so the DELETE + INSERT + UPDATE
    # sequence is atomic — no partial community data on crash.
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM communities")
        conn.execute("UPDATE nodes SET community_id = NULL")

        count = 0
        for comm in communities:
            cursor = conn.execute(
                """INSERT INTO communities
                       (name, level, cohesion, size, dominant_language, description)
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

            # Batch update community_id on member nodes
            member_qns = comm.get("members", [])
            if member_qns:
                placeholders = ",".join("?" * len(member_qns))
                conn.execute(
                    f"UPDATE nodes SET community_id = ? WHERE qualified_name IN ({placeholders})",  # nosec B608
                    [community_id] + member_qns,
                )
            count += 1

        conn.commit()
    except BaseException:
        conn.rollback()
        raise
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


_TEST_COMMUNITY_RE = re.compile(
    r"(^test[-/]|[-/]test([:/]|$)|it:should|describe:|spec[-/]|[-/]spec$)",
    re.IGNORECASE,
)


def _is_test_community(name: str) -> bool:
    """Return True if a community name indicates it is test-dominated."""
    return bool(_TEST_COMMUNITY_RE.search(name))


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
        # TESTED_BY edges are expected cross-community coupling (test → code),
        # not an architectural smell.
        if e.kind == "TESTED_BY":
            continue
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

    # Generate warnings for high coupling, skipping test-dominated pairs.
    warnings: list[str] = []
    comm_name_map = {c.get("id", 0): c["name"] for c in communities}
    for (c1, c2), count in cross_counts.most_common():
        if count > 10:
            name1 = comm_name_map.get(c1, f"community-{c1}")
            name2 = comm_name_map.get(c2, f"community-{c2}")
            # Skip pairs where either community is test-dominated — coupling
            # between test and production code is expected, not architectural.
            if _is_test_community(name1) or _is_test_community(name2):
                continue
            warnings.append(
                f"High coupling ({count} edges) between "
                f"'{name1}' and '{name2}'"
            )

    return {
        "communities": communities,
        "cross_community_edges": cross_edges,
        "warnings": warnings,
    }
