# Implementation Plan: Voyage AI Vector Embedding Provider

**Branch**: `001-voyage-ai-embeddings` | **Date**: 2026-04-04 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/001-voyage-ai-embeddings/spec.md`

## Summary

Add Voyage AI as a fourth vector embedding provider for semantic code search, following the exact same patterns as the existing Google Gemini and MiniMax providers. The `voyageai` Python SDK will be used with the `voyage-code-3` model (1024 dimensions, optimized for code). Implementation touches 3 files: `embeddings.py` (new provider class + dispatch), `pyproject.toml` (optional dependency group), and `tests/test_embeddings.py` (unit tests).

## Technical Context

**Language/Version**: Python 3.10+  
**Primary Dependencies**: `voyageai` SDK (new optional), `fastmcp`, `tree-sitter`, `networkx`  
**Storage**: SQLite (existing `embeddings` table, no schema changes needed)  
**Testing**: pytest with unittest.mock (matching existing test patterns)  
**Target Platform**: Cross-platform (macOS, Linux, Windows)  
**Project Type**: Library / CLI / MCP server  
**Performance Goals**: N/A (embedding is a batch offline operation)  
**Constraints**: Must not add `voyageai` as a required dependency; optional install only  
**Scale/Scope**: Single new provider class (~80 lines), dispatch update (~10 lines), tests (~100 lines)

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Constitution is not configured for this project (template only). No gates to evaluate. Proceeding based on project conventions from CLAUDE.md and CONTRIBUTING.md:

- [x] Line length: 100 chars (ruff)
- [x] Python target: 3.10+
- [x] SQL: parameterized queries (no SQL changes needed)
- [x] Security: no eval/exec/pickle, API key from env var only
- [x] Tests: follow existing patterns, maintain 50% coverage minimum
- [x] Lint: ruff check passes
- [x] Type-check: mypy passes

## Project Structure

### Documentation (this feature)

```text
specs/001-voyage-ai-embeddings/
├── plan.md              # This file
├── spec.md              # Feature specification
├── research.md          # Phase 0: SDK research findings
├── data-model.md        # Phase 1: No new data entities
├── quickstart.md        # Phase 1: Usage guide
└── checklists/
    └── requirements.md  # Spec quality checklist
```

### Source Code (files to modify)

```text
code_review_graph/
└── embeddings.py          # Add VoyageAIEmbeddingProvider class + update get_provider()

tests/
└── test_embeddings.py     # Add TestVoyageAIEmbeddingProvider + TestGetProviderVoyage

pyproject.toml             # Add voyage-embeddings optional dependency group
```

**Structure Decision**: No new files or directories needed. The provider plugs directly into the existing `embeddings.py` module following the established pattern. No changes to `EmbeddingStore`, MCP tools, CLI, or search — they all work generically via `get_provider()`.

## Complexity Tracking

No violations. This is a straightforward addition following established patterns.

## Implementation Details

### VoyageAIEmbeddingProvider Class

**Location**: `code_review_graph/embeddings.py` (after `MiniMaxEmbeddingProvider`, before `get_provider()`)

**Pattern**: Follows GoogleEmbeddingProvider structure exactly:
- Constructor: accepts `api_key: str`, optional `model: str` (default `"voyage-code-3"`)
- Lazy import of `voyageai` SDK with ImportError → clear install instructions
- `embed()`: batch processing (128 items per API call), `input_type="document"`
- `embed_query()`: single text, `input_type="query"`
- `dimension`: 1024 (static for voyage-code-3; could be model-dependent)
- `name`: `f"voyageai:{self.model}"`
- Retry: exponential backoff (3 retries, checking for "429"/"500"/"503" in error string)

### get_provider() Update

**Location**: `code_review_graph/embeddings.py`, in `get_provider()` function

Add before the Google provider block:
```
if provider == "voyage":
    api_key = os.environ.get("VOYAGEAI_API_KEY")
    if not api_key:
        raise ValueError("VOYAGEAI_API_KEY environment variable is required...")
    try:
        return VoyageAIEmbeddingProvider(api_key=api_key, **({"model": model} if model else {}))
    except ImportError:
        return None
```

### pyproject.toml Update

Add new optional dependency group:
```toml
voyage-embeddings = [
    "voyageai>=0.3.0",
]
```

### Test Coverage

**Location**: `tests/test_embeddings.py`

Two new test classes following existing patterns:
1. **TestVoyageAIEmbeddingProvider**: name, dimension, embed (mock SDK), embed_query (mock SDK), retry on transient error, error propagation on permanent error
2. **TestGetProviderVoyage**: with key → returns provider, without key → raises ValueError, missing SDK → returns None
