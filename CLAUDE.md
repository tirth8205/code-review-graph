# CLAUDE.md - Project Context for Claude Code

## Project Overview

**code-review-graph** is a persistent, incrementally-updated knowledge graph for token-efficient code reviews with Claude Code. It parses codebases using Tree-sitter, builds a structural graph in SQLite, and exposes it via MCP tools and prompts.

## Architecture

- **Core Package**: `code_review_graph/` (Python 3.10+)
  - `parser.py` — Tree-sitter multi-language AST parser (19 languages including Vue SFC, Solidity, Dart, R, Perl, Lua + Jupyter/Databricks notebooks)
  - `graph.py` — SQLite-backed graph store (nodes, edges, BFS impact analysis)
  - `tools.py` — 22 MCP tool implementations
  - `main.py` — FastMCP server entry point (stdio transport), registers 22 tools + 5 prompts
  - `incremental.py` — Git-based change detection, file watching
  - `embeddings.py` — Optional vector embeddings (Local sentence-transformers, Google Gemini, MiniMax)
  - `visualization.py` — D3.js interactive HTML graph generator
  - `cli.py` — CLI entry point (install, build, update, watch, status, visualize, serve, wiki, detect-changes, register, unregister, repos, eval)
  - `flows.py` — Execution flow detection and criticality scoring
  - `communities.py` — Community detection (Leiden algorithm or file-based grouping) and architecture overview
  - `search.py` — FTS5 hybrid search (keyword + vector)
  - `changes.py` — Risk-scored change impact analysis (detect-changes)
  - `refactor.py` — Rename preview, dead code detection, refactoring suggestions
  - `hints.py` — Review hint generation
  - `prompts.py` — 5 MCP prompt templates (review_changes, architecture_map, debug_issue, onboard_developer, pre_merge_check)
  - `wiki.py` — Markdown wiki generation from community structure
  - `skills.py` — Skill definitions for Claude Code plugin
  - `registry.py` — Multi-repo registry with connection pool
  - `migrations.py` — Database schema migrations (v1-v5)
  - `tsconfig_resolver.py` — TypeScript path alias resolution

- **VS Code Extension**: `code-review-graph-vscode/` (TypeScript)
  - Separate subproject with its own `package.json`, `tsconfig.json`
  - Reads from `.code-review-graph/graph.db` via SQLite

- **Database**: `.code-review-graph/graph.db` (SQLite, WAL mode)

## Key Commands

```bash
# Development
uv run pytest tests/ --tb=short -q          # Run tests (572 tests)
uv run ruff check code_review_graph/        # Lint
uv run mypy code_review_graph/ --ignore-missing-imports --no-strict-optional

# Build & test
uv run code-review-graph build              # Full graph build
uv run code-review-graph update             # Incremental update
uv run code-review-graph status             # Show stats
uv run code-review-graph serve              # Start MCP server
uv run code-review-graph wiki               # Generate markdown wiki
uv run code-review-graph detect-changes     # Risk-scored change analysis
uv run code-review-graph register <path>    # Register repo in multi-repo registry
uv run code-review-graph repos              # List registered repos
uv run code-review-graph eval               # Run evaluation benchmarks
```

## Code Conventions

- **Line length**: 100 chars (ruff)
- **Python target**: 3.10+
- **SQL**: Always use parameterized queries (`?` placeholders), never f-string values
- **Error handling**: Catch specific exceptions, log with `logger.warning/error`
- **Thread safety**: `threading.Lock` for shared caches, `check_same_thread=False` for SQLite
- **Node names**: Always sanitize via `_sanitize_name()` before returning to MCP clients
- **File reads**: Read bytes once, hash, then parse (TOCTOU-safe pattern)

## Security Invariants

- No `eval()`, `exec()`, `pickle`, or `yaml.unsafe_load()`
- No `shell=True` in subprocess calls
- `_validate_repo_root()` prevents path traversal via repo_root parameter
- `_sanitize_name()` strips control characters, caps at 256 chars (prompt injection defense)
- `escH()` in visualization escapes HTML entities including quotes and backticks
- SRI hash on D3.js CDN script tag
- API keys only from environment variables, never hardcoded

## Test Structure

- `tests/test_parser.py` — Parser correctness, cross-file resolution
- `tests/test_graph.py` — Graph CRUD, stats, impact radius
- `tests/test_tools.py` — MCP tool integration tests
- `tests/test_visualization.py` — Export, HTML generation, C++ resolution
- `tests/test_incremental.py` — Build, update, migration, git ops
- `tests/test_multilang.py` — 19 language parsing tests (including Vue, Solidity, Dart, R, Perl, XS, Lua)
- `tests/test_embeddings.py` — Vector encode/decode, similarity, store
- `tests/test_flows.py` — Execution flow detection and criticality
- `tests/test_communities.py` — Community detection, architecture overview
- `tests/test_changes.py` — Risk-scored change analysis
- `tests/test_refactor.py` — Rename preview, dead code, suggestions
- `tests/test_search.py` — FTS5 hybrid search
- `tests/test_hints.py` — Review hint generation
- `tests/test_prompts.py` — MCP prompt template tests
- `tests/test_wiki.py` — Wiki generation
- `tests/test_skills.py` — Skill definitions
- `tests/test_registry.py` — Multi-repo registry
- `tests/test_migrations.py` — Database migrations
- `tests/test_eval.py` — Evaluation framework
- `tests/test_tsconfig_resolver.py` — TypeScript path resolution
- `tests/test_integration_v2.py` — v2 pipeline integration test
- `tests/fixtures/` — Sample files for each supported language

## CI Pipeline

- **lint**: ruff on Python 3.10
- **type-check**: mypy
- **security**: bandit scan
- **test**: pytest matrix (3.10, 3.11, 3.12, 3.13) with 50% coverage minimum

## Active Technologies
- Python 3.10+ + `voyageai` SDK (new optional), `fastmcp`, `tree-sitter`, `networkx` (001-voyage-ai-embeddings)
- SQLite (existing `embeddings` table, no schema changes needed) (001-voyage-ai-embeddings)

## Recent Changes
- 001-voyage-ai-embeddings: Added Python 3.10+ + `voyageai` SDK (new optional), `fastmcp`, `tree-sitter`, `networkx`
