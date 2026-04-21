# CLAUDE.md - Project Context for Claude Code

## Project Overview

**code-review-graph** is a persistent, incrementally-updated knowledge graph for token-efficient code reviews with Claude Code. It parses codebases using Tree-sitter, builds a structural graph in SQLite, and exposes it via MCP tools and prompts.

## Graph Tool Usage (Token-Efficient)
When using code-review-graph MCP tools, follow these rules:
1. First call: `get_minimal_context(task="<description>")` — costs ~100 tokens, gives you the full picture.
2. All subsequent calls: use `detail_level="minimal"` unless you need more.
3. Prefer `query_graph` with a specific target over broad `list_*` calls.
4. The `next_tool_suggestions` field in every response tells you the optimal next step.
5. Target: ≤5 tool calls per task, ≤800 total tokens of graph context.

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


<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:ca08a54f -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd dolt push
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->

<!-- code-review-graph MCP tools -->
## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the
code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore
the codebase.** The graph is faster, cheaper (fewer tokens), and gives
you structural context (callers, dependents, test coverage) that file
scanning cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes` or `query_graph` instead of Grep
- **Understanding impact**: `get_impact_radius` instead of manually tracing imports
- **Code review**: `detect_changes` + `get_review_context` instead of reading entire files
- **Finding relationships**: `query_graph` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview` + `list_communities`

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

### Key Tools

| Tool | Use when |
|------|----------|
| `detect_changes` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context` | Need source snippets for review — token-efficient |
| `get_impact_radius` | Understanding blast radius of a change |
| `get_affected_flows` | Finding which execution paths are impacted |
| `query_graph` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `get_architecture_overview` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes` for code review.
3. Use `get_affected_flows` to understand impact.
4. Use `query_graph` pattern="tests_for" to check coverage.
