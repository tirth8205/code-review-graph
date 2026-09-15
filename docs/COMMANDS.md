# All Available Commands

## Skills and Slash Commands

These commands are installed for clients that support project skills or slash commands.

### `/code-review-graph:build-graph`
Build or update the knowledge graph.
- First run: full build
- Later runs: incremental update (changed files only)

### `/code-review-graph:review-delta`
Review changes since the last commit.
- Changed files come from `git diff`
- Blast radius is changed nodes plus 2-hop neighbours
- Output is a structured review with guidance

### `/code-review-graph:review-pr`
Review a PR or branch diff.
- Uses `main` (or `master`) as the base
- Covers every commit in the PR
- Output is a structured review with a risk assessment

`install` also writes four workflow skills to `.claude/skills/`: `explore-codebase`,
`review-changes`, `debug-issue` and `refactor-safely`.

## MCP Tools

Every `repo_root` parameter is optional and auto-detected from the current
directory when omitted.

Where a tool takes `base`, a local or remote branch ref (for example
`origin/main`) resolves to its merge base with `HEAD`, so the diff covers only
this branch's own commits. Commit ids and revision expressions such as `HEAD~3`
are used as given. If Git cannot find the merge base (shallow clone), the ref
is used as given.

### Core Tools

#### `build_or_update_graph_tool`
```
full_rebuild: bool = False           # True re-parses every file
repo_root: str | None
base: str | None = None              # Diff base; None means the commit the graph was last built at
postprocess: str = "full"            # "full", "minimal" (signatures + FTS), or "none"
recurse_submodules: bool | None      # None falls back to CRG_RECURSE_SUBMODULES
embedding_provider: str | None       # Refresh an existing embedding index; needs embedding_model
embedding_model: str | None          # Exact model for embedding_provider
```
If some files fail to parse, `status` is `"partial"` and the `summary` names
them. Their previous graph rows are kept.

#### `run_postprocess_tool`
```
flows: bool = True
communities: bool = True
fts: bool = True
repo_root: str | None
embedding_provider: str | None       # Refresh an existing embedding index; needs embedding_model
embedding_model: str | None
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
Responses may include estimated `context_savings` metadata.

#### `query_graph_tool`
```
pattern: str    # callers_of, references_to, callees_of, imports_of, importers_of,
                # children_of, tests_for, inheritors_of, file_summary
