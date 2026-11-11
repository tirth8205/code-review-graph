# Legal and Privacy

**Licence:** MIT. See [LICENSE](../LICENSE) in the project root; `pyproject.toml` declares
the same licence.

**Privacy**

- No telemetry.
- Graph data is stored locally, by default in `.code-review-graph/graph.db`.
- The context history log (`CRG_CONTEXT_HISTORY=1`, off by default) is a local file next to
  `graph.db` that holds token counts, tool names, a timestamp, a detail level and file and
  depth counts, never file names, paths or source text. It is never sent anywhere.
- Graph build, review, search and the CLI/MCP workflows run on your machine.
- The HTML graph from `visualize` loads its bundled copy of D3. Only if that copy cannot load
  does it fetch the same file, hash-checked, from the d3js.org CDN.
- The optional local embedding provider downloads a sentence-transformers model from
  Hugging Face on first use.
- The optional cloud embedding providers (`openai`, `google`, `minimax`, `voyage`) send the
  text being embedded to that provider, and only when you select one. `openai` is also picked
  when `CRG_OPENAI_API_KEY` and `CRG_OPENAI_BASE_URL` are both set and no provider is named.
  They print an egress warning unless `CRG_ACCEPT_CLOUD_EMBEDDINGS=1` is set or the `openai`
  or `voyage` base URL is localhost.
- The Streamable HTTP MCP transport (`serve --http`) binds to 127.0.0.1 by default and then
  checks the `Host` and `Origin` headers. If you pass `--host` with an address that is not
  localhost, the server is open to that network and those checks are off.

**Warranty:** provided as is, without warranty of any kind, as stated in the MIT licence.
