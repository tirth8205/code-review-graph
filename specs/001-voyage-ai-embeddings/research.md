# Research: Voyage AI Embedding Provider

## Decision 1: SDK vs Raw HTTP

**Decision**: Use the official `voyageai` Python SDK  
**Rationale**: The SDK handles authentication, request formatting, and response parsing cleanly. Unlike MiniMax (which uses raw urllib), Voyage AI has a well-maintained official SDK on PyPI. This matches the Google provider pattern which also uses an official SDK.  
**Alternatives considered**: Raw HTTP via `urllib.request` (like MiniMax). Rejected because the SDK is lightweight, well-supported, and simplifies error handling.

## Decision 2: Default Model

**Decision**: Use `voyage-code-3` as the default model  
**Rationale**: This is Voyage AI's code-specialized embedding model, optimized for code retrieval tasks. Since code-review-graph is a code analysis tool, this is the natural choice. Produces 1024-dimensional vectors.  
**Alternatives considered**: `voyage-3` (general-purpose, also 1024 dims). Could be used but lacks code-specific optimization. Users can override via the `model` parameter.

## Decision 3: API Key Environment Variable Name

**Decision**: `VOYAGEAI_API_KEY`  
**Rationale**: Follows the project's existing convention (`GOOGLE_API_KEY`, `MINIMAX_API_KEY`). The Voyage AI SDK itself defaults to reading `VOYAGE_API_KEY`, but we explicitly pass the key to the constructor for consistency with how the other providers work.  
**Alternatives considered**: `VOYAGE_API_KEY` (SDK default). Rejected to maintain naming consistency within this project.

## Decision 4: Batch Size

**Decision**: 128 items per API call  
**Rationale**: Voyage AI's API limit is 128 texts per request. The existing providers use 100, but Voyage AI documents 128 as the limit. Using 128 maximizes throughput.  
**Alternatives considered**: 100 (matching other providers). Using the documented limit is more efficient without risk.

## Decision 5: Provider Name in Dispatch

**Decision**: `provider == "voyage"` (not "voyageai")  
**Rationale**: Concise and matches the pattern of other providers ("google", "minimax", "local"). The full SDK name appears in the provider's `name` property (e.g., `"voyageai:voyage-code-3"`).  
**Alternatives considered**: `"voyageai"`. Longer, less consistent with the terse style of existing provider names.

## Decision 6: Input Type Differentiation

**Decision**: Use `input_type="document"` for `embed()` and `input_type="query"` for `embed_query()`  
**Rationale**: Voyage AI's API supports asymmetric embeddings — different vector representations for documents vs queries improve retrieval quality. This mirrors how Google uses `RETRIEVAL_DOCUMENT` vs `RETRIEVAL_QUERY` and MiniMax uses `"db"` vs `"query"`.  
**Alternatives considered**: Omitting input_type. Would work but sacrifices retrieval quality.

## Decision 7: Retry Pattern

**Decision**: Inline retry with exponential backoff (3 retries, 2^attempt seconds)  
**Rationale**: Exact match of the existing retry patterns in GoogleEmbeddingProvider and MiniMaxEmbeddingProvider. Checks for "429", "500", "503" in exception string.  
**Alternatives considered**: Using the Voyage AI SDK's built-in retry if available. Rejected for consistency with existing code patterns.

## Decision 8: Optional Dependency Group Name

**Decision**: `voyage-embeddings`  
**Rationale**: Follows existing naming: `embeddings` (local), `google-embeddings` (Google). Install via `pip install code-review-graph[voyage-embeddings]`.  
**Alternatives considered**: `voyageai-embeddings`. Rejected to match the terse provider name style.