target: str     # Node name, qualified name, or file path
repo_root: str | None
detail_level: str = "standard"   # "standard" or "minimal"
max_results: int = 100           # Minimal mode also caps visible results at 5
```

#### `get_review_context_tool`
```
changed_files: list[str] | None
max_depth: int = 2
include_source: bool = True
max_lines_per_file: int = 200    # Capped at 500
repo_root: str | None
base: str = "HEAD~1"
detail_level: str = "standard"   # "standard" or "minimal"
max_results: int = 100           # Graph nodes per list (max 100) and edges (max 150)
max_files: int = 25              # Files listed and given snippets (max 200)
```
Snippets share an 800-line budget across the response. Each list reports its
untruncated `*_total`, and `context.truncated` marks any cut.
Responses may include estimated `context_savings` metadata.

#### `traverse_graph_tool`
```
query: str
depth: int = 3                  # Clamped to 1-6
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
model: str | None    # Embedding model (falls back to provider-specific env vars)
provider: str | None # local, openai, google, minimax, voyage
detail_level: str = "standard"
```

#### `embed_graph_tool`
```
repo_root: str | None
model: str | None    # Embedding model name
provider: str | None # local, openai, google, minimax, voyage
```
Local embeddings need `pip install "code-review-graph[embeddings]"`. Cloud
providers use the standard library HTTP client and read their keys from
environment variables (see the README).

#### `list_graph_stats_tool`
```
repo_root: str | None
```

#### `find_large_functions_tool`
```
min_lines: int = 50                # Minimum line count
kind: str | None                   # File, Class, Function, or Test
file_path_pattern: str | None      # File path substring
limit: int = 50
repo_root: str | None
```

#### `get_docs_section_tool`
```
section_name: str    # usage, review-delta, review-pr, commands, legal, watch, embeddings, languages, troubleshooting
repo_root: str | None
```

### Flow Tools

#### `list_flows_tool`
```
sort_by: str = "criticality"  # criticality, depth, node_count, file_count, name
limit: int = 50               # Flows returned (max 200)
kind: str | None              # Entry point kind (e.g. "Test", "Function")
repo_root: str | None
detail_level: str = "standard"
```

#### `get_flow_tool`
```
flow_id: int | None          # Database ID from list_flows_tool
flow_name: str | None        # Name to search (partial match); ignored when flow_id is given
include_source: bool = False # Source snippet per step
repo_root: str | None
max_steps: int = 50          # Capped at 200; flow.total_steps reports the full count
max_source_lines: int = 400  # Shared across all steps; capped at 2000
```

#### `get_affected_flows_tool`
```
changed_files: list[str] | None  # Auto-detected from VCS
base: str = "HEAD~1"
repo_root: str | None
detail_level: str = "standard"   # "standard" full step details, "minimal" metadata only
max_flows: int = 50
```
Standard mode carries a full `steps` list per flow, so it caps visible flows
at 25 and spends a shared 400-step budget across them; minimal mode caps at
500. `total` reports the untruncated flow count. See #849.

### Community Tools

#### `list_communities_tool`
```
sort_by: str = "size"    # size, cohesion, name
min_size: int = 0
repo_root: str | None
detail_level: str = "standard"
max_results: int = 50    # Communities returned (max 200)
max_members: int = 10    # Member names per community in standard mode (max 25)
```
`size` reports the true member count; `members_truncated` marks a cut member list.

#### `get_community_tool`
```
community_name: str | None   # Name to search (partial match); ignored when community_id is given
community_id: int | None     # Database ID
include_members: bool = False
repo_root: str | None
max_members: int = 25        # Member entries returned (max 25)
```

#### `get_architecture_overview_tool`
```
repo_root: str | None
detail_level: str = "minimal"    # "minimal" (default) or "standard"
max_results: int = 100           # Cross-community rows and warnings (max 200)
max_members: int = 10            # Member names per community in standard mode (max 25)
```
`cross_community_edges_total` reports the untruncated row count.
Minimal responses may include estimated `context_savings` metadata.

### Graph Health and Architecture Tools

#### `get_hub_nodes_tool`
```
top_n: int = 10                  # Capped at 100
repo_root: str | None
detail_level: str = "standard"   # "minimal" returns name, kind, total_degree
```

#### `get_bridge_nodes_tool`
```
top_n: int = 10                  # Capped at 100
repo_root: str | None
detail_level: str = "standard"   # "minimal" returns name, kind, betweenness
```

#### `get_knowledge_gaps_tool`
```
repo_root: str | None
max_per_category: int = 15       # Entries per gap category (max 50)
detail_level: str = "standard"   # "minimal" drops file paths
```
`summary` maps each category to its untruncated count.

#### `get_surprising_connections_tool`
```
top_n: int = 15                  # Capped at 100
repo_root: str | None
detail_level: str = "standard"   # "minimal" returns source, target, kind, score
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
include_source: bool = False # Snippets share a 600-line budget
max_depth: int = 2
repo_root: str | None
detail_level: str = "standard"
max_results: int = 25        # Changed functions, test gaps, changed files (max 100)
max_flows: int = 20          # Affected flows embedded (max 200)
```
Main tool for code review. Maps changed files to affected functions, flows,
communities and test coverage gaps, and returns risk scores and review
priorities. Embedded flows carry per-flow metadata only; use
`get_affected_flows_tool` for step detail. `changed_functions_total`,
`test_gaps_total` and `affected_flows_total` report the untruncated counts.
Responses may include estimated `context_savings` metadata.

#### `refactor_tool`
```
mode: str = "rename"         # "rename", "dead_code", or "suggest"
old_name: str | None         # (rename) Current symbol name
new_name: str | None         # (rename) New name
kind: str | None             # (dead_code) Function or Class
file_pattern: str | None     # (dead_code) File path substring
repo_root: str | None
max_results: int = 50        # Edits/symbols/suggestions returned (max 150)
detail_level: str = "standard"  # "minimal" keeps identifying fields only
```
Truncating a rename preview truncates only the response. The stored preview
keeps every edit, so `apply_refactor_tool` applies the full set.

#### `apply_refactor_tool`
```
refactor_id: str             # ID from a prior refactor_tool call
repo_root: str | None
dry_run: bool = False        # Return a diff without writing files
max_diff_files: int = 25     # Per-file diffs in a dry run (max 150)
```

### Wiki Tools

#### `generate_wiki_tool`
```
repo_root: str | None
force: bool = False          # Regenerate all pages even if unchanged
```

#### `get_wiki_page_tool`
```
community_name: str
repo_root: str | None
max_chars: int = 20000       # Page content returned (max 80000)
```
`total_chars` reports the real page length; `truncated` marks a cut.

### Multi-Repo Tools

#### `list_repos_tool`
```
(no parameters)
```

#### `cross_repo_search_tool`
```
query: str
kind: str | None
limit: int = 20              # Results per repo
max_results: int = 50        # Merged results across searched repos (max 100)
repos: list[str] | None      # Aliases or folder names; default: every registered repo
```

`repos` limits the search to named registry entries. Matching is exact and
case-sensitive; paths are not accepted. An alias match wins over a folder-name
match. Results merge by each repo's local rank, with registry order as the
tie-breaker.

Names that match no entry are returned in `unknown`. A name that matches
several entries selects all of them and is listed in `ambiguous`. Both lists
are capped at 20 entries; `unknown_total` / `ambiguous_total` and
`unknown_truncated` / `ambiguous_truncated` report the real counts, and the
summary reports the counts rather than echoing the names.

## Result Bounds

Every tool that returns a list is bounded, so one MCP response cannot fill a
context window (see #849). The contract is the same everywhere:

- Defaults are small. Pass the tool's cap parameter to widen up to its hard
  ceiling, or a smaller value to narrow.
- Truncation is reported: the response carries the untruncated count
  (`total`, or a `*_total` field per list), sets `truncated: true`, and the
  summary says how many of how many are shown.
- Cap parameters reject values below 1 and reject booleans.
- Hard ceilings are enforced in code and pinned by `tests/test_token_budget.py`.

## MCP Prompts (5 workflow templates)

### `review_changes`
Pre-commit review using detect_changes, affected_flows and test gaps.
```
base: str = "HEAD~1"
```

### `architecture_map`
Architecture documentation using communities, flows and Mermaid diagrams.

### `debug_issue`
Guided debugging using search, flow tracing and recent changes.
```
description: str = ""
```

### `onboard_developer`
New developer orientation using stats, architecture and critical flows.

### `pre_merge_check`
PR readiness check with risk scoring, test gaps and dead code detection.
```
base: str = "HEAD~1"
```

## CLI Commands

Run `code-review-graph <command> --help` for the full option list.

```bash
# Setup
code-review-graph install           # Configure detected AI coding platforms (alias: init)
code-review-graph install --dry-run # Preview without writing files
code-review-graph install --platform codex  # Configure one platform
code-review-graph uninstall                 # Remove CRG configs, hooks, skills, and data
code-review-graph uninstall --platform codex  # Unbind one platform (keeps graph data and others)

