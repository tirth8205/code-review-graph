"""Tools 2, 3, 5, 6, 9: query / search / stats helpers."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from ..config_keys import normalize_spring_config_key
from ..context_savings import attach_context_savings, estimate_file_tokens
from ..embeddings import EmbeddingStore
from ..graph import GraphNode, GraphStore, _sanitize_name, edge_to_dict, node_to_dict
from ..hints import generate_hints, get_session
from ..incremental import get_changed_files, get_db_path, get_staged_and_unstaged
from ..search import hybrid_search
from ._common import _BUILTIN_CALL_NAMES, _get_store, _resolve_graph_file_paths

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool 2: get_impact_radius
# ---------------------------------------------------------------------------

_QUERY_PATTERNS = {
    "callers_of": "Find all functions that call a given function",
    "callees_of": "Find all functions called by a given function",
    "imports_of": "Find all imports of a given file or module",
    "importers_of": "Find all files that import a given file or module",
    "children_of": "Find all nodes contained in a file or class",
    "tests_for": "Find all tests for a given function or class",
    "inheritors_of": "Find all classes that inherit from a given class",
    "triggers_of": "Find methods invoked by a scheduler or other trigger",
    "triggered_by": "Find schedulers or other triggers that invoke a method",
    "publishers_of": "Find methods that publish an event",
    "listeners_of": "Find methods that listen for an event",
    "handlers_of": "Find methods that handle an endpoint",
    "endpoints_for": "Find endpoints handled by a method",
    "consumers_of": "Find classes that consume a Spring configuration property",
    "file_summary": "Get a summary of all nodes in a file",
}

_JAVA_FQN_PART = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_MAX_FQN_CANDIDATES = 100


def _looks_like_java_method_fqn(target: str) -> bool:
    """Return whether *target* has a package/Class/method-like shape."""
    if "::" in target:
        return False
    parts = target.split(".")
    if len(parts) < 2 or not all(_JAVA_FQN_PART.fullmatch(part) for part in parts):
        return False
    # Two segments are accepted only for the conventional Class.method form;
    # this keeps ordinary dotted filenames/modules on the legacy path.
    return len(parts) >= 3 or parts[-2][:1].isupper()


def _java_fqn_candidates(store: GraphStore, target: str) -> list[GraphNode] | None:
    """Resolve Java FQNs using language plus class/file evidence.

    ``None`` means that the target is not Java-FQN-shaped. An empty list means
    it is shaped like one but no safe match exists, so callers must not fall
    back to an unrelated globally unique method name.
    """
    if not _looks_like_java_method_fqn(target):
        return None

    parts = target.split(".")
    class_name, method_name = parts[-2:]
    matches: list[GraphNode] = []
    for candidate in store.search_nodes(method_name, limit=_MAX_FQN_CANDIDATES):
        if candidate.language.lower() != "java" or candidate.name != method_name:
            continue
        parent_name = candidate.parent_name or ""
        parent_match = parent_name.rsplit(".", 1)[-1] == class_name
        file_match = Path(candidate.file_path).stem == class_name
        qualified_tail = candidate.qualified_name.rsplit("::", 1)[-1]
        qualified_match = qualified_tail.endswith(f"{class_name}.{method_name}")
        if parent_match or file_match or qualified_match:
            matches.append(candidate)
    return matches


def _rank_disambiguation_candidates(
    candidates: list[GraphNode], target: str,
) -> list[dict[str, Any]]:
    """Return deterministic, sanitized candidates ordered by match quality."""
    target_lower = target.lower()

    def score(node: GraphNode) -> tuple[int, str]:
        if node.qualified_name == target:
            rank = 0
        elif node.name == target:
            rank = 1
        elif target_lower in node.qualified_name.lower():
            rank = 2
        else:
            rank = 3
        return rank, node.qualified_name

    return [node_to_dict(node) for node in sorted(candidates, key=score)]


def get_impact_radius(
    changed_files: list[str] | None = None,
    max_depth: int = 2,
    max_results: int = 500,
    repo_root: str | None = None,
    base: str = "HEAD~1",
    detail_level: str = "standard",
) -> dict[str, Any]:
    """Analyze the blast radius of changed files.

    Args:
        changed_files: Explicit list of changed file paths (relative to repo root).
                       If omitted, auto-detects from git diff.
        max_depth: How many hops to traverse in the graph (default: 2).
        max_results: Maximum impacted nodes to return (default: 500).
        repo_root: Repository root path. Auto-detected if omitted.
        base: Git ref for auto-detecting changes (default: HEAD~1).
        detail_level: "standard" (full output) or "minimal" (summary only).

    Returns:
        Changed nodes, impacted nodes, impacted files, connecting edges,
        plus ``truncated`` flag and ``total_impacted`` count.
    """
    if isinstance(max_results, bool) or max_results < 1:
        raise ValueError("max_results must be an integer greater than or equal to 1")

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

        # Resolve user-facing paths to the file paths stored in the graph.
        original_tokens = estimate_file_tokens(root, changed_files)
        abs_files = _resolve_graph_file_paths(store, root, changed_files)
        result = store.get_impact_radius(
            abs_files, max_depth=max_depth, max_nodes=max_results
        )

        impact_scores = result.get("impact_scores", {})
        changed_dicts = [node_to_dict(n) for n in result["changed_nodes"]]
        impacted_dicts = []
        for node in result["impacted_nodes"]:
            node_dict = node_to_dict(node)
            score = impact_scores.get(node.qualified_name)
            if score is not None:
                node_dict["impact_score"] = score
            impacted_dicts.append(node_dict)
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

        if detail_level == "minimal":
            impacted_count = len(impacted_dicts)
            if impacted_count > 20:
                risk = "high"
            elif impacted_count > 5:
                risk = "medium"
            else:
                risk = "low"
            key_entities = [
                n["name"] for n in impacted_dicts[:5]
            ]
            minimal_response = {
                "status": "ok",
                "summary": "\n".join(summary_parts),
                "risk": risk,
                "impacted_file_count": len(result["impacted_files"]),
                "key_entities": key_entities,
                "truncated": truncated,
                "nodes_omitted": max(0, total_impacted - len(impacted_dicts)),
            }
            attach_context_savings(minimal_response, original_tokens=original_tokens)
            return minimal_response

        response = {
            "status": "ok",
            "summary": "\n".join(summary_parts),
            "changed_files": changed_files,
            "changed_nodes": changed_dicts,
            "impacted_nodes": impacted_dicts,
            "impacted_files": result["impacted_files"],
            "edges": edge_dicts,
            "truncated": truncated,
            "total_impacted": total_impacted,
            "nodes_omitted": max(0, total_impacted - len(impacted_dicts)),
        }
        attach_context_savings(response, original_tokens=original_tokens)
        return response
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 3: query_graph
# ---------------------------------------------------------------------------


def query_graph(
    pattern: str,
    target: str,
    repo_root: str | None = None,
    detail_level: str = "standard",
    max_results: int = 100,
) -> dict[str, Any]:
    """Run a predefined graph query.

    Args:
        pattern: Query pattern. One of: callers_of, callees_of, imports_of,
                 importers_of, children_of, tests_for, inheritors_of,
                 triggers_of, triggered_by, publishers_of, listeners_of,
                 handlers_of, endpoints_for, consumers_of, file_summary.
        target: The node name, qualified name, or file path to query about.
        repo_root: Repository root path. Auto-detected if omitted.
        detail_level: "standard" (full output) or "minimal" (summary only).
        max_results: Maximum results to return. Minimal mode additionally caps
            visible results at five and reports the exact omitted count.

    Returns:
        Matching nodes and their aligned edges, with total and omitted counts.
    """
    if isinstance(max_results, bool) or max_results < 1:
        raise ValueError("max_results must be an integer greater than or equal to 1")

    store, root = _get_store(repo_root)
    try:
        if pattern not in _QUERY_PATTERNS:
            return {
                "status": "error",
                "error": (
                    f"Unknown pattern '{pattern}'. "
                    f"Available: {list(_QUERY_PATTERNS.keys())}"
                ),
            }

        response_limit = min(max_results, 5) if detail_level == "minimal" else max_results
        results: list[dict[str, Any]] = []
        edges_out: list[dict[str, Any]] = []
        total_results = 0

        def add_result(result: dict[str, Any], edge: Any | None = None) -> None:
            """Count every logical result but retain only the bounded prefix."""
            nonlocal total_results
            total_results += 1
            if len(results) >= response_limit:
                return
            results.append(result)
            if edge is not None:
                edges_out.append(edge_to_dict(edge))

        # For callers_of, skip common builtins early (bare names only)
        # "Who calls .map()?" returns hundreds of useless hits.
        # Qualified names (e.g. "utils.py::map") bypass this filter.
        if (
            pattern == "callers_of"
            and target in _BUILTIN_CALL_NAMES
            and "::" not in target
        ):
            return {
                "status": "ok", "pattern": pattern, "target": target,
                "description": _QUERY_PATTERNS[pattern],
                "summary": (
                    f"'{target}' is a common builtin "
                    "— callers_of skipped to avoid noise."
                ),
                "result_count": 0,
                "results_omitted": 0,
                "results": [], "edges": [],
            }

        # Resolve target - try as-is, then as absolute path, then search.
        # file_summary targets are paths, so skip broad node search.
        node = None
        raw_config_target = pattern == "consumers_of" and "::" not in target
        if pattern != "file_summary" and not raw_config_target:
            node = store.get_node(target)
            if not node:
                abs_target = str(root / target)
                node = store.get_node(abs_target)
            if not node:
                java_candidates = _java_fqn_candidates(store, target)
                candidates = (
                    java_candidates
                    if java_candidates is not None
                    else store.search_nodes(target, limit=20)
                )
                if len(candidates) == 1:
                    node = candidates[0]
                    target = node.qualified_name
                elif len(candidates) > 1:
                    ranked = _rank_disambiguation_candidates(candidates, target)
                    return {
                        "status": "ambiguous",
                        "summary": (
                            f"'{target}' matches {len(candidates)} node(s). "
                            "Re-run with a qualified_name from disambiguation."
                        ),
                        # Preserve the established key while adding the clearer
                        # agent-facing name introduced by #458.
                        "candidates": ranked,
                        "disambiguation": ranked,
                        "hint": (
                            "Use a qualified_name from disambiguation as the "
                            "target parameter."
                        ),
                    }

        if not node and pattern not in ("consumers_of", "file_summary"):
            return {
                "status": "not_found",
                "summary": f"No node found matching '{target}'.",
            }

        qn = node.qualified_name if node else target

        if pattern == "callers_of":
            seen_sources: set[str] = set()
            for e in store.iter_edges_by_target(qn):
                if e.kind == "CALLS":
                    if e.source_qualified not in seen_sources:
                        seen_sources.add(e.source_qualified)
                        caller = store.get_node(e.source_qualified)
                        if caller:
                            add_result(node_to_dict(caller), e)
            # Fallback: CALLS edges store unqualified target names
            # (e.g. "generateTestCode") while qn is fully qualified
            # (e.g. "file.ts::generateTestCode"). Search by plain name too.
            if node:
                for e in store.iter_edges_by_target_name(node.name):
                    if e.source_qualified not in seen_sources:
                        seen_sources.add(e.source_qualified)
                        caller = store.get_node(e.source_qualified)
                        if caller:
                            add_result(node_to_dict(caller), e)

        elif pattern == "callees_of":
            seen_targets: set[str] = set()
            for e in store.iter_edges_by_source(qn):
                if e.kind == "CALLS":
                    if e.target_qualified not in seen_targets:
                        seen_targets.add(e.target_qualified)
                        callee = store.get_node(e.target_qualified)
                        if callee:
                            add_result(node_to_dict(callee), e)
                        elif "::" not in e.target_qualified:
                            add_result({
                                "kind": "Function",
                                "name": e.target_qualified,
                                "qualified_name": e.target_qualified,
                            }, e)

        elif pattern == "imports_of":
            for e in store.iter_edges_by_source(qn):
                if e.kind == "IMPORTS_FROM":
                    add_result({"import_target": e.target_qualified}, e)

        elif pattern == "importers_of":
            # Find edges where target matches this file.
            # Use resolve() to canonicalize the path, matching how
            # _resolve_module_to_file stores edge targets.
            abs_target = (
                str((root / target).resolve()) if node is None
                else node.file_path
            )
            seen_importers: set[str] = set()
            for e in store.iter_edges_by_target(abs_target):
                if e.kind == "IMPORTS_FROM":
                    if e.source_qualified in seen_importers:
                        continue
                    seen_importers.add(e.source_qualified)
                    add_result({
                        "importer": e.source_qualified,
                        "file": e.file_path,
                    }, e)
            # C# fallback: `using X.Y;` directives produce IMPORTS_FROM edges
            # whose target is the raw namespace string, not a file path, so
            # the path lookup above misses them. Resolve the target file's
            # declared namespace(s) and also search edges by namespace.
            # See: #310
            if node is not None and node.language == "csharp":
                declared_ns: list[str] = []
                for n in store.iter_nodes_by_file(node.file_path):
                    if n.kind == "File":
                        declared_ns = list(
                            n.extra.get("csharp_namespaces", []) or []
                        )
                        break
                for ns in declared_ns:
                    for e in store.iter_edges_by_target(ns):
                        if e.kind != "IMPORTS_FROM":
                            continue
                        if e.source_qualified in seen_importers:
                            continue
                        seen_importers.add(e.source_qualified)
                        add_result({
                            "importer": e.source_qualified,
                            "file": e.file_path,
                        }, e)

        elif pattern == "children_of":
            for e in store.iter_edges_by_source(qn):
                if e.kind == "CONTAINS":
                    child = store.get_node(e.target_qualified)
                    if child:
                        add_result(node_to_dict(child))

        elif pattern == "tests_for":
            # Keep the normal sanitized node response while adding the
            # direct/indirect marker returned by the bounded store lookup.
            seen: set[str] = set()
            for match in store.get_transitive_tests(qn):
                test_qn = match.get("qualified_name")
                if not isinstance(test_qn, str) or test_qn in seen:
                    continue
                test = store.get_node(test_qn)
                if test:
                    result = node_to_dict(test)
                    result["indirect"] = bool(match.get("indirect", False))
                    add_result(result)
                    seen.add(test_qn)
            # Also search by naming convention
            name = node.name if node else target
            test_nodes = store.search_nodes(f"test_{name}", limit=10)
            test_nodes += store.search_nodes(f"Test{name}", limit=10)
            for t in test_nodes:
                if t.qualified_name not in seen and t.is_test:
                    result = node_to_dict(t)
                    result["indirect"] = False
                    add_result(result)
                    seen.add(t.qualified_name)

        elif pattern == "inheritors_of":
            for e in store.iter_edges_by_target(qn):
                if e.kind in ("INHERITS", "IMPLEMENTS"):
                    child = store.get_node(e.source_qualified)
                    if child:
                        add_result(node_to_dict(child), e)
            # Fallback: INHERITS/IMPLEMENTS edges store unqualified base names
            # (e.g. "Animal") while qn is fully qualified
            # (e.g. "sample.dart::Animal"). Search by plain name too. See: #87
            if total_results == 0 and node:
                for kind in ("INHERITS", "IMPLEMENTS"):
                    for e in store.iter_edges_by_target_name(node.name, kind=kind):
                        child = store.get_node(e.source_qualified)
                        if child:
                            add_result(node_to_dict(child), e)

        elif pattern == "triggers_of":
            for edge in store.get_edges_by_source(qn):
                if edge.kind != "TRIGGERS":
                    continue
                triggered = store.get_node(edge.target_qualified)
                if triggered:
                    add_result(node_to_dict(triggered), edge)
                else:
                    edges_out.append(edge_to_dict(edge))

        elif pattern == "triggered_by":
            for edge in store.get_edges_by_target(qn):
                if edge.kind != "TRIGGERS":
                    continue
                trigger = store.get_node(edge.source_qualified)
                if trigger:
                    add_result(node_to_dict(trigger), edge)
                else:
                    edges_out.append(edge_to_dict(edge))

        elif pattern in ("publishers_of", "listeners_of"):
            edge_kind = "PUBLISHES" if pattern == "publishers_of" else "HANDLES"
            for edge in store.get_edges_by_target(qn):
                if edge.kind != edge_kind:
                    continue
                source = store.get_node(edge.source_qualified)
                if source:
                    add_result(node_to_dict(source), edge)
                else:
                    edges_out.append(edge_to_dict(edge))

        elif pattern == "handlers_of":
            for edge in store.get_edges_by_target(qn):
                if edge.kind != "HANDLES":
                    continue
                handler = store.get_node(edge.source_qualified)
                if handler:
                    add_result(node_to_dict(handler), edge)
                else:
                    edges_out.append(edge_to_dict(edge))

        elif pattern == "endpoints_for":
            for edge in store.get_edges_by_source(qn):
                if edge.kind != "HANDLES":
                    continue
                endpoint = store.get_node(edge.target_qualified)
                if endpoint and endpoint.kind == "Endpoint":
                    add_result(node_to_dict(endpoint), edge)
                elif endpoint is None:
                    edges_out.append(edge_to_dict(edge))

        elif pattern == "consumers_of":
            raw_key = node.name if node else target.removeprefix("config:")
            raw_key = raw_key.removesuffix(".*")
            key = normalize_spring_config_key(raw_key)
            seen_config_sources: set[str] = set()
            for edge in store.get_config_consumers(key):
                consumer = store.get_node(edge.source_qualified)
                if consumer and consumer.qualified_name not in seen_config_sources:
                    add_result(node_to_dict(consumer), edge)
                    seen_config_sources.add(consumer.qualified_name)
                elif consumer is None:
                    edges_out.append(edge_to_dict(edge))

        elif pattern == "file_summary":
            graph_paths = _resolve_graph_file_paths(store, root, [target])
            for graph_path in graph_paths:
                for n in store.iter_nodes_by_file(graph_path):
                    add_result(node_to_dict(n))

        results_omitted = max(0, total_results - len(results))
        summary = (
            f"Found {total_results} result(s) "
            f"for {pattern}('{target}')"
        )
        if results_omitted:
            summary += f" — showing {len(results)}, {results_omitted} omitted"

        if detail_level == "minimal":
            minimal_results = [
                {
                    k: r[k]
                    for k in ("name", "kind", "file_path", "indirect")
                    if k in r
                }
                for r in results
            ]
            return {
                "status": "ok",
                "pattern": pattern,
                "target": target,
                "description": _QUERY_PATTERNS[pattern],
                "summary": summary,
                "result_count": total_results,
                "results_omitted": results_omitted,
                "results": minimal_results,
            }

        return {
            "status": "ok",
            "pattern": pattern,
            "target": target,
            "description": _QUERY_PATTERNS[pattern],
            "summary": summary,
            "result_count": total_results,
            "results_omitted": results_omitted,
            "results": results,
            "edges": edges_out,
        }
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 5: semantic_search_nodes
# ---------------------------------------------------------------------------


def semantic_search_nodes(
    query: str,
    kind: str | None = None,
    limit: int = 20,
    repo_root: str | None = None,
    context_files: list[str] | None = None,
    model: str | None = None,
    provider: str | None = None,
    detail_level: str = "standard",
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
        detail_level: "standard" (full output) or "minimal" (summary only).

    Returns:
        Ranked list of matching nodes.
    """
    store, root = _get_store(repo_root)
    try:
        mode_out: list[str] = []
        results = hybrid_search(
            store, query, kind=kind, limit=limit, context_files=context_files,
            model=model, provider=provider, _out_mode=mode_out,
        )

        search_mode = mode_out[0] if mode_out else "keyword"

        summary = f"Found {len(results)} node(s) matching '{query}'" + (
            f" (kind={kind})" if kind else ""
        )

        if detail_level == "minimal":
            minimal_results = [
                {
                    k: r[k]
                    for k in ("name", "kind", "file_path", "score")
                    if k in r
                }
                for r in results[:5]
            ]
            return {
                "status": "ok",
                "query": query,
                "search_mode": search_mode,
                "summary": summary,
                "results": minimal_results,
                "result_count": len(results),
                "results_omitted": max(0, len(results) - len(minimal_results)),
            }

        result: dict[str, object] = {
            "status": "ok",
            "query": query,
            "search_mode": search_mode,
            "summary": summary,
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
                summary_parts.append(
                    "  (install sentence-transformers for semantic search)"
                )
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
            d["line_count"] = (
                (n.line_end - n.line_start + 1)
                if n.line_start and n.line_end
                else 0
            )
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


# -------------------------------------------------------------------
# traverse_graph: free-form BFS / DFS traversal
# -------------------------------------------------------------------


def traverse_graph_func(
    query: str,
    mode: str = "bfs",
    depth: int = 3,
    token_budget: int = 2000,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """BFS/DFS traversal from best-matching node.

    Args:
        query: Search string to find the starting node.
        mode: "bfs" (breadth-first) or "dfs" (depth-first).
        depth: Max traversal depth (1-6). Default: 3.
        token_budget: Approximate token limit for results.
        repo_root: Repository root path.
    """
    store, root = _get_store(repo_root)
    try:
        results = hybrid_search(store, query, limit=1)
        if not results:
            return {
                "error": f"No node matching '{query}'",
                "nodes": [],
            }

        start_qn = results[0]["qualified_name"]
        depth = max(1, min(depth, 6))

        # BFS / DFS traversal
        visited: dict[str, int] = {}  # qn -> depth
        queue: list[tuple[str, int]] = [
            (start_qn, 0),
        ]
        traversal: list[dict] = []
        approx_tokens = 0

        while queue:
            if mode == "bfs":
                current_qn, cur_depth = queue.pop(0)
            else:
                current_qn, cur_depth = queue.pop()

            if current_qn in visited:
                continue
            if cur_depth > depth:
                continue

            visited[current_qn] = cur_depth
            node = store.get_node(current_qn)
            if not node:
                continue

            entry = {
                "name": _sanitize_name(node.name),
                "qualified_name": node.qualified_name,
                "kind": node.kind,
                "file": node.file_path,
                "depth": cur_depth,
            }
            approx_tokens += len(str(entry)) // 4
            if approx_tokens > token_budget:
                break

            traversal.append(entry)

            # Get neighbours
            out_edges = store.get_edges_by_source(
                current_qn
            )
            in_edges = store.get_edges_by_target(
                current_qn
            )
            for e in out_edges:
                tgt = e.target_qualified
                if tgt not in visited:
                    queue.append((tgt, cur_depth + 1))
            for e in in_edges:
                src = e.source_qualified
                if src not in visited:
                    queue.append((src, cur_depth + 1))

        return {
            "start_node": start_qn,
            "mode": mode,
            "max_depth": depth,
            "nodes_visited": len(traversal),
            "traversal": traversal,
            "truncated": approx_tokens > token_budget,
            "next_tool_suggestions": [
                "query_graph callers_of"
                " -- focused relationship query",
                "get_impact_radius"
                " -- blast radius analysis",
            ],
        }
    finally:
        store.close()
