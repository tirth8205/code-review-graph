# LLM-OPTIMIZED REFERENCE: code-review-graph

Agents: fetch one section at a time with get_docs_section_tool(section_name="..."). Do not load the whole file.

<section name="usage">
Install: pip install code-review-graph, then code-review-graph install && code-review-graph build.
First call in any task: get_minimal_context_tool(task="<task>"). About 100 tokens: summary, risk, communities, flows_affected, next_tool_suggestions. status: not_ready means call build_or_update_graph_tool first.
Pass detail_level="minimal" wherever a tool accepts it; use "standard" only when minimal is not enough.
Prefer a targeted query_graph_tool call over a broad listing tool.
Budget: about five tool calls and 800 tokens of graph output per task.
context_savings, when present, is an estimate, not an exact token count.
</section>

<section name="review-delta">
1. get_minimal_context_tool(task="review changes"); build_or_update_graph_tool() if not_ready.
2. detect_changes_tool(detail_level="minimal") for risk-scored changed functions, test gaps and affected flows.
3. get_review_context_tool(detail_level="minimal") when you need source snippets and review_guidance.
4. query_graph_tool(pattern="tests_for", target="<function>") for each untested high-risk function.
Use detail_level="standard" only for high-risk items. Report summary, risk, issues, blast radius, recommendations.
</section>

<section name="review-pr">
1. get_minimal_context_tool(task="review PR", base="main"); build_or_update_graph_tool() if not_ready.
2. detect_changes_tool(base="main", detail_level="minimal"); get_affected_flows_tool(base="main", detail_level="minimal") when you need flow steps.
3. query_graph_tool with callers_of and tests_for for high-risk functions.
Report a structured review with a blast-radius table and risk scores. Do not include whole files unless asked.
</section>

<section name="commands">
MCP tools (30): build_or_update_graph_tool, run_postprocess_tool, get_minimal_context_tool, get_impact_radius_tool, query_graph_tool, get_review_context_tool, semantic_search_nodes_tool, embed_graph_tool, list_graph_stats_tool, get_docs_section_tool, find_large_functions_tool, list_flows_tool, get_flow_tool, get_affected_flows_tool, list_communities_tool, get_community_tool, get_architecture_overview_tool, detect_changes_tool, refactor_tool, apply_refactor_tool, generate_wiki_tool, get_wiki_page_tool, get_hub_nodes_tool, get_bridge_nodes_tool, get_knowledge_gaps_tool, get_surprising_connections_tool, get_suggested_questions_tool, traverse_graph_tool, list_repos_tool, cross_repo_search_tool.
query_graph_tool patterns: callers_of, references_to, callees_of, imports_of, importers_of, children_of, tests_for, inheritors_of, triggers_of, triggered_by, publishers_of, listeners_of, handlers_of, endpoints_for, consumers_of, file_summary.
refactor_tool modes: rename (old_name, new_name; apply with apply_refactor_tool(refactor_id)), dead_code, suggest.
MCP prompts (5): review_changes, architecture_map, debug_issue, onboard_developer, pre_merge_check.
Skills: build-graph, debug-issue, explore-codebase, refactor-safely, review-changes, review-delta, review-pr.
CLI: code-review-graph {install, init, uninstall, build, update, postprocess, embed, watch, status, forget, visualize, wiki, register, unregister, repos, eval, detect-changes, enrich, dead-code, query, impact, search, flows, flow, communities, community, architecture, large-functions, refactor, serve, mcp, daemon}. Run code-review-graph <command> --help for flags.
Result bounds: lists are capped. Defaults are small; pass the tool's cap parameter to widen up to its hard ceiling. Truncation is reported: total (or a *_total field per list) gives the full count, truncated is true, and the summary says how many of how many are shown.
Cap parameters: max_results (query_graph, get_review_context, detect_changes, list_communities, get_architecture_overview, refactor, cross_repo_search); max_flows (get_affected_flows, detect_changes); max_members (list_communities, get_community, get_architecture_overview); max_steps and max_source_lines (get_flow); max_per_category (get_knowledge_gaps); top_n (get_hub_nodes, get_bridge_nodes, get_surprising_connections); limit (list_flows, semantic_search_nodes, find_large_functions, cross_repo_search); max_files (get_review_context); max_chars (get_wiki_page); max_diff_files (apply_refactor).
Caps reject values below 1 and booleans. get_affected_flows treats max_flows=0 as no caller limit, still capped at 25 flows in standard mode or 500 in minimal, with a shared 400-step budget (#849).
</section>

<section name="legal">
MIT licence. Graph build and review run locally; no telemetry. Database: .code-review-graph/graph.db. Cloud embedding providers (openai, google, minimax, voyage) send node names, docstrings and file paths to that provider only when you select them; a stderr warning is printed unless CRG_ACCEPT_CLOUD_EMBEDDINGS=1.
</section>

<section name="watch">
code-review-graph watch updates the graph on file save (watchdog).
code-review-graph install adds hooks instead: PostToolUse on Edit|Write runs update (event names vary by platform), SessionStart runs status, and a git pre-commit hook runs update.
</section>

<section name="embeddings">
Local provider: pip install "code-review-graph[embeddings]"; default model all-MiniLM-L6-v2, override with CRG_EMBEDDING_MODEL.
Cloud providers, selected with embed_graph_tool(provider=...): openai needs CRG_OPENAI_BASE_URL, CRG_OPENAI_API_KEY and CRG_OPENAI_MODEL (any OpenAI-compatible endpoint); google needs GOOGLE_API_KEY and pip install "code-review-graph[google-embeddings]"; minimax needs MINIMAX_API_KEY; voyage needs VOYAGE_API_KEY, model from CRG_VOYAGE_MODEL or voyage-code-3.
Run embed_graph_tool once; changing model or provider re-embeds every node. semantic_search_nodes_tool uses vectors when they exist for the chosen provider, otherwise FTS5 keyword search.
</section>

<section name="languages">
Tree-sitter (tree-sitter-language-pack): Python, JavaScript, TypeScript, TSX, Go, Rust, Java, C, C++, C#, VB.NET, Ruby, Kotlin, Swift, PHP, Scala, Solidity, Dart, R, Perl, Lua, Luau, Objective-C, Bash and Zsh, Elixir, Zig, PowerShell, Julia, ReScript, GDScript, Nix, Verilog and SystemVerilog, SQL, HCL (Terraform .tf and .hcl).
Targeted parsers: Vue and Svelte single-file components, Astro (through the TypeScript parser), Jupyter and Databricks notebooks, Perl XS (parsed as C), Ansible playbooks, roles and tasks, Spring configuration (.properties and YAML). Other YAML is not parsed.
Custom languages: .code-review-graph/languages.toml (extensions and node types per grammar), see docs/CUSTOM_LANGUAGES.md. Built-in extensions and language names cannot be overridden.
</section>

<section name="troubleshooting">
Locked database: SQLite runs in WAL mode with a 5 s busy timeout and recovers on its own. Run one build at a time.
Large repos: a cold build of about 3,000 files takes about 40 s; an incremental update on the hook path about 2.5 s (docs/REPRODUCING.md). Add patterns to .code-review-graphignore to skip generated code.
Stale graph: code-review-graph update, or build_or_update_graph_tool(full_rebuild=True) when nodes are missing.
Missing nodes: check the languages section and your ignore patterns.
Windows and WSL: use forward slashes in repo_root; in WSL install uv inside WSL and use WSL2 for file watching.
</section>

Agents: for any question about code-review-graph usage, commands or workflows, call get_docs_section_tool with the section name and answer from that section plus the current graph state only.
