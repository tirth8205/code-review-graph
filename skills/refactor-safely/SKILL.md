---
name: refactor-safely
description: Plan and execute safe refactoring using dependency analysis
---

## Refactor Safely

Plan a refactor from the dependency graph and apply renames from a preview.

### Steps

1. Call `refactor_tool` with `mode="suggest"` for refactoring candidates, or `mode="dead_code"` for unreferenced code.
2. For a rename, call `refactor_tool` with `mode="rename"`, `old_name` and `new_name`. Check the returned edit list before applying.
3. Call `apply_refactor_tool` with the returned `refactor_id` to apply the rename.
4. Before a large refactor, call `get_impact_radius_tool` and `get_affected_flows_tool` to see the dependents and critical paths involved.
5. Call `find_large_functions_tool` to find functions worth splitting.
6. After the change, call `detect_changes_tool` to confirm the impact matches the plan.

## Token Efficiency Rules
- Call `get_minimal_context_tool(task="<your task>")` before any other graph tool.
- Pass `detail_level="minimal"` wherever a tool accepts it. Use "standard" only when minimal is not enough.
- Prefer a targeted `query_graph_tool` call over a broad listing call.
- Budget: about five tool calls and 800 tokens of graph output per task.
- Read the implementation and its tests before changing code. The graph narrows scope; it does not replace the source.
