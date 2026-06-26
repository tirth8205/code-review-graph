# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 2.3.x   | Yes       |
| < 2.3   | No        |

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly:

1. **Do NOT open a public GitHub issue**
2. Use [GitHub private vulnerability reporting](https://github.com/tirth8205/code-review-graph/security/advisories/new)
   — the "Report a vulnerability" button under the repository's **Security** tab. This is the
   canonical reporting channel.
3. Include:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

We will acknowledge receipt within 48 hours and aim to release a fix within 7 days for critical issues.

## Security Model

### Threat Surface

code-review-graph is a **local development tool**. It:
- Runs as a local MCP server via stdio or localhost-bound streamable HTTP
- Stores data in a local SQLite database (`.code-review-graph/graph.db`)
- Makes no network calls during normal graph and review workflows
- Only reads source files within the validated repository root

### Mitigations

| Vector | Mitigation |
|--------|------------|
| SQL Injection | All queries use parameterized `?` placeholders |
| Path Traversal | `_validate_repo_root()` requires `.git`, `.svn`, or `.code-review-graph` directory |
| Prompt Injection | `_sanitize_name()` strips control characters, caps at 256 chars |
| XSS (visualization) | `escH()` escapes HTML entities; `</script>` escaped in JSON |
| Subprocess Injection | No `shell=True`; all git commands use list arguments |
| Supply Chain | Dependencies pinned with upper bounds; `uv.lock` has SHA256 hashes |
| CDN Tampering | D3.js loaded with Subresource Integrity (SRI) hash |
| API Key Leakage | Cloud embedding credentials are loaded from environment/configuration only, never hardcoded |

### Optional Network Calls

- **Cloud embeddings**: Only when explicitly configured with OpenAI-compatible, Google Gemini, or MiniMax providers. Cloud providers emit an egress warning unless `CRG_ACCEPT_CLOUD_EMBEDDINGS=1` is set.
- **Local embeddings model download**: One-time download from HuggingFace on first use of `sentence-transformers`
- **D3.js CDN**: Visualization HTML loads D3.js v7 from `d3js.org` (with SRI verification)

## Security Scanning

The CI pipeline runs:
- **Bandit** security scanner on every PR
- **Ruff** linter for code quality
- **mypy** type checker

Bandit exemptions are documented in `pyproject.toml` with justifications for each skip.
