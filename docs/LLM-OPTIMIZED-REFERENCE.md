# LLM-OPTIMIZED REFERENCE -- code-review-graph v2.2.1

Claude Code: Read ONLY the exact `<section>` you need. Never load the whole file.

<section name="usage">
Quick install: pip install code-review-graph
Then: code-review-graph install && code-review-graph build
First run: /code-review-graph:build-graph
After that use only delta/pr commands.
ALWAYS start with get_minimal_context_tool(task="your task") — returns ~100 tokens with risk, communities, flows, and suggested next tools.
Use detail_level="minimal" on all subsequent calls unless you need more detail.
</section>

<section name="review-delta">
1. Call get_minimal_context_tool(task="review changes") first.
2. If risk is low: detect_changes_tool(detail_level="minimal") → report summary.
3. If risk is medium/high: detect_changes_tool(detail_level="standard") → expand on high-risk items.
Target: ≤5 tool calls, ≤800 tokens total context.
</section>

<section name="review-pr">
Fetch PR diff -> detect_changes_tool -> get_affected_flows_tool -> structured review with blast-radius table and risk scores.
Never include full files unless explicitly asked.
</section>

<section name="commands">
MCP tools (24): get_minimal_context_tool, build_or_update_graph_tool, run_postprocess_tool, get_impact_radius_tool, query_graph_tool, get_review_context_tool, semantic_search_nodes_tool, embed_graph_tool, list_graph_stats_tool, get_docs_section_tool, find_large_functions_tool, list_flows_tool, get_flow_tool, get_affected_flows_tool, list_communities_tool, get_community_tool, get_architecture_overview_tool, detect_changes_tool, refactor_tool, apply_refactor_tool, generate_wiki_tool, get_wiki_page_tool, list_repos_tool, cross_repo_search_tool
MCP prompts (5): review_changes, architecture_map, debug_issue, onboard_developer, pre_merge_check
Skills: build-graph, review-delta, review-pr
CLI: code-review-graph [install|init|build|update|status|watch|visualize|serve|wiki|detect-changes|enrich|postprocess|register|unregister|repos|eval]
Token efficiency: All tools support detail_level="minimal" for compact output. Always call get_minimal_context_tool first.
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
semantic_search_nodes_tool auto-uses vectors when available, falls back to keyword + FTS5.
Providers: Local (all-MiniLM-L6-v2, 384-dim), Google Gemini, MiniMax (embo-01, 1536-dim).
Configure via CRG_EMBEDDING_MODEL env var or model parameter.
</section>

<section name="languages">
Supported (19): Python, TypeScript/TSX, JavaScript, Vue, Go, Rust, Java, Scala, C#, Ruby, Kotlin, Swift, PHP, Solidity, C/C++, Dart, R, Perl, Lua
Parser: Tree-sitter via tree-sitter-language-pack
</section>

<section name="troubleshooting">
DB lock: SQLite WAL mode, auto-recovers. Only one build at a time.
Large repos: First build 30-60s. Incremental <2s. Add patterns to .code-review-graphignore.
Stale graph: Run /code-review-graph:build-graph manually.
Missing nodes: Check language support + ignore patterns. Use full_rebuild=True.
Windows/WSL: Use forward slashes in paths. Ensure uv is on PATH in WSL.
</section>

**Instruction to Claude Code (always follow):**
When user asks anything about "code-review-graph", "how to use", "commands", "review-delta", etc.:
1. Call get_docs_section_tool with the exact section name.
2. Use ONLY that content + current graph state.
3. Never include full docs or source code in your reasoning.
This guarantees 90%+ token savings.
