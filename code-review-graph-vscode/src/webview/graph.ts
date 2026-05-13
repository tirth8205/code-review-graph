/**
 * Webview entry point for the D3.js force-directed graph visualization.
 * Runs in the browser context inside the VS Code webview panel.
 *
 * Communicates with the extension host via postMessage / addEventListener.
 * NO Node.js APIs are available here.
 */

import * as d3 from "d3";

declare function acquireVsCodeApi(): {
  postMessage(msg: unknown): void;
  getState(): unknown;
  setState(state: unknown): void;
};

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type NodeKind = "File" | "Class" | "Function" | "Test" | "Type";

type EdgeKind =
  | "CALLS"
  | "IMPORTS_FROM"
  | "INHERITS"
  | "IMPLEMENTS"
  | "TESTED_BY"
  | "CONTAINS"
  | "DEPENDS_ON";

interface GraphNode {
  id: number;
  kind: NodeKind;
  name: string;
  qualifiedName: string;
  filePath: string;
  lineStart: number | null;
  lineEnd: number | null;
  language: string | null;
  parentName: string | null;
  params: string | null;
  returnType: string | null;
  modifiers: string | null;
  isTest: boolean;
  fileHash: string | null;
}

interface GraphEdge {
  id: number;
  kind: EdgeKind;
  sourceQualified: string;
  targetQualified: string;
  filePath: string;
  line: number;
}

/** D3 simulation node extends GraphNode with x/y/vx/vy. */
interface SimNode extends d3.SimulationNodeDatum, GraphNode {}

