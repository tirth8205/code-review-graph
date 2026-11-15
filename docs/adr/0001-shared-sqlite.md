# Shared SQLite

Status: Accepted for the #31 implementation; see crg-3uv.6 and crg-3uv.7.

When shared storage is selected, CRG owns one SQLite file. A connection binds
one canonical checkout before resolving any graph tables. The checkout
namespace covers native nodes, edges, metadata, FTS, embeddings, flows and
communities; SQLite stores all of them in the same WAL database. Existing
single-checkout indexes stay where they are. Rebuild from source to populate
the shared file.

Shared schema version 2 stores each exact embedding payload once and keeps
symbol links scoped to the checkout. A writer migrates version 1 vector
tables in one transaction, preserving legacy links without inferring Git
provenance. New payloads are eligible for reuse through a common Git
directory or a directly shared read remote. Reusing a vector does not publish
it under a different remote.

`prune` previews orphan vector removal with the selected shared database.
`CRG_RETENTION_DAYS`, `CRG_RETENTION_PERCENT`, and `CRG_RETENTION_MAX_MIB`
default to 7, 10, and 512. The effective orphan budget is the smaller
space limit; active links are never pruned. Use `prune --apply --compact`
to reclaim physical space explicitly outside the watcher.

A refresh owns the native writer lock for that file and publishes its changes
together in one transaction. An interrupted writer loses the kernel lock and
its uncommitted changes. A caller may reuse a completion only for its own
checkout, matching inputs and graph profile. The watcher and tool callers use
the same writer and completion records. Data directory routing and watcher
health reporting do not change the refresh profile for a shared file.

A parser worker may reuse a syntax tree for identical preprocessed bytes
and the same loaded grammar. It still extracts nodes and resolves names for
each checkout. Syntax trees are retained only in a bounded worker-local
cache; the SQLite database is the only durable graph store.

AI Hub selects and supervises the engine; it does not own graph data or
refresh leases. Its runtime acceptance and activation remain governed by
AI Hub ADR 0026 and ADR 0030.
