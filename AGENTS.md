# AGENTS.md

## code-review-graph

This repository ships an MCP server and graph tooling for token-efficient code review.

### Working Rules

- Prefer the `code-review-graph` MCP tools before broad file scanning.
- Start with `get_minimal_context` for graph-aware exploration or review tasks.
- Use `detail_level="minimal"` first, then escalate only when needed.
- Keep reviews focused on bugs, regressions, risk, and missing tests.

### Local Commands

```bash
uv run code-review-graph build
uv run code-review-graph update
uv run pytest
```

### Key Paths

- `code_review_graph/main.py`: MCP server entrypoint
- `code_review_graph/tools/`: tool implementations
- `code_review_graph/skills.py`: platform install helpers and instruction injection
- `tests/test_tools.py`: MCP tool integration tests
- `tests/test_skills.py`: platform install and instruction tests
