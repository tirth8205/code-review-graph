# Legal & Privacy

**License:** MIT (see [LICENSE](../LICENSE) in project root)

**Privacy:**
- Zero telemetry
- All graph data stored locally in `.code-review-graph/graph.db`
- Core graph build, review, search, and CLI/MCP workflows run locally
- Optional local embeddings may download a sentence-transformers model from HuggingFace when first used
- Optional cloud embedding providers (`openai`, `google`, `minimax`) send embedded source snippets to the configured provider only when explicitly selected
- Remote embedding providers print an egress warning unless `CRG_ACCEPT_CLOUD_EMBEDDINGS=1` is set
- Streamable HTTP MCP transport binds to localhost by default

**Data:** Core graph data stays on your machine. If you opt into a cloud embedding provider, the text being embedded leaves your machine under that provider's terms.

**Warranty:** Provided as-is, without warranty of any kind.
