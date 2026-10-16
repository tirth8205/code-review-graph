# Memory Lens portable tool bridge

This bridge connects a tool's visible user prompts to the version 1 `memory-api` protocol. It uses Node 22 or newer and built-in modules only. Code and synthetic HTTP integration tests are provided; no tool hooks or external connections are enabled by installing this package.

Your local workspace has **one shared agent memory graph across all your chats and tools**. Native tool/chat IDs identify where prompts came from; they do not create separate memory graphs. All configured tools retrieve the same active preferences, facts, decisions, and constraints. The existing code graph remains a separate second retrieval layer.

The full package's local SQLite backend and integration test suite require Node 24; the standalone portable bridge uses no SQLite APIs.

## Configure privately

For the delivered local setup, use `http://127.0.0.1:8787/memory-api` and the same private `MEMORY_LENS_TOKEN` configured on the backend. See [backend setup](README-backend.md). Set these in the tool's launch environment or your private secrets manager. Do not paste credentials into chat or commit them. A separately deployed service can use an HTTPS endpoint and its own workspace credential instead.

```sh
export MEMORY_LENS_API_URL='http://127.0.0.1:8787/memory-api'
# MEMORY_LENS_TOKEN is supplied privately, as described in backend setup.
node bin/memory-lens.mjs health
```

`health` does not send a token. Other actions send `Authorization: Bearer …` to that exact endpoint. HTTPS is required except for localhost development. Redirects are refused. Optional `MEMORY_LENS_TIMEOUT_MS` sets each request's timeout (default 30000; range 50–120000).

## Any tool: explicit event ingestion

An IDE extension or chat wrapper can pipe each visible user prompt into the bridge when it is submitted:

```sh
node bin/memory-lens.mjs ingest <<'JSON'
{
  "source": "my-editor",
  "external_session_id": "the-native-conversation-id",
  "turn_id": "the-native-turn-id",
  "base_revision": 0,
  "user_message": "Use Stripe for billing. Refunds are allowed for seven days."
}
JSON
```

`source` plus `external_session_id` identifies one native conversation for provenance and queue ordering. Each origin maps to the same canonical memory graph. The bridge sends `source`, `external_session_id`, and `native_turn_id` with every capture and qualifies the API event ID with a SHA256 of the tool, native chat, and original capture ID. Identical turn IDs from different chats/tools therefore coexist; exact retries keep their qualified identity and original payload. The backend rejects a reused event with different content. For causal protection, resolve the current shared memory revision before capture and include it as `base_revision`; retries retain that original revision. Omitting it gives the service no reliable indication that capture started before a later global manual correction. Generic ingestion still accepts an optional `assistant_message` for legacy metadata compatibility, but automatic capture never sends it and memory extraction excludes it.

Admission limits match the API: source 100 characters; conversation/turn/request IDs 255; visible user and assistant messages 20,000 each; context query 500; command 4,000; total encoded ingestion 100,000 bytes. Oversized visible exchanges are rejected before network or outbox admission, with a visible local error. Hooks emit a generic skipped-capture diagnostic and keep the agent usable. Capture text is never silently truncated; only the retrieval query derived from a longer supported prompt is capped to 500 characters.

Read and edit your shared memory through any configured native tool/chat origin:

```sh
node bin/memory-lens.mjs graph --source my-editor --session the-native-conversation-id
node bin/memory-lens.mjs context --source my-editor --session the-native-conversation-id --query refunds --limit 10
node bin/memory-lens.mjs command --source my-editor --session the-native-conversation-id <<'TEXT'
Change the refund window to 14 days.
TEXT
```

You can select the canonical API graph with `--session-id UUID` instead. `sessions` returns one "Agent memory" entry. Selected-ID ingestion without an explicit origin uses `manual-cli` plus a stable selected-memory identifier for provenance. `command --request-id STABLE_ID` makes manual command retries idempotent; otherwise a UUID is generated. Commands are not automatically retried because doing so without preserving request identity could repeat a requested edit. `context` returns the shared revision, active memory text, node IDs, and an explicit `pending_ingestion` count when queued captures could not finish. A forgotten preference stops appearing across all your tools and chats.

## Automatic Codex lifecycle capture

With the bridge environment configured, install into an explicit target project:

```sh
node /ABSOLUTE_PATH/bin/memory-lens.mjs install-codex-hooks --project /ABSOLUTE_TARGET_PROJECT
```

