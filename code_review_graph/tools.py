"""MCP tool definitions for the Code Review Graph server.

Exposes 22 tools:
1. build_or_update_graph  - full or incremental build
2. get_impact_radius      - blast radius from changed files
3. query_graph            - predefined graph queries
4. get_review_context     - focused subgraph + review prompt
5. semantic_search_nodes  - keyword + vector search across nodes
6. list_graph_stats       - aggregate statistics
7. embed_graph            - compute vector embeddings for semantic search
8. get_docs_section       - token-optimized documentation retrieval
9. find_large_functions   - find oversized functions/classes by line count
10. list_flows            - list execution flows sorted by criticality
11. get_flow              - get details of a single execution flow
12. get_affected_flows    - find flows affected by changed files
13. list_communities      - list detected code communities
14. get_community         - get details of a single community
15. get_architecture_overview - architecture overview from community structure
16. detect_changes        - risk-scored change impact analysis for code review
17. refactor_tool         - unified refactoring (rename preview, dead code, suggestions)
18. apply_refactor_tool   - apply a previously previewed refactoring
19. generate_wiki         - generate markdown wiki from community structure
20. get_wiki_page         - retrieve a specific wiki page
21. list_repos            - list registered repositories
22. cross_repo_search     - search across all registered repositories
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .changes import analyze_changes, parse_git_diff_ranges
from .communities import get_architecture_overview, get_communities
from .embeddings import EmbeddingStore, embed_all_nodes
from .flows import get_affected_flows as _get_affected_flows
from .flows import get_flow_by_id, get_flows
from .graph import GraphStore, edge_to_dict, node_to_dict
from .hints import generate_hints, get_session
from .incremental import (
    find_project_root,
    full_build,
    get_changed_files,
    get_db_path,
    get_staged_and_unstaged,
    incremental_update,
)
from .refactor import apply_refactor, find_dead_code, rename_preview, suggest_refactorings
from .search import hybrid_search

logger = logging.getLogger(__name__)

# Common JS/TS builtin method names filtered from callers_of results.
# "Who calls .map()?" returns hundreds of hits and is never useful.
# These are kept in the graph (callees_of still shows them) but excluded
# when doing reverse call tracing to reduce noise.
_BUILTIN_CALL_NAMES: set[str] = {
    "map", "filter", "reduce", "reduceRight", "forEach", "find", "findIndex",
    "some", "every", "includes", "indexOf", "lastIndexOf",
    "push", "pop", "shift", "unshift", "splice", "slice",
    "concat", "join", "flat", "flatMap", "sort", "reverse", "fill",
    "keys", "values", "entries", "from", "isArray", "of", "at",
    "trim", "trimStart", "trimEnd", "split", "replace", "replaceAll",
    "match", "matchAll", "search", "substring", "substr",
    "toLowerCase", "toUpperCase", "startsWith", "endsWith",
    "padStart", "padEnd", "repeat", "charAt", "charCodeAt",
    "assign", "freeze", "defineProperty", "getOwnPropertyNames",
    "hasOwnProperty", "create", "is", "fromEntries",
    "log", "warn", "error", "info", "debug", "trace", "dir", "table",
    "time", "timeEnd", "assert", "clear", "count",
    "then", "catch", "finally", "resolve", "reject", "all", "allSettled", "race", "any",
    "parse", "stringify",
    "floor", "ceil", "round", "random", "max", "min", "abs", "pow", "sqrt",
    "addEventListener", "removeEventListener", "querySelector", "querySelectorAll",
    "getElementById", "createElement", "appendChild", "removeChild",
    "setAttribute", "getAttribute", "preventDefault", "stopPropagation",
    "setTimeout", "clearTimeout", "setInterval", "clearInterval",
    "toString", "valueOf", "toJSON", "toISOString",
    "getTime", "getFullYear", "now",
    "isNaN", "parseInt", "parseFloat", "toFixed",
    "encodeURIComponent", "decodeURIComponent",
    "call", "apply", "bind", "next",
    "emit", "on", "off", "once",
    "pipe", "write", "read", "end", "close", "destroy",
    "send", "status", "json", "redirect",
    "set", "get", "delete", "has",
    "findUnique", "findFirst", "findMany", "createMany",
    "update", "updateMany", "deleteMany", "upsert",
    "aggregate", "groupBy", "transaction",
    "describe", "it", "test", "expect", "beforeEach", "afterEach",
    "beforeAll", "afterAll", "mock", "spyOn",
    "require", "fetch",
}


def _validate_repo_root(path: Path) -> Path:
    """Validate that a path is a plausible project root.

    Ensures the path is an existing directory that contains a ``.git``
    or ``.code-review-graph`` directory, preventing arbitrary file-system
    traversal via the ``repo_root`` parameter.
    """
    resolved = path.resolve()
    if not resolved.is_dir():
        raise ValueError(
            f"repo_root is not an existing directory: {resolved}"
        )
    if not (resolved / ".git").exists() and not (resolved / ".code-review-graph").exists():
        raise ValueError(
            f"repo_root does not look like a project root (no .git or "
            f".code-review-graph directory found): {resolved}"
        )
    return resolved


def _get_store(repo_root: str | None = None) -> tuple[GraphStore, Path]:
    """Resolve repo root and open the graph store."""
    root = _validate_repo_root(Path(repo_root)) if repo_root else find_project_root()
    db_path = get_db_path(root)
    return GraphStore(db_path), root


# ---------------------------------------------------------------------------
# Tool 1: build_or_update_graph
# ---------------------------------------------------------------------------


def build_or_update_graph(
    full_rebuild: bool = False,
    repo_root: str | None = None,
    base: str = "HEAD~1",
) -> dict[str, Any]:
    """Build or incrementally update the code knowledge graph.

    Args:
        full_rebuild: If True, re-parse every file. If False (default),
                      only re-parse files changed since `base`.
        repo_root: Path to the repository root. Auto-detected if omitted.
        base: Git ref for incremental diff (default: HEAD~1).

    Returns:
        Summary with files_parsed/updated, node/edge counts, and errors.
    """
    store, root = _get_store(repo_root)
    try:
        if full_rebuild:
            result = full_build(root, store)
            build_result = {
                "status": "ok",
                "build_type": "full",
                "summary": (
                    f"Full build complete: parsed {result['files_parsed']} files, "
                    f"created {result['total_nodes']} nodes and {result['total_edges']} edges."
                ),
                **result,
            }
        else:
            result = incremental_update(root, store, base=base)
            if result["files_updated"] == 0:
                return {
                    "status": "ok",
                    "build_type": "incremental",
                    "summary": "No changes detected. Graph is up to date.",
                    **result,
                }
            build_result = {
                "status": "ok",
                "build_type": "incremental",
                "summary": (
                    f"Incremental update: {result['files_updated']} files re-parsed, "
                    f"{result['total_nodes']} nodes and {result['total_edges']} edges updated. "
                    f"Changed: {result['changed_files']}. "
                    f"Dependents also updated: {result['dependent_files']}."
                ),
                **result,
            }

        # -- Post-build: compute signatures for nodes that don't have them --
        try:
            rows = store._conn.execute(
                "SELECT id, name, kind, params, return_type "
                "FROM nodes WHERE signature IS NULL"
            ).fetchall()
            for row in rows:
                node_id, name, kind, params, ret = (
                    row[0], row[1], row[2], row[3], row[4],
                )
                if kind in ("Function", "Test"):
                    sig = f"def {name}({params or ''})"
                    if ret:
                        sig += f" -> {ret}"
                elif kind == "Class":
                    sig = f"class {name}"
                else:
                    sig = name
                store._conn.execute(
                    "UPDATE nodes SET signature = ? WHERE id = ?",
                    (sig[:512], node_id),
                )
            store._conn.commit()
        except Exception as e:
            logger.warning("Signature computation failed: %s", e)

        # -- Post-build: rebuild FTS index --
        try:
            from code_review_graph.search import rebuild_fts_index

            fts_count = rebuild_fts_index(store)
            build_result["fts_indexed"] = fts_count
        except Exception as e:
            logger.warning("FTS index rebuild failed: %s", e)

        # -- Post-build: trace execution flows --
        try:
            from code_review_graph.flows import store_flows as _store_flows
            from code_review_graph.flows import trace_flows as _trace_flows

            flows = _trace_flows(store)
            count = _store_flows(store, flows)
            build_result["flows_detected"] = count
        except Exception as e:
            logger.warning("Flow detection failed: %s", e)

        # -- Post-build: detect communities --
        try:
            from code_review_graph.communities import (
                detect_communities as _detect_communities,
            )
            from code_review_graph.communities import (
                store_communities as _store_communities,
            )

            comms = _detect_communities(store)
            count = _store_communities(store, comms)
            build_result["communities_detected"] = count
        except Exception as e:
            logger.warning("Community detection failed: %s", e)

        return build_result
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 2: get_impact_radius
# ---------------------------------------------------------------------------


def get_impact_radius(
    changed_files: list[str] | None = None,
    max_depth: int = 2,
    max_results: int = 500,
    repo_root: str | None = None,
    base: str = "HEAD~1",
) -> dict[str, Any]:
    """Analyze the blast radius of changed files.

    Args:
        changed_files: Explicit list of changed file paths (relative to repo root).
                       If omitted, auto-detects from git diff.
        max_depth: How many hops to traverse in the graph (default: 2).
        max_results: Maximum impacted nodes to return (default: 500).
        repo_root: Repository root path. Auto-detected if omitted.
        base: Git ref for auto-detecting changes (default: HEAD~1).

    Returns:
        Changed nodes, impacted nodes, impacted files, connecting edges,
        plus ``truncated`` flag and ``total_impacted`` count.
    """
    store, root = _get_store(repo_root)
    try:
        if changed_files is None:
            changed_files = get_changed_files(root, base)
            if not changed_files:
                changed_files = get_staged_and_unstaged(root)

        if not changed_files:
            return {
                "status": "ok",
                "summary": "No changed files detected.",
                "changed_nodes": [],
                "impacted_nodes": [],
                "impacted_files": [],
                "truncated": False,
                "total_impacted": 0,
            }

        # Convert to absolute paths for graph lookup
        abs_files = [str(root / f) for f in changed_files]
        result = store.get_impact_radius(
            abs_files, max_depth=max_depth, max_nodes=max_results
        )

        changed_dicts = [node_to_dict(n) for n in result["changed_nodes"]]
        impacted_dicts = [node_to_dict(n) for n in result["impacted_nodes"]]
        edge_dicts = [edge_to_dict(e) for e in result["edges"]]
        truncated = result["truncated"]
        total_impacted = result["total_impacted"]

        summary_parts = [
            f"Blast radius for {len(changed_files)} changed file(s):",
            f"  - {len(changed_dicts)} nodes directly changed",
            f"  - {len(impacted_dicts)} nodes impacted (within {max_depth} hops)",
            f"  - {len(result['impacted_files'])} additional files affected",
        ]
        if truncated:
            summary_parts.append(
                f"  - Results truncated: showing {len(impacted_dicts)}"
                f" of {total_impacted} impacted nodes"
            )

        return {
            "status": "ok",
            "summary": "\n".join(summary_parts),
            "changed_files": changed_files,
            "changed_nodes": changed_dicts,
            "impacted_nodes": impacted_dicts,
            "impacted_files": result["impacted_files"],
            "edges": edge_dicts,
            "truncated": truncated,
            "total_impacted": total_impacted,
        }
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 3: query_graph
# ---------------------------------------------------------------------------

_QUERY_PATTERNS = {
    "callers_of": "Find all functions that call a given function",
    "callees_of": "Find all functions called by a given function",
    "imports_of": "Find all imports of a given file or module",
    "importers_of": "Find all files that import a given file or module",
    "children_of": "Find all nodes contained in a file or class",
    "tests_for": "Find all tests for a given function or class",
    "inheritors_of": "Find all classes that inherit from a given class",
    "file_summary": "Get a summary of all nodes in a file",
}


def query_graph(
    pattern: str,
    target: str,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Run a predefined graph query.

    Args:
        pattern: Query pattern. One of: callers_of, callees_of, imports_of,
                 importers_of, children_of, tests_for, inheritors_of, file_summary.
        target: The node name, qualified name, or file path to query about.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Matching nodes and edges for the query.
    """
    store, root = _get_store(repo_root)
    try:
        if pattern not in _QUERY_PATTERNS:
            return {
                "status": "error",
                "error": f"Unknown pattern '{pattern}'. Available: {list(_QUERY_PATTERNS.keys())}",
            }

        results: list[dict] = []
        edges_out: list[dict] = []

        # For callers_of, skip common builtins early (bare names only)
        # "Who calls .map()?" returns hundreds of useless hits.
        # Qualified names (e.g. "utils.py::map") bypass this filter.
        if pattern == "callers_of" and target in _BUILTIN_CALL_NAMES and "::" not in target:
            return {
                "status": "ok", "pattern": pattern, "target": target,
                "description": _QUERY_PATTERNS[pattern],
                "summary": f"'{target}' is a common builtin — callers_of skipped to avoid noise.",
                "results": [], "edges": [],
            }

        # Resolve target - try as-is, then as absolute path, then search
        node = store.get_node(target)
        if not node:
            abs_target = str(root / target)
            node = store.get_node(abs_target)
        if not node:
            # Search by name
            candidates = store.search_nodes(target, limit=5)
            if len(candidates) == 1:
                node = candidates[0]
                target = node.qualified_name
            elif len(candidates) > 1:
                return {
                    "status": "ambiguous",
                    "summary": f"Multiple matches for '{target}'. Please use a qualified name.",
                    "candidates": [node_to_dict(c) for c in candidates],
                }

        if not node and pattern != "file_summary":
            return {
                "status": "not_found",
                "summary": f"No node found matching '{target}'.",
            }

        qn = node.qualified_name if node else target

        if pattern == "callers_of":
            for e in store.get_edges_by_target(qn):
                if e.kind == "CALLS":
                    caller = store.get_node(e.source_qualified)
                    if caller:
                        results.append(node_to_dict(caller))
                    edges_out.append(edge_to_dict(e))
            # Fallback: CALLS edges store unqualified target names
            # (e.g. "generateTestCode") while qn is fully qualified
            # (e.g. "file.ts::generateTestCode"). Search by plain name too.
            if not results and node:
                for e in store.search_edges_by_target_name(node.name):
                    caller = store.get_node(e.source_qualified)
                    if caller:
                        results.append(node_to_dict(caller))
                    edges_out.append(edge_to_dict(e))

        elif pattern == "callees_of":
            for e in store.get_edges_by_source(qn):
                if e.kind == "CALLS":
                    callee = store.get_node(e.target_qualified)
                    if callee:
                        results.append(node_to_dict(callee))
                    edges_out.append(edge_to_dict(e))

        elif pattern == "imports_of":
            for e in store.get_edges_by_source(qn):
                if e.kind == "IMPORTS_FROM":
                    results.append({"import_target": e.target_qualified})
                    edges_out.append(edge_to_dict(e))

        elif pattern == "importers_of":
            # Find edges where target matches this file
            abs_target = str(root / target) if node is None else node.file_path
            for e in store.get_edges_by_target(abs_target):
                if e.kind == "IMPORTS_FROM":
                    results.append({"importer": e.source_qualified, "file": e.file_path})
                    edges_out.append(edge_to_dict(e))

        elif pattern == "children_of":
            for e in store.get_edges_by_source(qn):
                if e.kind == "CONTAINS":
                    child = store.get_node(e.target_qualified)
                    if child:
                        results.append(node_to_dict(child))

        elif pattern == "tests_for":
            for e in store.get_edges_by_target(qn):
                if e.kind == "TESTED_BY":
                    test = store.get_node(e.source_qualified)
                    if test:
                        results.append(node_to_dict(test))
            # Also search by naming convention
            name = node.name if node else target
            test_nodes = store.search_nodes(f"test_{name}", limit=10)
            test_nodes += store.search_nodes(f"Test{name}", limit=10)
            seen = {r.get("qualified_name") for r in results}
            for t in test_nodes:
                if t.qualified_name not in seen and t.is_test:
                    results.append(node_to_dict(t))

        elif pattern == "inheritors_of":
            for e in store.get_edges_by_target(qn):
                if e.kind in ("INHERITS", "IMPLEMENTS"):
                    child = store.get_node(e.source_qualified)
                    if child:
                        results.append(node_to_dict(child))
                    edges_out.append(edge_to_dict(e))

        elif pattern == "file_summary":
            abs_path = str(root / target)
            file_nodes = store.get_nodes_by_file(abs_path)
            for n in file_nodes:
                results.append(node_to_dict(n))

        return {
            "status": "ok",
            "pattern": pattern,
            "target": target,
            "description": _QUERY_PATTERNS[pattern],
            "summary": f"Found {len(results)} result(s) for {pattern}('{target}')",
            "results": results,
            "edges": edges_out,
        }
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 4: get_review_context
# ---------------------------------------------------------------------------