/** D3 simulation link with resolved source/target. */
interface SimLink extends d3.SimulationLinkDatum<SimNode> {
  kind: EdgeKind;
  sourceQualified: string;
  targetQualified: string;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const NODE_RADIUS: Record<NodeKind, number> = {
  File: 18,
  Class: 12,
  Function: 6,
  Test: 6,
  Type: 5,
};

const NODE_COLOR: Record<NodeKind, string> = {
  File: "#58a6ff",
  Class: "#f0883e",
  Function: "#3fb950",
  Test: "#d2a8ff",
  Type: "#8b949e",
};

const NODE_SHAPE: Record<NodeKind, d3.SymbolType> = {
  File: d3.symbolCircle,
  Class: d3.symbolSquare,
  Function: d3.symbolTriangle,
  Test: d3.symbolDiamond,
  Type: d3.symbolCross,
};

const NODE_AREA: Record<NodeKind, number> = {
  File: 616,
  Class: 452,
  Function: 314,
  Test: 314,
  Type: 314,
};

const EDGE_COLOR: Record<EdgeKind, string> = {
  CALLS: "#3fb950",
  IMPORTS_FROM: "#f0883e",
  INHERITS: "#d2a8ff",
  IMPLEMENTS: "#f9e2af",
  TESTED_BY: "#f38ba8",
  CONTAINS: "rgba(139,148,158,0.15)",
  DEPENDS_ON: "#fab387",
};

const ALL_EDGE_KINDS: EdgeKind[] = [
  "CALLS",
  "IMPORTS_FROM",
  "INHERITS",
  "IMPLEMENTS",
  "TESTED_BY",
  "CONTAINS",
  "DEPENDS_ON",
];

// ---------------------------------------------------------------------------
// Global state
// ---------------------------------------------------------------------------

const vscodeApi = acquireVsCodeApi();

let allNodes: SimNode[] = [];
let allEdges: SimLink[] = [];
let nodeMap = new Map<string, SimNode>();

let visibleEdgeKinds = new Set<EdgeKind>(ALL_EDGE_KINDS);
let selectedNode: SimNode | null = null;
let depthLimit = 0; // 0 = show all

let simulation: d3.Simulation<SimNode, SimLink> | null = null;
let svg: d3.Selection<SVGSVGElement, unknown, null, undefined>;
let container: d3.Selection<SVGGElement, unknown, null, undefined>;
let linkGroup: d3.Selection<SVGGElement, unknown, null, undefined>;
let nodeGroup: d3.Selection<SVGGElement, unknown, null, undefined>;
let labelGroup: d3.Selection<SVGGElement, unknown, null, undefined>;
let zoomBehavior: d3.ZoomBehavior<SVGSVGElement, unknown>;

let linkSelection: d3.Selection<SVGLineElement, SimLink, SVGGElement, unknown>;
let nodeSelection: d3.Selection<SVGPathElement, SimNode, SVGGElement, unknown>;
let labelSelection: d3.Selection<SVGTextElement, SimNode, SVGGElement, unknown>;

let currentTheme: "dark" | "light" = "dark";

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

function init(): void {
  createSvg();
  bindToolbarEvents();
  bindExtensionMessages();

  vscodeApi.postMessage({ command: "ready" });
}

// ---------------------------------------------------------------------------
// SVG setup
// ---------------------------------------------------------------------------

function createSvg(): void {
  const graphEl = document.getElementById("graph-area")!;
  const width = graphEl.clientWidth || window.innerWidth;
  const height = graphEl.clientHeight || window.innerHeight;

  svg = d3
    .select(graphEl)
    .append("svg")
    .attr("width", "100%")
    .attr("height", "100%")
    .attr("viewBox", `0 0 ${width} ${height}`);

  // Arrow marker definitions -- one per edge kind
  const defs = svg.append("defs");
  for (const kind of ALL_EDGE_KINDS) {
    defs
      .append("marker")
      .attr("id", `arrow-${kind}`)
      .attr("viewBox", "0 -5 10 10")
      .attr("refX", 20)
      .attr("refY", 0)
      .attr("markerWidth", 6)
      .attr("markerHeight", 6)
      .attr("orient", "auto")
      .append("path")
      .attr("d", "M0,-5L10,0L0,5")
      .attr("fill", EDGE_COLOR[kind]);
  }

  container = svg.append("g").attr("class", "graph-container");
  linkGroup = container.append("g").attr("class", "links");
  nodeGroup = container.append("g").attr("class", "nodes");
  labelGroup = container.append("g").attr("class", "labels");

  // Initialize empty selections
  linkSelection = linkGroup.selectAll<SVGLineElement, SimLink>("line");
  nodeSelection = nodeGroup.selectAll<SVGPathElement, SimNode>("path.node-shape");
  labelSelection = labelGroup.selectAll<SVGTextElement, SimNode>("text");

  // Zoom + pan
  zoomBehavior = d3
    .zoom<SVGSVGElement, unknown>()
    .scaleExtent([0.05, 8])
    .on("zoom", (event: d3.D3ZoomEvent<SVGSVGElement, unknown>) => {
      container.attr("transform", event.transform.toString());
    });

  svg.call(zoomBehavior);

  // Resize handler
  const resizeObserver = new ResizeObserver(() => {
    const w = graphEl.clientWidth;
    const h = graphEl.clientHeight;
    svg.attr("viewBox", `0 0 ${w} ${h}`);
  });
  resizeObserver.observe(graphEl);
}

// ---------------------------------------------------------------------------
// Data ingestion
// ---------------------------------------------------------------------------

function setData(nodes: GraphNode[], edges: GraphEdge[]): void {
  // Build SimNodes
  allNodes = nodes.map((n) => ({ ...n } as SimNode));
  nodeMap = new Map(allNodes.map((n) => [n.qualifiedName, n]));

  // Build SimLinks, filtering to edges where both endpoints exist
  allEdges = [];
  for (const e of edges) {
    const src = nodeMap.get(e.sourceQualified);
    const tgt = nodeMap.get(e.targetQualified);
    if (src && tgt) {
      allEdges.push({
        source: src,
        target: tgt,
        kind: e.kind,
        sourceQualified: e.sourceQualified,
        targetQualified: e.targetQualified,
      });
    }
  }

  // Reset depth filter
  depthLimit = 0;
  const slider = document.getElementById("depth-slider") as HTMLInputElement | null;
  if (slider) {
    slider.value = "0";
  }
  const depthValue = document.getElementById("depth-value");
  if (depthValue) {
    depthValue.textContent = "All";
  }

  // Show/hide empty state
  const emptyState = document.getElementById("empty-state");
  const graphArea = document.getElementById("graph-area");
  if (nodes.length === 0) {
    if (emptyState) emptyState.style.display = "block";
    if (graphArea) {
      // Hide the SVG but keep the container
      const svgHide = graphArea.querySelector("svg");
      if (svgHide) svgHide.style.display = "none";
    }
    updateDepthSliderState();
    return;
  }
  if (emptyState) emptyState.style.display = "none";
  const svgEl = graphArea?.querySelector("svg");
  if (svgEl) svgEl.style.display = "";

  buildGraph();

  updateDepthSliderState();
}

// ---------------------------------------------------------------------------
// Graph construction
// ---------------------------------------------------------------------------

function getVisibleData(): { nodes: SimNode[]; links: SimLink[] } {
  // Filter edges by visible kinds
  let links = allEdges.filter((e) => visibleEdgeKinds.has(e.kind));

  let nodes: SimNode[];

  if (selectedNode && depthLimit > 0) {
    // BFS from selected node up to depthLimit
    const reachable = new Set<string>();
    reachable.add(selectedNode.qualifiedName);
    let frontier = new Set<string>([selectedNode.qualifiedName]);

    for (let d = 0; d < depthLimit; d++) {
      const next = new Set<string>();
      for (const qn of frontier) {
        for (const link of links) {
          const srcQn =
            typeof link.source === "object"
              ? (link.source as SimNode).qualifiedName
              : link.sourceQualified;
          const tgtQn =
            typeof link.target === "object"
              ? (link.target as SimNode).qualifiedName
              : link.targetQualified;

          if (srcQn === qn && !reachable.has(tgtQn)) {
            reachable.add(tgtQn);
            next.add(tgtQn);
          }
          if (tgtQn === qn && !reachable.has(srcQn)) {
            reachable.add(srcQn);
            next.add(srcQn);
          }
        }
      }
      frontier = next;
      if (frontier.size === 0) break;
    }

    nodes = allNodes.filter((n) => reachable.has(n.qualifiedName));
    const reachableSet = reachable;
    links = links.filter((l) => {
      const srcQn =
        typeof l.source === "object"
          ? (l.source as SimNode).qualifiedName
          : l.sourceQualified;
      const tgtQn =
        typeof l.target === "object"
          ? (l.target as SimNode).qualifiedName
          : l.targetQualified;
      return reachableSet.has(srcQn) && reachableSet.has(tgtQn);
    });
  } else {
    nodes = [...allNodes];
  }

  // Apply search filter
  const searchInput = document.getElementById("search-input") as HTMLInputElement | null;
  const query = searchInput?.value?.trim().toLowerCase() ?? "";
  if (query.length > 0) {
    const matchingQns = new Set(
      nodes
        .filter((n) => n.name.toLowerCase().includes(query) || n.qualifiedName.toLowerCase().includes(query))
        .map((n) => n.qualifiedName)
    );
    // Keep matching nodes + their direct neighbors
    const expanded = new Set(matchingQns);
    for (const link of links) {
      const srcQn =
        typeof link.source === "object"
          ? (link.source as SimNode).qualifiedName
          : link.sourceQualified;
      const tgtQn =
        typeof link.target === "object"
          ? (link.target as SimNode).qualifiedName
          : link.targetQualified;
      if (matchingQns.has(srcQn)) expanded.add(tgtQn);
      if (matchingQns.has(tgtQn)) expanded.add(srcQn);
    }
    nodes = nodes.filter((n) => expanded.has(n.qualifiedName));
    links = links.filter((l) => {
      const srcQn =
        typeof l.source === "object"
          ? (l.source as SimNode).qualifiedName
          : l.sourceQualified;
      const tgtQn =
        typeof l.target === "object"
          ? (l.target as SimNode).qualifiedName
          : l.targetQualified;
      return expanded.has(srcQn) && expanded.has(tgtQn);
    });
  }

  return { nodes, links };
}

function buildGraph(): void {
  const { nodes, links } = getVisibleData();

  // Stop existing simulation
  if (simulation) {
    simulation.stop();
  }

  const graphEl = document.getElementById("graph-area")!;
  const width = graphEl.clientWidth || window.innerWidth;
  const height = graphEl.clientHeight || window.innerHeight;

  // --- Links ---
  linkSelection = linkGroup
    .selectAll<SVGLineElement, SimLink>("line")
    .data(links, (d) => `${d.sourceQualified}-${d.targetQualified}-${d.kind}`)
    .join("line")
    .attr("stroke", (d) => EDGE_COLOR[d.kind])
    .attr("stroke-width", 1.5)
    .attr("stroke-opacity", 0.4)
    .attr("marker-end", (d) => `url(#arrow-${d.kind})`);

  // --- Nodes ---
  nodeSelection = nodeGroup
    .selectAll<SVGPathElement, SimNode>("path.node-shape")
    .data(nodes, (d) => d.qualifiedName)
    .join("path")
    .attr("class", "node-shape")
    .attr("d", (d) => d3.symbol().type(NODE_SHAPE[d.kind] ?? d3.symbolCircle).size(NODE_AREA[d.kind] ?? 314)()!)
    .attr("fill", (d) => NODE_COLOR[d.kind] ?? "#cdd6f4")
    .attr("stroke", "none")
    .attr("stroke-width", 2)
    .attr("cursor", "pointer")
    .on("click", (_event, d) => {
      selectNode(d);
      vscodeApi.postMessage({
        command: "nodeClicked",
        qualifiedName: d.qualifiedName,
        filePath: d.filePath,
        lineStart: d.lineStart ?? 1,
      });
    })
    .on("dblclick", (_event, d) => {
      // Center on node and expand depth by 1
      selectNode(d);
      depthLimit = Math.min(depthLimit + 1, 10);
      const slider = document.getElementById("depth-slider") as HTMLInputElement | null;
      if (slider) slider.value = String(depthLimit);
      const depthValue = document.getElementById("depth-value");
      if (depthValue) depthValue.textContent = String(depthLimit);
      buildGraph();
      centerOnNode(d);
    })
    .on("mouseenter", (_event, d) => {
      showTooltip(d);
      highlightConnected(d);
    })
    .on("mouseleave", () => {
      hideTooltip();
      unhighlightAll();
    })
    .call(
      d3
        .drag<SVGPathElement, SimNode>()
        .on("start", (event, d) => {
          if (!event.active) simulation?.alphaTarget(0.3).restart();
          d.fx = d.x;
          d.fy = d.y;
        })
        .on("drag", (event, d) => {
          d.fx = event.x;
          d.fy = event.y;
        })
        .on("end", (event, d) => {
          if (!event.active) simulation?.alphaTarget(0);
          d.fx = null;
          d.fy = null;
        })
    )
    .attr("tabindex", 0)
    .attr("role", "button")
    .attr("aria-label", (d) => `${d.kind}: ${d.name}`)
    .on("keydown", (event: KeyboardEvent, d: SimNode) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        selectNode(d);
        vscodeApi.postMessage({
          command: "nodeClicked",
          qualifiedName: d.qualifiedName,
          filePath: d.filePath,
          lineStart: d.lineStart ?? 1,
        });
      } else if (event.key === "Escape") {
        event.preventDefault();
        selectedNode = null;
        unhighlightAll();
        nodeSelection.attr("stroke", "none");
      } else if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.key)) {
        event.preventDefault();
        const visibleNodes = nodeSelection.data();
        let best: SimNode | null = null;
        let bestDist = Infinity;
        for (const n of visibleNodes) {
          if (n.qualifiedName === d.qualifiedName || n.x == null || n.y == null || d.x == null || d.y == null) continue;
          const dx = n.x - d.x;
          const dy = n.y - d.y;
          const dist = Math.sqrt(dx * dx + dy * dy);
          let ok = false;
          if (event.key === "ArrowRight" && dx > 0 && Math.abs(dy) < Math.abs(dx)) ok = true;
          if (event.key === "ArrowLeft" && dx < 0 && Math.abs(dy) < Math.abs(dx)) ok = true;
          if (event.key === "ArrowDown" && dy > 0 && Math.abs(dx) < Math.abs(dy)) ok = true;
          if (event.key === "ArrowUp" && dy < 0 && Math.abs(dx) < Math.abs(dy)) ok = true;
          if (ok && dist < bestDist) {
            best = n;
            bestDist = dist;
          }
        }
        if (best) {
          const target = nodeGroup.selectAll<SVGPathElement, SimNode>("path.node-shape")
            .filter((n) => n.qualifiedName === best!.qualifiedName)
            .node();
          if (target) (target as HTMLElement).focus();
        }
      }
    })
    .on("focus", (_event: FocusEvent, d: SimNode) => {
      showTooltip(d);
      highlightConnected(d);
    })
    .on("blur", () => {
      hideTooltip();
      unhighlightAll();
    });

  // Highlight search matches
  const searchInput = document.getElementById("search-input") as HTMLInputElement | null;
  const query = searchInput?.value?.trim().toLowerCase() ?? "";
  if (query.length > 0) {
    nodeSelection.attr("stroke", (d) => {
      const matches =
        d.name.toLowerCase().includes(query) ||
        d.qualifiedName.toLowerCase().includes(query);
      return matches ? "#e6edf3" : "none";
    });
  }

  // Highlight selected node
  if (selectedNode) {
    nodeSelection.attr("stroke", (d) => {
      if (d.qualifiedName === selectedNode!.qualifiedName) return "#e6edf3";
      if (query.length > 0) {
        const matches =
          d.name.toLowerCase().includes(query) ||
          d.qualifiedName.toLowerCase().includes(query);
        return matches ? "#e6edf3" : "none";
      }
      return "none";
    });
  }

  // --- Labels ---
  labelSelection = labelGroup
    .selectAll<SVGTextElement, SimNode>("text")
    .data(nodes, (d) => d.qualifiedName)
    .join("text")
    .text((d) => d.name)
    .attr("font-size", 10)
    .attr("fill", currentTheme === "dark" ? "#cdd6f4" : "#4c4f69")
    .attr("text-anchor", "middle")
    .attr("dy", (d) => (NODE_RADIUS[d.kind] ?? 10) + 14)
    .attr("pointer-events", "none");

  // --- Force simulation ---
  simulation = d3
    .forceSimulation<SimNode>(nodes)
    .alphaDecay(0.02)
    .force(
      "link",
      d3
        .forceLink<SimNode, SimLink>(links)
        .id((d) => d.qualifiedName)
        .distance(100)
    )
    .force("charge", d3.forceManyBody<SimNode>().strength(-200))
    .force("center", d3.forceCenter(width / 2, height / 2))
    .force(
      "collide",
      d3.forceCollide<SimNode>().radius((d) => (NODE_RADIUS[d.kind] ?? 10) + 5)
    )
    .on("tick", () => {
      linkSelection
        .attr("x1", (d) => (d.source as SimNode).x!)
        .attr("y1", (d) => (d.source as SimNode).y!)
        .attr("x2", (d) => (d.target as SimNode).x!)
        .attr("y2", (d) => (d.target as SimNode).y!);

      nodeSelection.attr("transform", (d) => `translate(${d.x},${d.y})`);

      labelSelection.attr("x", (d) => d.x!).attr("y", (d) => d.y!);
    });

  // Update node count display
  const countEl = document.getElementById("node-count");
  if (countEl) {
    countEl.textContent = `${nodes.length} nodes, ${links.length} edges`;
  }
}

