# Memory Lens local memory API

This backend implements persistent live memory for the Lovable interface and portable tool bridge. It uses Node 24 built-ins, SQLite, and the OpenAI Responses API with `gpt-6-astra`. No package installation is required.

The delivered setup is local and single-owner. It does not provision Lovable Cloud, account authentication, or an internet-facing API. All clients possessing the configured local bearer credential add nodes to **ONE shared Agent memory graph** in that local workspace. Native `(source, external_session_id)` values identify provenance; they do not create separate graphs. The existing code KG remains a separate second layer.

## Start

Supply `MEMORY_LENS_TOKEN` (a bearer secret of at least 16 characters) and `OPENAI_API_KEY` through your environment. Keep the OpenAI key on this server; the browser and bridge use only the memory API credential.

For interactive zsh setup, these prompts avoid including credential values in command history:

```zsh
read -rs 'MEMORY_LENS_TOKEN?Local memory bearer secret: '
export MEMORY_LENS_TOKEN
printf '\n'
read -rs 'OPENAI_API_KEY?OpenAI API key: '
export OPENAI_API_KEY
printf '\n'
export MEMORY_LENS_ALLOWED_ORIGINS='https://id-preview--f720ce7c-c364-47ef-8074-2ebe2210988b.lovable.app'
node backend/server.mjs
```

The API defaults to `http://127.0.0.1:8787/memory-api`. The startup output reports whether a key is configured, without printing credentials. Configuration does not mean that an OpenAI request has been verified.

Optional environment settings:

| Variable | Default / purpose |
| --- | --- |
| `MEMORY_LENS_DB` | `~/.memory-lens/memory.sqlite`; override to choose the SQLite file |
| `MEMORY_LENS_PORT` | `8787` |
| `MEMORY_LENS_HOST` | `127.0.0.1`; binds loopback unless you explicitly change it |
| `MEMORY_LENS_ALLOWED_ORIGINS` | Comma-separated exact browser origins; no wildcard. If absent, only localhost/loopback browser origins are permitted. CLI clients without an Origin header still require authentication. |

The SQLite file is created with mode `0600`; its parent directory is created with mode `0700` when needed. It retains captured user prompts, evidence, and version history. The backward-compatible API may also retain an optional assistant response as source metadata, but that field is never sent to OpenAI and never used for memory extraction. No repository files, transcripts, or hidden reasoning are scanned.

Connect the portable bridge with `MEMORY_LENS_API_URL=http://127.0.0.1:8787/memory-api` and the same `MEMORY_LENS_TOKEN`. The Lovable browser must call this API from your machine, with its exact preview origin allowed. Browser local-network permissions may need to be granted. A remote server cannot reach your machine's loopback address. This setup does not create a tunnel or public deployment.

## API version 1

Send JSON by POST with `Authorization: Bearer <local-memory-token>`. Only `health` is unauthenticated. Identity is established by the credential; caller-supplied `owner_id` is rejected.

| Action | Request fields | Result |
| --- | --- | --- |
| `health` | None | API version, model, `openai_configured`, local mode, readiness |
| `ensure_session` | `source`, `external_session_id`, optional `title` | Registers a native source and returns the same canonical workspace graph `session_id`, `revision` |
| `list_sessions` | None | Exactly one shared graph, titled `Agent memory` |
| `graph` | `session_id` | `nodes`, active `edges`, recent captured `turns`, `revision` |
| `ingest_turn` | `session_id`, `turn_id`, `user_message`, optional `assistant_message`, `base_revision`, and provenance `source` / `external_session_id` / `native_turn_id` | Durable `applied`, `noop`, or `duplicate`, revision, changed IDs |
| `command` | `session_id`, `request_id`, `command` | Same mutation result plus explanatory message |
| `context` | `session_id`, optional `query`, optional `limit` | Active reference text, selected node IDs, revision |
| `layered_context` | `session_id`, optional `query`, optional `limit` (1–20) | Memory first, then a separate existing code KG, with ordered stages and combined agent reference text |

`issue_bridge_token` and `revoke_bridge_token` report unsupported local account administration. There is no fake browser JWT flow or bridge-token chaining. To rotate access in this local setup, change the server's bearer secret and update the clients.