def get_review_context(
    changed_files: list[str] | None = None,
    max_depth: int = 2,
    include_source: bool = True,
    max_lines_per_file: int = 200,
    repo_root: str | None = None,
    base: str = "HEAD~1",
) -> dict[str, Any]:
    """Generate a focused review context from changed files.

    Builds a token-optimized subgraph + source snippets for code review.

    Args:
        changed_files: Files to review (auto-detected from git diff if omitted).
        max_depth: Impact radius depth (default: 2).
        include_source: Whether to include source code snippets (default: True).
        max_lines_per_file: Max source lines per file in output (default: 200).
        repo_root: Repository root path. Auto-detected if omitted.
        base: Git ref for change detection (default: HEAD~1).

    Returns:
        Structured review context with subgraph, source snippets, and review guidance.
    """
    store, root = _get_store(repo_root)
    try:
        # Get impact radius first
        if changed_files is None:
            changed_files = get_changed_files(root, base)
            if not changed_files:
                changed_files = get_staged_and_unstaged(root)

        if not changed_files:
            return {
                "status": "ok",
                "summary": "No changes detected. Nothing to review.",
                "context": {},
            }

        abs_files = [str(root / f) for f in changed_files]
        impact = store.get_impact_radius(abs_files, max_depth=max_depth)

        # Build review context
        context: dict[str, Any] = {
            "changed_files": changed_files,
            "impacted_files": impact["impacted_files"],
            "graph": {
                "changed_nodes": [node_to_dict(n) for n in impact["changed_nodes"]],
                "impacted_nodes": [node_to_dict(n) for n in impact["impacted_nodes"]],
                "edges": [edge_to_dict(e) for e in impact["edges"]],
            },
        }

        # Add source snippets for changed files
        if include_source:
            snippets = {}
            for rel_path in changed_files:
                full_path = root / rel_path
                if full_path.is_file():
                    try:
                        lines = full_path.read_text(errors="replace").splitlines()
                        if len(lines) > max_lines_per_file:
                            # Include only the relevant functions/classes
                            relevant_lines = _extract_relevant_lines(
                                lines, impact["changed_nodes"], str(full_path)
                            )
                            snippets[rel_path] = relevant_lines
                        else:
                            snippets[rel_path] = "\n".join(
                                f"{i+1}: {line}" for i, line in enumerate(lines)
                            )
                    except (OSError, UnicodeDecodeError):
                        snippets[rel_path] = "(could not read file)"
            context["source_snippets"] = snippets

        # Generate review guidance
        guidance = _generate_review_guidance(impact, changed_files)
        context["review_guidance"] = guidance

        summary_parts = [
            f"Review context for {len(changed_files)} changed file(s):",
            f"  - {len(impact['changed_nodes'])} directly changed nodes",
            f"  - {len(impact['impacted_nodes'])} impacted nodes"
            f" in {len(impact['impacted_files'])} files",
            "",
            "Review guidance:",
            guidance,
        ]

        return {
            "status": "ok",
            "summary": "\n".join(summary_parts),
            "context": context,
        }
    finally:
        store.close()


