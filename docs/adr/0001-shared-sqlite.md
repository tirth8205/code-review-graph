# Shared SQLite

Status: Accepted for the #31 implementation; see crg-3uv.6 and crg-3uv.7.

When shared storage is selected, CRG owns one SQLite file. A connection binds
one canonical checkout before resolving any graph tables. The checkout
namespace covers native nodes, edges, metadata, FTS, embeddings, flows and
communities; SQLite stores all of them in the same WAL database. Existing
single-checkout indexes stay where they are. Rebuild from source to populate
the shared file.

Shared schema version 3 stores each exact embedding payload once and keeps
symbol links scoped to the checkout. A writer migrates version 1 vector
tables in one transaction, preserving legacy links without inferring Git
provenance for the vectors. It seeds structural generations only for
checkouts whose stored root matches the namespace and whose graph was
completed. Conflicting legacy vectors without timestamps keep their
individual links. New payloads are eligible for reuse through a common Git
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

A verified CLI, MCP or startup watcher refresh also interns its input manifest
digest and records it in its checkout's completion in the same transaction.
The manifest uses relative paths, SHA-256 of the bytes and the indexing
profile; it excludes file timestamps and Git anchors. Equal inputs alone do
not authorize graph reuse. Verified refreshes also intern normalized node and
edge payloads by SHA-256 and bind their immutable generation to the checkout
and Git ref in the same transaction. Equal materialized structural graphs
share their payloads and membership. The watcher publishes structural changes
without needing to rescan source files for the input manifest.

`query --generation-ref refs/heads/main` and the corresponding MCP parameter
read the published structural generation for the requested checkout. A
structural query also selects the current ref's generation if the native
tables were last built on a different ref. The VS Code reader follows the
same ref and checkout binding. Historical search rebuilds its FTS index from
published nodes and reads the published vector links. Historical flows
preserve their path through qualified symbols and remap their node IDs to
the materialized graph. The native graph remains the fast path for its
current ref. Historical community queries restore the published community
rows and memberships by qualified symbol, including the detector metadata.

Shared builds reuse Python AST evidence by source SHA-256 and extractor
profile. This evidence describes local function bindings and unreachable
calls/imports only; paths, aliases and resolved targets never enter the
payload. The build looks up the facts before dispatching a Python file to
a worker, with the same direct Git membership rule as embeddings. Workers
still parse with Tree-sitter and resolve imports against the requesting
checkout. The Python method-scope pass also shares colliding lexical
scopes, keyed by byte offset, Tree-sitter package and grammar profile.
Workers use their native syntax tree for the remaining passes; the writer
persists newly found scopes without permitting workers to write SQLite.

AI Hub selects and supervises the engine; it does not own graph data or
refresh leases. Its runtime acceptance and activation remain governed by
AI Hub ADR 0026 and ADR 0030.