# Build and update
code-review-graph build                        # Full build
code-review-graph build --skip-flows           # Parse + signatures + FTS only
code-review-graph build --skip-postprocess     # Raw parse only
code-review-graph update                       # Incremental update from the last-built commit
code-review-graph update --base origin/main    # Custom base ref
code-review-graph update --brief               # Update graph, then show the risk panel
code-review-graph update --brief --verify      # ...and cross-check against tiktoken
code-review-graph postprocess                  # Re-run flows, communities, FTS
code-review-graph forget PATH [PATH ...]       # Drop parsed files from the graph (no full rebuild)
code-review-graph forget src/legacy --dry-run  # Preview which files would be forgotten
code-review-graph embed --provider local       # Compute vector embeddings for semantic search
code-review-graph update --embedding-provider local --embedding-model all-MiniLM-L6-v2
                                                # Refresh an existing embedding index (default: off)

# Monitor and inspect
code-review-graph status                       # Graph statistics (no graph: exit 1, no DB created)
code-review-graph status --json                # One JSON object
code-review-graph watch                        # Auto-update on file changes (needs an existing graph)
code-review-graph visualize                    # Interactive HTML graph (needs an existing graph)
code-review-graph visualize --format graphml   # Formats: html, json, graphml, cypher, obsidian, svg
code-review-graph visualize --serve            # Serve graph.html on localhost:8765

# Analysis
code-review-graph detect-changes               # Risk-scored change analysis (read-only)
code-review-graph detect-changes --base HEAD~3 # Custom base revision
code-review-graph detect-changes --base origin/main # Branch refs use their merge base with HEAD
code-review-graph detect-changes --brief       # Compact panel with token-savings estimate
code-review-graph detect-changes --brief --verify  # ...and cross-check against tiktoken
code-review-graph detect-changes --churn       # Add opt-in change-frequency risk (CRG_CHURN_WINDOW_DAYS, default 90)
code-review-graph dead-code                    # Functions/classes with no callers or test references
code-review-graph dead-code --kind Function --file-pattern src/ --json

