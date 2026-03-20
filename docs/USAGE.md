# Code Review Graph — User Guide

**Version:** v1.8.4 (Mar 20, 2026)

## Installation

```bash
pip install code-review-graph
code-review-graph install    # creates .mcp.json for Claude Code
code-review-graph build      # parse your codebase
```

Restart Claude Code to pick up the MCP server.

## Core Workflow

### 1. Build the graph (first time only)
```
/code-review-graph:build-graph
```
Parses your entire codebase. Takes ~10s for 500 files.

### 2. Review changes (daily use)
```
/code-review-graph:review-delta
```
Reviews only files changed since last commit + everything impacted. 5-10x fewer tokens than a full review.

### 3. Review a PR
```
/code-review-graph:review-pr
```
Comprehensive structural review of a branch diff with blast-radius analysis.

### 4. Watch mode (optional)
```bash
code-review-graph watch
```
Auto-updates the graph on every file save. Zero manual work.

### 5. Visualize the graph (optional)
```bash
code-review-graph visualize
open .code-review-graph/graph.html
```
Interactive D3.js force-directed graph. Starts collapsed (File nodes only) — click a file to expand its children. Use the search bar to filter, and click legend edge types to toggle visibility.

### 6. Semantic search (optional)
```bash
pip install "code-review-graph[embeddings]"
```
Then use `embed_graph_tool` to compute vectors. `semantic_search_nodes_tool` automatically uses vector similarity.

## Token Savings

| Scenario | Without graph | With graph |
|----------|:---:|:---:|
| Review 200-file project | ~150k tokens | ~25k tokens |
| Incremental review | ~150k tokens | ~8k tokens |
| PR review | ~100k tokens | ~15k tokens |

## Supported Languages

Python, TypeScript, JavaScript, Vue, Go, Rust, Java, C#, Ruby, Kotlin, Swift, PHP, Solidity, C/C++

## What Gets Indexed

- **Nodes**: Files, Classes, Functions/Methods, Types, Tests
- **Edges**: CALLS, IMPORTS_FROM, INHERITS, IMPLEMENTS, CONTAINS, TESTED_BY, DEPENDS_ON

See [schema.md](schema.md) for full details.

## Ignore Patterns

By default, these paths are excluded from indexing:

```
.code-review-graph/**    node_modules/**    .git/**
__pycache__/**           *.pyc              .venv/**
venv/**                  dist/**            build/**
.next/**                 target/**          *.min.js
*.min.css                *.map              *.lock
package-lock.json        yarn.lock          *.db
*.sqlite                 *.db-journal
```

To add custom patterns, create a `.code-review-graphignore` file in your repo root (same syntax as `.gitignore`):

```
generated/**
vendor/**
*.generated.ts
```
