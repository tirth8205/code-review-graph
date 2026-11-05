---
name: debug-issue
description: Systematically debug issues using graph-powered code navigation
---

## Debug Issue

Trace a bug through the knowledge graph before reading source.

### Steps

1. Call `semantic_search_nodes_tool` to find code related to the issue.
2. Call `query_graph_tool` with `callers_of` and `callees_of` to trace the call chain in both directions.
3. Call `get_flow_tool` for the execution path that reaches the suspect code. Its entry point is where the bug is triggered.
4. Call `detect_changes_tool` to check whether a recent change caused the issue.
5. Call `get_impact_radius_tool` on the suspect files to see what a fix would affect.

## Token Efficiency Rules
- Call `get_minimal_context_tool(task="<your task>")` before any other graph tool.
- Pass `detail_level="minimal"` wherever a tool accepts it. Use "standard" only when minimal is not enough.
- Prefer a targeted `query_graph_tool` call over a broad listing call.
- Budget: about five tool calls and 800 tokens of graph output per task.
- Read the implementation and its tests before changing code. The graph narrows scope; it does not replace the source.
