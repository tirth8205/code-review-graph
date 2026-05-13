/**
 * Webview panel for the interactive graph visualization.
 * Uses D3.js (bundled via esbuild) to render a force-directed graph.
 *
 * Hosts the toolbar HTML, CSS, and manages communication with the
 * browser-side graph.ts script.
 */

import * as vscode from "vscode";
import * as path from "node:path";
import * as crypto from "node:crypto";
import type { SqliteReader, ImpactRadius } from "../backend/sqlite";

export class GraphWebviewPanel {
  private static currentPanel: GraphWebviewPanel | undefined;
  private readonly panel: vscode.WebviewPanel;
  private readonly reader: SqliteReader;
  private readonly impactRadius?: ImpactRadius;
  private readonly highlightQualifiedName?: string;
  private disposables: vscode.Disposable[] = [];

  private constructor(
    panel: vscode.WebviewPanel,
    extensionUri: vscode.Uri,
    reader: SqliteReader,
    impactRadius?: ImpactRadius,
    highlightQualifiedName?: string
  ) {
    this.panel = panel;
    this.reader = reader;
    this.impactRadius = impactRadius;
    this.highlightQualifiedName = highlightQualifiedName;

    this.panel.onDidDispose(() => this.dispose(), null, this.disposables);

    this.panel.webview.html = this.getHtmlContent(
      this.panel.webview,
      extensionUri
    );

    this.panel.webview.onDidReceiveMessage(
      (message) => this.handleMessage(message),
      null,
      this.disposables
    );

    // Listen for theme changes
    this.disposables.push(
      vscode.window.onDidChangeActiveColorTheme((theme) => {
        const themeKind =
          theme.kind === vscode.ColorThemeKind.Light ||
          theme.kind === vscode.ColorThemeKind.HighContrastLight
            ? "light"
            : "dark";
        this.panel.webview.postMessage({
          command: "setTheme",
          theme: themeKind,
        });
      })
    );
  }

  static createOrShow(
    extensionUri: vscode.Uri,
    reader: SqliteReader,
    impactRadius?: ImpactRadius,
    highlightQualifiedName?: string
  ): void {
    const column = vscode.ViewColumn.Beside;

    if (GraphWebviewPanel.currentPanel) {
      GraphWebviewPanel.currentPanel.panel.reveal(column);

      // Re-send data if a new highlight is requested
      if (highlightQualifiedName) {
        GraphWebviewPanel.currentPanel.panel.webview.postMessage({
          command: "highlightNode",
          qualifiedName: highlightQualifiedName,
        });
      }

      return;
    }

    const panel = vscode.window.createWebviewPanel(
      "codeReviewGraph.graph",
      "Code Graph",
      column,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [vscode.Uri.joinPath(extensionUri, "dist")],
      }
    );

