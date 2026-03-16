"""Interactive D3.js graph visualization for code knowledge graphs.

Exports graph data to JSON and generates a self-contained HTML file with
a force-directed D3.js visualization. Dark theme, zoomable, draggable,
with collapsible file clusters, tooltips, legend, and stats bar.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .graph import GraphStore, edge_to_dict, node_to_dict


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

    Returns ``{"nodes": [...], "edges": [...], "stats": {...}}``.
    """
    nodes = []
    seen_qn: set[str] = set()
    for file_path in store.get_all_files():
        for gnode in store.get_nodes_by_file(file_path):
            if gnode.qualified_name in seen_qn:
                continue
            seen_qn.add(gnode.qualified_name)
            d = node_to_dict(gnode)
            d["params"] = gnode.params
            d["return_type"] = gnode.return_type
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

    return {
        "nodes": nodes,
        "edges": edges,
        "stats": asdict(stats),
    }


def generate_html(store: GraphStore, output_path: str | Path) -> Path:
    """Generate a self-contained interactive HTML visualization.

    Writes the HTML file to *output_path* and returns the resolved Path.
    """
    output_path = Path(output_path)
    data = export_graph_data(store)
    data_json = json.dumps(data, default=str)
    html = _HTML_TEMPLATE.replace("__GRAPH_DATA__", data_json)
    output_path.write_text(html, encoding="utf-8")
    return output_path


# ---------------------------------------------------------------------------
# Full D3.js interactive HTML template
# ---------------------------------------------------------------------------

# Template lives in this file for zero-dependency packaging (no external files
# to locate at runtime).  The ``# noqa: E501`` on the module is set via
# pyproject.toml per-file-ignores for this reason.

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Code Review Graph</title>
<script src="https://d3js.org/d3.v7.min.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { width: 100%; height: 100%; overflow: hidden; }
  body {
    background: #0d1117;
    color: #c9d1d9;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    font-size: 13px;
  }
  svg { display: block; width: 100%; height: 100%; }

  #legend {
    position: absolute; top: 16px; left: 16px;
    background: rgba(22, 27, 34, 0.95); border: 1px solid #30363d;
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
  .legend-line {
    width: 24px; height: 0; flex-shrink: 0;
    border-top-width: 2px;
  }
  .l-calls    { border-top: 2px solid #3fb950; }
  .l-imports  { border-top: 2px dashed #f0883e; }
  .l-inherits { border-top: 2.5px dotted #d2a8ff; }
  .l-contains { border-top: 1.5px solid rgba(139,148,158,0.3); }

  #stats-bar {
    position: absolute; bottom: 0; left: 0; right: 0;
    background: rgba(13, 17, 23, 0.95); border-top: 1px solid #21262d;
    padding: 8px 24px; display: flex; gap: 32px; justify-content: center;
    font-size: 12px; color: #8b949e; backdrop-filter: blur(12px);
  }
  .stat-item { display: flex; gap: 6px; align-items: center; }
  .stat-value { color: #e6edf3; font-weight: 600; }

  #tooltip {
    position: absolute; pointer-events: none;
    background: rgba(22, 27, 34, 0.97); color: #c9d1d9;
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
    display: flex; gap: 8px; z-index: 10;
  }
  #controls button {
    background: rgba(22, 27, 34, 0.95); color: #c9d1d9;
    border: 1px solid #30363d; border-radius: 8px;
    padding: 8px 14px; font-size: 12px; cursor: pointer;
    backdrop-filter: blur(12px); transition: all 0.15s;
  }
  #controls button:hover { background: #30363d; border-color: #8b949e; }
  #controls button.active { background: #1f6feb; border-color: #58a6ff; color: #fff; }
  #search {
    background: rgba(22, 27, 34, 0.95); color: #c9d1d9;
    border: 1px solid #30363d; border-radius: 8px;
    padding: 8px 14px; font-size: 12px; width: 220px;
    outline: none; backdrop-filter: blur(12px);
  }
  #search:focus { border-color: #58a6ff; }
  #search::placeholder { color: #484f58; }

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

<div id="controls">
  <input id="search" type="text" placeholder="Search nodes\u2026" autocomplete="off" spellcheck="false" aria-label="Search graph nodes by name">
  <button id="btn-fit" title="Fit to screen" aria-label="Fit graph to screen">Fit</button>
  <button id="btn-labels" title="Toggle labels" class="active" aria-label="Toggle node labels" aria-pressed="true">Labels</button>
</div>

