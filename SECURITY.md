# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 2.3.x   | Yes       |
| < 2.3   | No        |

## Reporting a Vulnerability

Do not open a public GitHub issue. Use [GitHub private vulnerability reporting](https://github.com/tirth8205/code-review-graph/security/advisories/new) (the "Report a vulnerability" button under the repository's Security tab). Include a description, steps to reproduce, the likely impact and a suggested fix if you have one.

We aim to acknowledge reports within 48 hours and to release a fix for critical issues within 7 days.

## Security Model

### Threat Surface

code-review-graph is a local development tool. It:

- runs as a local MCP server over stdio, or over Streamable HTTP bound to localhost with `serve --http` (`--host` can bind another address);
- stores data in a local SQLite database (`.code-review-graph/graph.db`);
- makes no network calls during graph builds, updates and reviews, unless you ask for cloud embeddings (see below);
- reads source files only under the validated repository root, plus the folders that symlinks in it point to when you pass `--follow-symlinks` or set `CRG_FOLLOW_SYMLINKS=1` (off by default).

### Mitigations

| Vector | Mitigation |
|--------|------------|
| SQL injection | All queries use parameterised `?` placeholders |
| Path traversal | `_validate_repo_root()` requires an existing directory containing `.git`, `.svn` or `.code-review-graph` |
| Multi-worktree routing | `serve --multi-worktree` permits only the primary checkout and active linked worktrees from the same Git common directory; each root is handled by a separate child server |
| Multi-repo routing | `serve --http --multi-repo` refuses a tool call whose `repo_root` is missing, relative or not in the registry, so it never answers from a repository the client did not name |
| Prompt injection | `_sanitize_name()` strips control characters and caps names at 256 characters |
| XSS (visualization) | `escH()` escapes HTML entities; `</script>` is escaped inside embedded JSON |
| Subprocess injection | No `shell=True`; git and svn are invoked with argument lists |
| DNS rebinding (`serve --http`) | `http_origin_guard.py` validates the Host and Origin headers when the server is bound to a loopback address, the default. Binding another address with `--host` turns this check off |
| Tool approval | Tools that only read carry the MCP hint `readOnlyHint: true`, so a client may run them without asking. The five that write (`build_or_update_graph_tool`, `run_postprocess_tool`, `embed_graph_tool`, `generate_wiki_tool`, `apply_refactor_tool`) carry `readOnlyHint: false` and `destructiveHint: true` |
| Supply chain | Dependency ranges have upper bounds, and CI tests the lowest version each range allows; `uv.lock` records SHA256 hashes |
| CDN tampering | D3.js is bundled and loaded with a Subresource Integrity hash; the CDN fallback carries the same hash |
| Remote model code | The local embedding model loads with `trust_remote_code` off; only `CRG_ALLOW_REMOTE_CODE=1` turns it on |
| API key leakage | Cloud embedding credentials are read from environment variables only |

### Optional Network Calls

- Cloud embeddings: only when an OpenAI-compatible, Google Gemini, MiniMax or Voyage AI provider is configured. Cloud providers print an egress warning unless `CRG_ACCEPT_CLOUD_EMBEDDINGS=1` is set.
- Local embedding model: `sentence-transformers` downloads the model from Hugging Face on first use.
- D3.js: the visualization HTML loads the D3 v7 copy shipped with the package (same origin, SRI-verified) and falls back to the `d3js.org` CDN (also SRI-verified, `crossorigin="anonymous"`) only if the local copy is unavailable.

## Security Scanning

CI runs bandit, ruff and mypy on every pull request. Bandit exemptions are listed in `pyproject.toml` with a reason for each.
