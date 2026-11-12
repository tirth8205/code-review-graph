---
name: review-changes
description: Perform a structured code review using change detection and impact
---

## Review Changes

Review a change set with risk scores and blast radius from the knowledge graph.

### Steps

1. Call `detect_changes_tool` for risk-scored changed functions, test gaps and affected flows.
2. Call `get_affected_flows_tool(detail_level="standard")` only when you need the steps of an affected flow; `minimal` returns no steps.
3. For each high-risk function, call `query_graph_tool` with `pattern="tests_for"` to check test coverage.
4. Call `get_impact_radius_tool` when the blast radius is not clear from step 1.
5. Suggest specific test cases for untested changes.

### Output Format

Group findings by risk level (high, medium, low). For each finding give what changed and why it matters, its test coverage, and the suggested fix. End with a merge recommendation.

## Token Efficiency Rules
- Call `get_minimal_context_tool(task="<your task>")` before any other graph tool.
- Pass `detail_level="minimal"` unless it is not enough. Minimal drops source, so leave `semantic_search_nodes_tool(include_source=True)` and `get_review_context_tool` at standard.
- Prefer a targeted `query_graph_tool` call over a broad listing call.
- Budget: about five tool calls per task. Source snippets stand in for file reads.
- Read the implementation and its tests before changing code. The graph narrows scope; it does not replace the source.
