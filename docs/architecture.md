# Architecture

## System Overview

`code-review-graph` is a local-first code intelligence graph exposed through a CLI and MCP server. It maintains a persistent, incrementally updated knowledge graph of a codebase so AI coding tools can review changes with structural context instead of reading broad file dumps. Claude Code is supported, but it is one client among several.

## Component Diagram

```
┌──────────────────────────────────────────────────────────────┐
│                    AI coding clients / CLI                    │
│                                                              │
│  MCP clients              Hooks / watch mode                 │
│  ├── Codex                └── incremental update             │
│  ├── Claude Code, CodeBuddy Code                             │
│  ├── Cursor, Windsurf, Zed, Continue                         │
│  └── Gemini CLI, Qwen, Qoder, Copilot, OpenCode              │
│          │                        │                          │
│          ▼                        ▼                          │
│  ┌────────────────────────────────────────────┐              │
│  │      MCP Server (stdio or localhost HTTP)  │              │
│  │                                            │              │
│  │  30 MCP Tools + 5 MCP Prompts              │              │
│  │  ├── Core: build, impact, query, review,   │              │
│  │  │   search, traverse, embed, stats, docs  │              │
│  │  ├── Flows: list, get, affected            │              │
│  │  ├── Communities: list, get, architecture  │              │
│  │  ├── Analysis: detect_changes, refactor,   │              │
│  │  │   apply_refactor, hotspots, gaps        │              │
│  │  ├── Wiki: generate, get_page              │              │
│  │  └── Multi-repo: list_repos, cross_search  │              │
│  └────────────────┬───────────────────────────┘              │
└───────────────────┼──────────────────────────────────────────┘
                    │
        ┌───────────┼───────────────┐
        ▼           ▼               ▼
   ┌─────────┐ ┌─────────┐  ┌─────────────┐
   │ Parser  │ │  Graph  │  │ Incremental │
   │         │ │  Store  │  │   Engine    │
   └────┬────┘ └────┬────┘  └──────┬──────┘
        │           │              │
        ▼           ▼              ▼
   Tree-sitter   SQLite DB      git/svn diff
   grammars      (.code-review- subprocess
                 graph/
                 graph.db)
```

## Data Flow

### Full Build
1. `collect_all_files()` gathers tracked files (`git ls-files`) and applies `.code-review-graphignore` (gitignored files are skipped automatically when git is available)
2. For each file, `CodeParser.parse_file()` uses Tree-sitter to extract AST
3. AST walker identifies structural nodes (classes, functions, imports) and edges (calls, inheritance)
4. `GraphStore.store_file_nodes_edges()` persists to SQLite with file hash for change detection
5. Metadata updated with timestamp

### Incremental Update
1. `get_changed_files()` uses VCS metadata to identify changed files (git diff by default, with SVN support in the incremental layer)
2. `find_dependents()` queries the graph for files importing the changed files
3. Changed + dependent files are re-parsed (others skipped via hash comparison)
4. Only affected rows in SQLite are updated

### Review Context Generation
1. Changed files identified (git diff or explicit list)
2. `get_impact_radius()` walks outward from the changed nodes, relaxing a weighted
   best-path score per node (per-edge-kind weight × depth decay) rather than a plain BFS
3. Source snippets extracted for changed areas only
4. Review guidance generated (test coverage gaps, wide blast radius warnings)
5. Assembled into a structured, token-efficient context for MCP clients and the CLI
6. Where a cheap baseline can be estimated, compact `context_savings` metadata is attached as an estimate rather than an exact tokenisation

## Storage

### SQLite Schema
- **nodes** table: id, kind, name, qualified_name, file_path, line_start/end, language, community_id, etc.
- **edges** table: id, kind, source_qualified, target_qualified, file_path, line
- **metadata** table: key-value pairs (last_updated, build_type, schema_version)
- **flows** table: id, name, entry_point_id, depth, node_count, file_count, criticality, path_json
- **flow_memberships** table: flow_id, node_id, position
- **communities** table: id, name, level, parent_id, cohesion, size, dominant_language, description
- **nodes_fts** (FTS5 virtual table): full-text search on name, qualified_name, file_path, signature
- **community_summaries**, **flow_snapshots**, **risk_index** tables: compact precomputed summaries for token-efficient queries
- **embeddings** table (separate DB): qualified_name, vector, text_hash, provider

Indexes on qualified_name, file_path, edge source/target, criticality, community_id, and cohesion for fast lookups.

WAL mode enabled for concurrent read access during updates.

### Qualified Names
Nodes are uniquely identified by qualified names:
- Files: absolute path (e.g., `/repo/src/auth.py`)
- Functions: `file_path::function_name` (e.g., `/repo/src/auth.py::authenticate`)
- Methods: `file_path::ClassName.method_name` (e.g., `/repo/src/auth.py::AuthService.login`)

### C# namespace identity and binding

C# declarations include their namespace and complete containing-type path:
`/repo/Handlers.cs::App.Report.ExportHandler.Run`. `parent_name` contains
`App.Report.ExportHandler`; `extra.csharp_namespace` separately records `App`.
This distinguishes types in different namespaces even when they occupy the same
file. Namespace strings remain metadata, not synthetic graph nodes. The existing
file-level `csharp_namespaces` list remains available for file import/impact queries.

