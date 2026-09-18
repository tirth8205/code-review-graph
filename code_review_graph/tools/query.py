"""Tools 2, 3, 5, 6, 9: query / search / stats helpers."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from ..config_keys import normalize_spring_config_key
from ..constants import IMPORT_SCOPE_KEY
from ..context_savings import attach_context_savings, estimate_file_tokens
from ..embeddings import EmbeddingStore
from ..errors import ChangeDiscoveryError
from ..graph import (
    IMPACT_RESOLUTIONS,
    QUERY_RESOLUTIONS,
    RESOLUTION_ALL,
    RESOLUTION_DIRECT,
    RESOLUTION_UNRESOLVED,
    GraphEdge,
    GraphNode,
    GraphStore,
    _compatible_edge_languages,
    _sanitize_name,
    edge_to_dict,
    import_scope_ancestors,
    node_to_dict,
)
from ..hints import generate_hints, get_session
from ..incremental import (
    discover_review_changes,
    get_db_path,
)
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
    _error_response,
    _get_store,
    _resolve_graph_file_paths,
)

logger = logging.getLogger(__name__)

# Hard ceilings for the impact response, so its size is a constant rather
# than a function of the repository. ``edges`` and ``changed_nodes`` had no
# ceiling at all, and ``max_results`` is not exposed on the MCP tool, so a
# single changed file of cli/cli returned 1,361 connecting edges and one of
# kubernetes returned 500 impacted nodes -- 138k and 189k tokens against a
# documented 12k budget for this tool. The numbers are ``get_review_context``'s
# own, because it caps the same three lists of the same radius for the same
# reason. ``total_impacted``, ``nodes_omitted``, ``edges_omitted`` and
# ``changed_nodes_omitted`` still report the full counts.
_MAX_IMPACT_NODES_SHOWN = 100
_MAX_IMPACT_EDGES = 150
_MAX_IMPACT_FILES = 200

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
_MAX_DOTTED_TARGET_CANDIDATES = 100

#: Patterns whose rows describe one edge, so a call site can be attached and
#: the answer can be split by how certain that edge's target attribution is.
_CALL_SITE_PATTERNS = frozenset({"callers_of", "callees_of", "references_to"})


def _attach_call_site(
    result: dict[str, Any], edge: GraphEdge,
) -> dict[str, Any]:
    """Record where the call is written, not just who writes it.

    Every edge carries a file and a line. Without them on the row a reading
    agent has a caller's name and must open the file to find the call, which
    is the file read the graph exists to avoid.

    ``file`` is present only when the call is written somewhere other than the
    row's own ``file_path``: a repeated path is the single most expensive
    string in a row, and the reader already has it on the same object, so
    omitting the duplicate is lossless. Where the two differ -- a header, a
    generated file, a mixin applied elsewhere -- the path is spelled out, so a
    call is never silently mislocated. Read it as
    ``call_site.get("file", row["file_path"])``.
    """
    call_site: dict[str, Any] = {"line": edge.line}
    if edge.file_path != result.get("file_path"):
        call_site["file"] = edge.file_path
    result["call_site"] = call_site
    return result


def _interleave_call_sites(
    sites: list[tuple[str, dict[str, Any], GraphEdge]],
) -> list[tuple[dict[str, Any], GraphEdge]]:
    """Order call sites so every distinct node is seen before any repeat.

    One row per call site is what makes repeated calls visible, but it would
    let a caller that calls the target forty times consume the whole
    ``max_results`` window and hide every other caller. Emitting the first
    call site of each node, then the second of each, keeps the truncated
    prefix as wide as the un-truncated answer while still reporting repeats.
    Ordering within a round is first-seen, so the result stays deterministic.
    """
    seen_order: dict[str, int] = {}
    occurrence: dict[str, int] = {}
    keyed: list[tuple[int, int, int, dict[str, Any], GraphEdge]] = []
    for index, (key, result, edge) in enumerate(sites):
        seen_order.setdefault(key, len(seen_order))
        round_index = occurrence.get(key, 0)
        occurrence[key] = round_index + 1
        keyed.append((round_index, seen_order[key], index, result, edge))
    keyed.sort(key=lambda item: item[:3])
    return [(result, edge) for _, _, _, result, edge in keyed]


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
    for candidate in store.search_nodes(
        method_name, limit=_MAX_DOTTED_TARGET_CANDIDATES,
    ):
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
    resolution: str = RESOLUTION_ALL,
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
        resolution: "all" (default) traverses every edge; "direct" refuses to
            traverse a CALLS/REFERENCES edge whose target was never bound to
            an indexed node.

    Returns:
        Changed nodes, impacted nodes, impacted files, connecting edges,
        plus ``truncated`` flag and ``total_impacted`` count. An impacted node
        that calls or references the changed code directly also carries the
        ``call_site`` it does so at, and ``unresolved_call_sites`` counts the
        call sites that name a changed symbol but were never bound to it.
    """
    if isinstance(max_results, bool) or max_results < 1:
        raise ValueError("max_results must be an integer greater than or equal to 1")
    if resolution not in IMPACT_RESOLUTIONS:
        raise ValueError(
            f"resolution must be one of {list(IMPACT_RESOLUTIONS)}, got {resolution!r}"
        )

    store, root = _get_store(repo_root)
    try:
        if changed_files is None:
            changed_files, base = discover_review_changes(root, base)

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
            abs_files, max_depth=max_depth, max_nodes=max_results,
            resolution=resolution,
        )

        impact_scores = result.get("impact_scores", {})
        changed_dicts = [node_to_dict(n) for n in result["changed_nodes"]]
        # Where a node reaches the changed code in one hop, say where: the
        # connecting edge already carries the file and line, and it is the
        # first thing a reviewer needs after "this is impacted". A node
        # further out has no single call site, so it gets none.
        changed_qns = {n.qualified_name for n in result["changed_nodes"]}
        direct_sites: dict[str, list[Any]] = {}
        for edge in result["edges"]:
            if (
                edge.kind in ("CALLS", "REFERENCES")
                and edge.target_qualified in changed_qns
                and edge.source_qualified not in changed_qns
            ):
                direct_sites.setdefault(edge.source_qualified, []).append(edge)
        impacted_dicts = []
        shown_impacted, _total_shown, _cut = _bounded(
            result["impacted_nodes"], max_results, _MAX_IMPACT_NODES_SHOWN,
        )
        for node in shown_impacted:
            node_dict = node_to_dict(node)
            score = impact_scores.get(node.qualified_name)
            if score is not None:
                node_dict["impact_score"] = score
            node_sites = direct_sites.get(node.qualified_name)
            if node_sites:
                # One site per node, plus a count: the blast radius is a list
                # of what to look at, not the full call-site listing that
                # query_graph("callers_of") returns.
                first = min(node_sites, key=lambda e: (e.file_path, e.line))
                _attach_call_site(node_dict, first)
                if len(node_sites) > 1:
                    node_dict["call_site_count"] = len(node_sites)
            impacted_dicts.append(node_dict)
        # Edges that touch the changed code are kept first: a truncated list
        # has to keep the part that explains the radius rather than whichever
        # rows happened to sort first.
        ordered_edges = sorted(
            result["edges"],
            key=lambda e: (
                e.source_qualified not in changed_qns
                and e.target_qualified not in changed_qns,
            ),
        )
        shown_edges, edges_total, _edges_cut = _bounded(
            ordered_edges, max_results, _MAX_IMPACT_EDGES,
        )
        edge_dicts = [edge_to_dict(e) for e in shown_edges]
        edges_omitted = edges_total - len(edge_dicts)
        changed_dicts, changed_total, _cn_cut = _bounded(
            changed_dicts, max_results, _MAX_IMPACT_NODES_SHOWN,
        )
        changed_nodes_omitted = changed_total - len(changed_dicts)
        shown_files, files_total, _files_cut = _bounded(
            result["impacted_files"], max_results, _MAX_IMPACT_FILES,
        )
        files_omitted = files_total - len(shown_files)
        # The traversal joins qualified names, so a call site that only ever
        # names the changed symbol is unreachable and silently absent above.
        # Counting it is what stops an empty-looking radius reading as proof.
        unresolved_call_sites = store.count_unresolved_call_sites({
            n.name for n in result["changed_nodes"] if n.kind != "File"
        })
        # ``truncated`` has to mean "there is more", whichever cap bit.
        truncated = (
            result["truncated"]
            or len(impacted_dicts) < len(result["impacted_nodes"])
            or edges_omitted > 0
            or changed_nodes_omitted > 0
            or files_omitted > 0
        )
        total_impacted = result["total_impacted"]

        summary_parts = [
            f"Blast radius for {len(changed_files)} changed file(s):",
            f"  - {len(result['changed_nodes'])} nodes directly changed",
            f"  - {total_impacted} nodes impacted (within {max_depth} hops)",
            f"  - {files_total} additional files affected",
        ]
        if len(impacted_dicts) < total_impacted:
            summary_parts.append(
                f"  - Results truncated: showing {len(impacted_dicts)}"
                f" of {total_impacted} impacted nodes"
            )
        if edges_omitted or changed_nodes_omitted or files_omitted:
            summary_parts.append(
                f"  - Also truncated: {edges_omitted} edges,"
                f" {changed_nodes_omitted} changed nodes,"
                f" {files_omitted} impacted files omitted"
            )

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
            # The full count, not the displayed one: the risk band must not
            # change because a display cap trimmed the list.
            impacted_count = total_impacted
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
                # One integer, and the only thing in this response that says
                # the radius has a blind spot at all.
                "unresolved_call_sites": unresolved_call_sites,
            }
            if confidence:
                minimal_response["confidence"] = confidence
            attach_context_savings(minimal_response, original_tokens=original_tokens)
            return minimal_response

        response: dict[str, Any] = {
            "status": "ok",
            "summary": "\n".join(summary_parts),
            "changed_files": changed_files,
            "changed_nodes": changed_dicts,
            "changed_nodes_omitted": changed_nodes_omitted,
            "impacted_nodes": impacted_dicts,
            "impacted_files": shown_files,
            "impacted_files_omitted": files_omitted,
            "edges": edge_dicts,
            "edges_omitted": edges_omitted,
            "truncated": truncated,
            "total_impacted": total_impacted,
            "nodes_omitted": max(0, total_impacted - len(impacted_dicts)),
            "resolution": resolution,
            "unresolved_call_sites": unresolved_call_sites,
        }
        if confidence:
            response["confidence"] = confidence
        attach_context_savings(response, original_tokens=original_tokens)
        return response
    except ChangeDiscoveryError as exc:
        # Distinct from the "no changed files" answer above, and deliberately
        # so: that one is an all-clear a client will act on. Git that could
        # not be run, or that overran the discovery budget, says nothing
        # about the working tree (#262).
        return _error_response(str(exc))
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
    resolution: str = RESOLUTION_ALL,
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
        resolution: For callers_of/callees_of/references_to, filter rows by how
            certain the edge's target attribution is. "all" (default) keeps
            both, "direct" keeps only calls bound to an indexed node, and
            "unresolved" keeps only the bare-name matches.

    Returns:
        Matching nodes and their aligned edges, with total and omitted counts.
        For callers_of, callees_of and references_to each row also carries the
        ``call_site`` it was found at, one row per call site rather than one
        per node, plus a ``resolution_split`` saying how much of the answer is
        certain.
    """
    if isinstance(max_results, bool) or max_results < 1:
        raise ValueError("max_results must be an integer greater than or equal to 1")
    if resolution not in QUERY_RESOLUTIONS:
        raise ValueError(
            f"resolution must be one of {list(QUERY_RESOLUTIONS)}, got {resolution!r}"
        )

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

        minimal = detail_level == "minimal"
        response_limit = min(max_results, 5) if minimal else max_results
        results: list[dict[str, Any]] = []
        edges_out: list[dict[str, Any]] = []
        total_results = 0
        # (dedupe key, row, edge) for the call-site patterns, collected before
        # emission so repeats can be interleaved behind first sightings.
        call_sites: list[tuple[str, dict[str, Any], GraphEdge]] = []

        def add_result(result: dict[str, Any], edge: Any | None = None) -> None:
            """Count every logical result but retain only the bounded prefix."""
            nonlocal total_results
            total_results += 1
            if len(results) >= response_limit:
                return
            results.append(result)
            if edge is not None:
                edges_out.append(edge_to_dict(edge))

        # One row per call site means the same node is looked up once per
        # call, so memoize what used to be deduplicated away.
        node_cache: dict[str, GraphNode | None] = {}

        def lookup_node(qualified: str) -> GraphNode | None:
            if qualified not in node_cache:
                node_cache[qualified] = store.get_node(qualified)
            return node_cache[qualified]

        def add_call_site(
            key: str, result: dict[str, Any], edge: GraphEdge, certain: bool,
        ) -> None:
            """Stage one call site, marking the uncertain ones."""
            if not certain:
                result["target_resolution"] = RESOLUTION_UNRESOLVED
            if resolution == RESOLUTION_DIRECT and not certain:
                return
            if resolution == RESOLUTION_UNRESOLVED and certain:
                return
            call_sites.append((key, _attach_call_site(result, edge), edge))

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
                qualified_tail_candidates = []
                if "." in target:
                    # Nested type paths are exact indexed tails but also look
                    # Java-shaped; recognize the exact node before the Java
                    # FQN guard, without changing bare-name disambiguation.
                    qualified_tail_candidates = (
                        store.search_nodes_by_qualified_tail(
                            target, limit=_MAX_DOTTED_TARGET_CANDIDATES,
                        )
                    )
                java_candidates = _java_fqn_candidates(store, target)
                if java_candidates:
                    # Established behavior: a Java-shaped target that has real
                    # Java matches resolves against Java, and the unfiltered
                    # tail lookup never gets to displace it.
                    candidates = java_candidates
                elif qualified_tail_candidates:
                    # No Java match. An exact qualified-tail hit is a structural
                    # match on the whole symbol path, not the fuzzy global-name
                    # fallback the Java guard exists to block, so it is safe to
                    # use here. This is what makes C# nested paths such as
                    # ``Details.QueryHandler.Handle`` addressable (#934); they
                    # are Java-FQN-shaped and have no Java candidates.
                    candidates = qualified_tail_candidates
                elif java_candidates is not None:
                    # Java-shaped with no safe match: do not fall back.
                    candidates = []
                else:
                    candidates = store.search_nodes(target, limit=20)
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
                    # Count the population the candidates were drawn from, not
                    # the truncated slice, so candidates_truncated stays honest
                    # when more than _MAX_DOTTED_TARGET_CANDIDATES nodes match.
                    if java_candidates:
                        candidate_count = len(candidates)
                    elif qualified_tail_candidates:
                        candidate_count = store.count_nodes_by_qualified_tail(target)
                    elif java_candidates is not None:
                        candidate_count = len(candidates)
                    else:
                        candidate_count = store.count_search_nodes(target)
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
            # Sources reached through the qualified target. A source found
            # here is proven, so the bare-name fallback below must not also
            # claim it; repeated calls from one source stay separate rows.
            seen_sources: set[str] = set()
            for e in store.iter_edges_by_target(qn):
                if e.kind == "CALLS":
                    caller = lookup_node(e.source_qualified)
                    if caller:
                        seen_sources.add(e.source_qualified)
                        add_call_site(
                            e.source_qualified, node_to_dict(caller), e,
                            certain=True,
                        )
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
                    # A bare target plus a matching name is not evidence that
                    # this node was called: `some_dict.get(...)` writes the
                    # target `get`. Where the parser recorded what the
                    # receiver is, hold the fallback to the same rule the
                    # endpoint resolver applies. See: #997
                    if not store.receiver_evidence_admits(e.extra, node):
                        continue
                    if e.source_qualified in seen_sources:
                        continue
                    caller = lookup_node(e.source_qualified)
                    if caller:
                        add_call_site(
                            e.source_qualified, node_to_dict(caller), e,
                            certain=False,
                        )

        elif pattern == "references_to":
            seen_reference_sources: set[str] = set()
            for e in store.iter_edges_by_target(qn):
                if e.kind != "REFERENCES":
                    continue
                source = lookup_node(e.source_qualified)
                if source:
                    seen_reference_sources.add(e.source_qualified)
                    add_call_site(
                        e.source_qualified, node_to_dict(source), e, certain=True,
                    )
            # Fallback: a module that imports the symbol through a specifier
            # the parser cannot resolve (npm workspace alias, tsconfig path
            # mapping) stores the REFERENCES target as a bare name rather than
            # "<file>::<Symbol>". Without this pass those dependents are
            # dropped silently, which reads as proof of absence. Only merge
            # when the bare name identifies exactly one node, so a name shared
            # by two symbols is never attributed to both.
            if node and store.count_nodes_by_name(node.name) == 1:
                for e in store.iter_edges_by_target_name(
                    node.name,
                    kind="REFERENCES",
                    language=node.language or None,
                ):
                    if (
                        "ambiguous_targets" in e.extra
                        or e.source_qualified in seen_reference_sources
                        or not store.receiver_evidence_admits(e.extra, node)
                    ):
                        continue
                    source = lookup_node(e.source_qualified)
                    if source:
                        add_call_site(
                            e.source_qualified, node_to_dict(source), e,
                            certain=False,
                        )

        elif pattern == "callees_of":
            # The candidate list for an unresolved target is the same list at
            # every call site, so it is spelled out once per target.
            described_targets: set[str] = set()
            for e in store.iter_edges_by_source(qn):
                if e.kind != "CALLS":
                    continue
                callee = lookup_node(e.target_qualified)
                if callee:
                    add_call_site(
                        e.target_qualified, node_to_dict(callee), e, certain=True,
                    )
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
                    first_sighting = e.target_qualified not in described_targets
                    described_targets.add(e.target_qualified)
                    if isinstance(unresolved, list) and first_sighting:
                        candidate_resolution = (
                            "ambiguous"
                            if e.extra.get("ambiguous_targets")
                            else "unresolved"
                        )
                        result["resolution"] = candidate_resolution
                        result["candidates"] = [
                            _sanitize_name(candidate)
                            for candidate in unresolved[:20]
                            if isinstance(candidate, str)
                        ]
                        candidate_count = e.extra.get(
                            f"{candidate_resolution}_target_count",
                        )
                        if not isinstance(candidate_count, int):
                            candidate_count = len(unresolved)
                        result["candidate_count"] = candidate_count
                        result["candidates_truncated"] = bool(
                            e.extra.get(
                                f"{candidate_resolution}_targets_truncated",
                            )
                            or candidate_count > len(result["candidates"])
                        )
                    add_call_site(
                        e.target_qualified, result, e, certain=False,
                    )

        elif pattern == "imports_of":
            for e in store.iter_edges_by_source(qn):
                if e.kind == "IMPORTS_FROM":
                    row: dict[str, Any] = {"import_target": e.target_qualified}
                    scope = e.extra.get(IMPORT_SCOPE_KEY)
                    if scope:
                        # The target is a directory, not a file: say so
                        # rather than let a reader take it for a path that
                        # should have a node.
                        row["import_target_kind"] = scope
                    add_result(row, e)

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
            # A Go import names a package and a Ruby ``require_all`` names a
            # tree, so those edges target a DIRECTORY. One edge per import
            # rather than one per file in the package is what keeps the graph
            # linear in imports; expanding the directory here is the other
            # half of that trade. See IMPORT_SCOPE_KEY in constants.py.
            for directory, scopes in import_scope_ancestors(abs_target):
                for e in store.iter_edges_by_target(directory):
                    if e.kind != "IMPORTS_FROM":
                        continue
                    if e.extra.get(IMPORT_SCOPE_KEY) not in scopes:
                        continue
                    if e.source_qualified in seen_importers:
                        continue
                    seen_importers.add(e.source_qualified)
                    add_result({
                        "importer": e.source_qualified,
                        "file": e.file_path,
                        "via_package": directory,
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
                # Ambiguity is measured on the indexed name alone: several
                # declarations answer to this bare base name. Which one it
                # binds to is #943's work — file namespaces and imports cannot
                # prove it — so caveat the matches instead of guessing.
                languages = (
                    _compatible_edge_languages(node.language) if node.language else (None,)
                )
                ambiguous_base = sum(
                    store.count_nodes_by_name(node.name, language=language, kinds=("Class", "Type"))
                    for language in languages
                ) > 1
                for kind in ("INHERITS", "IMPLEMENTS"):
                    for e in store.iter_edges_by_target_name(
                        node.name, kind=kind, language=node.language or None,
                    ):
                        child = store.get_node(e.source_qualified)
                        if child:
                            child_result = node_to_dict(child)
                            if ambiguous_base:
                                child_result["inferred_by"] = "bare_name"
                            add_result(child_result, e)

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

        resolution_split: dict[str, int] | None = None
        distinct_nodes = 0
        if pattern in _CALL_SITE_PATTERNS:
            resolution_split = {RESOLUTION_DIRECT: 0, RESOLUTION_UNRESOLVED: 0}
            distinct_keys: set[str] = set()
            for key, row, _edge in call_sites:
                distinct_keys.add(key)
                bucket = (
                    RESOLUTION_UNRESOLVED
                    if row.get("target_resolution") == RESOLUTION_UNRESOLVED
                    else RESOLUTION_DIRECT
                )
                resolution_split[bucket] += 1
            distinct_nodes = len(distinct_keys)
            for row, edge in _interleave_call_sites(call_sites):
                add_result(row, edge)

        results_omitted = max(0, total_results - len(results))
        if pattern in _CALL_SITE_PATTERNS:
            summary = (
                f"Found {total_results} call site(s) across {distinct_nodes} "
                f"node(s) for {pattern}('{target}')"
            )
        else:
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
            result_fields: tuple[str, ...] = ("name", "kind", "file_path", "indirect")
            if pattern == "inheritors_of":
                result_fields += ("inferred_by",)
            if pattern in _CALL_SITE_PATTERNS:
                # The line is what turns a name into a location, and it costs
                # one integer. ``target_resolution`` appears only on the rows
                # that are guesses, so certain rows pay nothing for it.
                result_fields += ("call_site", "target_resolution")
            minimal_results = [
                {
                    k: r[k]
                    for k in result_fields
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
            if resolution_split is not None:
                minimal_response["distinct_nodes"] = distinct_nodes
                minimal_response["resolution_split"] = resolution_split
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
        if resolution_split is not None:
            response["distinct_nodes"] = distinct_nodes
            response["resolution_split"] = resolution_split
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
                for r in results[:5]
            ]
            minimal_response: dict[str, Any] = {
                "status": "ok",
                "query": query,
                "search_mode": search_mode,
                "summary": summary,
                "results": minimal_results,
                "result_count": len(results),
                "results_omitted": max(0, len(results) - len(minimal_results)),
            }
            if confidence:
                minimal_response["confidence"] = confidence
            return minimal_response

        result: dict[str, object] = {
            "status": "ok",
            "query": query,
            "search_mode": search_mode,
            "summary": summary,
            "results": results,
        }
        if confidence:
            result["confidence"] = confidence
        result["_hints"] = generate_hints(
            "semantic_search_nodes_tool", result, get_session()
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

        # CALLS is the largest edge kind and the one a reader trusts most, so
        # say up front how much of it is bound to a node and how much is a
        # bare name the query layer can only match by name.
        calls_by_resolution = store.count_edges_by_resolution("CALLS")
        summary_parts.append("")
        summary_parts.append(
            "CALLS by target resolution: "
            f"{calls_by_resolution['direct']} direct, "
            f"{calls_by_resolution['unresolved']} unresolved"
        )

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
            "calls_by_resolution": calls_by_resolution,
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
