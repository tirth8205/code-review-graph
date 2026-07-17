# Code Review Graph — User Guide

**Applies to:** v2.3.6

## Installation

```bash
pip install code-review-graph
code-review-graph install    # auto-detects and configures all supported platforms
code-review-graph build      # parse your codebase
```

`install` detects which AI coding tools you have, writes the correct MCP configuration for each one, and installs platform-native hooks where supported. Restart your editor/tool after installing.

To target a specific platform instead of auto-detecting all:

```bash
code-review-graph install --platform codex
code-review-graph install --platform cursor
code-review-graph install --platform claude-code
code-review-graph install --platform codebuddy
code-review-graph install --platform bob-shell
code-review-graph install --platform bob-ide
```

### Supported Platforms

| Platform | Config file |
|----------|-------------|
| **Codex** | `~/.codex/config.toml` + `~/.codex/hooks.json` |
| **Claude Code** | `.mcp.json` + `.claude/settings.json` |
| **CodeBuddy Code** | `.mcp.json` + `CODEBUDDY.md` + `.codebuddy/settings.json` + `.codebuddy/skills/<name>/SKILL.md` |
| **Cursor** | `.cursor/mcp.json` |
| **Windsurf** | `~/.codeium/windsurf/mcp_config.json` |
| **Zed** | `.zed/settings.json` |
| **Continue** | `.continue/config.json` |
| **OpenCode** | `opencode.jsonc` (preferred) or `opencode.json` |
| **Antigravity** | `~/.gemini/antigravity/mcp_config.json` |
| **Gemini CLI** | `.gemini/settings.json` |
| **Qwen Code** | `~/.qwen/settings.json` |
| **Kiro** | `.kiro/settings/mcp.json` |
| **Qoder** | `.qoder/mcp.json` |
| **GitHub Copilot** | `.vscode/mcp.json` |
| **GitHub Copilot CLI** | `~/.copilot/mcp-config.json` |
| **IBM Bob Shell** | `~/.bob/mcp_settings.json` |
| **IBM Bob IDE** | `.bob/mcp.json` |

The IBM Bob paths and command-based STDIO entries follow the official
[Bob Shell MCP](https://bob.ibm.com/docs/shell/configuration/mcp/mcp-bobshell)
and [Bob IDE MCP](https://bob.ibm.com/docs/ide/configuration/mcp/mcp-in-bob)
contracts. `install --platform all` configures Bob only when its executable,
global MCP file, or workspace `.bob` directory already provides a detection
signal.

The CodeBuddy project layout follows its official documentation for
[MCP configuration](https://www.codebuddy.ai/docs/cli/mcp),
[skills](https://www.codebuddy.ai/docs/cli/skills), and
[hooks](https://www.codebuddy.ai/docs/cli/hooks). The shared `.mcp.json` is
merged with JSONC awareness, while hook commands resolve the repository at
runtime so committed settings do not contain one developer's checkout path.

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
Reviews only files changed since last commit plus the graph-derived impact radius. Relevant review and impact responses include compact estimated `context_savings` metadata. Across the 6 benchmark repositories, graph queries use ~82x fewer tokens per question (median; range 38x–528x) than reading the whole corpus — see the [README benchmarks](../README.md#benchmarks) and [REPRODUCING.md](REPRODUCING.md) for the methodology.

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
Then use `embed_graph_tool` to compute vectors. `semantic_search_nodes_tool` automatically uses vector similarity when matching embeddings are available and falls back to keyword/FTS search otherwise.

Embedding providers are local sentence-transformers, OpenAI-compatible endpoints, Google Gemini, and MiniMax. Local embeddings use `CRG_EMBEDDING_MODEL`; OpenAI-compatible providers use `CRG_OPENAI_BASE_URL`, `CRG_OPENAI_API_KEY`, and `CRG_OPENAI_MODEL`. Cloud providers are opt-in and print an egress warning unless `CRG_ACCEPT_CLOUD_EMBEDDINGS=1` is set.

Function/class documentation summaries are included in embedding text. For a
graph created by an older release, run a full build once before re-embedding so
all files gain that metadata. Embedding refresh after build/update/watch is
always default-off; opt in with an exact provider and model, for example:

```bash
code-review-graph build \
  --embedding-provider local \
  --embedding-model all-MiniLM-L6-v2
```

The same two options work with `update`, `postprocess`, and `watch`. They must be
provided together. A refresh only updates a previously embedded graph, refuses
to migrate vectors to a different provider/model/endpoint, purges deleted-node
vectors, and degrades provider or transport failures to graph-build warnings.

### 7. Detect changes with risk scoring (v2)
```
Ask your MCP client: "Review my recent changes with risk scoring"
```
Uses `detect_changes_tool` to map diffs to affected functions, flows, communities, and test gaps.

### 8. Explore architecture (v2)
```
Ask your MCP client: "Show me the architecture of this project"
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

## Context Savings

CRG reduces review context by sending graph-derived structural context instead of broad file dumps. The exact reduction depends on the repository and change shape. The evaluation runner reports the current benchmark data used in the README:

```bash
code-review-graph eval --all
```

Since v2.3.4, review and impact tools include compact `context_savings` metadata. In v2.3.5 the CLI surfaces this as a boxed `Token Savings` panel on both `detect-changes --brief` and `update --brief`, with a per-category breakdown (Functions / Tests / Risk / Other) that sums exactly to the graph response size. Add `--verify` to cross-check the displayed numbers against OpenAI's `cl100k_base` tokenizer (requires `pip install tiktoken`). All numbers are labelled estimated because they use a conservative approximation rather than model-specific tokenisation; calibration shows the estimate stays within ~1% of real GPT-4 tokens in aggregate. Small single-file changes can occasionally use more context than the raw file because graph metadata has overhead.

## Supported Languages

The parser currently covers Python, JavaScript, TypeScript/TSX, Go, Rust, Java, C/C++, C#, VB.NET, Ruby, Kotlin, Swift, PHP, Scala, Solidity, Dart, R, Perl, Lua/Luau, Objective-C, shell scripts, Elixir, Zig, PowerShell, Julia, ReScript, GDScript, Nix, Verilog/SystemVerilog, SQL, Vue/Svelte single-file components, Astro files parsed through the TypeScript parser, Jupyter/Databricks notebooks (`.ipynb`), and Perl XS files (`.xs`).

Extension-less scripts are detected by shebang for common bash/sh/zsh/ksh/dash/ash, Python, Node, Ruby, Perl, Lua, Rscript, and PHP interpreters.

Languages not covered yet can be added without a fork via a `.code-review-graph/languages.toml` config — see [CUSTOM_LANGUAGES.md](CUSTOM_LANGUAGES.md).

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
