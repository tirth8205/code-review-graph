---
name: build-graph
description: Build or update the code review knowledge graph. Run this first to initialise, or let hooks keep it updated automatically.
argument-hint: "[full]"
---

# Build Graph

Start with `get_minimal_context_tool` to identify the graph state. Check `code-review-graph status` against the checkout's current Git HEAD. Use `code-review-graph doctor` to diagnose an unusable graph.

- For a missing graph, call `build_or_update_graph_tool(full_rebuild=True)`.
- For an existing graph, call `build_or_update_graph_tool()` to refresh it.
- If the operation reports contention, confirm the current writer has finished and verify graph freshness before relying on its results.
- If the operation fails or reports a partial build, inspect its diagnostic before using graph-backed results.
- If the last update skipped post-processing, call `run_postprocess_tool` before querying flows or communities.
- Use `list_repos_tool` to identify registered repositories before using `cross_repo_search_tool`.

For follow-up graph queries, use `detail_level="minimal"` when source snippets are unnecessary. Read the implementation and its tests before changing code.
