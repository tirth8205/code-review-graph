# Knowledge Graph Schema

The graph is one SQLite database, `.code-review-graph/graph.db`, opened in WAL mode.
The base tables and indexes come from `_SCHEMA_SQL` in `code_review_graph/graph.py`.
Everything else is added by the versioned migrations in `code_review_graph/migrations.py`.
The current schema version is 10.

## Node Types

`nodes.kind` holds one of the values below.

### File
One row per parsed file.

| Column | Value |
|---|---|
| name | File path, as stored at build time (absolute) |
| file_path | Same as `name` |
| language | Detected language (`python`, `typescript`, `go`, ...) |
| line_start | 1 |
| line_end | Line count |
| file_hash | SHA-256 of the file bytes, used for change detection |

### Class
A class, struct, interface, enum or module definition.

| Column | Value |
|---|---|
| name | Class name |
| file_path | Containing file |
| line_start, line_end | Definition range |
| language | Source language |
| parent_name | Enclosing class, for nested classes |
| modifiers | Access modifiers (`public`, `abstract`, ...) where the grammar exposes them |

### Function
A function, method or constructor.

| Column | Value |
|---|---|
| name | Function name |
| file_path | Containing file |
| line_start, line_end | Definition range |
| language | Source language |
| parent_name | Enclosing class, for methods |
| params | Parameter list as source text |
| return_type | Return type annotation |
| signature | Signature text computed by post-processing and indexed by `nodes_fts` (added in v2) |
| is_test | 1 for test functions |

### Test
Same columns as Function, with `kind = 'Test'` and `is_test = 1`. A function is a test when
any of these hold (`_is_test_function` in `parser.py`):

- Its name matches `^test_`, `^Test`, `_test$`, `_spec$`, `.test.` or `.spec.`.
- It is in a test file (`test_*.py`, `*_test.py`, `*.test.ts`, `*.spec.js`, `*_test.go`,
  `tests/`, `__tests__/`, `*Test.java`, `*Test.kt`, `*_test.dart`, R `testthat`, Julia
  `test/`, ReScript `*_test.res`) and is named like a test-runner call (`describe`, `it`,
  `test`, `beforeEach`, ...).
- It carries a test annotation: JUnit `@Test`, `@ParameterizedTest`, `@RepeatedTest`,
  `@TestFactory`, or Rust `#[test]`, `#[tokio::test]`, `#[async_std::test]`, `#[rstest]`,
  `#[proptest]`.

### Type
A type alias, interface, enum or similar construct where the language parser emits one.
Columns as for Class.

### Endpoint
A synthesised routed entry point, emitted for Spring request mappings and WebFlux functional
routes. Linked to the handling method by a `HANDLES` edge.

### Scheduler
A synthesised node for a `@Scheduled` method. Linked to the method it fires by a `TRIGGERS`
edge.

### ConfigProperty
A configuration key parsed from Spring `application.properties` or `application.yml`.
Values are discarded; only the key is stored. Linked to the code that binds it by a
`DEPENDS_ON_CONFIG` edge.

### Event
A synthesised node for a Spring application event, created after the build by
`event_resolver.py` from `PUBLISHES` and `HANDLES` edges. Its `file_path` is the placeholder
`event` and `extra` carries `{"event_type": ..., "virtual": true}`.

## Edge Types

Every edge has `source_qualified`, `target_qualified`, `file_path` (where the relationship
was seen), `line`, `extra` (JSON), `confidence`, `confidence_tier` and, for
`CALLS` and `REFERENCES`, `target_resolution`.

| Kind | Source -> target | Notes |
|---|---|---|
| CALLS | caller -> called function | Target may be a bare name until a resolver qualifies it; `target_resolution` records which |
| IMPORTS_FROM | importing file -> imported module, file or package directory | `file_path` equals the source. `extra.import_scope` marks a DIRECTORY target: `package` (a Go import names a directory of files) or `tree` (a Ruby `require_all` names everything below one). The read path expands a directory to its members; see `import_scope_ancestors` in `graph.py` |
| INHERITS | child class -> parent class | |
| IMPLEMENTS | implementing class -> interface | |
| CONTAINS | file -> class or function; class -> method | Structural containment |
| TESTED_BY | function -> test function | |
| REFERENCES | node -> symbol used as a value | Callback maps, arrays, assignment |
| DEPENDS_ON | general dependency | Ansible role `meta` dependencies, Solidity `using` directives |
| INJECTS | Spring bean -> injected field or constructor parameter type | Spring enrichment |
| CONSUMES | `@KafkaListener` / `@KafkaHandler` method -> `kafka:<topic>` | Spring Kafka enrichment |
| PRODUCES | Kafka producer -> `kafka:<topic>` | Spring Kafka enrichment |
| TEMPORAL_STUB | class -> declared Temporal workflow or activity interface of a stub field | Used by `temporal_resolver.py` to resolve calls made through the stub |
| DEPENDS_ON_CONFIG | `@ConfigurationProperties` class -> `ConfigProperty` | Spring enrichment |
| HANDLES | `Endpoint` -> controller method; `@EventListener` method -> event | Spring enrichment |
| TRIGGERS | `Scheduler` -> `@Scheduled` method | Spring enrichment |
| PUBLISHES | method -> Spring application event | Spring enrichment |