def _extract_relevant_lines(
    lines: list[str], nodes: list, file_path: str
) -> str:
    """Extract only the lines relevant to changed nodes."""
    ranges = []
    for n in nodes:
        if n.file_path == file_path:
            start = max(0, n.line_start - 3)  # 2 lines context before
            end = min(len(lines), n.line_end + 2)  # 1 line context after
            ranges.append((start, end))

    if not ranges:
        # Show first N lines as fallback
        return "\n".join(f"{i+1}: {line}" for i, line in enumerate(lines[:50]))

    # Merge overlapping ranges
    ranges.sort()
    merged = [ranges[0]]
    for start, end in ranges[1:]:
        if start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    parts: list[str] = []
    for start, end in merged:
        if parts:
            parts.append("...")
        for i in range(start, end):
            parts.append(f"{i+1}: {lines[i]}")

    return "\n".join(parts)


def _generate_review_guidance(impact: dict, changed_files: list[str]) -> str:
    """Generate review guidance based on the impact analysis."""
    guidance_parts = []

    # Check for test coverage
    changed_funcs = [
        n for n in impact["changed_nodes"] if n.kind == "Function"
    ]
    test_edges = [e for e in impact["edges"] if e.kind == "TESTED_BY"]
    tested_funcs = {e.source_qualified for e in test_edges}

    untested = [
        f for f in changed_funcs
        if f.qualified_name not in tested_funcs and not f.is_test
    ]
    if untested:
        guidance_parts.append(
            f"- {len(untested)} changed function(s) lack test coverage: "
            + ", ".join(n.name for n in untested[:5])
        )

    # Check for wide blast radius
    if len(impact["impacted_nodes"]) > 20:
        guidance_parts.append(
            f"- Wide blast radius: {len(impact['impacted_nodes'])} nodes impacted. "
            "Review callers and dependents carefully."
        )

    # Check for inheritance changes
    inheritance_edges = [e for e in impact["edges"] if e.kind in ("INHERITS", "IMPLEMENTS")]
    if inheritance_edges:
        guidance_parts.append(
            f"- {len(inheritance_edges)} inheritance/implementation relationship(s) affected. "
            "Check for Liskov substitution violations."
        )

    # Check for cross-file impact
    impacted_file_count = len(impact["impacted_files"])
    if impacted_file_count > 3:
        guidance_parts.append(
            f"- Changes impact {impacted_file_count} other files."
            " Consider splitting into smaller PRs."
        )

    if not guidance_parts:
        guidance_parts.append("- Changes appear well-contained with minimal blast radius.")

    return "\n".join(guidance_parts)