    GraphWebviewPanel.currentPanel = new GraphWebviewPanel(
      panel,
      extensionUri,
      reader,
      impactRadius,
      highlightQualifiedName
    );
  }

  private dispose(): void {
    GraphWebviewPanel.currentPanel = undefined;
    this.panel.dispose();
    for (const d of this.disposables) {
      d.dispose();
    }
    this.disposables = [];
  }

  // -----------------------------------------------------------------------
  // Message handling
  // -----------------------------------------------------------------------

  private handleMessage(message: {
    command: string;
    [key: string]: unknown;
  }): void {
    switch (message.command) {
      case "ready":
        this.sendGraphData();
        break;

      case "nodeClicked":
        this.openFileAtLine(
          message.filePath as string,
          message.lineStart as number
        );
        // Bidirectional sync: reveal in tree view
        if (message.qualifiedName) {
          vscode.commands.executeCommand(
            "codeReviewGraph.revealInTree",
            message.qualifiedName as string
          );
        }
        break;

      case "exportSvg":
        this.exportSvgToClipboard(message.svg as string);
        break;

      case "exportPng":
        this.savePngToFile(message.data as string);
        break;
    }
  }

  /**
   * Send full graph data to the webview.
   * If an impact radius was provided, send only those nodes/edges.
   * Otherwise send the full graph.
   */
  private sendGraphData(): void {
    let nodes;
    let edges;

    if (this.impactRadius) {
      nodes = [
        ...this.impactRadius.changedNodes,
        ...this.impactRadius.impactedNodes,
      ];
      edges = this.impactRadius.edges;
    } else {
      // Load all nodes and edges
      const files = this.reader.getAllFiles();
      nodes = files.flatMap((f) => this.reader.getNodesByFile(f));
      const qualifiedNames = new Set(nodes.map((n) => n.qualifiedName));
      edges = this.reader.getEdgesAmong(qualifiedNames);
    }

    // Enforce maxNodes setting
    const config = vscode.workspace.getConfiguration("codeReviewGraph");
    const maxNodes = config.get<number>("graph.maxNodes", 500);
    let truncated = false;
    if (nodes.length > maxNodes) {
      truncated = true;
      nodes = nodes.slice(0, maxNodes);
      const nodeQns = new Set(nodes.map((n: { qualifiedName: string }) => n.qualifiedName));
      edges = edges.filter(
        (e: { sourceQualified: string; targetQualified: string }) =>
          nodeQns.has(e.sourceQualified) && nodeQns.has(e.targetQualified)
      );
    }

    this.panel.webview.postMessage({
      command: "setData",
      nodes,
      edges,
      truncated,
      maxNodes,
    });

    // Send theme
    const themeKind =
      vscode.window.activeColorTheme.kind === vscode.ColorThemeKind.Light ||
      vscode.window.activeColorTheme.kind ===
        vscode.ColorThemeKind.HighContrastLight
        ? "light"
        : "dark";
    this.panel.webview.postMessage({
      command: "setTheme",
      theme: themeKind,
    });

    // Highlight node if requested
    if (this.highlightQualifiedName) {
      // Small delay to let the graph render first
      setTimeout(() => {
        this.panel.webview.postMessage({
          command: "highlightNode",
          qualifiedName: this.highlightQualifiedName,
        });
      }, 1000);
    }
  }

  /**
   * Open a file in the editor at a specific line.
   */
  private async openFileAtLine(
    filePath: string,
    lineStart: number
  ): Promise<void> {
    const workspaceRoot =
      vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    const fullPath = workspaceRoot
      ? path.join(workspaceRoot, filePath)
      : filePath;

    try {
      const doc = await vscode.workspace.openTextDocument(fullPath);
      const line = Math.max(0, (lineStart ?? 1) - 1);
      await vscode.window.showTextDocument(doc, {
        viewColumn: vscode.ViewColumn.One,
        selection: new vscode.Range(line, 0, line, 0),
        preserveFocus: false,
      });
    } catch {
      vscode.window.showWarningMessage(
        `Code Graph: Could not open file ${filePath}`
      );
    }
  }

  /**
   * Copy SVG string to clipboard.
   */
  private async exportSvgToClipboard(svgString: string): Promise<void> {
    await vscode.env.clipboard.writeText(svgString);
    vscode.window.showInformationMessage(
      "Code Graph: SVG copied to clipboard."
    );
  }

  /**
   * Save PNG data URL to a file.
   */
  private async savePngToFile(dataUrl: string): Promise<void> {
    const uri = await vscode.window.showSaveDialog({
      defaultUri: vscode.Uri.file("code-graph.png"),
      filters: { "PNG Image": ["png"] },
    });
    if (!uri) { return; }

    const base64 = dataUrl.replace(/^data:image\/png;base64,/, "");
    const buffer = Buffer.from(base64, "base64");
    await vscode.workspace.fs.writeFile(uri, buffer);
    vscode.window.showInformationMessage("Code Graph: PNG saved.");
  }

  /**
   * Highlight a node by qualified name from external code (tree view click).
   */
  static highlightNode(qualifiedName: string): void {
    if (GraphWebviewPanel.currentPanel) {
      GraphWebviewPanel.currentPanel.panel.webview.postMessage({
        command: "highlightNode",
        qualifiedName,
      });
    }
  }

  // -----------------------------------------------------------------------
  // HTML content
  // -----------------------------------------------------------------------

  private getHtmlContent(
    webview: vscode.Webview,
    extensionUri: vscode.Uri
  ): string {
    const scriptUri = webview.asWebviewUri(
      vscode.Uri.joinPath(extensionUri, "dist", "webview", "graph.js")
    );

    const nonce = getNonce();

    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'nonce-${nonce}'; style-src 'unsafe-inline'; img-src ${webview.cspSource};">
  <title>Code Graph</title>
  <style>
    /* ------------------------------------------------------------------ */
    /* CSS variables from VS Code theme                                    */
    /* ------------------------------------------------------------------ */
    :root {
      --bg: var(--vscode-editor-background, #1e1e2e);
      --fg: var(--vscode-editor-foreground, #cdd6f4);
      --toolbar-bg: var(--vscode-sideBar-background, #181825);
      --toolbar-border: var(--vscode-panel-border, #313244);
      --input-bg: var(--vscode-input-background, #313244);
      --input-fg: var(--vscode-input-foreground, #cdd6f4);
      --input-border: var(--vscode-input-border, #45475a);
      --btn-bg: var(--vscode-button-background, #89b4fa);
      --btn-fg: var(--vscode-button-foreground, #1e1e2e);
      --btn-hover: var(--vscode-button-hoverBackground, #74c7ec);
      --badge-bg: var(--vscode-badge-background, #45475a);
      --badge-fg: var(--vscode-badge-foreground, #cdd6f4);
      --font: var(--vscode-font-family, 'Segoe UI', sans-serif);
      --font-size: var(--vscode-font-size, 13px);
      --font-mono: var(--vscode-editor-font-family, 'Fira Code', monospace);
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: var(--font);
      font-size: var(--font-size);
      background: var(--bg);
      color: var(--fg);
      overflow: hidden;
      width: 100vw;
      height: 100vh;
      display: flex;
      flex-direction: column;
    }

    /* ------------------------------------------------------------------ */
    /* Toolbar                                                             */
    /* ------------------------------------------------------------------ */
    #toolbar {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 6px 12px;
      background: var(--toolbar-bg);
      border-bottom: 1px solid var(--toolbar-border);
      flex-shrink: 0;
      flex-wrap: wrap;
      min-height: 40px;
    }

    #toolbar .toolbar-group {
      display: flex;
      align-items: center;
      gap: 6px;
    }

    #toolbar .toolbar-label {
      font-size: 11px;
      color: var(--fg);
      opacity: 0.7;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      white-space: nowrap;
    }

    #toolbar .toolbar-separator {
      width: 1px;
      height: 20px;
      background: var(--toolbar-border);
      margin: 0 4px;
    }

    /* Search */
    #search-input {
      background: var(--input-bg);
      color: var(--input-fg);
      border: 1px solid var(--input-border);
      border-radius: 4px;
      padding: 4px 8px;
      font-size: 12px;
      font-family: var(--font);
      width: 180px;
      outline: none;
    }
    #search-input:focus {
      border-color: var(--btn-bg);
    }
    #search-input::placeholder {
      color: var(--fg);
      opacity: 0.4;
    }

    /* Edge pills */
    .edge-pill {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 2px 8px;
      border-radius: 10px;
      font-size: 10px;
      font-weight: 600;
      cursor: pointer;
      user-select: none;
      border: 1px solid transparent;
      opacity: 0.55;
      transition: opacity 0.15s, border-color 0.15s;
    }
    .edge-pill.active {
      opacity: 1;
      border-color: currentColor;
    }
    .edge-pill:focus-visible { outline: 2px solid var(--btn-bg, #89b4fa); outline-offset: 2px; }
    .edge-pill .pill-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
    }

    /* Edge filter popover */
    #edge-popover {
      display: none;
      position: absolute;
      top: 100%;
      left: 0;
      margin-top: 4px;
      background: var(--toolbar-bg);
      border: 1px solid var(--toolbar-border);
      border-radius: 6px;
      padding: 8px 12px;
      z-index: 100;
      box-shadow: 0 4px 16px rgba(0,0,0,0.3);
      min-width: 160px;
    }
    #edge-popover.visible { display: flex; flex-wrap: wrap; gap: 6px; }
    #edge-filter-wrap { position: relative; }

    /* Depth slider */
    #depth-slider {
      width: 80px;
      accent-color: var(--btn-bg);
      cursor: pointer;
    }
    #depth-slider:disabled {
      opacity: 0.4;
      cursor: not-allowed;
    }
    #depth-value {
      font-size: 11px;
      font-family: var(--font-mono);
      min-width: 24px;
      text-align: center;
    }

    /* Toolbar buttons */
    .toolbar-btn {
      background: var(--badge-bg);
      color: var(--badge-fg);
      border: none;
      border-radius: 4px;
      padding: 4px 10px;
      font-size: 11px;
      font-family: var(--font);
      cursor: pointer;
      white-space: nowrap;
      transition: background 0.15s;
    }
    .toolbar-btn:hover {
      background: var(--btn-bg);
      color: var(--btn-fg);
    }

    /* Node count badge */
    #node-count {
      position: absolute;
      bottom: 8px;
      right: 12px;
      font-size: 11px;
      color: var(--fg);
      opacity: 0.6;
      white-space: nowrap;
      z-index: 5;
    }

    /* ------------------------------------------------------------------ */
    /* Graph area                                                          */
    /* ------------------------------------------------------------------ */
    #graph-area {
      flex: 1;
      overflow: hidden;
      position: relative;
    }
    #graph-area svg {
      display: block;
    }

    /* ------------------------------------------------------------------ */
    /* Tooltip                                                             */
    /* ------------------------------------------------------------------ */
    #tooltip {
      display: none;
      position: fixed;
      z-index: 1000;
      background: var(--toolbar-bg);
      border: 1px solid var(--toolbar-border);
      border-radius: 6px;
      padding: 8px 12px;
      font-size: 12px;
      color: var(--fg);
      max-width: 360px;
      pointer-events: none;
      box-shadow: 0 4px 12px rgba(0,0,0,0.3);
      line-height: 1.5;
    }
    #tooltip strong {
      font-size: 13px;
    }
    #tooltip .tooltip-kind {
      display: inline-block;
      font-size: 10px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      padding: 1px 6px;
      border-radius: 3px;
      background: var(--badge-bg);
      color: var(--badge-fg);
      margin: 2px 0;
    }
    #tooltip .tooltip-path {
      font-family: var(--font-mono);
      font-size: 11px;
      opacity: 0.7;
      word-break: break-all;
    }
    #tooltip .tooltip-params {
      font-family: var(--font-mono);
      font-size: 11px;
      color: var(--vscode-debugTokenExpression-string, #a6e3a1);
    }
    #tooltip .tooltip-return {
      font-family: var(--font-mono);
      font-size: 11px;
      color: var(--vscode-debugTokenExpression-number, #89b4fa);
    }

    /* ------------------------------------------------------------------ */
    /* Pulse animation for highlighted nodes                               */
    /* ------------------------------------------------------------------ */
    @keyframes pulse {
      0% { r: 16; stroke-opacity: 1; }
      100% { r: 30; stroke-opacity: 0; }
    }
    .pulse-ring {
      animation: pulse 0.6s ease-out;
    }

    path.node-shape:focus { outline: none; }
    path.node-shape:focus-visible { stroke: var(--btn-bg, #89b4fa) !important; stroke-width: 3 !important; }
  </style>
</head>
<body>
  <!-- Toolbar -->
  <div id="toolbar">
    <!-- Search -->
    <div class="toolbar-group">
      <input id="search-input" type="text" placeholder="Search nodes..." spellcheck="false" aria-label="Search nodes" />
    </div>

    <div class="toolbar-separator"></div>

    <!-- Edge type filter -->
    <div class="toolbar-group" id="edge-filter-wrap">
      <button id="btn-edge-filter" class="toolbar-btn" title="Toggle edge type filters" aria-label="Toggle edge type filters">Edges</button>
      <div id="edge-popover">
        <span id="edge-CALLS" class="edge-pill active" style="color:#3fb950" role="button" tabindex="0" aria-pressed="true" aria-label="Toggle Calls edges" title="Toggle CALLS edges"><span class="pill-dot" style="background:#3fb950"></span>Calls</span>
        <span id="edge-IMPORTS_FROM" class="edge-pill active" style="color:#f0883e" role="button" tabindex="0" aria-pressed="true" aria-label="Toggle Imports edges" title="Toggle IMPORTS_FROM edges"><span class="pill-dot" style="background:#f0883e"></span>Imports</span>
        <span id="edge-INHERITS" class="edge-pill active" style="color:#d2a8ff" role="button" tabindex="0" aria-pressed="true" aria-label="Toggle Inherits edges" title="Toggle INHERITS edges"><span class="pill-dot" style="background:#d2a8ff"></span>Inherits</span>
        <span id="edge-IMPLEMENTS" class="edge-pill active" style="color:#f9e2af" role="button" tabindex="0" aria-pressed="true" aria-label="Toggle Implements edges" title="Toggle IMPLEMENTS edges"><span class="pill-dot" style="background:#f9e2af"></span>Implements</span>
        <span id="edge-TESTED_BY" class="edge-pill active" style="color:#f38ba8" role="button" tabindex="0" aria-pressed="true" aria-label="Toggle Tested edges" title="Toggle TESTED_BY edges"><span class="pill-dot" style="background:#f38ba8"></span>Tested</span>
        <span id="edge-CONTAINS" class="edge-pill active" style="color:#8b949e" role="button" tabindex="0" aria-pressed="true" aria-label="Toggle Contains edges" title="Toggle CONTAINS edges"><span class="pill-dot" style="background:#8b949e"></span>Contains</span>
        <span id="edge-DEPENDS_ON" class="edge-pill active" style="color:#fab387" role="button" tabindex="0" aria-pressed="true" aria-label="Toggle Depends edges" title="Toggle DEPENDS_ON edges"><span class="pill-dot" style="background:#fab387"></span>Depends</span>
      </div>
    </div>

    <div class="toolbar-separator"></div>

    <!-- Depth slider -->
    <div class="toolbar-group">
      <span class="toolbar-label">Depth</span>
      <input id="depth-slider" type="range" min="0" max="10" value="0" aria-label="Graph depth limit" disabled title="Select a node first, then adjust depth" />
      <span id="depth-value">All</span>
    </div>

    <div class="toolbar-separator"></div>

    <!-- Action buttons -->
    <div class="toolbar-group">
      <button id="btn-fit" class="toolbar-btn">Fit</button>
      <button id="btn-export" class="toolbar-btn">Export SVG</button>
      <button id="btn-export-png" class="toolbar-btn">Export PNG</button>
    </div>

  </div>

  <!-- Graph -->
  <div id="graph-area">
    <span id="node-count" aria-live="polite"></span>
    <span id="truncation-warning" style="display:none;color:var(--btn-bg);font-size:11px;cursor:pointer;" title="Increase codeReviewGraph.graph.maxNodes in settings"></span>
    <div id="empty-state" style="display:none;position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);text-align:center;color:var(--fg);opacity:0.6">
      <p style="font-size:16px;margin-bottom:12px">No graph data available</p>
      <p style="font-size:13px">Run <strong>Code Graph: Build Graph</strong> from the Command Palette to get started.</p>
    </div>
  </div>

  <!-- Tooltip -->
  <div id="tooltip" role="tooltip" aria-live="polite"></div>

  <script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
  }
}

function getNonce(): string {
  return crypto.randomBytes(16).toString("hex");
}
