---
name: refactor-safely
description: Plan and execute safe refactoring using dependency analysis
---

## Refactor Safely

Use the knowledge graph to plan and execute refactoring with confidence.

### Steps

1. Use `refactor_tool` with mode="suggest" for community-driven refactoring suggestions.
2. Use `refactor_tool` with mode="dead_code" to find unreferenced code.
3. For renames, use `refactor_tool` with mode="rename" to preview all affected locations.
4. Use `apply_refactor_tool` with the refactor_id to apply renames.
5. After changes, run `detect_changes_tool` to verify the refactoring impact.

### Safety Checks

- Always preview before applying (rename mode gives you an edit list).
- Check `get_impact_radius_tool` before major refactors.
- Use `get_affected_flows_tool` to ensure no critical paths are broken.
- Run `find_large_functions_tool` to identify decomposition targets.

## Token Efficiency Rules
- Start with `get_minimal_context_tool(task="<your task>")` before other graph tools.
- Use `detail_level="minimal"` on all calls. Only escalate to "standard" when minimal is insufficient.
- Target: complete any review/debug/refactor task in ≤5 tool calls and ≤800 total output tokens.
- Read the implementation and its tests before changing code. The graph narrows scope; it does not replace the source.
