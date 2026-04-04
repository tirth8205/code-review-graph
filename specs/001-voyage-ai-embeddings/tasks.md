# Tasks: Voyage AI Vector Embedding Provider

**Input**: Design documents from `/specs/001-voyage-ai-embeddings/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md

**Tests**: Tests are included — the spec (FR-010) explicitly requires test coverage matching existing provider tests.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

---

## Phase 1: Setup

**Purpose**: Add the Voyage AI SDK as an optional dependency

- [x] T001 Add `voyage-embeddings` optional dependency group with `voyageai>=0.3.0` in `pyproject.toml`
- [x] T002 Run `uv sync` to verify dependency resolution succeeds

**Checkpoint**: Project builds cleanly with the new optional dependency group available

---

## Phase 2: Foundational

**Purpose**: No foundational/blocking tasks needed. The existing `EmbeddingProvider` ABC, `EmbeddingStore`, and `get_provider()` dispatch are already in place. All user stories can build directly on the existing infrastructure.

**Checkpoint**: N/A — existing infrastructure is sufficient

---

## Phase 3: User Story 1 - Use Voyage AI as the Embedding Provider (Priority: P1) MVP

**Goal**: A developer can select "voyage" as the provider, embed code nodes via Voyage AI's API, and get ranked semantic search results.

**Independent Test**: Configure with `VOYAGEAI_API_KEY`, embed nodes, run `semantic_search_nodes` and verify results.

### Tests for User Story 1

- [x] T003 [P] [US1] Add `TestVoyageAIEmbeddingProvider.test_name` — verify `name` property returns `"voyageai:voyage-code-3"` in `tests/test_embeddings.py`
- [x] T004 [P] [US1] Add `TestVoyageAIEmbeddingProvider.test_dimension` — verify `dimension` property returns 1024 in `tests/test_embeddings.py`
- [x] T005 [P] [US1] Add `TestVoyageAIEmbeddingProvider.test_embed_calls_api_with_document_type` — mock `voyageai.Client.embed`, verify batch embedding with `input_type="document"` in `tests/test_embeddings.py`
- [x] T006 [P] [US1] Add `TestVoyageAIEmbeddingProvider.test_embed_query_calls_api_with_query_type` — mock SDK, verify single query with `input_type="query"` in `tests/test_embeddings.py`
- [x] T007 [P] [US1] Add `TestGetProviderVoyage.test_get_provider_voyage_with_key` — verify `get_provider("voyage")` returns `VoyageAIEmbeddingProvider` when `VOYAGEAI_API_KEY` is set in `tests/test_embeddings.py`

### Implementation for User Story 1

- [x] T008 [US1] Implement `VoyageAIEmbeddingProvider` class in `code_review_graph/embeddings.py` — constructor with `api_key` and `model` params, lazy `voyageai` import, `embed()` with batch size 128 and `input_type="document"`, `embed_query()` with `input_type="query"`, `dimension` property (1024), `name` property (`"voyageai:{model}"`)
- [x] T009 [US1] Add `provider == "voyage"` branch to `get_provider()` in `code_review_graph/embeddings.py` — read `VOYAGEAI_API_KEY` env var, raise `ValueError` if missing, return `VoyageAIEmbeddingProvider` with optional model passthrough, wrap in try/except ImportError
- [x] T010 [US1] Run tests T003-T007 and verify they pass: `uv run pytest tests/test_embeddings.py -k "VoyageAI or voyage" -v`

**Checkpoint**: Voyage AI provider works end-to-end — initialization, batch embedding, query embedding, and provider dispatch all functional

---

## Phase 4: User Story 2 - Graceful Error Handling (Priority: P2)

**Goal**: Clear errors for missing API key, exponential backoff retry on transient API failures, immediate propagation of permanent errors.

**Independent Test**: Omit API key and verify ValueError; mock transient errors (429/500/503) and verify retry; mock permanent error and verify no retry.

### Tests for User Story 2

- [x] T011 [P] [US2] Add `TestGetProviderVoyage.test_get_provider_voyage_without_key_raises` — verify `ValueError` with `"VOYAGEAI_API_KEY"` message when env var is missing in `tests/test_embeddings.py`
- [x] T012 [P] [US2] Add `TestVoyageAIEmbeddingProvider.test_retry_on_transient_error` — mock SDK to raise error with "429" on first call then succeed, verify retry behavior in `tests/test_embeddings.py`
- [x] T013 [P] [US2] Add `TestVoyageAIEmbeddingProvider.test_permanent_error_raises_immediately` — mock SDK to raise non-retryable error, verify no retry in `tests/test_embeddings.py`

### Implementation for User Story 2

- [x] T014 [US2] Add retry logic to `VoyageAIEmbeddingProvider` in `code_review_graph/embeddings.py` — exponential backoff (3 retries, `2 ** attempt` wait), retry on "429"/"500"/"503" in error string, matching existing `GoogleEmbeddingProvider._call_with_retry` pattern
- [x] T015 [US2] Run tests T011-T013 and verify they pass: `uv run pytest tests/test_embeddings.py -k "voyage" -v`

**Checkpoint**: Error handling matches existing providers — clear messages for config errors, resilient to transient API failures

---

## Phase 5: User Story 3 - Optional Dependency Install (Priority: P3)

**Goal**: The `voyageai` package is only needed when using the Voyage AI provider; missing SDK raises a clear install instruction.

**Independent Test**: Without `voyageai` installed, selecting "voyage" provider raises ImportError with install instructions.

### Tests for User Story 3

- [x] T016 [P] [US3] Add `TestVoyageAIEmbeddingProvider.test_import_error_without_sdk` — mock `import voyageai` to raise `ImportError`, verify error message includes `"pip install code-review-graph[voyage-embeddings]"` in `tests/test_embeddings.py`
- [x] T017 [P] [US3] Add `TestGetProviderVoyage.test_get_provider_voyage_missing_sdk_returns_none` — verify `get_provider("voyage")` returns `None` when SDK import fails in `tests/test_embeddings.py`

### Implementation for User Story 3

- [x] T018 [US3] Verify the ImportError handling in `VoyageAIEmbeddingProvider.__init__` raises clear message with install instructions in `code_review_graph/embeddings.py` (likely already done in T008, validate here)
- [x] T019 [US3] Run tests T016-T017 and verify they pass: `uv run pytest tests/test_embeddings.py -k "import_error or missing_sdk" -v`

**Checkpoint**: Clean dependency isolation — base install unaffected, clear guidance when SDK is missing

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: CI validation and regression check

- [x] T020 Run full test suite to verify zero regressions: `uv run pytest tests/ --tb=short -q`
- [x] T021 Run linter: `uv run ruff check code_review_graph/`
- [x] T022 Run type checker: `uv run mypy code_review_graph/ --ignore-missing-imports --no-strict-optional`
- [x] T023 Verify `uv run code-review-graph status` still works (smoke test)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: N/A — no foundational tasks needed
- **User Story 1 (Phase 3)**: Depends on Phase 1 (T001-T002)
- **User Story 2 (Phase 4)**: Depends on Phase 3 (builds on VoyageAIEmbeddingProvider from T008)
- **User Story 3 (Phase 5)**: Depends on Phase 3 (tests the ImportError path from T008)
- **Polish (Phase 6)**: Depends on all user stories complete

### User Story Dependencies

- **User Story 1 (P1)**: Independent after Setup. Core provider implementation.
- **User Story 2 (P2)**: Builds on US1's provider class to add retry logic. Could be merged into US1 since it modifies the same class.
- **User Story 3 (P3)**: Tests the ImportError path already created in US1. Primarily validation.

### Within Each User Story

- Tests written first (T003-T007, T011-T013, T016-T017)
- Implementation follows (T008-T009, T014, T018)
- Verification last (T010, T015, T019)

### Parallel Opportunities

- All test tasks within a phase marked [P] can be written in parallel (same file but different test classes/methods)
- T003, T004, T005, T006, T007 can all be written together
- T011, T012, T013 can all be written together
- T016, T017 can be written together
- T020, T021, T022, T023 are independent and can run in parallel

---

## Parallel Example: User Story 1

```bash
# Write all US1 tests in parallel (all in tests/test_embeddings.py, different methods):
T003: test_name
T004: test_dimension
T005: test_embed_calls_api_with_document_type
T006: test_embed_query_calls_api_with_query_type
T007: test_get_provider_voyage_with_key

# Then implement sequentially (same file, depends on test definitions):
T008: VoyageAIEmbeddingProvider class
T009: get_provider() dispatch update
T010: Run and verify tests pass
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Add dependency to pyproject.toml
2. Complete Phase 3: Implement provider + dispatch + tests
3. **STOP and VALIDATE**: Run `uv run pytest tests/test_embeddings.py -k "voyage" -v`
4. At this point, Voyage AI is fully functional

### Incremental Delivery

1. Phase 1 → dependency available
2. Phase 3 (US1) → core provider works (MVP!)
3. Phase 4 (US2) → retry/error handling hardened
4. Phase 5 (US3) → ImportError path validated
5. Phase 6 → full CI validation, ready for PR

---

## Notes

- All implementation happens in 3 files: `embeddings.py`, `pyproject.toml`, `test_embeddings.py`
- US2 and US3 are refinements of US1 — they add error handling and dependency isolation tests to the same code
- The provider integrates automatically with `EmbeddingStore`, `embed_graph_tool`, `semantic_search_nodes`, and hybrid search via `get_provider()` — no changes needed in those modules
- Follow CONTRIBUTING.md: feature branch, all tests pass, ruff lint passes, submit PR
