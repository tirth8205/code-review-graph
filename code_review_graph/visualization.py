"""Interactive D3.js graph visualization for code knowledge graphs.

Exports graph data to JSON and generates a self-contained HTML file with
a force-directed D3.js visualization. Dark theme, zoomable, draggable,
with collapsible file clusters, tooltips, legend, and stats bar.

Supports multiple rendering modes for large graphs:
- ``full``  — render every node (default, current behavior)
- ``community`` — aggregate by community; double-click to drill down
- ``file``  — aggregate by file; each file is a node
- ``auto``  — choose community mode when node count exceeds threshold
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

from .graph import GraphStore, edge_to_dict, node_to_dict

logger = logging.getLogger(__name__)


def _build_name_index(
    nodes: list[dict], seen_qn: set[str]
) -> dict[str, list[str]]:
    """Build a mapping from short/module-style names to qualified names.

    Returns ``{short_name: [qualified_name, ...]}``.
    """
    index: dict[str, list[str]] = {}

    def _add(key: str, qn: str) -> None:
        index.setdefault(key, []).append(qn)

    for n in nodes:
        qn = n["qualified_name"]
        _add(n["name"], qn)
        # Index by "file::name" suffix (e.g. "cli.py::main")
        if "::" in qn:
            _add(qn.rsplit("/", 1)[-1], qn)
        # Index by module-style path (e.g. "merit.cli" or "merit.cli.main")
        fp = n.get("file_path", "")
        if fp:
            mod = fp.replace("/", ".").replace(".py", "")
            if n["kind"] == "File":
                _add(mod, qn)
                # Index by every path suffix so C/C++ bare includes resolve.
                # e.g. "/abs/libs/trading/Foo.hpp" is also indexed as
                # "Foo.hpp", "trading/Foo.hpp", "libs/trading/Foo.hpp", …
                parts = fp.replace("\\", "/").split("/")
                for i in range(len(parts)):
                    suffix = "/".join(parts[i:])
                    if suffix:
                        _add(suffix, qn)
            else:
                _add(mod + "." + n["name"], qn)
    return index


def _resolve_target(
    target: str,
    source: str,
    seen_qn: set[str],
    name_index: dict[str, list[str]],
) -> str | None:
    """Try to resolve an unqualified edge target to a full qualified name.

    Returns the resolved qualified name, or None if unresolvable.
    """
    # Already fully qualified
    if target in seen_qn:
        return target

    candidates = name_index.get(target)
    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0]

    # Disambiguate: prefer node in the same file as the source
    src_file = source.split("::")[0] if "::" in source else source
    same_file = [c for c in candidates if c.startswith(src_file)]
    if len(same_file) == 1:
        return same_file[0]

    # Prefer node in the same top-level directory
    src_parts = src_file.rsplit("/", 1)[0] if "/" in src_file else ""
    same_dir = [c for c in candidates if c.startswith(src_parts)]
    if len(same_dir) == 1:
        return same_dir[0]

    # Ambiguous — pick first match rather than dropping the edge
    return candidates[0]


def export_graph_data(store: GraphStore) -> dict:
    """Export all graph nodes and edges as a JSON-serializable dict.

    Returns ``{"nodes": [...], "edges": [...], "stats": {...},
    "flows": [...], "communities": [...]}``.
    """
    nodes = []
    seen_qn: set[str] = set()

    # Preload community_id mapping from DB (column may not exist in old schemas)
    community_map = store.get_all_community_ids()

    for file_path in store.get_all_files():
        for gnode in store.get_nodes_by_file(file_path):
            if gnode.qualified_name in seen_qn:
                continue
            seen_qn.add(gnode.qualified_name)
            d = node_to_dict(gnode)
            d["params"] = gnode.params
            d["return_type"] = gnode.return_type
            d["community_id"] = community_map.get(gnode.qualified_name)
            nodes.append(d)

    name_index = _build_name_index(nodes, seen_qn)

    all_edges = [edge_to_dict(e) for e in store.get_all_edges()]

    # Resolve short/unqualified edge targets to full qualified names,
    # then drop edges that still can't be resolved (external/stdlib calls).
    edges = []
    for e in all_edges:
        src = _resolve_target(e["source"], e["source"], seen_qn, name_index)
        tgt = _resolve_target(e["target"], e["source"], seen_qn, name_index)
        if src and tgt:
            e["source"] = src
            e["target"] = tgt
            edges.append(e)

    stats = store.get_stats()

    # Include flows (graceful fallback if table doesn't exist)
    try:
        from code_review_graph.flows import get_flows
        flows = get_flows(store, limit=100)
    except (ImportError, sqlite3.OperationalError) as exc:
        logger.debug("flows unavailable for export: %s", exc)
        flows = []

    # Include communities (graceful fallback if table doesn't exist)
    try:
        from code_review_graph.communities import get_communities
        communities = get_communities(store)
    except (ImportError, sqlite3.OperationalError) as exc:
        logger.debug("communities unavailable for export: %s", exc)
        communities = []

    return {
        "nodes": nodes,
        "edges": edges,
        "stats": asdict(stats),
        "flows": flows,
        "communities": communities,
    }


def _aggregate_community(data: dict) -> dict:
    """Aggregate full graph data into community-level super-nodes.

    Each community becomes a single node sized by member count.
    Edges between super-nodes represent the count of cross-community edges.
    Returns a new dict with the same schema as *data* but fewer nodes/edges.
    Also returns per-community detail data for drill-down rendering.
    """
    communities = data.get("communities") or []
    nodes = data["nodes"]
    edges = data["edges"]

    # Build mapping: qualified_name -> community_id
    qn_to_cid: dict[str, int] = {}
    for c in communities:
        for qn in c.get("members", []):
            qn_to_cid[qn] = c["id"]

    # Also use node-level community_id for nodes not in community member lists
    for n in nodes:
        if n.get("community_id") is not None and n["qualified_name"] not in qn_to_cid:
            qn_to_cid[n["qualified_name"]] = n["community_id"]

    # Assign uncategorized nodes to a synthetic community id = -1
    uncategorized_members: list[str] = []
    for n in nodes:
        if n["qualified_name"] not in qn_to_cid:
            qn_to_cid[n["qualified_name"]] = -1
            uncategorized_members.append(n["qualified_name"])

    # Build community info map (including the synthetic uncategorized one)
    cid_info: dict[int, dict] = {}
    for c in communities:
        cid_info[c["id"]] = c
    if uncategorized_members:
        cid_info[-1] = {
            "id": -1,
            "name": "Uncategorized",
            "size": len(uncategorized_members),
            "members": uncategorized_members,
            "dominant_language": "",
            "description": "Nodes not assigned to any community",
            "cohesion": 0,
            "level": 0,
        }

    # Build super-nodes (one per community)
    super_nodes = []
    for cid, info in cid_info.items():
        size = info.get("size", len(info.get("members", [])))
        if size == 0:
            continue
        super_nodes.append({
            "qualified_name": f"__community__{cid}",
            "name": info.get("name", f"Community {cid}"),
            "kind": "Community",
            "file_path": "",
            "line_start": None,
            "line_end": None,
            "language": info.get("dominant_language", ""),
            "community_id": cid,
            "member_count": size,
            "description": info.get("description", ""),
            "id": cid,
        })

    # Build super-edges: aggregate cross-community edges
    cross_edge_counts: Counter[tuple[int, int]] = Counter()
    for e in edges:
        src_cid = qn_to_cid.get(e["source"])
        tgt_cid = qn_to_cid.get(e["target"])
        if src_cid is not None and tgt_cid is not None and src_cid != tgt_cid:
            pair = (min(src_cid, tgt_cid), max(src_cid, tgt_cid))
            cross_edge_counts[pair] += 1

    super_edges = []
    for (c1, c2), count in cross_edge_counts.items():
        super_edges.append({
            "source": f"__community__{c1}",
            "target": f"__community__{c2}",
            "kind": "CROSS_COMMUNITY",
            "weight": count,
        })

    # Build per-community detail data for drill-down
    community_details: dict[int, dict] = {}
    cid_members_set: dict[int, set[str]] = defaultdict(set)
    for qn, cid in qn_to_cid.items():
        cid_members_set[cid].add(qn)

    for cid, member_qns in cid_members_set.items():
        detail_nodes = [n for n in nodes if n["qualified_name"] in member_qns]
        detail_edges = [
            e for e in edges
            if e["source"] in member_qns and e["target"] in member_qns
        ]
        community_details[cid] = {
            "nodes": detail_nodes,
            "edges": detail_edges,
        }

    return {
        "nodes": super_nodes,
        "edges": super_edges,
        "stats": data["stats"],
        "flows": data.get("flows", []),
        "communities": communities,
        "mode": "community",
        "community_details": {
            str(k): v for k, v in community_details.items()
        },
    }


def _aggregate_file(data: dict) -> dict:
    """Aggregate full graph data into file-level nodes.

    Each file becomes a node sized by symbol count.
    Edges between files represent aggregated cross-file dependencies.
    """
    nodes = data["nodes"]
    edges = data["edges"]

    # Count symbols per file
    file_symbol_count: Counter[str] = Counter()
    qn_to_file: dict[str, str] = {}
    file_languages: dict[str, str] = {}

    for n in nodes:
        fp = n.get("file_path", "")
        if not fp:
            continue
        qn_to_file[n["qualified_name"]] = fp
        if n["kind"] != "File":
            file_symbol_count[fp] += 1
        else:
            file_symbol_count.setdefault(fp, 0)
        if n.get("language"):
            file_languages[fp] = n["language"]

    # Build file nodes
    file_nodes = []
    for fp, count in file_symbol_count.items():
        parts = fp.replace("\\", "/").split("/")
        short = parts[-1] if parts else fp
        parent = parts[-2] if len(parts) >= 2 else ""
        label = f"{parent}/{short}" if parent else short
        # Recover community_id from the majority of symbols in this file
        cid = None
        for n in nodes:
            if n.get("file_path") == fp and n.get("community_id") is not None:
                cid = n["community_id"]
                break
        file_nodes.append({
            "qualified_name": fp,
            "name": label,
            "kind": "File",
            "file_path": fp,
            "line_start": None,
            "line_end": None,
            "language": file_languages.get(fp, ""),
            "community_id": cid,
            "symbol_count": count,
        })

    # Aggregate cross-file edges
    cross_file_counts: Counter[tuple[str, str]] = Counter()
    for e in edges:
        src_fp = qn_to_file.get(e["source"])
        tgt_fp = qn_to_file.get(e["target"])
        if src_fp and tgt_fp and src_fp != tgt_fp:
            pair = (src_fp, tgt_fp)
            cross_file_counts[pair] += 1

    file_edges = []
    for (f1, f2), count in cross_file_counts.items():
        file_edges.append({
            "source": f1,
            "target": f2,
            "kind": "DEPENDS_ON",
            "weight": count,
        })

    return {
        "nodes": file_nodes,
        "edges": file_edges,
        "stats": data["stats"],
        "flows": data.get("flows", []),
        "communities": data.get("communities", []),
        "mode": "file",
    }


def generate_html(
    store: GraphStore,
    output_path: str | Path,
    mode: str = "auto",
    max_full_nodes: int = 3000,
) -> Path:
    """Generate a self-contained interactive HTML visualization.

    Args:
        store: The GraphStore to read graph data from.
        output_path: Path for the output HTML file.
        mode: Rendering mode — ``"auto"``, ``"full"``, ``"community"``,
              or ``"file"``.  ``"auto"`` switches to ``"community"`` when
              the node count exceeds *max_full_nodes*.
        max_full_nodes: Threshold for auto-switching to community mode.

    Writes the HTML file to *output_path* and returns the resolved Path.
    """
    output_path = Path(output_path)
    stats = store.get_stats()
    if stats.total_nodes > 50000:
        logger.warning(
            "Graph has %d nodes — visualization may be slow. "
            "Consider filtering by file pattern.", stats.total_nodes,
        )
    data = export_graph_data(store)

    # Determine effective mode
    effective_mode = mode
    if effective_mode == "auto":
        effective_mode = (
            "community" if stats.total_nodes > max_full_nodes else "full"
        )

    if effective_mode == "community":
        # Keep full data available for drill-down; aggregate for top-level
        agg = _aggregate_community(data)
        # Escape </script> inside JSON to prevent premature tag closure
        data_json = json.dumps(agg, default=str).replace("</", "<\\/")
        html = _AGGREGATED_HTML_TEMPLATE.replace("__GRAPH_DATA__", data_json)
    elif effective_mode == "file":
        agg = _aggregate_file(data)
        data_json = json.dumps(agg, default=str).replace("</", "<\\/")
        html = _AGGREGATED_HTML_TEMPLATE.replace("__GRAPH_DATA__", data_json)
    else:
        # full mode — original behavior
        data_json = json.dumps(data, default=str).replace("</", "<\\/")
        html = _HTML_TEMPLATE.replace("__GRAPH_DATA__", data_json)

    output_path.write_text(html, encoding="utf-8")
    return output_path


# ---------------------------------------------------------------------------
# Full D3.js interactive HTML template
# ---------------------------------------------------------------------------

# Template lives in this file for zero-dependency packaging (no external files
# to locate at runtime). The E501 suppression for this module is configured via
# pyproject.toml per-file-ignores for this reason.

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Code Review Graph</title>
<script src="https://d3js.org/d3.v7.min.js" integrity="sha384-CjloA8y00+1SDAUkjs099PVfnY2KmDC2BZnws9kh8D/lX1s46w6EPhpXdqMfjK6i" crossorigin="anonymous"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { width: 100%; height: 100%; overflow: hidden; }
  body {
    background: #0d1117; color: #c9d1d9;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    font-size: 13px;
  }
  svg { display: block; width: 100%; height: 100%; }
  #legend {
    position: absolute; top: 16px; left: 16px;
    background: rgba(22,27,34,0.95); border: 1px solid #30363d;
    border-radius: 10px; padding: 16px 20px;
    font-size: 12px; line-height: 1.8;
    box-shadow: 0 8px 32px rgba(0,0,0,0.5);
    backdrop-filter: blur(12px); z-index: 10;
  }
  #legend h3 {
    font-size: 11px; font-weight: 700; margin-bottom: 6px;
    color: #8b949e; text-transform: uppercase; letter-spacing: 1px;
  }
  .legend-section { margin-bottom: 10px; }
  .legend-section:last-child { margin-bottom: 0; }
  .legend-item { display: flex; align-items: center; gap: 10px; padding: 2px 0; cursor: default; }
  .legend-item[data-edge-kind] { cursor: pointer; user-select: none; }
  .legend-item[data-edge-kind].dimmed { opacity: 0.3; }
  .legend-circle { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
  .legend-line { width: 24px; height: 0; flex-shrink: 0; border-top-width: 2px; }
  .l-calls    { border-top: 2px solid #3fb950; }
  .l-imports  { border-top: 2px dashed #f0883e; }
  .l-inherits { border-top: 2.5px dotted #d2a8ff; }
  .l-contains { border-top: 1.5px solid rgba(139,148,158,0.3); }
  #stats-bar {
    position: absolute; bottom: 0; left: 0; right: 0;
    background: rgba(13,17,23,0.95); border-top: 1px solid #21262d;
    padding: 8px 24px; display: flex; gap: 32px; justify-content: center;
    font-size: 12px; color: #8b949e; backdrop-filter: blur(12px);
  }
  .stat-item { display: flex; gap: 6px; align-items: center; }
  .stat-value { color: #e6edf3; font-weight: 600; }
  #tooltip {
    position: absolute; pointer-events: none;
    background: rgba(22,27,34,0.97); color: #c9d1d9;
    border: 1px solid #30363d; border-radius: 8px;
    padding: 12px 16px; font-size: 12px;
    box-shadow: 0 8px 32px rgba(0,0,0,0.6);
    max-width: 360px; line-height: 1.7;
    opacity: 0; transition: opacity 0.15s ease;
    z-index: 1000; backdrop-filter: blur(12px);
  }
  #tooltip.visible { opacity: 1; }
  .tt-name { font-weight: 700; font-size: 14px; color: #e6edf3; }
  .tt-kind {
    display: inline-block; font-size: 9px; font-weight: 700;
    padding: 2px 8px; border-radius: 10px; margin-left: 8px;
    text-transform: uppercase; letter-spacing: 0.5px;
  }
  .tt-row { margin-top: 4px; }
  .tt-label { color: #8b949e; }
  .tt-file { color: #58a6ff; font-size: 11px; }
  #controls {
    position: absolute; top: 16px; right: 16px;
    display: flex; gap: 8px; z-index: 10; flex-wrap: wrap;
    max-width: 650px; justify-content: flex-end;
  }
  #controls button, #controls select {
    background: rgba(22,27,34,0.95); color: #c9d1d9;
    border: 1px solid #30363d; border-radius: 8px;
    padding: 8px 14px; font-size: 12px; cursor: pointer;
    backdrop-filter: blur(12px); transition: all 0.15s;
  }
  #controls button:hover, #controls select:hover { background: #30363d; border-color: #8b949e; }
  #controls button.active { background: #1f6feb; border-color: #58a6ff; color: #fff; }
  #controls select { outline: none; max-width: 200px; }
  #controls select option { background: #161b22; color: #c9d1d9; }
  #search {
    background: rgba(22,27,34,0.95); color: #c9d1d9;
    border: 1px solid #30363d; border-radius: 8px;
    padding: 8px 14px; font-size: 12px; width: 220px;
    outline: none; backdrop-filter: blur(12px);
  }
  #search:focus { border-color: #58a6ff; }
  #search::placeholder { color: #484f58; }
  #search-results {
    position: absolute; top: 52px; right: 16px;
    background: rgba(22,27,34,0.97); border: 1px solid #30363d;
    border-radius: 8px; max-height: 240px; overflow-y: auto;
    z-index: 15; display: none; min-width: 220px;
    box-shadow: 0 8px 32px rgba(0,0,0,0.6);
  }
  .sr-item {
    padding: 8px 14px; cursor: pointer; font-size: 12px;
    border-bottom: 1px solid #21262d; display: flex; gap: 8px; align-items: center;
  }
  .sr-item:hover { background: #30363d; }
  .sr-item:last-child { border-bottom: none; }
  .sr-kind { font-size: 9px; padding: 2px 6px; border-radius: 8px; text-transform: uppercase; font-weight: 700; }
  #detail-panel {
    position: absolute; top: 16px; right: 16px;
    width: 320px; max-height: calc(100vh - 80px);
    background: rgba(22,27,34,0.97); border: 1px solid #30363d;
    border-radius: 10px; padding: 20px;
    box-shadow: 0 8px 32px rgba(0,0,0,0.6);
    backdrop-filter: blur(12px); z-index: 20;
    overflow-y: auto; display: none; font-size: 12px;
  }
  #detail-panel.visible { display: block; }
  #detail-panel h2 { font-size: 16px; color: #e6edf3; margin-bottom: 4px; word-break: break-all; }
  #detail-panel .dp-close {
    position: absolute; top: 12px; right: 14px;
    cursor: pointer; color: #8b949e; font-size: 18px; line-height: 1;
    border: none; background: none;
  }
  #detail-panel .dp-close:hover { color: #e6edf3; }
  .dp-section { margin-top: 14px; }
  .dp-section h4 { color: #8b949e; font-size: 10px; text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 6px; }
  .dp-list { list-style: none; }
  .dp-list li { padding: 3px 0; color: #c9d1d9; cursor: pointer; }
  .dp-list li:hover { color: #58a6ff; text-decoration: underline; }
  .dp-meta { color: #8b949e; }
  .dp-meta span { color: #e6edf3; font-weight: 600; }
  #filter-panel {
    position: absolute; bottom: 50px; left: 16px;
    background: rgba(22,27,34,0.95); border: 1px solid #30363d;
    border-radius: 10px; padding: 14px 18px;
    font-size: 12px; backdrop-filter: blur(12px); z-index: 10;
    box-shadow: 0 8px 32px rgba(0,0,0,0.5);
  }
  #filter-panel h3 {
    font-size: 11px; font-weight: 700; margin-bottom: 8px;
    color: #8b949e; text-transform: uppercase; letter-spacing: 1px;
  }
  .filter-item { display: flex; align-items: center; gap: 8px; padding: 3px 0; cursor: pointer; user-select: none; }
  .filter-item input { accent-color: #58a6ff; cursor: pointer; }
  #community-legend {
    position: absolute; bottom: 50px; right: 16px;
    background: rgba(22,27,34,0.95);
    border: 1px solid #30363d;
    border-radius: 10px;
    padding: 14px 18px; font-size: 12px;
    backdrop-filter: blur(12px); z-index: 10;
    box-shadow: 0 8px 32px rgba(0,0,0,0.5);
    max-height: 300px; overflow-y: auto;
    display: none;
  }
  #community-legend.visible { display: block; }
  #community-legend h3 {
    font-size: 11px; font-weight: 700;
    margin-bottom: 8px; color: #8b949e;
    text-transform: uppercase; letter-spacing: 1px;
  }
  .cl-item {
    display: flex; align-items: center;
    gap: 8px; padding: 3px 0;
    cursor: pointer; user-select: none;
  }
  .cl-item input {
    accent-color: #58a6ff; cursor: pointer;
  }
  .cl-swatch {
    width: 10px; height: 10px;
    border-radius: 50%; flex-shrink: 0;
  }
  marker { overflow: visible; }
</style>
</head>
<body>
<div id="legend" role="complementary" aria-label="Graph legend">
  <h3>Nodes</h3>
  <div class="legend-section">
    <div class="legend-item"><span class="legend-circle" style="background:#58a6ff"></span> File</div>
    <div class="legend-item"><span class="legend-circle" style="background:#f0883e"></span> Class</div>
    <div class="legend-item"><span class="legend-circle" style="background:#3fb950"></span> Function</div>
    <div class="legend-item"><span class="legend-circle" style="background:#d2a8ff"></span> Test</div>
    <div class="legend-item"><span class="legend-circle" style="background:#8b949e"></span> Type</div>
  </div>
  <h3>Edges</h3>
  <div class="legend-section">
    <div class="legend-item" data-edge-kind="CALLS"><span class="legend-line l-calls"></span> Calls</div>
    <div class="legend-item" data-edge-kind="IMPORTS_FROM"><span class="legend-line l-imports"></span> Imports</div>
    <div class="legend-item" data-edge-kind="INHERITS"><span class="legend-line l-inherits"></span> Inherits</div>
    <div class="legend-item" data-edge-kind="CONTAINS"><span class="legend-line l-contains"></span> Contains</div>
  </div>
</div>
<div id="filter-panel">
  <h3>Filter by Kind</h3>
  <label class="filter-item"><input type="checkbox" data-kind="File" checked> File</label>
  <label class="filter-item"><input type="checkbox" data-kind="Class" checked> Class</label>
  <label class="filter-item"><input type="checkbox" data-kind="Function" checked> Function</label>
  <label class="filter-item"><input type="checkbox" data-kind="Test" checked> Test</label>
  <label class="filter-item"><input type="checkbox" data-kind="Type" checked> Type</label>
</div>
<div id="controls">
  <input id="search" type="text" placeholder="Search nodes&#8230;" autocomplete="off" spellcheck="false" aria-label="Search graph nodes by name">
  <select id="flow-select" aria-label="Select execution flow to highlight"><option value="">Flows</option></select>
  <button id="btn-community" title="Toggle community coloring" aria-label="Toggle community coloring">Communities</button>
  <button id="btn-fit" title="Fit to screen" aria-label="Fit graph to screen">Fit</button>
  <button id="btn-labels" title="Toggle labels" class="active" aria-label="Toggle node labels" aria-pressed="true">Labels</button>
</div>
<div id="search-results"></div>
<div id="detail-panel"><button class="dp-close" aria-label="Close detail panel">&times;</button><div id="dp-content"></div></div>
<div id="stats-bar" role="status" aria-label="Graph statistics"></div>
<div id="community-legend" aria-label="Community legend"></div>
<div id="tooltip"></div>
<svg role="img" aria-label="Interactive code knowledge graph visualization. Use search to find nodes, click files to expand."></svg>
<script>
"use strict";
var graphData = __GRAPH_DATA__;
var KIND_COLOR  = { File:"#58a6ff", Class:"#f0883e", Function:"#3fb950", Test:"#d2a8ff", Type:"#8b949e" };
var KIND_RADIUS = { File:18, Class:12, Function:6, Test:6, Type:5 };
var EDGE_COLOR  = { CALLS:"#3fb950", IMPORTS_FROM:"#f0883e", INHERITS:"#d2a8ff", CONTAINS:"rgba(139,148,158,0.15)" };
var communityColorScale = d3.scaleOrdinal(d3.schemeTableau10);
var communityColoringOn = false;
function escH(s) { return !s ? "" : s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;").replace(/`/g,"&#96;"); }
function displayName(d) {
  if (d.kind === "File") {
    var fp = d.file_path || d.qualified_name || d.name;
    var parts = fp.replace(/\\/g, "/").split("/");
    var fname = parts.pop();
    var parent = parts.pop() || "";
    return parent ? parent + "/" + fname : fname;
  }
  return d.name;
}
var nodes = graphData.nodes.map(function(d) { var o = Object.assign({}, d); o._id = d.qualified_name; o.label = displayName(d); return o; });
var edges = graphData.edges.map(function(d) { var o = Object.assign({}, d); o._source = d.source; o._target = d.target; return o; });
var stats = graphData.stats;
var flows = graphData.flows || [];
var communities = graphData.communities || [];
var nodeById = new Map(nodes.map(function(n) { return [n.qualified_name, n]; }));
var hiddenEdgeKinds = new Set();
var hiddenNodeKinds = new Set();
var collapsedFiles = new Set();
var containsChildren = new Map();
var childToParent = new Map();
edges.forEach(function(e) {
  if (e.kind === "CONTAINS") {
    if (!containsChildren.has(e._source)) containsChildren.set(e._source, new Set());
    containsChildren.get(e._source).add(e._target);
    childToParent.set(e._target, e._source);
  }
});
function allDescendants(qn) {
  var result = new Set();
  var stack = [qn];
  while (stack.length) {
    var cur = stack.pop();
    var children = containsChildren.get(cur);
    if (!children) continue;
    children.forEach(function(c) { if (!result.has(c)) { result.add(c); stack.push(c); } });
  }
  return result;
}
var nodeToCommunity = new Map();
communities.forEach(function(c) {
  (c.members || []).forEach(function(qn) {
    nodeToCommunity.set(qn, c.id);
  });
});
// Compute node degree (connection count) for size scaling
var nodeDegree = new Map();
edges.forEach(function(e) {
  var s = e._source, t = e._target;
  nodeDegree.set(s, (nodeDegree.get(s) || 0) + 1);
  nodeDegree.set(t, (nodeDegree.get(t) || 0) + 1);
});
var maxDegree = 1;
nodeDegree.forEach(function(v) {
  if (v > maxDegree) maxDegree = v;
});
function degreeRadius(d) {
  var base = KIND_RADIUS[d.kind] || 6;
  var deg = nodeDegree.get(d.qualified_name) || 0;
  // Scale: base + up to 2x base proportional to degree
  var scale = 1 + (deg / maxDegree);
  return Math.round(base * scale);
}
var nodeIdToQn = new Map();
nodes.forEach(function(n) { nodeIdToQn.set(n.id, n.qualified_name); });
var flowSelect = document.getElementById("flow-select");
flows.forEach(function(f, i) {
  var opt = document.createElement("option");
  opt.value = i;
  opt.textContent = f.name + " (" + f.node_count + " nodes)";
  flowSelect.appendChild(opt);
});
var statsBar = document.getElementById("stats-bar");
var langList = (stats.languages || []).join(", ") || "n/a";
function si(l, v) { return '<div class="stat-item"><span class="tt-label">' + escH(l) + '</span> <span class="stat-value">' + escH(String(v)) + '</span></div>'; }
statsBar.textContent = "";
statsBar.insertAdjacentHTML("beforeend", si("Nodes", stats.total_nodes) + si("Edges", stats.total_edges) + si("Files", stats.files_count) + si("Languages", langList));
var tooltip = document.getElementById("tooltip");
function showTooltip(ev, d) {
  var bg = communityColoringOn && d.community_id != null ? communityColorScale(d.community_id) : (KIND_COLOR[d.kind] || "#555");
  var relFile = d.file_path ? d.file_path.split("/").slice(-3).join("/") : "";
  var h = '<span class="tt-name">' + escH(d.label) + '</span>';
  h += '<span class="tt-kind" style="background:' + bg + ';color:#0d1117">' + escH(d.kind) + '</span>';
  if (relFile) h += '<div class="tt-row tt-file">' + escH(relFile) + '</div>';
  if (d.line_start != null) h += '<div class="tt-row"><span class="tt-label">Lines: </span>' + d.line_start + ' \u2013 ' + (d.line_end || d.line_start) + '</div>';
  if (d.params) h += '<div class="tt-row"><span class="tt-label">Params: </span>' + escH(d.params) + '</div>';
  if (d.return_type) h += '<div class="tt-row"><span class="tt-label">Returns: </span>' + escH(d.return_type) + '</div>';
  if (d.community_id != null) {
    var comm = communities.find(function(c) { return c.id === d.community_id; });
    if (comm) h += '<div class="tt-row"><span class="tt-label">Community: </span>' + escH(comm.name) + '</div>';
  }
  tooltip.textContent = "";
  tooltip.insertAdjacentHTML("beforeend", h);
  tooltip.classList.add("visible");
  moveTooltip(ev);
}
function moveTooltip(ev) {
  var p = 14;
  var x = ev.pageX + p, y = ev.pageY + p;
  var r = tooltip.getBoundingClientRect();
  if (x + r.width > innerWidth - p) x = ev.pageX - r.width - p;
  if (y + r.height > innerHeight - p) y = ev.pageY - r.height - p;
  tooltip.style.left = x + "px"; tooltip.style.top = y + "px";
}
function hideTooltip() { tooltip.classList.remove("visible"); }
var W = innerWidth, H = innerHeight;
var svg = d3.select("svg").attr("viewBox", [0, 0, W, H]);
var gRoot = svg.append("g");
var currentTransform = d3.zoomIdentity;
var zoomBehavior = d3.zoom()
  .scaleExtent([0.05, 8])
  .on("zoom", function(ev) { currentTransform = ev.transform; gRoot.attr("transform", ev.transform); updateLabelVisibility(); });
svg.call(zoomBehavior);
var defs = svg.append("defs");
var glow = defs.append("filter").attr("id","glow").attr("x","-50%").attr("y","-50%").attr("width","200%").attr("height","200%");
glow.append("feGaussianBlur").attr("stdDeviation","3").attr("result","blur");
glow.append("feComposite").attr("in","SourceGraphic").attr("in2","blur").attr("operator","over");
[{id:"arrow-calls",color:"#3fb950"},{id:"arrow-imports",color:"#f0883e"},{id:"arrow-inherits",color:"#d2a8ff"}].forEach(function(mk) {
  defs.append("marker").attr("id", mk.id)
    .attr("viewBox","0 -5 10 10").attr("refX",28).attr("refY",0)
    .attr("markerWidth",8).attr("markerHeight",8).attr("orient","auto")
    .append("path").attr("d","M0,-4L10,0L0,4Z").attr("fill",mk.color);
});
var N = nodes.length;
var isLarge = N > 300;
var simulation = d3.forceSimulation(nodes)
  .force("link", d3.forceLink(edges).id(function(d) { return d.qualified_name; })
    .distance(function(d) { return d.kind === "CONTAINS" ? 35 : (isLarge ? 80 : 120); })
    .strength(function(d) { return d.kind === "CONTAINS" ? 1.5 : 0.15; }))
  .force("charge", d3.forceManyBody().strength(function(d) { return d.kind === "File" ? (isLarge ? -200 : -400) : (isLarge ? -60 : -120); }).theta(0.85).distanceMax(600))
  .force("collide", d3.forceCollide().radius(function(d) { return degreeRadius(d) + 4; }))
  .force("center", d3.forceCenter(W / 2, H / 2))
  .force("x", d3.forceX(W / 2).strength(0.03))
  .force("y", d3.forceY(H / 2).strength(0.03))
  .alphaDecay(isLarge ? 0.04 : 0.025)
  .velocityDecay(0.4);
var EDGE_CFG = {
  CONTAINS:     { dash:null, width:1, opacity:0.08, marker:"" },
  CALLS:        { dash:null, width:1.5, opacity:0.7, marker:"url(#arrow-calls)" },
  IMPORTS_FROM: { dash:"6,3", width:1.5, opacity:0.65, marker:"url(#arrow-imports)" },
  INHERITS:     { dash:"3,4", width:2, opacity:0.7, marker:"url(#arrow-inherits)" },
};
function eStyle(d) { return EDGE_CFG[d.kind] || {dash:null,width:1,opacity:0.3,marker:""}; }
function eColor(d) { return EDGE_COLOR[d.kind] || "#484f58"; }
function nodeColor(d) {
  if (communityColoringOn && d.community_id != null) return communityColorScale(d.community_id);
  return KIND_COLOR[d.kind] || "#8b949e";
}
var linkGroup  = gRoot.append("g").attr("class","links");
var nodeGroup  = gRoot.append("g").attr("class","nodes");
var labelGroup = gRoot.append("g").attr("class","labels");
var linkSel, labelSel;
var showLabels = true;
function updateLinks() {
  var vis = new Set(nodes.filter(function(n) { return !n._hidden; }).map(function(n) { return n.qualified_name; }));
  var visEdges = edges.filter(function(e) {
    if (hiddenEdgeKinds.has(e.kind)) return false;
    var s = typeof e.source === "object" ? e.source.qualified_name : e._source;
    var t = typeof e.target === "object" ? e.target.qualified_name : e._target;
    return vis.has(s) && vis.has(t);
  });
  linkSel = linkGroup.selectAll("line").data(visEdges, function(d) { return d._source+"->"+d._target+":"+d.kind; });
  linkSel.exit().remove();
  var enter = linkSel.enter().append("line");
  linkSel = enter.merge(linkSel);
  linkSel
    .attr("stroke", function(d) { return eColor(d); })
    .attr("stroke-width", function(d) { return eStyle(d).width; })
    .attr("stroke-dasharray", function(d) { return eStyle(d).dash; })
    .attr("opacity", function(d) { return eStyle(d).opacity; })
    .attr("marker-end", function(d) { return eStyle(d).marker; });
}
function updateNodes() {
  var hiddenSet = new Set();
  collapsedFiles.forEach(function(fqn) { allDescendants(fqn).forEach(function(c) { hiddenSet.add(c); }); });
  nodes.forEach(function(n) { n._hidden = hiddenSet.has(n.qualified_name) || hiddenNodeKinds.has(n.kind); });
  var vis = nodes.filter(function(n) { return !n._hidden; });
  var nodeSel = nodeGroup.selectAll("g.node-g").data(vis, function(d) { return d.qualified_name; });
  nodeSel.exit().remove();
  var enter = nodeSel.enter().append("g").attr("class","node-g");
  enter.filter(function(d) { return d.kind === "File"; }).append("circle")
    .attr("class","glow-ring")
    .attr("r", function(d) { return degreeRadius(d) + 5; })
    .attr("fill","none")
    .attr("stroke", function(d) { return nodeColor(d); })
    .attr("stroke-width", 1.5).attr("opacity", 0.3).attr("filter","url(#glow)");
  enter.append("circle").attr("class","node-circle")
    .attr("r", function(d) { return degreeRadius(d); })
    .attr("fill", function(d) { return nodeColor(d); })
    .attr("stroke", function(d) { return d.kind === "File" ? "rgba(88,166,255,0.3)" : "rgba(255,255,255,0.08)"; })
    .attr("stroke-width", function(d) { return d.kind === "File" ? 2 : 1; })
    .attr("cursor", "pointer");
  enter
    .on("mouseover", function(ev, d) { highlightConnected(d, true); showTooltip(ev, d); })
    .on("mousemove", function(ev) { moveTooltip(ev); })
    .on("mouseout",  function(ev, d) { highlightConnected(d, false); hideTooltip(); })
    .on("click", function(ev, d) {
      ev.stopPropagation();
      if (d.kind === "File" && !ev.shiftKey) toggleCollapse(d.qualified_name);
      showDetailPanel(d);
    })
    .call(d3.drag().on("start", dragS).on("drag", dragD).on("end", dragE));
  nodeSel = enter.merge(nodeSel);
  labelSel = labelGroup.selectAll("text.node-label").data(vis, function(d) { return d.qualified_name; });
  labelSel.exit().remove();
  var lEnter = labelSel.enter().append("text").attr("class","node-label")
    .attr("text-anchor","start").attr("dy","0.35em")
    .text(function(d) { return d.label; })
    .attr("fill", function(d) { return d.kind === "File" ? "#e6edf3" : d.kind === "Class" ? "#f0883e" : "#8b949e"; })
    .attr("font-size", function(d) { return d.kind === "File" ? "12px" : d.kind === "Class" ? "11px" : "10px"; })
    .attr("font-weight", function(d) { return d.kind === "File" ? 700 : d.kind === "Class" ? 600 : 400; });
  labelSel = lEnter.merge(labelSel);
  updateLinks();
  updateLabelVisibility();
}
function updateLabelVisibility() {
  if (!labelSel) return;
  var s = currentTransform.k;
  labelSel.attr("display", function(d) {
    if (!showLabels) return "none";
    if (d.kind === "File") return null;
    if (d.kind === "Class") return s > 0.5 ? null : "none";
    return s > 1.0 ? null : "none";
  });
}
function highlightConnected(d, on) {
  if (on) {
    var connected = new Set([d.qualified_name]);
    edges.forEach(function(e) {
      var s = typeof e.source === "object" ? e.source.qualified_name : e._source;
      var t = typeof e.target === "object" ? e.target.qualified_name : e._target;
      if (s === d.qualified_name) connected.add(t);
      if (t === d.qualified_name) connected.add(s);
    });
    nodeGroup.selectAll("g.node-g").select(".node-circle")
      .transition().duration(150).attr("opacity", function(n) { return connected.has(n.qualified_name) ? 1 : 0.15; });
    linkSel.transition().duration(150)
      .attr("opacity", function(e) {
        var s = typeof e.source === "object" ? e.source.qualified_name : e._source;
        var t = typeof e.target === "object" ? e.target.qualified_name : e._target;
        return (s === d.qualified_name || t === d.qualified_name) ? 0.9 : 0.03;
      })
      .attr("stroke-width", function(e) {
        var s = typeof e.source === "object" ? e.source.qualified_name : e._source;
        var t = typeof e.target === "object" ? e.target.qualified_name : e._target;
        return (s === d.qualified_name || t === d.qualified_name) ? 2.5 : eStyle(e).width;
      });
    labelSel.transition().duration(150).attr("opacity", function(n) { return connected.has(n.qualified_name) ? 1 : 0.1; });
  } else {
    nodeGroup.selectAll("g.node-g").select(".node-circle").transition().duration(300).attr("opacity", 1);
    linkSel.transition().duration(300)
      .attr("opacity", function(e) { return eStyle(e).opacity; })
      .attr("stroke-width", function(e) { return eStyle(e).width; });
    labelSel.transition().duration(300).attr("opacity", 1);
    updateLabelVisibility();
  }
}
function toggleCollapse(qn) {
  if (collapsedFiles.has(qn)) collapsedFiles.delete(qn); else collapsedFiles.add(qn);
  nodeGroup.selectAll("g.node-g").select(".glow-ring")
    .attr("stroke-dasharray", function(d) { return collapsedFiles.has(d.qualified_name) ? "4,3" : null; })
    .attr("opacity", function(d) { return collapsedFiles.has(d.qualified_name) ? 0.6 : 0.3; });
  updateNodes();
  simulation.alpha(0.3).restart();
}
function dragS(ev, d) { if (!ev.active) simulation.alphaTarget(0.1).restart(); d.fx = d.x; d.fy = d.y; }
function dragD(ev, d) { d.fx = ev.x; d.fy = ev.y; }
function dragE(ev, d) { if (!ev.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; }
simulation.on("tick", function() {
  if (linkSel) linkSel
    .attr("x1", function(d) { return d.source.x; }).attr("y1", function(d) { return d.source.y; })
    .attr("x2", function(d) { return d.target.x; }).attr("y2", function(d) { return d.target.y; });
  nodeGroup.selectAll("g.node-g").attr("transform", function(d) { return "translate(" + d.x + "," + d.y + ")"; });
  if (labelSel) labelSel
    .attr("x", function(d) { return d.x + degreeRadius(d) + 5; })
    .attr("y", function(d) { return d.y; });
});
// Only auto-collapse File nodes on very large graphs, otherwise all edges
// become invisible because they connect to Functions/Classes that are now
// hidden beneath collapsed Files. See: #132
if (N > 2000) {
  nodes.forEach(function(n) { if (n.kind === "File") collapsedFiles.add(n.qualified_name); });
}
updateNodes();
function fitGraph() {
  var b = gRoot.node().getBBox();
  if (b.width === 0 || b.height === 0) return;
  var pad = 0.1;
  var fw = b.width * (1 + 2*pad), fh = b.height * (1 + 2*pad);
  var s = Math.min(W / fw, H / fh, 2.5);
  var tx = W/2 - (b.x + b.width/2)*s, ty = H/2 - (b.y + b.height/2)*s;
  svg.transition().duration(600).call(zoomBehavior.transform, d3.zoomIdentity.translate(tx, ty).scale(s));
}
simulation.on("end", fitGraph);
function zoomToNode(qn) {
  var nd = nodeById.get(qn);
  if (!nd || nd.x == null) return;
  var s = 2.0;
  var tx = W/2 - nd.x*s, ty = H/2 - nd.y*s;
  svg.transition().duration(600).call(zoomBehavior.transform, d3.zoomIdentity.translate(tx, ty).scale(s));
}
document.getElementById("btn-fit").addEventListener("click", fitGraph);
document.getElementById("btn-labels").addEventListener("click", function() {
  showLabels = !showLabels;
  this.classList.toggle("active");
  this.setAttribute("aria-pressed", showLabels);
  updateLabelVisibility();
});
document.querySelectorAll(".legend-item[data-edge-kind]").forEach(function(el) {
  el.addEventListener("click", function() {
    var kind = this.dataset.edgeKind;
    if (hiddenEdgeKinds.has(kind)) { hiddenEdgeKinds.delete(kind); this.classList.remove("dimmed"); }
    else { hiddenEdgeKinds.add(kind); this.classList.add("dimmed"); }
    updateLinks();
  });
});
document.querySelectorAll("#filter-panel input[data-kind]").forEach(function(el) {
  el.addEventListener("change", function() {
    var kind = this.dataset.kind;
    if (this.checked) hiddenNodeKinds.delete(kind); else hiddenNodeKinds.add(kind);
    updateNodes();
    simulation.alpha(0.15).restart();
  });
});
document.getElementById("btn-community").addEventListener("click", function() {
  communityColoringOn = !communityColoringOn;
  this.classList.toggle("active");
  nodeGroup.selectAll("g.node-g").select(".node-circle")
    .transition().duration(300)
    .attr("fill", function(d) { return nodeColor(d); });
  nodeGroup.selectAll("g.node-g").select(".glow-ring")
    .transition().duration(300)
    .attr("stroke", function(d) { return nodeColor(d); });
  // Toggle community legend visibility with coloring
  var cl = document.getElementById("community-legend");
  if (communityColoringOn) cl.classList.add("visible");
  else cl.classList.remove("visible");
});
// Build community legend with toggle checkboxes
var hiddenCommunities = new Set();
(function buildCommunityLegend() {
  var cl = document.getElementById("community-legend");
  if (!communities.length) return;
  var h = '<h3>Communities</h3>';
  communities.slice(0, 30).forEach(function(c) {
    var col = communityColorScale(c.id);
    var nm = escH(c.name || ("Community " + c.id));
    h += '<label class="cl-item">'
      + '<input type="checkbox" data-cid="'
      + c.id + '" checked>'
      + '<span class="cl-swatch" style="background:'
      + col + '"></span> ' + nm + '</label>';
  });
  cl.textContent = "";
  cl.insertAdjacentHTML("beforeend", h);
  cl.querySelectorAll("input[data-cid]").forEach(
    function(el) {
      el.addEventListener("change", function() {
        var cid = parseInt(this.dataset.cid);
        if (this.checked) hiddenCommunities.delete(cid);
        else hiddenCommunities.add(cid);
        applyCommunityFilter();
      });
    }
  );
})();
function applyCommunityFilter() {
  if (!communityColoringOn || !hiddenCommunities.size) {
    nodeGroup.selectAll("g.node-g")
      .attr("display", function(d) {
        return d._hidden ? "none" : null;
      });
    updateLinks();
    return;
  }
  nodeGroup.selectAll("g.node-g")
    .attr("display", function(d) {
      if (d._hidden) return "none";
      var cid = d.community_id;
      if (cid == null) {
        cid = nodeToCommunity.get(d.qualified_name);
      }
      if (cid != null && hiddenCommunities.has(cid)) {
        return "none";
      }
      return null;
    });
  if (labelSel) labelSel
    .attr("display", function(d) {
      var cid = d.community_id;
      if (cid == null) {
        cid = nodeToCommunity.get(d.qualified_name);
      }
      if (cid != null && hiddenCommunities.has(cid)) {
        return "none";
      }
      return null;
    });
  updateLinks();
}
var activeFlowQns = null;
flowSelect.addEventListener("change", function() {
  var idx = this.value;
  if (idx === "") { activeFlowQns = null; clearFlowHighlight(); return; }
  var flow = flows[parseInt(idx)];
  if (!flow) return;
  var pathQns = new Set();
  (flow.path || []).forEach(function(nid) { var qn = nodeIdToQn.get(nid); if (qn) pathQns.add(qn); });
  activeFlowQns = pathQns;
  applyFlowHighlight();
});
function applyFlowHighlight() {
  if (!activeFlowQns || activeFlowQns.size === 0) { clearFlowHighlight(); return; }
  nodeGroup.selectAll("g.node-g").select(".node-circle").transition().duration(200)
    .attr("opacity", function(d) { return activeFlowQns.has(d.qualified_name) ? 1 : 0.2; });
  if (labelSel) labelSel.transition().duration(200)
    .attr("opacity", function(d) { return activeFlowQns.has(d.qualified_name) ? 1 : 0.1; });
  if (linkSel) linkSel.transition().duration(200)
    .attr("opacity", function(e) {
      var s = typeof e.source === "object" ? e.source.qualified_name : e._source;
      var t = typeof e.target === "object" ? e.target.qualified_name : e._target;
      return (activeFlowQns.has(s) && activeFlowQns.has(t)) ? 0.9 : 0.03;
    });
}
function clearFlowHighlight() {
  nodeGroup.selectAll("g.node-g").select(".node-circle").transition().duration(300).attr("opacity", 1);
  if (linkSel) linkSel.transition().duration(300).attr("opacity", function(e) { return eStyle(e).opacity; });
  if (labelSel) labelSel.transition().duration(300).attr("opacity", 1);
  updateLabelVisibility();
}
var detailPanel = document.getElementById("detail-panel");
var dpContent = document.getElementById("dp-content");
document.querySelector("#detail-panel .dp-close").addEventListener("click", function() {
  detailPanel.classList.remove("visible");
});
svg.on("click", function() { detailPanel.classList.remove("visible"); });
function showDetailPanel(d) {
  var callers = [], callees = [];
  edges.forEach(function(e) {
    var s = typeof e.source === "object" ? e.source.qualified_name : e._source;
    var t = typeof e.target === "object" ? e.target.qualified_name : e._target;
    if (t === d.qualified_name && e.kind === "CALLS") { var sN = nodeById.get(s); if (sN) callers.push(sN); }
    if (s === d.qualified_name && e.kind === "CALLS") { var tN = nodeById.get(t); if (tN) callees.push(tN); }
  });
  var relFile = d.file_path ? d.file_path.split("/").slice(-3).join("/") : "";
  var bg = communityColoringOn && d.community_id != null ? communityColorScale(d.community_id) : (KIND_COLOR[d.kind] || "#555");
  var h = '<h2>' + escH(d.label) + '</h2>';
  h += '<span class="tt-kind" style="background:' + bg + ';color:#0d1117">' + escH(d.kind) + '</span>';
  if (relFile) h += '<div class="dp-meta" style="margin-top:8px">' + escH(relFile) + (d.line_start != null ? ':' + d.line_start : '') + '</div>';
  if (d.params) h += '<div class="dp-meta"><span class="tt-label">Params:</span> ' + escH(d.params) + '</div>';
  if (d.return_type) h += '<div class="dp-meta"><span class="tt-label">Returns:</span> ' + escH(d.return_type) + '</div>';
  if (d.community_id != null) {
    var comm = communities.find(function(c) { return c.id === d.community_id; });
    if (comm) h += '<div class="dp-meta"><span class="tt-label">Community:</span> ' + escH(comm.name) + '</div>';
  }
  if (callers.length) {
    h += '<div class="dp-section"><h4>Callers (' + callers.length + ')</h4><ul class="dp-list">';
    callers.slice(0, 20).forEach(function(c) { h += '<li data-qn="' + escH(c.qualified_name) + '">' + escH(c.label) + '</li>'; });
    h += '</ul></div>';
  }
  if (callees.length) {
    h += '<div class="dp-section"><h4>Callees (' + callees.length + ')</h4><ul class="dp-list">';
    callees.slice(0, 20).forEach(function(c) { h += '<li data-qn="' + escH(c.qualified_name) + '">' + escH(c.label) + '</li>'; });
    h += '</ul></div>';
  }
  dpContent.textContent = "";
  dpContent.insertAdjacentHTML("beforeend", h);
  detailPanel.classList.add("visible");
  dpContent.querySelectorAll("li[data-qn]").forEach(function(li) {
    li.addEventListener("click", function() {
      var qn = li.dataset.qn;
      zoomToNode(qn);
      var nd = nodeById.get(qn);
      if (nd) showDetailPanel(nd);
    });
  });
}
var searchInput = document.getElementById("search");
var searchResults = document.getElementById("search-results");
var searchTerm = "";
searchInput.addEventListener("input", function() {
  searchTerm = this.value.trim().toLowerCase();
  applySearchFilter();
  showSearchResults();
});
searchInput.addEventListener("focus", showSearchResults);
document.addEventListener("click", function(ev) {
  if (!searchResults.contains(ev.target) && ev.target !== searchInput) searchResults.style.display = "none";
});
function showSearchResults() {
  if (!searchTerm) { searchResults.style.display = "none"; return; }
  var matched = [];
  nodes.forEach(function(n) {
    if (n._hidden) return;
    var hay = (n.label + " " + n.qualified_name).toLowerCase();
    if (hay.indexOf(searchTerm) !== -1) matched.push(n);
  });
  if (!matched.length) { searchResults.style.display = "none"; return; }
  searchResults.textContent = "";
  matched.slice(0, 15).forEach(function(n) {
    var bg = KIND_COLOR[n.kind] || "#555";
    var div = document.createElement("div");
    div.className = "sr-item";
    var kindSpan = document.createElement("span");
    kindSpan.className = "sr-kind";
    kindSpan.style.background = bg;
    kindSpan.style.color = "#0d1117";
    kindSpan.textContent = n.kind;
    div.appendChild(kindSpan);
    div.appendChild(document.createTextNode(" " + n.label));
    div.addEventListener("click", function() {
      zoomToNode(n.qualified_name);
      showDetailPanel(n);
      searchResults.style.display = "none";
    });
    searchResults.appendChild(div);
  });
  searchResults.style.display = "block";
}
function applySearchFilter() {
  if (!searchTerm) {
    nodeGroup.selectAll("g.node-g").select(".node-circle").attr("opacity", 1);
    if (labelSel) labelSel.attr("opacity", 1);
    if (linkSel) linkSel.attr("opacity", function(e) { return eStyle(e).opacity; });
    updateLabelVisibility();
    return;
  }
  var matched = new Set();
  nodes.forEach(function(n) {
    if (n._hidden) return;
    var hay = (n.label + " " + n.qualified_name).toLowerCase();
    if (hay.indexOf(searchTerm) !== -1) matched.add(n.qualified_name);
  });
  nodeGroup.selectAll("g.node-g").select(".node-circle")
    .attr("opacity", function(d) { return matched.has(d.qualified_name) ? 1 : 0.08; });
  if (labelSel) labelSel
    .attr("opacity", function(d) { return matched.has(d.qualified_name) ? 1 : 0.05; })
    .attr("display", function(d) { return matched.has(d.qualified_name) ? null : "none"; });
  if (linkSel) linkSel.attr("opacity", function(e) {
    var s = typeof e.source === "object" ? e.source.qualified_name : e._source;
    var t = typeof e.target === "object" ? e.target.qualified_name : e._target;
    return (matched.has(s) || matched.has(t)) ? eStyle(e).opacity : 0.02;
  });
}
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Aggregated-mode HTML template (community / file)
# ---------------------------------------------------------------------------
# Supports community super-nodes with drill-down (double-click) and a Back
# button to return to the overview.
# NOTE: innerHTML / insertAdjacentHTML usage below mirrors the original
# _HTML_TEMPLATE and is safe because all interpolated values pass through
# escH() which escapes &, <, >, ", ', and backtick characters.

_AGGREGATED_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Code Review Graph (Aggregated)</title>
<script src="https://d3js.org/d3.v7.min.js" integrity="sha384-CjloA8y00+1SDAUkjs099PVfnY2KmDC2BZnws9kh8D/lX1s46w6EPhpXdqMfjK6i" crossorigin="anonymous"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { width: 100%; height: 100%; overflow: hidden; }
  body {
    background: #0d1117; color: #c9d1d9;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    font-size: 13px;
  }
  svg { display: block; width: 100%; height: 100%; }
  #legend {
    position: absolute; top: 16px; left: 16px;
    background: rgba(22,27,34,0.95); border: 1px solid #30363d;
    border-radius: 10px; padding: 16px 20px;
    font-size: 12px; line-height: 1.8;
    box-shadow: 0 8px 32px rgba(0,0,0,0.5);
    backdrop-filter: blur(12px); z-index: 10;
  }
  #legend h3 {
    font-size: 11px; font-weight: 700; margin-bottom: 6px;
    color: #8b949e; text-transform: uppercase; letter-spacing: 1px;
  }
  .legend-section { margin-bottom: 10px; }
  .legend-section:last-child { margin-bottom: 0; }
  .legend-item { display: flex; align-items: center; gap: 10px; padding: 2px 0; cursor: default; }
  .legend-circle { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
  .legend-line { width: 24px; height: 0; flex-shrink: 0; border-top-width: 2px; }
  .l-cross { border-top: 2px solid #58a6ff; }
  .l-dep   { border-top: 2px dashed #f0883e; }
  .l-calls { border-top: 2px solid #3fb950; }
  .l-imports { border-top: 2px dashed #f0883e; }
  .l-inherits { border-top: 2.5px dotted #d2a8ff; }
  .l-contains { border-top: 1.5px solid rgba(139,148,158,0.3); }
  #stats-bar {
    position: absolute; bottom: 0; left: 0; right: 0;
    background: rgba(13,17,23,0.95); border-top: 1px solid #21262d;
    padding: 8px 24px; display: flex; gap: 32px; justify-content: center;
    font-size: 12px; color: #8b949e; backdrop-filter: blur(12px);
  }
  .stat-item { display: flex; gap: 6px; align-items: center; }
  .stat-value { color: #e6edf3; font-weight: 600; }
  #tooltip {
    position: absolute; pointer-events: none;
    background: rgba(22,27,34,0.97); color: #c9d1d9;
    border: 1px solid #30363d; border-radius: 8px;
    padding: 12px 16px; font-size: 12px;
    box-shadow: 0 8px 32px rgba(0,0,0,0.6);
    max-width: 360px; line-height: 1.7;
    opacity: 0; transition: opacity 0.15s ease;
    z-index: 1000; backdrop-filter: blur(12px);
  }
  #tooltip.visible { opacity: 1; }
  .tt-name { font-weight: 700; font-size: 14px; color: #e6edf3; }
  .tt-kind {
    display: inline-block; font-size: 9px; font-weight: 700;
    padding: 2px 8px; border-radius: 10px; margin-left: 8px;
    text-transform: uppercase; letter-spacing: 0.5px;
  }
  .tt-row { margin-top: 4px; }
  .tt-label { color: #8b949e; }
  .tt-file { color: #58a6ff; font-size: 11px; }
  #controls {
    position: absolute; top: 16px; right: 16px;
    display: flex; gap: 8px; z-index: 10; flex-wrap: wrap;
    max-width: 650px; justify-content: flex-end;
  }
  #controls button, #controls select {
    background: rgba(22,27,34,0.95); color: #c9d1d9;
    border: 1px solid #30363d; border-radius: 8px;
    padding: 8px 14px; font-size: 12px; cursor: pointer;
    backdrop-filter: blur(12px); transition: all 0.15s;
  }
  #controls button:hover, #controls select:hover { background: #30363d; border-color: #8b949e; }
  #controls button.active { background: #1f6feb; border-color: #58a6ff; color: #fff; }
  #controls select { outline: none; max-width: 200px; }
  #controls select option { background: #161b22; color: #c9d1d9; }
  #search {
    background: rgba(22,27,34,0.95); color: #c9d1d9;
    border: 1px solid #30363d; border-radius: 8px;
    padding: 8px 14px; font-size: 12px; width: 220px;
    outline: none; backdrop-filter: blur(12px);
  }
  #search:focus { border-color: #58a6ff; }
  #search::placeholder { color: #484f58; }
  #search-results {
    position: absolute; top: 52px; right: 16px;
    background: rgba(22,27,34,0.97); border: 1px solid #30363d;
    border-radius: 8px; max-height: 240px; overflow-y: auto;
    z-index: 15; display: none; min-width: 220px;
    box-shadow: 0 8px 32px rgba(0,0,0,0.6);
  }
  .sr-item {
    padding: 8px 14px; cursor: pointer; font-size: 12px;
    border-bottom: 1px solid #21262d; display: flex; gap: 8px; align-items: center;
  }
  .sr-item:hover { background: #30363d; }
  .sr-item:last-child { border-bottom: none; }
  .sr-kind { font-size: 9px; padding: 2px 6px; border-radius: 8px; text-transform: uppercase; font-weight: 700; }
  #detail-panel {
    position: absolute; top: 16px; right: 16px;
    width: 320px; max-height: calc(100vh - 80px);
    background: rgba(22,27,34,0.97); border: 1px solid #30363d;
    border-radius: 10px; padding: 20px;
    box-shadow: 0 8px 32px rgba(0,0,0,0.6);
    backdrop-filter: blur(12px); z-index: 20;
    overflow-y: auto; display: none; font-size: 12px;
  }
  #detail-panel.visible { display: block; }
  #detail-panel h2 { font-size: 16px; color: #e6edf3; margin-bottom: 4px; word-break: break-all; }
  #detail-panel .dp-close {
    position: absolute; top: 12px; right: 14px;
    cursor: pointer; color: #8b949e; font-size: 18px; line-height: 1;
    border: none; background: none;
  }
  #detail-panel .dp-close:hover { color: #e6edf3; }
  .dp-section { margin-top: 14px; }
  .dp-section h4 { color: #8b949e; font-size: 10px; text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 6px; }
  .dp-list { list-style: none; }
  .dp-list li { padding: 3px 0; color: #c9d1d9; cursor: pointer; }
  .dp-list li:hover { color: #58a6ff; text-decoration: underline; }
  .dp-meta { color: #8b949e; }
  .dp-meta span { color: #e6edf3; font-weight: 600; }
  #btn-back {
    display: none;
    position: absolute; bottom: 50px; right: 16px;
    background: #1f6feb; color: #fff;
    border: 1px solid #58a6ff; border-radius: 8px;
    padding: 10px 18px; font-size: 13px; cursor: pointer;
    z-index: 10; font-weight: 600;
    box-shadow: 0 4px 16px rgba(31,111,235,0.4);
    transition: all 0.15s;
  }
  #btn-back:hover { background: #388bfd; }
  #filter-panel {
    position: absolute; bottom: 50px; left: 16px;
    background: rgba(22,27,34,0.95); border: 1px solid #30363d;
    border-radius: 10px; padding: 14px 18px;
    font-size: 12px; backdrop-filter: blur(12px); z-index: 10;
    box-shadow: 0 8px 32px rgba(0,0,0,0.5);
  }
  #filter-panel h3 {
    font-size: 11px; font-weight: 700; margin-bottom: 8px;
    color: #8b949e; text-transform: uppercase; letter-spacing: 1px;
  }
  .filter-item { display: flex; align-items: center; gap: 8px; padding: 3px 0; cursor: pointer; user-select: none; }
  .filter-item input { accent-color: #58a6ff; cursor: pointer; }
  marker { overflow: visible; }
</style>
</head>
<body>
<div id="legend" role="complementary" aria-label="Graph legend">
  <h3>Nodes</h3>
  <div class="legend-section" id="legend-nodes"></div>
  <h3>Edges</h3>
  <div class="legend-section" id="legend-edges"></div>
</div>
<div id="filter-panel">
  <h3>View Mode</h3>
  <div id="filter-info" style="color:#8b949e;font-size:11px;"></div>
</div>
<div id="controls">
  <input id="search" type="text" placeholder="Search nodes&#8230;" autocomplete="off" spellcheck="false" aria-label="Search graph nodes by name">
  <button id="btn-fit" title="Fit to screen" aria-label="Fit graph to screen">Fit</button>
  <button id="btn-labels" title="Toggle labels" class="active" aria-label="Toggle node labels" aria-pressed="true">Labels</button>
</div>
<div id="search-results"></div>
<div id="detail-panel"><button class="dp-close" aria-label="Close detail panel">&times;</button><div id="dp-content"></div></div>
<div id="stats-bar" role="status" aria-label="Graph statistics"></div>
<div id="tooltip"></div>
<button id="btn-back" aria-label="Back to overview">&larr; Back to Overview</button>
<svg role="img" aria-label="Interactive code knowledge graph visualization (aggregated view)."></svg>
<script>
"use strict";
var graphData = __GRAPH_DATA__;
var dataMode = graphData.mode || "full";
var communityDetails = graphData.community_details || {};

var communityColorScale = d3.scaleOrdinal(d3.schemeTableau10);
function escH(s) { return !s ? "" : s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;").replace(/`/g,"&#96;"); }

var KIND_COLOR = {
  Community: "#1f6feb", File: "#58a6ff", Class: "#f0883e",
  Function: "#3fb950", Test: "#d2a8ff", Type: "#8b949e"
};
var EDGE_COLOR = {
  CROSS_COMMUNITY: "#58a6ff", DEPENDS_ON: "#f0883e",
  CALLS: "#3fb950", IMPORTS_FROM: "#f0883e",
  INHERITS: "#d2a8ff", CONTAINS: "rgba(139,148,158,0.15)"
};
var EDGE_CFG = {
  CROSS_COMMUNITY: { dash: null, width: 2, opacity: 0.6, marker: "" },
  DEPENDS_ON:      { dash: "6,3", width: 1.5, opacity: 0.5, marker: "" },
  CONTAINS:        { dash: null, width: 1, opacity: 0.08, marker: "" },
  CALLS:           { dash: null, width: 1.5, opacity: 0.7, marker: "url(#arrow-calls)" },
  IMPORTS_FROM:    { dash: "6,3", width: 1.5, opacity: 0.65, marker: "url(#arrow-imports)" },
  INHERITS:        { dash: "3,4", width: 2, opacity: 0.7, marker: "url(#arrow-inherits)" },
};
function eStyle(d) { return EDGE_CFG[d.kind] || { dash: null, width: 1, opacity: 0.3, marker: "" }; }
function eColor(d) { return EDGE_COLOR[d.kind] || "#484f58"; }

/* --- Legend setup --- */
var legendNodes = document.getElementById("legend-nodes");
var legendEdges = document.getElementById("legend-edges");
function buildLegend(nodeKinds, edgeKinds) {
  legendNodes.textContent = "";
  legendEdges.textContent = "";
  nodeKinds.forEach(function(k) {
    var div = document.createElement("div");
    div.className = "legend-item";
    var circle = document.createElement("span");
    circle.className = "legend-circle";
    circle.style.background = KIND_COLOR[k] || "#8b949e";
    div.appendChild(circle);
    div.appendChild(document.createTextNode(" " + k));
    legendNodes.appendChild(div);
  });
  edgeKinds.forEach(function(k) {
    var div = document.createElement("div");
    div.className = "legend-item";
    var cls = k === "CROSS_COMMUNITY" ? "l-cross" : k === "DEPENDS_ON" ? "l-dep" : k === "CALLS" ? "l-calls" : k === "IMPORTS_FROM" ? "l-imports" : k === "INHERITS" ? "l-inherits" : "l-contains";
    var line = document.createElement("span");
    line.className = "legend-line " + cls;
    div.appendChild(line);
    var label = k.replace(/_/g, " ").replace(/\b\w/g, function(c) { return c.toUpperCase(); });
    div.appendChild(document.createTextNode(" " + label));
    legendEdges.appendChild(div);
  });
}

/* --- Stats bar --- */
var statsBar = document.getElementById("stats-bar");
var stats = graphData.stats;
function addStat(label, value) {
  var div = document.createElement("div");
  div.className = "stat-item";
  var lbl = document.createElement("span");
  lbl.className = "tt-label";
  lbl.textContent = label;
  var val = document.createElement("span");
  val.className = "stat-value";
  val.textContent = String(value);
  div.appendChild(lbl);
  div.appendChild(document.createTextNode(" "));
  div.appendChild(val);
  statsBar.appendChild(div);
}
var langList = (stats.languages || []).join(", ") || "n/a";
statsBar.textContent = "";
addStat("Nodes", stats.total_nodes);
addStat("Edges", stats.total_edges);
addStat("Files", stats.files_count);
addStat("Languages", langList);
addStat("Mode", dataMode);

/* --- Filter info --- */
var filterInfo = document.getElementById("filter-info");
if (dataMode === "community") {
  filterInfo.textContent = "Showing communities. Double-click to drill down.";
} else if (dataMode === "file") {
  filterInfo.textContent = "Showing file-level aggregation.";
} else {
  filterInfo.textContent = "Showing all nodes.";
}

/* --- Tooltip --- */
var tooltip = document.getElementById("tooltip");
function showTooltip(ev, d) {
  var bg = KIND_COLOR[d.kind] || "#555";
  tooltip.textContent = "";
  var nameSpan = document.createElement("span");
  nameSpan.className = "tt-name";
  nameSpan.textContent = d.name || d.label;
  tooltip.appendChild(nameSpan);
  var kindSpan = document.createElement("span");
  kindSpan.className = "tt-kind";
  kindSpan.style.background = bg;
  kindSpan.style.color = "#0d1117";
  kindSpan.textContent = d.kind;
  tooltip.appendChild(kindSpan);
  function addRow(label, value) {
    var row = document.createElement("div");
    row.className = "tt-row";
    var lbl = document.createElement("span");
    lbl.className = "tt-label";
    lbl.textContent = label + ": ";
    row.appendChild(lbl);
    row.appendChild(document.createTextNode(String(value)));
    tooltip.appendChild(row);
  }
  if (d.member_count != null) addRow("Members", d.member_count);
  if (d.symbol_count != null) addRow("Symbols", d.symbol_count);
  if (d.description) addRow("Description", d.description);
  if (d.language) addRow("Language", d.language);
  if (d.file_path) {
    var relFile = d.file_path.split("/").slice(-3).join("/");
    if (relFile) {
      var fileRow = document.createElement("div");
      fileRow.className = "tt-row tt-file";
      fileRow.textContent = relFile;
      tooltip.appendChild(fileRow);
    }
  }
  if (d.line_start != null) addRow("Lines", d.line_start + " \u2013 " + (d.line_end || d.line_start));
  if (d.params) addRow("Params", d.params);
  if (d.return_type) addRow("Returns", d.return_type);
  if (d.weight != null) addRow("Weight", d.weight);
  tooltip.classList.add("visible");
  moveTooltip(ev);
}
function moveTooltip(ev) {
  var p = 14;
  var x = ev.pageX + p, y = ev.pageY + p;
  var r = tooltip.getBoundingClientRect();
  if (x + r.width > innerWidth - p) x = ev.pageX - r.width - p;
  if (y + r.height > innerHeight - p) y = ev.pageY - r.height - p;
  tooltip.style.left = x + "px"; tooltip.style.top = y + "px";
}
function hideTooltip() { tooltip.classList.remove("visible"); }

/* --- SVG setup --- */
var W = innerWidth, H = innerHeight;
var svg = d3.select("svg").attr("viewBox", [0, 0, W, H]);
var gRoot = svg.append("g");
var currentTransform = d3.zoomIdentity;
var zoomBehavior = d3.zoom()
  .scaleExtent([0.05, 8])
  .on("zoom", function(ev) { currentTransform = ev.transform; gRoot.attr("transform", ev.transform); updateLabelVisibility(); });
svg.call(zoomBehavior);
var defs = svg.append("defs");
var glow = defs.append("filter").attr("id","glow").attr("x","-50%").attr("y","-50%").attr("width","200%").attr("height","200%");
glow.append("feGaussianBlur").attr("stdDeviation","3").attr("result","blur");
glow.append("feComposite").attr("in","SourceGraphic").attr("in2","blur").attr("operator","over");
[{id:"arrow-calls",color:"#3fb950"},{id:"arrow-imports",color:"#f0883e"},{id:"arrow-inherits",color:"#d2a8ff"}].forEach(function(mk) {
  defs.append("marker").attr("id", mk.id)
    .attr("viewBox","0 -5 10 10").attr("refX",28).attr("refY",0)
    .attr("markerWidth",8).attr("markerHeight",8).attr("orient","auto")
    .append("path").attr("d","M0,-4L10,0L0,4Z").attr("fill",mk.color);
});

var linkGroup  = gRoot.append("g").attr("class","links");
var nodeGroup  = gRoot.append("g").attr("class","nodes");
var labelGroup = gRoot.append("g").attr("class","labels");
var linkSel = null, labelSel = null;
var showLabels = true;
var simulation = null;
var currentNodes = [];
var currentEdges = [];
var isDrilledDown = false;

function nodeRadius(d) {
  if (d.kind === "Community") return Math.max(12, Math.min(40, 8 + Math.sqrt(d.member_count || 1) * 3));
  if (d.kind === "File") {
    if (d.symbol_count != null) return Math.max(8, Math.min(30, 6 + Math.sqrt(d.symbol_count || 1) * 2));
    return 18;
  }
  return { Class: 12, Function: 6, Test: 6, Type: 5 }[d.kind] || 6;
}
function nodeColor(d) {
  if (d.kind === "Community" && d.community_id != null) return communityColorScale(d.community_id);
  return KIND_COLOR[d.kind] || "#8b949e";
}

function renderGraph(nodesData, edgesData, drillDown) {
  isDrilledDown = !!drillDown;
  currentNodes = nodesData.map(function(d) {
    var o = Object.assign({}, d);
    o._id = d.qualified_name;
    o.label = d.name;
    return o;
  });
  currentEdges = edgesData.map(function(d) {
    var o = Object.assign({}, d);
    o._source = d.source;
    o._target = d.target;
    return o;
  });

  /* Update legend based on current data */
  var nodeKindSet = new Set(); var edgeKindSet = new Set();
  currentNodes.forEach(function(n) { nodeKindSet.add(n.kind); });
  currentEdges.forEach(function(e) { edgeKindSet.add(e.kind); });
  buildLegend(Array.from(nodeKindSet), Array.from(edgeKindSet));

  /* Back button */
  document.getElementById("btn-back").style.display = isDrilledDown ? "block" : "none";

  /* Clear existing */
  linkGroup.selectAll("*").remove();
  nodeGroup.selectAll("*").remove();
  labelGroup.selectAll("*").remove();
  if (simulation) simulation.stop();

  var N = currentNodes.length;
  var isLarge = N > 300;
  var nodeById = new Map(currentNodes.map(function(n) { return [n.qualified_name, n]; }));

  simulation = d3.forceSimulation(currentNodes)
    .force("link", d3.forceLink(currentEdges).id(function(d) { return d.qualified_name; })
      .distance(function(d) {
        if (d.kind === "CONTAINS") return 35;
        if (d.kind === "CROSS_COMMUNITY" || d.kind === "DEPENDS_ON") return Math.max(100, 200 - (d.weight || 1) * 5);
        return isLarge ? 80 : 120;
      })
      .strength(function(d) {
        if (d.kind === "CONTAINS") return 1.5;
        if (d.kind === "CROSS_COMMUNITY" || d.kind === "DEPENDS_ON") return 0.1 + Math.min(0.5, (d.weight || 1) * 0.02);
        return 0.15;
      }))
    .force("charge", d3.forceManyBody().strength(function(d) {
      if (d.kind === "Community") return -400 - (d.member_count || 0) * 2;
      return d.kind === "File" ? (isLarge ? -200 : -400) : (isLarge ? -60 : -120);
    }).theta(0.85).distanceMax(600))
    .force("collide", d3.forceCollide().radius(function(d) { return nodeRadius(d) + 6; }))
    .force("center", d3.forceCenter(W / 2, H / 2))
    .force("x", d3.forceX(W / 2).strength(0.03))
    .force("y", d3.forceY(H / 2).strength(0.03))
    .alphaDecay(isLarge ? 0.04 : 0.025)
    .velocityDecay(0.4);

  /* Draw edges */
  linkSel = linkGroup.selectAll("line").data(currentEdges, function(d) { return d._source + "->" + d._target + ":" + d.kind; });
  linkSel.exit().remove();
  var linkEnter = linkSel.enter().append("line");
  linkSel = linkEnter.merge(linkSel);
  linkSel
    .attr("stroke", function(d) { return eColor(d); })
    .attr("stroke-width", function(d) {
      if (d.weight && (d.kind === "CROSS_COMMUNITY" || d.kind === "DEPENDS_ON")) return Math.max(1, Math.min(6, Math.sqrt(d.weight)));
      return eStyle(d).width;
    })
    .attr("stroke-dasharray", function(d) { return eStyle(d).dash; })
    .attr("opacity", function(d) { return eStyle(d).opacity; })
    .attr("marker-end", function(d) { return eStyle(d).marker; });

  /* Draw nodes */
  var nodeSel = nodeGroup.selectAll("g.node-g").data(currentNodes, function(d) { return d.qualified_name; });
  nodeSel.exit().remove();
  var enter = nodeSel.enter().append("g").attr("class", "node-g");
  enter.append("circle")
    .attr("class", "glow-ring")
    .attr("r", function(d) { return nodeRadius(d) + 5; })
    .attr("fill", "none")
    .attr("stroke", function(d) { return nodeColor(d); })
    .attr("stroke-width", 1.5).attr("opacity", 0.3).attr("filter", "url(#glow)");
  enter.append("circle").attr("class", "node-circle")
    .attr("r", function(d) { return nodeRadius(d); })
    .attr("fill", function(d) { return nodeColor(d); })
    .attr("stroke", "rgba(255,255,255,0.15)")
    .attr("stroke-width", 2)
    .attr("cursor", "pointer");
  enter
    .on("mouseover", function(ev, d) { highlightConnected(d, true); showTooltip(ev, d); })
    .on("mousemove", function(ev) { moveTooltip(ev); })
    .on("mouseout", function(ev, d) { highlightConnected(d, false); hideTooltip(); })
    .on("click", function(ev, d) { ev.stopPropagation(); showDetailPanel(d, nodeById); })
    .on("dblclick", function(ev, d) {
      ev.stopPropagation();
      if (d.kind === "Community" && dataMode === "community") drillIntoCommunity(d);
    })
    .call(d3.drag()
      .on("start", function(ev, d) { if (!ev.active) simulation.alphaTarget(0.1).restart(); d.fx = d.x; d.fy = d.y; })
      .on("drag", function(ev, d) { d.fx = ev.x; d.fy = ev.y; })
      .on("end", function(ev, d) { if (!ev.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; })
    );
  nodeSel = enter.merge(nodeSel);

  /* Draw labels */
  labelSel = labelGroup.selectAll("text.node-label").data(currentNodes, function(d) { return d.qualified_name; });
  labelSel.exit().remove();
  var lEnter = labelSel.enter().append("text").attr("class", "node-label")
    .attr("text-anchor", "start").attr("dy", "0.35em")
    .text(function(d) { return d.label; })
    .attr("fill", function(d) { return d.kind === "Community" ? "#e6edf3" : d.kind === "File" ? "#e6edf3" : "#8b949e"; })
    .attr("font-size", function(d) { return d.kind === "Community" ? "13px" : d.kind === "File" ? "12px" : "10px"; })
    .attr("font-weight", function(d) { return (d.kind === "Community" || d.kind === "File") ? 700 : 400; });
  labelSel = lEnter.merge(labelSel);

  simulation.on("tick", function() {
    linkSel
      .attr("x1", function(d) { return d.source.x; }).attr("y1", function(d) { return d.source.y; })
      .attr("x2", function(d) { return d.target.x; }).attr("y2", function(d) { return d.target.y; });
    nodeGroup.selectAll("g.node-g").attr("transform", function(d) { return "translate(" + d.x + "," + d.y + ")"; });
    labelSel
      .attr("x", function(d) { return d.x + nodeRadius(d) + 5; })
      .attr("y", function(d) { return d.y; });
  });

  simulation.on("end", fitGraph);
  updateLabelVisibility();
}

function updateLabelVisibility() {
  if (!labelSel) return;
  var s = currentTransform.k;
  labelSel.attr("display", function(d) {
    if (!showLabels) return "none";
    if (d.kind === "Community" || d.kind === "File") return null;
    if (d.kind === "Class") return s > 0.5 ? null : "none";
    return s > 1.0 ? null : "none";
  });
}

function highlightConnected(d, on) {
  if (on) {
    var connected = new Set([d.qualified_name]);
    currentEdges.forEach(function(e) {
      var s = typeof e.source === "object" ? e.source.qualified_name : e._source;
      var t = typeof e.target === "object" ? e.target.qualified_name : e._target;
      if (s === d.qualified_name) connected.add(t);
      if (t === d.qualified_name) connected.add(s);
    });
    nodeGroup.selectAll("g.node-g").select(".node-circle")
      .transition().duration(150).attr("opacity", function(n) { return connected.has(n.qualified_name) ? 1 : 0.15; });
    if (linkSel) linkSel.transition().duration(150)
      .attr("opacity", function(e) {
        var s = typeof e.source === "object" ? e.source.qualified_name : e._source;
        var t = typeof e.target === "object" ? e.target.qualified_name : e._target;
        return (s === d.qualified_name || t === d.qualified_name) ? 0.9 : 0.03;
      })
      .attr("stroke-width", function(e) {
        var s = typeof e.source === "object" ? e.source.qualified_name : e._source;
        var t = typeof e.target === "object" ? e.target.qualified_name : e._target;
        var base = e.weight ? Math.max(1, Math.min(6, Math.sqrt(e.weight))) : eStyle(e).width;
        return (s === d.qualified_name || t === d.qualified_name) ? base + 1.5 : base;
      });
    if (labelSel) labelSel.transition().duration(150).attr("opacity", function(n) { return connected.has(n.qualified_name) ? 1 : 0.1; });
  } else {
    nodeGroup.selectAll("g.node-g").select(".node-circle").transition().duration(300).attr("opacity", 1);
    if (linkSel) linkSel.transition().duration(300)
      .attr("opacity", function(e) { return eStyle(e).opacity; })
      .attr("stroke-width", function(e) {
        if (e.weight && (e.kind === "CROSS_COMMUNITY" || e.kind === "DEPENDS_ON")) return Math.max(1, Math.min(6, Math.sqrt(e.weight)));
        return eStyle(e).width;
      });
    if (labelSel) labelSel.transition().duration(300).attr("opacity", 1);
    updateLabelVisibility();
  }
}

function fitGraph() {
  var b = gRoot.node().getBBox();
  if (b.width === 0 || b.height === 0) return;
  var pad = 0.1;
  var fw = b.width * (1 + 2 * pad), fh = b.height * (1 + 2 * pad);
  var s = Math.min(W / fw, H / fh, 2.5);
  var tx = W / 2 - (b.x + b.width / 2) * s, ty = H / 2 - (b.y + b.height / 2) * s;
  svg.transition().duration(600).call(zoomBehavior.transform, d3.zoomIdentity.translate(tx, ty).scale(s));
}

function zoomToNode(qn) {
  var nd = currentNodes.find(function(n) { return n.qualified_name === qn; });
  if (!nd || nd.x == null) return;
  var s = 2.0;
  var tx = W / 2 - nd.x * s, ty = H / 2 - nd.y * s;
  svg.transition().duration(600).call(zoomBehavior.transform, d3.zoomIdentity.translate(tx, ty).scale(s));
}

/* --- Detail panel --- */
var detailPanel = document.getElementById("detail-panel");
var dpContent = document.getElementById("dp-content");
document.querySelector("#detail-panel .dp-close").addEventListener("click", function() {
  detailPanel.classList.remove("visible");
});
svg.on("click", function() { detailPanel.classList.remove("visible"); });
function showDetailPanel(d, nodeById) {
  dpContent.textContent = "";
  var h2 = document.createElement("h2");
  h2.textContent = d.label || d.name;
  dpContent.appendChild(h2);
  var kindSpan = document.createElement("span");
  kindSpan.className = "tt-kind";
  kindSpan.style.background = KIND_COLOR[d.kind] || "#555";
  kindSpan.style.color = "#0d1117";
  kindSpan.textContent = d.kind;
  dpContent.appendChild(kindSpan);
  function addMeta(label, value) {
    var div = document.createElement("div");
    div.className = "dp-meta";
    var lbl = document.createElement("span");
    lbl.className = "tt-label";
    lbl.textContent = label + ": ";
    div.appendChild(lbl);
    var val = document.createElement("span");
    val.textContent = String(value);
    div.appendChild(val);
    dpContent.appendChild(div);
  }
  if (d.member_count != null) addMeta("Members", d.member_count);
  if (d.symbol_count != null) addMeta("Symbols", d.symbol_count);
  if (d.description) {
    var desc = document.createElement("div");
    desc.className = "dp-meta";
    desc.style.marginTop = "6px";
    desc.textContent = d.description;
    dpContent.appendChild(desc);
  }
  if (d.language) addMeta("Language", d.language);
  if (d.file_path) {
    var relFile = d.file_path.split("/").slice(-3).join("/");
    if (relFile) {
      var fDiv = document.createElement("div");
      fDiv.className = "dp-meta";
      fDiv.style.marginTop = "8px";
      fDiv.textContent = relFile + (d.line_start != null ? ":" + d.line_start : "");
      dpContent.appendChild(fDiv);
    }
  }
  if (d.params) addMeta("Params", d.params);
  if (d.return_type) addMeta("Returns", d.return_type);
  /* Show connected super-nodes */
  var neighbors = [];
  currentEdges.forEach(function(e) {
    var s = typeof e.source === "object" ? e.source.qualified_name : e._source;
    var t = typeof e.target === "object" ? e.target.qualified_name : e._target;
    if (s === d.qualified_name && nodeById.has(t)) neighbors.push({ node: nodeById.get(t), weight: e.weight || 1 });
    if (t === d.qualified_name && nodeById.has(s)) neighbors.push({ node: nodeById.get(s), weight: e.weight || 1 });
  });
  if (neighbors.length) {
    var section = document.createElement("div");
    section.className = "dp-section";
    var h4 = document.createElement("h4");
    h4.textContent = "Connected (" + neighbors.length + ")";
    section.appendChild(h4);
    var ul = document.createElement("ul");
    ul.className = "dp-list";
    neighbors.sort(function(a, b) { return b.weight - a.weight; });
    neighbors.slice(0, 20).forEach(function(nb) {
      var li = document.createElement("li");
      li.dataset.qn = nb.node.qualified_name;
      li.textContent = (nb.node.label || nb.node.name) + " (" + nb.weight + ")";
      li.addEventListener("click", function() { zoomToNode(nb.node.qualified_name); });
      ul.appendChild(li);
    });
    section.appendChild(ul);
    dpContent.appendChild(section);
  }
  if (d.kind === "Community" && dataMode === "community") {
    var drillSection = document.createElement("div");
    drillSection.className = "dp-section";
    drillSection.style.marginTop = "12px";
    var drillHint = document.createElement("em");
    drillHint.style.color = "#58a6ff";
    drillHint.textContent = "Double-click node to drill down";
    drillSection.appendChild(drillHint);
    dpContent.appendChild(drillSection);
  }
  detailPanel.classList.add("visible");
}

/* --- Drill-down (community mode) --- */
function drillIntoCommunity(d) {
  var cid = String(d.community_id != null ? d.community_id : d.id);
  var detail = communityDetails[cid];
  if (!detail || !detail.nodes || detail.nodes.length === 0) return;
  filterInfo.textContent = "Viewing community: " + (d.name || d.label) + ". Click Back to return.";
  renderGraph(detail.nodes, detail.edges, true);
}

/* --- Back button --- */
document.getElementById("btn-back").addEventListener("click", function() {
  filterInfo.textContent = dataMode === "community"
    ? "Showing communities. Double-click to drill down."
    : "Showing file-level aggregation.";
  renderGraph(graphData.nodes, graphData.edges, false);
});

/* --- Controls --- */
document.getElementById("btn-fit").addEventListener("click", fitGraph);
document.getElementById("btn-labels").addEventListener("click", function() {
  showLabels = !showLabels;
  this.classList.toggle("active");
  this.setAttribute("aria-pressed", showLabels);
  updateLabelVisibility();
});

/* --- Search --- */
var searchInput = document.getElementById("search");
var searchResults = document.getElementById("search-results");
var searchTerm = "";
searchInput.addEventListener("input", function() {
  searchTerm = this.value.trim().toLowerCase();
  applySearchFilter();
  showSearchResults();
});
searchInput.addEventListener("focus", showSearchResults);
document.addEventListener("click", function(ev) {
  if (!searchResults.contains(ev.target) && ev.target !== searchInput) searchResults.style.display = "none";
});
function showSearchResults() {
  if (!searchTerm) { searchResults.style.display = "none"; return; }
  var matched = [];
  currentNodes.forEach(function(n) {
    var hay = ((n.label || "") + " " + (n.qualified_name || "") + " " + (n.name || "")).toLowerCase();
    if (hay.indexOf(searchTerm) !== -1) matched.push(n);
  });
  if (!matched.length) { searchResults.style.display = "none"; return; }
  searchResults.textContent = "";
  matched.slice(0, 15).forEach(function(n) {
    var bg = KIND_COLOR[n.kind] || "#555";
    var div = document.createElement("div");
    div.className = "sr-item";
    var kindSpan = document.createElement("span");
    kindSpan.className = "sr-kind";
    kindSpan.style.background = bg;
    kindSpan.style.color = "#0d1117";
    kindSpan.textContent = n.kind;
    div.appendChild(kindSpan);
    div.appendChild(document.createTextNode(" " + (n.label || n.name)));
    div.addEventListener("click", function() {
      zoomToNode(n.qualified_name);
      searchResults.style.display = "none";
    });
    searchResults.appendChild(div);
  });
  searchResults.style.display = "block";
}
function applySearchFilter() {
  if (!searchTerm) {
    nodeGroup.selectAll("g.node-g").select(".node-circle").attr("opacity", 1);
    if (labelSel) labelSel.attr("opacity", 1);
    if (linkSel) linkSel.attr("opacity", function(e) { return eStyle(e).opacity; });
    updateLabelVisibility();
    return;
  }
  var matched = new Set();
  currentNodes.forEach(function(n) {
    var hay = ((n.label || "") + " " + (n.qualified_name || "") + " " + (n.name || "")).toLowerCase();
    if (hay.indexOf(searchTerm) !== -1) matched.add(n.qualified_name);
  });
  nodeGroup.selectAll("g.node-g").select(".node-circle")
    .attr("opacity", function(d) { return matched.has(d.qualified_name) ? 1 : 0.08; });
  if (labelSel) labelSel
    .attr("opacity", function(d) { return matched.has(d.qualified_name) ? 1 : 0.05; })
    .attr("display", function(d) { return matched.has(d.qualified_name) ? null : "none"; });
  if (linkSel) linkSel.attr("opacity", function(e) {
    var s = typeof e.source === "object" ? e.source.qualified_name : e._source;
    var t = typeof e.target === "object" ? e.target.qualified_name : e._target;
    return (matched.has(s) || matched.has(t)) ? eStyle(e).opacity : 0.02;
  });
}

/* --- Initial render --- */
renderGraph(graphData.nodes, graphData.edges, false);
</script>
</body>
</html>
"""