# Read-only graph queries (CLI mirrors of the MCP tools)
code-review-graph query callers_of <target>    # Patterns: callers_of, callees_of, imports_of, importers_of,
                                                #   children_of, tests_for, inheritors_of, file_summary
code-review-graph impact [--files F ...] [--depth N] [--base REF]
code-review-graph search <query> [--kind Function] [--limit N]
code-review-graph flows [--sort criticality] [--limit N] [--kind KIND]
code-review-graph flow --id ID | --name NAME [--source]
code-review-graph communities [--sort size] [--min-size N]
code-review-graph community --id ID | --name NAME [--members]
code-review-graph architecture [--detail-level minimal|standard]
code-review-graph large-functions [--min-lines N] [--kind Function] [--path SUBSTR] [--limit N]
code-review-graph refactor rename --old-name A --new-name B
code-review-graph refactor dead_code [--kind Function] [--path SUBSTR]
code-review-graph refactor suggest

# Wiki
code-review-graph wiki                         # Markdown wiki from communities (needs an existing graph)

# Multi-repo
code-review-graph register <path> [--alias name]  # Register a repository
code-review-graph unregister <path_or_alias>       # Remove from registry
code-review-graph repos                            # List registered repositories

# Daemon (multi-repo watcher), installed with the package
code-review-graph daemon start [--foreground]       # Start the watch daemon
code-review-graph daemon stop                       # Stop the daemon
code-review-graph daemon restart [--foreground]     # Restart the daemon
code-review-graph daemon status                     # Daemon status and repos
code-review-graph daemon logs [--repo ALIAS] [--follow] [--lines N]  # Daemon or per-repo logs (default 50 lines)
code-review-graph daemon add <path> [--alias NAME]  # Add a repo to the daemon config
code-review-graph daemon remove <path_or_alias>     # Remove a repo from the daemon config

# Evaluation
code-review-graph eval                         # Run evaluation benchmarks

# Server
code-review-graph serve                        # Start MCP server (stdio)
code-review-graph serve --http                 # Streamable HTTP on 127.0.0.1:5555 (--host, --port)
code-review-graph serve --tools query_graph_tool,detect_changes_tool  # Tool allowlist (or CRG_TOOLS)
code-review-graph mcp                          # Alias for serve; accepts only --repo and --auto-watch
```

Notes:

- `update` and `detect-changes` need a Git repository. `update` with no `--base`
  diffs from the commit the graph was last built at; when that commit is
  missing (fresh graph, rewritten history, shallow clone) it falls back to a
  full rebuild.
- `update` prints a `Warning:` line on stderr naming files that failed to
  parse. Those files keep their previous graph rows.
- `detect-changes --brief` is read-only against the existing graph and takes
  about a second. `update --brief` re-parses changed files first, then prints
  the same panel. Use `update --brief` after a rebase or a large change set,
  or when the graph may be stale.
- `status`, `detect-changes`, `visualize`, `wiki`, `watch`, `forget` and
  `dead-code` exit 1 when no graph exists and do not create one. `forget` and
  `dead-code` still move a legacy top-level `.code-review-graph.db` into
  `.code-review-graph/graph.db` before running.
- `install` appends a Git `pre-commit` hook that prints a risk summary before
  each commit. The hook skips linked worktrees unless `CRG_HOOK_WORKTREES=1`
  is set, so a worktree does not build a second graph for another branch.
- For an empty or incomplete graph, run `code-review-graph build`. See
  docs/TROUBLESHOOTING.md, "Empty or incomplete graph".

## Standalone Daemon CLI (`crg-daemon`)

`crg-daemon` is installed with `code-review-graph` and mirrors the
`code-review-graph daemon` subcommands:

```bash
crg-daemon start [--foreground]       # Start the multi-repo watch daemon
crg-daemon stop                       # Stop the daemon and all watcher processes
crg-daemon restart [--foreground]     # Restart (stop + start)
crg-daemon status                     # Daemon status, repos, and process liveness
crg-daemon logs [--repo ALIAS] [-f] [-n N]  # Tail daemon or per-repo log files
crg-daemon add <path> [--alias NAME]  # Add a repository to watch.toml
crg-daemon remove <path_or_alias>     # Remove a repository from watch.toml
```

### Configuration

The daemon reads `~/.code-review-graph/watch.toml` (or `$CRG_HOME/watch.toml`):

```toml
[daemon]
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

The daemon spawns one `code-review-graph watch` child process per repo with
`subprocess.Popen`. It polls the config file and starts or stops children as
repos are added or removed. A health check every 30 seconds restarts dead
watchers. No tmux or screen is needed.
