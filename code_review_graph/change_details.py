"""Bounded graph context for a change: the ``details`` of ``detect-changes``.

``detect-changes --details risk,hubs,...`` adds one ``details`` object to its
JSON report. The GitHub Action's renderer turns it into extra sections of the
review comment when the workflow opts in (issue #882). Every list here is cut
to :data:`MAX_ROWS`; the renderer cuts again and caps the bytes it writes.

Sections:

``risk``
    Each top symbol's risk score split into the terms that make it up
    (:func:`~code_review_graph.changes.risk_signals`), so a reader sees why a
    score moved, not only that it did.
``communities``
    The communities the changed symbols belong to.
``hubs``
    Changed production symbols ranked by connections, with their rank among
    all production symbols and how many other communities their direct
    callers and callees sit in.
``coupling``
    Links (one per source, target and edge kind) from or to a changed symbol
    that cross a community or a language boundary. Test code is left out.
``impact``
    The impact radius of the changed files, and the direct callers of the top
    symbols.
``gaps``
    The test gaps, ranked by how much depends on them.
``graph``
    Where the graph came from: size, languages, community count and the
    algorithm that found them, flow count, last build, versions.

All of it is read from the local graph; nothing leaves the machine.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from collections.abc import Sequence
from typing import Any

from . import communities
from .changes import risk_signals
from .constants import MAX_IMPACT_DEPTH
from .graph import GraphEdge, GraphNode, GraphStore, _sanitize_name

#: Section names, in the order they are built and rendered. The Action's
#: renderer (``scripts/render_pr_comment.py``) keeps its own copy because it
#: runs on the standard library alone; a test holds the two equal.
DETAIL_SECTIONS: tuple[str, ...] = (
    "risk", "communities", "hubs", "coupling", "impact", "gaps", "graph",
)

#: Longest list any section returns.
MAX_ROWS = 10
#: Callers named per symbol in the ``impact`` section.
MAX_CALLERS = 5

# Containment is structure, and a test reaching the code it tests is the
# point of the test: neither is coupling.
_NOT_COUPLING = frozenset({"CONTAINS", "TESTED_BY"})

# A .tsx component calling a .ts service is not crossing a language boundary
# in any sense a reviewer cares about, nor is C++ including a C header.
_LANGUAGE_FAMILY = {
    "typescript": "javascript",
    "tsx": "javascript",
    "vue": "javascript",
    "svelte": "javascript",
    "cpp": "c",
    "objc": "c",
    "luau": "lua",
    "notebook": "python",
}


def parse_detail_sections(value: str) -> tuple[str, ...]:
    """Parse a comma-separated section list; ``all`` means every section.

    Order and repeats in *value* do not matter: the result follows
    :data:`DETAIL_SECTIONS`. An empty value is no sections. An unknown name
    raises :class:`ValueError` naming it, so a typo in a workflow fails
    loudly instead of quietly dropping a section.
    """
    names = [part.strip().lower() for part in str(value).split(",") if part.strip()]
    unknown = [name for name in names if name != "all" and name not in DETAIL_SECTIONS]
    if unknown:
        raise ValueError(
            f"unknown detail section(s): {', '.join(unknown)}; "
            f"choose from {', '.join(DETAIL_SECTIONS)}, or all"
        )
    if "all" in names:
        return DETAIL_SECTIONS
    return tuple(name for name in DETAIL_SECTIONS if name in names)


def build_change_details(
    store: GraphStore,
    report: dict[str, Any],
    sections: Sequence[str],
    *,
    changed_files: list[str],
    churn_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build the ``details`` object for *report*, an ``analyze_changes`` result.

    *changed_files* are the changed paths as the graph stores them (absolute
    and normalized), for the impact radius. *churn_counts* must be what the
    report was scored with, so the ``risk`` signals add up to its scores.
    """
    changed = _changed_nodes(store, report)
    by_id = {node.id: node for node in changed}
    community_of = store.get_community_ids_by_qualified_names(
        [node.qualified_name for node in changed]
    )
    details: dict[str, Any] = {"sections": list(sections)}
    for name in sections:
        if name == "risk":
            details[name] = _risk(store, report, by_id, churn_counts)
        elif name == "communities":
            details[name] = _communities(store, changed, community_of)
        elif name == "hubs":
            details[name] = _hubs(store, changed, community_of)
        elif name == "coupling":
            details[name] = _coupling(store, changed)
        elif name == "impact":
            details[name] = _impact(store, report, by_id, changed_files)
        elif name == "gaps":
            details[name] = _gaps(store, report, changed)
        elif name == "graph":
            details[name] = _graph(store)
        else:
            raise ValueError(f"unknown detail section: {name}")
    return details


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _changed_nodes(store: GraphStore, report: dict[str, Any]) -> list[GraphNode]:
    """The changed functions and classes of *report*, as graph nodes."""
    nodes: list[GraphNode] = []
    seen: set[int] = set()
    for entry in report.get("changed_functions") or []:
        node_id = entry.get("id")
        if not isinstance(node_id, int) or node_id in seen:
            continue
        seen.add(node_id)
        node = store.get_node_by_id(node_id)
        if node is not None:
            nodes.append(node)
    return nodes


