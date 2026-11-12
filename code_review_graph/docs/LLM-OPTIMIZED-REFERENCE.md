# LLM-OPTIMIZED REFERENCE: code-review-graph

Agents: fetch one section at a time with get_docs_section_tool(section_name="..."). Do not load the whole file.

<section name="usage">
Install: pip install code-review-graph, then code-review-graph install && code-review-graph build.
install --platform <name> sets up one client: codex, claude, cursor, windsurf, zed, continue, opencode, mimo (MiMo Code), antigravity, gemini-cli, qwen, kiro, qoder, copilot, copilot-cli, codebuddy, hermes or dsh (DeepSeek Harness). The default, all, sets up every client it finds.
First call in any task: get_minimal_context_tool(task="<task>"). About 100 tokens: summary, risk, communities, flows_affected, next_tool_suggestions. status: not_ready means call build_or_update_graph_tool first; its reason is missing_graph, empty_graph or stale_graph (built at another commit). A commit the pre-commit hook updated still counts as current.
next_tool_suggestions give the registered tool names, which end in _tool; call them as given.
Pass detail_level="minimal" unless it is not enough. Minimal drops source, so leave semantic_search_nodes_tool(include_source=True) and get_review_context_tool at standard.
Prefer a targeted query_graph_tool call over a broad listing tool.
To see how code works, semantic_search_nodes_tool(query="...", include_source=True, expand_neighbors=True) returns each hit's source and the top hits' direct callers and callees; open a file only when a snippet is marked source_truncated. Leave detail_level at standard for this call; minimal drops the source.
Budget: about five tool calls per task. Source snippets stand in for file reads.
context_savings, when present, is an estimate, not an exact token count.
Set CRG_CONTEXT_HISTORY=1 (off by default) to log each estimate to context-history.jsonl next to graph.db, holding token counts, tool names, a timestamp, a detail level and file and depth counts, never file names, paths or source text; read it with code-review-graph context-history [--by day|tool] [--days N] [--json].
</section>

<section name="review-delta">
1. get_minimal_context_tool(task="review changes"); build_or_update_graph_tool() if not_ready.
2. detect_changes_tool(detail_level="minimal") for risk-scored changed functions, test gaps and affected flows. removed_symbols lists deleted functions and classes, each with up to three used_by entries (callers, subclasses, importers, tests) and used_by_total; it counts towards the risk score.
3. get_review_context_tool() (standard detail; minimal returns counts and names only, with no source and no review_guidance) when you need source snippets and review_guidance. Pass max_files to keep it small.
4. query_graph_tool(pattern="tests_for", target="<function>") for each untested high-risk function.
Use detail_level="standard" only for high-risk items. Report summary, risk, issues, blast radius, recommendations.
</section>

<section name="review-pr">
1. get_minimal_context_tool(task="review PR", base="main"); build_or_update_graph_tool() if not_ready.
2. detect_changes_tool(base="main", detail_level="minimal"); get_affected_flows_tool(base="main") when you need flow steps (minimal mode returns per-flow metadata only, with no steps).
3. query_graph_tool with callers_of and tests_for for high-risk functions.
Report a structured review with a blast-radius table and risk scores. Do not include whole files unless asked.
</section>