# ---------------------------------------------------------------------------
# Tool 5: semantic_search_nodes
# ---------------------------------------------------------------------------


def semantic_search_nodes(
    query: str,
    kind: str | None = None,
    limit: int = 20,
    repo_root: str | None = None,
    context_files: list[str] | None = None,
) -> dict[str, Any]:
    """Search for nodes by name, keyword, or semantic similarity.

    Uses hybrid search (FTS5 BM25 + vector embeddings merged via Reciprocal
    Rank Fusion) as the primary search path, with graceful fallback to
    keyword matching.

    Args:
        query: Search string to match against node names and qualified names.
        kind: Optional filter by node kind (File, Class, Function, Type, Test).
        limit: Maximum results to return (default: 20).
        repo_root: Repository root path. Auto-detected if omitted.
        context_files: Optional list of file paths. Nodes in these files
            receive a relevance boost.

    Returns:
        Ranked list of matching nodes.
    """
    store, root = _get_store(repo_root)
    try:
        results = hybrid_search(
            store, query, kind=kind, limit=limit, context_files=context_files,
        )

        search_mode = "hybrid"
        if not results:
            search_mode = "keyword"

        result = {
            "status": "ok",
            "query": query,
            "search_mode": search_mode,
            "summary": f"Found {len(results)} node(s) matching '{query}'" + (
                f" (kind={kind})" if kind else ""
            ),
            "results": results,
        }
        result["_hints"] = generate_hints(
            "semantic_search_nodes", result, get_session()
        )
        return result
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 6: list_graph_stats
# ---------------------------------------------------------------------------