// ---------------------------------------------------------------------------
// Selection & highlight
// ---------------------------------------------------------------------------

function selectNode(node: SimNode): void {
  selectedNode = node;
  nodeSelection.attr("stroke", (d) =>
    d.qualifiedName === node.qualifiedName ? "#e6edf3" : "none"
  );
  updateDepthSliderState();
}

function updateDepthSliderState(): void {
  const slider = document.getElementById("depth-slider") as HTMLInputElement | null;
  const depthValue = document.getElementById("depth-value");
  if (slider) {
    if (selectedNode) {
      slider.disabled = false;
    } else {
      slider.disabled = true;
      if (depthValue) depthValue.textContent = "N/A";
    }
  }
}

function highlightConnected(node: SimNode): void {
  const connectedQns = new Set<string>();
  connectedQns.add(node.qualifiedName);

  linkSelection.attr("stroke-opacity", (d) => {
    const srcQn = (d.source as SimNode).qualifiedName;
    const tgtQn = (d.target as SimNode).qualifiedName;
    if (srcQn === node.qualifiedName || tgtQn === node.qualifiedName) {
      connectedQns.add(srcQn);
      connectedQns.add(tgtQn);
      return 0.8;
    }
    return 0.1;
  });

  nodeSelection.attr("opacity", (d) =>
    connectedQns.has(d.qualifiedName) ? 1 : 0.2
  );
  labelSelection.attr("opacity", (d) =>
    connectedQns.has(d.qualifiedName) ? 1 : 0.2
  );
}

