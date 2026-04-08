---
name: code-review-graph-debug
description: >-
  Use when debugging issues. Systematically traces call chains via callers_of,
  callees_of, and get_flow instead of grepping through files.
version: 1.0.0
---

<!-- code-review-graph-debug v1.0.0 | lifecycle: active -->

# Debug Issue

Systematically debug issues using graph-powered code navigation.

## Workflow

1. Use `semantic_search_nodes` to find code related to the issue by keyword.
2. Use `query_graph` with `callers_of` and `callees_of` to trace call chains.
3. Use `get_flow` to see full execution paths through suspected areas.
4. Run `detect_changes` to check if recent changes caused the issue.
5. Use `get_impact_radius` on suspected files to see what else may be affected.

## Token Efficiency Rules

- ALWAYS start with `get_minimal_context(task="debug <issue>")` before any other graph tool.
- Use `detail_level="minimal"` on all calls.
- Target: complete any debug session in ≤5 tool calls and ≤800 total output tokens.

## Tips

- Check both callers and callees to understand the full context of a function.
- Look at affected flows to find the entry point that triggers the bug.
- Recent changes (detect_changes) are the most common source of new issues.