`OVERRIDES` has an impact weight in `constants.py` but no parser emits it.

## Qualified Name Format

```text
/absolute/path/to/file.py                                  # File
/absolute/path/to/file.py::function_name                   # top-level function
/absolute/path/to/file.py::ClassName.method_name           # method
/absolute/path/to/file.py::OuterClass.InnerClass.method    # nested class method
```

`nodes.symbol` stores the part after the first `::` (or the whole name when there is none)
so that dotted-tail lookups are an indexed equality test.

## SQLite Tables

Base tables from `graph.py`. Columns marked with a version are added by that migration on
existing databases.

```sql
CREATE TABLE nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL UNIQUE,
    file_path TEXT NOT NULL,
    line_start INTEGER,
    line_end INTEGER,
    language TEXT,
    parent_name TEXT,
    params TEXT,
    return_type TEXT,
    modifiers TEXT,
    is_test INTEGER DEFAULT 0,
    file_hash TEXT,
    extra TEXT DEFAULT '{}',
    symbol TEXT,                 -- v10
    docstring TEXT,              -- v13
    name_tokens TEXT,            -- v13
    updated_at REAL NOT NULL,
    signature TEXT,              -- v2
    community_id INTEGER         -- v4
);

CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    source_qualified TEXT NOT NULL,
    target_qualified TEXT NOT NULL,
    file_path TEXT NOT NULL,
    line INTEGER DEFAULT 0,
    extra TEXT DEFAULT '{}',
    confidence REAL DEFAULT 1.0,              -- v9
    confidence_tier TEXT DEFAULT 'EXTRACTED', -- v9
    target_resolution TEXT,                   -- v11; 'direct' | 'unresolved' | NULL
    updated_at REAL NOT NULL
);

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

Tables added by migrations:

```sql
-- v3
CREATE TABLE flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    entry_point_id INTEGER NOT NULL,
    depth INTEGER NOT NULL,
    node_count INTEGER NOT NULL,
    file_count INTEGER NOT NULL,
    criticality REAL NOT NULL DEFAULT 0.0,
    path_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE flow_memberships (
    flow_id INTEGER NOT NULL,
    node_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (flow_id, node_id)
);

-- v4
CREATE TABLE communities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 0,
    parent_id INTEGER,
    cohesion REAL NOT NULL DEFAULT 0.0,
    size INTEGER NOT NULL DEFAULT 0,
    dominant_language TEXT,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- v5, widened by v13 (rebuilt by search.rebuild_fts_index from the same
-- migrations.NODES_FTS_DDL, so the two cannot drift)
CREATE VIRTUAL TABLE nodes_fts USING fts5(
    name, qualified_name, file_path, signature, docstring, name_tokens,
    content='nodes', content_rowid='rowid',
    tokenize='porter unicode61'
);

-- v6
CREATE TABLE community_summaries (
    community_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    purpose TEXT DEFAULT '',
    key_symbols TEXT DEFAULT '[]',
    risk TEXT DEFAULT 'unknown',
    size INTEGER DEFAULT 0,
    dominant_language TEXT DEFAULT '',
    FOREIGN KEY (community_id) REFERENCES communities(id)
);

CREATE TABLE flow_snapshots (
    flow_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    entry_point TEXT NOT NULL,
    critical_path TEXT DEFAULT '[]',
    criticality REAL DEFAULT 0.0,
    node_count INTEGER DEFAULT 0,
    file_count INTEGER DEFAULT 0,
    FOREIGN KEY (flow_id) REFERENCES flows(id)
);

CREATE TABLE risk_index (
    node_id INTEGER PRIMARY KEY,
    qualified_name TEXT NOT NULL,
    risk_score REAL DEFAULT 0.0,
    caller_count INTEGER DEFAULT 0,
    test_coverage TEXT DEFAULT 'unknown',
    security_relevant INTEGER DEFAULT 0,
    last_computed TEXT DEFAULT '',
    FOREIGN KEY (node_id) REFERENCES nodes(id)
);

