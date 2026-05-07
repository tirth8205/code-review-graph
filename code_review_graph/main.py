"""MCP server entry point for Code Review Graph.

Run as: code-review-graph serve
Communicates via stdio (standard MCP transport), or use
``code-review-graph serve --http`` for Streamable HTTP on localhost (port 5555
by default).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

from .prompts import (
    architecture_map_prompt,
    debug_issue_prompt,
    onboard_developer_prompt,
    pre_merge_check_prompt,
    review_changes_prompt,
)
from .tools import (
    apply_refactor_func,
    build_or_update_graph,
    cross_repo_search_func,
    detect_changes_func,
    embed_graph,
    find_large_functions,
    generate_wiki_func,
    get_affected_flows_func,
    get_architecture_overview_func,
    get_bridge_nodes_func,
    get_community_func,
    get_docs_section,
    get_flow,
    get_hub_nodes_func,
    get_impact_radius,
    get_knowledge_gaps_func,
    get_minimal_context,
    get_review_context,
    get_suggested_questions_func,
    get_surprising_connections_func,
    get_wiki_page_func,
    traverse_graph_func,
    list_communities_func,
    list_flows,
    list_graph_stats,
    list_repos_func,
    query_graph,
    refactor_func,
    run_postprocess,
    semantic_search_nodes,
)

from .graph import GraphStore
from .incremental import find_project_root, get_db_path, start_watch_thread

logger = logging.getLogger(__name__)

# NOTE: Thread-safe for stdio MCP (single-threaded). If adding HTTP/SSE
# transport with concurrent requests, replace with contextvars.ContextVar.
_default_repo_root: str | None = None


def _resolve_repo_root(repo_root: Optional[str]) -> Optional[str]:
    """Resolve repo_root for a tool call.

    Order of precedence:
    1. Explicit ``repo_root`` passed by the MCP client (highest).
    2. ``--repo`` CLI flag passed to ``code-review-graph serve``
       (captured in ``_default_repo_root``).
    3. None — the underlying impl will fall back to the server's cwd.

    All MCP tools that accept ``repo_root`` should use this helper so
    ``serve --repo <X>`` applies consistently, including
    ``get_docs_section_tool``. See: #222.
    """
    return repo_root if repo_root else _default_repo_root


mcp = FastMCP(
    "code-review-graph",
    instructions=(
        "Persistent incremental knowledge graph for token-efficient, "
        "context-aware code reviews. Parses your codebase with Tree-sitter, "
        "builds a structural graph, and provides smart impact analysis."
    ),
)


@mcp.tool()
async def build_or_update_graph_tool(
    full_rebuild: bool = False,
    repo_root: Optional[str] = None,
    base: str = "HEAD~1",
    postprocess: str = "full",
    recurse_submodules: Optional[bool] = None,
) -> dict:
    """Build or incrementally update the code knowledge graph.

    Call this first to initialize the graph, or after making changes.
    By default performs an incremental update (only changed files).
    Set full_rebuild=True to re-parse every file.

    Runs the blocking full_build / incremental_update work in a thread
    via ``asyncio.to_thread`` so the stdio event loop stays responsive.
    Without this wrapper, long builds deadlocked on Windows because
    ``ProcessPoolExecutor`` (used by parallel parsing) interacted badly
    with the sync handler blocking the only event-loop thread. See:
    #46, #136.

    Args:
        full_rebuild: If True, re-parse all files. Default: False (incremental).
        repo_root: Repository root path. Auto-detected from current directory if omitted.
        base: Git ref to diff against for incremental updates. Default: HEAD~1.
        postprocess: Post-processing level: "full" (default), "minimal" (signatures+FTS only),
                     or "none" (skip all post-processing). Use "minimal" for faster builds.
        recurse_submodules: If True, include files from git submodules.
            When None (default), falls back to CRG_RECURSE_SUBMODULES env var.
    """
    return await asyncio.to_thread(
        build_or_update_graph,
        full_rebuild=full_rebuild,
        repo_root=_resolve_repo_root(repo_root),
        base=base,
        postprocess=postprocess,
        recurse_submodules=recurse_submodules,
    )


@mcp.tool()
async def run_postprocess_tool(
    flows: bool = True,
    communities: bool = True,
    fts: bool = True,
    repo_root: Optional[str] = None,
) -> dict:
    """Run post-processing on existing graph (flows, communities, FTS index).

    Use after building with postprocess="none" or "minimal", or to re-run
    expensive steps independently. Signatures are always computed.

    Offloaded to a thread via ``asyncio.to_thread`` so community
    detection on large graphs doesn't block the MCP event loop. See:
    #46, #136.

    Args:
        flows: Run flow detection. Default: True.
        communities: Run community detection. Default: True.
        fts: Rebuild FTS index. Default: True.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return await asyncio.to_thread(
        run_postprocess,
        flows=flows, communities=communities, fts=fts,
        repo_root=_resolve_repo_root(repo_root),
    )


