# Features

Release highlights by version. The full changelog is in [CHANGELOG.md](../CHANGELOG.md).

## v2.3.8 (Current)
- **Bounded MCP responses**: #849 found `get_affected_flows` returning ~247k tokens inside a workflow documented as "5 tool calls, 800 tokens total". A sweep of the other 29 tools found the same bug in ten more places: `list_communities` returned 206k tokens with default arguments, `get_community` 134k, `get_architecture_overview` 625k in standard mode. All are now capped under one contract: `total` (or a per-list `*_total`) reports the untruncated count, `truncated` marks a cut, and the summary says how many of how many are shown. Cap parameters reject values below 1 and reject booleans. `detail_level="minimal"` was added to the analysis and refactor tools. `tests/test_token_budget.py` pins the per-tool budget table so a removed cap fails CI. Four query tools (`get_impact_radius`, `find_large_functions`, `traverse_graph`, `semantic_search_nodes`) are still unbounded in the worst case and are tracked in #888. See [COMMANDS.md](COMMANDS.md#result-bounds).
- **Framework-aware PHP parsing**: traits, enums, object creation and base clauses are indexed. Composer PSR-4 resolution is longest-prefix, multi-directory, cached and bounded to the repository. Blade references ignore comments and escaped directives. Laravel Route and Eloquent edges require explicit framework, import or receiver evidence.
- **Custom languages without forking**: a `.code-review-graph/languages.toml` file indexes any grammar shipped by tree-sitter-language-pack (extension map plus node-type lists, validated and capped). Built-in languages always win. See [CUSTOM_LANGUAGES.md](CUSTOM_LANGUAGES.md).
- **GitHub Action for risk-scored PR reviews**: the composite `action.yml` builds or restores the graph from the CI cache, runs `detect-changes` against the PR base, and updates one sticky comment with a risk table, affected flows, test gaps and the Token Savings line. Optional `fail-on-risk` merge gate. This repository runs it in `.github/workflows/pr-review.yml`. See [GITHUB_ACTION.md](GITHUB_ACTION.md).
- **`agent_baseline` eval benchmark**: compares graph queries with a grep-and-read-top-3 agent baseline instead of the whole-corpus baseline. Wired into all six pinned eval configs.
- **Co-change ground truth for `impact_accuracy`**: predictions are also graded against the files co-changed in the same commit. The graph-derived metric is labelled "circular (upper bound)".
- **Weekly eval CI**: `.github/workflows/eval.yml` runs a report-only cron on the two smallest pinned configs and uploads CSV artifacts with a job summary.
- **docs/FAQ.md**: comparison with LSP, RAG, grep and adjacent tools; when not to use it; verification steps; monorepo, worktree and registry guidance.
- **Contribution scaffolding**: issue forms (bug, feature, platform), a PR template mirroring the CONTRIBUTING checklist, and dependabot config for pip and GitHub Actions.
- **Windows fixes**: `daemon status` no longer fails with WinError 87 (#511). CLI `detect-changes` maps diff paths to absolute native paths, so it no longer reports 0 functions (#528).
- **Provider-name validation**: an unknown embedding provider raises an error listing the valid names instead of falling back to the local model.
- **Connection leaks fixed**: the five analysis MCP tools and the wiki-page tool close their SQLite connections (`try/finally store.close()`).
- **`fastmcp<4` cap**: the next fastmcp major release cannot break the server silently.
- **Worktree-safe git hooks**: `install` resolves the hooks directory with `git rev-parse --git-path hooks`, so linked worktrees and `core.hooksPath` (husky) setups get a working pre-commit hook.

## v2.3.5
- **Token Savings panel**: `detect-changes --brief` and the new `update --brief` print a boxed panel with the full-context baseline, graph response size, saved tokens, percentage, and a per-category breakdown (Functions / Tests / Risk / Other) that sums to the graph response size.
- **`--verify` flag**: adds a `Verified (tiktoken)` row computed with OpenAI's `cl100k_base` tokenizer. Calibration across 222 files put the aggregate estimate within about 1% of real tokens; see [REPRODUCING.md](REPRODUCING.md#calibration-result-committed).
- **`update --brief`**: incremental update plus the risk panel in one command. `detect-changes --brief` is read-only against the existing graph; use `update --brief` when the graph may be stale (after a rebase or a large change set).
- **`embed` CLI subcommand**: embedding generation from the shell. Previously reachable only over MCP.
- **Deterministic eval pipeline**: all 6 eval configs pin upstream SHAs, `eval/runner.py` uses full clones with explicit `returncode` checks, and Leiden runs with a fixed seed (`CRG_LEIDEN_SEED=42`).
- **`multi_hop_retrieval` benchmark**: 11 hand-curated two-step tool-chain tasks (`hybrid_search` then `query_graph`) across the 6 test repos. Average score 0.909.
- **Richer semantic search**: embedding text includes the dotted form (`Module.Class.method`), word-split identifiers, and the enclosing module directory. The multi-hop score rose from 0.545 to 0.909.
- **Identifier-aware search boost**: `extract_query_identifiers` pulls dotted, snake_case and CamelCase tokens out of natural-language queries and doubles the score of matching qualified names in hybrid search.
- **Path normalisation fix**: `eval/runner.py` resolves repo paths before storing them, so eval-built and CLI-built graphs match and `update` does not create duplicate nodes.
- **Test-gap dedup**: the `Untested:` line in the brief summary dedupes by bare name.
- **FTS5 rebuild in eval**: the eval framework calls `run_post_processing` after `full_build`, so the FTS5 index is populated.

## v2.3.4
- **Estimated context savings**: review, impact, detect-changes and compact architecture responses include `context_savings` metadata (`estimated`, `saved_tokens`, `saved_percent`) where a baseline can be estimated.
- **Compact architecture overview by default**: `get_architecture_overview_tool` defaults to `detail_level="minimal"`. Use `detail_level="standard"` for member lists and per-edge detail.
- **Bounded change analysis**: `CRG_MAX_CHANGED_FUNCS`, `CRG_MAX_TRANSITIVE_FRONTIER` and `CRG_TOOL_TIMEOUT` keep large MCP review calls responsive.
- **Windows MCP reliability**: local embedding models are pre-warmed on Windows before FastMCP starts worker dispatch, avoiding semantic-search deadlocks.
- **Parser correctness**: Rust `#[test]` and common async test attributes produce `Test` nodes.
- **Graph lookup correctness**: review, impact and file-summary tools resolve user-facing paths to stored graph paths; `callers_of` includes cross-file callers even when same-file callers exist.
- **Install/runtime reliability**: generated Codex/Claude hooks drain stdin, bundled docs ship in wheels, missing local embeddings report an unavailable status, and `.svn` roots pass validation.
- **CLI reliability**: `build --skip-postprocess` and `update --skip-flows` honour the requested post-processing level.
- **Broad parser surface**: see the language list in [USAGE.md](USAGE.md#supported-languages).
- **Local-first**: SQLite graph storage stays local, with no telemetry and no cloud-default behaviour.

## v2.0.0
- **22 MCP tools** (up from 9): 13 new tools for flows, communities, architecture, refactoring, wiki, multi-repo, and risk-scored change detection.
- **5 MCP prompts**: `review_changes`, `architecture_map`, `debug_issue`, `onboard_developer`, `pre_merge_check`.
- **18 languages** (up from 15): added Dart, R, Perl.
- **Execution flows**: trace call chains from entry points (HTTP handlers, CLI commands, tests), sorted by criticality score.
- **Community detection**: cluster related code with the Leiden algorithm (igraph) or file-based grouping.
- **Architecture overview**: architecture map with module summaries and cross-community coupling warnings.
- **Risk-scored change detection**: `detect_changes` maps git diffs to affected functions, flows, communities and test coverage gaps, in priority order.
- **Refactoring tools**: rename preview with edit list, dead code detection, community-driven refactoring suggestions.
- **Wiki generation**: markdown wiki pages for each community.
- **Multi-repo registry**: register several repositories and search across them with `cross_repo_search`.
- **Full-text search**: FTS5 virtual table with porter stemming for hybrid keyword and vector search.
- **Database migrations**: versioned schema migrations with automatic upgrade on startup.
- **Optional dependency groups**: `[embeddings]`, `[google-embeddings]`, `[communities]`, `[eval]`, `[wiki]`, `[all]`.
- **Evaluation framework**: benchmark suite with matplotlib reports.
- **TypeScript path resolution**: tsconfig.json `paths`/`baseUrl` alias resolution for imports.

## v1.8.4
- **Multi-word AND search**: `search_nodes` requires all words to match (case-insensitive).
- **Call target resolution**: bare call targets are resolved to qualified names using same-file definitions, improving `callers_of`/`callees_of`.
- **Impact radius pagination**: `get_impact_radius` returns a `truncated` flag and `total_impacted` count; `max_results` controls output size.
- **`find_large_functions_tool`**: find functions, classes or files above a line-count threshold.
- **15 languages**: added Vue SFC and Solidity.

## v1.8.3
- **Parser recursion guard**: `_MAX_AST_DEPTH = 180` prevents stack overflow on deeply nested ASTs.
- **Module cache bound**: `_MODULE_CACHE_MAX = 15,000` with automatic eviction.
- **Embeddings thread safety**: `check_same_thread=False` on the EmbeddingStore SQLite connection.
- **Embeddings retry**: exponential backoff for Google Gemini API calls.
- **Visualisation XSS hardening**: `</` escaped to `<\/` in JSON serialisation.
- **CLI error handling**: broad `except` split into specific handlers.
- **Git timeout**: configurable through `CRG_GIT_TIMEOUT`.
- **Governance files**: CONTRIBUTING.md, SECURITY.md, CODE_OF_CONDUCT.md.

## v1.8.2
- **C# parsing fix**: language identifier renamed from `c_sharp` to `csharp`.
- **Watch mode thread safety**: SQLite connections compatible with Python 3.10/3.11 watchdog threads.
- **Full rebuild cleanup**: stale data from deleted files is purged during a full rebuild.
- **Dependency trim**: removed the unused `gitpython` dependency.

## v1.7.0
- **`install` command**: primary entry point for setup. `init` remains as an alias.
- **`--dry-run` flag**: preview what `install`/`init` would write.
- **PyPI auto-publish**: GitHub releases publish to PyPI.

## v1.6.4
- **Portable MCP config**: `init` generates a `uvx`-based `.mcp.json` with no absolute paths.
- **Removed symlink workaround**: the `_safe_path` helper for spaces in paths is no longer needed.

## v1.6.3
- **SessionStart hook**: Claude Code prefers graph MCP tools over full codebase scans at session start.
- **Marketplace ready**: plugin.json corrected for the Claude Code plugin marketplace.

## v1.6.2
- **24 audit fixes**: bug fixes, performance improvements, parser fixes, more tests.
- **C/C++ support**: classes, functions, imports, calls, inheritance.
- **Name extraction fixes**: Kotlin, Swift (`simple_identifier`), Ruby (`constant`).
- **Performance**: NetworkX graph caching, batch edge queries, chunked embedding search, git subprocess timeouts.
- **CI hardening**: coverage enforcement, bandit security scan, mypy type checking.
- **Accessibility**: ARIA labels in the D3.js visualisation.

## v1.5.3
- **No git required**: `build`, `status`, `visualize` and `watch` work on any directory.
- **File organisation**: generated files moved into `.code-review-graph/` (auto-created `.gitignore`, legacy migration).
- **Visualisation density**: starts collapsed (File nodes only), search bar, clickable edge type toggles, scale-aware layout for large graphs.

## v1.4.0
- **`init` command**: automatic `.mcp.json` setup for Claude Code.
- **Interactive D3.js visualisation**: `code-review-graph visualize` writes an HTML graph.

## v1.3.0
- **Python version check with Docker fallback**: detects Python 3.10+ and suggests Docker if unavailable.
- **`pip install code-review-graph`**: no git clone needed; `code-review-graph` command available after install.

## v1.2.0
- **Structured logging** throughout the codebase.
- **Watch debounce**: better file-change detection in watch mode.
- **CI**: GitHub Actions pipeline with test coverage reporting.

## v1.1.0
- **Watch mode**: `code-review-graph watch` rebuilds the graph on file changes.
- **Vector embeddings**: optional `[embeddings]` extra for semantic code search.
- **Go, Rust, Java** verified with dedicated tests.

## v1.0.0
- **Persistent SQLite knowledge graph** with no external database.
- **Tree-sitter multi-language parsing**: classes, functions, imports, calls, inheritance.
- **Incremental updates** via `git diff` with dependency cascade.
- **Impact-radius analysis**: BFS through the call, import and inheritance graph.
- **6 MCP tools**, **3 skills** (build-graph, review-delta, review-pr), and **PostToolUse hooks** (Write|Edit|Bash) for background updates.

## Privacy & Data
- Graph data is stored locally in `.code-review-graph/graph.db` (SQLite), auto-gitignored.
- No telemetry. Core graph and review workflows need no network access.
- Optional embedding features call local or remote services only when explicitly enabled.
- Respects `.gitignore` and `.code-review-graphignore`.