def _where(node: GraphNode) -> dict[str, Any]:
    return {
        "qualified_name": _sanitize_name(node.qualified_name),
        "file_path": node.file_path,
        "line_start": node.line_start,
    }


def _risk(
    store: GraphStore,
    report: dict[str, Any],
    by_id: dict[int, GraphNode],
    churn_counts: dict[str, int] | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in (report.get("review_priorities") or [])[:MAX_ROWS]:
        node = by_id.get(entry.get("id"))  # type: ignore[arg-type]
        if node is None:
            continue
        signals = risk_signals(store, node, churn_counts)
        rows.append({
            **_where(node),
            "risk_score": entry.get("risk_score"),
            "signals": {
                key: {"score": round(float(value["score"]), 4), "count": int(value["count"])}
                for key, value in signals.items()
            },
        })
    return rows


def _communities(
    store: GraphStore,
    changed: list[GraphNode],
    community_of: dict[str, int | None],
) -> dict[str, Any]:
    counts: Counter[int] = Counter()
    unassigned = 0
    for node in changed:
        cid = community_of.get(node.qualified_name)
        if cid is None:
            unassigned += 1
        else:
            counts[cid] += 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:MAX_ROWS]
    rows = store.get_communities_by_ids(cid for cid, _ in ordered)
    touched = []
    for cid, n_changed in ordered:
        row = rows.get(cid)
        touched.append({
            "id": cid,
            "name": _sanitize_name(str(row["name"])) if row is not None else "?",
            "size": int(row["size"] or 0) if row is not None else 0,
            "changed": n_changed,
        })
    return {
        "in_graph": len(store.get_communities_list()),
        "touched_total": len(counts),
        "unassigned": unassigned,
        "touched": touched,
    }


def _hubs(
    store: GraphStore,
    changed: list[GraphNode],
    community_of: dict[str, int | None],
) -> dict[str, Any]:
    # Production code only, on both sides of the rank: a changed test that
    # calls fifty helpers is busy, not a hub anything depends on.
    degrees = store.get_node_degrees(include_tests=False)
    totals = sorted(i + o for i, o in degrees.values() if i + o > 0)
    candidates: list[tuple[int, str, GraphNode]] = []
    for node in changed:
        if _is_test(node):
            continue
        inward, outward = degrees.get(node.qualified_name, (0, 0))
        if inward + outward > 0:
            candidates.append((inward + outward, node.qualified_name, node))
    # Most connected first; equal degrees fall back to the name, so the order
    # is the same on every run.
    candidates.sort(key=lambda item: (-item[0], item[1]))
    rows = []
    for degree, _, node in candidates[:MAX_ROWS]:
        rows.append({
            **_where(node),
            "degree": degree,
            # 1 + the number of symbols with strictly more connections.
            "rank": len(totals) - bisect_right(totals, degree) + 1,
            "communities_linked": _communities_linked(store, node, community_of),
        })
    return {"ranked": len(totals), "total": len(candidates), "nodes": rows}


