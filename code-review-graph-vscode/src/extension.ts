import * as vscode from "vscode";
import * as path from "node:path";
import * as fs from "node:fs";
import type { ChildProcess } from "node:child_process";

import { SqliteReader } from "./backend/sqlite";
import type { GraphNode } from "./backend/sqlite";
import { CliWrapper } from "./backend/cli";
import {
  CodeGraphTreeProvider,
  BlastRadiusTreeProvider,
  StatsTreeProvider,
} from "./views/treeView";
import { GraphWebviewPanel } from "./views/graphWebview";
import { Installer } from "./onboarding/installer";
import { registerWalkthroughCommands, showWelcomeIfNeeded } from "./onboarding/welcome";
import { StatusBar } from "./views/statusBar";
import { ScmDecorationProvider } from "./features/scmDecorations";

let sqliteReader: SqliteReader | undefined;
let autoUpdateTimer: ReturnType<typeof setTimeout> | undefined;
let scmDecorationProvider: ScmDecorationProvider | undefined;
let watchProcess: ChildProcess | undefined;
let watchOutput: vscode.OutputChannel | undefined;

/**
 * Locate the graph database file in the workspace.
 * Checks `.code-review-graph/graph.db` first, then falls back to `.code-review-graph.db`.
 */
function findGraphDb(workspaceRoot: string): string | undefined {
  const primary = path.join(workspaceRoot, ".code-review-graph", "graph.db");
  if (fs.existsSync(primary)) {
    return primary;
  }

  const fallback = path.join(workspaceRoot, ".code-review-graph.db");
  if (fs.existsSync(fallback)) {
    return fallback;
  }

  return undefined;
}

/**
 * Get the workspace root folder path, or undefined if no workspace is open.
 * Checks all workspace folders for a graph database (multi-root support).
 */
function getWorkspaceRoot(): string | undefined {
  const folders = vscode.workspace.workspaceFolders;
  if (!folders) { return undefined; }

  // Prefer the folder that has a graph database
  for (const folder of folders) {
    if (findGraphDb(folder.uri.fsPath)) {
      return folder.uri.fsPath;
    }
  }

  // Fall back to first folder
  return folders[0]?.uri.fsPath;
}


/**
 * Navigate to a node's source file location.
 */
async function navigateToNode(node: GraphNode): Promise<void> {
  const workspaceRoot = getWorkspaceRoot();
  const filePath = workspaceRoot
    ? path.join(workspaceRoot, node.filePath)
    : node.filePath;

  const doc = await vscode.workspace.openTextDocument(filePath);
  const line = Math.max(0, (node.lineStart ?? 1) - 1);
  await vscode.window.showTextDocument(doc, {
    selection: new vscode.Range(line, 0, line, 0),
  });
}

/**
 * Register all extension commands.
 */