function unhighlightAll(): void {
  linkSelection.attr("stroke-opacity", 0.4);
  nodeSelection.attr("opacity", 1);
  labelSelection.attr("opacity", 1);
}

// ---------------------------------------------------------------------------
// Tooltip
// ---------------------------------------------------------------------------

function showTooltip(node: SimNode): void {
  const tooltip = document.getElementById("tooltip")!;
  tooltip.style.display = "block";

  let html = `<strong>${escapeHtml(node.name)}</strong><br/>`;
  html += `<span class="tooltip-kind">${escapeHtml(node.kind)}</span><br/>`;
  html += `<span class="tooltip-path">${escapeHtml(node.filePath)}</span>`;
  if (node.lineStart != null) {
    html += `<br/>Lines ${node.lineStart}`;
    if (node.lineEnd != null && node.lineEnd !== node.lineStart) {
      html += `-${node.lineEnd}`;
    }
  }
  if (node.params) {
    html += `<br/><span class="tooltip-params">${escapeHtml(node.params)}</span>`;
  }
  if (node.returnType) {
    html += ` <span class="tooltip-return">&rarr; ${escapeHtml(node.returnType)}</span>`;
  }

  tooltip.innerHTML = html;

  // Position near cursor -- we'll update on mousemove too
  document.addEventListener("mousemove", positionTooltip);
}

