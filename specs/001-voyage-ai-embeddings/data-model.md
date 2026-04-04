# Data Model: Voyage AI Embedding Provider

## No New Data Entities

This feature adds no new database tables or schema changes. The existing `embeddings` table already supports multiple providers via the `provider` column:

```sql
CREATE TABLE IF NOT EXISTS embeddings (
    qualified_name TEXT PRIMARY KEY,
    vector BLOB NOT NULL,
    text_hash TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'unknown'
);
```

Voyage AI embeddings will be stored as:
- `vector`: 1024 floats packed as binary (4096 bytes per vector)
- `provider`: `"voyageai:voyage-code-3"` (or user-specified model)
- `text_hash`: SHA-256 of the node text representation (unchanged)

## Provider Switching Behavior

When a user switches from another provider to Voyage AI (or vice versa), the existing `embed_nodes()` logic in `EmbeddingStore` automatically re-embeds nodes where the stored `provider` doesn't match the current provider. No migration needed.