Node IDs include the canonical workspace graph prefix; edges connect active memory nodes within that graph. Nodes, history, and source turns retain the tool, native chat ID, native turn ID, source kind, and exact captured evidence. `graph.sources` lists native origins. Forgotten preferences disappear from context across every connected tool. Corrections retain the node ID and superseded source history; explicit restoration reuses the original node.

Model responses use strict structured output and are validated again by the server. Automatic extraction receives **only the user prompt and the shared workspace memory snapshot**. Explicit memory commands receive their command and the same shared snapshot. Unknown or foreign workspace targets, fabricated evidence, duplicate active facts, and assistant-only assertions cannot become stored facts. Greetings are instructed to yield no operations. There is no heuristic or demo fallback when OpenAI is unavailable.

Automatic tool capture happens at `UserPromptSubmit`. User preferences such as "Use British English" or "Avoid em dashes" become evidence-backed constraints alongside decisions and facts. They can be corrected or forgotten through natural-language commands. Agent responses represent work done; `Stop` does not submit them to the memory extractor. The optional `assistant_message` wire field remains for older API clients' capture records and idempotency checks, without being forwarded to the provider.

## Durability and concurrency

Turns and commands reserve their idempotency ID in SQLite before calling OpenAI. Turn IDs are scoped by `(source, external_session_id, turn_id)`, so identical native IDs from different tools or chats cannot collide inside the shared graph. Supply `source` and `external_session_id` together; ingestion registers a new origin only after validating the canonical graph ID. `native_turn_id` identifies the original tool turn. Older clients without provenance use an explicit `legacy-api` / `workspace` origin. Completed same-content retries return `duplicate` without another model call. An ID reused with different content in the same origin returns `409 conflicting_turn`. Identical requests in one process share extraction; a pending reservation in another process returns a visible conflict. An interrupted reservation becomes retryable after a bounded 120-second lease.

Graph, history, revision, and event status commit in one SQLite transaction using a revision check. Concurrent changes return `409 revision_conflict`; the rejected extraction never overwrites the newer graph. Retrying re-extracts against the latest snapshot.

The bridge records `base_revision` before the coding turn. A manually applied memory command advances a stored manual revision. An older captured turn arriving later is acknowledged as a durable `noop` without a model request, retaining source evidence while preserving the manual edit. This intentionally suppresses *all* memory changes from the older turn. Clients omitting `base_revision` get the first observed server revision as their causal boundary; an exchange delayed before its first arrival has weaker protection. Automatic extraction also cannot restore forgotten or exact superseded facts. Explicit commands can restore them.

## Limits and failure behavior

- Request JSON: 100,000 bytes; UTF-8 is decoded after collecting chunks.
- Source: 100 characters; native/session/turn/request IDs: 255; title: 200.
- User and assistant messages: 20,000 characters each; commands: 4,000; context query: 500.
- Model operations: 50 per response; node content/evidence: 2,000 characters; edge label: 100.
- Shared graph: 1,000 nodes and 3,000 edges per installation. An oversized migrated graph permits corrections and Forget while rejecting further growth.
- Context: at most 20 nodes and 12,000 characters, with quoted untrusted reference values; forgotten and superseded content is excluded. Query overlap ranks active memories without excluding them. Pins, manual corrections, constraints, and recent changes receive priority so a generic follow-up such as "Continue" still receives the current policy.
- OpenAI request timeout: 30 seconds. HTTP request body timeout: 15 seconds.

No configured OpenAI key gives `503 openai_not_configured`. Provider errors, refusal, incomplete/invalid output, and timeouts give a sanitized `503 upstream_unavailable`. No fake memory is written. Source capture remains recorded with failed status, and same-content retries are allowed. Response errors never contain raw provider bodies or credentials.

## Verification

```sh
node --test tests/backend*.test.mjs tests/integration-bridge-api.test.mjs
```

Tests exercise the real HTTP server, real SQLite persistence/restart, controlled localhost Responses fixtures, and a real bridge child process. They cover auth, independent workspace isolation, one shared graph across native origins, origin-scoped idempotency, source-preserving correction/Forget history, punctuation-distinct identities, explicit restoration, rejected duplicate facts, fabricated evidence, UTF-8 streaming, concurrency/CAS, delayed turn protection, crash leases, missing key, provider failure/timeout, origin checks, and context caps.