@mcp.tool()
def get_minimal_context_tool(
    task: str = "",
    changed_files: Optional[list[str]] = None,
    repo_root: Optional[str] = None,
    base: str = "HEAD~1",
) -> dict:
    """Get ultra-compact context for any task (~100 tokens). Always call this first.

    Returns graph stats, risk score, top communities/flows, and suggested
    next tools in a single compact response. Use this as the entry point
    before any other graph tool to minimize token usage.

    Args:
        task: What you are doing (e.g. "review PR #42", "debug login timeout").
        changed_files: Explicit list of changed files. Auto-detected if omitted.
        repo_root: Repository root path. Auto-detected if omitted.
        base: Git ref for diff comparison. Default: HEAD~1.
    """
    return get_minimal_context(
        task=task, changed_files=changed_files,
        repo_root=_resolve_repo_root(repo_root), base=base,
    )


@mcp.tool()
def get_impact_radius_tool(
    changed_files: Optional[list[str]] = None,
    max_depth: int = 2,
    repo_root: Optional[str] = None,
    base: str = "HEAD~1",
    detail_level: str = "standard",
) -> dict:
    """Analyze the blast radius of changed files in the codebase.

    Shows which functions, classes, and files are impacted by changes.
    Auto-detects changed files from git if not specified.

    Args:
        changed_files: List of changed file paths (relative to repo root). Auto-detected if omitted.
        max_depth: Number of hops to traverse in the dependency graph. Default: 2.
        repo_root: Repository root path. Auto-detected if omitted.
        base: Git ref for auto-detecting changes. Default: HEAD~1.
        detail_level: "standard" for full output, "minimal" for compact summary. Default: standard.
    """
    return get_impact_radius(
        changed_files=changed_files, max_depth=max_depth,
        repo_root=_resolve_repo_root(repo_root), base=base, detail_level=detail_level,
    )


@mcp.tool()
def query_graph_tool(
    pattern: str,
    target: str,
    repo_root: Optional[str] = None,
    detail_level: str = "standard",
) -> dict:
    """Run a predefined graph query to explore code relationships.

    Available patterns:
    - callers_of: Find functions that call the target
    - callees_of: Find functions called by the target
    - imports_of: Find what the target imports
    - importers_of: Find files that import the target
    - children_of: Find nodes contained in a file or class
    - tests_for: Find tests for the target
    - inheritors_of: Find classes inheriting from the target
    - file_summary: Get all nodes in a file

    Args:
        pattern: Query pattern name (see above).
        target: Node name, qualified name, or file path to query.
        repo_root: Repository root path. Auto-detected if omitted.
        detail_level: "standard" for full output, "minimal" for compact summary. Default: standard.
    """
    return query_graph(
        pattern=pattern, target=target, repo_root=_resolve_repo_root(repo_root),
        detail_level=detail_level,
    )


