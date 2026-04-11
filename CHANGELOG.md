# Changelog

## [Unreleased]

### Added
- **Codex platform install support**: `code-review-graph install --platform codex` now appends a `mcp_servers.code-review-graph` section to `~/.codex/config.toml` without overwriting existing Codex settings

## [2.2.2] - 2026-04-08

### Added
- **Kotlin call extraction**: `simple_identifier` + `navigation_expression` support for Kotlin method calls (PR #107)
- **JUnit/Kotlin test detection**: Annotation-based test classification (`@Test`, `@ParameterizedTest`, etc.) for Java/Kotlin/C# (PR #107)

### Fixed
- **Windows encoding crash**: All `write_text`/`read_text` calls in `skills.py` now use `encoding='utf-8'` explicitly (PR #152, fixes #147, #148)
- **Invalid `--quiet` flag in hooks**: Removed non-existent `--quiet` and `--json` flags from generated hook commands (PR #152, fixes #149)

### Housekeeping
- Untracked `.claude-plugin/` directory and added to `.gitignore`
- GitHub issue triage: responded to 30+ issues, closed 14, reviewed 24 PRs

## [2.2.1] - 2026-04-07

### Added
- **Parallel parsing**: `ProcessPoolExecutor` for 3-5x faster builds (`CRG_PARSE_WORKERS`, `CRG_SERIAL_PARSE`)
- **Lazy post-processing**: `postprocess="full"|"minimal"|"none"` parameter, `run_postprocess` MCP tool + CLI command
- **SQLite-native BFS**: Recursive CTE replaces NetworkX for impact analysis (`CRG_BFS_ENGINE`)
- **Configurable limits**: `CRG_MAX_IMPACT_NODES`, `CRG_MAX_IMPACT_DEPTH`, `CRG_MAX_BFS_DEPTH`, `CRG_MAX_SEARCH_RESULTS`
- **Multi-hop dependents**: N-hop `find_dependents()` with `CRG_DEPENDENT_HOPS` (default 2) and 500-file cap
- **Token-efficient output**: `detail_level="minimal"` on 8 tools for 40-60% token reduction
- **`get_minimal_context` tool**: Ultra-compact entry point (~100 tokens) with task-based tool routing
- **Token-efficient prompts**: All 5 MCP prompts rewritten with minimal-first workflows
- **Incremental flow/community updates**: `incremental_trace_flows()`, `incremental_detect_communities()`
- **Visualization aggregation**: Community/file/auto modes with drill-down for large graphs (`--mode`)
- **Token-efficiency benchmarks**: 5 workflow benchmarks in `eval/token_benchmark.py`
- **DB schema v6**: Pre-computed `community_summaries`, `flow_snapshots`, `risk_index` tables
- **Token Efficiency Rules** in all skill templates and CLAUDE.md

### Changed
- CLI `build`/`update` support `--skip-flows`, `--skip-postprocess` flags
- PostToolUse hook uses `--skip-flows` for faster incremental updates
- VS Code extension schema version bumped to v6

### Fixed
- mypy type errors in parallel parsing and context tool
- Bandit false positive on prompt preamble string
- Import sorting in graph.py, main.py, tools/__init__.py
- Unused imports cleaned up in cli.py

### Housekeeping
- Gitignore: untrack `marketing-diagram.excalidraw`, `evaluate/results/`, `evaluate/reports/`
- Updated FEATURES.md, LLM-OPTIMIZED-REFERENCE.md, CHANGELOG.md for v2.2.1

## [2.1.0] - 2026-04-03

### Added
- **Jupyter notebook parsing**: Parse `.ipynb` files — extract functions, classes, imports across Python, R, and SQL cells
- **Databricks notebook parsing**: Parse Databricks `.py` notebook exports with `# COMMAND ----------` cell boundaries
- **Lua language support**: Full parsing for `.lua` files (functions, local functions, method calls, requires) — 20th language
- **Perl XS support**: Parse `.xs` files with improved Perl call detection and test coverage
- **Zero-config onboarding**: `install` now sets up skills, hooks, and CLAUDE.md by default so the graph is used automatically
- **Platform rule injection**: Graph instructions injected into all platform rule files (CLAUDE.md, .cursorrules, etc.) on install
- **Smart install detection**: Auto-detects whether installed via uvx or pip and generates correct `.mcp.json`
- **`--platform claude-code` alias**: Accepts both `claude` and `claude-code` as platform names

### Fixed
- **JS/TS arrow functions indexed**: `const foo = () => {}` and `const bar = function() {}` now correctly appear as nodes (#66)
- **`importers_of` path resolution**: Normalized with `resolve()` to match stored edge targets (#65)
- **Custom embedding models**: Support for custom model architectures and restored model param wiring in search (#79)

## [2.0.0] - 2026-03-27

### Added
- **12 new features**: flows, communities, hybrid search, change analysis, refactoring, hints, prompts, skills, wiki, multi-repo registry, migrations, eval framework
- **14 new modules** (~10,000 lines): `flows.py`, `communities.py`, `search.py`, `changes.py`, `refactor.py`, `hints.py`, `prompts.py`, `skills.py`, `wiki.py`, `registry.py`, `migrations.py`, `eval/`
- **15 new MCP tools**: `list_flows`, `get_flow`, `get_affected_flows`, `list_communities`, `get_community`, `get_architecture_overview`, `detect_changes`, `refactor`, `apply_refactor`, `generate_wiki`, `get_wiki_page`, `list_repos`, `cross_repo_search`, `find_large_functions`, `semantic_search_nodes`
- **5 MCP prompts**: `review_changes`, `architecture_map`, `debug_issue`, `onboard_developer`, `pre_merge_check`
- **7 new CLI commands**: `detect-changes`, `wiki`, `eval`, `register`, `unregister`, `repos`, `install --skills/--hooks/--all`
- **Interactive visualization upgrade**: Detail panel, community coloring, flow path highlighting, search-to-zoom, kind filters

### Security
- Fix path traversal in wiki page reader
- Add regex allowlist for git ref validation
- Add explicit SSL context for MiniMax API

### Fixed
- Fix git diff argument ordering (broke incremental updates)
- Fix `node_qualified_name` schema mismatch in wiki flow query
- Batch N+1 queries in `get_impact_radius` and risk scoring

### Architecture
- Decompose `_extract_from_tree` into 6 focused methods
- Add 17 public query methods to `GraphStore`
- Split `tools.py` into 10 themed sub-modules

## [1.8.4] - 2026-03-20

### Added
- **Vue SFC parsing**: Parse `.vue` Single File Components by extracting `<script>` blocks with automatic `lang="ts"` detection
- **Solidity support**: Full parsing for `.sol` files (functions, events, modifiers, inheritance)
- **`find_large_functions_tool`**: New MCP tool to find functions, classes, or files exceeding a line-count threshold
- **Call target resolution**: Bare call targets resolved to qualified names using same-file definitions (`_resolve_call_targets`)
- **Multi-word AND search**: `search_nodes` now requires all words to match (case-insensitive)
- **Impact radius pagination**: `get_impact_radius` returns `truncated` flag, `total_impacted` count, and accepts `max_results` parameter

### Changed
- Language count updated from 12 to 14 across all documentation
- MCP tool count updated from 8 to 9 across all documentation
- VS Code extension updated to v0.2.0 with 5 new commands documented

### Fixed
- Test assertions updated to handle qualified call targets from `_resolve_call_targets`

## [1.8.3] - 2026-03-20

### Fixed
- **Parser recursion guard**: Added `_MAX_AST_DEPTH = 180` limit to `_extract_from_tree()` preventing stack overflow on deeply nested ASTs
- **Module cache bound**: Added `_MODULE_CACHE_MAX = 15_000` with automatic eviction to prevent unbounded memory growth in `_module_file_cache`
- **Embeddings thread safety**: Added `check_same_thread=False` to `EmbeddingStore` SQLite connection
- **Embeddings retry logic**: Added `_call_with_retry()` with exponential backoff for Google Gemini API calls
- **Visualization XSS hardening**: Added `</` to `<\/` replacement in JSON serialization to prevent script injection
- **CLI error handling**: Split broad `except` into specific `json.JSONDecodeError` and `(KeyError, TypeError)` handlers
- **Git timeout**: Made configurable via `CRG_GIT_TIMEOUT` environment variable (default 30s)

### Added
- **Governance files**: Added CONTRIBUTING.md, SECURITY.md, CODE_OF_CONDUCT.md
- **Project URLs**: Added Homepage, Repository, Issues, Changelog URLs to pyproject.toml metadata

## [1.8.2] - 2026-03-17

### Fixed
- **C# parsing broken**: Renamed language identifier from `c_sharp` to `csharp` to match `tree-sitter-language-pack`'s actual identifier. Previously, all C# files were silently skipped because `_get_parser()` swallowed the `LookupError`.

## [1.8.1] - 2026-03-17

### Fixed
- Add missing `max_nodes` parameter to `get_impact_radius` method signature (caused `NameError` at runtime)
- Fix `.gitignore` test assertion to match expanded comment format

## [1.8.0] - 2026-03-17

### Security
- **Prompt injection mitigation**: Node names are now sanitized (control characters stripped, length capped at 256) before appearing in MCP tool responses, preventing graph-laundered prompt injection attacks
- **Path traversal protection**: `repo_root` parameter now validates that the target directory contains a `.git` or `.code-review-graph` directory, preventing arbitrary file exfiltration via MCP tools
- **VSCode RCE fix**: `cliPath` setting is now scoped to `machine` level only, preventing malicious workspace settings from pointing to attacker-controlled binaries
- **XSS fix in visualization**: `escH()` now escapes quotes and backticks in addition to angle brackets, closing stored XSS via crafted node names in generated HTML
- **SRI for CDN assets**: D3.js script tag now includes `integrity` and `crossorigin` attributes to prevent CDN compromise
- **Secure nonce generation**: VSCode webview CSP nonces now use `crypto.randomBytes()` instead of `Math.random()`
- **Symlink protection**: Build, watch mode, and file collection now skip symbolic links to prevent parsing files outside the repository
- **TOCTOU elimination**: File bytes are now read once, then hashed and parsed from the same buffer, closing the time-of-check-to-time-of-use gap

### Fixed
- **Thread-safe NetworkX cache**: Added `threading.Lock` around graph cache reads/writes to prevent race conditions between watch mode and MCP request handling
- **BFS resource limits**: Impact radius traversal now caps at 500 nodes to prevent memory exhaustion on dense graphs
- **SQL parameter batching**: `get_edges_among` now batches queries to stay under SQLite's variable limit on large node sets
- **Database path leakage**: Improved `.gitignore` inside `.code-review-graph/` with explicit warnings about absolute paths in the database

### Changed
- **Pinned dependency bounds**: All dependencies now have upper-bound version constraints to mitigate supply-chain risks

## [1.7.2] - 2026-03-09

### Fixed
- **Watch mode thread safety**: SQLite connections now use `check_same_thread=False` for Python 3.10/3.11 compatibility with watchdog's background threads
- **Full rebuild stale data**: `full_build` now purges nodes/edges from files deleted since last build
- **Removed unused dependency**: `gitpython` was listed in dependencies but never imported — removed to shrink install footprint
- **Stale Docker reference**: Removed non-existent Docker image suggestion from Python version check

## [1.7.0] - 2026-03-09

### Added
- **`install` command** — primary entry point for new users (`code-review-graph install`). `init` remains as an alias for backwards compatibility.
- **`--dry-run` flag** on `install`/`init` — shows what would be written without modifying files
- **PyPI publish workflow** — GitHub releases now automatically publish to PyPI via API token
- **Professional README** — complete rewrite with real benchmark data:
  - Code reviews: 6.8x average token reduction (tested on httpx, FastAPI, Next.js)
  - Live coding tasks: 14.1x average, up to 49.1x on large repos

### Changed
- README restructured around the install-and-forget user experience
- CLI banner now shows `install` as the primary command

## [1.6.4] - 2026-03-06

### Changed
- **Portable MCP config**: `init` now generates `uvx`-based `.mcp.json` instead of absolute Python paths — works on any machine with `uv` installed
- Removed `_safe_path` symlink workaround (no longer needed with `uvx`)

## [1.6.3] - 2026-03-06

### Added
- **SessionStart hook** — Claude Code now automatically prefers graph MCP tools over full codebase scans at the start of every session, saving tokens on general queries
- `homepage` and `author.url` fields in plugin.json for marketplace discoverability

### Fixed
- plugin.json schema: renamed `tags` to `keywords`, removed invalid `skills` path (auto-discovered from default location)
- Removed screenshot placeholder section from README

## [1.6.2] - 2026-02-27

### Fixed
- **Critical**: Incremental hash comparison bug — `file_hash` read from wrong field, causing every file to re-parse
- Watch mode `on_deleted` handler now filters by ignore patterns
- Removed dead code in `full_build` and duplicate `main()` in `incremental.py`
- `get_staged_and_unstaged` handles git renamed files (`R old -> new`)
- TROUBLESHOOTING.md hook config path corrected

### Added
- **Parser: C/C++ support** — full node extraction (structs, classes, functions, includes, calls, inheritance)
- **Parser: name extraction** fixes for Kotlin/Swift (`simple_identifier`), Ruby (`constant`), C/C++ nested `function_declarator`
- `GraphStore` context manager (`__enter__`/`__exit__`)
- `get_all_edges()` and `get_edges_among()` public methods on `GraphStore`
- NetworkX graph caching with automatic invalidation on writes
- Subprocess timeout (30s) on all git calls
- Progress logging every 50 files in full build
- SHA-256 hashing in embeddings (replaced MD5)
- Chunked embedding search (`fetchmany(500)`)
- Batch edge collection in `get_impact_radius` (single SQL query)
- ARIA labels throughout D3.js visualization
- **CI**: Coverage enforcement (`--cov-fail-under=50`), bandit security scanning, mypy type checking
- **Tests**: `test_incremental.py` (24 tests), `test_embeddings.py` (16 tests)
- **Test fixtures**: C, C++, C#, Ruby, PHP, Kotlin, Swift with multilang test classes
- **Docs**: API response schemas in COMMANDS.md, ignore patterns in USAGE.md

## [1.5.3] - 2026-02-27

### Fixed
- `init` now auto-creates symlinks when paths contain spaces (macOS iCloud, OneDrive, etc.)
- `build`, `status`, `visualize`, `watch` work without a git repository (falls back to cwd)
- Skills discoverable via plugin.json (`name` field added to SKILL.md frontmatter)

## [1.5.0] - 2026-02-26

### Added
- **File organization**: All generated files now live in `.code-review-graph/` directory instead of repo root
  - Auto-created `.gitignore` inside the directory prevents accidental commits
  - Automatic migration from legacy `.code-review-graph.db` at repo root
- **Visualization: start collapsed**: Only File nodes visible on load; click to expand children
- **Visualization: search bar**: Filter nodes by name or qualified name in real-time
- **Visualization: edge type toggles**: Click legend items to show/hide edge types (Calls, Imports, Inherits, Contains)
- **Visualization: scale-aware layout**: Force simulation adapts charge, distance, and decay for large graphs (300+ nodes)

### Changed
- Database path: `.code-review-graph.db` → `.code-review-graph/graph.db`
- HTML visualization path: `.code-review-graph.html` → `.code-review-graph/graph.html`
- `.code-review-graph/**` added to default ignore patterns (prevents self-indexing)

### Removed
- `references/` directory (duplicate of `docs/`, caused stale path references)
- `agents/` directory (unused, not wired into any code)
- `settings.json` at repo root (decorative, not loaded by code)

## [1.4.0] - 2026-02-26

### Added
- `init` command: automatic `.mcp.json` setup for Claude Code integration
- `visualize` command: interactive D3.js force-directed graph visualization
- `serve` command: start MCP server directly from CLI

### Changed
- Comprehensive documentation overhaul across all reference files

## [1.3.0] - 2026-02-26

### Added
- Universal installation: now works with `pip install code-review-graph[embeddings]` on Python 3.10+
- CLI entry point (`code-review-graph` command works after normal pip install)
- Clear Python version check with helpful Docker fallback for older Python users
- Improved README installation section with one-command + Docker option

### Changed
- Minimum Python requirement lowered from 3.11 → 3.10 (covers ~90% of users)

### Fixed
- Installation friction for most developers
