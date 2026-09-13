---
name: debug-issue
description: Systematically debug issues using graph-powered code navigation
---

## Debug Issue

Use the knowledge graph to systematically trace and debug issues.

### Steps

Choose the steps that resolve the task; skip known context or unavailable tools.

1. Use `semantic_search_nodes_tool` to find code related to the issue.
2. Use `query_graph_tool` with `callers_of` and `callees_of` to trace call chains.
3. Use `get_flow_tool` to see full execution paths through suspected areas.
4. Run `detect_changes_tool` to check if recent changes caused the issue.
5. Use `get_impact_radius_tool` on suspected files to see what else is affected.

### Tips

- Check both callers and callees to understand the full context.
- Look at affected flows to find the entry point that triggers the bug.
- Recent changes are the most common source of new issues.

## Token Efficiency Rules
- Use `get_minimal_context_tool(task="<your task>")` when you need a starting map; skip it for a known target.
- Prefer `detail_level="minimal"` where supported; expand the specific results needed for the task.
- Let evidence determine tool calls and context size; do not stop source inspection to meet a token or call target.
- Read the implementation and its tests before changing code. The graph narrows scope; it does not replace the source.
