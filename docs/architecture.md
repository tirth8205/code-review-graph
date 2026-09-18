# Architecture

## Overview

`code-review-graph` keeps a persistent, incrementally updated graph of a codebase in SQLite
and exposes it through a CLI and an MCP server. AI coding tools query the graph for
structural context (callers, dependents, tests, impact) instead of reading whole files.
Claude Code is one supported client among several.

## Components

```text
┌──────────────────────────────────────────────────────────────┐
│                    AI coding clients / CLI                   │
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
│  │  30 MCP tools + 5 MCP prompts              │              │
│  │  ├── Core: build, impact, query, review,   │              │
│  │  │   search, traverse, embed, stats, docs  │              │
│  │  ├── Flows: list, get, affected            │              │
│  │  ├── Communities: list, get, architecture  │              │
│  │  ├── Analysis: detect_changes, refactor,   │              │
│  │  │   apply_refactor, hubs, bridges, gaps   │              │
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

## Modules

All modules live in `code_review_graph/`.

| Module | Role |
|---|---|
| `parser.py` | Tree-sitter multi-language parser plus targeted fallbacks; emits nodes and edges per file |
| `custom_languages.py` | Config-driven languages from `.code-review-graph/languages.toml` |
| `graph.py` | `GraphStore`: SQLite storage, queries, impact radius |
| `migrations.py` | Versioned schema migrations, currently v11 (see `schema.md`) |
| `incremental.py` | File collection and ignore rules, git/SVN change detection, full and incremental builds, post-build resolvers |
| `postprocessing.py` | `run_post_processing()`: endpoint resolution, signatures, FTS sync, flows, communities, embedding refresh |
| `python_resolver.py`, `jedi_resolver.py`, `tsconfig_resolver.py`, `spring_resolver.py`, `event_resolver.py`, `temporal_resolver.py`, `rescript_resolver.py`, `hcl_resolver.py`, `scoped_resolver.py` | Post-build cross-file resolution |
| `flows.py` | Execution flow detection and criticality scoring |
| `communities.py` | Leiden community detection (igraph) with a file-based fallback |
| `search.py` | Hybrid search: FTS5 BM25 plus vector similarity |
| `embeddings.py` | Embedding providers and the `embeddings` table |
| `changes.py`, `refactor.py`, `analysis.py`, `hints.py`, `uncertainty.py`, `context_savings.py` | Change risk analysis, refactoring helpers, hub/bridge/gap analysis, response hints, empty-result markers, savings estimates |
| `tools/` | MCP tool implementations, split by domain |
| `main.py`, `prompts.py` | FastMCP server (30 tools, 5 prompts) |
| `cli.py`, `daemon.py`, `daemon_cli.py` | CLI and the multi-repo watch daemon |
| `visualization.py`, `exports.py`, `wiki.py`, `graph_diff.py`, `memory.py`, `forget.py` | HTML visualisation, export formats, wiki generation, snapshot diffing, Q&A memory, file removal |
| `skills.py`, `uninstall.py`, `registry.py`, `enrich.py`, `http_origin_guard.py`, `config_keys.py`, `constants.py` | Platform install/uninstall, multi-repo registry, hook enrichment, HTTP Host/Origin checks, shared helpers |
| `eval/`, `token_benchmark.py` | Evaluation framework and standalone token benchmark |

## Data Flow

### Full build (`incremental.full_build()`)
1. `collect_all_files()` lists tracked files (`git ls-files` when git is available, so
   untracked and gitignored files are skipped) and applies `.code-review-graphignore`.
2. Each file is read once; the bytes are hashed (SHA-256) and passed to
   `CodeParser.parse_bytes()`, which walks the Tree-sitter tree and emits nodes and edges.
3. `GraphStore.store_file_nodes_edges()` (or `store_file_batch()`) replaces the file's rows
   in one transaction, storing the hash for change detection.
4. Metadata is written: `last_updated`, `last_build_type`, and the git or SVN head.
5. Post-build resolvers qualify cross-file targets, then `run_post_processing()` computes
   signatures, rebuilds the FTS index, traces flows, detects communities and refreshes
   embeddings, unless skipped. An incremental update rewrites only the changed files'
   FTS entries (`search.update_fts_index`); a full build rebuilds the index.

### Incremental update (`incremental.incremental_update()`)
1. `get_changed_files()` asks the VCS for changed paths (git diff by default; SVN is
   supported).
2. `find_dependents()` collects files with `IMPORTS_FROM`, `CALLS`, `INHERITS` or
   `IMPLEMENTS` edges into the changed files, up to two hops and 500 files.
3. Changed and dependent files whose hash differs from the stored one are re-parsed; the
   rest are skipped.
4. Only those files' rows are replaced, then post-processing runs as for a full build.

### Review context (`tools/review.py: get_review_context()`)
1. Changed files come from a git diff or an explicit list.
2. `GraphStore.get_impact_radius()` finds the impacted nodes (see below).
3. Source snippets are extracted for the changed regions only, within a file budget.
4. `_generate_review_guidance()` adds warnings: changed functions without `TESTED_BY`
   edges, more than 20 impacted nodes, and inheritance changes.
5. `attach_context_savings()` adds an estimate of the tokens saved against reading the
   files, labelled `estimated: true`.

The MCP entry points are `get_review_context_tool`, `get_impact_radius_tool` and
`detect_changes_tool` in `main.py`.

## Impact Analysis

`GraphStore.get_impact_radius()` delegates to `get_impact_radius_sql()`, a bounded
best-score relaxation run inside SQLite. Setting `CRG_BFS_ENGINE=networkx` selects the older
Python-side traversal instead.

1. Seed with every node in the changed files.
2. For each depth step (default 2, `CRG_MAX_IMPACT_DEPTH`), propagate along edges in the
   direction set per edge kind in `constants.IMPACT_EDGE_DIRECTIONS`: dependency-shaped
   edges such as `CALLS` and `IMPORTS_FROM` flow from target to source (callers and
   importers of changed code), `TESTED_BY` flows from production code to its tests, and
   `CONTAINS` is not expanded because the whole file is already seeded.
3. Each reached node keeps its best score: previous score x edge weight
   (`constants.IMPACT_EDGE_WEIGHTS`, default 0.5) x depth decay
   (`CRG_IMPACT_DEPTH_DECAY`, default 0.6).
4. Results are ordered by score and cut at `CRG_MAX_IMPACT_NODES` (default 500); the
   response says whether it was truncated.

## Storage

One SQLite database, `.code-review-graph/graph.db`, in WAL mode so readers are not blocked
during updates. Tables: `nodes`, `edges`, `metadata`, `flows`, `flow_memberships`,
`communities`, `nodes_fts` (FTS5), `nodes_fts_state`, `community_summaries`,
`flow_snapshots`, `risk_index`, and
`embeddings` (created by `EmbeddingStore` in the same file). Columns, indexes and the
migration history are in [schema.md](schema.md).

Nodes are identified by qualified name: the file path for File nodes, `path::name` for
top-level symbols and `path::Class.method` for members.

## Parsing

The parser walks the Tree-sitter tree directly rather than using tree-sitter query files,
which keeps it independent of query syntax differences between grammar versions. Per-language
node type tables (`_CLASS_TYPES`, `_FUNCTION_TYPES` and similar in `parser.py`) drive:

1. Class, function and type extraction with names, parameters, return types and bases.
2. Call detection inside function bodies.
3. Import resolution to module paths, refined later by the post-build resolvers.

Languages without a grammar can be added through `.code-review-graph/languages.toml`
(`docs/CUSTOM_LANGUAGES.md`). Some formats (notebooks, Vue and Svelte SFCs, SQL, Ansible,
Spring configuration) use targeted parsers instead of Tree-sitter.

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

## Visualisation

`visualization.py` writes a self-contained HTML file with a D3.js force-directed graph of the
nodes and edges, with filters by node and edge kind and a search box.
