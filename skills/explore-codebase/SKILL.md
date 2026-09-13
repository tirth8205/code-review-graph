---
name: explore-codebase
description: Navigate and understand codebase structure using the knowledge graph
---

## Explore Codebase

Use the code-review-graph MCP tools to explore and understand the codebase.

### Steps

Choose the steps that resolve the task; skip known context or unavailable tools.

1. Run `list_graph_stats_tool` to see overall codebase metrics.
2. Run `get_architecture_overview_tool` for high-level community structure.
3. Use `list_communities_tool` to find major modules, then `get_community_tool` for details.
4. Use `semantic_search_nodes_tool` to find specific functions or classes.
5. Use `query_graph_tool` with patterns like `callers_of`, `callees_of`, `imports_of` to trace relationships.
6. Use `list_flows_tool` and `get_flow_tool` to understand execution paths.

### Tips

- Start broad (stats, architecture) then narrow down to specific areas.
- Use `children_of` on a file to see all its functions and classes.
- Use `find_large_functions_tool` to identify complex code.

## Token Efficiency Rules
- Use `get_minimal_context_tool(task="<your task>")` when you need a starting map; skip it for a known target.
- Prefer `detail_level="minimal"` where supported; expand the specific results needed for the task.
- Let evidence determine tool calls and context size; do not stop source inspection to meet a token or call target.
- Read the implementation and its tests before changing code. The graph narrows scope; it does not replace the source.