@mcp.tool()
def get_review_context_tool(
    changed_files: Optional[list[str]] = None,
    max_depth: int = 2,
    include_source: bool = True,
    max_lines_per_file: int = 200,
    repo_root: Optional[str] = None,
    base: str = "HEAD~1",
    detail_level: str = "standard",
) -> dict:
    """Generate a focused, token-efficient review context for code changes.

    Combines impact analysis with source snippets and review guidance.
    Use this for comprehensive code reviews.

    Args:
        changed_files: Files to review. Auto-detected from git diff if omitted.
        max_depth: Impact radius depth. Default: 2.
        include_source: Include source code snippets. Default: True.
        max_lines_per_file: Max source lines per file. Default: 200.
        repo_root: Repository root path. Auto-detected if omitted.
        base: Git ref for change detection. Default: HEAD~1.
        detail_level: "standard" for full output, "minimal" for
            token-efficient summary. Default: standard.
    """
    return get_review_context(
        changed_files=changed_files, max_depth=max_depth,
        include_source=include_source, max_lines_per_file=max_lines_per_file,
        repo_root=_resolve_repo_root(repo_root), base=base, detail_level=detail_level,
    )


@mcp.tool()
def semantic_search_nodes_tool(
    query: str,
    kind: Optional[str] = None,
    limit: int = 20,
    repo_root: Optional[str] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    detail_level: str = "standard",
) -> dict:
    """Search for code entities by name, keyword, or semantic similarity.

    Uses vector embeddings for semantic search when available (run embed_graph_tool
    first, with a provider of your choice: "local" needs sentence-transformers,
    "openai" / "google" / "minimax" need their respective env vars). Falls back
    to FTS5 / keyword matching when no matching embeddings exist for the given
    provider.

    Args:
        query: Search string to match against node names.
        kind: Optional filter: File, Class, Function, Type, or Test.
        limit: Maximum results. Default: 20.
        repo_root: Repository root path. Auto-detected if omitted.
        model: Embedding model for query vectors. Must match the model used
               during embed_graph. Falls back to CRG_EMBEDDING_MODEL env var
               (local) or CRG_OPENAI_MODEL (openai).
        provider: Embedding provider: "local" (default), "openai", "google",
                  or "minimax". Must match the provider used during embed_graph.
        detail_level: "standard" for full output, "minimal" for compact summary. Default: standard.
    """
    return semantic_search_nodes(
        query=query, kind=kind, limit=limit, repo_root=_resolve_repo_root(repo_root),
        model=model, provider=provider, detail_level=detail_level,
    )