function positionTooltip(event: MouseEvent): void {
  const tooltip = document.getElementById("tooltip")!;
  const x = event.clientX + 12;
  const y = event.clientY + 12;

  // Keep tooltip in viewport
  const rect = tooltip.getBoundingClientRect();
  const maxX = window.innerWidth - rect.width - 8;
  const maxY = window.innerHeight - rect.height - 8;

  tooltip.style.left = `${Math.min(x, maxX)}px`;
  tooltip.style.top = `${Math.min(y, maxY)}px`;
}

function hideTooltip(): void {
  const tooltip = document.getElementById("tooltip")!;
  tooltip.style.display = "none";
  document.removeEventListener("mousemove", positionTooltip);
}

function escapeHtml(text: string): string {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

// ---------------------------------------------------------------------------
// Highlight node (from extension message)
// ---------------------------------------------------------------------------

function highlightNodeByName(qualifiedName: string): void {
  const node = nodeMap.get(qualifiedName);
  if (!node) return;

  selectNode(node);
  centerOnNode(node);

  // Add pulsing ring animation
  const ring = nodeGroup
    .append("circle")
    .attr("cx", node.x ?? 0)
    .attr("cy", node.y ?? 0)
    .attr("r", (NODE_RADIUS[node.kind] ?? 10) + 4)
    .attr("fill", "none")
    .attr("stroke", "#e6edf3")
    .attr("stroke-width", 3)
    .attr("class", "pulse-ring");

  // Remove after animation completes
  ring
    .transition()
    .duration(600)
    .attr("r", (NODE_RADIUS[node.kind] ?? 10) + 20)
    .attr("stroke-opacity", 0)
    .on("end", function () {
      d3.select(this).remove();
    });

  // Second pulse
  setTimeout(() => {
    if (!node.x) return;
    const ring2 = nodeGroup
      .append("circle")
      .attr("cx", node.x)
      .attr("cy", node.y ?? 0)
      .attr("r", (NODE_RADIUS[node.kind] ?? 10) + 4)
      .attr("fill", "none")
      .attr("stroke", "#e6edf3")
      .attr("stroke-width", 3);

    ring2
      .transition()
      .duration(600)
      .attr("r", (NODE_RADIUS[node.kind] ?? 10) + 20)
      .attr("stroke-opacity", 0)
      .on("end", function () {
        d3.select(this).remove();
      });
  }, 300);
}

// ---------------------------------------------------------------------------
// Camera
// ---------------------------------------------------------------------------

function centerOnNode(node: SimNode): void {
  if (!node.x || !node.y) return;

  const graphEl = document.getElementById("graph-area")!;
  const width = graphEl.clientWidth;
  const height = graphEl.clientHeight;

  const transform = d3.zoomIdentity
    .translate(width / 2, height / 2)
    .scale(1.5)
    .translate(-node.x, -node.y);

  svg
    .transition()
    .duration(500)
    .call(zoomBehavior.transform, transform);
}

function fitToView(): void {
  const graphEl = document.getElementById("graph-area")!;
  const width = graphEl.clientWidth;
  const height = graphEl.clientHeight;

  if (allNodes.length === 0) return;

  // Find bounding box of visible nodes
  const visibleNodes = nodeSelection.data();
  if (visibleNodes.length === 0) return;

  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const n of visibleNodes) {
    if (n.x == null || n.y == null) continue;
    const r = NODE_RADIUS[n.kind] ?? 10;
    minX = Math.min(minX, n.x - r);
    maxX = Math.max(maxX, n.x + r);
    minY = Math.min(minY, n.y - r);
    maxY = Math.max(maxY, n.y + r);
  }

  if (!isFinite(minX)) return;

  const padding = 60;
  const bboxWidth = maxX - minX + padding * 2;
  const bboxHeight = maxY - minY + padding * 2;
  const scale = Math.min(width / bboxWidth, height / bboxHeight, 2);
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;

  const transform = d3.zoomIdentity
    .translate(width / 2, height / 2)
    .scale(scale)
    .translate(-cx, -cy);

  svg
    .transition()
    .duration(500)
    .call(zoomBehavior.transform, transform);
}

