---
name: build-graph
description: Build or update the code review knowledge graph. Run this first to initialize, or let hooks keep it updated automatically.
argument-hint: "[full]"
---

# Build Graph

Build or incrementally update the knowledge graph for this repository.

## Steps

1. Call `list_graph_stats_tool`. If `last_updated` is null, the graph has never been built.
2. For a first build, or when the argument is `full`, call `build_or_update_graph_tool(full_rebuild=True)`. Otherwise call `build_or_update_graph_tool()` for an incremental update.
3. Report the response: `status` (`ok`, `partial` or `error`) and `summary`.

## When to Use

- First set-up of a repository, or after a branch switch or large refactor.
- When the graph looks stale. Hooks installed by `code-review-graph install` run an update after each edit and before each commit, so manual builds are rarely needed.

## Notes

- The database is `.code-review-graph/graph.db` in the repository root.
- Binary files, dependency and build directories, and patterns in `.code-review-graphignore` are skipped.
- For the list of supported languages call `get_docs_section_tool(section_name="languages")`.
