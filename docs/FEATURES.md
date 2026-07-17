# Features

## v2.3.6 (Current)
- **Custom languages without forking**: drop a `.code-review-graph/languages.toml` into your repo to index any grammar shipped by tree-sitter-language-pack — extension map plus node-type lists, validated and capped, with built-in languages always winning. See [CUSTOM_LANGUAGES.md](CUSTOM_LANGUAGES.md).
- **GitHub Action for risk-scored PR reviews**: composite `action.yml` builds/restores the graph from CI cache, runs `detect-changes` against the PR base, and upserts a sticky comment with risk table, affected flows, test gaps, and the Token Savings line. Optional `fail-on-risk` merge gate. Dogfooded on this repo via `.github/workflows/pr-review.yml`. See [GITHUB_ACTION.md](GITHUB_ACTION.md).
- **`agent_baseline` eval benchmark**: compares graph queries against a realistic grep-and-read-top-k agent baseline instead of the whole-corpus strawman; wired into all six pinned eval configs.
- **Co-change ground truth for `impact_accuracy`**: predictions are also graded against files actually co-changed in the same commit; the legacy metric is explicitly labelled "graph-derived (circular — upper bound)".
- **Weekly eval CI**: `.github/workflows/eval.yml` runs a report-only cron of the two smallest pinned configs with CSV artifacts and a job summary.
- **docs/FAQ.md**: how CRG compares to LSP, RAG, grep/agentic search, and adjacent tools; when NOT to use it; verification steps; monorepo/worktree and registry guidance.
- **Contribution scaffolding**: GitHub issue forms (bug/feature/platform), a PR template mirroring the CONTRIBUTING checklist, and dependabot config for pip + GitHub Actions.
- **Windows fixes**: `daemon status` no longer crashes with WinError 87 (#511), and CLI `detect-changes` maps diff paths to absolute native paths so it no longer reports 0 functions (#528).
- **Provider-name validation**: unknown embedding provider names raise a clear error listing valid providers instead of silently falling back to the local model.
- **Store-leak fixes**: the five analysis MCP tools and the wiki-page tool no longer leak SQLite connections (try/finally `store.close()`).
- **`fastmcp<4` cap**: the next fastmcp major can no longer silently break the server.
- **Worktree-safe git hooks**: `install` resolves the real hooks directory via `git rev-parse --git-path hooks`, so linked worktrees and `core.hooksPath` (husky) setups get a working pre-commit hook.

## v2.3.5
- **Token Savings panel on every brief CLI call**: `code-review-graph detect-changes --brief` and the new `code-review-graph update --brief` print a boxed `Token Savings` panel — full-context baseline, graph response, saved tokens, percent, and per-category breakdown (Functions / Tests / Risk / Other) that sums exactly to the graph response size.
- **`--verify` flag**: cross-checks the displayed numbers against OpenAI's `cl100k_base` tokenizer (the GPT-4 family). Adds a second `Verified (tiktoken)` row showing real token counts. Calibration across 222 mixed-language files shows the estimate is within ~1% of real tokens in aggregate.
- **`update --brief`**: incremental update + the same risk panel in one command. Distinct from `detect-changes --brief` (which is read-only against the existing graph) — use update when the graph might be stale (post-rebase, large change set).
- **`code-review-graph embed` CLI subcommand**: explicit shell-level access to embedding generation. Previously only reachable via MCP.
- **Deterministic eval pipeline**: all 6 eval configs pin upstream SHAs, `eval/runner.py` uses full clones with explicit `returncode` checks, and Leiden community detection uses a fixed seed (`CRG_LEIDEN_SEED=42`). Two runs on different machines produce identical numbers.
- **`multi_hop_retrieval` benchmark**: 11 hand-curated 2-step tool-chain tasks (`hybrid_search` → `query_graph`) across the 6 test repos. Average score 0.909.
- **Richer semantic search**: `embeddings._node_to_text` now includes the dotted form (`Module.Class.method`), word-split identifiers, and enclosing module directory. Search ranking on natural-language queries improved from 0.545 → 0.909 on the multi-hop benchmark.
- **Identifier-aware search boost**: `extract_query_identifiers` pulls dotted / snake_case / CamelCase tokens out of NL queries and boosts matching qualified-names ×2.0 in hybrid search.
- **Path normalization fix**: `eval/runner.py` now resolves repo paths absolutely before storing, so the eval-built graph matches the CLI/MCP-built graph and `update` doesn't create duplicate nodes for the same source location.
- **Test-gap dedup**: the `Untested:` line in the brief summary dedupes by bare name (defensive guard if duplicate qualified_names slip in).
- **FTS5 auto-rebuild in eval**: the eval framework now calls `run_post_processing` after `full_build`, so FTS5 is populated automatically instead of leaving the index empty.

## v2.3.4
- **Estimated context savings**: Review, impact, detect-changes, and compact architecture responses include tiny `context_savings` metadata (`estimated`, `saved_tokens`, `saved_percent`) where a baseline can be estimated.
- **Compact architecture overview by default**: `get_architecture_overview_tool` defaults to `detail_level="minimal"` to avoid huge member lists and per-edge payloads. Use `detail_level="standard"` for full detail.
- **Bounded change analysis**: `CRG_MAX_CHANGED_FUNCS`, `CRG_MAX_TRANSITIVE_FRONTIER`, and `CRG_TOOL_TIMEOUT` help keep large MCP review calls responsive.
- **Windows MCP reliability**: Local embedding models are pre-warmed on Windows before FastMCP starts worker dispatch to avoid semantic-search deadlocks.
- **Parser correctness**: Rust `#[test]` and common async test attributes now produce `Test` nodes.
- **Graph lookup correctness**: Review, impact, and file-summary tools resolve user-facing paths to stored graph paths; `callers_of` includes cross-file callers even when same-file callers exist.
- **Install/runtime reliability**: Generated Codex/Claude hooks drain stdin, bundled docs are available from wheels, missing local embeddings report unavailable status, and `.svn` roots pass validation.
- **CLI reliability**: `build --skip-postprocess` and `update --skip-flows` honor the requested post-processing level.
- **Broad parser surface**: Python, JavaScript/TypeScript/TSX, Go, Rust, Java, C/C++, C#, Ruby, Kotlin, Swift, PHP, Scala, Solidity, Dart, R, Perl, Lua/Luau, Objective-C, shell scripts, Elixir, Zig, PowerShell, Julia, ReScript, GDScript, Nix, Verilog/SystemVerilog, SQL, Vue/Svelte SFCs, Astro files parsed through the TypeScript parser, Jupyter/Databricks notebooks, and Perl XS files.
- **Local-first by design**: SQLite graph storage remains local, with no telemetry and no cloud-default behavior.

## v2.0.0
- **22 MCP tools** (up from 9): 13 new tools for flows, communities, architecture, refactoring, wiki, multi-repo, and risk-scored change detection.
- **5 MCP prompts**: `review_changes`, `architecture_map`, `debug_issue`, `onboard_developer`, `pre_merge_check` workflow templates.
- **18 languages** (up from 15): Added Dart, R, Perl support.
- **Execution flows**: Trace call chains from entry points (HTTP handlers, CLI commands, tests), sorted by criticality score.
- **Community detection**: Cluster related code entities via Leiden algorithm (igraph) or file-based grouping.
- **Architecture overview**: Auto-generated architecture map with module summaries and cross-community coupling warnings.
- **Risk-scored change detection**: `detect_changes` maps git diffs to affected functions, flows, communities, and test coverage gaps with priority ordering.
- **Refactoring tools**: Rename preview with edit list, dead code detection, community-driven refactoring suggestions.
- **Wiki generation**: Auto-generate markdown wiki pages for each community with optional LLM summaries (ollama).
- **Multi-repo registry**: Register multiple repositories, search across all of them with `cross_repo_search`.
- **Full-text search**: FTS5 virtual table with porter stemming for hybrid keyword + vector search.
- **Database migrations**: Versioned schema migrations (v1-v5) with automatic upgrade on startup.
- **Optional dependency groups**: `[embeddings]`, `[google-embeddings]`, `[communities]`, `[eval]`, `[wiki]`, `[all]`.
- **Evaluation framework**: Benchmark suite with matplotlib visualization.
- **TypeScript path resolution**: tsconfig.json paths/baseUrl alias resolution for imports.
- **486 tests** across 22 test files.

## v1.8.4
- **Multi-word AND search**: `search_nodes` now requires all words to match (case-insensitive), producing more precise results.
- **Call target resolution**: Bare call targets are resolved to qualified names using same-file definitions, improving `callers_of`/`callees_of` accuracy.
- **Impact radius pagination**: `get_impact_radius` returns `truncated` flag and `total_impacted` count; `max_results` parameter controls output size.
- **`find_large_functions_tool`**: New MCP tool to find functions, classes, or files exceeding a line-count threshold.
- **15 languages**: Added Vue SFC and Solidity support.
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
- Core graph data is stored locally
- Graph stored in `.code-review-graph/graph.db` (SQLite), auto-gitignored
- No telemetry; core graph/review workflows do not require network access
- Optional embedding and wiki features may call configured local or remote services when explicitly enabled
- Respects `.gitignore` and `.code-review-graphignore`
