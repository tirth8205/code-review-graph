"""Local browser UI and HTTP API for exploring the code graph."""

from __future__ import annotations

import json
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .service import GraphService

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>axon-web</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #f7f7f4;
      --panel: #ffffff;
      --text: #1c1f23;
      --muted: #69707a;
      --line: #d8d8d2;
      --accent: #146c94;
      --accent-2: #7b4f9f;
      --warn: #a15c00;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #151719;
        --panel: #202327;
        --text: #f1f3f4;
        --muted: #a3abb5;
        --line: #3b4046;
        --accent: #55b6d9;
        --accent-2: #c59cff;
        --warn: #f0b35a;
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font: 14px/1.4 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 18px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    h1 { margin: 0; font-size: 18px; font-weight: 650; letter-spacing: 0; }
    main {
      display: grid;
      grid-template-columns: minmax(300px, 420px) minmax(0, 1fr);
      min-height: calc(100vh - 57px);
    }
    aside {
      border-right: 1px solid var(--line);
      background: var(--panel);
      padding: 14px;
      overflow: auto;
    }
    section { padding: 14px; overflow: auto; }
    input, select, button {
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--text);
      font: inherit;
    }
    input { width: 100%; padding: 0 10px; }
    button { padding: 0 10px; cursor: pointer; }
    button.primary { background: var(--accent); border-color: var(--accent); color: white; }
    .row { display: flex; gap: 8px; align-items: center; margin-bottom: 10px; }
    .row > * { flex: 1; }
    .stats {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      margin: 10px 0 14px;
    }
    .metric {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px;
      background: color-mix(in srgb, var(--panel) 90%, var(--bg));
    }
    .metric strong { display: block; font-size: 18px; }
    .muted { color: var(--muted); }
    .list { display: grid; gap: 8px; }
    .item {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 9px;
      background: var(--panel);
      cursor: pointer;
    }
    .item:hover { border-color: var(--accent); }
    .item-title { font-weight: 650; overflow-wrap: anywhere; }
    .badge {
      display: inline-flex;
      align-items: center;
      min-height: 20px;
      padding: 0 6px;
      border-radius: 999px;
      background: color-mix(in srgb, var(--accent) 15%, transparent);
      color: var(--accent);
      font-size: 12px;
      margin-right: 4px;
    }
    .graph {
      width: 100%;
      min-height: 520px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      overflow: auto;
      padding: 12px;
    }
    svg { width: 100%; min-width: 780px; height: 520px; }
    line { stroke: var(--line); stroke-width: 1.2; }
    circle { fill: var(--accent); }
    circle.kind-File { fill: var(--muted); }
    circle.kind-Class { fill: var(--accent-2); }
    text { fill: var(--text); font-size: 12px; }
    pre {
      margin: 12px 0 0;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      overflow: auto;
      background: var(--panel);
      white-space: pre-wrap;
    }
    @media (max-width: 860px) {
      main { grid-template-columns: 1fr; }
      aside { border-right: 0; border-bottom: 1px solid var(--line); }
    }
  </style>
