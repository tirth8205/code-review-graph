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
from ..parser import normalize_file_path
from ..search import hybrid_search
from ..uncertainty import (
    empty_impact_confidence,
    empty_query_confidence,
    empty_search_confidence,
)
from ._common import (
    _BUILTIN_CALL_NAMES,
    _bounded,
    _get_store,
    _resolve_graph_file_paths,
    _shown_of,
    _validate_positive_int,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool 2: get_impact_radius
# ---------------------------------------------------------------------------

_QUERY_PATTERNS = {
    "callers_of": "Find all functions that call a given function",
    "references_to": "Find all nodes that reference a given symbol",
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

# Hard ceilings for the query-owned response lists. They mirror the review
# context contract: a caller may lower a bound, but cannot ask for a payload
# that dwarfs the client. Minimal search already projects to a tiny row, while
# minimal large-function rows carry enough identity to act on more matches.
_MAX_IMPACT_FILES = 200
_MAX_IMPACT_NODES = 100
_MAX_IMPACT_EDGES = 150
_MAX_SEARCH_RESULTS_STANDARD = 100
_MAX_SEARCH_RESULTS_MINIMAL = 5
_MAX_LARGE_FUNCTIONS_STANDARD = 100
_MAX_LARGE_FUNCTIONS_MINIMAL = 500
_MAX_TRAVERSAL_TOKEN_BUDGET = 10_000
_FETCH_ALL = 10**9


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

    Every response list respects ``max_results`` and a per-list hard ceiling.
    Count fields always describe the untruncated result.
    """
    _validate_positive_int(max_results, "max_results")

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
                "changed_files": [],
                "changed_nodes": [],
                "impacted_nodes": [],
                "impacted_files": [],
                "edges": [],
                "changed_files_total": 0,
                "changed_nodes_total": 0,
                "impacted_files_total": 0,
                "edges_total": 0,
                "truncated": False,
                "total_impacted": 0,
            }

        # Resolve user-facing paths to the file paths stored in the graph.
        original_tokens = estimate_file_tokens(root, changed_files)
        abs_files = _resolve_graph_file_paths(store, root, changed_files)
        result = store.get_impact_radius(
            abs_files, max_depth=max_depth, max_nodes=_FETCH_ALL
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
        shown_changed_files, changed_files_total, changed_files_cut = _bounded(
            changed_files, max_results, _MAX_IMPACT_FILES,
        )
        shown_changed_nodes, changed_nodes_total, changed_nodes_cut = _bounded(
            changed_dicts, max_results, _MAX_IMPACT_NODES,
        )
        shown_impacted_nodes, _impacted_total, impacted_nodes_cut = _bounded(
            impacted_dicts, max_results, _MAX_IMPACT_NODES,
        )
        shown_impacted_files, impacted_files_total, impacted_files_cut = _bounded(
            result["impacted_files"], max_results, _MAX_IMPACT_FILES,
        )
        shown_edges, edges_total, edges_cut = _bounded(
            edge_dicts, max_results, _MAX_IMPACT_EDGES,
        )
        total_impacted = result["total_impacted"]
        truncated = (
            result["truncated"] or changed_files_cut or changed_nodes_cut
            or impacted_nodes_cut or impacted_files_cut or edges_cut
        )

        summary_parts = [
            f"Blast radius for {changed_files_total} changed file(s)"
            + _shown_of(len(shown_changed_files), changed_files_total) + ":",
            f"  - {changed_nodes_total} nodes directly changed"
            + _shown_of(len(shown_changed_nodes), changed_nodes_total),
            f"  - {total_impacted} nodes impacted (within {max_depth} hops)"
            + _shown_of(len(shown_impacted_nodes), total_impacted),
            f"  - {impacted_files_total} additional files affected"
            + _shown_of(len(shown_impacted_files), impacted_files_total),
        ]

        # "Nothing is impacted" and "nothing about these files is indexed"
        # look identical to a reader without this marker.
        confidence = None
        if not impacted_dicts:
            changed_language = next(
                (n.language for n in result["changed_nodes"] if n.language), None,
            )
            confidence = empty_impact_confidence(
                store, root, changed_files, abs_files, changed_language,
            )

        if detail_level == "minimal":
            impacted_count = total_impacted
            if impacted_count > 20:
                risk = "high"
            elif impacted_count > 5:
                risk = "medium"
            else:
                risk = "low"
            key_entities = [
                n["name"] for n in shown_impacted_nodes[:5]
            ]
            minimal_response = {
                "status": "ok",
                "summary": "\n".join(summary_parts),
                "risk": risk,
                "impacted_file_count": impacted_files_total,
                "key_entities": key_entities,
                "truncated": truncated,
                "nodes_omitted": max(
                    0, total_impacted - len(shown_impacted_nodes)
                ),
            }
            if confidence:
                minimal_response["confidence"] = confidence
            attach_context_savings(minimal_response, original_tokens=original_tokens)
            return minimal_response

        response: dict[str, Any] = {
            "status": "ok",
            "summary": "\n".join(summary_parts),
            "changed_files": shown_changed_files,
            "changed_nodes": shown_changed_nodes,
            "impacted_nodes": shown_impacted_nodes,
            "impacted_files": shown_impacted_files,
            "edges": shown_edges,
            "changed_files_total": changed_files_total,
            "changed_nodes_total": changed_nodes_total,
            "impacted_files_total": impacted_files_total,
            "edges_total": edges_total,
            "truncated": truncated,
            "total_impacted": total_impacted,
            "nodes_omitted": max(
                0, total_impacted - len(shown_impacted_nodes)
            ),
        }
        if confidence:
            response["confidence"] = confidence
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
        pattern: Query pattern. One of: callers_of, references_to, callees_of,
                 imports_of, importers_of, children_of, tests_for, inheritors_of,
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
                abs_target = normalize_file_path(root / target)
                node = store.get_node(abs_target)
            if not node:
                java_candidates = _java_fqn_candidates(store, target)
                candidates = (
                    java_candidates
                    if java_candidates is not None
                    else store.search_nodes(target, limit=20)
                )
                if pattern == "inheritors_of" and "::" not in target:
                    exact_type_candidates = [
                        candidate
                        for candidate in candidates
                        if candidate.name == target
                        and candidate.kind
                        in {"Class", "Interface", "Type", "Struct", "Enum", "Trait"}
                    ]
                    if exact_type_candidates:
                        candidates = exact_type_candidates
                if len(candidates) == 1:
                    node = candidates[0]
                    target = node.qualified_name
                elif len(candidates) > 1:
                    candidate_count = (
                        len(candidates)
                        if java_candidates is not None
                        else store.count_search_nodes(target)
                    )
                    ranked = _rank_disambiguation_candidates(candidates, target)
                    return {
                        "status": "ambiguous",
                        "summary": (
                            f"'{target}' matches {candidate_count} node(s). "
                            "Re-run with a qualified_name from disambiguation."
                        ),
                        # Preserve the established key while adding the clearer
                        # agent-facing name introduced by #458.
                        "candidates": ranked,
                        "disambiguation": ranked,
                        "candidate_count": candidate_count,
                        "candidates_truncated": candidate_count > len(candidates),
                        "hint": (
                            "Use a qualified_name from disambiguation as the "
                            "target parameter."
                        ),
                    }

        if not node and pattern not in ("consumers_of", "file_summary"):
            # This branch, not the empty-result path below, is where an
            # unresolved target actually lands for most patterns, so the
            # not-indexed marker has to be attached here too.
            unresolved: dict[str, Any] = {
                "status": "not_found",
                "summary": f"No node found matching '{target}'.",
            }
            unresolved_note = empty_query_confidence(store, root, pattern, target, None)
            if unresolved_note:
                unresolved["confidence"] = unresolved_note
            return unresolved

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
                cpp_overload_count = (
                    store.count_nodes_by_name(
                        node.name,
                        language="cpp",
                        kinds=("Function", "Test"),
                    )
                    if node.language == "cpp"
                    else 0
                )
                for e in store.iter_edges_by_target_name(
                    node.name,
                    language=node.language or None,
                ):
                    # A C++ overload set deliberately keeps the target bare.
                    # Its candidates support disambiguation, but do not prove
                    # that any one exact overload was called.
                    if (
                        "ambiguous_targets" in e.extra
                        or "unresolved_targets" in e.extra
                        or (node.language == "cpp" and e.extra.get("receiver"))
                    ):
                        continue
                    if cpp_overload_count > 1:
                        continue
                    if e.source_qualified not in seen_sources:
                        seen_sources.add(e.source_qualified)
                        caller = store.get_node(e.source_qualified)
                        if caller:
                            caller_result = node_to_dict(caller)
                            caller_result["target_resolution"] = "unresolved"
                            add_result(caller_result, e)

        elif pattern == "references_to":
            seen_reference_sources: set[str] = set()
            for e in store.iter_edges_by_target(qn):
                if (
                    e.kind != "REFERENCES"
                    or e.source_qualified in seen_reference_sources
                ):
                    continue
                source = store.get_node(e.source_qualified)
                if source:
                    seen_reference_sources.add(e.source_qualified)
                    add_result(node_to_dict(source), e)

        elif pattern == "callees_of":
            seen_targets: set[str] = set()
            for e in store.iter_edges_by_source(qn):
                if e.kind == "CALLS":
                    if e.target_qualified not in seen_targets:
                        seen_targets.add(e.target_qualified)
                        callee = store.get_node(e.target_qualified)
                        if callee:
                            add_result(node_to_dict(callee), e)
                        elif (
                            isinstance(e.extra.get("ambiguous_targets"), list)
                            or isinstance(e.extra.get("unresolved_targets"), list)
                            or "::" not in e.target_qualified
                            or (node is not None and node.language == "cpp")
                        ):
                            unresolved = (
                                e.extra.get("ambiguous_targets")
                                or e.extra.get("unresolved_targets")
                            )
                            result: dict[str, Any] = {
                                "kind": "Function",
                                "name": e.target_qualified,
                                "qualified_name": e.target_qualified,
                            }
                            if isinstance(unresolved, list):
                                resolution = (
                                    "ambiguous"
                                    if e.extra.get("ambiguous_targets")
                                    else "unresolved"
                                )
                                result["resolution"] = resolution
                                result["candidates"] = [
                                    _sanitize_name(candidate)
                                    for candidate in unresolved[:20]
                                    if isinstance(candidate, str)
                                ]
                                candidate_count = e.extra.get(
                                    f"{resolution}_target_count",
                                )
                                if not isinstance(candidate_count, int):
                                    candidate_count = len(unresolved)
                                result["candidate_count"] = candidate_count
                                result["candidates_truncated"] = bool(
                                    e.extra.get(
                                        f"{resolution}_targets_truncated",
                                    )
                                    or candidate_count > len(result["candidates"])
                                )
                            add_result(result, e)

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
            cpp_overload_set = bool(
                node
                and node.language == "cpp"
                and store.count_nodes_by_name(
                    node.name,
                    language="cpp",
                    kinds=("Function", "Test"),
                ) > 1
            )
            test_nodes = []
            if not cpp_overload_set:
                test_nodes = store.search_nodes(f"test_{name}", limit=10)
                test_nodes += store.search_nodes(f"Test{name}", limit=10)
            for t in test_nodes:
                if t.qualified_name not in seen and t.is_test:
                    result = node_to_dict(t)
                    result["indirect"] = False
                    result["inferred_by"] = "naming_convention"
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
                    for e in store.iter_edges_by_target_name(
                        node.name, kind=kind, language=node.language or None,
                    ):
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

        # A zero here is the dangerous direction: agents read it as "none
        # exist" and either conclude wrongly or fall back to grepping the
        # repository. One capped sentence prevents both, and is attached only
        # when the result set is empty so non-empty responses are unchanged.
        confidence = (
            empty_query_confidence(store, root, pattern, target, node)
            if total_results == 0
            else None
        )

        if detail_level == "minimal":
            minimal_results = [
                {
                    k: r[k]
                    for k in ("name", "kind", "file_path", "indirect")
                    if k in r
                }
                for r in results
            ]
            minimal_response: dict[str, Any] = {
                "status": "ok",
                "pattern": pattern,
                "target": target,
                "description": _QUERY_PATTERNS[pattern],
                "summary": summary,
                "result_count": total_results,
                "results_omitted": results_omitted,
                "results": minimal_results,
            }
            if confidence:
                minimal_response["confidence"] = confidence
            return minimal_response

        response: dict[str, Any] = {
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
        if confidence:
            response["confidence"] = confidence
        return response
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
        Ranked list of matching nodes. ``total`` is the untruncated match
        count; ``results`` is bounded by ``limit`` and the detail-level
        ceiling.
    """
    _validate_positive_int(limit, "limit")

    store, root = _get_store(repo_root)
    try:
        mode_out: list[str] = []
        results = hybrid_search(
            store, query, kind=kind, limit=_FETCH_ALL,
            context_files=context_files,
            model=model, provider=provider, _out_mode=mode_out,
        )

        search_mode = mode_out[0] if mode_out else "keyword"
        hard_cap = (
            _MAX_SEARCH_RESULTS_MINIMAL
            if detail_level == "minimal"
            else _MAX_SEARCH_RESULTS_STANDARD
        )
        visible_results, total, truncated = _bounded(results, limit, hard_cap)

        summary = (
            f"Found {total} node(s) matching '{query}'"
            + (f" (kind={kind})" if kind else "")
            + _shown_of(len(visible_results), total)
        )

        # Zero hits can mean "no such symbol" or "never indexed"/"stale index";
        # only the marker distinguishes them.
        confidence = (
            empty_search_confidence(store, root, query) if not results else None
        )

        if detail_level == "minimal":
            minimal_results = [
                {
                    k: r[k]
                    for k in ("name", "kind", "file_path", "score")
                    if k in r
                }
                for r in visible_results
            ]
            minimal_response: dict[str, Any] = {
                "status": "ok",
                "query": query,
                "search_mode": search_mode,
                "summary": summary,
                "results": minimal_results,
                "result_count": len(minimal_results),
                "total": total,
                "truncated": truncated,
                "results_omitted": max(0, total - len(minimal_results)),
            }
            if confidence:
                minimal_response["confidence"] = confidence
            return minimal_response

        result: dict[str, object] = {
            "status": "ok",
            "query": query,
            "search_mode": search_mode,
            "summary": summary,
            "results": visible_results,
            "result_count": len(visible_results),
            "total": total,
            "truncated": truncated,
            "results_omitted": max(0, total - len(visible_results)),
        }
        if confidence:
            result["confidence"] = confidence
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
    detail_level: str = "standard",
) -> dict[str, Any]:
    """Find functions, classes, or files exceeding a line-count threshold.

    Useful for identifying decomposition targets, code-quality audits,
    and enforcing size limits during code review.

    Args:
        min_lines: Minimum line count to flag (default: 50).
        kind: Filter by node kind: Function, Class, File, or Test.
        file_path_pattern: Filter by file path substring (e.g., "components/").
        limit: Maximum results (default: 50).
        repo_root: Repository root path. Auto-detected if omitted.
        detail_level: "standard" returns full rows; "minimal" projects
            identifying fields and permits a larger ceiling.

    Returns:
        Oversized nodes with line counts, ordered largest first. ``total`` is
        the untruncated count and ``results`` respects both ``limit`` and the
        detail-level ceiling.
    """
    _validate_positive_int(limit, "limit")

    store, root = _get_store(repo_root)
    try:
        nodes = store.get_nodes_by_size(
            min_lines=min_lines,
            kind=kind,
            file_path_pattern=file_path_pattern,
            limit=_FETCH_ALL,
        )

        results = []
        for node in nodes:
            row = node_to_dict(node)
            row["line_count"] = (
                (node.line_end - node.line_start + 1)
                if node.line_start and node.line_end
                else 0
            )
            # Make file_path relative for readability
            try:
                row["relative_path"] = str(
                    Path(node.file_path).relative_to(root)
                )
            except ValueError:
                row["relative_path"] = node.file_path
            results.append(row)

        hard_cap = (
            _MAX_LARGE_FUNCTIONS_MINIMAL
            if detail_level == "minimal"
            else _MAX_LARGE_FUNCTIONS_STANDARD
        )
        visible_results, total, truncated = _bounded(results, limit, hard_cap)
        if detail_level == "minimal":
            visible_results = [
                {
                    key: row[key]
                    for key in (
                        "name", "qualified_name", "kind", "relative_path",
                        "line_start", "line_end", "line_count",
                    )
                    if key in row
                }
                for row in visible_results
            ]

        summary_parts = [
            f"Found {total} node(s) with >= {min_lines} lines"
            + (f" (kind={kind})" if kind else "")
            + (f" matching '{file_path_pattern}'" if file_path_pattern else "")
            + _shown_of(len(visible_results), total)
            + ":",
        ]
        for row in visible_results[:10]:
            summary_parts.append(
                f"  {row['line_count']:>4} lines | {row['kind']:>8} | "
                f"{row['name']} ({row['relative_path']}:{row['line_start']})"
            )
        if len(visible_results) > 10:
            summary_parts.append(f"  ... and {len(visible_results) - 10} more")

        return {
            "status": "ok",
            "summary": "\n".join(summary_parts),
            "total_found": total,
            "min_lines": min_lines,
            "total": total,
            "result_count": len(visible_results),
            "truncated": truncated,
            "results_omitted": max(0, total - len(visible_results)),
            "results": visible_results,
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
        token_budget: Approximate token limit for results, capped at
            ``_MAX_TRAVERSAL_TOKEN_BUDGET``.
        repo_root: Repository root path.
    """
    _validate_positive_int(token_budget, "token_budget")
    visible_budget = min(token_budget, _MAX_TRAVERSAL_TOKEN_BUDGET)

    store, _root = _get_store(repo_root)
    try:
        results = hybrid_search(store, query, limit=1)
        if not results:
            return {
                "error": f"No node matching '{query}'",
                "nodes": [],
            }

        start_qn = results[0]["qualified_name"]
        depth = max(1, min(depth, 6))

        # Walk the complete bounded-depth result first. The caller gets the
        # true node count even when the response budget only permits a prefix.
        visited: set[str] = set()
        queue: list[tuple[str, int]] = [(start_qn, 0)]
        traversal: list[dict[str, Any]] = []

        while queue:
            if mode == "bfs":
                current_qn, current_depth = queue.pop(0)
            else:
                current_qn, current_depth = queue.pop()

            if current_qn in visited or current_depth > depth:
                continue
            visited.add(current_qn)

            node = store.get_node(current_qn)
            if not node:
                continue

            traversal.append({
                "name": _sanitize_name(node.name),
                "qualified_name": node.qualified_name,
                "kind": node.kind,
                "file": node.file_path,
                "depth": current_depth,
            })

            out_edges = store.get_edges_by_source(current_qn)
            in_edges = store.get_edges_by_target(current_qn)
            for edge in out_edges:
                if edge.target_qualified not in visited:
                    queue.append((edge.target_qualified, current_depth + 1))
            for edge in in_edges:
                if edge.source_qualified not in visited:
                    queue.append((edge.source_qualified, current_depth + 1))

        total = len(traversal)
        visible_traversal: list[dict[str, Any]] = []
        used_tokens = 0
        for entry in traversal:
            entry_tokens = max(1, len(str(entry)) // 4)
            if used_tokens + entry_tokens > visible_budget:
                break
            visible_traversal.append(entry)
            used_tokens += entry_tokens

        truncated = len(visible_traversal) < total
        summary = (
            f"Traversed {total} node(s) from '{start_qn}'"
            + _shown_of(len(visible_traversal), total)
        )
        return {
            "start_node": start_qn,
            "mode": mode,
            "max_depth": depth,
            "token_budget": visible_budget,
            "nodes_visited": len(visible_traversal),
            "total": total,
            "traversal": visible_traversal,
            "truncated": truncated,
            "summary": summary,
            "next_tool_suggestions": [
                "query_graph callers_of -- focused relationship query",
                "get_impact_radius -- blast radius analysis",
            ],
        }
    finally:
        store.close()
