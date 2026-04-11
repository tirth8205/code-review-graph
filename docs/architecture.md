# Architecture

## System Overview

`code-review-graph` is a Claude Code plugin that maintains a persistent, incrementally-updated knowledge graph of a codebase. It's designed to make code reviews faster and more context-aware by providing structural understanding of code relationships.

## Component Diagram

```
┌──────────────────────────────────────────────────────────────┐
│                        Claude Code                           │
│                                                              │
│  Skills (SKILL.md)          Hooks (settings.json)            │
│  ├── build-graph            ├── PostToolUse (Write|Edit|Bash) │
│  ├── review-delta           │    → incremental update         │
│  └── review-pr              └── PreToolUse (git commit)       │
│          │                       → detect-changes             │
│          ▼                        ▼                          │
│  ┌────────────────────────────────────────────┐              │
│  │            MCP Server (stdio)              │              │
│  │                                            │              │
│  │  22 MCP Tools + 5 MCP Prompts              │              │
│  │  ├── Core: build, impact, query, review,   │              │
│  │  │   search, embed, stats, docs, large_fn  │              │
│  │  ├── Flows: list, get, affected            │              │
│  │  ├── Communities: list, get, architecture   │              │
│  │  ├── Analysis: detect_changes, refactor,   │              │
│  │  │   apply_refactor                        │              │
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
   Tree-sitter   SQLite DB      git diff
   grammars      (.code-review- subprocess
                 graph/
                 graph.db)
```

## Data Flow

### Full Build
1. `collect_all_files()` gathers tracked files (`git ls-files`) and applies `.code-review-graphignore` (gitignored files are skipped automatically when git is available)
2. For each file, `CodeParser.parse_file()` uses Tree-sitter to extract AST
3. AST walker identifies structural nodes (classes, functions, imports) and edges (calls, inheritance)
4. `GraphStore.store_file_nodes_edges()` persists to SQLite with file hash for change detection
5. Metadata updated with timestamp

### Incremental Update
1. `get_changed_files()` runs `git diff --name-only` against base ref
2. `find_dependents()` queries the graph for files importing the changed files
3. Changed + dependent files are re-parsed (others skipped via hash comparison)
4. Only affected rows in SQLite are updated

### Review Context Generation
1. Changed files identified (git diff or explicit list)
2. `get_impact_radius()` performs BFS from changed nodes through the graph
3. Source snippets extracted for changed areas only
4. Review guidance generated (test coverage gaps, wide blast radius warnings)
5. Assembled into a structured, token-efficient context for Claude

## Storage

### SQLite Schema
- **nodes** table: id, kind, name, qualified_name, file_path, line_start/end, language, community_id, etc.
- **edges** table: id, kind, source_qualified, target_qualified, file_path, line
- **metadata** table: key-value pairs (last_updated, build_type, schema_version)
- **flows** table: id, name, entry_point_id, depth, node_count, file_count, criticality, path_json
- **flow_memberships** table: flow_id, node_id, position
- **communities** table: id, name, level, parent_id, cohesion, size, dominant_language, description
- **nodes_fts** (FTS5 virtual table): full-text search on name, qualified_name, file_path, signature
- **embeddings** table (separate DB): node_id, model, vector, hash

Indexes on qualified_name, file_path, edge source/target, criticality, community_id, and cohesion for fast lookups.

WAL mode enabled for concurrent read access during updates.

### Qualified Names
Nodes are uniquely identified by qualified names:
- Files: absolute path (e.g., `/repo/src/auth.py`)
- Functions: `file_path::function_name` (e.g., `/repo/src/auth.py::authenticate`)
- Methods: `file_path::ClassName.method_name` (e.g., `/repo/src/auth.py::AuthService.login`)

## Parsing Strategy

Tree-sitter provides language-agnostic AST access. The parser:
1. Walks the AST recursively
2. Pattern-matches on node types (language-specific mappings in `_CLASS_TYPES`, `_FUNCTION_TYPES`, etc.)
3. Extracts names, parameters, return types, base classes
4. Identifies calls within function bodies
5. Resolves imports to module paths

This approach is more robust than tree-sitter queries across grammar versions.

## Visualization

The `visualization.py` module generates an interactive D3.js force-directed graph as a self-contained HTML file. It reads all nodes and edges from the SQLite graph store and renders them in the browser, allowing developers to visually explore code relationships, filter by node kind, and inspect dependencies.

## Impact Analysis Algorithm

BFS from seed nodes (changed files' contents):
1. Seed = all qualified names in changed files
2. For each node in frontier:
   - Follow forward edges (what this node affects)
   - Follow reverse edges (what depends on this node)
3. Expand up to `max_depth` hops (default: 2)
4. Collect all reached nodes as "impacted"

This captures both downstream effects (things that call changed code) and upstream context (things that the changed code depends on).