Parsing records facts; `csharp_resolver.py` binds calls after storage. C# calls and
imports carry `csharp_scopes`, ordered pairs of namespace names and namespace-body
byte offsets, innermost first. Compilation-unit scope uses offset `-1`. Reopened
bodies with the same namespace name have different offsets, so an ordinary using
cannot leak between them. File-scoped namespaces include all following members,
including grammars that represent those members as siblings. `source_offset`
distinguishes both receiver evidence and stored edges on the same source line
without a schema migration. Calls also retain `csharp_containing_type` so field
and property initializers keep their lexical context when their graph caller is a File.

The resolver selects a receiver type before looking up its method. It checks
enclosing types, enclosing namespaces, and imports at their actual lexical scopes;
it does not use file co-location, short-name uniqueness, or suffix matching as
visibility evidence. Namespace imports expose types, not child namespaces.
`global::` qualifications and simple namespace/type aliases retain their meaning.
`Alias::Type` searches only namespace aliases in the call's lexical scopes; it
does not select a same-named type, namespace, or local. Unqualified calls can reach
static methods in enclosing types, using parsed `csharp_static` evidence. Lookup
stops at a nearer method group; `this.Run()` stays within the immediate type.
Unqualified calls hidden by recorded local, parameter, field or property bindings
stay unresolved.
This follows the lookup order in the C# specification's
[namespace and type names](https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/language-specification/basic-concepts#78-namespace-and-type-names)
and [using directives](https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/language-specification/namespaces#146-using-directives).

Explicit global usings are collected across files owned by the nearest single
`.csproj`, rather than across all projects in the repository. With multiple project
files in that directory, ownership is unknown and global usings stay local to their
file. Loose C# files without a project share the review root as one compilation.
This is a structural approximation: linked/conditional compile items, generated or
implicit usings, project references and target frameworks require MSBuild evaluation.
Rebuild after changing project layout when updates are driven only by source watches.

Every C# call keeps `csharp_raw_target` and receiver evidence after binding. Resolved
edges are marked `INFERRED`; unresolved edges retain `unresolved_targets` so generic
graph fallbacks cannot bind them using weaker evidence. Each C# update re-evaluates
these calls, including unchanged callers. `TESTED_BY` mirrors move with their calls.
Before deleting a callee file, incoming managed calls return to their raw references
so recreating a declaration can resolve them again.
Binding changes persist a `csharp_flows_dirty` marker with the edge update.
The next full postprocess retraces all flows, including unchanged callers and
old/new entry points. Deferred postprocessing retains the marker even if a later
update parses no files; successful full flow replacement clears it atomically.
This reuses the existing full trace until the binder can provide a complete
affected set for incremental tracing.

`CSHARP_IDENTITY_VERSION = "5"` upgrades the old namespace-free format, the
nested-type-only format proposed in #937, and graphs lacking per-call lexical
context, static/callable evidence, or generic arity. Incremental updates reparse existing C# files
despite matching hashes.
The attempted version is recorded together with
failed file paths; subsequent updates retry those files alone and preserve their
last stored data until parsing succeeds. This avoids extending #944's repeated
full-rebuild loop. No SQL migration attempts to reconstruct missing source identity.

A generic declaration is keyed by arity — ``App.Box`1`` — so a constructed
reference reaches the declaration it names and never a same-named one of another
arity: `I<int>` binds to `I<T>`, `I` binds to `I`, and `Pair<int>` binds to
neither when only `Pair<K, V>` exists. The receiver's spelling is retained on the
call; only the key is derived from it, and the key is built from the syntax rather
than by scanning for angle brackets, so nested arguments and tuples count
correctly. A constructed containing type such as `Outer<T>.Inner` carries a second
arity that one key cannot describe, and stays unresolved.

This remains a structural graph, not a C# compiler. Overload selection, type
argument substitution and constraints (#943), inherited members, assembly
accessibility, and unqualified calls imported with `using static` are outside this
change; unsupported or ambiguous bindings stay unresolved. Inheritance target
spelling is unchanged.

## Parsing Strategy

Tree-sitter provides language-agnostic AST access. The parser:
1. Walks the AST recursively
2. Pattern-matches on node types (language-specific mappings in `_CLASS_TYPES`, `_FUNCTION_TYPES`, etc.)
3. Extracts names, parameters, return types, base classes
4. Identifies calls within function bodies
5. Resolves imports to module paths

This approach is more robust than tree-sitter queries across grammar versions.

## Visualization

The `visualization.py` module generates an interactive D3.js force-directed graph as a self-contained HTML file. It reads all nodes and edges from the SQLite graph store and renders them in the browser, allowing developers to visually explore code relationships, filter by node kind, and inspect dependencies.

## Impact Analysis Algorithm

BFS from seed nodes (changed files' contents):
1. Seed = all qualified names in changed files
2. For each node in frontier:
   - Follow forward edges (what this node affects)
   - Follow reverse edges (what depends on this node)
3. Expand up to `max_depth` hops (default: 2)
4. Collect all reached nodes as "impacted"

This captures both downstream effects (things that call changed code) and upstream context (things that the changed code depends on).
