# CLAUDE.md - Project Context for Claude Code

## Project Overview

**code-review-graph** is a persistent, incrementally-updated knowledge graph for token-efficient code reviews with Claude Code. It parses codebases using Tree-sitter, builds a structural graph in SQLite, and exposes it via MCP tools.

## Architecture

- **Core Package**: `code_review_graph/` (Python 3.10+)
  - `parser.py` — Tree-sitter multi-language AST parser (14 languages including Vue SFC and Solidity)
  - `graph.py` — SQLite-backed graph store (nodes, edges, BFS impact analysis)
  - `tools.py` — 9 MCP tool implementations
  - `incremental.py` — Git-based change detection, file watching
  - `embeddings.py` — Optional vector embeddings (local or Google Gemini)
  - `visualization.py` — D3.js interactive HTML graph generator
  - `cli.py` — CLI entry point (`code-review-graph build/update/watch/serve/...`)
  - `main.py` — FastMCP server entry point (stdio transport)

- **VS Code Extension**: `code-review-graph-vscode/` (TypeScript)
  - Separate subproject with its own `package.json`, `tsconfig.json`
  - Reads from `.code-review-graph/graph.db` via SQLite

- **Database**: `.code-review-graph/graph.db` (SQLite, WAL mode)

## Key Commands

```bash
# Development
uv run pytest tests/ --tb=short -q          # Run tests (182 tests)
uv run ruff check code_review_graph/        # Lint
uv run mypy code_review_graph/ --ignore-missing-imports --no-strict-optional

# Build & test
uv run code-review-graph build              # Full graph build
uv run code-review-graph update             # Incremental update
uv run code-review-graph status             # Show stats
uv run code-review-graph serve              # Start MCP server
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
- `tests/test_multilang.py` — 14 language parsing tests (including Vue and Solidity)
- `tests/test_embeddings.py` — Vector encode/decode, similarity, store
- `tests/fixtures/` — Sample files for each supported language

## CI Pipeline

- **lint**: ruff on Python 3.10
- **type-check**: mypy
- **security**: bandit scan
- **test**: pytest matrix (3.10, 3.11, 3.12, 3.13) with 50% coverage minimum