<div id="stats-bar" role="status" aria-label="Graph statistics"></div>
<div id="tooltip"></div>
<svg role="img" aria-label="Interactive code knowledge graph visualization. Use search to find nodes, click files to expand."></svg>

<script>
const graphData = __GRAPH_DATA__;

// -- Config --
const KIND_COLOR  = { File:"#58a6ff", Class:"#f0883e", Function:"#3fb950", Test:"#d2a8ff", Type:"#8b949e" };
const KIND_RADIUS = { File:18, Class:12, Function:6, Test:6, Type:5 };
const EDGE_COLOR  = { CALLS:"#3fb950", IMPORTS_FROM:"#f0883e", INHERITS:"#d2a8ff", CONTAINS:"rgba(139,148,158,0.15)" };

// -- Display name: short, clean labels --
function displayName(d) {
  if (d.kind === "File") {
    const fp = d.file_path || d.qualified_name || d.name;
    const parts = fp.replace(/\\/g, "/").split("/");
    const fname = parts.pop();
    const parent = parts.pop() || "";
    return parent ? parent + "/" + fname : fname;
  }
  return d.name;
}

// -- Prepare data --
const nodes = graphData.nodes.map(d => ({...d, _id: d.qualified_name, label: displayName(d)}));
const edges = graphData.edges.map(d => ({...d, _source: d.source, _target: d.target}));
const stats = graphData.stats;
const nodeById = new Map(nodes.map(n => [n.qualified_name, n]));

// Edge kind toggle
const hiddenEdgeKinds = new Set();

// Containment hierarchy
const collapsedFiles = new Set();
const containsChildren = new Map();
const childToParent = new Map();
edges.forEach(e => {
  if (e.kind === "CONTAINS") {
    if (!containsChildren.has(e._source)) containsChildren.set(e._source, new Set());
    containsChildren.get(e._source).add(e._target);
    childToParent.set(e._target, e._source);
  }
});

function allDescendants(qn) {
  const result = new Set();
  const children = containsChildren.get(qn);
  if (!children) return result;
  for (const c of children) { result.add(c); for (const d of allDescendants(c)) result.add(d); }
  return result;
}

// -- Stats bar --
const statsBar = document.getElementById("stats-bar");
const langList = (stats.languages || []).join(", ") || "n/a";
const si = (l,v) => `<div class="stat-item"><span class="tt-label">${l}</span> <span class="stat-value">${v}</span></div>`;
statsBar.innerHTML = si("Nodes", stats.total_nodes) + si("Edges", stats.total_edges)
  + si("Files", stats.files_count) + si("Languages", langList);

// -- Tooltip --
const tooltip = document.getElementById("tooltip");
function escH(s) { return !s ? "" : s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }

function showTooltip(ev, d) {
  const bg = KIND_COLOR[d.kind] || "#555";
  const relFile = d.file_path ? d.file_path.split("/").slice(-3).join("/") : "";
  let h = `<span class="tt-name">${escH(d.label)}</span>`;
  h += `<span class="tt-kind" style="background:${bg};color:#0d1117">${d.kind}</span>`;
  if (relFile) h += `<div class="tt-row tt-file">${escH(relFile)}</div>`;
  if (d.line_start != null) h += `<div class="tt-row"><span class="tt-label">Lines: </span>${d.line_start} \u2013 ${d.line_end || d.line_start}</div>`;
  if (d.params) h += `<div class="tt-row"><span class="tt-label">Params: </span>${escH(d.params)}</div>`;
  if (d.return_type) h += `<div class="tt-row"><span class="tt-label">Returns: </span>${escH(d.return_type)}</div>`;
  tooltip.innerHTML = h;
  tooltip.classList.add("visible");
  moveTooltip(ev);
}
function moveTooltip(ev) {
  const p = 14;
  let x = ev.pageX + p, y = ev.pageY + p;
  const r = tooltip.getBoundingClientRect();
  if (x + r.width > innerWidth - p) x = ev.pageX - r.width - p;
  if (y + r.height > innerHeight - p) y = ev.pageY - r.height - p;
  tooltip.style.left = x + "px"; tooltip.style.top = y + "px";
}
function hideTooltip() { tooltip.classList.remove("visible"); }

// -- SVG setup --
const W = innerWidth, H = innerHeight;
const svg = d3.select("svg").attr("viewBox", [0, 0, W, H]);
const g = svg.append("g");
let currentTransform = d3.zoomIdentity;

const zoomBehavior = d3.zoom()
  .scaleExtent([0.05, 8])
  .on("zoom", ev => { currentTransform = ev.transform; g.attr("transform", ev.transform); updateLabelVisibility(); });
