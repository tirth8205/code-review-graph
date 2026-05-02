# LLM-OPTIMIZED REFERENCE -- code-review-graph v2.3.6

AI coding agents: Read ONLY the exact `<section>` you need. Never load the whole file.

<section name="usage">
Quick install: pip install code-review-graph
Then: code-review-graph install && code-review-graph build
First run: /code-review-graph:build-graph
After that use only delta/pr commands.
ALWAYS start with get_minimal_context_tool(task="your task") — returns ~100 tokens with risk, communities, flows, and suggested next tools.
Use detail_level="minimal" on all subsequent calls unless you need more detail.
When present, context_savings is an estimated compact hint, not exact tokenization.
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
Core MCP tools: get_minimal_context_tool, detect_changes_tool, get_review_context_tool, get_impact_radius_tool, query_graph_tool, semantic_search_nodes_tool, get_architecture_overview_tool, get_affected_flows_tool, list_flows_tool, list_communities_tool, refactor_tool, build_or_update_graph_tool, run_postprocess_tool, embed_graph_tool, list_graph_stats_tool, get_docs_section_tool
MCP prompts (5): review_changes, architecture_map, debug_issue, onboard_developer, pre_merge_check
Skills: build-graph, debug-issue, explore-codebase, refactor-safely, review-changes, review-delta, review-pr
CLI: code-review-graph [install|init|build|update|status|watch|visualize|serve|mcp|wiki|detect-changes|postprocess|embed|register|unregister|repos|eval|daemon]
Token efficiency: Prefer detail_level="minimal" where available. Always call get_minimal_context_tool first. Some review/context tools return compact estimated context_savings metadata.
</section>

<section name="legal">
MIT licence. Core graph/review workflows are local and there is no telemetry. DB file: .code-review-graph/graph.db. Optional cloud embeddings send embedded source snippets to the configured provider only when selected.
</section>

<section name="watch">
Run: code-review-graph watch (auto-updates graph on file save via watchdog)
Or use PostToolUse (Write|Edit|Bash) hooks for automatic background updates.
</section>

<section name="embeddings">
Optional: pip install "code-review-graph[embeddings]"
Then call embed_graph_tool to compute vectors.
semantic_search_nodes_tool auto-uses vectors when available, falls back to keyword + FTS5.
Providers: local sentence-transformers, OpenAI-compatible endpoints, Google Gemini, and MiniMax.
Configure via provider/model parameters, CRG_EMBEDDING_MODEL for local, or CRG_OPENAI_* for OpenAI-compatible endpoints.
</section>

<section name="languages">
Supported: Python, JavaScript/TypeScript/TSX, Go, Rust, Java, C/C++, C#, Ruby, Kotlin, Swift, PHP, Scala, Solidity, Dart, R, Perl, Lua/Luau, Objective-C, shell scripts, Elixir, Zig, PowerShell, Julia, ReScript, GDScript, Nix, Verilog/SystemVerilog, SQL, Terraform/OpenTofu structure (`.tf`; generic `.hcl` files are recognized as file nodes), Ansible playbooks/roles/tasks, Vue/Svelte SFCs, Astro files parsed through the TypeScript parser, Jupyter/Databricks notebooks, and Perl XS files. Generic YAML is not treated as source code.
Parser: Tree-sitter via tree-sitter-language-pack
Custom languages: add .code-review-graph/languages.toml (extensions + node types per grammar) — no fork needed, see docs/CUSTOM_LANGUAGES.md. Built-ins cannot be overridden.
</section>

<section name="troubleshooting">
DB lock: SQLite WAL mode, auto-recovers. Only one build at a time.
Large repos: First build 30-60s. Incremental <2s. Add patterns to .code-review-graphignore.
Stale graph: Run /code-review-graph:build-graph manually.
Missing nodes: Check language support + ignore patterns. Use full_rebuild=True.
Windows/WSL: Use forward slashes in paths. Ensure uv is on PATH in WSL.
</section>

**Instruction to AI coding agents (always follow):**
When user asks anything about "code-review-graph", "how to use", "commands", "review-delta", etc.:
1. Call get_docs_section_tool with the exact section name.
2. Use ONLY that content + current graph state.
3. Never include full docs or source code in your reasoning.
This keeps documentation lookup compact and avoids loading broad reference files by default.
