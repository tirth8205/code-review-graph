---
name: code-review-graph-explore
description: >-
  Use when navigating or understanding codebase structure. Uses code-review-graph
  MCP tools (list_graph_stats, get_architecture_overview, query_graph) instead of
  Grep/Glob/Read for token efficiency.
version: 1.0.0
---

<!-- code-review-graph-explore v1.0.0 | lifecycle: active -->

# Explore Codebase

Navigate and understand codebase structure using the code-review-graph knowledge graph.

## Workflow

1. Run `list_graph_stats` to see overall codebase metrics (files, nodes, edges).
2. Run `get_architecture_overview` for high-level community structure and coupling warnings.
3. Use `list_communities` to find major modules, then `get_community` for details.
4. Use `semantic_search_nodes` to find specific functions or classes by keyword.
5. Use `query_graph` with patterns like `callers_of`, `callees_of`, `imports_of`, `tests_for` to trace relationships.
6. Use `list_flows` and `get_flow` to understand execution paths.

## Token Efficiency Rules

- ALWAYS start with `get_minimal_context(task="<your task>")` before any other graph tool.
- Use `detail_level="minimal"` on all calls. Only escalate to "standard" when minimal is insufficient.
- Target: complete any explore task in ≤5 tool calls and ≤800 total output tokens.
- Use graph tools BEFORE Grep/Glob/Read — the graph is faster and cheaper.

## Tips

- Start broad (stats, architecture) then narrow down to specific areas.
- Use `children_of` on a file node to see all its functions and classes.
- Use `find_large_functions` to identify complex code worth reviewing.
