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

### `build_or_update_graph_tool`
```
full_rebuild: bool = False    # True for full re-parse
repo_root: str | None         # Auto-detected
base: str = "HEAD~1"          # Git diff base
```

### `get_impact_radius_tool`
```
changed_files: list[str] | None  # Auto-detected from git
max_depth: int = 2               # Hops in graph
max_results: int = 500           # Max impacted nodes to return
repo_root: str | None
base: str = "HEAD~1"
```
> BFS traversal is capped at `max_results` nodes. Response includes `truncated` (bool) and `total_impacted` (int).
> `repo_root` must point to a directory containing `.git` or `.code-review-graph`.

### `query_graph_tool`
```
pattern: str    # callers_of, callees_of, imports_of, importers_of,
                # children_of, tests_for, inheritors_of, file_summary
target: str     # Node name, qualified name, or file path
repo_root: str | None
```

### `get_review_context_tool`
```
changed_files: list[str] | None
max_depth: int = 2
include_source: bool = True
max_lines_per_file: int = 200
repo_root: str | None
base: str = "HEAD~1"
```

### `semantic_search_nodes_tool`
```
query: str           # Search string
kind: str | None     # File, Class, Function, Type, Test
limit: int = 20
repo_root: str | None
```

### `embed_graph_tool`
```
repo_root: str | None
```
Requires: `pip install code-review-graph[embeddings]`

### `list_graph_stats_tool`
```
repo_root: str | None
```

### `find_large_functions_tool`
```
min_lines: int = 50                # Minimum line count threshold
max_lines: int | None              # Optional maximum line count
kind: str | None                   # File, Class, Function, or Test
file_path_pattern: str | None      # Filter by file path substring
limit: int = 50                    # Max results to return
repo_root: str | None
```
> Find functions, classes, or files exceeding a line-count threshold. Results ordered by size (largest first).

### `get_docs_section_tool`
```
section_name: str    # usage, review-delta, review-pr, commands, legal, watch, embeddings, languages, troubleshooting
```

## CLI Commands

```bash
# Register MCP server with Claude Code
code-review-graph install           # also available as: code-review-graph init
code-review-graph install --dry-run # preview without writing files

# Full build
code-review-graph build

# Incremental update
code-review-graph update
code-review-graph update --base origin/main  # custom base ref

# Check status
code-review-graph status

# Watch mode
code-review-graph watch

# Generate graph visualisation
code-review-graph visualize

# Start MCP server
code-review-graph serve
```

## API Response Schemas

### `build_or_update_graph_tool`
```json
{
  "files_parsed": 150,       // (full build) or "files_updated": 12 (incremental)
  "total_nodes": 420,
  "total_edges": 380,
  "changed_files": ["src/auth.py"],      // incremental only
  "dependent_files": ["src/routes.py"],   // incremental only
  "errors": [{"file": "bad.py", "error": "SyntaxError"}]
}
```

### `get_impact_radius_tool`
```json
{
  "changed_nodes": [
    {"id": 1, "kind": "Function", "name": "login", "qualified_name": "src/auth.py::login", "file_path": "src/auth.py", "line_start": 10, "line_end": 25, "language": "python", "is_test": false}
  ],
  "impacted_nodes": [ /* same shape */ ],
  "impacted_files": ["src/routes.py", "src/middleware.py"],
  "edges": [
    {"id": 5, "kind": "CALLS", "source": "src/auth.py::login", "target": "src/db.py::get_user", "file_path": "src/auth.py", "line": 15}
  ],
  "truncated": false,
  "total_impacted": 3
}
```

### `query_graph_tool`
```json
{
  "results": [
    {"id": 1, "kind": "Function", "name": "login", "qualified_name": "...", "file_path": "...", "line_start": 10, "line_end": 25}
  ]
}
```

### `get_review_context_tool`
```json
{
  "impact": { /* same as get_impact_radius_tool */ },
  "source_snippets": {
    "src/auth.py": "def login(...):\n    ..."
  },
  "review_guidance": "Focus on: login() changed parameters, check callers in routes.py"
}
```

### `semantic_search_nodes_tool`
```json
{
  "results": [
    {"id": 1, "kind": "Function", "name": "authenticate", "qualified_name": "...", "file_path": "...", "similarity_score": 0.8732}
  ]
}
```

### `list_graph_stats_tool`
```json
{
  "total_nodes": 420,
  "total_edges": 380,
  "nodes_by_kind": {"File": 50, "Function": 280, "Class": 60, "Type": 15, "Test": 15},
  "edges_by_kind": {"CALLS": 200, "CONTAINS": 100, "IMPORTS_FROM": 50, "INHERITS": 20, "TESTED_BY": 10},
  "languages": ["python", "typescript", "go"],
  "files_count": 50,
  "last_updated": "2026-02-27T14:30:00"
}
```
