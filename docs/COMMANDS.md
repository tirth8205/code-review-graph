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

## MCP Tools (22 total)

### Core Tools

#### `build_or_update_graph_tool`
```
full_rebuild: bool = False    # True for full re-parse
repo_root: str | None         # Auto-detected
base: str = "HEAD~1"          # Git diff base
```

#### `get_impact_radius_tool`
```
changed_files: list[str] | None  # Auto-detected from git
max_depth: int = 2               # Hops in graph
repo_root: str | None
base: str = "HEAD~1"
```

#### `query_graph_tool`
```
pattern: str    # callers_of, callees_of, imports_of, importers_of,
                # children_of, tests_for, inheritors_of, file_summary
target: str     # Node name, qualified name, or file path
repo_root: str | None
```

#### `get_review_context_tool`
```
changed_files: list[str] | None
max_depth: int = 2
include_source: bool = True
max_lines_per_file: int = 200
repo_root: str | None
base: str = "HEAD~1"
```

#### `semantic_search_nodes_tool`
```
query: str           # Search string
kind: str | None     # File, Class, Function, Type, Test
limit: int = 20
repo_root: str | None
model: str | None    # Embedding model (falls back to CRG_EMBEDDING_MODEL env var)
```

#### `embed_graph_tool`
```
repo_root: str | None
model: str | None    # Embedding model name
```
Requires: `pip install code-review-graph[embeddings]`

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
changed_files: list[str] | None  # Auto-detected from git
base: str = "HEAD~1"
repo_root: str | None
```

### Community Tools

#### `list_communities_tool`
```
sort_by: str = "size"    # size, cohesion, name
min_size: int = 0
repo_root: str | None
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
```

### Change Analysis and Refactoring Tools

#### `detect_changes_tool`
```
base: str = "HEAD~1"
changed_files: list[str] | None
include_source: bool = False
max_depth: int = 2
repo_root: str | None
```
Primary tool for code review. Maps git diffs to affected functions, flows, communities, and test coverage gaps. Returns risk scores and prioritized review items.

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
code-review-graph install           # Register MCP server with Claude Code (alias: init)
code-review-graph install --dry-run # Preview without writing files

# Build and update
code-review-graph build                        # Full build
code-review-graph update                       # Incremental update
code-review-graph update --base origin/main    # Custom base ref

# Monitor and inspect
code-review-graph status                       # Graph statistics
code-review-graph watch                        # Auto-update on file changes
code-review-graph visualize                    # Generate interactive HTML graph

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
