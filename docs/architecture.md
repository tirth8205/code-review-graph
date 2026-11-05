# Architecture

## Overview

`code-review-graph` keeps a persistent, incrementally updated graph of a codebase in SQLite
and exposes it through a CLI and an MCP server. AI coding tools query the graph for
structural context (callers, dependents, tests, impact) instead of reading whole files.
Claude Code is one supported client among several.

## Components

```text
┌──────────────────────────────────────────────────────────────┐
│                    AI coding clients / CLI                   │
│                                                              │
│  MCP clients              Hooks / watch mode                 │
│  ├── Codex                └── incremental update             │
│  ├── Claude Code, CodeBuddy Code                             │
│  ├── Cursor, Windsurf, Zed, Continue                         │
│  └── Gemini CLI, Qwen, Qoder, Copilot, OpenCode              │
│          │                        │                          │
│          ▼                        ▼                          │
│  ┌────────────────────────────────────────────┐              │
│  │      MCP Server (stdio or localhost HTTP)  │              │
│  │                                            │              │
│  │  30 MCP tools + 5 MCP prompts              │              │
│  │  ├── Core: build, impact, query, review,   │              │
│  │  │   search, traverse, embed, stats, docs  │              │
│  │  ├── Flows: list, get, affected            │              │
│  │  ├── Communities: list, get, architecture  │              │
│  │  ├── Analysis: detect_changes, refactor,   │              │
│  │  │   apply_refactor, hubs, bridges, gaps   │              │
│  │  ├── Wiki: generate, get_page              │              │
│  │  └── Multi-repo: list_repos, cross_search  │              │
│  └────────────────┬───────────────────────────┘              │
└───────────────────┼──────────────────────────────────────────┘
                    │
        ┌───────────┼───────────────┐
        ▼           ▼               ▼
   ┌─────────┐ ┌─────────┐  ┌─────────────┐
   │ Parser  │ │  Graph  │  │ Incremental │
   │         │ │  Store  │  │   Engine    │
   └────┬────┘ └────┬────┘  └──────┬──────┘
        │           │              │
        ▼           ▼              ▼
   Tree-sitter   SQLite DB      git/svn diff
   grammars      (.code-review- subprocess
                 graph/
                 graph.db)
```

## Modules

All modules live in `code_review_graph/`.

| Module | Role |
|---|---|
| `parser.py` | Tree-sitter multi-language parser plus targeted fallbacks; emits nodes and edges per file |
| `custom_languages.py` | Config-driven languages from `.code-review-graph/languages.toml` |
| `graph.py` | `GraphStore`: SQLite storage, queries, impact radius |
| `migrations.py` | Versioned schema migrations, currently v11 (see `schema.md`) |
| `incremental.py` | File collection and ignore rules, git/SVN change detection, full and incremental builds, post-build resolvers |
| `postprocessing.py` | `run_post_processing()`: endpoint resolution, signatures, FTS sync, flows, communities, embedding refresh |
| `python_resolver.py`, `jedi_resolver.py`, `tsconfig_resolver.py`, `spring_resolver.py`, `event_resolver.py`, `temporal_resolver.py`, `rescript_resolver.py`, `hcl_resolver.py`, `scoped_resolver.py` | Post-build cross-file resolution |
| `flows.py` | Execution flow detection and criticality scoring |
| `communities.py` | Leiden community detection (igraph) with a file-based fallback |
| `search.py` | Hybrid search: FTS5 BM25 plus vector similarity |
| `embeddings.py` | Embedding providers and the `embeddings` table |
| `changes.py`, `refactor.py`, `analysis.py`, `hints.py`, `uncertainty.py`, `context_savings.py` | Change risk analysis, refactoring helpers, hub/bridge/gap analysis, response hints, empty-result markers, savings estimates |
| `tools/` | MCP tool implementations, split by domain |
| `main.py`, `prompts.py` | FastMCP server (30 tools, 5 prompts) |
| `cli.py`, `daemon.py`, `daemon_cli.py` | CLI and the multi-repo watch daemon |
| `visualization.py`, `exports.py`, `wiki.py`, `graph_diff.py`, `memory.py`, `forget.py` | HTML visualisation, export formats, wiki generation, snapshot diffing, Q&A memory, file removal |
| `skills.py`, `uninstall.py`, `registry.py`, `enrich.py`, `http_origin_guard.py`, `config_keys.py`, `constants.py` | Platform install/uninstall, multi-repo registry, hook enrichment, HTTP Host/Origin checks, shared helpers |
| `eval/`, `token_benchmark.py` | Evaluation framework and standalone token benchmark |

## Data Flow

