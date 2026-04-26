# Code Review Graph — User Guide

**Version:** v2.1.0 (Apr 3, 2026)

## Installation

```bash
pip install code-review-graph
code-review-graph install    # auto-detects and configures all supported platforms
code-review-graph build      # parse your codebase
```

`install` detects which AI coding tools you have and writes the correct MCP configuration for each one. Restart your editor/tool after installing.

To target a specific platform instead of auto-detecting all:

```bash
code-review-graph install --platform codex
code-review-graph install --platform cursor
code-review-graph install --platform claude-code
```

### Supported Platforms

| Platform | Config file |
|----------|-------------|
| **Codex** | `~/.codex/config.toml` |
| **Claude Code** | `.mcp.json` |
| **Cursor** | `.cursor/mcp.json` |
| **Windsurf** | `.windsurf/mcp.json` |
| **Zed** | `.zed/settings.json` |
| **Continue** | `.continue/config.json` |
| **OpenCode** | `.opencode.json` |
| **Antigravity** | `~/.gemini/antigravity/mcp_config.json` |
| **Gemini CLI** | `.gemini/settings.json` |
| **Qwen Code** | `~/.qwen/settings.json` |
| **Qoder** | `.qoder/mcp.json` |

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

Embedding providers: Local (sentence-transformers), Google Gemini, MiniMax. Configure via `CRG_EMBEDDING_MODEL` env var.

### 7. Detect changes with risk scoring (v2)
```
Ask Claude: "Review my recent changes with risk scoring"
```
Uses `detect_changes_tool` to map diffs to affected functions, flows, communities, and test gaps.

### 8. Explore architecture (v2)
```
Ask Claude: "Show me the architecture of this project"
```
Uses `get_architecture_overview_tool` for community-based architecture map with coupling warnings.

### 9. Generate wiki (v2)
```bash
code-review-graph wiki
```
Creates markdown wiki pages for each detected community in `.code-review-graph/wiki/`.

### 10. Multi-repo search (v2)
```bash
code-review-graph register /path/to/other/repo --alias mylib
```
Then use `cross_repo_search_tool` to search across all registered repositories.

## Token Savings

| Scenario | Without graph | With graph |
|----------|:---:|:---:|
| Review 200-file project | ~150k tokens | ~25k tokens |
| Incremental review | ~150k tokens | ~8k tokens |
| PR review | ~100k tokens | ~15k tokens |

## Supported Languages

Python, TypeScript/TSX, JavaScript, Vue, Go, Rust, Java, Scala, C#, Ruby, Kotlin, Swift, PHP, Solidity, C/C++, Dart, R, Perl

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

In git repos, indexing is based on tracked files (`git ls-files`), so gitignored files are skipped automatically. Use `.code-review-graphignore` to exclude tracked files or when git isn't available.
