# CLAUDE.md - Project Context for Claude Code

## Project Overview

code-review-graph is a local-first knowledge graph for code review. It parses a repository with Tree-sitter (plus targeted fallbacks), stores nodes and edges in SQLite, updates the graph incrementally from Git changes, and serves compact context over MCP and the CLI to AI coding tools. Supported platforms are the entries in `PLATFORMS` in `skills.py`: Claude Code, Codex, Cursor, Windsurf, Zed, Continue, OpenCode, Antigravity, Gemini CLI, Qwen Code, Kiro, Qoder, GitHub Copilot, GitHub Copilot CLI, Hermes Agent and CodeBuddy Code.

## Graph Tool Usage (Token-Efficient)

When using code-review-graph MCP tools:

1. Call `get_minimal_context_tool(task="<description>")` first. It costs about 100 tokens and gives the overview.
2. Use `detail_level="minimal"` on every later call unless that is not enough.
3. Prefer `query_graph_tool` with a specific target over broad `list_*` calls.
4. Follow the `next_tool_suggestions` field in each response.
5. Target: at most 5 tool calls and 800 tokens of graph context per task.

All registered tool names end in `_tool`. The 30 tools are defined in `main.py`.

## Architecture

Core package `code_review_graph/` (Python 3.10+):

- `main.py`: FastMCP server entry point. Registers 30 tools and 5 prompts.
- `tools/`: tool implementations by domain: `build.py`, `query.py`, `review.py`, `context.py`, `flows_tools.py`, `community_tools.py`, `refactor_tools.py`, `docs.py`, `registry_tools.py`, `analysis_tools.py`; shared helpers in `_common.py`.
- `prompts.py`: 5 MCP prompts (review_changes, architecture_map, debug_issue, onboard_developer, pre_merge_check).
- `cli.py`: the `code-review-graph` command. `daemon.py` and `daemon_cli.py`: the `crg-daemon` multi-repo watch daemon.
- `parser.py`: Tree-sitter multi-language parser with fallbacks for notebooks and other formats. `custom_languages.py`: languages defined in `.code-review-graph/languages.toml` (see docs/CUSTOM_LANGUAGES.md).
- `graph.py`: SQLite graph store (nodes, edges, impact analysis). `migrations.py`: schema migrations; the current schema version is 10 and must equal `SUPPORTED_SCHEMA_VERSION` in the VS Code extension (CI checks this).
- `incremental.py`: full build, Git/SVN change detection, incremental update, stale-file reconciliation, watch mode. `postprocessing.py`: shared post-build pipeline (signatures, flows, communities, FTS).
- Post-build resolvers: `python_resolver.py`, `jedi_resolver.py` (optional `enrichment` extra), `spring_resolver.py`, `event_resolver.py`, `temporal_resolver.py`, `config_keys.py`, `scoped_resolver.py` (PHP, Rust, C#), `rescript_resolver.py`, `hcl_resolver.py`, `tsconfig_resolver.py` (tsconfig and jsconfig path aliases).
- `flows.py`: execution flows and criticality. `communities.py`: Leiden via igraph (optional) or file-based grouping, plus the architecture overview. `analysis.py`: hub and bridge nodes, knowledge gaps, surprise scoring, suggested questions.
- `search.py`: FTS5 keyword search combined with optional vector search. `embeddings.py`: providers for local sentence-transformers, OpenAI-compatible endpoints, Google Gemini, MiniMax and Voyage AI.
- `changes.py`: risk-scored change analysis. `refactor.py`: rename preview, dead code, suggestions. `hints.py`: `next_tool_suggestions`. `uncertainty.py`: `confidence` notes on empty results. `context_savings.py`: estimated context-savings metadata.
- `visualization.py`: D3.js HTML graph (D3 is bundled in `assets/`). `exports.py`: JSON, GraphML, Neo4j Cypher, Obsidian, SVG. `wiki.py`: Markdown wiki. `graph_diff.py`: snapshot diffing. `memory.py`: stored Q&A feedback.
- `skills.py`: `install` (platform MCP configs, hooks, skills, instruction blocks). `_legacy_instructions.py`: instruction blocks shipped by earlier releases. `jsonc.py`: comment-preserving JSONC tokenising and splices, shared by install and uninstall so neither flattens a commented config. `uninstall.py`: reverses `install`. `enrich.py`: PreToolUse hook enrichment. `forget.py`: drops files from the graph. `registry.py`: multi-repo registry. `http_origin_guard.py`: Host/Origin checks for `serve --http`. `token_benchmark.py` and `eval/`: benchmarks. `constants.py`: shared constants.
- `docs/LLM-OPTIMIZED-REFERENCE.md`: the reference served by `get_docs_section_tool`.

VS Code extension: `code-review-graph-vscode/` (TypeScript, separate `package.json` and `tsconfig.json`). Reads `.code-review-graph/graph.db` directly.

Database: `.code-review-graph/graph.db` (SQLite, WAL mode).

## Key Commands

```bash
# Development
uv run pytest tests/ --tb=short -q
uv run ruff check code_review_graph/
uv run mypy code_review_graph/ --ignore-missing-imports --no-strict-optional

# Graph
uv run code-review-graph build              # full build
uv run code-review-graph update             # incremental update
uv run code-review-graph status             # graph statistics
uv run code-review-graph detect-changes     # risk-scored change analysis (read-only)
uv run code-review-graph forget PATH        # drop files from the graph
uv run code-review-graph serve              # MCP server (stdio; --http for localhost)
uv run code-review-graph wiki               # Markdown wiki
uv run code-review-graph register <path>    # add a repo to the multi-repo registry
uv run code-review-graph eval               # benchmarks
uv run code-review-graph --help             # full command list
```

## Code Conventions

- Line length 100 (ruff). Python 3.10+.
- SQL: parameterised queries with `?` placeholders. Never format values into SQL strings.
- Errors: catch specific exceptions and log with `logger.warning` or `logger.error`.
- Threads: `threading.Lock` around shared caches; SQLite opened with `check_same_thread=False`.
- Node names: pass through `_sanitize_name()` before returning them to MCP clients.
- File reads: read the bytes once, hash them, then parse the same bytes.

## Security Invariants

- No `eval()`, `exec()`, `pickle` or `yaml.unsafe_load()`.
- No `shell=True` in subprocess calls.
- `_validate_repo_root()` requires an existing directory containing `.git`, `.svn` or `.code-review-graph`, which blocks path traversal through `repo_root`.
- `_sanitize_name()` strips control characters and caps names at 256 characters.
- `escH()` in `visualization.py` escapes HTML entities including quotes and backticks; `</script>` is escaped inside embedded JSON.
- D3.js is bundled and loaded with an SRI hash; the CDN fallback carries the same hash.
- `http_origin_guard.py` validates Host and Origin for `serve --http`.
- API keys come from environment variables only.

## Test Structure

`tests/` holds over 100 pytest modules. The main groups:

- Core: `test_parser.py`, `test_graph.py`, `test_incremental.py`, `test_tools.py`, `test_main.py`, `test_cli*.py`.
- Features: `test_flows.py`, `test_communities.py`, `test_changes.py`, `test_refactor.py`, `test_search.py`, `test_hints.py`, `test_prompts.py`, `test_wiki.py`, `test_embeddings.py`, `test_eval.py`, `test_registry.py`, `test_migrations.py`, `test_uncertainty.py`, `test_context_savings.py`, `test_token_budget.py` (per-tool token budgets).
- Languages: `test_multilang.py`, `test_custom_languages.py`, `test_notebook.py`, plus per-language files such as `test_php_*.py`, `test_spring_*.py`, `test_kotlin_imports.py`, `test_go_embeddings.py`, `test_cpp_*.py`, `test_typescript_node_extensions.py`, `test_tsconfig_resolver.py`, `test_hcl_parser.py`, `test_dbt_parser.py`, `test_ansible_parser.py`.
- Regression modules are named after the behaviour they pin, not the PR that produced them. The originating PR number goes in the module docstring.
- Watch and daemon: `test_watch_*.py`, `test_daemon*.py`.
- Install and platforms: `test_skills.py`, `test_cli_install.py`, `test_uninstall.py`, `test_git_hook_worktree.py`, `test_hermes_install.py`, `test_qoder_bundled_skills.py`, `test_installer_ownership.py` (what install may rewrite and what belongs to the user), `test_released_shapes.py` (reads the hooks and MCP entries every released tag wrote and requires them to still be recognised; needs tags, skips without them), `test_platform_lifecycle.py` (opt-in marker `platform_lifecycle`).
- Windows: `test_windows_compat.py`, `test_windows_path_identity.py`.
- Docs and GitHub Action: `test_documentation.py`, `test_action_render.py`.
- Distribution gate: `test_packaging.py`, marked `packaging` and **skipped by default**. Builds a wheel and an sdist with `python -m build`, installs each into its own virtual environment, and drives the installed program with the checkout out of reach. Needs network and takes a couple of minutes. Run it before a release with `uv run --python 3.13 python -m pytest tests/test_packaging.py -m packaging`.
- `tests/fixtures/`: sample files per supported language.

## CI Pipeline

`.github/workflows/ci.yml`:

- lint: ruff on Python 3.10.
- type-check: mypy.
- security: bandit with the exemptions in `pyproject.toml`.
- schema-sync: `LATEST_VERSION` in `migrations.py` must equal `SUPPORTED_SCHEMA_VERSION` in the VS Code extension.
- test: pytest on Python 3.10, 3.11, 3.12 and 3.13; coverage must be at least 65%.
- google-embeddings: constructs the Google provider with the `google-embeddings` and `all` extras.
- windows-native: a subset of the suite on windows-latest.

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

## Branching Model

Three long-lived branches, one direction: feature PR → `staging` (default) → `testing` → `main` → tag → PyPI.
Open every PR against `staging`. Never push to or open PRs against `testing` or `main`; those only
receive promotion PRs, always merged with a merge commit.

`staging` → `testing` is automatic: `.github/workflows/auto-promote.yml` runs once a day and merges
the promotion PR when `staging` is ahead, every required check is green, and the promotion gate has
not failed on `testing`. The decision lives in `scripts/auto_promote.py`. It merges only a PR it
opened itself — same repository, correctly aimed, labelled `auto-promotion`, pinned to the commit
whose checks were read — so a promotion PR you open by hand is left alone. It needs Settings →
Actions → General → Workflow permissions → *Allow GitHub Actions to create and approve pull
requests*. **Promotion to `main` is never automatic** — the maintainer opens and merges that PR by
hand. Full rules in CONTRIBUTING.md "Branching and promotion".

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

**This project has a knowledge graph. Start with the code-review-graph
MCP tools to narrow scope, then read the source.** The graph is cheaper than scanning files and
gives you structural context (callers, dependents, test coverage) that file search cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes_tool` or `query_graph_tool` instead of Grep
- **Understanding impact**: `get_impact_radius_tool` instead of manually tracing imports
- **Code review**: `detect_changes_tool` + `get_review_context_tool` instead of reading entire files
- **Finding relationships**: `query_graph_tool` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview_tool` + `list_communities_tool`

### Verify in the source

- Narrow scope with the graph, then read the source. Do not change code from graph output alone.
- For any non-trivial change, read the implementation and the relevant tests before concluding.
- Verify the exact source when touching behavior, database logic, migrations, retries, fallbacks,
  recovery, or compatibility code.
- When the graph and the source disagree, the source wins. The graph may be stale or may not
  model that relationship.
- An empty graph result can mean "not indexed" or "not statically visible", not "does not exist".

### Key Tools

| Tool | Use when |
| ------ | ---------- |
| `detect_changes_tool` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context_tool` | Need source snippets for review — token-efficient |
| `get_impact_radius_tool` | Understanding blast radius of a change |
| `get_affected_flows_tool` | Finding which execution paths are impacted |
| `query_graph_tool` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes_tool` | Finding functions/classes by name or keyword |
| `get_architecture_overview_tool` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes_tool` for code review.
3. Use `get_affected_flows_tool` to understand impact.
4. Use `query_graph_tool` pattern="tests_for" to check coverage.
<!-- /code-review-graph MCP tools -->
