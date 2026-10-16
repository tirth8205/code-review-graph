# code-review-graph: Agent memory

One growing agent memory graph across your chats and tools, alongside the existing code-review-graph code graph.

Only user prompts and explicit human memory commands create or change memories. Native tool/chat IDs record provenance; they do not partition memory. Assistant replies and Stop events never feed extraction. Each installation has its own private owner graph.

The agent reads memory first, then searches the existing code graph, then receives bounded context. Stores stay separate, with no permanent cross-graph edges. The original graph-building engine is unchanged.

## Implementation and verification

Node 24 SQLite service with GPT-6 Astra extraction, atomic edits, evidence, version history, idempotent events and causal revisions. Portable CLI/event/MCP bridge and immediate Codex UserPromptSubmit capture, with private retry queues and context injection. Lovable shows only memory and supports natural-language add, connect, correct and forget.

Final local tests: **84 passed**. Lovable reports **89 frontend tests passed**, types/build/lint passed. Real nonprivate prompts resolved the same graph across tools. A live frontend command added a refund policy and billing relationship. The prepared prompt hook returned four active memories and **12 genuine code nodes** from the user's existing index.

Read [backend setup](README-backend.md), [bridge setup](README-bridge.md) and [verification](docs/verification.md). Keep the OpenAI key on the backend. The delivered machine-local service uses http://127.0.0.1:8788/memory-api; general examples default to 8787.

The [Lovable preview](https://id-preview--f720ce7c-c364-47ef-8074-2ebe2210988b.lovable.app/?mode=live&v=f41d9558) is connected to the local service in the paired browser tab and automatically reconnects after reload. A new tab must pair its own credential. Hosted multi-user Cloud source remains unapplied.

Project-local hooks are prepared in .codex/hooks.json. Open /hooks in a compatible Codex CLI to review and trust them before native automatic capture runs. The local launcher reads its workspace credential privately; it contains no credential value. A real standalone hook invocation was verified with a nonprivate prompt. Other tools require their lifecycle adapter or explicit event/MCP ingestion.

## Demo

Submit a user prompt requesting British English and em dashes. Inspect two independently editable preferences. Add a fact from another tool; the same graph grows with source provenance. Say "Forget the em-dash preference; it was only for this task." British English remains active globally. The next prompt reads memory, then the existing code graph.

The one-minute native 4K video uses British female AI narration, original music, animated product interactions and actual Framer Motion. Visual source lives in docs/video/motion. It is authored product animation, not recorded live-agent footage. Token savings have not been benchmarked.

lovable-app is a partial review mirror; the connected Lovable project owns the complete app. The local service is single-owner. Possession of its access credential grants that workspace's API access.