function registerCommands(
  context: vscode.ExtensionContext,
  cli: CliWrapper
): void {
  context.subscriptions.push(
    vscode.commands.registerCommand(
      "codeReviewGraph.buildGraph",
      async () => {
        const workspaceRoot = getWorkspaceRoot();
        if (!workspaceRoot) {
          vscode.window.showErrorMessage("No workspace folder is open.");
          return;
        }

        const result = await cli.buildGraph(workspaceRoot);
        if (result.success) {
          await reinitialize(context);
          vscode.window.showInformationMessage("Code Graph: Build complete.");
        } else {
          vscode.window.showErrorMessage(
            `Code Graph: Build failed. ${result.stderr}`
          );
        }
      }
    )
  );

  context.subscriptions.push(
    vscode.commands.registerCommand(
      "codeReviewGraph.updateGraph",
      async () => {
        const workspaceRoot = getWorkspaceRoot();
        if (!workspaceRoot) {
          vscode.window.showErrorMessage("No workspace folder is open.");
          return;
        }

        await vscode.window.withProgress(
          {
            location: vscode.ProgressLocation.Notification,
            title: "Code Graph: Updating graph...",
            cancellable: false,
          },
          async () => {
            const result = await cli.updateGraph(workspaceRoot);
            if (!result.success) {
              vscode.window.showErrorMessage(
                `Code Graph: Update failed. ${result.stderr}`
              );
            }
          }
        );
      }
    )
  );

  context.subscriptions.push(
    vscode.commands.registerCommand(
      "codeReviewGraph.showBlastRadius",
      async (qualifiedNameOrUri?: string | vscode.Uri) => {
        if (!sqliteReader) {
          vscode.window.showWarningMessage(
            "Code Graph: No graph database loaded."
          );
          return;
        }

        let qualifiedName: string | undefined;
        if (typeof qualifiedNameOrUri === "string") {
          qualifiedName = qualifiedNameOrUri;
        } else {
          qualifiedName = await vscode.window.showInputBox({
            prompt:
              "Enter the qualified name (e.g., my_module.MyClass.my_method)",
            placeHolder: "my_module.my_function",
          });
        }

        if (!qualifiedName) {
          return;
        }

        // Find the file for this node and compute impact radius
        const node = sqliteReader.getNode(qualifiedName);
        if (!node) {
          vscode.window.showInformationMessage(
            `Code Graph: Node "${qualifiedName}" not found.`
          );
          return;
        }

        const config = vscode.workspace.getConfiguration("codeReviewGraph");
        const depth = config.get<number>("blastRadiusDepth", 2);
        const impact = sqliteReader.getImpactRadius([node.filePath], depth);

        if (impact.impactedNodes.length === 0) {
          vscode.window.showInformationMessage(
            `Code Graph: No blast radius found for "${qualifiedName}".`
          );
          return;
        }

        await vscode.commands.executeCommand(
          "codeReviewGraph.blastRadius.focus"
        );
      }
    )
  );

  context.subscriptions.push(
    vscode.commands.registerCommand(
      "codeReviewGraph.findCallers",
      async (qualifiedName?: string) => {
        if (!sqliteReader) {
          vscode.window.showWarningMessage(
            "Code Graph: No graph database loaded."
          );
          return;
        }

        if (!qualifiedName) {
          qualifiedName = await vscode.window.showInputBox({
            prompt: "Enter the qualified name to find callers for",
            placeHolder: "my_module.my_function",
          });
        }

        if (!qualifiedName) {
          return;
        }

        const edges = sqliteReader.getEdgesByTarget(qualifiedName);
        const callerEdges = edges.filter((e) => e.kind === "CALLS");

        if (callerEdges.length === 0) {
          vscode.window.showInformationMessage(
            `Code Graph: No callers found for "${qualifiedName}".`
          );
          return;
        }

        const items = callerEdges.map((e) => {
          const callerNode = sqliteReader!.getNode(e.sourceQualified);
          return {
            label: callerNode?.name ?? e.sourceQualified,
            description: callerNode?.filePath ?? e.filePath,
            detail: `Line ${callerNode?.lineStart ?? e.line}`,
            node: callerNode,
            edge: e,
          };
        });

        const selected = await vscode.window.showQuickPick(items, {
          placeHolder: `Callers of ${qualifiedName}`,
        });

        if (selected?.node) {
          await navigateToNode(selected.node);
        }
      }
    )
  );

  context.subscriptions.push(
    vscode.commands.registerCommand(
      "codeReviewGraph.findTests",
      async (qualifiedName?: string) => {
        if (!sqliteReader) {
          vscode.window.showWarningMessage(
            "Code Graph: No graph database loaded."
          );
          return;
        }

        if (!qualifiedName) {
          qualifiedName = await vscode.window.showInputBox({
            prompt: "Enter the qualified name to find tests for",
            placeHolder: "my_module.my_function",
          });
        }

        if (!qualifiedName) {
          return;
        }

        // Find tests via TESTED_BY edges
        const edges = sqliteReader.getEdgesByTarget(qualifiedName);
        const testEdges = edges.filter((e) => e.kind === "TESTED_BY");

        // Also check reverse: source is the node, target is the test
        const outEdges = sqliteReader.getEdgesBySource(qualifiedName);
        const outTestEdges = outEdges.filter((e) => e.kind === "TESTED_BY");

        const allTestQualifiedNames = new Set([
          ...testEdges.map((e) => e.sourceQualified),
          ...outTestEdges.map((e) => e.targetQualified),
        ]);

        if (allTestQualifiedNames.size === 0) {
          vscode.window.showInformationMessage(
            `Code Graph: No tests found for "${qualifiedName}".`
          );
          return;
        }

        const items: Array<{
          label: string;
          description: string;
          detail: string;
          node: GraphNode | undefined;
        }> = [];

        for (const tqn of allTestQualifiedNames) {
          const testNode = sqliteReader.getNode(tqn);
          items.push({
            label: testNode?.name ?? tqn,
            description: testNode?.filePath ?? "",
            detail: `Line ${testNode?.lineStart ?? "?"}`,
            node: testNode,
          });
        }

        const selected = await vscode.window.showQuickPick(items, {
          placeHolder: `Tests for ${qualifiedName}`,
        });

        if (selected?.node) {
          await navigateToNode(selected.node);
        }
      }
    )
  );

  // -----------------------------------------------------------------
  // codeReviewGraph.queryGraph — expose all 8 query patterns
  // -----------------------------------------------------------------
  context.subscriptions.push(
    vscode.commands.registerCommand("codeReviewGraph.queryGraph", async () => {
      if (!sqliteReader) {
        vscode.window.showWarningMessage("Code Graph: No graph database loaded.");
        return;
      }

      const patterns = [
        { label: "callers_of", description: "Find functions calling the target" },
        { label: "callees_of", description: "Find functions called by the target" },
        { label: "imports_of", description: "Find modules imported by a file" },
        { label: "importers_of", description: "Find files importing from the target" },
        { label: "children_of", description: "Find nodes contained in a file or class" },
        { label: "tests_for", description: "Find tests for a function or class" },
        { label: "inheritors_of", description: "Find classes inheriting/implementing the target" },
        { label: "file_summary", description: "List all nodes in a file" },
      ];

      const pattern = await vscode.window.showQuickPick(patterns, {
        placeHolder: "Select a query pattern",
      });
      if (!pattern) { return; }

      const target = await vscode.window.showInputBox({
        prompt: `Enter the target for ${pattern.label}`,
        placeHolder: "e.g., my_module.py::my_function or path/to/file.py",
      });
      if (!target) { return; }

      // Map pattern to edge kind + direction
      type QueryDef = { edgeKind: string; direction: "incoming" | "outgoing"; nodeFilter?: string };
      const queryMap: Record<string, QueryDef> = {
        callers_of: { edgeKind: "CALLS", direction: "incoming" },
        callees_of: { edgeKind: "CALLS", direction: "outgoing" },
        imports_of: { edgeKind: "IMPORTS_FROM", direction: "outgoing" },
        importers_of: { edgeKind: "IMPORTS_FROM", direction: "incoming" },
        children_of: { edgeKind: "CONTAINS", direction: "outgoing" },
        tests_for: { edgeKind: "TESTED_BY", direction: "incoming" },
        inheritors_of: { edgeKind: "INHERITS", direction: "incoming" },
        file_summary: { edgeKind: "CONTAINS", direction: "outgoing" },
      };

      const qdef = queryMap[pattern.label];
      if (!qdef) { return; }

      // Try exact match, then search
      let node = sqliteReader.getNode(target);
      if (!node) {
        const matches = sqliteReader.searchNodes(target, 5);
        if (matches.length === 1) {
          node = matches[0];
        } else if (matches.length > 1) {
          const selected = await vscode.window.showQuickPick(
            matches.map(m => ({
              label: m.name,
              description: `${m.kind} · ${m.filePath}`,
              node: m,
            })),
            { placeHolder: `Multiple matches for "${target}" — select one` },
          );
          if (!selected) { return; }
          node = selected.node;
        }
      }

      if (!node) {
        vscode.window.showInformationMessage(`Code Graph: "${target}" not found.`);
        return;
      }

      const edges = qdef.direction === "incoming"
        ? sqliteReader.getEdgesByTarget(node.qualifiedName)
        : sqliteReader.getEdgesBySource(node.qualifiedName);

      const filtered = edges.filter(e => e.kind === qdef.edgeKind);

      if (filtered.length === 0) {
        vscode.window.showInformationMessage(
          `Code Graph: No ${pattern.label} results for "${node.name}".`
        );
        return;
      }

      const items = filtered.map(e => {
        const relatedQn = qdef.direction === "incoming" ? e.sourceQualified : e.targetQualified;
        const relatedNode = sqliteReader!.getNode(relatedQn);
        return {
          label: relatedNode?.name ?? relatedQn,
          description: relatedNode ? `${relatedNode.kind} · ${relatedNode.filePath}` : "",
          detail: `Line ${relatedNode?.lineStart ?? e.line}`,
          node: relatedNode,
        };
      });

      const selected = await vscode.window.showQuickPick(items, {
        placeHolder: `${pattern.label}: ${node.name} (${filtered.length} results)`,
      });

      if (selected?.node) {
        await navigateToNode(selected.node);
      }
    })
  );

  // -----------------------------------------------------------------
  // codeReviewGraph.findCallees
  // -----------------------------------------------------------------
  context.subscriptions.push(
    vscode.commands.registerCommand(
      "codeReviewGraph.findCallees",
      async (qualifiedName?: string) => {
        if (!sqliteReader) {
          vscode.window.showWarningMessage("Code Graph: No graph database loaded.");
          return;
        }

        if (!qualifiedName) {
          qualifiedName = await vscode.window.showInputBox({
            prompt: "Enter the qualified name to find callees for",
            placeHolder: "my_module.my_function",
          });
        }
        if (!qualifiedName) { return; }

        const edges = sqliteReader.getEdgesBySource(qualifiedName);
        const calleeEdges = edges.filter(e => e.kind === "CALLS");

        if (calleeEdges.length === 0) {
          vscode.window.showInformationMessage(
            `Code Graph: No callees found for "${qualifiedName}".`
          );
          return;
        }

        const items = calleeEdges.map(e => {
          const calleeNode = sqliteReader!.getNode(e.targetQualified);
          return {
            label: calleeNode?.name ?? e.targetQualified,
            description: calleeNode?.filePath ?? e.filePath,
            detail: `Line ${calleeNode?.lineStart ?? e.line}`,
            node: calleeNode,
          };
        });

        const selected = await vscode.window.showQuickPick(items, {
          placeHolder: `Callees of ${qualifiedName}`,
        });

        if (selected?.node) {
          await navigateToNode(selected.node);
        }
      }
    )
  );

  // -----------------------------------------------------------------
  // codeReviewGraph.findLargeFunctions
  // -----------------------------------------------------------------
  context.subscriptions.push(
    vscode.commands.registerCommand(
      "codeReviewGraph.findLargeFunctions",
      async () => {
        if (!sqliteReader) {
          vscode.window.showWarningMessage("Code Graph: No graph database loaded.");
          return;
        }

        const minLinesStr = await vscode.window.showInputBox({
          prompt: "Minimum line count threshold",
          placeHolder: "50",
          value: "50",
        });
        if (!minLinesStr) { return; }

        const minLines = parseInt(minLinesStr, 10);
        if (isNaN(minLines) || minLines < 1) {
          vscode.window.showWarningMessage("Code Graph: Invalid line count.");
          return;
        }

        const results = sqliteReader.getNodesBySize(minLines, undefined, undefined, 50);

        if (results.length === 0) {
          vscode.window.showInformationMessage(
            `Code Graph: No functions found with ${minLines}+ lines.`
          );
          return;
        }

        const items = results.map(r => ({
          label: `${r.name} (${r.lineCount} lines)`,
          description: `${r.kind} · ${r.filePath}`,
          detail: `Lines ${r.lineStart ?? "?"}–${r.lineEnd ?? "?"}`,
          node: r as GraphNode,
        }));

        const selected = await vscode.window.showQuickPick(items, {
          placeHolder: `${results.length} nodes with ${minLines}+ lines`,
        });

        if (selected?.node) {
          await navigateToNode(selected.node);
        }
      }
    )
  );

  // -----------------------------------------------------------------
  // codeReviewGraph.embedGraph
  // -----------------------------------------------------------------
  context.subscriptions.push(
    vscode.commands.registerCommand("codeReviewGraph.embedGraph", async () => {
      const workspaceRoot = getWorkspaceRoot();
      if (!workspaceRoot) {
        vscode.window.showErrorMessage("No workspace folder is open.");
        return;
      }

      const result = await cli.embedGraph(workspaceRoot);
      if (result.success) {
        vscode.window.showInformationMessage("Code Graph: Embeddings computed.");
      } else {
        const msg = result.stderr.includes("not installed")
          ? "Install embeddings support: pip install code-review-graph[embeddings]"
          : `Embedding failed: ${result.stderr}`;
        vscode.window.showErrorMessage(`Code Graph: ${msg}`);
      }
    })
  );

  // -----------------------------------------------------------------
  // codeReviewGraph.watchGraph
  // -----------------------------------------------------------------
  context.subscriptions.push(
    vscode.commands.registerCommand("codeReviewGraph.watchGraph", async () => {
      const workspaceRoot = getWorkspaceRoot();
      if (!workspaceRoot) {
        vscode.window.showErrorMessage("No workspace folder is open.");
        return;
      }

      if (watchProcess) {
        watchProcess.kill();
        watchProcess = undefined;
        vscode.window.showInformationMessage("Code Graph: Watch mode stopped.");
        return;
      }

      watchOutput ??= vscode.window.createOutputChannel("Code Graph Watch");
      watchOutput.appendLine(`Starting watch mode in ${workspaceRoot}`);
      watchProcess = cli.startWatchGraph(workspaceRoot);

      watchProcess.stdout?.on("data", (data) => {
        watchOutput?.append(data.toString());
      });
      watchProcess.stderr?.on("data", (data) => {
        watchOutput?.append(data.toString());
      });
      watchProcess.on("error", (error) => {
        watchProcess = undefined;
        vscode.window.showErrorMessage(`Code Graph: Watch failed. ${error.message}`);
      });
      watchProcess.on("close", (code, signal) => {
        const expectedStop = signal === "SIGTERM" || signal === "SIGKILL";
        watchProcess = undefined;
        watchOutput?.appendLine(`Watch stopped with code=${code} signal=${signal ?? ""}`);
        if (code && !expectedStop) {
          vscode.window.showWarningMessage(`Code Graph: Watch stopped with code ${code}.`);
        }
      });

      vscode.window.showInformationMessage("Code Graph: Watch mode started.");
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("codeReviewGraph.showGraph", async () => {
      if (!sqliteReader) {
        vscode.window.showWarningMessage(
          "Code Graph: No graph database loaded. Run 'Code Graph: Build Graph' first."
        );
        return;
      }

      GraphWebviewPanel.createOrShow(context.extensionUri, sqliteReader);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("codeReviewGraph.search", async () => {
      if (!sqliteReader) {
        vscode.window.showWarningMessage(
          "Code Graph: No graph database loaded."
        );
        return;
      }

      const query = await vscode.window.showInputBox({
        prompt: "Search the code graph",
        placeHolder: "Enter a function, class, or module name",
      });

      if (!query) {
        return;
      }

      const results = sqliteReader.searchNodes(query);

      if (results.length === 0) {
        vscode.window.showInformationMessage(
          `Code Graph: No results found for "${query}".`
        );
        return;
      }

      const items = results.map((r) => ({
        label: r.name,
        description: r.kind,
        detail: r.filePath
          ? `${r.filePath}:${r.lineStart ?? ""}`
          : undefined,
        result: r,
      }));

      const selected = await vscode.window.showQuickPick(items, {
        placeHolder: `Results for "${query}"`,
      });

      if (selected?.result) {
        await navigateToNode(selected.result);
      }
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand(
      "codeReviewGraph.reviewChanges",
      async () => {
        if (!sqliteReader) {
          vscode.window.showWarningMessage(
            "Code Graph: No graph database loaded."
          );
          return;
        }

        const workspaceRoot = getWorkspaceRoot();
        if (!workspaceRoot) {
          vscode.window.showErrorMessage("No workspace folder is open.");
          return;
        }

        await vscode.window.withProgress(
          {
            location: vscode.ProgressLocation.Notification,
            title: "Code Graph: Analyzing changes...",
            cancellable: false,
          },
          async () => {
            const { execFile } = await import("node:child_process");
            const { promisify } = await import("node:util");
            const execFileAsync = promisify(execFile);

            let changedFiles: string[] = [];
            try {
              const r1 = await execFileAsync(
                "git", ["diff", "--name-only", "HEAD"],
                { cwd: workspaceRoot }
              );
              const r2 = await execFileAsync(
                "git", ["diff", "--cached", "--name-only"],
                { cwd: workspaceRoot }
              );
              changedFiles = [...new Set([
                ...r1.stdout.trim().split("\n").filter(Boolean),
                ...r2.stdout.trim().split("\n").filter(Boolean),
              ])];
            } catch {
              // git not available or not a git repo
            }

            if (changedFiles.length === 0) {
              vscode.window.showInformationMessage(
                "Code Graph: No changes detected."
              );
              return;
            }

            const absFiles = changedFiles.map(f => path.join(workspaceRoot, f));
            const impact = sqliteReader!.getImpactRadius(absFiles);

            // --- Generate review guidance ---
            const guidance: string[] = [];
            const impactedFileCount = new Set(
              impact.impactedNodes.map(n => n.filePath)
            ).size;

            // Test coverage check
            const untestedFns: string[] = [];
            for (const node of impact.changedNodes) {
              if (node.kind !== "Function" || node.isTest) { continue; }
              const edges = sqliteReader!.getEdgesByTarget(node.qualifiedName);
              const hasCoverage = edges.some(e => e.kind === "TESTED_BY");
              if (!hasCoverage) {
                const out = sqliteReader!.getEdgesBySource(node.qualifiedName);
                if (!out.some(e => e.kind === "TESTED_BY")) {
                  untestedFns.push(node.name);
                }
              }
            }

            if (untestedFns.length > 0) {
              guidance.push(
                `\u26a0\ufe0f **${untestedFns.length} changed function(s) lack test coverage**: ${untestedFns.slice(0, 5).join(", ")}${untestedFns.length > 5 ? "..." : ""}`
              );
            }

            // Wide blast radius warning
            if (impactedFileCount > 10) {
              guidance.push(
                `\u26a0\ufe0f **Wide blast radius**: ${impactedFileCount} files impacted — consider splitting this change.`
              );
            }

            // Inheritance changes
            const inheritanceChanges = impact.edges.filter(
              e => e.kind === "INHERITS" || e.kind === "IMPLEMENTS"
            );
            if (inheritanceChanges.length > 0) {
              guidance.push(
                `\u26a0\ufe0f **Inheritance chain affected**: ${inheritanceChanges.length} inheritance/implementation edge(s) touched.`
              );
            }

            // Cross-file impacts
            if (impact.impactedNodes.length > 0) {
              guidance.push(
                `\u2139\ufe0f ${impact.impactedNodes.length} nodes in ${impactedFileCount} file(s) may be affected by these changes.`
              );
            }

            // Show guidance in output channel
            const channel = vscode.window.createOutputChannel("Code Graph Review", { log: true });
            channel.appendLine("# Review Guidance");
            channel.appendLine("");
            channel.appendLine(`Changed files: ${changedFiles.length}`);
            channel.appendLine(`Changed nodes: ${impact.changedNodes.length}`);
            channel.appendLine(`Impacted nodes: ${impact.impactedNodes.length}`);
            channel.appendLine(`Impacted files: ${impactedFileCount}`);
            channel.appendLine("");
            if (guidance.length > 0) {
              for (const g of guidance) { channel.appendLine(g); }
            } else {
              channel.appendLine("\u2705 No concerns detected.");
            }
            channel.show(true);

            // Also show in graph
            GraphWebviewPanel.createOrShow(
              context.extensionUri,
              sqliteReader!,
              impact
            );

            // Update SCM decorations
            if (scmDecorationProvider && sqliteReader) {
              await scmDecorationProvider.update(sqliteReader, workspaceRoot);
            }
          }
        );
      }
    )
  );
}

/**
 * Reinitialize the reader and tree providers after a graph rebuild.
 */
async function reinitialize(
  context: vscode.ExtensionContext
): Promise<void> {
  const workspaceRoot = getWorkspaceRoot();
  if (!workspaceRoot) {
    return;
  }

  const dbPath = findGraphDb(workspaceRoot);
  if (!dbPath) {
    return;
  }

  sqliteReader?.close();
  sqliteReader = new SqliteReader(dbPath);

  // Refresh tree views
  await vscode.commands.executeCommand(
    "codeReviewGraph.codeGraph.refresh"
  );
}

/**
 * Set up a FileSystemWatcher to detect changes to the graph database.
 */
function watchGraphDb(context: vscode.ExtensionContext): void {
  const watcher = vscode.workspace.createFileSystemWatcher(
    "**/.code-review-graph/graph.db"
  );

  const dbPathRef = { current: "" };
  const workspaceRoot = getWorkspaceRoot();
  if (workspaceRoot) {
    const dbPath = findGraphDb(workspaceRoot);
    if (dbPath) {
      dbPathRef.current = dbPath;
    }
  }

  watcher.onDidChange(() => {
    // Close and reopen to pick up external writes
    if (sqliteReader && dbPathRef.current) {
      sqliteReader.close();
      sqliteReader = new SqliteReader(dbPathRef.current);
      vscode.commands.executeCommand("codeReviewGraph.codeGraph.refresh");
    }
  });

  watcher.onDidCreate(async () => {
    const wsRoot = getWorkspaceRoot();
    if (wsRoot && !sqliteReader) {
      const dbPath = findGraphDb(wsRoot);
      if (dbPath) {
        dbPathRef.current = dbPath;
        sqliteReader = new SqliteReader(dbPath);
        vscode.commands.executeCommand("codeReviewGraph.codeGraph.refresh");
      }
    }
  });

  watcher.onDidDelete(() => {
    sqliteReader?.close();
    sqliteReader = undefined;
    dbPathRef.current = "";
  });

  context.subscriptions.push(watcher);
}

/**
 * Set up debounced auto-update on file save.
 */
function setupAutoUpdate(
  context: vscode.ExtensionContext,
  cli: CliWrapper
): void {
  const AUTO_UPDATE_DEBOUNCE_MS = 2000;

  const onSave = vscode.workspace.onDidSaveTextDocument(() => {
    const config = vscode.workspace.getConfiguration("codeReviewGraph");
    if (!config.get<boolean>("autoUpdate", true)) {
      return;
    }

    if (autoUpdateTimer) {
      clearTimeout(autoUpdateTimer);
    }

    autoUpdateTimer = setTimeout(async () => {
      const wsRoot = getWorkspaceRoot();
      if (!wsRoot || !sqliteReader) {
        return;
      }

      try {
        await cli.updateGraph(wsRoot);
      } catch {
        // Silently ignore update errors on save; user can manually update
      }
    }, AUTO_UPDATE_DEBOUNCE_MS);
  });

  context.subscriptions.push(onSave);
}

/**
 * Extension activation entry point.
 */
export async function activate(
  context: vscode.ExtensionContext
): Promise<void> {
  const cli = new CliWrapper();
  const installer = new Installer(cli);

  // Register walkthrough commands
  registerWalkthroughCommands(context, cli, installer);

  const workspaceRoot = getWorkspaceRoot();

  if (workspaceRoot) {
    const dbPath = findGraphDb(workspaceRoot);

    if (dbPath) {
      // Graph database found - initialize
      sqliteReader = new SqliteReader(dbPath);

      // Schema compatibility check
      const schemaWarning = sqliteReader.checkSchemaCompatibility();
      if (schemaWarning) {
        const choice = await vscode.window.showWarningMessage(
          `Code Graph: ${schemaWarning}`,
          "Rebuild Graph",
          "Dismiss"
        );
        if (choice === "Rebuild Graph") {
          await vscode.commands.executeCommand("codeReviewGraph.buildGraph");
        }
      }

      // Register tree view providers
      const codeGraphProvider = new CodeGraphTreeProvider(
        sqliteReader,
        workspaceRoot
      );
      const blastRadiusProvider = new BlastRadiusTreeProvider();
      const statsProvider = new StatsTreeProvider(sqliteReader);

      context.subscriptions.push(
        vscode.window.registerTreeDataProvider(
          "codeReviewGraph.codeGraph",
          codeGraphProvider
        ),
        vscode.window.registerTreeDataProvider(
          "codeReviewGraph.blastRadius",
          blastRadiusProvider
        ),
        vscode.window.registerTreeDataProvider(
          "codeReviewGraph.stats",
          statsProvider
        )
      );

      // Create status bar
      const statusBar = new StatusBar();
      statusBar.update(sqliteReader);
      statusBar.show();
      context.subscriptions.push(statusBar);

      // Register SCM file decoration provider
      scmDecorationProvider = new ScmDecorationProvider();
      context.subscriptions.push(
        vscode.window.registerFileDecorationProvider(scmDecorationProvider)
      );
    } else {
      // No graph database found - show welcome
      showWelcomeIfNeeded(context);
    }
  }

  // Register revealInTree command for bidirectional graph→tree sync
  context.subscriptions.push(
    vscode.commands.registerCommand(
      "codeReviewGraph.revealInTree",
      (_qualifiedName: string) => {
        // When a node is clicked in the graph, highlight it in the graph
        // The graph webview already calls this; the tree view sync relies
        // on the file navigation that nodeClicked also triggers.
        // This command is a hook for future tree reveal integration.
        GraphWebviewPanel.highlightNode(_qualifiedName);
      }
    )
  );

  // Register commands (always, even without a database)
  registerCommands(context, cli);

  // Watch for graph.db changes
  watchGraphDb(context);

  // Set up auto-update on save
  setupAutoUpdate(context, cli);
}

/**
 * Extension deactivation cleanup.
 */
export function deactivate(): void {
  if (autoUpdateTimer) {
    clearTimeout(autoUpdateTimer);
    autoUpdateTimer = undefined;
  }

  if (watchProcess) {
    watchProcess.kill();
    watchProcess = undefined;
  }
  watchOutput?.dispose();
  watchOutput = undefined;

  sqliteReader?.close();
  sqliteReader = undefined;
}
