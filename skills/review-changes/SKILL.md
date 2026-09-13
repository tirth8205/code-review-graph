---
name: review-changes
description: Perform a structured code review using change detection and impact
---

## Review Changes

Perform a thorough, risk-aware code review using the knowledge graph.

### Steps

Choose the steps that resolve the task; skip known context or unavailable tools.

1. Run `detect_changes_tool` to get risk-scored change analysis.
2. Run `get_affected_flows_tool` to find impacted execution paths.
3. For each high-risk function, run `query_graph_tool` with pattern="tests_for" to check test coverage.
4. Run `get_impact_radius_tool` to understand the blast radius.
5. For any untested changes, suggest specific test cases.

### Output Format

Provide findings grouped by risk level (high/medium/low) with:
- What changed and why it matters
- Test coverage status
- Suggested improvements
- Overall merge recommendation

## Token Efficiency Rules
- Use `get_minimal_context_tool(task="<your task>")` when you need a starting map; skip it for a known target.
- Prefer `detail_level="minimal"` where supported; expand the specific results needed for the task.
- Let evidence determine tool calls and context size; do not stop source inspection to meet a token or call target.
- Read the implementation and its tests before changing code. The graph narrows scope; it does not replace the source.