### Full build (`incremental.full_build()`)
1. `collect_all_files()` lists tracked files (`git ls-files` when git is available, so
   untracked and gitignored files are skipped) and applies `.code-review-graphignore`.
2. Each file is read once; the bytes are hashed (SHA-256) and passed to
   `CodeParser.parse_bytes()`, which walks the Tree-sitter tree and emits nodes and edges.
3. `GraphStore.store_file_nodes_edges()` (or `store_file_batch()`) replaces the file's rows
   in one transaction, storing the hash for change detection.
4. Metadata is written: `last_updated`, `last_build_type`, and the git or SVN head.
5. Post-build resolvers qualify cross-file targets, then `run_post_processing()` computes
   signatures, rebuilds the FTS index, traces flows, detects communities and refreshes
   embeddings, unless skipped. An incremental update rewrites only the changed files'
   FTS entries (`search.update_fts_index`); a full build rebuilds the index.

### Incremental update (`incremental.incremental_update()`)
1. `get_changed_files()` asks the VCS for changed paths (git diff by default; SVN is
   supported).
2. `find_dependents()` collects files with `IMPORTS_FROM`, `CALLS`, `INHERITS` or
   `IMPLEMENTS` edges into the changed files, up to two hops and 500 files.
3. Changed and dependent files whose hash differs from the stored one are re-parsed; the
   rest are skipped.
4. Only those files' rows are replaced, then post-processing runs as for a full build.

### Review context (`tools/review.py: get_review_context()`)
1. Changed files come from a git diff or an explicit list.
2. `GraphStore.get_impact_radius()` finds the impacted nodes (see below).
3. Source snippets are extracted for the changed regions only, within a file budget.
4. `_generate_review_guidance()` adds warnings: changed functions without `TESTED_BY`
   edges, more than 20 impacted nodes, and inheritance changes.
5. `attach_context_savings()` adds an estimate of the tokens saved against reading the
   files, labelled `estimated: true`.

The MCP entry points are `get_review_context_tool`, `get_impact_radius_tool` and
`detect_changes_tool` in `main.py`.

## Impact Analysis

`GraphStore.get_impact_radius()` delegates to `get_impact_radius_sql()`, a bounded
best-score relaxation run inside SQLite. Setting `CRG_BFS_ENGINE=networkx` selects the older
Python-side traversal instead.

1. Seed with every node in the changed files.
2. For each depth step (default 2, `CRG_MAX_IMPACT_DEPTH`), propagate along edges in the
   direction set per edge kind in `constants.IMPACT_EDGE_DIRECTIONS`: dependency-shaped
   edges such as `CALLS` and `IMPORTS_FROM` flow from target to source (callers and
   importers of changed code), `TESTED_BY` flows from production code to its tests, and
   `CONTAINS` is not expanded because the whole file is already seeded.
3. Each reached node keeps its best score: previous score x edge weight
   (`constants.IMPACT_EDGE_WEIGHTS`, default 0.5) x depth decay
   (`CRG_IMPACT_DEPTH_DECAY`, default 0.6).
4. Results are ordered by score and cut at `CRG_MAX_IMPACT_NODES` (default 500); the
   response says whether it was truncated.

## Storage

One SQLite database, `.code-review-graph/graph.db`, in WAL mode so readers are not blocked
during updates. Tables: `nodes`, `edges`, `metadata`, `flows`, `flow_memberships`,
`communities`, `nodes_fts` (FTS5), `nodes_fts_state`, `community_summaries`,
`flow_snapshots`, `risk_index`, and
`embeddings` (created by `EmbeddingStore` in the same file). Columns, indexes and the
migration history are in [schema.md](schema.md).

Nodes are identified by qualified name: the file path for File nodes, `path::name` for
top-level symbols and `path::Class.method` for members.

## Parsing

The parser walks the Tree-sitter tree directly rather than using tree-sitter query files,
which keeps it independent of query syntax differences between grammar versions. Per-language
node type tables (`_CLASS_TYPES`, `_FUNCTION_TYPES` and similar in `parser.py`) drive:

1. Class, function and type extraction with names, parameters, return types and bases.
2. Call detection inside function bodies.
3. Import resolution to module paths, refined later by the post-build resolvers.

Languages without a grammar can be added through `.code-review-graph/languages.toml`
(`docs/CUSTOM_LANGUAGES.md`). Some formats (notebooks, Vue and Svelte SFCs, SQL, Ansible,
Spring configuration) use targeted parsers instead of Tree-sitter.

## Visualisation

`visualization.py` writes a self-contained HTML file with a D3.js force-directed graph of the
nodes and edges, with filters by node and edge kind and a search box.
