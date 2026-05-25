# All Available Commands

## Skills (Claude Code slash commands)

### `/code-review-graph:build-graph`
Build or update the knowledge graph.
- First time: performs a full build
- Subsequent: incremental update (only changed files)

### `/code-review-graph:review-delta`
Review only changes since last commit.
- Auto-detects changed files via git diff
- Computes blast radius (2-hop default)
- Generates structured review with guidance

### `/code-review-graph:review-pr`
Review a PR or branch diff.
- Uses main/master as base
- Full impact analysis across all PR commits
- Structured output with risk assessment

## MCP Tools

### Core Tools

#### `build_or_update_graph_tool`
```
full_rebuild: bool = False           # True for full re-parse
repo_root: str | None                # Auto-detected
base: str = "HEAD~1"                 # VCS diff base for incremental updates
postprocess: str = "full"            # "full", "minimal", or "none"
recurse_submodules: bool | None      # Falls back to CRG_RECURSE_SUBMODULES
```

#### `run_postprocess_tool`
```
flows: bool = True
communities: bool = True
fts: bool = True
repo_root: str | None
```

#### `get_minimal_context_tool`
```
task: str = ""                       # What you are doing
changed_files: list[str] | None      # Auto-detected from VCS when omitted
repo_root: str | None
base: str = "HEAD~1"
```

#### `get_impact_radius_tool`
```
changed_files: list[str] | None  # Auto-detected from VCS
max_depth: int = 2               # Hops in graph
repo_root: str | None
base: str = "HEAD~1"
detail_level: str = "standard"   # "standard" or "minimal"
```
Relevant responses may include compact estimated `context_savings` metadata.

#### `query_graph_tool`
```
pattern: str    # callers_of, callees_of, imports_of, importers_of,
                # children_of, tests_for, inheritors_of, file_summary
target: str     # Node name, qualified name, or file path
repo_root: str | None
detail_level: str = "standard"   # "standard" or "minimal"
```

#### `get_review_context_tool`
```
changed_files: list[str] | None
max_depth: int = 2
include_source: bool = True
max_lines_per_file: int = 200
repo_root: str | None
base: str = "HEAD~1"
detail_level: str = "standard"   # "standard" or "minimal"
```
Relevant responses may include compact estimated `context_savings` metadata.

#### `traverse_graph_tool`
```
query: str
depth: int = 3                  # 1-6
mode: str = "bfs"               # "bfs" or "dfs"
token_budget: int = 2000
repo_root: str | None
```

#### `semantic_search_nodes_tool`
```
query: str           # Search string
kind: str | None     # File, Class, Function, Type, Test
limit: int = 20
repo_root: str | None
model: str | None    # Embedding model (falls back to CRG_EMBEDDING_MODEL env var)
provider: str | None # local, openai, google, minimax
detail_level: str = "standard"
```

#### `embed_graph_tool`
```
repo_root: str | None
model: str | None    # Embedding model name
provider: str | None # local, openai, google, minimax
```
Local embeddings require: `pip install code-review-graph[embeddings]`. Cloud providers use stdlib HTTP clients and require their provider environment variables.

#### `list_graph_stats_tool`
```
repo_root: str | None
```

#### `find_large_functions_tool`
```
min_lines: int = 50                # Minimum line count threshold
kind: str | None                   # File, Class, Function, or Test
file_path_pattern: str | None      # Filter by file path substring
limit: int = 50                    # Max results to return
repo_root: str | None
```

#### `get_docs_section_tool`
```
section_name: str    # usage, review-delta, review-pr, commands, legal, watch, embeddings, languages, troubleshooting
```

### Flow Tools

#### `list_flows_tool`
```
sort_by: str = "criticality"  # criticality, depth, node_count, file_count, name
limit: int = 50
kind: str | None              # Filter by entry point kind (e.g. "Test", "Function")
repo_root: str | None
detail_level: str = "standard"
```

#### `get_flow_tool`
```
flow_id: int | None          # Database ID from list_flows_tool
flow_name: str | None        # Name to search (partial match)
include_source: bool = False # Include source snippets for each step
repo_root: str | None
```

#### `get_affected_flows_tool`
```
changed_files: list[str] | None  # Auto-detected from VCS
base: str = "HEAD~1"
repo_root: str | None
```

### Community Tools

#### `list_communities_tool`
```
sort_by: str = "size"    # size, cohesion, name
min_size: int = 0
repo_root: str | None
detail_level: str = "standard"
```

#### `get_community_tool`
```
community_name: str | None   # Name to search (partial match)
community_id: int | None     # Database ID
include_members: bool = False
repo_root: str | None
```

#### `get_architecture_overview_tool`
```
repo_root: str | None
detail_level: str = "minimal"    # "minimal" compact default, "standard" full detail
```
Minimal responses may include compact estimated `context_savings` metadata.

### Graph Health and Architecture Tools

#### `get_hub_nodes_tool`
```
top_n: int = 10
repo_root: str | None
```

#### `get_bridge_nodes_tool`
```
top_n: int = 10
repo_root: str | None
```

#### `get_knowledge_gaps_tool`
```
repo_root: str | None
```

#### `get_surprising_connections_tool`
```
top_n: int = 15
repo_root: str | None
```

#### `get_suggested_questions_tool`
```
repo_root: str | None
```

### Change Analysis and Refactoring Tools