// ---------------------------------------------------------------------------
// Toolbar events
// ---------------------------------------------------------------------------

function bindToolbarEvents(): void {
  // Search
  const searchInput = document.getElementById("search-input") as HTMLInputElement | null;
  if (searchInput) {
    let debounceTimer: ReturnType<typeof setTimeout>;
    searchInput.addEventListener("input", () => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => {
        buildGraph();
      }, 250);
    });
  }

  // Edge toggle pills
  for (const kind of ALL_EDGE_KINDS) {
    const pill = document.getElementById(`edge-${kind}`);
    if (pill) {
      const toggle = () => {
        if (visibleEdgeKinds.has(kind)) {
          visibleEdgeKinds.delete(kind);
          pill.classList.remove("active");
          pill.setAttribute("aria-pressed", "false");
        } else {
          visibleEdgeKinds.add(kind);
          pill.classList.add("active");
          pill.setAttribute("aria-pressed", "true");
        }
        buildGraph();
      };
      pill.addEventListener("click", toggle);
      pill.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggle(); }
      });
    }
  }

  // Edge filter popover toggle
  const edgeFilterBtn = document.getElementById("btn-edge-filter");
  const edgePopover = document.getElementById("edge-popover");
  if (edgeFilterBtn && edgePopover) {
    edgeFilterBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      edgePopover.classList.toggle("visible");
    });
    document.addEventListener("click", (e) => {
      if (!edgePopover.contains(e.target as Node) && e.target !== edgeFilterBtn) {
        edgePopover.classList.remove("visible");
      }
    });
  }

  // Depth slider
  const depthSlider = document.getElementById("depth-slider") as HTMLInputElement | null;
  if (depthSlider) {
    depthSlider.addEventListener("input", () => {
      depthLimit = parseInt(depthSlider.value, 10);
      const depthValue = document.getElementById("depth-value");
      if (depthValue) {
        depthValue.textContent = depthLimit === 0 ? "All" : String(depthLimit);
      }
      buildGraph();
    });
  }

  // Fit button
  const fitBtn = document.getElementById("btn-fit");
  if (fitBtn) {
    fitBtn.addEventListener("click", () => {
      fitToView();
    });
  }

  // Export SVG button
  const exportBtn = document.getElementById("btn-export");
  if (exportBtn) {
    exportBtn.addEventListener("click", () => {
      const svgEl = document.querySelector("#graph-area svg");
      if (svgEl) {
        const serializer = new XMLSerializer();
        const svgString = serializer.serializeToString(svgEl);
        vscodeApi.postMessage({
          command: "exportSvg",
          svg: svgString,
        });
      }
    });
  }

  // Export PNG button
  const exportPngBtn = document.getElementById("btn-export-png");
  if (exportPngBtn) {
    exportPngBtn.addEventListener("click", () => {
      const svgEl = document.querySelector("#graph-area svg") as SVGSVGElement | null;
      if (!svgEl) { return; }

      const serializer = new XMLSerializer();
      const svgString = serializer.serializeToString(svgEl);
      const canvas = document.createElement("canvas");
      const bbox = svgEl.getBoundingClientRect();
      canvas.width = bbox.width * 2;  // 2x for retina
      canvas.height = bbox.height * 2;
      const ctx = canvas.getContext("2d");
      if (!ctx) { return; }
      ctx.scale(2, 2);

      const img = new Image();
      img.onload = () => {
        ctx.drawImage(img, 0, 0);
        const pngData = canvas.toDataURL("image/png");
        vscodeApi.postMessage({ command: "exportPng", data: pngData });
      };
      img.src = "data:image/svg+xml;base64," + btoa(unescape(encodeURIComponent(svgString)));
    });
  }
}

// ---------------------------------------------------------------------------
// Extension message handling
// ---------------------------------------------------------------------------

function bindExtensionMessages(): void {
  window.addEventListener("message", (event) => {
    const message = event.data;
    switch (message.command) {
      case "setData":
        setData(
          message.nodes as GraphNode[],
          message.edges as GraphEdge[]
        );
        // Auto-fit after simulation settles a bit
        setTimeout(() => fitToView(), 800);
        // Show truncation warning if needed
        if (message.truncated) {
          const warn = document.getElementById("truncation-warning");
          if (warn) {
            warn.style.display = "inline";
            warn.textContent = `\u26a0 Showing ${message.maxNodes} of more nodes. Increase maxNodes in settings.`;
          }
        }
        break;

      case "highlightNode":
        highlightNodeByName(message.qualifiedName as string);
        break;

      case "setTheme":
        currentTheme = message.theme as "dark" | "light";
        applyTheme();
        break;
    }
  });
}

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------

function applyTheme(): void {
  const textColor = currentTheme === "dark" ? "#cdd6f4" : "#4c4f69";
  labelSelection.attr("fill", textColor);
}

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

init();