def _communities_linked(
    store: GraphStore, node: GraphNode, community_of: dict[str, int | None],
) -> int:
    """Other communities among the direct callers and callees of *node*."""
    neighbours = [
        edge.source_qualified
        for edge in store.get_edges_by_target(node.qualified_name)
        if edge.kind == "CALLS"
    ] + [
        edge.target_qualified
        for edge in store.get_edges_by_source(node.qualified_name)
        if edge.kind == "CALLS"
    ]
    if not neighbours:
        return 0
    own = community_of.get(node.qualified_name)
    found = store.get_community_ids_by_qualified_names(neighbours)
    return len({cid for cid in found.values() if cid is not None and cid != own})


def _language_family(language: str) -> str:
    return _LANGUAGE_FAMILY.get(language, language)


def _is_test(node: GraphNode) -> bool:
    return bool(node.is_test) or node.kind == "Test"


def _coupling(store: GraphStore, changed: list[GraphNode]) -> dict[str, Any]:
    edges: dict[int, GraphEdge] = {}
    for node in changed:
        for edge in store.get_edges_by_source(node.qualified_name):
            if edge.kind not in _NOT_COUPLING:
                edges[edge.id] = edge
        for edge in store.get_edges_by_target(node.qualified_name):
            if edge.kind not in _NOT_COUPLING:
                edges[edge.id] = edge
    endpoints = {e.source_qualified for e in edges.values()} | {
        e.target_qualified for e in edges.values()
    }
    nodes = {n.qualified_name: n for n in store.get_nodes_by_qualified_names(endpoints)}
    community_of = store.get_community_ids_by_qualified_names(sorted(endpoints))

    cross_community = 0
    cross_language = 0
    found: list[tuple[GraphEdge, GraphNode, GraphNode, list[str]]] = []
    # Two references on two lines are one link between the same two symbols.
    seen: set[tuple[str, str, str]] = set()
    for edge in sorted(edges.values(), key=lambda e: e.id):
        src = nodes.get(edge.source_qualified)
        tgt = nodes.get(edge.target_qualified)
        if src is None or tgt is None or "File" in (src.kind, tgt.kind):
            continue
        if _is_test(src) or _is_test(tgt):
            continue
        link = (src.qualified_name, tgt.qualified_name, edge.kind)
        if link in seen:
            continue
        seen.add(link)
        reasons = []
        if (src.language and tgt.language
                and _language_family(src.language) != _language_family(tgt.language)):
            reasons.append("cross-language")
            cross_language += 1
        src_cid = community_of.get(src.qualified_name)
        tgt_cid = community_of.get(tgt.qualified_name)
        if src_cid is not None and tgt_cid is not None and src_cid != tgt_cid:
            reasons.append("cross-community")
            cross_community += 1
        if reasons:
            found.append((edge, src, tgt, reasons))

    # Edges crossing both boundaries first, then a language boundary alone,
    # then by name so the order is stable.
    found.sort(key=lambda item: (
        -len(item[3]), item[3][0] != "cross-language",
        item[1].qualified_name, item[2].qualified_name, item[0].kind,
    ))
    kept = found[:MAX_ROWS]
    names = store.get_communities_by_ids(
        cid
        for _, src, tgt, _ in kept
        for cid in (community_of.get(src.qualified_name), community_of.get(tgt.qualified_name))
        if cid is not None
    )

    def community_name(qualified_name: str) -> str | None:
        cid = community_of.get(qualified_name)
        row = names.get(cid) if cid is not None else None
        return _sanitize_name(str(row["name"])) if row is not None else None

    return {
        "total": len(found),
        "cross_community": cross_community,
        "cross_language": cross_language,
        "edges": [
            {
                "source": _sanitize_name(src.qualified_name),
                "target": _sanitize_name(tgt.qualified_name),
                "kind": edge.kind,
                "reasons": reasons,
                "source_language": src.language,
                "target_language": tgt.language,
                "source_community": community_name(src.qualified_name),
                "target_community": community_name(tgt.qualified_name),
            }
            for edge, src, tgt, reasons in kept
        ],
    }