</head>
<body>
  <header>
    <h1>axon-web</h1>
    <div id="repo" class="muted"></div>
  </header>
  <main>
    <aside>
      <div class="row">
        <input id="search" placeholder="Search symbols, files, qualified names">
        <button id="searchBtn" class="primary">Search</button>
      </div>
      <div class="row">
        <input id="impact" placeholder="Impact files, comma-separated">
        <button id="impactBtn">Impact</button>
      </div>
      <div id="stats" class="stats"></div>
      <div id="results" class="list"></div>
    </aside>
    <section>
      <div class="graph"><svg id="graph"></svg></div>
      <pre id="details" class="muted">Select a node or run a query.</pre>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    let graphData = {nodes: [], edges: []};

    async function api(path) {
      const res = await fetch(path);
      if (!res.ok) throw new Error(await res.text());
      return res.json();
    }

    function showDetails(value) {
      $("details").textContent = JSON.stringify(value, null, 2);
    }

    function renderStats(status) {
      $("repo").textContent = status.repo_root || "";
      const s = status.stats || {};
      const t = status.token_estimate || {};
      const telemetry = status.telemetry || {};
      $("stats").innerHTML = [
        ["Nodes", formatNumber(s.total_nodes || 0)],
        ["Edges", formatNumber(s.total_edges || 0)],
        ["Files", formatNumber(s.files_count || 0)],
        ["Tokens saved", formatNumber(t.saved_tokens || 0)],
        ["Reduction", `${t.reduction_percent || 0}%`],
        ["Source / graph", `${t.compression_ratio || 0}x`],
        ["API requests", formatNumber(telemetry.total_requests || 0)],
        ["Est. saved total", formatNumber(telemetry.estimated_saved_tokens || 0)],
        ["Est. saved avg", formatNumber(Math.round(telemetry.average_saved_tokens || 0))],
      ].map(([k, v]) => `<div class="metric"><span class="muted">${k}</span><strong>${v}</strong></div>`).join("");
    }

    function formatNumber(value) {
      return Number(value || 0).toLocaleString();
    }

    function renderList(nodes) {
      $("results").innerHTML = nodes.map((n) => `
        <div class="item" data-qn="${escapeHtml(n.qualified_name)}">
          <span class="badge">${escapeHtml(n.kind)}</span>
          <div class="item-title">${escapeHtml(n.name)}</div>
          <div class="muted">${escapeHtml(n.file_path)}:${n.line_start}</div>
        </div>
      `).join("");
      document.querySelectorAll(".item").forEach((el) => {
        el.addEventListener("click", async () => {
          const qn = el.getAttribute("data-qn");
          showDetails(await api(`/api/node?qualified_name=${encodeURIComponent(qn)}`));
        });
      });
    }

    function renderGraph(data) {
      graphData = data;
      const svg = $("graph");
      const nodes = data.nodes || [];
      const edges = data.edges || [];
      const width = Math.max(780, svg.clientWidth || 780);
      const height = 520;
      const radius = Math.min(width, height) * 0.38;
      const centerX = width / 2;
      const centerY = height / 2;
      const pos = new Map();
      nodes.forEach((n, i) => {
        const angle = (Math.PI * 2 * i) / Math.max(nodes.length, 1);
        pos.set(n.qualified_name, {
          x: centerX + Math.cos(angle) * radius,
          y: centerY + Math.sin(angle) * radius,
        });
      });
      const edgeHtml = edges.map((e) => {
        const a = pos.get(e.source);
        const b = pos.get(e.target);
        if (!a || !b) return "";
        return `<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"></line>`;
      }).join("");
      const nodeHtml = nodes.map((n) => {
        const p = pos.get(n.qualified_name);
        const label = n.name.length > 28 ? `${n.name.slice(0, 25)}...` : n.name;
        return `<g data-qn="${escapeHtml(n.qualified_name)}">
          <circle class="kind-${escapeHtml(n.kind)}" cx="${p.x}" cy="${p.y}" r="${n.kind === "File" ? 5 : 7}"></circle>
          <text x="${p.x + 9}" y="${p.y + 4}">${escapeHtml(label)}</text>
        </g>`;
      }).join("");
      svg.innerHTML = edgeHtml + nodeHtml;
      svg.querySelectorAll("g").forEach((el) => {
        el.addEventListener("click", async () => {
          showDetails(await api(`/api/node?qualified_name=${encodeURIComponent(el.getAttribute("data-qn"))}`));
        });
      });
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }[ch]));
    }

    async function refresh() {
      renderStats(await api("/api/status"));
      renderGraph(await api("/api/graph?limit=250"));
    }

    $("searchBtn").addEventListener("click", async () => {
      const q = $("search").value.trim();
      if (!q) return;
      const result = await api(`/api/search?q=${encodeURIComponent(q)}`);
      renderList(result.nodes || []);
      showDetails(result);
    });
    $("search").addEventListener("keydown", (event) => {
      if (event.key === "Enter") $("searchBtn").click();
    });
    $("impactBtn").addEventListener("click", async () => {
      const files = $("impact").value.trim();
      if (!files) return;
      const result = await api(`/api/impact?files=${encodeURIComponent(files)}`);
      renderList(result.impacted_nodes || []);
      showDetails(result);
    });

    const events = new EventSource("/events");
    events.addEventListener("graph-updated", refresh);
    refresh().catch((err) => showDetails({status: "error", error: String(err)}));
  </script>
</body>
</html>
"""


class AxonWebHandler(BaseHTTPRequestHandler):
    service: GraphService

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                self._send_html(INDEX_HTML)
                return
            elif parsed.path == "/events":
                self._send_events()
                return
            elif parsed.path == "/api/status":
                payload = self.service.status()
                operation = None
            elif parsed.path == "/api/search":
                q = query.get("q", [""])[0]
                limit = int(query.get("limit", ["50"])[0])
                payload = self.service.search(q, limit=limit)
                operation = "search"
            elif parsed.path == "/api/node":
                qn = query.get("qualified_name", [""])[0]
                payload = self.service.get_node(qn)
                operation = "node"
            elif parsed.path == "/api/query":
                pattern = query.get("pattern", [""])[0]
                target = query.get("target", [""])[0]
                payload = self.service.query(pattern, target)
                operation = f"query:{pattern or 'unknown'}"
            elif parsed.path == "/api/impact":
                raw_files = query.get("files", [""])[0]
                files = [f.strip() for f in raw_files.split(",") if f.strip()]
                depth = int(query.get("depth", ["2"])[0])
                payload = self.service.impact(files, max_depth=depth)
                operation = "impact"
            elif parsed.path == "/api/graph":
                limit = int(query.get("limit", ["1000"])[0])
                payload = self.service.graph(limit=limit)
                operation = "graph"
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if operation:
                self._record_telemetry(operation, payload)
            self._send_json(payload)
        except Exception as exc:
            self._send_json({"status": "error", "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _send_html(self, content: str) -> None:
        data = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _record_telemetry(self, operation: str, payload: dict[str, Any]) -> None:
        try:
            self.service.record_telemetry(operation, payload, surface="web")
        except Exception:
            return

    def _send_events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        db_path = Path(self.service.db_path)
        last_mtime = db_path.stat().st_mtime if db_path.exists() else 0.0
        while True:
            try:
                time.sleep(1)
                mtime = db_path.stat().st_mtime if db_path.exists() else 0.0
                if mtime != last_mtime:
                    last_mtime = mtime
                    self.wfile.write(b"event: graph-updated\n")
                    self.wfile.write(b"data: {}\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break


def run_web(
    repo_root: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
) -> None:
    service = GraphService(repo_root)
    handler = type("BoundAxonWebHandler", (AxonWebHandler,), {"service": service})
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"axon-web serving {service.repo_root} at {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        service.close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="axon-web")
    parser.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8765, help="Bind port")
    parser.add_argument("--open", action="store_true", dest="open_browser")
    args = parser.parse_args()
    run_web(
        repo_root=args.repo,
        host=args.host,
        port=args.port,
        open_browser=args.open_browser,
    )