svg.call(zoomBehavior);

// Arrow markers — colored per edge type
const defs = svg.append("defs");

// Glow filter for file nodes
const glow = defs.append("filter").attr("id","glow").attr("x","-50%").attr("y","-50%").attr("width","200%").attr("height","200%");
glow.append("feGaussianBlur").attr("stdDeviation","3").attr("result","blur");
glow.append("feComposite").attr("in","SourceGraphic").attr("in2","blur").attr("operator","over");

[{id:"arrow-calls",color:"#3fb950"},{id:"arrow-imports",color:"#f0883e"},{id:"arrow-inherits",color:"#d2a8ff"}].forEach(mk => {
  defs.append("marker").attr("id", mk.id)
    .attr("viewBox","0 -5 10 10").attr("refX",28).attr("refY",0)
    .attr("markerWidth",8).attr("markerHeight",8).attr("orient","auto")
    .append("path").attr("d","M0,-4L10,0L0,4Z").attr("fill",mk.color);
});

// -- Scale-aware simulation --
const N = nodes.length;
const isLarge = N > 300;
const chargeFile = isLarge ? -200 : -400;
const chargeOther = isLarge ? -60 : -120;
const linkDist = isLarge ? 80 : 120;
const alphaDecay = isLarge ? 0.04 : 0.025;

const simulation = d3.forceSimulation(nodes)
  .force("link", d3.forceLink(edges).id(d => d.qualified_name)
    .distance(d => d.kind === "CONTAINS" ? 35 : linkDist)
    .strength(d => d.kind === "CONTAINS" ? 1.5 : 0.15))
  .force("charge", d3.forceManyBody().strength(d => d.kind === "File" ? chargeFile : chargeOther).theta(0.85).distanceMax(600))
  .force("collide", d3.forceCollide().radius(d => (KIND_RADIUS[d.kind] || 6) + 4))
  .force("center", d3.forceCenter(W / 2, H / 2))
  .force("x", d3.forceX(W / 2).strength(0.03))
  .force("y", d3.forceY(H / 2).strength(0.03))
  .alphaDecay(alphaDecay)
  .velocityDecay(0.4);

// -- Edge styles --
const EDGE_CFG = {
  CONTAINS:     { dash:null,   width:1,   opacity:0.08, marker:"" },
  CALLS:        { dash:null,   width:1.5, opacity:0.7,  marker:"url(#arrow-calls)" },
  IMPORTS_FROM: { dash:"6,3",  width:1.5, opacity:0.65, marker:"url(#arrow-imports)" },
  INHERITS:     { dash:"3,4",  width:2,   opacity:0.7,  marker:"url(#arrow-inherits)" },
};

function eStyle(d) { return EDGE_CFG[d.kind] || {dash:null,width:1,opacity:0.3,marker:""}; }
function eColor(d) { return EDGE_COLOR[d.kind] || "#484f58"; }

// -- Draw layers --
const linkGroup  = g.append("g").attr("class","links");
const nodeGroup  = g.append("g").attr("class","nodes");
const labelGroup = g.append("g").attr("class","labels");

let linkSel, labelSel;
let showLabels = true;

function updateLinks() {
  const vis = new Set(nodes.filter(n => !n._hidden).map(n => n.qualified_name));
  const visEdges = edges.filter(e => {
    if (hiddenEdgeKinds.has(e.kind)) return false;
    const s = typeof e.source === "object" ? e.source.qualified_name : e._source;
    const t = typeof e.target === "object" ? e.target.qualified_name : e._target;
    return vis.has(s) && vis.has(t);
  });
  linkSel = linkGroup.selectAll("line").data(visEdges, d => d._source+"->"+d._target+":"+d.kind);
  linkSel.exit().remove();
  const enter = linkSel.enter().append("line");
  linkSel = enter.merge(linkSel);
  linkSel
    .attr("stroke", d => eColor(d))
    .attr("stroke-width", d => eStyle(d).width)
    .attr("stroke-dasharray", d => eStyle(d).dash)
    .attr("opacity", d => eStyle(d).opacity)
    .attr("marker-end", d => eStyle(d).marker);
}

