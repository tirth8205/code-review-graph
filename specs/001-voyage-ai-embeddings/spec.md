# Feature Specification: Voyage AI Vector Embedding Provider

**Feature Branch**: `001-voyage-ai-embeddings`  
**Created**: 2026-04-04  
**Status**: Draft  
**Input**: User description: "Upgrade this project with Voyage AI supporting for vector embedding, following CONTRIBUTING.md for contributing to the original repo"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Use Voyage AI as the Embedding Provider (Priority: P1)

A developer who has a Voyage AI subscription wants to use Voyage AI's embedding models for semantic code search. They set their Voyage AI API key as an environment variable and configure the project to use the "voyage" provider. When they run the embedding pipeline, their code nodes are embedded using Voyage AI and semantic search returns accurate results.

**Why this priority**: This is the core value — enabling Voyage AI as an embedding option alongside the existing Local, Google Gemini, and MiniMax providers.

**Independent Test**: Can be fully tested by configuring the Voyage AI provider, embedding a set of code nodes, and verifying that semantic search returns relevant results ranked by similarity.

**Acceptance Scenarios**:

1. **Given** a user has a valid Voyage AI API key set as an environment variable, **When** they select "voyage" as the embedding provider, **Then** the system initializes the Voyage AI provider and is ready to embed nodes.
2. **Given** the Voyage AI provider is configured, **When** the user embeds code nodes, **Then** each node receives a vector embedding from Voyage AI's API and is stored for later search.
3. **Given** embedded nodes exist from Voyage AI, **When** the user performs a semantic search query, **Then** results are ranked by cosine similarity using the Voyage AI-generated vectors.

---

### User Story 2 - Graceful Handling When Voyage AI Is Unavailable (Priority: P2)

A developer attempts to use the Voyage AI provider without setting the required API key, or the Voyage AI API is temporarily unreachable. The system provides a clear error message for missing configuration, and retries transient API failures before surfacing the error.

**Why this priority**: Robust error handling ensures a smooth user experience and aligns with the existing provider pattern (Google and MiniMax both validate API keys and retry on transient errors).

**Independent Test**: Can be tested by omitting the API key and verifying the error message, and by simulating transient API errors to confirm retry behavior.

**Acceptance Scenarios**:

1. **Given** the Voyage AI API key environment variable is not set, **When** the user selects "voyage" as the provider, **Then** the system raises a clear error indicating the missing key.
2. **Given** a valid configuration, **When** the Voyage AI API returns a transient error (rate limit or server error), **Then** the system retries with exponential backoff before failing.
3. **Given** a valid configuration, **When** the Voyage AI API returns a permanent error, **Then** the system surfaces the error without retrying.

---

### User Story 3 - Install Voyage AI Dependencies (Priority: P3)

A developer wants to install only the dependencies needed for Voyage AI embeddings without pulling in unrelated embedding libraries. They install the project with a dedicated optional dependency group.

**Why this priority**: Keeps the base install lightweight. Users who don't use Voyage AI should not need its dependencies.

**Independent Test**: Can be tested by installing the optional dependency group and verifying the Voyage AI provider loads successfully, and by confirming the provider raises an import error when the dependency is missing.

**Acceptance Scenarios**:

1. **Given** the user installs the project with the Voyage AI optional dependencies, **When** they select the "voyage" provider, **Then** the provider initializes without import errors.
2. **Given** the user has not installed the Voyage AI optional dependencies, **When** they select the "voyage" provider, **Then** the system raises a clear error with installation instructions.

---

### Edge Cases

- What happens when the Voyage AI API key is set but invalid (authentication failure)? The system should surface the API authentication error clearly.
- How does the system handle embedding requests that exceed Voyage AI's batch size limits? The system should chunk requests into batches, consistent with existing providers.
- What happens when switching providers (e.g., from Google to Voyage AI) with existing embeddings from a different provider already stored? The existing provider-tagging mechanism re-embeds nodes when the provider changes.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST support "voyage" as a valid provider name in the embedding provider selection.
- **FR-002**: System MUST require a Voyage AI API key via an environment variable before initializing the provider.
- **FR-003**: System MUST embed text inputs by calling the Voyage AI embeddings API and returning float vectors.
- **FR-004**: System MUST support separate embedding operations for indexing (batch document embedding) and querying (single query embedding).
- **FR-005**: System MUST retry transient API errors (rate limits, server errors) with exponential backoff, consistent with existing provider behavior.
- **FR-006**: System MUST batch large embedding requests to stay within API limits.
- **FR-007**: System MUST provide the Voyage AI dependencies as an optional install group so the base package remains lightweight.
- **FR-008**: System MUST raise a clear, actionable error when the Voyage AI dependency package is not installed.
- **FR-009**: System MUST correctly report its vector dimension and provider name for storage and retrieval consistency.
- **FR-010**: System MUST include tests for the new provider following the existing test patterns in the project's test suite.

### Key Entities

- **VoyageAIEmbeddingProvider**: A new embedding provider that implements the existing `EmbeddingProvider` interface, responsible for communicating with the Voyage AI API.
- **Environment Configuration**: The API key (`VOYAGEAI_API_KEY`) used to authenticate with Voyage AI's service.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Users can configure and use Voyage AI as an embedding provider with no more setup steps than existing providers (set env var, select provider).
- **SC-002**: Semantic search using Voyage AI embeddings returns relevant code nodes for standard queries (matching the quality bar of existing providers).
- **SC-003**: All existing tests continue to pass after the addition (zero regressions).
- **SC-004**: The new provider includes test coverage consistent with the existing provider tests (covering initialization, embedding, search, error handling, and retry logic).
- **SC-005**: The contribution passes the project's CI pipeline (lint, type-check, security scan, tests with 50% minimum coverage).

## Assumptions

- The Voyage AI Python SDK (`voyageai`) is the standard client library for accessing Voyage AI's embedding API.
- The environment variable name for the API key follows the project's convention: `VOYAGEAI_API_KEY`.
- Voyage AI's default embedding model and dimensions are used unless the user specifies a custom model, consistent with how the Google provider handles model selection.
- The existing `EmbeddingProvider` abstract interface is sufficient — no changes to the base class are needed.
- The existing embedding storage (SQLite BLOBs with provider tagging) handles Voyage AI vectors without schema changes, since provider switching is already supported.
- The contribution targets the original upstream repository and follows the CONTRIBUTING.md guidelines (feature branch, tests, lint, PR).