def list_graph_stats(repo_root: str | None = None) -> dict[str, Any]:
    """Get aggregate statistics about the knowledge graph.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Total nodes, edges, breakdown by kind, languages, and last update time.
    """
    store, root = _get_store(repo_root)
    try:
        stats = store.get_stats()

        summary_parts = [
            f"Graph statistics for {root.name}:",
            f"  Files: {stats.files_count}",
            f"  Total nodes: {stats.total_nodes}",
            f"  Total edges: {stats.total_edges}",
            f"  Languages: {', '.join(stats.languages) if stats.languages else 'none'}",
            f"  Last updated: {stats.last_updated or 'never'}",
            "",
            "Nodes by kind:",
        ]
        for kind, count in sorted(stats.nodes_by_kind.items()):
            summary_parts.append(f"  {kind}: {count}")
        summary_parts.append("")
        summary_parts.append("Edges by kind:")
        for kind, count in sorted(stats.edges_by_kind.items()):
            summary_parts.append(f"  {kind}: {count}")

        # Add embedding info if available
        emb_store = EmbeddingStore(get_db_path(root))
        try:
            emb_count = emb_store.count()
            summary_parts.append("")
            summary_parts.append(f"Embeddings: {emb_count} nodes embedded")
            if not emb_store.available:
                summary_parts.append("  (install sentence-transformers for semantic search)")
        finally:
            emb_store.close()

        return {
            "status": "ok",
            "summary": "\n".join(summary_parts),
            "total_nodes": stats.total_nodes,
            "total_edges": stats.total_edges,
            "nodes_by_kind": stats.nodes_by_kind,
            "edges_by_kind": stats.edges_by_kind,
            "languages": stats.languages,
            "files_count": stats.files_count,
            "last_updated": stats.last_updated,
            "embeddings_count": emb_count,
        }
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 7: embed_graph
# ---------------------------------------------------------------------------


def embed_graph(repo_root: str | None = None) -> dict[str, Any]:
    """Compute vector embeddings for all graph nodes to enable semantic search.

    Requires: `pip install code-review-graph[embeddings]`
    Uses the all-MiniLM-L6-v2 model (fast, 384-dim).

    Only embeds nodes that don't already have up-to-date embeddings.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Number of nodes embedded and total embedding count.
    """
    store, root = _get_store(repo_root)
    db_path = get_db_path(root)
    emb_store = EmbeddingStore(db_path)
    try:
        if not emb_store.available:
            return {
                "status": "error",
                "error": (
                    "sentence-transformers is not installed. "
                    "Install with: pip install code-review-graph[embeddings]"
                ),
            }

        newly_embedded = embed_all_nodes(store, emb_store)
        total = emb_store.count()

        return {
            "status": "ok",
            "summary": (
                f"Embedded {newly_embedded} new node(s). "
                f"Total embeddings: {total}. "
                "Semantic search is now active."
            ),
            "newly_embedded": newly_embedded,
            "total_embeddings": total,
        }
    finally:
        emb_store.close()
        store.close()


# ---------------------------------------------------------------------------
# Tool 8: get_docs_section
# ---------------------------------------------------------------------------

def get_docs_section(section_name: str, repo_root: str | None = None) -> dict[str, Any]:
    """Return a specific section from the LLM-optimized reference.

    Used by skills and Claude Code to load only the exact documentation
    section needed, keeping token usage minimal (90%+ savings).

    Args:
        section_name: Exact section name. One of: usage, review-delta,
                      review-pr, commands, legal, watch, embeddings,
                      languages, troubleshooting.
        repo_root: Repository root path. Auto-detected from current directory if omitted.

    Returns:
        The section content, or an error if not found.
    """
    import re as _re

    search_roots: list[Path] = []

    if repo_root:
        search_roots.append(Path(repo_root))

    try:
        _, root = _get_store(repo_root)
        if root not in search_roots:
            search_roots.append(root)
    except (RuntimeError, ValueError):
        pass

    for search_root in search_roots:
        candidate = search_root / "docs" / "LLM-OPTIMIZED-REFERENCE.md"
        if candidate.exists():
            content = candidate.read_text(encoding="utf-8")
            match = _re.search(
                rf'<section name="{_re.escape(section_name)}">'
                r"(.*?)</section>",
                content,
                _re.DOTALL | _re.IGNORECASE,
            )
            if match:
                return {
                    "status": "ok",
                    "section": section_name,
                    "content": match.group(1).strip(),
                }

    available = [
        "usage", "review-delta", "review-pr", "commands",
        "legal", "watch", "embeddings", "languages", "troubleshooting",
    ]
    return {
        "status": "not_found",
        "error": (
            f"Section '{section_name}' not found. "
            f"Available: {', '.join(available)}"
        ),
    }


# ---------------------------------------------------------------------------
# Tool 9: find_large_functions
# ---------------------------------------------------------------------------