<section name="commands">
MCP tools (30): build_or_update_graph_tool, run_postprocess_tool, get_minimal_context_tool, get_impact_radius_tool, query_graph_tool, get_review_context_tool, semantic_search_nodes_tool, embed_graph_tool, list_graph_stats_tool, get_docs_section_tool, find_large_functions_tool, list_flows_tool, get_flow_tool, get_affected_flows_tool, list_communities_tool, get_community_tool, get_architecture_overview_tool, detect_changes_tool, refactor_tool, apply_refactor_tool, generate_wiki_tool, get_wiki_page_tool, get_hub_nodes_tool, get_bridge_nodes_tool, get_knowledge_gaps_tool, get_surprising_connections_tool, get_suggested_questions_tool, traverse_graph_tool, list_repos_tool, cross_repo_search_tool.
query_graph_tool patterns: callers_of, references_to, callees_of, imports_of, importers_of, children_of, tests_for, inheritors_of, triggers_of, triggered_by, publishers_of, listeners_of, advises, advised_by, handlers_of, endpoints_for, consumers_of, file_summary, path_to (needs to=<node or file>; hops carry certainty: direct, unique_name or shared_name). resolution="direct" keeps only calls bound to an indexed node; "unresolved" keeps only the bare-name matches (query_graph_tool; get_impact_radius_tool takes "direct" too).
get_knowledge_gaps_tool reports import_cycles (files that import each other) beside isolated nodes, untested hotspots and thin communities.
refactor_tool modes: rename (old_name, new_name; apply with apply_refactor_tool(refactor_id)), dead_code, suggest.
MCP prompts (5): review_changes, architecture_map, debug_issue, onboard_developer, pre_merge_check.
Skills: build-graph, debug-issue, explore-codebase, refactor-safely, review-changes, review-delta, review-pr. install writes debug-issue, explore-codebase, refactor-safely and review-changes for Claude Code, Gemini CLI, CodeBuddy and Hermes Agent, and all seven for Qoder. Codex gets one global code-review-graph skill in $CODEX_HOME/skills (default ~/.codex/skills), with a read-only CLI fallback. Other clients get no skill files. --no-skills skips them all.
CLI: code-review-graph {install, init, uninstall, doctor, build, update, postprocess, embed, watch, status, context-history, forget, visualize, wiki, register, unregister, repos, eval, detect-changes, dead-code, query, impact, search, flows, flow, communities, community, architecture, large-functions, refactor, serve, mcp, daemon}. enrich, hook-update and hook-status are for hooks only and hidden from --help. Run code-review-graph <command> --help for flags.
Exit codes: 0 ok; 1 error (as detect-changes --base with a revision Git does not know, or doctor with a failed check); 2 bad usage (an unknown command prints "Did you mean ...?"); 75 another write holds the lock. The MCP review tools answer an unknown base with status: error.
Result bounds: lists are capped. Defaults are small; pass the tool's cap parameter to widen up to its hard ceiling. Truncation is reported: total (or a *_total field per list) gives the full count, truncated is true, and the summary says how many of how many are shown.
Cap parameters: max_results (get_impact_radius, query_graph, get_review_context, detect_changes, list_communities, get_architecture_overview, refactor, cross_repo_search); max_flows (get_affected_flows, detect_changes); max_members (list_communities, get_community, get_architecture_overview); max_steps and max_source_lines (get_flow); max_source_lines (semantic_search_nodes, with include_source); max_per_category (get_knowledge_gaps); top_n (get_hub_nodes, get_bridge_nodes, get_surprising_connections); limit (list_flows, semantic_search_nodes, find_large_functions, cross_repo_search); token_budget (traverse_graph, at most 10000); max_files (get_review_context; get_impact_radius with detail_level="compact", at most 200); max_chars (get_wiki_page); max_diff_files (apply_refactor).
Caps reject values below 1 and booleans. get_affected_flows treats max_flows=0 as no caller limit, still capped at 25 flows in standard mode or 500 in minimal, with a shared 400-step budget (#849).
</section>

<section name="legal">
MIT licence. Graph build and review run locally; no telemetry. Database: .code-review-graph/graph.db. Cloud embedding providers (openai, google, minimax, voyage) send node names, docstrings and file paths to that provider only when you select them (openai is also used when CRG_OPENAI_API_KEY and CRG_OPENAI_BASE_URL are set and no provider is named); a stderr warning is printed unless CRG_ACCEPT_CLOUD_EMBEDDINGS=1 or the openai or voyage base URL is localhost.
</section>

<section name="watch">
code-review-graph watch updates the graph on file save (watchdog).
For several repositories, code-review-graph daemon (or crg-daemon) start, stop, restart, status, logs, add <path> and remove <path_or_alias> run one watch per repository listed in ~/.code-review-graph/watch.toml.
code-review-graph install adds hooks instead: PostToolUse on Edit|Write runs hook-update, which updates the repository that holds the edited file (event names vary by platform); SessionStart runs status; git pre-commit, post-checkout, post-merge and post-rewrite hooks run update, so a commit, branch switch, merge or pull keeps the graph current. Git hooks skip linked worktrees unless CRG_HOOK_WORKTREES=1.
install --steering (Claude Code only, off by default) adds a hook that puts graph context beside each search and read.
</section>

<section name="embeddings">
Local provider: pip install "code-review-graph[embeddings]"; default model all-MiniLM-L6-v2, override with CRG_EMBEDDING_MODEL.
Cloud providers, selected with embed_graph_tool(provider=...): openai needs CRG_OPENAI_BASE_URL, CRG_OPENAI_API_KEY and CRG_OPENAI_MODEL (any OpenAI-compatible endpoint); google needs GOOGLE_API_KEY and pip install "code-review-graph[google-embeddings]"; minimax needs MINIMAX_API_KEY; voyage needs VOYAGE_API_KEY, model from CRG_VOYAGE_MODEL or voyage-code-3.
With no provider named, openai is used when CRG_OPENAI_API_KEY and CRG_OPENAI_BASE_URL are both set; otherwise local.
Run embed_graph_tool once; changing model or provider re-embeds every node. semantic_search_nodes_tool uses vectors when they exist for the chosen provider, otherwise FTS5 keyword search.
</section>

<section name="languages">
Tree-sitter (tree-sitter-language-pack): Python, JavaScript, TypeScript, TSX, Go, Rust, Java, C, C++, C#, Ruby, Kotlin, Swift, PHP, Scala, Solidity, Dart, R, Perl, Lua, Luau, Objective-C, Bash and Zsh, Elixir, Zig, PowerShell, Julia, GDScript, Nix, Verilog and SystemVerilog, SQL/PL/SQL (Oracle), HCL (Terraform .tf and .hcl).
Targeted parsers: XSLT stylesheets (templates and functions, through the XML grammar), Vue and Svelte single-file components, Liquid templates (assign, outputs, render and include of snippets), Laravel Blade templates (*.blade.php; @extends, @include, @component and @livewire references), HTML pages (inline scripts and id-anchored DocSection blocks), Astro (through the TypeScript parser), Jupyter and Databricks notebooks, Perl XS (parsed as C), Ansible playbooks, roles and tasks, Spring configuration (.properties and YAML), value-free generic YAML structure, value-free JSON structure (.json; translation, test, docs, example and data files keep only a file node; lock files skipped), Robot Framework (.robot and .resource), VB.NET and ReScript (structural parsers; the language pack has no grammar for them).
Files with no extension: the language comes from the shebang line (sh, bash, zsh, ksh, dash, ash, python, pypy, node, ruby, perl, lua, Rscript, php).
Custom languages: .code-review-graph/languages.toml (extensions and node types per grammar), see docs/CUSTOM_LANGUAGES.md. Built-in extensions and language names cannot be overridden.
</section>

<section name="troubleshooting">
Locked database: SQLite runs in WAL mode with a 5 s busy timeout and recovers on its own.
Overlapping writes: only one build, update, postprocess, embed or forget runs per repository. A second one stops with status contended (CLI exit code 75); pass --wait (CLI) to wait for the first, or retry the MCP call once it finishes.
Large repos: a cold build of about 3,000 files takes about 40 s; an incremental update on the hook path about 2.5 s (docs/REPRODUCING.md). Add patterns to .code-review-graphignore to skip generated code.
Stale graph: code-review-graph update, or build_or_update_graph_tool(full_rebuild=True) when nodes are missing.
Setup check: code-review-graph doctor (read-only; --json) lists every copy on PATH and its version, the version each MCP entry in the repository's configs runs, the graph schema, whether it was built at HEAD and whether the last build finished, and the hooks. "Written by a newer code-review-graph" means an older copy answered: upgrade every copy doctor lists, then restart the editor.
Missing nodes: check the languages section and your ignore patterns. Symlinked folders are skipped unless you pass --follow-symlinks to build, update or watch, or set CRG_FOLLOW_SYMLINKS=1 (at most CRG_MAX_FOLLOWED_FILES files, default 10000). Symlinked files are never followed.
Windows and WSL: use forward slashes in repo_root; in WSL install uv inside WSL and use WSL2 for file watching.
</section>

Agents: for any question about code-review-graph usage, commands or workflows, call get_docs_section_tool with the section name and answer from that section plus the current graph state only.
