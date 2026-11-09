"""Graph analysis: hub detection, bridge nodes, knowledge gaps, import cycles,
surprise scoring, suggested questions."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict, deque
from pathlib import Path

from .constants import (
    IMPORT_CONTEXT_DEFERRED,
    IMPORT_CONTEXT_KEY,
    IMPORT_CONTEXT_PARENT_PACKAGE,
    IMPORT_CONTEXT_TYPE_ONLY,
)
from .graph import GraphEdge, GraphStore, _sanitize_name
from .parser import normalize_file_path

logger = logging.getLogger(__name__)

# How much of one import cycle is spelled out. A cycle can hold hundreds of files
# and each row carries paths, so the rest is told as a count.
_MAX_CYCLE_FILES = 10
_MAX_CYCLE_HOPS = 10


def find_hub_nodes(store: GraphStore, top_n: int = 10) -> list[dict]:
    """Find the most connected nodes (highest in+out degree), excluding File nodes.

    Returns list of dicts with: name, qualified_name, kind, file,
    in_degree, out_degree, total_degree, community_id
    """
    # Build degree counts from all edges
    edges = store.get_all_edges()
    in_degree: dict[str, int] = Counter()
    out_degree: dict[str, int] = Counter()
    for e in edges:
        out_degree[e.source_qualified] += 1
        in_degree[e.target_qualified] += 1

    # Get all non-File nodes
    nodes = store.get_all_nodes(exclude_files=True)
    community_map = store.get_all_community_ids()

    scored = []
    for n in nodes:
        qn = n.qualified_name
        ind = in_degree.get(qn, 0)
        outd = out_degree.get(qn, 0)
        total = ind + outd
        if total == 0:
            continue
        scored.append({
            "name": _sanitize_name(n.name),
            "qualified_name": n.qualified_name,
            "kind": n.kind,
            "file": n.file_path,
            "in_degree": ind,
            "out_degree": outd,
            "total_degree": total,
            "community_id": community_map.get(qn),
        })

    scored.sort(
        key=lambda x: x.get("total_degree", 0),  # type: ignore[arg-type,return-value]
        reverse=True,
    )
    return scored[:top_n]


def find_bridge_nodes(
    store: GraphStore, top_n: int = 10
) -> list[dict]:
    """Find nodes with highest betweenness centrality.

    These are architectural chokepoints that sit on shortest paths
    between many node pairs. If they break, multiple communities
    lose connectivity.

    Returns list of dicts with: name, qualified_name, kind, file,
    betweenness, community_id
    """
    import networkx as nx

    # Build the graph — use cached version if available
    nxg = store._build_networkx_graph()

    # Compute betweenness centrality (approximate for large graphs)
    n_nodes = nxg.number_of_nodes()
    if n_nodes > 5000:
        # Sample-based approximation for large graphs
        k = min(500, n_nodes)
        bc = nx.betweenness_centrality(nxg, k=k, normalized=True)
    elif n_nodes > 0:
        bc = nx.betweenness_centrality(nxg, normalized=True)
    else:
        return []

    community_map = store.get_all_community_ids()
    node_map = {
        n.qualified_name: n
        for n in store.get_all_nodes(exclude_files=True)
    }

    results = []
    for qn, score in bc.items():
        if score <= 0 or qn not in node_map:
            continue
        n = node_map[qn]
        if n.kind == "File":
            continue
        results.append({
            "name": _sanitize_name(n.name),
            "qualified_name": n.qualified_name,
            "kind": n.kind,
            "file": n.file_path,
            "betweenness": round(score, 6),
            "community_id": community_map.get(qn),
        })

    results.sort(
        key=lambda x: float(x.get("betweenness", 0)),  # type: ignore[arg-type,return-value]
        reverse=True,
    )
    del nxg, bc
    store.shrink_memory(clear_nx_cache=True)
    return results[:top_n]


def _shortest_loop(adjacency: dict[str, set[str]], start: str) -> list[str]:
    """Return the fewest files that lead from *start* back to itself."""
    came_from: dict[str, str] = {}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for neighbour in sorted(adjacency.get(current, ())):
            if neighbour == start:
                loop = [current]
                while loop[-1] != start:
                    loop.append(came_from[loop[-1]])
                return loop[::-1]
            if neighbour not in came_from:
                came_from[neighbour] = current
                queue.append(neighbour)
    return [start]


def find_import_cycles(
    store: GraphStore,
    edges: list[GraphEdge] | None = None,
    repo_root: Path | None = None,
) -> list[dict]:
    """Find groups of files that import each other, worst first.

    Only imports that run count: one under ``if TYPE_CHECKING`` or written
    ``import type`` never does. Only imports that name a file count too, so Go
    packages, which the compiler already keeps free of cycles, and C# namespaces
    are not followed.

    Returns list of dicts with: name (one loop, written out), size (files in the
    group), load_time (the loop can fail as the files load; False means every
    loop has an import that runs late), files, loop_length, and cycle (that
    loop's imports with their lines). Paths are relative to *repo_root* when it
    is given. Long lists are cut: size and loop_length keep the real counts.
    """
    import networkx as nx

    files = set(store.get_all_files())
    all_imports: dict[tuple[str, str], int] = {}
    load_imports: dict[tuple[str, str], int] = {}
    for edge in store.get_all_edges() if edges is None else edges:
        source, target = edge.source_qualified, edge.target_qualified
        if (
            edge.kind != "IMPORTS_FROM"
            or source == target
            or source not in files
            or target not in files
        ):
            continue
        context = edge.extra.get(IMPORT_CONTEXT_KEY)
        if context in (IMPORT_CONTEXT_TYPE_ONLY, IMPORT_CONTEXT_PARENT_PACKAGE):
            continue
        pair = (source, target)
        all_imports[pair] = min(all_imports.get(pair, edge.line), edge.line)
        if context != IMPORT_CONTEXT_DEFERRED:
            load_imports[pair] = min(load_imports.get(pair, edge.line), edge.line)

    groups = [
        group
        for group in nx.strongly_connected_components(nx.DiGraph(list(all_imports)))
        if len(group) > 1
    ]
    if not groups:
        return []
    load_groups = sorted(
        (
            group
            for group in nx.strongly_connected_components(nx.DiGraph(list(load_imports)))
            if len(group) > 1
        ),
        key=lambda group: (-len(group), min(group)),
    )

    prefix = f"{normalize_file_path(repo_root)}/" if repo_root else ""

    def short(path: str) -> str:
        """Drop the repository's own folder: it would repeat in every row."""
        return _sanitize_name(path.removeprefix(prefix))

    def adjacency(imports: dict[tuple[str, str], int]) -> dict[str, set[str]]:
        result: dict[str, set[str]] = defaultdict(set)
        for source, target in imports:
            result[source].add(target)
        return result

    all_adjacency = adjacency(all_imports)
    load_adjacency = adjacency(load_imports)

    cycles: list[dict] = []
    for group in groups:
        # A loop that runs at load is the one that can fail, so show it first.
        load_group = next((g for g in load_groups if g <= group), None)
        if load_group is not None:
            loop = _shortest_loop(load_adjacency, min(load_group))
        else:
            loop = _shortest_loop(all_adjacency, min(group))
        hops = list(zip(loop, loop[1:] + loop[:1]))
        cycles.append({
            "name": _sanitize_name(" -> ".join(short(path) for path in [*loop, loop[0]])),
            "size": len(group),
            "load_time": load_group is not None,
            "files": [short(path) for path in sorted(group)[:_MAX_CYCLE_FILES]],
            "loop_length": len(loop),
            "cycle": [
                {
                    "file": short(source),
                    "line": load_imports.get((source, target), all_imports[(source, target)]),
                    "imports": short(target),
                }
                for source, target in hops[:_MAX_CYCLE_HOPS]
            ],
        })
    cycles.sort(key=lambda c: (not c["load_time"], -c["size"], c["name"]))
    return cycles


def find_knowledge_gaps(
    store: GraphStore, repo_root: Path | None = None,
) -> dict[str, list[dict]]:
    """Identify structural weaknesses in the codebase graph.

    Returns dict with categories:
    - isolated_nodes: degree <= 1, disconnected from graph
    - thin_communities: fewer than 3 members
    - untested_hotspots: high-degree nodes with no TESTED_BY edges
    - single_file_communities: entire community in one file
    - import_cycles: files that import each other (see find_import_cycles)
    """
    edges = store.get_all_edges()
    nodes = store.get_all_nodes(exclude_files=True)
    community_map = store.get_all_community_ids()

    # Build degree map
    degree: dict[str, int] = Counter()
    tested_nodes: set[str] = set()
    for e in edges:
        degree[e.source_qualified] += 1
        degree[e.target_qualified] += 1
        if e.kind == "TESTED_BY":
            tested_nodes.add(e.source_qualified)

    # 1. Isolated nodes (degree <= 1, not File)
    isolated = []
    for n in nodes:
        d = degree.get(n.qualified_name, 0)
        if d <= 1:
            isolated.append({
                "name": _sanitize_name(n.name),
                "qualified_name": n.qualified_name,
                "kind": n.kind,
                "file": n.file_path,
                "degree": d,
            })

    # 2. Build community sizes and file maps from node data
    comm_sizes: Counter[int] = Counter()
    comm_files: dict[int, set[str]] = defaultdict(set)
    for n in nodes:
        cid = community_map.get(n.qualified_name)
        if cid is not None:
            comm_sizes[cid] += 1
            comm_files[cid].add(n.file_path)

    # Thin communities (< 3 members)
    communities = store.get_communities_list()
    thin = []
    for c in communities:
        cid = int(c["id"])
        size = comm_sizes.get(cid, 0)
        if size < 3:
            thin.append({
                "community_id": cid,
                "name": str(c["name"]),
                "size": size,
            })

    # 3. Untested hotspots (degree >= 5, no TESTED_BY)
    untested_hotspots = []
    for n in nodes:
        d = degree.get(n.qualified_name, 0)
        if (d >= 5
                and n.qualified_name not in tested_nodes
                and not n.is_test):
            untested_hotspots.append({
                "name": _sanitize_name(n.name),
                "qualified_name": n.qualified_name,
                "kind": n.kind,
                "file": n.file_path,
                "degree": d,
            })
    untested_hotspots.sort(
        key=lambda x: x.get("degree", 0),  # type: ignore[arg-type,return-value]
        reverse=True,
    )

    # 4. Single-file communities
    single_file = []
    for c in communities:
        cid = int(c["id"])
        files = comm_files.get(cid, set())
        size = comm_sizes.get(cid, 0)
        if len(files) == 1 and size >= 3:
            single_file.append({
                "community_id": cid,
                "name": str(c["name"]),
                "size": size,
                "file": next(iter(files)),
            })

    return {
        "isolated_nodes": isolated[:50],
        "thin_communities": thin,
        "untested_hotspots": untested_hotspots[:20],
        "single_file_communities": single_file,
        "import_cycles": find_import_cycles(store, edges, repo_root),
    }


def find_surprising_connections(
    store: GraphStore, top_n: int = 15
) -> list[dict]:
    """Find edges with high surprise scores.

    Detects unexpected architectural coupling based on:
    - Cross-community: source and target in different communities
    - Cross-language: different file languages
    - Peripheral-to-hub: low-degree node to high-degree node
    - Cross-file-type: test calling production or vice versa
    - Non-standard edge kind for the node types
    """
    edges = store.get_all_edges()
    nodes = store.get_all_nodes(exclude_files=True)
    community_map = store.get_all_community_ids()

    node_map = {n.qualified_name: n for n in nodes}

    # Build degree map
    degree: dict[str, int] = Counter()
    for e in edges:
        degree[e.source_qualified] += 1
        degree[e.target_qualified] += 1

    # Median degree for peripheral detection
    degrees = [d for d in degree.values() if d > 0]
    if not degrees:
        return []
    median_deg = sorted(degrees)[len(degrees) // 2]
    high_deg_threshold = max(median_deg * 3, 10)

    scored_edges = []
    for e in edges:
        src = node_map.get(e.source_qualified)
        tgt = node_map.get(e.target_qualified)
        if not src or not tgt:
            continue
        if src.kind == "File" or tgt.kind == "File":
            continue

        score = 0.0
        reasons = []

        # Cross-community (+0.3)
        src_cid = community_map.get(e.source_qualified)
        tgt_cid = community_map.get(e.target_qualified)
        if (src_cid is not None
                and tgt_cid is not None
                and src_cid != tgt_cid):
            score += 0.3
            reasons.append("cross-community")

        # Cross-language (+0.2)
        src_lang = (
            src.file_path.rsplit(".", 1)[-1]
            if "." in src.file_path else ""
        )
        tgt_lang = (
            tgt.file_path.rsplit(".", 1)[-1]
            if "." in tgt.file_path else ""
        )
        if src_lang and tgt_lang and src_lang != tgt_lang:
            score += 0.2
            reasons.append("cross-language")

        # Peripheral-to-hub (+0.2)
        src_deg = degree.get(e.source_qualified, 0)
        tgt_deg = degree.get(e.target_qualified, 0)
        if ((src_deg <= 2 and tgt_deg >= high_deg_threshold)
                or (tgt_deg <= 2
                    and src_deg >= high_deg_threshold)):
            score += 0.2
            reasons.append("peripheral-to-hub")

        # Cross-file-type: test <-> non-test (+0.15)
        if src.is_test != tgt.is_test and e.kind == "CALLS":
            score += 0.15
            reasons.append("cross-test-boundary")

        # Non-standard edge kind (+0.15)
        if e.kind == "CALLS" and src.kind == "Type":
            score += 0.15
            reasons.append("unusual-edge-kind")

        if score > 0:
            scored_edges.append({
                "source": _sanitize_name(src.name),
                "source_qualified": e.source_qualified,
                "target": _sanitize_name(tgt.name),
                "target_qualified": e.target_qualified,
                "edge_kind": e.kind,
                "surprise_score": round(score, 2),
                "reasons": reasons,
                "source_community": src_cid,
                "target_community": tgt_cid,
            })

    scored_edges.sort(
        key=lambda x: float(x.get("surprise_score", 0)),  # type: ignore[arg-type,return-value]
        reverse=True,
    )
    return scored_edges[:top_n]


def generate_suggested_questions(
    store: GraphStore,
) -> list[dict]:
    """Auto-generate review questions from graph analysis.

    Categories:
    - bridge_node: Why does X connect communities A and B?
    - isolated_node: Is X dead code or dynamically invoked?
    - low_cohesion: Should community X be split?
    - hub_risk: Does hub node X have adequate test coverage?
    - surprising: Why does A call B across community boundary?
    """
    questions = []

    # Bridge node questions
    bridges = find_bridge_nodes(store, top_n=3)
    for b in bridges:
        questions.append({
            "category": "bridge_node",
            "question": (
                f"'{b['name']}' is a critical connector "
                f"between multiple code regions. Is it "
                f"adequately tested and documented?"
            ),
            "target": b["qualified_name"],
            "priority": "high",
        })

    # Hub risk questions
    hubs = find_hub_nodes(store, top_n=3)
    edges = store.get_all_edges()
    tested = {
        e.source_qualified
        for e in edges if e.kind == "TESTED_BY"
    }
    for h in hubs:
        if h["qualified_name"] not in tested:
            questions.append({
                "category": "hub_risk",
                "question": (
                    f"Hub node '{h['name']}' has "
                    f"{h['total_degree']} connections but no "
                    f"direct test coverage. Should it be "
                    f"tested?"
                ),
                "target": h["qualified_name"],
                "priority": "high",
            })

    # Surprising connection questions
    surprises = find_surprising_connections(store, top_n=3)
    for s in surprises:
        if "cross-community" in s["reasons"]:
            questions.append({
                "category": "surprising_connection",
                "question": (
                    f"'{s['source']}' (community "
                    f"{s['source_community']}) calls "
                    f"'{s['target']}' (community "
                    f"{s['target_community']}). Is this "
                    f"coupling intentional?"
                ),
                "target": s["source_qualified"],
                "priority": "medium",
            })

    # Knowledge gap questions
    gaps = find_knowledge_gaps(store)

    for c in gaps["thin_communities"][:2]:
        questions.append({
            "category": "thin_community",
            "question": (
                f"Community '{c['name']}' has only "
                f"{c['size']} member(s). Should it be "
                f"merged with a neighbor?"
            ),
            "target": f"community:{c['community_id']}",
            "priority": "low",
        })

    for h in gaps["untested_hotspots"][:2]:
        questions.append({
            "category": "untested_hotspot",
            "question": (
                f"'{h['name']}' has {h['degree']} "
                f"connections but no test coverage. "
                f"Is this a risk?"
            ),
            "target": h["qualified_name"],
            "priority": "medium",
        })

    return questions
