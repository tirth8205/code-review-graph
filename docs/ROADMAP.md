# Roadmap

## Shipped

Releases after v2.3.6 are listed in [CHANGELOG.md](../CHANGELOG.md).

### v2.3.6
- Custom languages without forking: `.code-review-graph/languages.toml` maps extensions and node types to any tree-sitter-language-pack grammar (`docs/CUSTOM_LANGUAGES.md`)
- GitHub Action for risk-scored PR review comments: graph built or restored on the CI runner, one sticky comment per PR, optional `fail-on-risk` gate; used on this repository via `.github/workflows/pr-review.yml` (`docs/GITHUB_ACTION.md`)
- `agent_baseline` benchmark: graph queries against a grep-and-read-top-k baseline, in all six pinned eval configs
- Co-change ground truth for `impact_accuracy`; the graph-derived metric is labelled a circular upper bound
- Weekly eval CI: report-only cron run of the two smallest configs (`.github/workflows/eval.yml`)
- `docs/FAQ.md`: comparisons with LSP, RAG, grep and agentic search, and when not to use the tool
- Contribution scaffolding: issue forms, PR template, dependabot config
- Windows fixes for `daemon status` (#511) and `detect-changes` path mapping (#528)
- Reliability: embedding provider-name validation, SQLite store-leak fixes in analysis and wiki tools, `fastmcp<4` cap, hooks installed via `git rev-parse --git-path hooks`

### v2.3.5
- Token Savings panel on `detect-changes --brief` and the new `update --brief`, with a per-category breakdown that sums to the graph response size
- `--verify` flag cross-checks the displayed savings against OpenAI's `cl100k_base` tokenizer; the calibration table in `docs/REPRODUCING.md` puts the estimate within about 1% of real tokens in aggregate
- `code-review-graph embed` subcommand for explicit embedding generation
- Deterministic eval pipeline: pinned upstream SHAs in every config, full clones with `returncode` checks, fixed-seed Leiden community detection (`CRG_LEIDEN_SEED`)
- `multi_hop_retrieval` benchmark: 11 curated two-step tool-chain tasks; average score 0.909
- Richer embedding text and identifier-aware search boost lift multi-hop accuracy from 0.545 to 0.909
- Path normalisation fix in the eval pipeline; test-gap dedup in the brief summary
- `docs/REPRODUCING.md`: end-to-end recipe with canonical numbers and the tiktoken calibration table
- Demo GIF (`diagrams/context-savings-demo.gif`) showing both CLI surfaces and `--verify`

### v2.3.4
- 30 MCP tools and 5 MCP prompts
- Estimated context-savings metadata for review, impact, detect-changes and compact architecture responses
- Compact architecture overview by default
- Bounded change analysis for large diffs (`CRG_MAX_CHANGED_FUNCS`, `CRG_MAX_TRANSITIVE_FRONTIER`, `CRG_TOOL_TIMEOUT`)
- Windows FastMCP semantic-search deadlock mitigation
- Rust test detection and path lookup fixes

### v2.3.3
- Parser coverage extended across source languages, shell scripts, notebooks and SFC-style files
- Install targets for Gemini CLI, Qwen, Kiro, Qoder and GitHub Copilot variants
- Streamable HTTP MCP transport on localhost
- Parser/resolver, Windows, FastMCP and daemon fixes
- Community PR sweep and VS Code accessibility improvements

### v2.2.0
- Multi-repo watch daemon (`crg-daemon` / `code-review-graph daemon`)
- TOML daemon configuration (`~/.code-review-graph/watch.toml`)
- One `code-review-graph watch` child process per repo, config-file watching with reconciliation, PID file, health checks with restart
- Standalone `crg-daemon` entry point (7 subcommands) and a `daemon` subcommand group in the main CLI

### v2.0.0
- 22 MCP tools (up from 9) and 5 MCP prompts
- 18 languages (added Dart, R, Perl)
- Execution flow detection with criticality scoring
- Community detection (Leiden via igraph, file-based fallback)
- Architecture overview with coupling warnings
- Risk-scored change detection (`detect_changes`)
- Refactoring tools (rename preview, dead code, suggestions)
- Wiki generation from community structure
- Multi-repo registry with cross-repo search
- FTS5 full-text search with porter stemming
- Database migrations (v1 to v5)
- Evaluation framework with matplotlib reports
- TypeScript tsconfig path alias resolution
- MiniMax embedding provider (embo-01)
- Optional dependency groups: `[embeddings]`, `[google-embeddings]`, `[communities]`, `[eval]`, `[wiki]`, `[all]`
- 486 tests across 22 test files

### v1.8.4
- Multi-word AND search, call target resolution, impact radius pagination
- `find_large_functions_tool`, Vue SFC and Solidity support
- Documentation overhaul

### v1.7.0
- `install` command as the primary entry point (`init` kept as an alias)
- `--dry-run` for install/init
- PyPI publishing via GitHub Actions on release
- README rewrite with benchmark data from httpx, FastAPI and Next.js

### v1.6.x
- Portable `uvx`-based MCP config
- SessionStart hook for graph tool preference
- 24 audit fixes: C/C++ support, performance, CI hardening

### v1.5.x
- Generated files moved to `.code-review-graph/`
- Visualisation: collapsed start, search, edge toggles
- Works without git

### v1.4.0
- `init` command, interactive D3.js visualisation, `serve` command

### v1.3.0
- pip install, CLI entry point, Python version check

### v1.1.0 to v1.2.0
- Watch mode, vector embeddings, logging, CI coverage

### v1.0.0
- Persistent SQLite knowledge graph, Tree-sitter parsing, incremental updates
- Impact radius analysis, 6 MCP tools, 3 skills

## Planned

- GitHub App / bot mode beyond the shipped GitHub Action (org-wide install, check runs)
- Team sync (shared graph via a git-tracked database)
- Performance work for monorepos (more than 50k files)

## Ongoing

- Additional language grammars as requested
- Integration updates as AI coding platforms change