@mcp.tool()
async def embed_graph_tool(
    repo_root: Optional[str] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> dict:
    """Compute vector embeddings for all graph nodes to enable semantic search.

    Requires: pip install code-review-graph[embeddings] (local provider only;
    cloud providers use stdlib urllib).
    Default provider: local. Default model: all-MiniLM-L6-v2.
    Override provider via `provider` param, model via `model` param or
    CRG_EMBEDDING_MODEL / CRG_OPENAI_MODEL env vars.
    Changing the model or provider re-embeds all nodes automatically.

    After running this, semantic_search_nodes_tool will use vector similarity
    instead of keyword matching for much better results.

    Runs the blocking sentence-transformers / Gemini / HTTP inference in a
    thread via ``asyncio.to_thread`` so the stdio event loop stays
    responsive — without this wrapper, embedding a large graph would
    silently hang the MCP server on Windows. See: #46, #136.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
        model: Embedding model. For local: HuggingFace ID/path; for openai:
               model ID (e.g. "text-embedding-3-small"); for google: Gemini
               model ID. Falls back to CRG_EMBEDDING_MODEL / CRG_OPENAI_MODEL
               env vars as appropriate.
        provider: "local" (default), "openai", "google", or "minimax".
                  "openai" requires CRG_OPENAI_BASE_URL + CRG_OPENAI_API_KEY +
                  CRG_OPENAI_MODEL env vars and accepts any OpenAI-compatible
                  endpoint (real OpenAI, Azure, new-api, LiteLLM, vLLM, etc.).
    """
    return await asyncio.to_thread(
        embed_graph,
        repo_root=_resolve_repo_root(repo_root),
        model=model,
        provider=provider,
    )


@mcp.tool()
def list_graph_stats_tool(
    repo_root: Optional[str] = None,
) -> dict:
    """Get aggregate statistics about the code knowledge graph.

    Shows total nodes, edges, languages, files, and last update time.
    Useful for checking if the graph is built and up to date.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return list_graph_stats(repo_root=_resolve_repo_root(repo_root))


@mcp.tool()
def get_docs_section_tool(
    section_name: str,
    repo_root: Optional[str] = None,
) -> dict:
    """Get a specific section from the LLM-optimized documentation reference.

    Returns only the requested section content for minimal token usage.
    Use this before answering any user question about the plugin.

    Available sections: usage, review-delta, review-pr, commands, legal,
    watch, embeddings, languages, troubleshooting.

    Args:
        section_name: The section to retrieve (e.g. "review-delta", "usage").
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return get_docs_section(
        section_name=section_name,
        repo_root=_resolve_repo_root(repo_root),
    )


@mcp.tool()
def find_large_functions_tool(
    min_lines: int = 50,
    kind: Optional[str] = None,
    file_path_pattern: Optional[str] = None,
    limit: int = 50,
    repo_root: Optional[str] = None,
) -> dict:
    """Find functions, classes, or files exceeding a line-count threshold.

    Useful for decomposition audits, code quality checks, and enforcing
    size limits during code review. Results are ordered by line count.

    Args:
        min_lines: Minimum line count to flag. Default: 50.
        kind: Optional filter: Function, Class, File, or Test.
        file_path_pattern: Filter by file path substring (e.g. "components/").
        limit: Maximum results. Default: 50.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return find_large_functions(
        min_lines=min_lines, kind=kind, file_path_pattern=file_path_pattern,
        limit=limit, repo_root=_resolve_repo_root(repo_root),
    )


@mcp.tool()
def list_flows_tool(
    sort_by: str = "criticality",
    limit: int = 50,
    kind: Optional[str] = None,
    detail_level: str = "standard",
    repo_root: Optional[str] = None,
) -> dict:
    """List execution flows in the codebase, sorted by criticality.

    Each flow represents a call chain starting from an entry point
    (HTTP handler, CLI command, test function, etc.). Use this to
    understand the main execution paths through the codebase.

    Args:
        sort_by: Sort column: criticality, depth, node_count, file_count, or name.
        limit: Maximum flows to return. Default: 50.
        kind: Optional filter by entry point kind (e.g. "Test", "Function").
        detail_level: "standard" (default) returns full flow data; "minimal"
                      returns only name, criticality, and node_count per flow.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return list_flows(
        repo_root=_resolve_repo_root(repo_root), sort_by=sort_by, limit=limit, kind=kind,
        detail_level=detail_level,
    )


@mcp.tool()
def get_flow_tool(
    flow_id: Optional[int] = None,
    flow_name: Optional[str] = None,
    include_source: bool = False,
    repo_root: Optional[str] = None,
) -> dict:
    """Get detailed information about a single execution flow.

    Returns the full call path with each step's function name, file, and
    line numbers. Optionally includes source code snippets for each step.

    Provide either flow_id (from list_flows_tool) or flow_name to search by name.

    Args:
        flow_id: Database ID of the flow.
        flow_name: Name to search for (partial match). Ignored if flow_id given.
        include_source: Include source code snippets for each step. Default: False.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return get_flow(
        flow_id=flow_id, flow_name=flow_name,
        include_source=include_source, repo_root=_resolve_repo_root(repo_root),
    )


@mcp.tool()
def get_affected_flows_tool(
    changed_files: Optional[list[str]] = None,
    base: str = "HEAD~1",
    repo_root: Optional[str] = None,
) -> dict:
    """Find execution flows affected by changed files.

    Identifies which execution flows pass through nodes in the changed files.
    Useful during code review to understand which user-facing or critical paths
    are impacted by a change. Auto-detects changed files from git if not specified.

    Args:
        changed_files: List of changed file paths (relative to repo root). Auto-detected if omitted.
        base: Git ref for auto-detecting changes. Default: HEAD~1.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return get_affected_flows_func(
        changed_files=changed_files, base=base, repo_root=_resolve_repo_root(repo_root),
    )


@mcp.tool()
def list_communities_tool(
    sort_by: str = "size",
    min_size: int = 0,
    detail_level: str = "standard",
    repo_root: Optional[str] = None,
) -> dict:
    """List detected code communities in the codebase.

    Each community represents a cluster of related code entities (functions,
    classes) detected via the Leiden algorithm or file-based grouping.
    Use this to understand the high-level structure of the codebase.

    Args:
        sort_by: Sort column: size, cohesion, or name.
        min_size: Minimum community size to include. Default: 0.
        detail_level: "standard" (default) returns full community data;
                      "minimal" returns only name, size, and cohesion
                      per community.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return list_communities_func(
        repo_root=_resolve_repo_root(repo_root), sort_by=sort_by, min_size=min_size,
        detail_level=detail_level,
    )


@mcp.tool()
def get_community_tool(
    community_name: Optional[str] = None,
    community_id: Optional[int] = None,
    include_members: bool = False,
    repo_root: Optional[str] = None,
) -> dict:
    """Get detailed information about a single code community.

    Returns community metadata including size, cohesion, dominant language,
    and member list. Optionally includes full node details for each member.

    Provide either community_id (from list_communities_tool) or community_name
    to search by name.

    Args:
        community_name: Name to search for (partial match). Ignored if community_id given.
        community_id: Database ID of the community.
        include_members: Include full member node details. Default: False.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return get_community_func(
        community_name=community_name, community_id=community_id,
        include_members=include_members, repo_root=_resolve_repo_root(repo_root),
    )


@mcp.tool()
def get_architecture_overview_tool(
    repo_root: Optional[str] = None,
) -> dict:
    """Generate an architecture overview based on community structure.

    Builds a high-level view of the codebase architecture by analyzing
    community boundaries and cross-community coupling. Includes warnings
    for high coupling between communities.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return get_architecture_overview_func(repo_root=_resolve_repo_root(repo_root))


@mcp.tool()
async def detect_changes_tool(
    base: str = "HEAD~1",
    changed_files: Optional[list[str]] = None,
    include_source: bool = False,
    max_depth: int = 2,
    repo_root: Optional[str] = None,
    detail_level: str = "standard",
) -> dict:
    """Detect changes and produce risk-scored, priority-ordered review guidance.

    Primary tool for code review. Maps git diffs to affected functions,
    flows, communities, and test coverage gaps. Returns risk scores and
    prioritized review items. Replaces get_review_context for change-aware reviews.

    Offloaded to a thread via ``asyncio.to_thread`` — runs `git diff`
    subprocesses and BFS traversals that can take several seconds on
    large repos. See: #46, #136.

    Args:
        base: Git ref to diff against. Default: HEAD~1.
        changed_files: List of changed file paths (relative to repo root). Auto-detected if omitted.
        include_source: Include source code snippets for changed functions. Default: False.
        max_depth: Impact radius depth for BFS traversal. Default: 2.
        repo_root: Repository root path. Auto-detected if omitted.
        detail_level: "standard" for full output, "minimal" for
            token-efficient summary. Default: standard.
    """
    return await asyncio.to_thread(
        detect_changes_func,
        base=base, changed_files=changed_files,
        include_source=include_source, max_depth=max_depth,
        repo_root=_resolve_repo_root(repo_root), detail_level=detail_level,
    )


@mcp.tool()
def refactor_tool(
    mode: str = "rename",
    old_name: Optional[str] = None,
    new_name: Optional[str] = None,
    kind: Optional[str] = None,
    file_pattern: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict:
    """Graph-powered refactoring operations.

    Unified entry point for rename previews, dead code detection, and
    refactoring suggestions.

    Modes:
    - rename: Preview renaming a symbol. Returns an edit list and a refactor_id
      to pass to apply_refactor_tool. Requires old_name and new_name.
    - dead_code: Find unreferenced functions/classes (no callers, tests, or
      importers, and not entry points).
    - suggest: Get community-driven refactoring suggestions (move misplaced
      functions, remove dead code).

    Args:
        mode: Operation mode: "rename", "dead_code", or "suggest".
        old_name: (rename) Current symbol name to rename.
        new_name: (rename) Desired new name for the symbol.
        kind: (dead_code) Optional filter: Function or Class.
        file_pattern: (dead_code) Filter by file path substring.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return refactor_func(
        mode=mode, old_name=old_name, new_name=new_name,
        kind=kind, file_pattern=file_pattern, repo_root=_resolve_repo_root(repo_root),
    )


@mcp.tool()
def apply_refactor_tool(
    refactor_id: str,
    repo_root: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """Apply a previously previewed refactoring to source files.

    Takes a refactor_id from a prior refactor_tool(mode="rename") call and
    applies the exact string replacements to the target files. Previews
    expire after 10 minutes.

    Security: All edit paths are validated to be within the repo root.
    Only exact string replacements are performed (no regex, no eval).

    Args:
        refactor_id: The refactor ID from refactor_tool's response.
        repo_root: Repository root path. Auto-detected if omitted.
        dry_run: If True, return a unified diff of what would change
            without touching any files. The refactor_id remains valid so
            the same preview can be applied in a follow-up call without
            dry_run. Use this for a human-in-the-loop review before
            committing changes to disk. See: #176
    """
    return apply_refactor_func(
        refactor_id=refactor_id, repo_root=_resolve_repo_root(repo_root),
        dry_run=dry_run,
    )


@mcp.tool()
async def generate_wiki_tool(
    repo_root: Optional[str] = None,
    force: bool = False,
) -> dict:
    """Generate a markdown wiki from the code community structure.

    Creates a wiki page for each detected community and an index page.
    Pages are written to .code-review-graph/wiki/ inside the repository.
    Only regenerates pages whose content has changed unless force=True.

    Offloaded to a thread via ``asyncio.to_thread`` — on large graphs
    the page-generation loop touches every community and issues many
    SQLite reads, which would block the MCP event loop. See: #46, #136.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
        force: If True, regenerate all pages even if content unchanged. Default: False.
    """
    return await asyncio.to_thread(
        generate_wiki_func,
        repo_root=_resolve_repo_root(repo_root),
        force=force,
    )


@mcp.tool()
def get_wiki_page_tool(
    community_name: str,
    repo_root: Optional[str] = None,
) -> dict:
    """Retrieve a specific wiki page by community name.

    Returns the markdown content of the wiki page for the given community.
    The wiki must have been generated first via generate_wiki_tool.

    Args:
        community_name: Community name to look up.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return get_wiki_page_func(
        community_name=community_name, repo_root=_resolve_repo_root(repo_root),
    )


@mcp.tool()
def get_hub_nodes_tool(
    top_n: int = 10,
    repo_root: Optional[str] = None,
) -> dict:
    """Find the most connected nodes in the codebase (architectural hotspots).

    Hub nodes have the highest total degree (in + out edges). Changes to
    them have disproportionate blast radius. Excludes File nodes.

    Args:
        top_n: Number of top hubs to return. Default: 10.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return get_hub_nodes_func(
        repo_root=_resolve_repo_root(repo_root), top_n=top_n,
    )


@mcp.tool()
def get_bridge_nodes_tool(
    top_n: int = 10,
    repo_root: Optional[str] = None,
) -> dict:
    """Find architectural chokepoints via betweenness centrality.

    Bridge nodes sit on shortest paths between many node pairs.
    If they break, multiple code regions lose connectivity.
    Uses sampling approximation for graphs > 5000 nodes.

    Args:
        top_n: Number of top bridges to return. Default: 10.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return get_bridge_nodes_func(
        repo_root=_resolve_repo_root(repo_root), top_n=top_n,
    )


@mcp.tool()
def get_knowledge_gaps_tool(
    repo_root: Optional[str] = None,
) -> dict:
    """Identify structural weaknesses in the codebase graph.

    Finds isolated nodes (disconnected), thin communities (< 3 members),
    untested hotspots (high-degree nodes without test coverage), and
    single-file communities.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return get_knowledge_gaps_func(
        repo_root=_resolve_repo_root(repo_root),
    )


@mcp.tool()
def get_surprising_connections_tool(
    top_n: int = 15,
    repo_root: Optional[str] = None,
) -> dict:
    """Find unexpected architectural coupling via composite surprise scoring.

    Scores edges by: cross-community (+0.3), cross-language (+0.2),
    peripheral-to-hub (+0.2), cross-test-boundary (+0.15), and
    unusual edge kinds (+0.15).

    Args:
        top_n: Number of top surprises to return. Default: 15.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return get_surprising_connections_func(
        repo_root=_resolve_repo_root(repo_root), top_n=top_n,
    )


@mcp.tool()
def get_suggested_questions_tool(
    repo_root: Optional[str] = None,
) -> dict:
    """Auto-generate review questions from graph analysis.

    Produces prioritized questions about: bridge nodes needing tests,
    untested hub nodes, surprising cross-community coupling, thin
    communities, and untested hotspots.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return get_suggested_questions_func(
        repo_root=_resolve_repo_root(repo_root),
    )


@mcp.tool()
def traverse_graph_tool(
    query: str,
    mode: str = "bfs",
    depth: int = 3,
    token_budget: int = 2000,
    repo_root: Optional[str] = None,
) -> dict:
    """BFS/DFS traversal from best-matching node with token budget.

    Free-form graph exploration: finds the node best matching your
    query, then traverses outward via BFS or DFS up to the given
    depth, collecting connected nodes within the token budget.

    Args:
        query: Search string to find the starting node.
        mode: Traversal mode: "bfs" (breadth-first) or "dfs"
            (depth-first). Default: bfs.
        depth: Max traversal depth (1-6). Default: 3.
        token_budget: Approximate token limit for results.
            Default: 2000.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return traverse_graph_func(
        query=query, mode=mode, depth=depth,
        token_budget=token_budget,
        repo_root=_resolve_repo_root(repo_root) or "",
    )


@mcp.tool()
def list_repos_tool() -> dict:
    """List all registered repositories in the multi-repo registry.

    Returns the list of repos registered at ~/.code-review-graph/registry.json.
    Use the CLI 'register' command to add repos.
    """
    return list_repos_func()


@mcp.tool()
def cross_repo_search_tool(
    query: str,
    kind: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """Search for code entities across all registered repositories.

    Runs hybrid search on each registered repo's graph database and merges
    the results by score. Register repos first with the CLI 'register' command.

    Args:
        query: Search string to match against node names.
        kind: Optional filter: File, Class, Function, Type, or Test.
        limit: Maximum results per repo. Default: 20.
    """
    return cross_repo_search_func(query=query, kind=kind, limit=limit)


@mcp.prompt()
def review_changes(base: str = "HEAD~1") -> list[dict]:
    """Pre-commit review workflow using detect_changes, affected_flows, and test gaps.

    Produces a structured code review with risk levels and actionable findings.

    Args:
        base: Git ref to diff against. Default: HEAD~1.
    """
    return review_changes_prompt(base=base)


@mcp.prompt()
def architecture_map() -> list[dict]:
    """Architecture documentation using communities, flows, and Mermaid diagrams.

    Generates a comprehensive architecture map with module summaries and coupling warnings.
    """
    return architecture_map_prompt()


@mcp.prompt()
def debug_issue(description: str = "") -> list[dict]:
    """Guided debugging using search, flow tracing, and recent changes.

    Systematic debugging workflow that traces execution paths and identifies root causes.

    Args:
        description: Description of the issue to debug.
    """
    return debug_issue_prompt(description=description)


@mcp.prompt()
def onboard_developer() -> list[dict]:
    """New developer orientation using stats, architecture, and critical flows.

    Creates an onboarding guide covering codebase structure, key modules, and patterns.
    """
    return onboard_developer_prompt()


@mcp.prompt()
def pre_merge_check(base: str = "HEAD~1") -> list[dict]:
    """PR readiness check with risk scoring, test gaps, and dead code detection.

    Produces a merge readiness report with risk assessment and recommendations.

    Args:
        base: Git ref to diff against. Default: HEAD~1.
    """
    return pre_merge_check_prompt(base=base)


def _apply_tool_filter(tools: str | None = None) -> None:
    """Remove tools not listed in the allow-list.

    Accepts a comma-separated string of tool names to keep.  When set,
    every registered MCP tool whose name is **not** in the list is
    removed via ``FastMCP.remove_tool()``.

    The allow-list can be supplied in two ways (first match wins):

    1. ``tools`` argument (from ``serve --tools ...``).
    2. ``CRG_TOOLS`` environment variable.

    When neither is set, all tools remain available.

    This is useful for token-constrained environments: CRG exposes 28+
    tools by default (~8k description tokens per LLM turn).  Filtering
    to a working set of 5-10 tools can reduce overhead by 70-85%.

    Example::

        # via CLI
        code-review-graph serve --tools query_graph_tool,semantic_search_nodes_tool

        # via env var
        CRG_TOOLS=query_graph_tool,semantic_search_nodes_tool
    """
    import asyncio
    import os

    raw = tools or os.environ.get("CRG_TOOLS")
    if not raw:
        return
    allowed = {t.strip() for t in raw.split(",") if t.strip()}
    if not allowed:
        return
    # FastMCP >=3 exposes tool enumeration via the async ``list_tools``
    # method.  ``_apply_tool_filter`` is typically called from
    # ``main()`` before the MCP event loop starts, but tests may invoke
    # it from within a running event loop — in that case ``asyncio.run``
    # raises ``RuntimeError``.  Fall back to running the coroutine on a
    # dedicated short-lived loop in a worker thread.  Earlier code path
    # relied on ``mcp._tool_manager._tools`` which is a private
    # attribute that was removed in fastmcp>=3.0.
    def _list_tool_names() -> list[str]:
        coro_factory = mcp.list_tools
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return [t.name for t in asyncio.run(coro_factory())]
        import concurrent.futures

        def _runner() -> list[str]:
            return [t.name for t in asyncio.run(coro_factory())]

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_runner).result()

    for name in _list_tool_names():
        if name not in allowed:
            mcp.local_provider.remove_tool(name)



def main(
    repo_root: str | None = None,
    tools: str | None = None,
    auto_watch: bool = False,
    *,
    transport: str = "stdio",
    host: str | None = None,
    port: int | None = None,
) -> None:
    """Run the MCP server (stdio or HTTP).

    On Windows, Python 3.8+ defaults to ``ProactorEventLoop``, which
    interacts poorly with ``concurrent.futures.ProcessPoolExecutor``
    (used by ``full_build``) over a stdio MCP transport — the combination
    produces silent hangs on ``build_or_update_graph_tool`` and
    ``embed_graph_tool``. Switching to ``WindowsSelectorEventLoopPolicy``
    before fastmcp starts its loop avoids the deadlock.
    See: #46, #136

    Args:
        repo_root: Default repository root for all tool calls.
        tools: Comma-separated list of tool names to expose.
            Falls back to ``CRG_TOOLS`` env var.  When unset, all
            tools are available.
        auto_watch: Start filesystem watcher in a background daemon thread
            while the MCP server runs.
        transport: ``"stdio"`` (default) or ``"streamable-http"`` for local HTTP.
        host: Bind address when using HTTP (required for HTTP; set by CLI).
        port: Port when using HTTP (required for HTTP; set by CLI).
    """
    global _default_repo_root
    root = Path(repo_root) if repo_root else find_project_root()
    _default_repo_root = str(root)
    _apply_tool_filter(tools)

    watch_store: GraphStore | None = None
    if auto_watch:
        watch_store = GraphStore(get_db_path(root))
        thread = start_watch_thread(root, watch_store, daemon=True)
        if thread is None:
            logger.warning("Auto-watch was requested but could not be started")

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        if transport == "stdio":
            # Stdio MCP must keep stdout strictly JSON-RPC. FastMCP's banner/update
            # notices corrupt the handshake stream on clients like Codex CLI.
            mcp.run(transport="stdio", show_banner=False)
        elif transport == "streamable-http":
            if host is None or port is None:
                raise ValueError("streamable-http transport requires host and port")
            mcp.run(transport="streamable-http", host=host, port=port)
        else:
            raise ValueError(f"unsupported transport: {transport!r}")
    finally:
        if watch_store is not None:
            watch_store.close()


if __name__ == "__main__":
    main()
