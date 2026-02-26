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

    rows = store._conn.execute("SELECT * FROM edges").fetchall()  # noqa: SLF001
    edges = [edge_to_dict(store._row_to_edge(r)) for r in rows]  # noqa: SLF001

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

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Code Review Graph</title>
<script src="https://d3js.org/d3.v7.min.js"></script>
<style>
  /* ---- Reset & base ---- */
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { width: 100%; height: 100%; overflow: hidden; }
  body {
    background: #1a1a2e;
    color: #e0e0e0;
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    font-size: 13px;
  }

  /* ---- SVG canvas ---- */
  svg { display: block; width: 100%; height: 100%; }

  /* ---- Title ---- */
  #title {
    position: absolute; top: 16px; left: 50%; transform: translateX(-50%);
    font-size: 20px; font-weight: 600; letter-spacing: 0.5px;
    color: #c8d6e5; pointer-events: none; user-select: none;
    text-shadow: 0 2px 8px rgba(0,0,0,0.5);
  }

  /* ---- Legend ---- */
  #legend {
    position: absolute; top: 16px; left: 16px;
    background: rgba(30, 30, 50, 0.92); border: 1px solid #333;
    border-radius: 8px; padding: 14px 18px;
    font-size: 12px; line-height: 1.7;
    box-shadow: 0 4px 20px rgba(0,0,0,0.4);
    max-width: 200px;
  }
  #legend h3 {
    font-size: 13px; font-weight: 600; margin-bottom: 8px;
    color: #a0b0c0; text-transform: uppercase; letter-spacing: 0.8px;
  }
  .legend-section { margin-bottom: 10px; }
  .legend-section:last-child { margin-bottom: 0; }
  .legend-item {
    display: flex; align-items: center; gap: 8px; padding: 1px 0;
  }
  .legend-circle {
    width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0;
  }
  .legend-line {
    width: 28px; height: 0; flex-shrink: 0;
    border-top-width: 2px; border-top-color: #8899aa;
  }
  .legend-line.solid   { border-top-style: solid; }
  .legend-line.dashed  { border-top-style: dashed; }
  .legend-line.dotted  { border-top-style: dotted; border-top-width: 3px; }
  .legend-line.thick   { border-top-style: solid; border-top-width: 3px; opacity: 0.4; }

  /* ---- Stats bar ---- */
  #stats-bar {
    position: absolute; bottom: 0; left: 0; right: 0;
    background: rgba(22, 22, 38, 0.95); border-top: 1px solid #2a2a44;
    padding: 8px 24px; display: flex; gap: 32px; justify-content: center;
    font-size: 12px; color: #8899aa;
  }
  .stat-item { display: flex; gap: 6px; align-items: center; }
  .stat-value { color: #c8d6e5; font-weight: 600; }

  /* ---- Tooltip ---- */
  #tooltip {
    position: absolute; pointer-events: none;
    background: #2d2d44; color: #e0e0e0;
    border: 1px solid #444466; border-radius: 6px;
    padding: 10px 14px; font-size: 12px;
    box-shadow: 0 6px 24px rgba(0,0,0,0.5);
    max-width: 340px; line-height: 1.6;
    opacity: 0; transition: opacity 0.15s ease;
    z-index: 1000;
  }
  #tooltip.visible { opacity: 1; }
  .tt-name { font-weight: 600; font-size: 14px; color: #fff; }
  .tt-kind {
    display: inline-block; font-size: 10px; font-weight: 600;
    padding: 1px 6px; border-radius: 3px; margin-left: 6px;
    text-transform: uppercase; letter-spacing: 0.5px;
  }
  .tt-row { margin-top: 3px; }
  .tt-label { color: #8899aa; }

  /* ---- Edge arrows ---- */
  marker { overflow: visible; }

  /* ---- Node labels ---- */
  .node-label {
    font-size: 11px; fill: #b0b8c4; pointer-events: none;
    text-shadow: 0 1px 3px rgba(0,0,0,0.8), 0 0 6px rgba(0,0,0,0.6);
    user-select: none;
  }
  .node-label.file-label { font-size: 12px; font-weight: 600; fill: #d0d8e4; }
</style>
</head>
<body>

<div id="title">Code Review Graph</div>

<div id="legend">
  <h3>Nodes</h3>
  <div class="legend-section">
    <div class="legend-item">
      <span class="legend-circle" style="background:#4a90d9"></span> File
    </div>
    <div class="legend-item">
      <span class="legend-circle" style="background:#e8a838"></span> Class
    </div>
    <div class="legend-item">
      <span class="legend-circle" style="background:#50c878"></span> Function
    </div>
    <div class="legend-item">
      <span class="legend-circle" style="background:#9b59b6"></span> Test
    </div>
    <div class="legend-item">
      <span class="legend-circle" style="background:#95a5a6"></span> Type
    </div>
  </div>
  <h3>Edges</h3>
  <div class="legend-section">
    <div class="legend-item">
      <span class="legend-line thick"></span> Contains
    </div>
    <div class="legend-item">
      <span class="legend-line solid"></span> Calls
    </div>
    <div class="legend-item">
      <span class="legend-line dashed"></span> Imports
    </div>
    <div class="legend-item">
      <span class="legend-line dotted"></span> Inherits
    </div>
  </div>
</div>

<div id="stats-bar"></div>
<div id="tooltip"></div>
<svg></svg>

<script>
// -----------------------------------------------------------------------
// Data injected at build time
// -----------------------------------------------------------------------
const graphData = __GRAPH_DATA__;

// -----------------------------------------------------------------------
// Configuration
// -----------------------------------------------------------------------
const KIND_COLOR = {
  File:     "#4a90d9",
  Class:    "#e8a838",
  Function: "#50c878",
  Test:     "#9b59b6",
  Type:     "#95a5a6",
};

const KIND_RADIUS = {
  File:     20,
  Class:    14,
  Function: 8,
  Test:     8,
  Type:     8,
};

const KIND_BADGE_COLOR = {
  File:     "#3a78b8",
  Class:    "#c08020",
  Function: "#3da85e",
  Test:     "#7d3e99",
  Type:     "#778899",
};

// -----------------------------------------------------------------------
// Prepare data
// -----------------------------------------------------------------------
const nodes = graphData.nodes.map(d => ({...d, _id: d.qualified_name}));
const edges = graphData.edges.map(d => ({...d, _source: d.source, _target: d.target}));
const stats = graphData.stats;

// Build lookup
const nodeById = new Map(nodes.map(n => [n.qualified_name, n]));

// Track collapsed file nodes — collapsed children are hidden
const collapsedFiles = new Set();

// Build containment map: file_qn -> Set of child qualified_names
const containsChildren = new Map();   // parent qn -> Set(child qn)
const childToParent    = new Map();    // child qn -> parent qn (CONTAINS only)
edges.forEach(e => {
  if (e.kind === "CONTAINS") {
    if (!containsChildren.has(e._source)) containsChildren.set(e._source, new Set());
    containsChildren.get(e._source).add(e._target);
    childToParent.set(e._target, e._source);
  }
});

// Recursively collect all descendants via CONTAINS
function allDescendants(qn) {
  const result = new Set();
  const children = containsChildren.get(qn);
  if (!children) return result;
  for (const c of children) {
    result.add(c);
    for (const d of allDescendants(c)) result.add(d);
  }
  return result;
}

// -----------------------------------------------------------------------
// Stats bar
// -----------------------------------------------------------------------
const statsBar = document.getElementById("stats-bar");
const langList = (stats.languages || []).join(", ") || "n/a";
const si = (lbl, val) =>
  `<div class="stat-item"><span class="tt-label">${lbl}</span>`
  + ` <span class="stat-value">${val}</span></div>`;
statsBar.innerHTML =
  si("Nodes", stats.total_nodes)
  + si("Edges", stats.total_edges)
  + si("Files", stats.files_count)
  + si("Languages", langList);

// -----------------------------------------------------------------------
// Tooltip
// -----------------------------------------------------------------------
const tooltip = document.getElementById("tooltip");

function showTooltip(event, d) {
  const bg = KIND_BADGE_COLOR[d.kind] || '#555';
  let html = '<span class="tt-name">'
    + escHtml(d.name) + '</span>';
  html += '<span class="tt-kind" style="background:'
    + bg + '">' + d.kind + '</span>';
  html += '<div class="tt-row"><span class="tt-label">'
    + 'Qualified: </span>' + escHtml(d.qualified_name)
    + '</div>';
  if (d.line_start != null && d.line_end != null) {
    html += '<div class="tt-row"><span class="tt-label">'
      + 'Lines: </span>'
      + d.line_start + ' \u2013 ' + d.line_end + '</div>';
  }
  if (d.params) {
    html += '<div class="tt-row"><span class="tt-label">'
      + 'Params: </span>' + escHtml(d.params) + '</div>';
  }
  if (d.return_type) {
    html += '<div class="tt-row"><span class="tt-label">'
      + 'Returns: </span>' + escHtml(d.return_type)
      + '</div>';
  }
  tooltip.innerHTML = html;
  tooltip.classList.add("visible");
  positionTooltip(event);
}

function positionTooltip(event) {
  const pad = 14;
  let x = event.pageX + pad;
  let y = event.pageY + pad;
  const rect = tooltip.getBoundingClientRect();
  if (x + rect.width > window.innerWidth - pad) x = event.pageX - rect.width - pad;
  if (y + rect.height > window.innerHeight - pad) y = event.pageY - rect.height - pad;
  tooltip.style.left = x + "px";
  tooltip.style.top  = y + "px";
}

function hideTooltip() {
  tooltip.classList.remove("visible");
}

function escHtml(s) {
  if (!s) return "";
  return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

// -----------------------------------------------------------------------
// SVG setup
// -----------------------------------------------------------------------
const width  = window.innerWidth;
const height = window.innerHeight;

const svg = d3.select("svg")
  .attr("viewBox", [0, 0, width, height]);

// Zoom container
const g = svg.append("g");

const zoom = d3.zoom()
  .scaleExtent([0.08, 6])
  .on("zoom", (event) => g.attr("transform", event.transform));

svg.call(zoom);

// Arrow markers
const markerKinds = [
  { id: "arrow-calls",        color: "#6688aa" },
  { id: "arrow-imports",      color: "#6688aa" },
  { id: "arrow-inherits",     color: "#6688aa" },
];

const defs = svg.append("defs");
markerKinds.forEach(mk => {
  defs.append("marker")
    .attr("id", mk.id)
    .attr("viewBox", "0 -5 10 10")
    .attr("refX", 20)
    .attr("refY", 0)
    .attr("markerWidth", 6)
    .attr("markerHeight", 6)
    .attr("orient", "auto")
    .append("path")
      .attr("d", "M0,-4L10,0L0,4Z")
      .attr("fill", mk.color);
});

// -----------------------------------------------------------------------
// Simulation
// -----------------------------------------------------------------------
const simulation = d3.forceSimulation(nodes)
  .force("link", d3.forceLink(edges)
    .id(d => d.qualified_name)
    .distance(d => d.kind === "CONTAINS" ? 30 : 150)
    .strength(d => d.kind === "CONTAINS" ? 1.2 : 0.3)
  )
  .force("charge", d3.forceManyBody().strength(-200))
  .force("collide", d3.forceCollide().radius(d => (KIND_RADIUS[d.kind] || 8) + 6))
  .force("center", d3.forceCenter(width / 2, height / 2))
  .alphaDecay(0.02)
  .velocityDecay(0.35);

// -----------------------------------------------------------------------
// Draw edges
// -----------------------------------------------------------------------
const EDGE_STYLES = {
  CONTAINS:     {dash:"none", width:2.5, op:0.15, mk:""},
  CALLS:        {dash:"none", width:1.5, op:0.6,
                 mk:"url(#arrow-calls)"},
  IMPORTS_FROM: {dash:"6,3",  width:1.5, op:0.5,
                 mk:"url(#arrow-imports)"},
  INHERITS:     {dash:"2,3",  width:2.5, op:0.6,
                 mk:"url(#arrow-inherits)"},
};
const DEFAULT_EDGE = {dash:"none", width:1, op:0.4, mk:""};

function edgeStyle(d) {
  const s = EDGE_STYLES[d.kind] || DEFAULT_EDGE;
  return {dash: s.dash, width: s.width,
          opacity: s.op, marker: s.mk};
}

const linkGroup = g.append("g").attr("class", "links");
let linkSel = linkGroup.selectAll("line");

function updateLinks() {
  const visibleNodes = new Set(nodes.filter(n => !n._hidden).map(n => n.qualified_name));
  const visibleEdges = edges.filter(e => {
    const src = typeof e.source === "object" ? e.source.qualified_name : e._source;
    const tgt = typeof e.target === "object" ? e.target.qualified_name : e._target;
    return visibleNodes.has(src) && visibleNodes.has(tgt);
  });

  const key = d =>
    d._source + "->" + d._target + ":" + d.kind;
  linkSel = linkGroup.selectAll("line")
    .data(visibleEdges, key);
  linkSel.exit().transition().duration(300).attr("opacity", 0).remove();
  const enter = linkSel.enter().append("line");
  linkSel = enter.merge(linkSel);
  linkSel
    .attr("stroke", "#556677")
    .attr("stroke-width",   d => edgeStyle(d).width)
    .attr("stroke-dasharray", d => edgeStyle(d).dash === "none" ? null : edgeStyle(d).dash)
    .attr("opacity",        d => edgeStyle(d).opacity)
    .attr("marker-end",     d => edgeStyle(d).marker);
}

// -----------------------------------------------------------------------
// Draw nodes
// -----------------------------------------------------------------------
const nodeGroup = g.append("g").attr("class", "nodes");
let nodeSel = nodeGroup.selectAll("g");

const labelGroup = g.append("g").attr("class", "labels");
let labelSel = labelGroup.selectAll("text");

function updateNodes() {
  // Mark hidden nodes
  const hiddenSet = new Set();
  for (const fqn of collapsedFiles) {
    for (const child of allDescendants(fqn)) {
      hiddenSet.add(child);
    }
  }
  nodes.forEach(n => { n._hidden = hiddenSet.has(n.qualified_name); });

  // Nodes
  nodeSel = nodeGroup.selectAll("g.node-g")
    .data(nodes.filter(n => !n._hidden), d => d.qualified_name);

  nodeSel.exit().transition().duration(300).attr("opacity", 0).remove();

  const enter = nodeSel.enter().append("g").attr("class", "node-g");

  // Outer glow ring for File nodes
  enter.filter(d => d.kind === "File").append("circle")
    .attr("class", "glow-ring")
    .attr("r", d => KIND_RADIUS[d.kind] + 4)
    .attr("fill", "none")
    .attr("stroke", d => KIND_COLOR[d.kind])
    .attr("stroke-width", 2)
    .attr("opacity", 0.25);

  // Main circle
  enter.append("circle")
    .attr("class", "node-circle")
    .attr("r", d => KIND_RADIUS[d.kind] || 8)
    .attr("fill", d => KIND_COLOR[d.kind] || "#888")
    .attr("stroke", "#1a1a2e")
    .attr("stroke-width", 1.5)
    .attr("cursor", d => d.kind === "File" ? "pointer" : "grab")
    .attr("opacity", 0)
    .transition().duration(400).attr("opacity", 1);

  enter
    .on("mouseover", (event, d) => showTooltip(event, d))
    .on("mousemove", (event) => positionTooltip(event))
    .on("mouseout",  () => hideTooltip())
    .on("click", (event, d) => {
      if (d.kind === "File") {
        event.stopPropagation();
        toggleCollapse(d.qualified_name);
      }
    })
    .call(d3.drag()
      .on("start", dragStarted)
      .on("drag",  dragged)
      .on("end",   dragEnded)
    );

  nodeSel = enter.merge(nodeSel);

  // Labels
  labelSel = labelGroup.selectAll("text.node-label")
    .data(nodes.filter(n => !n._hidden), d => d.qualified_name);

  labelSel.exit().transition().duration(300).attr("opacity", 0).remove();

  const labelEnter = labelSel.enter().append("text")
    .attr("class", d => "node-label" + (d.kind === "File" ? " file-label" : ""))
    .attr("text-anchor", "start")
    .attr("dy", "0.35em")
    .text(d => d.name)
    .attr("opacity", 0)
    .transition().duration(400).attr("opacity", 1);

  labelSel = labelEnter.merge(labelSel);

  // Update links too
  updateLinks();
}

// -----------------------------------------------------------------------
// Collapse / expand
// -----------------------------------------------------------------------
function toggleCollapse(fileQN) {
  if (collapsedFiles.has(fileQN)) {
    collapsedFiles.delete(fileQN);
  } else {
    collapsedFiles.add(fileQN);
  }
  // Visual indicator on file node
  nodeGroup.selectAll("g.node-g").select(".glow-ring")
    .attr("stroke-dasharray", d => collapsedFiles.has(d.qualified_name) ? "4,3" : null)
    .attr("opacity", d => collapsedFiles.has(d.qualified_name) ? 0.6 : 0.25);

  updateNodes();
  simulation.alpha(0.3).restart();
}

// -----------------------------------------------------------------------
// Drag handlers
// -----------------------------------------------------------------------
function dragStarted(event, d) {
  if (!event.active) simulation.alphaTarget(0.15).restart();
  d.fx = d.x;
  d.fy = d.y;
}
function dragged(event, d) {
  d.fx = event.x;
  d.fy = event.y;
}
function dragEnded(event, d) {
  if (!event.active) simulation.alphaTarget(0);
  d.fx = null;
  d.fy = null;
}

// -----------------------------------------------------------------------
// Tick
// -----------------------------------------------------------------------
simulation.on("tick", () => {
  linkSel
    .attr("x1", d => d.source.x)
    .attr("y1", d => d.source.y)
    .attr("x2", d => d.target.x)
    .attr("y2", d => d.target.y);

  nodeGroup.selectAll("g.node-g")
    .attr("transform", d => `translate(${d.x},${d.y})`);

  labelGroup.selectAll("text.node-label")
    .attr("x", d => d.x + (KIND_RADIUS[d.kind] || 8) + 5)
    .attr("y", d => d.y);
});

// -----------------------------------------------------------------------
// Initial render
// -----------------------------------------------------------------------
updateNodes();

// Center view after layout stabilizes
simulation.on("end", () => {
  // Auto-fit the graph into the viewport
  const bounds = g.node().getBBox();
  if (bounds.width === 0 || bounds.height === 0) return;
  const padFrac = 0.08;
  const fullWidth  = bounds.width  * (1 + 2 * padFrac);
  const fullHeight = bounds.height * (1 + 2 * padFrac);
  const scale = Math.min(width / fullWidth, height / fullHeight, 2);
  const tx = width  / 2 - (bounds.x + bounds.width  / 2) * scale;
  const ty = height / 2 - (bounds.y + bounds.height / 2) * scale;
  svg.transition().duration(750).call(
    zoom.transform,
    d3.zoomIdentity.translate(tx, ty).scale(scale)
  );
});
</script>
</body>
</html>
"""