-- v12, widened by v13 to mirror every nodes_fts column
CREATE TABLE nodes_fts_state (
    node_id INTEGER PRIMARY KEY,
    name TEXT,
    qualified_name TEXT,
    file_path TEXT,
    signature TEXT,
    docstring TEXT,      -- v13
    name_tokens TEXT     -- v13
);
CREATE INDEX idx_nodes_fts_state_file ON nodes_fts_state(file_path);
```

`nodes_fts` is an external content table: it holds the inverted index but reads column
values from `nodes`. Removing one of its entries therefore needs the values that were
indexed, and those are gone once the node row is deleted. `nodes_fts_state` mirrors what
the index currently holds so `search.update_fts_index` can rewrite just the rows an
update touched instead of dropping and repopulating the whole index. It starts empty
after the migration; the first index sync fills it with one full rebuild. Its columns
have to be exactly `migrations.NODES_FTS_COLUMNS`, because an external-content delete
replays every indexed value; the `fts_state_synced` metadata key records the mirror
shape, and a mirror written under an older value forces one rebuild.

### Embeddings

`EmbeddingStore` in `code_review_graph/embeddings.py` creates this table in the same
`graph.db` when embeddings are first generated. It is not part of the migration chain; a
missing `provider` column is added when the store opens.

```sql
CREATE TABLE embeddings (
    qualified_name TEXT PRIMARY KEY,
    vector BLOB NOT NULL,            -- float32 array
    text_hash TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'unknown'
);
```

### Metadata keys

| Key | Set by |
|---|---|
| `schema_version` | `migrations.py`; `13` on a current database |
| `fts_state_synced` | `search.rebuild_fts_index`; mirror-shape version |
| `last_updated` | Full and incremental builds |
| `last_build_type` | Full and incremental builds |
| `git_head_sha`, `git_branch` | Builds in a git checkout |
| `svn_branch`, `svn_revision` | Builds in an SVN working copy |
| `postprocess_level` | Post-processing |

### Indexes

| Index | Columns | Added |
|---|---|---|
| `idx_nodes_file` | `nodes(file_path)` | base |
| `idx_nodes_kind` | `nodes(kind)` | base |
| `idx_nodes_qualified` | `nodes(qualified_name)` | base |
| `idx_edges_source` | `edges(source_qualified)` | base |
| `idx_edges_target` | `edges(target_qualified)` | base |
| `idx_edges_kind` | `edges(kind)` | base |
| `idx_edges_file` | `edges(file_path)` | base |
| `idx_edges_target_kind` | `edges(target_qualified, kind)` | base, v7 |
| `idx_edges_source_kind` | `edges(source_qualified, kind)` | base, v7 |
| `idx_flows_criticality` | `flows(criticality DESC)` | v3 |
| `idx_flows_entry` | `flows(entry_point_id)` | v3 |
| `idx_flow_memberships_node` | `flow_memberships(node_id)` | v3 |
| `idx_nodes_community` | `nodes(community_id)` | v4 |
| `idx_communities_parent` | `communities(parent_id)` | v4 |
| `idx_communities_cohesion` | `communities(cohesion DESC)` | v4 |
| `idx_risk_index_score` | `risk_index(risk_score DESC)` | v6 |
| `idx_edges_composite` | `edges(kind, source_qualified, target_qualified, file_path, line)` | v8 |
| `idx_nodes_symbol` | `nodes(symbol)` | v10 |
| `idx_edges_kind_target_resolution` | `edges(kind, target_resolution)` | v11 |

### Migrations

Each migration runs in its own transaction and updates `schema_version` on success.

| Version | Change |
|---|---|
| 2 | `nodes.signature` |
| 3 | `flows`, `flow_memberships` and their indexes |
| 4 | `communities`, `nodes.community_id` and their indexes |
| 5 | `nodes_fts` FTS5 table |
| 6 | `community_summaries`, `flow_snapshots`, `risk_index`, `idx_risk_index_score` |
| 7 | `idx_edges_target_kind`, `idx_edges_source_kind` |
| 8 | `idx_edges_composite` |
| 9 | `edges.confidence`, `edges.confidence_tier` |
| 10 | `nodes.symbol`, back-filled from `qualified_name`, and `idx_nodes_symbol` |
| 11 | `edges.target_resolution`, back-filled for `CALLS`/`REFERENCES`, and `idx_edges_kind_target_resolution` |
| 12 | `nodes_fts_state`, the mirror of the FTS index, and `idx_nodes_fts_state_file` |
| 13 | `nodes.docstring`, `nodes.name_tokens`, both back-filled; `nodes_fts` and `nodes_fts_state` widened to carry them |
