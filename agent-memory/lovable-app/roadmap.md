# Memory Lens

- [x] Build the midnight workspace and interactive graph, inspector, activity and context drawers.
- [x] Implement a typed replay adapter, isolated chat persistence, commands and version history.
- [x] Document the disconnected proposed CLI integration contract.
- [x] Test memory logic, replay controls and desktop/mobile interaction; check preview diagnostics.

## Memory-state regressions
- [x] Add focused tests and reproduce seeded duplicates, punctuation collisions and mixed-chat read leakage before changing the adapter (four regressions failed; 11 existing tests passed).
- [x] Fix canonical memory reuse, punctuation-safe identities and chat-scoped graph/context reads without UI or integration changes.
- [x] Run regression/full tests (16 passed), focused adapter/test TypeScript checks, lint (zero errors; seven existing warnings), and preview command/context/reload checks; automatic build OK.
## Live integration (code only)
- [x] Live mode selector, Connect source form, polling, test exchange, live commands/context/inspector.
- [x] Browser memory-api client with response validation and setup hints; UI and client tests.
- [x] Cloud-ready unapplied migration and undeployed memory-api handler with offline tests.
- [ ] Provision Cloud, apply migration, deploy function, configure OPENAI_API_KEY — blocked on separate approval.
- [ ] Live paid-model smoke test — blocked on a real key and approval.
- [x] ingest_turn.base_revision: client + test-exchange UI, prepared backend stale-capture noop (last_manual_revision in atomic commit), grounded evidence retained in history, offline regressions.
- [x] Review fixes: local "user" source kind, Pin only with advertised capability, stable command request_id across uncertain retries, HTTPS/loopback-only tokens + redirect:error, session_id checks, foreign-node rejection.
- [x] Cloud source (nondeployable): atomic reservations, command content hash, canonical reuse/restore, origin allowlist, JSON context, aligned limits; known limitations in cloud/README.md.
## Two-graph demo (code only)
- [x] ExistingGraphAdapter interface, labeled SAMPLE adapter, unconnected live adapter.
- [x] Ordered handoff (recall → search → prepare) with capsule carrying latest memory revision; 60-second sample demo with play/pause/step/replay and reduced motion.
- [x] Live mode: real context recall, trace stops at missing connector.
- [ ] Connect the user's real existing code graph — blocked until its source is supplied.
## Prompt-only memory + memory-first view
- [x] Replay and prepared backend derive memory from user prompts only; assistant-only claims are ignored (regressions).
- [x] British English / em-dash demo, forget/edit flow; test capture form is prompt-only.
- [x] Memory-only default view; "How it works (demo)" optional; live latest_retrieval status + Preview next-turn context via layered_context when advertised.