#### `detect_changes_tool`
```
base: str = "HEAD~1"
changed_files: list[str] | None
include_source: bool = False
max_depth: int = 2
repo_root: str | None
detail_level: str = "standard"
```
Primary tool for code review. Maps changed files to affected functions, flows, communities, and test coverage gaps. Returns risk scores and prioritized review items.
Relevant responses may include compact estimated `context_savings` metadata.

#### `refactor_tool`
```
mode: str = "rename"         # "rename", "dead_code", or "suggest"
old_name: str | None         # (rename) Current symbol name
new_name: str | None         # (rename) New name
kind: str | None             # (dead_code) Function or Class
file_pattern: str | None     # (dead_code) Filter by file path substring
repo_root: str | None
```

#### `apply_refactor_tool`
```
refactor_id: str             # ID from prior refactor_tool call
repo_root: str | None
dry_run: bool = False        # Return diff without writing files
```

### Wiki Tools

#### `generate_wiki_tool`
```
repo_root: str | None
force: bool = False          # Regenerate all pages even if unchanged
```

#### `get_wiki_page_tool`
```
community_name: str          # Community name to look up
repo_root: str | None
```

### Multi-Repo Tools

#### `list_repos_tool`
```
(no parameters)
```

#### `cross_repo_search_tool`
```
query: str
kind: str | None
limit: int = 20
```

## MCP Prompts (5 workflow templates)

### `review_changes`
Pre-commit review workflow using detect_changes, affected_flows, and test gaps.
```
base: str = "HEAD~1"
```

### `architecture_map`
Architecture documentation using communities, flows, and Mermaid diagrams.

### `debug_issue`
Guided debugging using search, flow tracing, and recent changes.
```
description: str = ""
```

### `onboard_developer`
New developer orientation using stats, architecture, and critical flows.

### `pre_merge_check`
PR readiness check with risk scoring, test gaps, and dead code detection.
```
base: str = "HEAD~1"
```

## CLI Commands

```bash
# Setup
code-review-graph install           # Configure detected AI coding platforms (alias: init)
code-review-graph install --dry-run # Preview without writing files
code-review-graph install --platform codex  # Configure one platform

# Build and update
code-review-graph build                        # Full build
code-review-graph build --skip-flows           # Parse + signatures + FTS only
code-review-graph build --skip-postprocess     # Raw parse only
code-review-graph update                       # Incremental update
code-review-graph update --base origin/main    # Custom base ref
code-review-graph postprocess                  # Re-run flows, communities, FTS

# Monitor and inspect
code-review-graph status                       # Graph statistics
code-review-graph watch                        # Auto-update on file changes
code-review-graph visualize                    # Generate interactive HTML graph
code-review-graph visualize --format graphml   # Export GraphML
code-review-graph visualize --serve            # Serve graph.html on localhost:8765

# Analysis
code-review-graph detect-changes               # Risk-scored change analysis
code-review-graph detect-changes --base HEAD~3 # Custom base ref
code-review-graph detect-changes --brief       # Compact output

# Wiki
code-review-graph wiki                         # Generate markdown wiki from communities

# Multi-repo
code-review-graph register <path> [--alias name]  # Register a repository
code-review-graph unregister <path_or_alias>       # Remove from registry
code-review-graph repos                            # List registered repositories

# Daemon (multi-repo watcher) — included with install, no extra dependencies
code-review-graph daemon start [--foreground]       # Start the watch daemon
code-review-graph daemon stop                       # Stop the daemon
code-review-graph daemon restart [--foreground]     # Restart the daemon
code-review-graph daemon status                     # Show daemon status and repos
code-review-graph daemon logs [--repo ALIAS] [-f]   # View daemon or per-repo logs
code-review-graph daemon add <path> [--alias NAME]  # Add a repo to daemon config
code-review-graph daemon remove <path_or_alias>     # Remove a repo from daemon config

# Evaluation
code-review-graph eval                         # Run evaluation benchmarks

# Server
code-review-graph serve                        # Start MCP server (stdio)
code-review-graph serve --http                 # Streamable HTTP on localhost:5555
code-review-graph serve --tools query_graph_tool,detect_changes_tool  # Tool allowlist
code-review-graph mcp                          # Alias for serve
```

## Standalone Daemon CLI (`crg-daemon`)

The `crg-daemon` command is included with every `code-review-graph` installation — no
separate install required. It is also available as a standalone entry point. It mirrors the
`code-review-graph daemon` subcommands:

```bash
crg-daemon start [--foreground]       # Start the multi-repo watch daemon
crg-daemon stop                       # Stop the daemon and all watcher processes
crg-daemon restart [--foreground]     # Restart (stop + start)
crg-daemon status                     # Show daemon status, repos, and process liveness
crg-daemon logs [--repo ALIAS] [-f] [-n N]  # Tail daemon or per-repo log files
crg-daemon add <path> [--alias NAME]  # Add a repository to watch.toml
crg-daemon remove <path_or_alias>     # Remove a repository from watch.toml
```

### Configuration

The daemon reads its configuration from `~/.code-review-graph/watch.toml`:

```toml
session_name = "crg-watch"   # logical daemon name
log_dir = "~/.code-review-graph/logs"
poll_interval = 2            # seconds between config file polls

[[repos]]
path = "/home/user/project-a"
alias = "project-a"

[[repos]]
path = "/home/user/project-b"
alias = "project-b"
```

The daemon spawns one `code-review-graph watch` child process per repo,
managed via `subprocess.Popen`. It monitors the config file for changes and
automatically reconciles child processes (starting/stopping as repos are
added or removed). Health checks run every 30 seconds and automatically
restart dead watchers. No external dependencies (tmux, screen, etc.) are
required.
