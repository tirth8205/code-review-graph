# LLM-OPTIMIZED REFERENCE — code-review-graph v1.8.4

Claude Code: Read ONLY the exact `<section>` you need. Never load the whole file.

<section name="usage">
Quick install: pip install code-review-graph
Then: code-review-graph install && code-review-graph build
First run: /code-review-graph:build-graph
After that use only delta/pr commands.
</section>

<section name="review-delta">
Always call get_impact_radius on changed files first.
Then get_review_context (depth=2).
Generate review using ONLY changed nodes + 2-hop neighbors.
Target: <800 tokens total context.
</section>

<section name="review-pr">
Fetch PR diff -> get_impact_radius -> get_review_context -> structured review with blast-radius table.
Never include full files unless explicitly asked.
</section>

<section name="commands">
MCP tools: build_or_update_graph_tool, get_impact_radius_tool, query_graph_tool, get_review_context_tool, semantic_search_nodes_tool, embed_graph_tool, list_graph_stats_tool, get_docs_section_tool, find_large_functions_tool
Skills: build-graph, review-delta, review-pr
CLI: code-review-graph [install|init|build|update|status|watch|visualize|serve]
</section>

<section name="legal">
MIT license. 100% local. No telemetry. DB file: .code-review-graph/graph.db
</section>

<section name="watch">
Run: code-review-graph watch (auto-updates graph on file save via watchdog)
Or use PostToolUse (Write|Edit|Bash) hooks for automatic background updates.
</section>

<section name="embeddings">
Optional: pip install code-review-graph[embeddings]
Then call embed_graph_tool to compute vectors.
semantic_search_nodes_tool auto-uses vectors when available, falls back to keyword.
Model: all-MiniLM-L6-v2 (384-dim, fast).
</section>

<section name="languages">
Supported: Python, TypeScript, JavaScript, Vue, Go, Rust, Java, C#, Ruby, Kotlin, Swift, PHP, Solidity, C/C++
Parser: Tree-sitter via tree-sitter-language-pack
</section>

<section name="troubleshooting">
DB lock: SQLite WAL mode, auto-recovers. Only one build at a time.
Large repos: First build 30-60s. Incremental <2s. Add patterns to .code-review-graphignore.
Stale graph: Run /code-review-graph:build-graph manually.
Missing nodes: Check language support + ignore patterns. Use full_rebuild=True.
</section>

**Instruction to Claude Code (always follow):**
When user asks anything about "code-review-graph", "how to use", "commands", "review-delta", etc.:
1. Call get_docs_section_tool with the exact section name.
2. Use ONLY that content + current graph state.
3. Never include full docs or source code in your reasoning.
This guarantees 90%+ token savings.