def _callers_of(store: GraphStore, qualified_name: str) -> list[str]:
    return sorted({
        edge.source_qualified
        for edge in store.get_edges_by_target(qualified_name)
        if edge.kind == "CALLS"
    })


def _impact(
    store: GraphStore,
    report: dict[str, Any],
    by_id: dict[int, GraphNode],
    changed_files: list[str],
) -> dict[str, Any]:
    radius = store.get_impact_radius(changed_files, max_depth=MAX_IMPACT_DEPTH)
    impacted = radius.get("impacted_nodes") or []
    per_file = Counter(node.file_path for node in impacted)
    top_files = sorted(per_file.items(), key=lambda item: (-item[1], item[0]))[:MAX_ROWS]
    callers = []
    for entry in (report.get("review_priorities") or [])[:MAX_ROWS]:
        node = by_id.get(entry.get("id"))  # type: ignore[arg-type]
        if node is None:
            continue
        sources = _callers_of(store, node.qualified_name)
        if not sources:
            continue
        callers.append({
            **_where(node),
            "total": len(sources),
            "callers": [_sanitize_name(qn) for qn in sources[:MAX_CALLERS]],
        })
    return {
        "depth": MAX_IMPACT_DEPTH,
        "impacted_nodes": int(radius.get("total_impacted") or len(impacted)),
        "impacted_files": len(per_file),
        "truncated": bool(radius.get("truncated")),
        "top_files": [{"file_path": path, "nodes": n} for path, n in top_files],
        "callers": callers,
    }


def _gaps(
    store: GraphStore, report: dict[str, Any], changed: list[GraphNode],
) -> dict[str, Any]:
    gaps = [g for g in report.get("test_gaps") or [] if isinstance(g, dict)]
    # A gap's name in the report is cut to 256 characters, so a lookup by it
    # misses a longer name. Every gap is a changed symbol: match it to the
    # changed node by the cut name, file and line, and query by the real name.
    own = {
        (_sanitize_name(n.qualified_name), n.file_path, n.line_start): n for n in changed
    }
    by_name = {
        n.qualified_name: n
        for n in store.get_nodes_by_qualified_names(
            str(g.get("qualified_name") or "") for g in gaps
        )
    }
    rows = []
    for gap in gaps:
        shown = str(gap.get("qualified_name") or "")
        node = own.get((shown, gap.get("file"), gap.get("line_start"))) or by_name.get(shown)
        qualified_name = node.qualified_name if node is not None else shown
        rows.append({
            "qualified_name": _sanitize_name(qualified_name),
            "file_path": gap.get("file"),
            "line_start": gap.get("line_start"),
            "callers": len(_callers_of(store, qualified_name)),
            "flows": store.count_flow_memberships(node.id) if node is not None else 0,
            "coverage": gap.get("coverage") or "none",
        })
    rows.sort(key=lambda row: (-row["callers"], -row["flows"], row["qualified_name"]))
    total = report.get("test_gaps_total")
    return {
        "total": total if isinstance(total, int) else len(gaps),
        "hotspots": rows[:MAX_ROWS],
    }


def _graph(store: GraphStore) -> dict[str, Any]:
    from . import __version__

    stats = store.get_stats()
    schema = store.get_metadata("schema_version")
    return {
        "nodes": stats.total_nodes,
        "edges": stats.total_edges,
        "files": stats.files_count,
        "languages": list(stats.languages)[:MAX_ROWS * 2],
        "communities": len(store.get_communities_list()),
        "community_detector": store.get_metadata(communities.DETECTOR_METADATA_KEY),
        "igraph_installed": communities.IGRAPH_AVAILABLE,
        "flows": store.count_flows(),
        "last_updated": store.get_metadata("last_updated"),
        "build_type": store.get_metadata("last_build_type"),
        "version": __version__,
        "schema_version": int(schema) if schema and schema.isdigit() else None,
    }