function updateNodes() {
  const hiddenSet = new Set();
  for (const fqn of collapsedFiles) for (const c of allDescendants(fqn)) hiddenSet.add(c);
  nodes.forEach(n => { n._hidden = hiddenSet.has(n.qualified_name); });

  const vis = nodes.filter(n => !n._hidden);
  let nodeSel = nodeGroup.selectAll("g.node-g").data(vis, d => d.qualified_name);
  nodeSel.exit().remove();

  const enter = nodeSel.enter().append("g").attr("class","node-g");

  // File glow ring
  enter.filter(d => d.kind === "File").append("circle")
    .attr("class","glow-ring")
    .attr("r", d => KIND_RADIUS[d.kind] + 5)
    .attr("fill","none")
    .attr("stroke", d => KIND_COLOR[d.kind])
    .attr("stroke-width", 1.5)
    .attr("opacity", 0.3)
    .attr("filter","url(#glow)");

  // Main node circle
  enter.append("circle").attr("class","node-circle")
    .attr("r", d => KIND_RADIUS[d.kind] || 6)
    .attr("fill", d => KIND_COLOR[d.kind] || "#8b949e")
    .attr("stroke", d => d.kind === "File" ? "rgba(88,166,255,0.3)" : "rgba(255,255,255,0.08)")
    .attr("stroke-width", d => d.kind === "File" ? 2 : 1)
    .attr("cursor", d => d.kind === "File" ? "pointer" : "grab");

  enter
    .on("mouseover", (ev, d) => { highlightConnected(d, true); showTooltip(ev, d); })
    .on("mousemove", (ev) => moveTooltip(ev))
    .on("mouseout",  (ev, d) => { highlightConnected(d, false); hideTooltip(); })
    .on("click", (ev, d) => { if (d.kind === "File") { ev.stopPropagation(); toggleCollapse(d.qualified_name); } })
    .call(d3.drag().on("start", dragS).on("drag", dragD).on("end", dragE));

  nodeSel = enter.merge(nodeSel);

  // Labels
  labelSel = labelGroup.selectAll("text.node-label").data(vis, d => d.qualified_name);
  labelSel.exit().remove();
  const lEnter = labelSel.enter().append("text").attr("class","node-label")
    .attr("text-anchor","start").attr("dy","0.35em")
    .text(d => d.label)
    .attr("fill", d => {
      if (d.kind === "File") return "#e6edf3";
      if (d.kind === "Class") return "#f0883e";
      return "#8b949e";
    })
    .attr("font-size", d => d.kind === "File" ? "12px" : d.kind === "Class" ? "11px" : "10px")
    .attr("font-weight", d => d.kind === "File" ? 700 : d.kind === "Class" ? 600 : 400);
  labelSel = lEnter.merge(labelSel);

  updateLinks();
  updateLabelVisibility();
}

function updateLabelVisibility() {
  if (!labelSel) return;
  const s = currentTransform.k;
  labelSel.attr("display", d => {
    if (!showLabels) return "none";
    if (d.kind === "File") return null;              // always visible
    if (d.kind === "Class") return s > 0.5 ? null : "none";
    return s > 1.0 ? null : "none";                  // functions/tests visible when zoomed in
  });
}

// -- Highlight connected nodes on hover --
function highlightConnected(d, on) {
  if (on) {
    const connected = new Set([d.qualified_name]);
    edges.forEach(e => {
      const s = typeof e.source === "object" ? e.source.qualified_name : e._source;
      const t = typeof e.target === "object" ? e.target.qualified_name : e._target;
      if (s === d.qualified_name) connected.add(t);
      if (t === d.qualified_name) connected.add(s);
    });
    nodeGroup.selectAll("g.node-g").select(".node-circle")
      .transition().duration(150)
      .attr("opacity", n => connected.has(n.qualified_name) ? 1 : 0.15);
    linkSel.transition().duration(150)
      .attr("opacity", e => {
        const s = typeof e.source === "object" ? e.source.qualified_name : e._source;
        const t = typeof e.target === "object" ? e.target.qualified_name : e._target;
        return (s === d.qualified_name || t === d.qualified_name) ? 0.9 : 0.03;
      })
      .attr("stroke-width", e => {
        const s = typeof e.source === "object" ? e.source.qualified_name : e._source;
        const t = typeof e.target === "object" ? e.target.qualified_name : e._target;
        return (s === d.qualified_name || t === d.qualified_name) ? 2.5 : eStyle(e).width;
      });
    labelSel.transition().duration(150)
      .attr("opacity", n => connected.has(n.qualified_name) ? 1 : 0.1);
  } else {
    nodeGroup.selectAll("g.node-g").select(".node-circle")
      .transition().duration(300).attr("opacity", 1);
    linkSel.transition().duration(300)
      .attr("opacity", e => eStyle(e).opacity)
      .attr("stroke-width", e => eStyle(e).width);
    labelSel.transition().duration(300).attr("opacity", 1);
    updateLabelVisibility();
  }
}