The installer only merges three command hooks into that project's `.codex/hooks.json`. It preserves other events, handlers, matchers, and top-level metadata; creates a private backup before changes; and refuses malformed files or symlink targets. It does not change global Codex configuration or hook trust. Launch Codex in the trusted project, then open `/hooks` to review and trust the new definitions. Your Codex version must support the documented events and fields. [Official Codex hooks documentation](https://learn.chatgpt.com/docs/hooks).

- `UserPromptSubmit`: resolves the current shared memory revision, durably queues and captures the user prompt immediately with native-chat provenance, then retrieves shared memory followed by the existing code graph. The agent receives quoted reference data using `hookSpecificOutput.additionalContext`. An unavailable API uses the last owner-wide cached revision (or zero) for capture; retries retain that original boundary.
- `Stop`: always returns `{}` with exit 0. It performs no network calls and never reads or captures `last_assistant_message`, even when another hook requests continued work.
- `SessionStart`: restores the same shared memory for startup, resume, clear, or compact in any native chat.

Hook capture keys use `nativeTurnId:prompt:SHA256(user-prompt)` and are then qualified by the tool and native chat for the API's shared graph. Repeated submission hooks for the same native turn and prompt do not create another capture or model call after durable acknowledgment. A changed prompt under that same native turn is rejected as a conflict. Completed private prompt text is retained for 24 hours, then compacted on later hook use into a private hash/revision/identity tombstone; the tombstone keeps duplicates idempotent without retaining the full prompt. Pending outbox records protect the original prompt evidence until acknowledged. A delayed acknowledgment preserves its original base revision. The bridge does not read `transcript_path`, agent responses, repository files, tool output, or hidden reasoning. Missing native session/turn fields safely skip capture rather than invent provenance.

Unusually long native turn IDs use a SHA256 prefix instead, keeping the resulting API capture ID within 255 characters while preserving the original native ID in private prompt state.

The bridge checks the API's unauthenticated health capability and automatically uses `layered_context` when advertised; older APIs continue using `context`. Capability discovery is cached for 60 seconds. `MEMORY_LENS_LAYERED_CONTEXT=0` explicitly selects memory-only context; `1` explicitly selects the layered action. An advertised layered endpoint failure is reported rather than silently returning memory-only data. The complete quoted hook envelope, including its header and pending-ingestion diagnostic, is bounded to 24,000 characters; truncation drops entire reference IDs rather than clipping canonical identities.

## MCP for compatible clients

Set the MCP connection's tool/chat provenance through launch configuration:

```sh
export MEMORY_LENS_SOURCE='my-editor'
export MEMORY_LENS_EXTERNAL_SESSION_ID='the-native-conversation-id'
node bin/memory-lens.mjs mcp
```

Alternatively set `MEMORY_LENS_SESSION_ID` to the canonical graph selected by the operator; native origins are re-ensured so an older cached graph mapping refreshes. The default selected-ID provenance is `mcp` plus a stable selected-memory identifier, or supply `MEMORY_LENS_SOURCE` and `MEMORY_LENS_EXTERNAL_SESSION_ID`. Tool arguments cannot override the configured memory owner or origin. Every MCP connection retrieves the same shared graph. The newline-delimited stdio JSON-RPC server exposes `memory_graph`, `memory_context`, `memory_command`, and `memory_ingest_turn`, with initialize, ping, and tool discovery. It writes only protocol JSON to stdout.

**MCP registration alone does not automatically capture prompts.** A client extension/lifecycle hook must supply each visible user prompt, or explicitly call `memory_ingest_turn`. Supply `base_revision` obtained before capture; without it, the server reserves the first-observed revision, which cannot establish when an older offline prompt was created. An omitted boundary stays omitted on exact retries so a newer graph revision cannot accidentally change the event payload. Native origins remain useful for provenance even though memory is shared; a client that cannot provide accurate native origin IDs should use the event API or lifecycle bridge.

## Existing code graph: a separate second layer

The local memory backend can call your existing `code-review-graph` MCP server after it retrieves your shared agent memory. Configure the backend launch environment with the absolute path of the repository whose code graph you already built:

```sh
export MEMORY_LENS_CODE_GRAPH_REPO='/ABSOLUTE_INDEXED_REPOSITORY'
# Start the local memory backend with its privately supplied credentials.
node backend/server.mjs
```

The default command is `code-review-graph serve --repo /ABSOLUTE_INDEXED_REPOSITORY`, so the existing engine must already be available on the backend's PATH. To select an existing virtual environment executable, optionally supply `MEMORY_LENS_CODE_GRAPH_COMMAND_JSON` as a JSON array of command arguments, for example `["/ABSOLUTE_ENV/bin/code-review-graph","serve","--repo","/ABSOLUTE_INDEXED_REPOSITORY"]`. This is trusted operator configuration. The adapter launches it directly with no shell, uses the selected repository as its working directory, and fixes every tool's `repo_root` to that repository. API requests, agent tool arguments, and conversation memory cannot select another repository or command.