No live OpenAI call or paid-model smoke test was made during implementation. A configured key and successful live response are still needed to validate your actual OpenAI account/model access.

## Two distinct knowledge graph layers

The additive retrieval pipeline is **shared agent memory KG → existing code KG → agent context**. All native chats and tools contribute to one editable user memory graph. The existing code KG is queried through its installed MCP server as a separate read-only source; it never becomes a set of memory nodes.

Each local installation can bind its own existing indexed checkout and MCP server command by setting:

- `MEMORY_LENS_CODE_GRAPH_REPO`: the existing checkout selected by that installation's operator.
- `MEMORY_LENS_CODE_GRAPH_COMMAND_JSON`: the server's executable and arguments as a JSON array. Use the existing MCP server's documented launch command; the bridge does not create or index a repository.

For example, the command value has the shape `["/absolute/path/to/existing-mcp-server", "its-stdio-option"]`; replace it with your actual installed server command. No repository or command configuration is accepted from HTTP callers. Configuration is optional and is not activated by the shipped tests or setup instructions above. The connector starts its process lazily on an authenticated layered retrieval and closes that child when the API closes.

An authenticated `layered_context` request first completes the shared memory context read. Only then does it pass the exact active memory text, revision, canonical graph ID, and query into existing code retrieval. The connector uses a minimal-context lookup, one bounded standard semantic search, and optionally a callers lookup for a returned canonical symbol. It performs no indexing, rebuilding, embedding, or source-file mutation.

The response retains the ordinary context fields `session_id`, `revision`, `node_ids`, and `text`, and adds separate `memory` and `code_graph` envelopes plus ordered `stages`. The stages are `memory`, `code_graph`, and `agent_context`. Code readiness is reported as `ready`, `not_ready`, `unavailable`, or `not_configured`. Missing or failing code retrieval leaves usable memory context and a truthful partial result; no sample graph is substituted. Both text blocks are JSON-quoted untrusted reference data, bounded to 12,000 encoded characters each and 24,000 combined.

`graph` may expose an ephemeral `latest_retrieval` trace for visualization. This trace is never written into SQLite nodes/edges and is visible only while its memory revision still matches the shared graph. A memory change from any tool invalidates it. A correction arriving while code retrieval is pending returns `409 revision_conflict` rather than return stale combined context.

`health` advertises the `layered_context` capability and existing-code-graph configuration/readiness without exposing private repository paths or repo IDs publicly. Configuration means "not yet verified" until a real retrieval succeeds. Every installation retains its own local database, token, and selected code repository; this extends the shared protocol, not the local single-owner tenancy model.

Layered regression verification uses synthetic connectors and controlled localhost HTTP/provider fixtures:

```sh
node --test tests/layered-api.test.mjs tests/backend*.test.mjs tests/integration-bridge-api.test.mjs
```

These tests verify ordered handoff of a corrected 14-day constraint, shared retrieval across native sources, rejected caller repository overrides, unchanged durable memory nodes, partial readiness, stale trace eviction, a correction during retrieval, encoded text bounds, and connector shutdown. No user's real chat or code was sent externally by these tests, no live existing-code MCP process was enabled by them, and the existing code-review checkout was not modified.

## Existing per-chat databases

On first opening an older per-chat database, the store transactionally creates a new canonical graph and consolidates a copy of its memory nodes, edges, source records, and turns. Every original legacy session and event row is retained unchanged. Exact normalized duplicate content shares one canonical node. Captured event and explicit history timestamps order node versions; session timestamps are a fallback when those records are absent. Corrections coalesce their historical predecessors, preserving provenance and remapping copied edges. Later explicit restorations survive, while automatic captures cannot revive superseded content. Other distinct content remains separate for explicit user reconciliation. No database is deleted or purged.

Only the canonical graph is listed or accepted by public graph/context/mutation actions. Tools must re-run `ensure_session` to replace an old cached graph ID. Every local installation retains its own database/token; the shared graph applies within that installation, never across users or machines automatically. The consolidation strategy is verified on synthetic old databases without opening any private live database during implementation.