// -- Collapse --
function toggleCollapse(qn) {
  collapsedFiles.has(qn) ? collapsedFiles.delete(qn) : collapsedFiles.add(qn);
  nodeGroup.selectAll("g.node-g").select(".glow-ring")
    .attr("stroke-dasharray", d => collapsedFiles.has(d.qualified_name) ? "4,3" : null)
    .attr("opacity", d => collapsedFiles.has(d.qualified_name) ? 0.6 : 0.3);
  updateNodes();
  simulation.alpha(0.3).restart();
}

// -- Drag --
function dragS(ev, d) { if (!ev.active) simulation.alphaTarget(0.1).restart(); d.fx = d.x; d.fy = d.y; }
function dragD(ev, d) { d.fx = ev.x; d.fy = ev.y; }
function dragE(ev, d) { if (!ev.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; }

// -- Tick --
simulation.on("tick", () => {
  if (linkSel) linkSel
    .attr("x1", d => d.source.x).attr("y1", d => d.source.y)
    .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
  nodeGroup.selectAll("g.node-g").attr("transform", d => `translate(${d.x},${d.y})`);
  if (labelSel) labelSel
    .attr("x", d => d.x + (KIND_RADIUS[d.kind] || 6) + 5)
    .attr("y", d => d.y);
});

// -- Start collapsed: only File nodes visible on load --
nodes.forEach(n => { if (n.kind === "File") collapsedFiles.add(n.qualified_name); });

// -- Initial render --
updateNodes();

// -- Auto-fit after stabilization --
function fitGraph() {
  const b = g.node().getBBox();
  if (b.width === 0 || b.height === 0) return;
  const pad = 0.1;
  const fw = b.width * (1 + 2*pad), fh = b.height * (1 + 2*pad);
  const s = Math.min(W / fw, H / fh, 2.5);
  const tx = W/2 - (b.x + b.width/2)*s, ty = H/2 - (b.y + b.height/2)*s;
  svg.transition().duration(600).call(zoomBehavior.transform, d3.zoomIdentity.translate(tx, ty).scale(s));
}
simulation.on("end", fitGraph);

// -- Controls --
document.getElementById("btn-fit").addEventListener("click", fitGraph);
document.getElementById("btn-labels").addEventListener("click", function() {
  showLabels = !showLabels;
  this.classList.toggle("active");
  this.setAttribute("aria-pressed", showLabels);
  updateLabelVisibility();
});

// -- Edge kind toggles via legend --
document.querySelectorAll(".legend-item[data-edge-kind]").forEach(el => {
  el.addEventListener("click", function() {
    const kind = this.dataset.edgeKind;
    if (hiddenEdgeKinds.has(kind)) { hiddenEdgeKinds.delete(kind); this.classList.remove("dimmed"); }
    else { hiddenEdgeKinds.add(kind); this.classList.add("dimmed"); }
    updateLinks();
  });
});

// -- Search --
let searchTerm = "";
document.getElementById("search").addEventListener("input", function() {
  searchTerm = this.value.trim().toLowerCase();
  applySearchFilter();
});
function applySearchFilter() {
  if (!searchTerm) {
    nodeGroup.selectAll("g.node-g").select(".node-circle").attr("opacity", 1);
    if (labelSel) labelSel.attr("opacity", 1);
    if (linkSel) linkSel.attr("opacity", e => eStyle(e).opacity);
    updateLabelVisibility();
    return;
  }
  const matched = new Set();
  nodes.forEach(n => {
    if (n._hidden) return;
    const hay = (n.label + " " + n.qualified_name).toLowerCase();
    if (hay.includes(searchTerm)) matched.add(n.qualified_name);
  });
  nodeGroup.selectAll("g.node-g").select(".node-circle")
    .attr("opacity", d => matched.has(d.qualified_name) ? 1 : 0.08);
  if (labelSel) labelSel
    .attr("opacity", d => matched.has(d.qualified_name) ? 1 : 0.05)
    .attr("display", d => matched.has(d.qualified_name) ? null : "none");
  if (linkSel) linkSel.attr("opacity", e => {
    const s = typeof e.source === "object" ? e.source.qualified_name : e._source;
    const t = typeof e.target === "object" ? e.target.qualified_name : e._target;
    return (matched.has(s) || matched.has(t)) ? eStyle(e).opacity : 0.02;
  });
}
</script>
</body>
</html>
"""