The code graph subprocess receives a limited runtime environment and nonsecret `CRG_*` configuration. It does not inherit the memory API token, the backend's OpenAI key, or unrelated service credentials. If your existing engine requires its own remote embedding credentials, supply them through its trusted private launcher or credential manager; keep them separate from the memory backend's launch environment.

`ExistingCodeGraph` lazily initializes stdio MCP and requests `get_minimal_context_tool` first. When the index is ready, it calls `semantic_search_nodes_tool` with limit 12, standard detail, and source snippets disabled; the existing engine's minimal search format omits canonical qualified names, which the visualization needs for stable node IDs. It can then call `query_graph_tool` once for direct callers of a returned canonical node, with at most eight results. The search receives the current task plus bounded, quoted conversation memory. This reuses the existing engine's search and relationship logic. The adapter never requests build, update, watch, embedding, or export tools. The engine may open or migrate its own graph store through its normal retrieval code.

The `layered_context` action returns your shared agent memory first and a **separate** code graph second. Code node IDs are the engine's qualified names; edges connect only returned canonical nodes. Results are capped at 20 nodes, 50 edges, and 12,000 characters of JSON-quoted, explicitly untrusted reference text. Forgotten or corrected facts remain governed by the shared memory service; code nodes are not inserted into that memory graph. Each local installation binds its own repository and has a stable opaque `repo_id` derived from its canonical path.

Absent configuration returns `not_configured`; missing, empty, or stale indexes return `not_ready`. An empty task with no memory returns `not_ready` with `reason: no_query`, so an unverified connector is never labeled ready. Older engine versions without `get_minimal_context_tool`, malformed responses, unavailable executables, and timed-out processes return `unavailable`, without fabricated nodes. An unavailable optional relationship lookup can return genuine search nodes with `reason: relationships_unavailable`. The client bounds each retrieval to ten seconds by default and each protocol response to 256 KiB, drains private stderr without logging it, and terminates failed processes and their process group with bounded cleanup. Windows uses native `taskkill` for tree termination; fixture verification was performed on macOS. The backend closes the MCP process when it shuts down. No real code graph integration is enabled in the delivered fixtures.

## Offline behavior and local privacy

The bridge persists each user prompt before its capture request. Applied, noop, or duplicate acknowledgments clear the outbox. Network/provider failures, non-durable responses, and conflicts remain queued; no fake success or replay fallback occurs. Each native origin replays in enqueue order with a stable monotonic tie-breaker. An earlier failed capture blocks later captures from that origin while other origins can continue. Shared prompt/context retrieval flushes queued captures across your origins, up to 20 per call with a bounded flush budget. Global manual edits protect the shared graph from older captures through their original causal revision. Manual `ingest` and `flush` exit 1 while work remains pending. Hooks keep the agent usable and emit a generic diagnostic without credentials/provider response bodies. Retry explicitly with `node bin/memory-lens.mjs flush`. Each HTTP request still has its configured timeout.

Files contain visible user prompt text and should be treated as private. The state root defaults to `~/.local/state/memory-lens`; optionally set `MEMORY_LENS_STATE_DIR` to a dedicated private directory. Directories are 0700 and records/backups are 0600. Endpoint + credential hashes partition local state so changing accounts or endpoints never flushes one account's conversations into another. Token values are never written to records. Failed captures persist until sent or you deliberately delete their private outbox. Completed user prompts compact after 24 hours on later hook use into hash/revision/identity tombstones; the session revision cache contains no conversation text.

## Verification

```sh
npm test
```

Bridge tests use real localhost HTTP fixtures and child processes for CLI, immediate user-only capture, complete assistant exclusion, safe installer merging, shared memory across chats/tools, origin-qualified idempotency, causal revisions, offline spooling, private tombstones, credential partitioning, and MCP framing. The SQLite integration verifies shared graph growth, cross-tool preference reuse, global forgetting, distinct native origins, duplicate capture without rebilling, and separate code retrieval. Existing code graph tests spawn a synthetic MCP server to verify operator repository binding, canonical identities, graph/text bounds, readiness, missing capabilities, credential boundaries, private diagnostics, timeouts, and process-tree cleanup. They make no paid model, real code graph, or external-account calls. Live capture requires an actual running backend, securely configured credentials, and reviewed/trusted hooks; a real GPT-6 Astra smoke test is separate from these fixture tests.
