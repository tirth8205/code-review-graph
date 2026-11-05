---
name: explore-codebase
description: Navigate and understand codebase structure using the knowledge graph
---

## Explore Codebase

Use the code-review-graph MCP tools to find your way around the codebase.

### Steps

1. Call `get_architecture_overview_tool` for the community structure. Call `list_communities_tool`, then `get_community_tool`, only for the modules you need.
2. Call `semantic_search_nodes_tool` to find a function or class by name or keyword.
3. Call `query_graph_tool` with `callers_of`, `callees_of` or `imports_of` to trace relationships. `children_of` on a file lists its functions and classes.
4. Call `list_flows_tool`, then `get_flow_tool` for one flow, to follow an execution path.
5. Call `find_large_functions_tool` to find oversized functions.
6. Call `list_graph_stats_tool` only when you need node, edge and language counts.

## Token Efficiency Rules
- Call `get_minimal_context_tool(task="<your task>")` before any other graph tool.
- Pass `detail_level="minimal"` wherever a tool accepts it. Use "standard" only when minimal is not enough.
- Prefer a targeted `query_graph_tool` call over a broad listing call.
- Budget: about five tool calls and 800 tokens of graph output per task.
- Read the implementation and its tests before changing code. The graph narrows scope; it does not replace the source.
