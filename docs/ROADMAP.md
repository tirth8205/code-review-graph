# Roadmap

## Shipped

### v2.3.6
- **Custom languages without forking**: `.code-review-graph/languages.toml` maps extensions and node types to any tree-sitter-language-pack grammar (`docs/CUSTOM_LANGUAGES.md`)
- **GitHub Action** for risk-scored PR review comments: graph built/restored on the CI runner, sticky comment upserted per push, optional `fail-on-risk` merge gate; dogfooded via `.github/workflows/pr-review.yml` (`docs/GITHUB_ACTION.md`)
- **`agent_baseline` benchmark**: graph queries vs a realistic grep-and-read-top-k agent baseline, wired into all six pinned eval configs
- **Co-change ground truth** for `impact_accuracy`; the legacy graph-derived metric is labelled as a circular upper bound
- **Weekly eval CI**: report-only cron run of the two smallest configs (`.github/workflows/eval.yml`)
- **`docs/FAQ.md`**: comparisons with LSP, RAG, grep/agentic search, and adjacent tools, plus when-not-to-use guidance
- **Contribution scaffolding**: issue forms, PR template, dependabot config
- **Windows fixes** for `daemon status` (#511) and `detect-changes` path mapping (#528)
- **Reliability**: embedding provider-name validation, SQLite store-leak fixes in analysis/wiki tools, `fastmcp<4` cap, hooks installed via `git rev-parse --git-path hooks`

### v2.3.5
- **Token Savings panel** on `detect-changes --brief` and the new `update --brief` — boxed CLI output with per-category breakdown that sums exactly to the graph response size
- **`--verify` flag** cross-checks the displayed savings against OpenAI's `cl100k_base` tokenizer; calibration data committed in `docs/REPRODUCING.md` shows the estimate is within ~1% of real GPT-4 tokens in aggregate
- **`code-review-graph embed`** CLI subcommand for explicit embedding generation
- **Deterministic eval pipeline**: pinned upstream SHAs in every config, full clones with `returncode` checks, fixed-seed Leiden community detection (`CRG_LEIDEN_SEED`)
- **`multi_hop_retrieval` benchmark**: 11 curated 2-step tool-chain tasks; average score 0.909
- **Richer embedding text** and **identifier-aware search boost** lift multi-hop accuracy from 0.545 to 0.909
- **Path normalization fix** in the eval pipeline + test-gap dedup in the brief summary
- **`docs/REPRODUCING.md`**: end-to-end recipe with canonical numbers and tiktoken calibration table
- Demo GIF (`diagrams/context-savings-demo.gif`) showing both CLI surfaces and `--verify`

### v2.3.4
- 30 MCP tools and 5 MCP prompts
- Estimated context savings metadata for review, impact, detect-changes, and compact architecture responses
- Compact architecture overview by default to reduce large MCP payloads
- Bounded change-analysis controls for large diffs (`CRG_MAX_CHANGED_FUNCS`, `CRG_MAX_TRANSITIVE_FRONTIER`, `CRG_TOOL_TIMEOUT`)
- Windows FastMCP semantic-search deadlock mitigation
- Rust test detection and path lookup correctness fixes
- Documentation and release metadata refreshed for the 2.3.4 release

### v2.3.3
- Broad parser surface expansion across source languages, shell scripts, notebooks, and SFC-style files
- Additional AI coding platform install targets including Gemini CLI, Qwen, Kiro, Qoder, and GitHub Copilot variants
- Streamable HTTP MCP transport on localhost
- Parser/resolver, Windows, FastMCP, and daemon reliability fixes
- Community PR sweep and VS Code accessibility improvements

### v2.2.0
- Multi-repo watch daemon (`crg-daemon` / `code-review-graph daemon`)
- TOML-based daemon configuration (`~/.code-review-graph/watch.toml`)
- Child process management: one `code-review-graph watch` process per repo
- Config file watching with automatic reconciliation of watcher processes
- Daemonization with PID file management
- Health checking with automatic restart of dead watchers
- Standalone `crg-daemon` CLI entry point (7 subcommands)
- Integrated `daemon` subcommand group in main CLI

### v2.0.0
- 22 MCP tools (up from 9) and 5 MCP prompts
- 18 languages (added Dart, R, Perl)
- Execution flow detection with criticality scoring
- Community detection (Leiden algorithm via igraph, file-based fallback)
- Architecture overview with coupling warnings
- Risk-scored change detection (`detect_changes`)
- Refactoring tools (rename preview, dead code, suggestions)
- Wiki generation from community structure
- Multi-repo registry with cross-repo search
- FTS5 full-text search with porter stemming
- Database migrations (v1-v5)
- Evaluation framework with matplotlib visualization
- TypeScript tsconfig path alias resolution
- MiniMax embedding provider (embo-01)
- Optional dependency groups: `[embeddings]`, `[google-embeddings]`, `[communities]`, `[eval]`, `[wiki]`, `[all]`
- 486 tests across 22 test files

### v1.8.4
- Multi-word AND search, call target resolution, impact radius pagination
- `find_large_functions_tool`, Vue SFC and Solidity support
- Documentation overhaul

### v1.7.0
- `install` command as primary entry point (`init` kept as alias)
- `--dry-run` flag for previewing install/init changes
- Automatic PyPI publishing via GitHub Actions on release
- README rewrite with real benchmark data from httpx, FastAPI, and Next.js

### v1.6.x
- Portable `uvx`-based MCP config
- SessionStart hook for automatic graph tool preference
- 24 audit fixes: C/C++ support, performance, CI hardening

### v1.5.x
- Generated files in `.code-review-graph/` directory
- Visualization density: collapsed start, search, edge toggles
- Works without git

### v1.4.0
- `init` command, interactive D3.js visualization, `serve` command

### v1.3.0
- Universal pip install, CLI entry point, Python version check

### v1.1.0-v1.2.0
- Watch mode, vector embeddings, logging, CI coverage

### v1.0.0 (Foundation)
- Persistent SQLite knowledge graph, Tree-sitter parsing, incremental updates
- Impact radius analysis, 6 MCP tools, 3 skills

## Planned

- GitHub App / bot mode beyond the shipped GitHub Action (org-wide install, check runs)
- Team sync (shared graph via git-tracked DB)
- Performance optimization for monorepos (>50k files)

## Ongoing

- Additional language grammars as requested
- Integration updates as AI coding platforms evolve
