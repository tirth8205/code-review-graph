---
name: refactor-safely
description: Plan and execute safe refactoring using dependency analysis
---

## Refactor Safely

Use the knowledge graph to plan and execute refactoring with confidence.

### Steps

Choose the steps that resolve the task; skip known context or unavailable tools.

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
- Use `get_minimal_context_tool(task="<your task>")` when you need a starting map; skip it for a known target.
- Prefer `detail_level="minimal"` where supported; expand the specific results needed for the task.
- Let evidence determine tool calls and context size; do not stop source inspection to meet a token or call target.
- Read the implementation and its tests before changing code. The graph narrows scope; it does not replace the source.
