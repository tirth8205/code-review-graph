# Code Review Graph for VS Code

Visualize code dependencies, blast radius, and review context from your code graph -- directly in VS Code.

## Features

- **Code Graph Explorer** -- Browse files, classes, functions, and their relationships in a tree view
- **Blast Radius** -- See which files and symbols are impacted when you change code
- **Review Changes** -- Automatically detect git changes and show their blast radius
- **Find Callers / Callees** -- Trace all callers or callees of any function
- **Find Tests** -- Locate tests for any symbol
- **Query Graph** -- Run semantic queries (callers, callees, imports, inheritance, tests) with 8 patterns
- **Find Large Functions** -- Identify functions or classes exceeding a line-count threshold
- **Interactive Graph** -- Force-directed D3.js visualization of your code dependencies
- **Live Search** -- Fuzzy search across your entire code graph with instant results
- **Compute Embeddings** -- Generate vector embeddings for semantic search
- **Watch Mode** -- Continuous graph updates as you work
- **Auto-Update** -- Graph rebuilds in the background when you save files

## Quick Start

### 1. Install the Extension

Install **Code Review Graph** from the VS Code Marketplace.

### 2. Install the Backend

The extension requires the `code-review-graph` Python CLI to parse your codebase.

```bash
# Recommended
uv pip install code-review-graph

# Alternatives
pipx install code-review-graph
pip install code-review-graph
```

Requires Python 3.10+.

### 3. Build Your Graph

Open the Command Palette (`Ctrl+Shift+P`) and run **Code Graph: Build Graph**.

The graph database is stored locally at `.code-review-graph/graph.db` and updates automatically on file save.

For continuous background indexing, run **Code Graph: Watch Mode**. The extension starts `code-review-graph watch --json-events`, streams progress to the **Code Graph Watch** output channel, and stops the watcher when the extension deactivates.

## Commands

| Command | Description |
|---|---|
| `Code Graph: Build Graph` | Parse the codebase and create the graph database |
| `Code Graph: Update Graph` | Incrementally update the graph |
| `Code Graph: Show Blast Radius` | Show the blast radius for a symbol |
| `Code Graph: Review Changes` | Analyze git changes and show impacted files |
| `Code Graph: Find Callers` | Find all callers of a function |
| `Code Graph: Find Callees` | Find all functions called by a target |
| `Code Graph: Find Tests` | Find tests for a symbol |
| `Code Graph: Find Large Functions` | Find functions/classes exceeding a size threshold |
| `Code Graph: Query Graph` | Run semantic queries (8 patterns: callers_of, callees_of, etc.) |
| `Code Graph: Search` | Search the code graph |
| `Code Graph: Show Graph` | Open the interactive graph visualization |
| `Code Graph: Compute Embeddings` | Generate vector embeddings for semantic search |
| `Code Graph: Watch Mode` | Run graph in watch mode for continuous updates |

## Browser UI and LSP

The Python backend also ships a standalone browser explorer:

```bash
code-review-graph build
axon web --repo . --host 127.0.0.1 --port 8765
# Open http://127.0.0.1:8765/
```

`axon-web --repo . --host 127.0.0.1 --port 8765` starts the same UI.

The browser dashboard includes graph size, estimated source-vs-graph token savings, observed web API request count, and estimated cumulative/average savings. Those estimates use the standard chars/4 approximation and are not model-provider billing data.

Editor integrations can use the stdio Language Server Protocol server:

```bash
code-review-graph lsp --repo .
```

## Settings

| Setting | Default | Description |
|---|---|---|
| `codeReviewGraph.cliPath` | `""` | Path to the CLI binary. Leave empty to auto-detect. |
| `codeReviewGraph.autoUpdate` | `true` | Auto-update the graph on file save. |
| `codeReviewGraph.blastRadiusDepth` | `2` | Max traversal depth for blast radius (1--10). |
| `codeReviewGraph.graphTheme` | `"auto"` | Graph color theme: `auto`, `light`, or `dark`. |
| `codeReviewGraph.graph.maxNodes` | `500` | Max nodes in the graph visualization (10--5000). |
| `codeReviewGraph.graph.defaultEdges` | All except CONTAINS | Edge types shown by default. |

## Requirements

- VS Code 1.85+
- Python 3.10+ (for the backend CLI)
- A workspace with source code to analyze

## Links

- [Main Repository](https://github.com/tirth8205/code-review-graph)
- [Report an Issue](https://github.com/tirth8205/code-review-graph/issues)

## License

MIT
