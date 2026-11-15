# Shared SQLite

Status: Accepted for the #31 implementation; see crg-3uv.6 and crg-3uv.7.

When shared storage is selected, CRG owns one SQLite file. A connection binds
one canonical checkout before resolving any graph tables. The checkout
namespace covers native nodes, edges, metadata, FTS, embeddings, flows and
communities; SQLite stores all of them in the same WAL database. Existing
single-checkout indexes stay where they are. Rebuild from source to populate
the shared file.

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
