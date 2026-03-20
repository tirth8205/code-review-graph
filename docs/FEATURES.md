# Features

## v1.8.4 (Current)
- **Multi-word AND search**: `search_nodes` now requires all words to match (case-insensitive), producing more precise results.
- **Call target resolution**: Bare call targets are resolved to qualified names using same-file definitions, improving `callers_of`/`callees_of` accuracy.
- **Impact radius pagination**: `get_impact_radius` returns `truncated` flag and `total_impacted` count; `max_results` parameter controls output size.
- **`find_large_functions_tool`**: New MCP tool to find functions, classes, or files exceeding a line-count threshold.
- **14 languages**: Added Vue SFC and Solidity support.
- **Documentation overhaul**: All docs updated with accurate language/tool counts, version references, and VS Code extension parity.

## v1.8.3
- **Parser recursion guard**: `_MAX_AST_DEPTH = 180` prevents stack overflow on deeply nested ASTs.
- **Module cache bound**: `_MODULE_CACHE_MAX = 15,000` with automatic eviction.
- **Embeddings thread safety**: `check_same_thread=False` on EmbeddingStore SQLite.
- **Embeddings retry logic**: Exponential backoff for Google Gemini API calls.
- **Visualization XSS hardening**: `</` escaped to `<\/` in JSON serialization.
- **CLI error handling**: Split broad `except` into specific handlers.
- **Git timeout**: Configurable via `CRG_GIT_TIMEOUT` env var.
- **Governance files**: CONTRIBUTING.md, SECURITY.md, CODE_OF_CONDUCT.md.

## v1.8.2
- **C# parsing fix**: Renamed language identifier from `c_sharp` to `csharp`.
- **Watch mode thread safety**: SQLite connections compatible with Python 3.10/3.11 watchdog threads.
- **Full rebuild cleanup**: Purges stale data from deleted files during full rebuild.
- **Dependency trim**: Removed unused `gitpython` dependency.

## v1.7.0
- **`install` command**: New primary entry point for setup (`code-review-graph install`). `init` remains as an alias.
- **`--dry-run` flag**: Preview what `install`/`init` would write without modifying files.
- **PyPI auto-publish**: GitHub releases now automatically publish to PyPI.
- **README rewrite**: Professional documentation with real benchmark data from httpx, FastAPI, and Next.js.

## v1.6.4
- **Portable MCP config**: `init` now generates `uvx`-based `.mcp.json` — no absolute paths, works on any machine with `uv` installed
- **Removed symlink workaround**: The `_safe_path` helper for spaces-in-paths is no longer needed with `uvx`

## v1.6.3
- **SessionStart hook**: Claude Code automatically prefers graph MCP tools over full codebase scans at session start
- **Marketplace ready**: plugin.json corrected for official Claude Code plugin marketplace submission
- **README cleanup**: Removed screenshot placeholders

## v1.6.2
- **24 audit fixes**: Critical bug fixes, performance improvements, parser enhancements, expanded test coverage
- **Parser: C/C++ support**: Full node extraction for C and C++ (classes, functions, imports, calls, inheritance)
- **Parser: name extraction**: Fixed for Kotlin, Swift (simple_identifier), Ruby (constant)
- **Performance**: NetworkX graph caching, batch edge queries, chunked embedding search, git subprocess timeouts
- **CI hardening**: Coverage enforcement (50%), bandit security scanning, mypy type checking
- **Tests**: +40 new tests for incremental updates, embeddings, and 7 new language fixtures
- **Docs**: API response schemas, ignore pattern documentation, fixed hook config reference
- **Accessibility**: ARIA labels throughout D3.js visualization

## v1.5.3
- **Spaces-in-path handling**: *(superseded in v1.6.4 by `uvx`-based config)* Previously used symlinks for spaces in paths
- **No git required**: `build`, `status`, `visualize`, `watch` now work on any directory without git
- **Plugin ready**: Skills registered in plugin.json, SKILL.md frontmatter fixed
- **File organization**: Generated files moved into `.code-review-graph/` directory (auto-created `.gitignore`, legacy migration)
- **Visualization density**: Starts collapsed (File nodes only), search bar, clickable edge type toggles, scale-aware layout for large graphs
- **Project cleanup**: Removed redundant `references/`, `agents/`, `settings.json`

## v1.4.0
- **`init` command**: Automatic `.mcp.json` setup for Claude Code integration
- **Interactive D3.js graph visualization**: `code-review-graph visualize` generates an HTML graph you can explore in-browser
- **Documentation overhaul**: Comprehensive docs audit across all reference files

## v1.3.0
- **Python version check with Docker fallback**: Automatically detects Python 3.10+ and suggests Docker if unavailable
- **Universal install**: `pip install code-review-graph` — no git clone needed
- **CLI entry point**: `code-review-graph` command available system-wide after pip install

## v1.2.0
- **Logging improvements**: Structured logging throughout the codebase
- **Watch debounce**: Smarter file-change detection in watch mode
- **tools.py fixes**: Bug fixes and reliability improvements for MCP tools
- **CI coverage**: GitHub Actions CI/CD pipeline with test coverage reporting

## v1.1.0
- **Watch mode**: `code-review-graph watch` — auto-rebuilds graph on file changes
- **Vector embeddings**: Optional `pip install .[embeddings]` for semantic code search
- **Go, Rust, Java verified**: 12+ languages with dedicated test coverage
- **47 tests passing**, 8 MCP tools registered
- README badges and cleaner install flow

## v1.0.0 (Foundation)
- **Persistent SQLite knowledge graph** — zero external dependencies
- **Tree-sitter multi-language parsing** — classes, functions, imports, calls, inheritance
- **Incremental updates** via `git diff` + automatic dependency cascade
- **Impact-radius / blast-radius analysis** — BFS through call/import/inheritance graph
- **6 MCP tools** for full graph interaction
- **3 review-first skills**: build-graph, review-delta, review-pr
- **PostToolUse hooks** (Write|Edit|Bash) for automatic background updates
- **FastMCP 3.0 compatible** stdio MCP server

## Privacy & Data
- All data stays 100% local
- Graph stored in `.code-review-graph/graph.db` (SQLite), auto-gitignored
- No telemetry, no network calls
- Respects `.gitignore` and `.code-review-graphignore`
