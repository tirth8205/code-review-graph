---
name: code-review-graph-review
description: >-
  Use when performing code review. Risk-scored analysis via detect_changes and
  get_affected_flows. Replaces manual file-by-file reading with graph-powered impact analysis.
version: 1.0.0
---

<!-- code-review-graph-review v1.0.0 | lifecycle: active -->

# Review Changes

Perform a structured, risk-aware code review using the knowledge graph.

## Workflow

1. Run `detect_changes` to get risk-scored change analysis (high/medium/low risk per function).
2. Run `get_affected_flows` to find impacted execution paths.
3. For each high-risk function, run `query_graph` with `pattern="tests_for"` to check test coverage.
4. Run `get_impact_radius` to understand the blast radius of the change.
5. For untested changes, suggest specific test cases based on the call graph.

## Output Format

Group findings by risk level (high/medium/low):
- What changed and why it matters
- Test coverage status
- Suggested improvements
- Overall merge recommendation

## Token Efficiency Rules

- ALWAYS start with `get_minimal_context(task="code review")` before any other graph tool.
- Use `detail_level="minimal"` on all calls.
- Target: complete any review in ≤5 tool calls and ≤800 total output tokens.
- Use `get_review_context` for token-efficient source snippets instead of Read.
