# Legal and Privacy

**Licence:** MIT. See [LICENSE](../LICENSE) in the project root; `pyproject.toml` declares
the same licence.

**Privacy**

- No telemetry.
- Graph data is stored locally, by default in `.code-review-graph/graph.db`.
- Graph build, review, search and the CLI/MCP workflows run on your machine.
- The optional local embedding provider downloads a sentence-transformers model from
  Hugging Face on first use.
- The optional cloud embedding providers (`openai`, `google`, `minimax`, `voyage`) send the
  text being embedded to that provider, and only when you select one. They print an egress
  warning unless `CRG_ACCEPT_CLOUD_EMBEDDINGS=1` is set.
- The Streamable HTTP MCP transport (`serve --http`) binds to localhost and validates the
  `Host` and `Origin` headers.

**Warranty:** provided as is, without warranty of any kind, as stated in the MIT licence.
