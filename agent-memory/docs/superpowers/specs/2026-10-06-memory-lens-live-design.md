# Agent memory design

Exactly two independent graphs: one growing owner/workspace memory graph and the existing code-review-graph index. Native chats/tools identify provenance, never partitions. Separate installations/users remain isolated.

UserPromptSubmit captures visible user text before work. Assistant output, hidden reasoning, transcripts and Stop never generate memories. Extraction splits independent preferences, retains evidence, merges repeats and records history. Source-qualified event IDs and original causal revisions protect idempotency and later human corrections.

The authenticated local POST protocol supports health, ensure_session, list_sessions, graph, ingest_turn, command, context and layered_context. Every native origin maps to the same canonical owner graph. Human commands add/connect/correct/forget that graph. Forgotten and superseded versions stay out of active context.

Retrieval order is memory, existing code graph, agent context. The existing MCP engine is operator-bound and queried with read tools only, bounded text and canonical identities. Code nodes never enter editable memory. No permanent cross-graph edges are created. Keyword-only indexes retry the literal task if the memory-enriched search is empty, retaining three calls maximum.

Lovable shows the shared memory layer with evidence/history, polls revisions and rejects stale/foreign replies. The OpenAI key never enters the frontend. Native lifecycle hooks require built-in client trust. Other tools use a lifecycle adapter or explicit event/MCP ingestion.

The delivered service is single-owner local SQLite. Hosted multi-user Cloud source is an unapplied blueprint requiring independent deployment verification.