def find_large_functions(
    min_lines: int = 50,
    kind: str | None = None,
    file_path_pattern: str | None = None,
    limit: int = 50,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Find functions, classes, or files exceeding a line-count threshold.

    Useful for identifying decomposition targets, code-quality audits,
    and enforcing size limits during code review.

    Args:
        min_lines: Minimum line count to flag (default: 50).
        kind: Filter by node kind: Function, Class, File, or Test.
        file_path_pattern: Filter by file path substring (e.g. "components/").
        limit: Maximum results (default: 50).
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Oversized nodes with line counts, ordered largest first.
    """
    store, root = _get_store(repo_root)
    try:
        nodes = store.get_nodes_by_size(
            min_lines=min_lines,
            kind=kind,
            file_path_pattern=file_path_pattern,
            limit=limit,
        )

        results = []
        for n in nodes:
            d = node_to_dict(n)
            d["line_count"] = (n.line_end - n.line_start + 1) if n.line_start and n.line_end else 0
            # Make file_path relative for readability
            try:
                d["relative_path"] = str(Path(n.file_path).relative_to(root))
            except ValueError:
                d["relative_path"] = n.file_path
            results.append(d)

        summary_parts = [
            f"Found {len(results)} node(s) with >= {min_lines} lines"
            + (f" (kind={kind})" if kind else "")
            + (f" matching '{file_path_pattern}'" if file_path_pattern else "")
            + ":",
        ]
        for r in results[:10]:
            summary_parts.append(
                f"  {r['line_count']:>4} lines | {r['kind']:>8} | "
                f"{r['name']} ({r['relative_path']}:{r['line_start']})"
            )
        if len(results) > 10:
            summary_parts.append(f"  ... and {len(results) - 10} more")

        return {
            "status": "ok",
            "summary": "\n".join(summary_parts),
            "total_found": len(results),
            "min_lines": min_lines,
            "results": results,
        }
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 10: list_flows  [EXPLORE]
# ---------------------------------------------------------------------------


def list_flows(
    repo_root: str | None = None,
    sort_by: str = "criticality",
    limit: int = 50,
    kind: str | None = None,
) -> dict[str, Any]:
    """List execution flows in the codebase, sorted by criticality.

    [EXPLORE] Retrieves stored execution flows from the knowledge graph.
    Each flow represents a call chain starting from an entry point
    (e.g. HTTP handler, CLI command, test function).

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
        sort_by: Sort column: criticality, depth, node_count, file_count, or name.
        limit: Maximum flows to return (default: 50).
        kind: Optional filter by entry point kind (e.g. "Test", "Function").

    Returns:
        List of flows with criticality scores.
    """
    store, root = _get_store(repo_root)
    try:
        fetch_limit = limit if not kind else limit * 10  # fetch more when filtering
        flows = get_flows(store, sort_by=sort_by, limit=fetch_limit)

        if kind:
            filtered = []
            for f in flows:
                ep_id = f.get("entry_point_id")
                if ep_id is not None:
                    row = store._conn.execute(
                        "SELECT kind FROM nodes WHERE id = ?", (ep_id,)
                    ).fetchone()
                    if row and row["kind"] == kind:
                        filtered.append(f)
            flows = filtered[:limit]

        result = {
            "status": "ok",
            "summary": f"Found {len(flows)} execution flow(s)",
            "flows": flows,
        }
        result["_hints"] = generate_hints("list_flows", result, get_session())
        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 11: get_flow  [EXPLORE]
# ---------------------------------------------------------------------------


def get_flow(
    flow_id: int | None = None,
    flow_name: str | None = None,
    include_source: bool = False,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Get details of a single execution flow.

    [EXPLORE] Retrieves full path details for a flow, including each step's
    function name, file, and line numbers.  Optionally includes source
    snippets for every step in the path.

    Args:
        flow_id: Database ID of the flow (from list_flows).
        flow_name: Name to search for (partial match). Ignored if flow_id given.
        include_source: If True, include source code snippets for each step.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Flow details with steps, or not_found status.
    """
    store, root = _get_store(repo_root)
    try:
        flow: dict | None = None

        if flow_id is not None:
            flow = get_flow_by_id(store, flow_id)
        elif flow_name is not None:
            # Search flows by name match
            all_flows = get_flows(store, sort_by="criticality", limit=500)
            for f in all_flows:
                if flow_name.lower() in f["name"].lower():
                    flow = get_flow_by_id(store, f["id"])
                    break

        if flow is None:
            return {
                "status": "not_found",
                "summary": "No flow found matching the given criteria.",
            }

        # Optionally include source snippets for each step
        if include_source and "steps" in flow:
            for step in flow["steps"]:
                fp = Path(step["file"]) if step.get("file") else None
                if fp is not None and not fp.is_absolute():
                    fp = root / fp
                file_path = fp
                if file_path and file_path.is_file():
                    try:
                        lines = file_path.read_text(errors="replace").splitlines()
                        start = max(0, (step.get("line_start") or 1) - 1)
                        end = min(len(lines), step.get("line_end") or len(lines))
                        step["source"] = "\n".join(
                            f"{i + 1}: {lines[i]}" for i in range(start, end)
                        )
                    except (OSError, UnicodeDecodeError):
                        step["source"] = "(could not read file)"

        result = {
            "status": "ok",
            "summary": (
                f"Flow '{flow['name']}': {flow['node_count']} nodes, "
                f"depth {flow['depth']}, criticality {flow['criticality']:.4f}"
            ),
            "flow": flow,
        }
        result["_hints"] = generate_hints("get_flow", result, get_session())
        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 12: get_affected_flows  [REVIEW]
# ---------------------------------------------------------------------------


def get_affected_flows_func(
    changed_files: list[str] | None = None,
    base: str = "HEAD~1",
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Find execution flows affected by changed files.

    [REVIEW] Identifies which execution flows pass through nodes in the
    changed files.  Useful during code review to understand which user-facing
    or critical paths are affected by a change.

    Args:
        changed_files: List of changed file paths (relative to repo root).
                       Auto-detected from git diff if omitted.
        base: Git ref for auto-detecting changes (default: HEAD~1).
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Affected flows sorted by criticality, with step details.
    """
    store, root = _get_store(repo_root)
    try:
        if changed_files is None:
            changed_files = get_changed_files(root, base)
            if not changed_files:
                changed_files = get_staged_and_unstaged(root)

        if not changed_files:
            return {
                "status": "ok",
                "summary": "No changed files detected.",
                "affected_flows": [],
                "total": 0,
            }

        # Convert to absolute paths for graph lookup
        abs_files = [str(root / f) for f in changed_files]
        result = _get_affected_flows(store, abs_files)

        total = result["total"]
        out = {
            "status": "ok",
            "summary": f"{total} flow(s) affected by changes in {len(changed_files)} file(s)",
            "changed_files": changed_files,
            "affected_flows": result["affected_flows"],
            "total": total,
        }
        out["_hints"] = generate_hints("get_affected_flows", out, get_session())
        return out
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 13: list_communities  [EXPLORE]
# ---------------------------------------------------------------------------


def list_communities_func(
    repo_root: str | None = None,
    sort_by: str = "size",
    min_size: int = 0,
) -> dict[str, Any]:
    """List detected code communities in the codebase.

    [EXPLORE] Retrieves stored communities from the knowledge graph.
    Each community represents a cluster of related code entities
    (functions, classes) detected via the Leiden algorithm or
    file-based grouping.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
        sort_by: Sort column: size, cohesion, or name.
        min_size: Minimum community size to include (default: 0).

    Returns:
        List of communities with size and cohesion scores.
    """
    store, root = _get_store(repo_root)
    try:
        communities = get_communities(store, sort_by=sort_by, min_size=min_size)
        result = {
            "status": "ok",
            "summary": f"Found {len(communities)} communities",
            "communities": communities,
        }
        result["_hints"] = generate_hints("list_communities", result, get_session())
        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 14: get_community  [EXPLORE]
# ---------------------------------------------------------------------------


def get_community_func(
    community_name: str | None = None,
    community_id: int | None = None,
    include_members: bool = False,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Get details of a single code community.

    [EXPLORE] Retrieves a community by its database ID or by name match.
    Optionally includes the full list of member nodes.

    Args:
        community_name: Name to search for (partial match). Ignored if community_id given.
        community_id: Database ID of the community.
        include_members: If True, include full member node details.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Community details, or not_found status.
    """
    store, root = _get_store(repo_root)
    try:
        community: dict | None = None
        all_communities = get_communities(store)

        if community_id is not None:
            for c in all_communities:
                if c.get("id") == community_id:
                    community = c
                    break
        elif community_name is not None:
            for c in all_communities:
                if community_name.lower() in c["name"].lower():
                    community = c
                    break

        if community is None:
            return {
                "status": "not_found",
                "summary": "No community found matching the given criteria.",
            }

        if include_members:
            cid = community.get("id")
            if cid is not None:
                rows = store._conn.execute(
                    "SELECT * FROM nodes WHERE community_id = ?", (cid,)
                ).fetchall()
                members = [node_to_dict(store._row_to_node(row)) for row in rows]
                community["member_details"] = members

        result = {
            "status": "ok",
            "summary": (
                f"Community '{community['name']}': {community['size']} nodes, "
                f"cohesion {community['cohesion']:.4f}"
            ),
            "community": community,
        }
        result["_hints"] = generate_hints("get_community", result, get_session())
        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 15: get_architecture_overview  [EXPLORE]
# ---------------------------------------------------------------------------


def get_architecture_overview_func(
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Generate an architecture overview based on community structure.

    [EXPLORE] Builds a high-level view of the codebase architecture by
    analyzing community boundaries and cross-community coupling.
    Includes warnings for high coupling between communities.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Architecture overview with communities, cross-community edges, and warnings.
    """
    store, root = _get_store(repo_root)
    try:
        overview = get_architecture_overview(store)
        n_communities = len(overview["communities"])
        n_cross = len(overview["cross_community_edges"])
        n_warnings = len(overview["warnings"])
        result = {
            "status": "ok",
            "summary": (
                f"Architecture: {n_communities} communities, "
                f"{n_cross} cross-community edges, {n_warnings} warning(s)"
            ),
            **overview,
        }
        result["_hints"] = generate_hints(
            "get_architecture_overview", result, get_session()
        )
        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 16: detect_changes  [REVIEW]
# ---------------------------------------------------------------------------


def detect_changes_func(
    base: str = "HEAD~1",
    changed_files: list[str] | None = None,
    include_source: bool = False,
    max_depth: int = 2,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Detect changes and produce risk-scored review guidance.

    [REVIEW] Primary tool for code review.  Maps git diffs to affected
    functions, flows, communities, and test coverage gaps.  Returns
    priority-ordered review guidance with risk scores.

    Args:
        base: Git ref to diff against (default: HEAD~1).
        changed_files: Explicit list of changed file paths (relative to repo
            root).  Auto-detected from git diff if omitted.
        include_source: If True, include source code snippets for changed
            functions.  Default: False.
        max_depth: Impact radius depth for BFS traversal.  Default: 2.
        repo_root: Repository root path.  Auto-detected if omitted.

    Returns:
        Risk-scored analysis with changed functions, affected flows,
        test gaps, and review priorities.
    """
    store, root = _get_store(repo_root)
    try:
        # Detect changed files if not provided.
        if changed_files is None:
            changed_files = get_changed_files(root, base)
            if not changed_files:
                changed_files = get_staged_and_unstaged(root)

        if not changed_files:
            return {
                "status": "ok",
                "summary": "No changed files detected.",
                "risk_score": 0.0,
                "changed_functions": [],
                "affected_flows": [],
                "test_gaps": [],
                "review_priorities": [],
            }

        # Convert to absolute paths for graph lookup.
        abs_files = [str(root / f) for f in changed_files]

        # Parse diff ranges for line-level mapping.
        diff_ranges = parse_git_diff_ranges(str(root), base)
        # Remap to absolute paths so they match graph file_paths.
        abs_ranges: dict[str, list[tuple[int, int]]] = {}
        for rel_path, ranges in diff_ranges.items():
            abs_path = str(root / rel_path)
            abs_ranges[abs_path] = ranges

        analysis = analyze_changes(
            store,
            changed_files=abs_files,
            changed_ranges=abs_ranges if abs_ranges else None,
            repo_root=str(root),
            base=base,
        )

        # Optionally include source snippets for changed functions.
        if include_source:
            for func in analysis.get("changed_functions", []):
                fp = func.get("file_path")
                ls = func.get("line_start")
                le = func.get("line_end")
                if fp and ls and le:
                    file_path = Path(fp)
                    if file_path.is_file():
                        try:
                            lines = file_path.read_text(errors="replace").splitlines()
                            start = max(0, ls - 1)
                            end = min(len(lines), le)
                            func["source"] = "\n".join(
                                f"{i + 1}: {lines[i]}" for i in range(start, end)
                            )
                        except (OSError, UnicodeDecodeError):
                            func["source"] = "(could not read file)"

        result = {
            "status": "ok",
            "changed_files": changed_files,
            **analysis,
        }
        result["_hints"] = generate_hints("detect_changes", result, get_session())
        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 17: refactor_tool  [REFACTOR]
# ---------------------------------------------------------------------------


def refactor_func(
    mode: str = "rename",
    old_name: str | None = None,
    new_name: str | None = None,
    kind: str | None = None,
    file_pattern: str | None = None,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Unified refactoring entry point.

    [REFACTOR] Supports three modes:
    - ``rename``: Preview renaming a symbol (requires *old_name* and *new_name*).
    - ``dead_code``: Find unreferenced functions/classes.
    - ``suggest``: Get community-driven refactoring suggestions.

    Args:
        mode: One of ``"rename"``, ``"dead_code"``, or ``"suggest"``.
        old_name: (rename mode) Current symbol name.
        new_name: (rename mode) Desired new name.
        kind: (dead_code mode) Optional node kind filter.
        file_pattern: (dead_code mode) Optional file path substring filter.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Mode-specific results dict.
    """
    valid_modes = {"rename", "dead_code", "suggest"}
    if mode not in valid_modes:
        return {
            "status": "error",
            "error": (
                f"Invalid mode '{mode}'. "
                f"Must be one of: {', '.join(sorted(valid_modes))}"
            ),
        }

    store, root = _get_store(repo_root)
    try:
        if mode == "rename":
            if not old_name or not new_name:
                return {
                    "status": "error",
                    "error": "rename mode requires both old_name and new_name.",
                }
            preview = rename_preview(store, old_name, new_name)
            if preview is None:
                return {
                    "status": "not_found",
                    "summary": f"No node found matching '{old_name}'.",
                }
            result = {
                "status": "ok",
                "summary": (
                    f"Rename preview: {old_name} -> {new_name}, "
                    f"{len(preview['edits'])} edit(s). "
                    f"Use apply_refactor_tool(refactor_id="
                    f"'{preview['refactor_id']}') to apply."
                ),
                **preview,
            }
            result["_hints"] = generate_hints("refactor", result, get_session())
            return result

        elif mode == "dead_code":
            dead = find_dead_code(store, kind=kind, file_pattern=file_pattern)
            result = {
                "status": "ok",
                "summary": f"Found {len(dead)} dead code symbol(s).",
                "dead_code": dead,
                "total": len(dead),
            }
            result["_hints"] = generate_hints("refactor", result, get_session())
            return result

        else:  # suggest
            suggestions = suggest_refactorings(store)
            result = {
                "status": "ok",
                "summary": (
                    f"Generated {len(suggestions)} refactoring suggestion(s)."
                ),
                "suggestions": suggestions,
                "total": len(suggestions),
            }
            result["_hints"] = generate_hints("refactor", result, get_session())
            return result

    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 18: apply_refactor_tool  [REFACTOR]
# ---------------------------------------------------------------------------


def apply_refactor_func(
    refactor_id: str,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Apply a previously previewed refactoring to source files.

    [REFACTOR] Validates the refactor_id, checks expiry, ensures all edit
    paths are within the repo root, then performs exact string replacements.

    Args:
        refactor_id: ID returned by a prior ``refactor_tool(mode="rename")``
            call.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Status with count of applied edits and modified files.
    """
    try:
        root = (
            _validate_repo_root(Path(repo_root))
            if repo_root
            else find_project_root()
        )
    except (RuntimeError, ValueError) as exc:
        return {"status": "error", "error": str(exc)}

    result = apply_refactor(refactor_id, root)
    return result


# ---------------------------------------------------------------------------
# Tool 19: generate_wiki  [DOCS]
# ---------------------------------------------------------------------------


def generate_wiki_func(
    repo_root: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Generate a markdown wiki from the community structure.

    [DOCS] Creates a wiki page for each detected community and an index
    page. Pages are written to ``.code-review-graph/wiki/`` inside the
    repository. Only regenerates pages whose content has changed unless
    force=True.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
        force: If True, regenerate all pages even if content is unchanged.

    Returns:
        Status with pages_generated, pages_updated, pages_unchanged counts.
    """
    from .wiki import generate_wiki

    store, root = _get_store(repo_root)
    try:
        wiki_dir = root / ".code-review-graph" / "wiki"
        result = generate_wiki(store, wiki_dir, force=force)
        total = result["pages_generated"] + result["pages_updated"] + result["pages_unchanged"]
        return {
            "status": "ok",
            "summary": (
                f"Wiki generated: {result['pages_generated']} new, "
                f"{result['pages_updated']} updated, "
                f"{result['pages_unchanged']} unchanged "
                f"({total} total pages)"
            ),
            "wiki_dir": str(wiki_dir),
            **result,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 20: get_wiki_page  [DOCS]
# ---------------------------------------------------------------------------


def get_wiki_page_func(
    community_name: str,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Retrieve a specific wiki page by community name.

    [DOCS] Returns the markdown content of the wiki page for the given
    community. The wiki must have been generated first via generate_wiki.

    Args:
        community_name: Community name to look up (slugified for filename).
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Page content or not_found status.
    """
    from .wiki import get_wiki_page

    _, root = _get_store(repo_root)
    wiki_dir = root / ".code-review-graph" / "wiki"
    content = get_wiki_page(wiki_dir, community_name)
    if content is None:
        return {
            "status": "not_found",
            "summary": f"No wiki page found for '{community_name}'.",
        }
    return {
        "status": "ok",
        "summary": f"Wiki page for '{community_name}' ({len(content)} chars)",
        "content": content,
    }


# ---------------------------------------------------------------------------
# Tool 21: list_repos  [REGISTRY]
# ---------------------------------------------------------------------------


def list_repos_func() -> dict[str, Any]:
    """List all registered repositories.

    [REGISTRY] Returns the list of repositories registered in the global
    multi-repo registry at ``~/.code-review-graph/registry.json``.

    Returns:
        List of registered repos with paths and aliases.
    """
    from .registry import Registry

    try:
        registry = Registry()
        repos = registry.list_repos()
        return {
            "status": "ok",
            "summary": f"{len(repos)} registered repository(ies)",
            "repos": repos,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool 22: cross_repo_search  [REGISTRY]
# ---------------------------------------------------------------------------


def cross_repo_search_func(
    query: str,
    kind: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search across all registered repositories.

    [REGISTRY] Runs hybrid_search on each registered repo's graph database
    and merges the results.

    Args:
        query: Search query string.
        kind: Optional node kind filter (e.g. "Function", "Class").
        limit: Maximum results per repo (default: 20).

    Returns:
        Combined search results from all registered repos.
    """
    from .registry import Registry

    try:
        registry = Registry()
        repos = registry.list_repos()
        if not repos:
            return {
                "status": "ok",
                "summary": "No repositories registered. Use 'register' to add repos.",
                "results": [],
            }

        all_results: list[dict[str, Any]] = []
        searched_repos: list[str] = []

        for repo_entry in repos:
            repo_path = Path(repo_entry["path"])
            db_path = repo_path / ".code-review-graph" / "graph.db"
            if not db_path.exists():
                continue

            try:
                store = GraphStore(str(db_path))
                try:
                    results = hybrid_search(store, query, kind=kind, limit=limit)
                    alias = repo_entry.get("alias", repo_path.name)
                    for r in results:
                        r["repo"] = alias
                        r["repo_path"] = str(repo_path)
                    all_results.extend(results)
                    searched_repos.append(alias)
                finally:
                    store.close()
            except Exception as exc:
                logger.warning("Search failed for %s: %s", repo_path, exc)

        # Sort all results by score descending
        all_results.sort(key=lambda r: r.get("score", 0), reverse=True)

        return {
            "status": "ok",
            "summary": (
                f"Found {len(all_results)} result(s) across "
                f"{len(searched_repos)} repo(s) for '{query}'"
            ),
            "results": all_results[:limit],
            "repos_searched": searched_repos,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
