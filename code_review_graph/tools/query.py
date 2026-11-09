"""Tools 2, 3, 5, 6, 9: query / search / stats helpers."""

from __future__ import annotations

import logging
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from ..config_keys import normalize_spring_config_key
from ..constants import IMPORT_SCOPE_KEY, MAX_IMPACT_NODES
from ..context_history import CallInfo
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
    followed_files,
    get_db_path,
    path_within_repo,
)
from ..parser import is_test_file, normalize_file_path, repo_relative_path
from ..search import hybrid_search
from ..uncertainty import (
    empty_impact_confidence,
    empty_query_confidence,
    empty_search_confidence,
    guessed_calls_confidence,
)
from ._common import (
    _BUILTIN_CALL_NAMES,
    _bounded,
    _error_response,
    _get_store,
    _resolve_graph_file_paths,
    _resolve_graph_file_paths_with_unmatched,
    _shown_of,
    _unmatched_file_warning,
    _validate_positive_int,
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

# Hard ceilings for the other query tools. A caller may lower a bound but not ask
# for a reply that dwarfs the client: at a huge ``limit`` these returned 1.8M
# tokens on this repository. They bound the work as well as the reply, so a small
# request stays cheap. A minimal row is about a third of a full one, so its
# ceiling has to be lower than three times the full one for minimal to cost less.
_MAX_SEARCH_RESULTS_STANDARD = 100
_MAX_SEARCH_RESULTS_MINIMAL = 5
_MAX_LARGE_FUNCTIONS_STANDARD = 100
_MAX_LARGE_FUNCTIONS_MINIMAL = 25
_MAX_TRAVERSAL_TOKEN_BUDGET = 10_000
# Search ranks this many times what it shows. ``total`` counts that window, so a
# full window means "at least this many", not the size of the whole graph.
_SEARCH_WINDOW_FACTOR = 3

# Source snippets attached to search hits when include_source is set. Without
# them a caller only gets names and line numbers, so it has to open the files
# itself and the graph query becomes additive rather than a substitute.
_MAX_SEARCH_SOURCE_LINES = 240
_MAX_SEARCH_SOURCE_LINES_PER_NODE = 60

# Expanding a hit to its callers and callees costs more lines, but it is what
# turns a search result into an answer: "how does X work" is nearly always
# answered by X plus the code on either side of it. Without this the caller
# reads those files itself and the graph has saved nothing.
_MAX_EXPANDED_SOURCE_LINES = 700
_MAX_EXPANDED_HITS = 3
_MAX_NEIGHBORS_PER_HIT = 4

# Lines alone do not bound a reply: one minified line can be megabytes.
_MAX_SEARCH_SOURCE_CHARS = 80_000
_MAX_SEARCH_SOURCE_CHARS_PER_NODE = 12_000

# _sanitize_name cuts names sent to clients to this length.
_SANITIZED_NAME_MAX = 256

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
    "advises": "Find methods an AOP advice targets via its pointcut",
    "advised_by": "Find AOP advice methods that intercept a given method",
    "handlers_of": "Find methods that handle an endpoint",
    "endpoints_for": "Find endpoints handled by a method",
    "consumers_of": "Find classes that consume a Spring configuration property",
    "file_summary": "Get a summary of all nodes in a file",
    "path_to": "Find how a given symbol or file reaches another",
}

_JAVA_FQN_PART = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
#: A test block's name, such as ``it:adds@L12``, which ends in its line number.
_TEST_BLOCK_NAME = re.compile(r"@L\d+$")
_MAX_DOTTED_TARGET_CANDIDATES = 100
_MAX_BARE_TARGET_CANDIDATES = 20

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
    Within a round, calls bound to a real node come before guesses, or a few
    bare ``len`` calls would fill the window and hide the real callee. The rest
    of the order is first-seen, so the result stays deterministic.
    """
    seen_order: dict[str, int] = {}
    occurrence: dict[str, int] = {}
    keyed: list[tuple[int, bool, int, int, dict[str, Any], GraphEdge]] = []
    for index, (key, result, edge) in enumerate(sites):
        seen_order.setdefault(key, len(seen_order))
        round_index = occurrence.get(key, 0)
        occurrence[key] = round_index + 1
        is_guess = result.get("target_resolution") == RESOLUTION_UNRESOLVED
        keyed.append((round_index, is_guess, seen_order[key], index, result, edge))
    keyed.sort(key=lambda item: item[:4])
    return [(result, edge) for _, _, _, _, result, edge in keyed]


def _test_is_named_after(test: GraphNode, name: str) -> bool:
    """Whether a naming-convention hit really is named after *name*.

    A test block's name holds free text, so ``it "tests json output"`` also hits
    a search for ``test_json``. Only its file can speak for a block.
    """
    if not _TEST_BLOCK_NAME.search(test.name):
        return True
    word = re.escape(name.lower())
    return bool(
        re.search(rf"(?:^|[^a-z0-9]){word}(?:[^a-z0-9]|$)", Path(test.file_path).name.lower())
    )


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


def _namespaced_member_candidates(store: GraphStore, target: str) -> list[GraphNode]:
    """Resolve ``Class.method`` to a Ruby or C# method whose type sits in a namespace.

    Ruby and C# symbols carry the whole namespace (``Billing.Base.audit``,
    ``Lib.Service.Run``), so the exact qualified-tail lookup misses the short
    form people actually type.
    """
    method_name = target.rsplit(".", 1)[-1]
    return [
        candidate
        for candidate in store.search_nodes(
            method_name, limit=_MAX_DOTTED_TARGET_CANDIDATES,
        )
        if candidate.language.lower() in ("ruby", "csharp")
        and candidate.name == method_name
        and candidate.qualified_name.endswith(f".{target}")
    ]


def _exact_name_candidates(
    store: GraphStore, root: Path, target: str,
) -> tuple[list[GraphNode], int]:
    """Return the nodes a bare *target* names exactly, and the tests set aside.

    The full-text index tokenises, so ``hybrid_search`` also hits every
    ``test_hybrid_search_*``. A name written in full means that name. A test or
    test helper with the same name gives way to the real symbol; when only test
    symbols have the name, they are the answer.
    """
    exact = store.get_nodes_by_name(target)
    real = [
        candidate
        for candidate in exact
        if not candidate.is_test and not is_test_file(candidate.file_path, root)
    ]
    if real:
        return real, len(exact) - len(real)
    return exact, 0


def _selectable_qualified_name(node: GraphNode, root: Path) -> str:
    """Return a qualified name that still selects *node* when sent back.

    A long checkout path can push a name past the cut ``_sanitize_name`` makes,
    and a cut name selects nothing. ``query_graph`` also reads a name relative
    to the repo root, which is much shorter.
    """
    qualified = node.qualified_name
    prefix = f"{normalize_file_path(root)}/"
    if len(qualified) > _SANITIZED_NAME_MAX and qualified.startswith(prefix):
        qualified = qualified[len(prefix):]
    return _sanitize_name(qualified)


def _rank_disambiguation_candidates(
    candidates: list[GraphNode], target: str, root: Path,
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

    return [
        {
            **node_to_dict(node),
            "qualified_name": _selectable_qualified_name(node, root),
        }
        for node in sorted(candidates, key=score)
    ]


def _compact_impact_response(
    result: dict[str, Any], root: Path, changed_files: list[str],
    unmatched_files: list[str], max_files: int, resolution: str,
    unresolved_call_sites: int,
) -> dict[str, Any]:
    """Project the radius onto files without serializing its graph records."""
    def requested_path(path: str) -> str:
        # The caller named this file, so show it as named, made relative to
        # the repository when it was given as an absolute path inside it.
        relative = repo_relative_path(path, root)
        return path if relative is None else relative

    def graph_path(path: str) -> str:
        relative = repo_relative_path(path, root)
        if relative is None:
            # A graph may include an external file: retain its identity.
            return path
        stored_relative = repo_relative_path(path) is not None
        if stored_relative and not (root / relative).exists():
            # A cwd-relative graph path (``fixtures/repo/src/app.py`` for root
            # ``.../fixtures/repo``) starts with the root's trailing parts.
            # Only a path the graph stored relative gets this: an absolute
            # one under the root is exact already, and a file deleted since
            # the build must not lose a directory that shares the root's name
            # (``django/x.py`` in a checkout called ``django``).
            parts = relative.split("/")
            for k in range(min(len(root.parts), len(parts) - 1), 0, -1):
                if tuple(parts[:k]) == root.parts[-k:]:
                    return "/".join(parts[k:])
        return relative

    # The graph already ranks nodes best-first. Keep each file's first
    # appearance, rather than using impacted_files' unordered set order.
    file_set = set(result["impacted_files"])
    ranked_files = list(dict.fromkeys(
        node.file_path for node in result["impacted_nodes"]
        if node.file_path in file_set
    ))
    ranked_set = set(ranked_files)
    ranked_files.extend(sorted(file_set - ranked_set))
    shown, total, cut = _bounded(ranked_files, max_files, _MAX_IMPACT_FILES)
    # The changed-file list is the caller's own, and a whole-repo diff makes
    # it as long as the repository: it gets the same display limit.
    changed_shown, changed_total, changed_cut = _bounded(
        changed_files, max_files, _MAX_IMPACT_FILES,
    )
    unmatched_shown, unmatched_total, unmatched_cut = _bounded(
        unmatched_files, max_files, _MAX_IMPACT_FILES,
    )
    return {
        "status": "ok",
        "summary": f"Showing {len(shown)} of {total} impacted files (best-first).",
        "changed_files": [requested_path(path) for path in changed_shown],
        "changed_file_count": changed_total,
        "changed_files_omitted": changed_total - len(changed_shown),
        # Changed files the graph has no nodes for, so a short radius is not
        # read as a small one.
        "unmatched_files": [requested_path(path) for path in unmatched_shown],
        "unmatched_files_total": unmatched_total,
        "unmatched_files_truncated": unmatched_cut,
        "impacted_files": [graph_path(path) for path in shown],
        "impacted_file_count": total,
        "impacted_files_omitted": total - len(shown),
        "total_impacted": result["total_impacted"],
        "truncated": result["truncated"] or cut or changed_cut or unmatched_cut,
        "resolution": resolution,
        "unresolved_call_sites": unresolved_call_sites,
    }


# How sure a hop is, strongest first, by how it was bound. This ranks first:
# graphs built before #984 was fixed store 1.0 even on an edge that names no node,
# so the stored score only breaks ties within a level.
_HOP_CERTAINTY = ("direct", "unique_name", "shared_name")
_HOP_DIRECT, _HOP_UNIQUE_NAME, _HOP_SHARED_NAME = range(len(_HOP_CERTAINTY))

# TESTED_BY stays out: "A is tested by a test that calls B" is no route from A to B.
_ROUTE_EDGE_KINDS = frozenset({
    "CALLS", "REFERENCES", "INHERITS", "IMPLEMENTS", "IMPORTS_FROM", "CONTAINS",
})
_NAME_MATCH_EDGE_KINDS = frozenset({"CALLS", "REFERENCES", "INHERITS", "IMPLEMENTS"})
# A bare name more symbols than this share says nothing about any one of them.
_MAX_NAME_MATCH_CANDIDATES = 10
_MAX_ROUTE_VISITS = 200_000
_MAX_ROUTE_HOPS = 30


_Hop = tuple[str, int, GraphEdge]
_Route = list[tuple[str, str, int, GraphEdge]]
# Resolver candidate lists and the totals they record, under these keys.
_CANDIDATE_KEYS = (
    ("ambiguous_targets", "ambiguous_target_count"),
    ("unresolved_targets", "unresolved_target_count"),
)


class _RouteGraph:
    """The graph as hops, read only as far as the search walks.

    Nodes and outgoing edges come from indexed lookups when first reached.
    ``nodes.name`` has no index, so the bare-name table is read once, and only
    when a route has to guess at a name.
    """

    def __init__(self, store: GraphStore, root: Path) -> None:
        self._store = store
        self._root = root
        self._nodes: dict[str, GraphNode | None] = {}
        self._by_name: dict[str, list[GraphNode]] | None = None
        self._direct: dict[str, list[_Hop]] = {}
        self._all: dict[str, list[_Hop]] = {}
        self._edges: dict[str, list[GraphEdge]] = {}

    def node(self, qualified_name: str) -> GraphNode | None:
        if qualified_name not in self._nodes:
            self._nodes[qualified_name] = self._store.get_node(qualified_name)
        return self._nodes[qualified_name]

    def _is_test(self, node: GraphNode) -> bool:
        return node.is_test or is_test_file(node.file_path, self._root)

    def _edges_from(self, qualified_name: str) -> list[GraphEdge]:
        if qualified_name not in self._edges:
            self._edges[qualified_name] = [
                edge
                for edge in self._store.iter_edges_by_source(qualified_name)
                if edge.kind in _ROUTE_EDGE_KINDS
            ]
        return self._edges[qualified_name]

    def _named(self, name: str) -> list[GraphNode]:
        if self._by_name is None:
            self._by_name = defaultdict(list)
            for node in self._store.get_all_nodes(exclude_files=True):
                self._by_name[node.name].append(node)
        return self._by_name.get(name, [])

    def _name_matches(self, edge: GraphEdge, source: GraphNode) -> list[GraphNode]:
        """The nodes a bare edge target could mean, by the rules callers_of uses."""
        name = edge.target_qualified
        if name in _BUILTIN_CALL_NAMES or "::" in name:
            return []
        languages = _compatible_edge_languages(source.language) if source.language else None
        matches = [
            candidate
            for candidate in self._named(name)
            if candidate.kind != "File"
            and not self._is_test(candidate)
            and (languages is None or candidate.language in languages)
            and GraphStore.receiver_evidence_admits(edge.extra, candidate)
        ]
        return matches if len(matches) <= _MAX_NAME_MATCH_CANDIDATES else []

    def _guessed_hops(self, edge: GraphEdge, source: GraphNode) -> list[_Hop]:
        if edge.kind not in _NAME_MATCH_EDGE_KINDS:
            return []
        for key, count_key in _CANDIDATE_KEYS:
            named = edge.extra.get(key)
            if isinstance(named, list):
                # The resolver declined to pick: these are its candidates. The
                # stored list is cut at 20, so its recorded total counts too.
                recorded = edge.extra.get(count_key)
                total = max(len(named), recorded if isinstance(recorded, int) else 0)
                if total > _MAX_NAME_MATCH_CANDIDATES:
                    return []
                hops: list[_Hop] = []
                for candidate in named:
                    node = self.node(candidate) if isinstance(candidate, str) else None
                    if node is not None and not self._is_test(node):
                        hops.append((node.qualified_name, _HOP_SHARED_NAME, edge))
                return hops
        matches = self._name_matches(edge, source)
        certainty = _HOP_UNIQUE_NAME if len(matches) == 1 else _HOP_SHARED_NAME
        return [(match.qualified_name, certainty, edge) for match in matches]

    @staticmethod
    def _ordered(hops: list[_Hop]) -> list[_Hop]:
        return sorted(hops, key=lambda hop: (hop[1], hop[0], hop[2].line))

    def hops_from(self, qualified_name: str, weakest_allowed: int) -> list[_Hop]:
        """Every hop out of a node, in a fixed order, strongest first.

        Name guesses are worked out only once a search allows them.
        """
        if qualified_name not in self._direct:
            direct: list[_Hop] = []
            for edge in self._edges_from(qualified_name):
                node = self.node(edge.target_qualified)
                if node is not None:
                    direct.append((node.qualified_name, _HOP_DIRECT, edge))
            self._direct[qualified_name] = self._ordered(direct)
        if weakest_allowed == _HOP_DIRECT:
            return self._direct[qualified_name]
        if qualified_name not in self._all:
            source = self.node(qualified_name)
            guessed: list[_Hop] = []
            if source is not None:
                for edge in self._edges_from(qualified_name):
                    if self.node(edge.target_qualified) is None:
                        guessed.extend(self._guessed_hops(edge, source))
            self._all[qualified_name] = self._ordered(self._direct[qualified_name] + guessed)
        return self._all[qualified_name]

    def _search(
        self, source: str, target: str, weakest_allowed: int,
    ) -> tuple[_Route | None, bool]:
        """Breadth-first search; also say whether it stopped at the visit limit."""
        came_from: dict[str, tuple[str, int, GraphEdge] | None] = {source: None}
        queue = deque([source])
        visits = 0
        while queue and visits < _MAX_ROUTE_VISITS:
            visits += 1
            current = queue.popleft()
            for next_name, certainty, edge in self.hops_from(current, weakest_allowed):
                if certainty > weakest_allowed or next_name in came_from:
                    continue
                came_from[next_name] = (current, certainty, edge)
                if next_name == target:
                    route: _Route = []
                    step = next_name
                    while (entry := came_from[step]) is not None:
                        route.append((entry[0], step, entry[1], entry[2]))
                        step = entry[0]
                    return route[::-1], True
                queue.append(next_name)
        return None, not queue

    def route(self, source: str, target: str) -> tuple[_Route | None, bool]:
        """The route whose weakest hop is strongest; the shortest of those.

        Trying each level in turn means a longer route through bound edges is
        preferred to a shorter one that guesses at a name. A level that stops at
        the visit limit has not ruled out a stronger route, so the search ends
        there and says it is incomplete rather than settle for a weaker one.
        """
        for weakest_allowed in range(len(_HOP_CERTAINTY)):
            route, complete = self._search(source, target, weakest_allowed)
            if route is not None or not complete:
                return route, complete
        return None, True


def _route_endpoint_candidates(
    store: GraphStore, root: Path, graph: _RouteGraph, name: str,
) -> tuple[list[GraphNode], int]:
    """Return the nodes *name* could mean and how many there are in all."""
    for key in (name, normalize_file_path(root / name)):
        if (node := graph.node(key)) is not None:
            return [node], 1
    exact = store.get_nodes_by_name(name)
    real = [
        candidate
        for candidate in exact
        if not candidate.is_test and not is_test_file(candidate.file_path, root)
    ]
    candidates = sorted(real or exact, key=lambda node: node.qualified_name)
    if candidates:
        return candidates, len(candidates)
    if java_candidates := _java_fqn_candidates(store, name):
        return java_candidates, len(java_candidates)
    if "." in name:
        tail = store.search_nodes_by_qualified_tail(
            name, limit=_MAX_DOTTED_TARGET_CANDIDATES,
        )
        if tail:
            return tail, store.count_nodes_by_qualified_tail(name)
    return store.search_nodes(name, limit=20), store.count_search_nodes(name)


def _unresolved_route_endpoint(
    store: GraphStore,
    root: Path,
    name: str,
    which: str,
    candidates: list[GraphNode],
    count: int,
    minimal: bool,
) -> dict[str, Any]:
    """Say which end of a route could not be pinned down, and why."""
    if not candidates:
        response: dict[str, Any] = {
            "status": "not_found",
            "endpoint": which,
            "summary": f"No node found matching {which} '{name}'.",
        }
        note = empty_query_confidence(store, root, "path_to", name, None)
        if note:
            response["confidence"] = note
        return response
    ranked = _rank_disambiguation_candidates(candidates[:20], name, root)
    if minimal:
        ranked = [
            {key: row[key] for key in ("qualified_name", "kind")} for row in ranked[:5]
        ]
    response = {
        "status": "ambiguous",
        "endpoint": which,
        "summary": (
            f"{which} '{name}' matches {count} node(s). "
            "Re-run with a qualified_name from candidates."
        ),
        "candidates": ranked,
        "candidate_count": count,
        "candidates_truncated": count > len(ranked),
        "hint": f"Use a qualified_name from candidates as {which}.",
    }
    if not minimal:
        response["disambiguation"] = ranked
    return response


def _query_route(
    store: GraphStore, root: Path, target: str, to: str, minimal: bool,
) -> dict[str, Any]:
    """Answer ``path_to``: how *target* reaches *to*, and how sure that is."""
    graph = _RouteGraph(store, root)
    ends: list[GraphNode] = []
    for name, which in ((target, "target"), (to, "to")):
        candidates, count = _route_endpoint_candidates(store, root, graph, name)
        if len(candidates) != 1:
            return _unresolved_route_endpoint(
                store, root, name, which, candidates, count, minimal,
            )
        ends.append(candidates[0])
    start, finish = ends
    root_prefix = f"{normalize_file_path(root)}/"

    def relative(path: str) -> str:
        return _sanitize_name(path.removeprefix(root_prefix))

    def label(node: GraphNode) -> str:
        """The qualified name from the repository root: ``pkg/a.py::First.run``.

        A bare name would make ``First.run`` and ``Second.run`` both read ``run``.
        """
        return relative(node.qualified_name)

    response: dict[str, Any] = {
        "status": "ok",
        "pattern": "path_to",
        "target": start.qualified_name,
        "to": finish.qualified_name,
        "description": _QUERY_PATTERNS["path_to"],
        "result_count": 0,
        "results_omitted": 0,
        "results": [],
    }
    if start.qualified_name == finish.qualified_name:
        response["summary"] = "target and to are the same node, so there are no hops."
        return response

    route, complete = graph.route(start.qualified_name, finish.qualified_name)
    if route is None and not complete:
        response["search_incomplete"] = True
        response["summary"] = (
            f"The search stopped after {_MAX_ROUTE_VISITS} nodes without reaching "
            f"'{label(finish)}', so a route may still exist. Try nearer ends."
        )
        return response
    if route is None:
        response["summary"] = (
            f"No route from '{label(start)}' to '{label(finish)}' through calls, "
            "references, inheritance, imports or containment. Callbacks and dynamic "
            "dispatch are not in the graph, so this does not prove there is none."
        )
        if graph.route(finish.qualified_name, start.qualified_name)[0] is not None:
            response["reverse_route_exists"] = True
            response["summary"] += " There is a route the other way: swap target and to."
        note = empty_query_confidence(store, root, "path_to", target, start)
        if note:
            response["confidence"] = note
        return response

    def hop_row(source: str, destination: str, certainty: int, edge: GraphEdge) -> dict[str, Any]:
        if minimal:
            row: dict[str, Any] = {
                "from": relative(source),
                "to": relative(destination),
                "kind": edge.kind,
                "line": edge.line,
                "certainty": _HOP_CERTAINTY[certainty],
            }
            # The call site is in the source's own file unless this says otherwise.
            source_node = graph.node(source)
            if source_node is not None and edge.file_path != source_node.file_path:
                row["file"] = relative(edge.file_path)
            return row
        row = {
            "from": _sanitize_name(source),
            "to": _sanitize_name(destination),
            "kind": edge.kind,
            "file_path": edge.file_path,
            "line": edge.line,
            "certainty": _HOP_CERTAINTY[certainty],
        }
        if edge.confidence < 1.0:
            row["confidence"] = round(edge.confidence, 2)
        return row

    # How the hop was bound first; the stored score only breaks a tie.
    weakest = max(route, key=lambda hop: (hop[2], -hop[3].confidence))
    weakest_name = _HOP_CERTAINTY[weakest[2]]
    response["result_count"] = len(route)
    response["results_omitted"] = max(0, len(route) - _MAX_ROUTE_HOPS)
    response["results"] = [hop_row(*hop) for hop in route[:_MAX_ROUTE_HOPS]]
    response["certainty"] = weakest_name
    response["weakest_hop"] = hop_row(*weakest)
    response["summary"] = (
        f"'{label(start)}' reaches '{label(finish)}' in {len(route)} hop(s); "
        f"weakest hop: {weakest_name}."
    )
    return response


def get_impact_radius(
    changed_files: list[str] | None = None,
    max_depth: int = 2,
    max_results: int = MAX_IMPACT_NODES,
    repo_root: str | None = None,
    base: str = "HEAD~1",
    detail_level: str = "standard",
    resolution: str = RESOLUTION_ALL,
    max_files: int = 25,
) -> dict[str, Any]:
    """Analyze the blast radius of changed files.

    Args:
        changed_files: Explicit list of changed file paths (relative to repo root).
                       If omitted, auto-detects from git diff.
        max_depth: How many hops to traverse in the graph (default: 2).
        max_results: Maximum impacted nodes to return (default:
            ``CRG_MAX_IMPACT_NODES``, or 500).
        repo_root: Repository root path. Auto-detected if omitted.
        base: Git ref for auto-detecting changes (default: HEAD~1).
        detail_level: "standard" (full output), "minimal" (summary only),
                      or "compact" (ranked file paths and counts, no node/edge records).
        max_files: Compact-mode file display limit (default: 25, hard ceiling: 200).
                   Independent of max_results, so it does not shrink the traversal.
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
    if isinstance(max_files, bool) or not isinstance(max_files, int) or max_files < 1:
        raise ValueError("max_files must be an integer greater than or equal to 1")
    if resolution not in IMPACT_RESOLUTIONS:
        raise ValueError(
            f"resolution must be one of {list(IMPACT_RESOLUTIONS)}, got {resolution!r}"
        )

    store, root = _get_store(repo_root)
    try:
        if changed_files is None:
            changed_files, base = discover_review_changes(root, base)

        if not changed_files:
            if detail_level == "compact":
                return _compact_impact_response(
                    {"impacted_files": [], "impacted_nodes": [],
                     "total_impacted": 0, "truncated": False},
                    root, [], [], max_files, resolution, 0,
                )
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
        history_call = CallInfo(
            data_dir=store.db_path.parent, tool="get_impact_radius_tool",
            detail_level=detail_level, files=len(changed_files), depth=max_depth,
        )
        abs_files, unmatched_files = _resolve_graph_file_paths_with_unmatched(
            store, root, changed_files,
        )
        shown_unmatched, unmatched_total, unmatched_cut = _bounded(
            unmatched_files, 5 if detail_level == "minimal" else max_results, _MAX_IMPACT_FILES,
        )
        result = store.get_impact_radius(
            abs_files, max_depth=max_depth, max_nodes=max_results,
            resolution=resolution,
        )

        if detail_level == "compact":
            compact_response = _compact_impact_response(
                result, root, changed_files, unmatched_files, max_files, resolution,
                store.count_unresolved_call_sites({
                    n.name for n in result["changed_nodes"] if n.kind != "File"
                }),
            )
            if not result["impacted_nodes"]:
                language = next(
                    (n.language for n in result["changed_nodes"] if n.language), None,
                )
                confidence = empty_impact_confidence(
                    store, root, changed_files, abs_files, language,
                )
                if confidence:
                    compact_response["confidence"] = confidence
            attach_context_savings(compact_response, original_tokens=original_tokens)
            return compact_response

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
        risk = (
            "unknown" if not abs_files else
            "high" if total_impacted > 20 else
            "medium" if total_impacted > 5 else "low"
        )

        summary_parts = [
            f"Blast radius for {len(changed_files)} changed file(s):",
            f"  - {len(result['changed_nodes'])} nodes directly changed",
            f"  - {total_impacted} nodes impacted (within {max_depth} hops)",
            f"  - {files_total} additional files affected",
        ]
        if unmatched_files:
            summary_parts.append(_unmatched_file_warning(unmatched_files))
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
            key_entities = [
                n["name"] for n in impacted_dicts[:5]
            ]
            minimal_response = {
                "status": "ok",
                "summary": "\n".join(summary_parts),
                "risk": risk,
                "unmatched_files": shown_unmatched,
                "unmatched_files_total": unmatched_total,
                "unmatched_files_truncated": unmatched_cut,
                "impacted_file_count": len(result["impacted_files"]),
                "key_entities": key_entities,
                "truncated": truncated or unmatched_cut,
                "nodes_omitted": max(0, total_impacted - len(impacted_dicts)),
                # One integer, and the only thing in this response that says
                # the radius has a blind spot at all.
                "unresolved_call_sites": unresolved_call_sites,
            }
            if confidence:
                minimal_response["confidence"] = confidence
            attach_context_savings(
                minimal_response, original_tokens=original_tokens, call=history_call,
            )
            return minimal_response

        response: dict[str, Any] = {
            "status": "ok",
            "risk": risk,
            "unmatched_files": shown_unmatched,
            "unmatched_files_total": unmatched_total,
            "unmatched_files_truncated": unmatched_cut,
            "summary": "\n".join(summary_parts),
            "changed_files": changed_files,
            "changed_nodes": changed_dicts,
            "changed_nodes_omitted": changed_nodes_omitted,
            "impacted_nodes": impacted_dicts,
            "impacted_files": shown_files,
            "impacted_files_omitted": files_omitted,
            "edges": edge_dicts,
            "edges_omitted": edges_omitted,
            "truncated": truncated or unmatched_cut,
            "total_impacted": total_impacted,
            "nodes_omitted": max(0, total_impacted - len(impacted_dicts)),
            "resolution": resolution,
            "unresolved_call_sites": unresolved_call_sites,
        }
        if confidence:
            response["confidence"] = confidence
        attach_context_savings(response, original_tokens=original_tokens, call=history_call)
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
    to: str | None = None,
) -> dict[str, Any]:
    """Run a predefined graph query.

    Args:
        pattern: Query pattern. One of: callers_of, references_to, callees_of,
                 imports_of, importers_of, children_of, tests_for, inheritors_of,
                 triggers_of, triggered_by, publishers_of, listeners_of,
                 handlers_of, endpoints_for, consumers_of, advises, advised_by,
                 file_summary, path_to.
        target: The node name, qualified name, or file path to query about.
        repo_root: Repository root path. Auto-detected if omitted.
        detail_level: "standard" (full output) or "minimal" (summary only).
        max_results: Maximum results to return. Minimal mode additionally caps
            visible results at five and reports the exact omitted count.
        resolution: For callers_of/callees_of/references_to, filter rows by how
            certain the edge's target attribution is. "all" (default) keeps
            both, "direct" keeps only calls bound to an indexed node, and
            "unresolved" keeps only the bare-name matches.
        to: For path_to, the node, qualified name or file to reach. A route is one
            answer, so max_results and the minimal cap do not cut it.

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
        if pattern == "path_to":
            if not to:
                return {
                    "status": "error",
                    "error": "path_to needs to: the node or file to reach.",
                }
            return _query_route(store, root, target, to, minimal)
        response_limit = min(max_results, 5) if minimal else max_results
        results: list[dict[str, Any]] = []
        edges_out: list[dict[str, Any]] = []
        total_results = 0
        # Results matched by a name or an unpinned call, not a traced edge.
        inferred_results = 0
        # (dedupe key, row, edge) for the call-site patterns, collected before
        # emission so repeats can be interleaved behind first sightings.
        call_sites: list[tuple[str, dict[str, Any], GraphEdge]] = []

        def add_result(result: dict[str, Any], edge: Any | None = None) -> None:
            """Count every logical result but retain only the bounded prefix."""
            nonlocal total_results, inferred_results
            total_results += 1
            if result.get("inferred_by"):
                inferred_results += 1
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
        same_name_tests = 0
        raw_config_target = pattern == "consumers_of" and "::" not in target
        if pattern != "file_summary" and not raw_config_target:
            node = store.get_node(target)
            if not node:
                # Only the path part is a path: the symbol in ``a.xsl::match=/``
                # must keep the slash that normalising would strip.
                target_path, separator, symbol = target.partition("::")
                abs_target = f"{normalize_file_path(root / target_path)}{separator}{symbol}"
                node = store.get_node(abs_target)
            if not node:
                exact_candidates: list[GraphNode] = []
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
                    # Java-shaped with no safe match: do not fall back to a
                    # fuzzy search. A namespaced Ruby or C# method is an exact
                    # match on the type and method, so it is still safe.
                    candidates = _namespaced_member_candidates(store, target)
                else:
                    exact_candidates, same_name_tests = _exact_name_candidates(
                        store, root, target,
                    )
                    candidates = exact_candidates or store.search_nodes(
                        target, limit=_MAX_BARE_TARGET_CANDIDATES,
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
                    # Count the population the candidates were drawn from, not
                    # the truncated slice, so candidates_truncated stays honest
                    # when more than _MAX_DOTTED_TARGET_CANDIDATES nodes match.
                    if java_candidates:
                        candidate_count = len(candidates)
                    elif qualified_tail_candidates:
                        candidate_count = store.count_nodes_by_qualified_tail(target)
                    elif java_candidates is not None:
                        candidate_count = len(candidates)
                    elif exact_candidates:
                        candidate_count = len(candidates)
                        candidates = candidates[:_MAX_BARE_TARGET_CANDIDATES]
                    else:
                        candidate_count = store.count_search_nodes(target)
                    ranked = _rank_disambiguation_candidates(candidates, target, root)
                    if minimal:
                        # Names only, once: the agent needs one of these to
                        # re-run the query, not a full row for each.
                        shortlist = [
                            {key: row[key] for key in ("qualified_name", "kind")}
                            for row in ranked[:response_limit]
                        ]
                        truncated = candidate_count > len(shortlist)
                        return {
                            "status": "ambiguous",
                            "summary": (
                                f"'{target}' matches {candidate_count} node(s). "
                                "Re-run with a qualified_name from candidates."
                            ),
                            "candidates": shortlist,
                            "candidate_count": candidate_count,
                            "candidates_truncated": truncated,
                            "hint": (
                                "Use a qualified_name from candidates as the "
                                "target parameter."
                                + (
                                    " detail_level='standard' lists more."
                                    if truncated
                                    else ""
                                )
                            ),
                        }
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
            # A PHP test may reach this through the container, as in
            # ``app(Service::class)->run()``, with a call the graph could not
            # pin to one class. Name it, labelled, so the answer says the link
            # is a guess (#850).
            for test_qn in store.get_ambiguous_call_tests(qn):
                test = store.get_node(test_qn)
                if test is None or test_qn in seen or not test.is_test:
                    continue
                result = node_to_dict(test)
                result["indirect"] = False
                result["inferred_by"] = "ambiguous_call"
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
                # Java, Kotlin and Ruby name a test class after the class it
                # tests, with the word last. The search also hits every method of
                # such a class, and a method called ``Query`` says nothing about
                # a struct called ``QueryTest``, so this holds for classes alone.
                if node and node.kind == "Class":
                    for suffix in ("Test", "Tests", "IT", "Spec"):
                        test_nodes += [
                            t for t in store.search_nodes(f"{name}{suffix}", limit=10)
                            if t.name == f"{name}{suffix}" and t.kind == "Class"
                        ]
            for t in test_nodes:
                if (
                    t.qualified_name not in seen
                    and t.is_test
                    and _test_is_named_after(t, name)
                ):
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

        elif pattern == "advises":
            for edge in store.get_edges_by_source(qn):
                if edge.kind != "CALLS" or not edge.extra.get("aop_resolved"):
                    continue
                target_node = store.get_node(edge.target_qualified)
                if target_node:
                    add_result(node_to_dict(target_node), edge)
                else:
                    edges_out.append(edge_to_dict(edge))

        elif pattern == "advised_by":
            for edge in store.get_edges_by_target(qn):
                if edge.kind != "CALLS" or not edge.extra.get("aop_resolved"):
                    continue
                advice_node = store.get_node(edge.source_qualified)
                if advice_node:
                    add_result(node_to_dict(advice_node), edge)
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
        if same_name_tests:
            summary += f" — skipped {same_name_tests} same-named test symbol(s)"
        if pattern == "tests_for" and inferred_results:
            # Say it where it is read first: a guessed test link is not a
            # traced one, and an agent should not report it as coverage.
            summary += f"; {inferred_results} of them guesses (see inferred_by)"

        # A zero here is the dangerous direction: agents read it as "none
        # exist" and either conclude wrongly or fall back to grepping the
        # repository. One capped sentence prevents both. The same field tells
        # a reader when part of a non-empty answer is only name matches, unless
        # they filtered by resolution and so already know.
        confidence: str | None = None
        if total_results == 0:
            confidence = empty_query_confidence(store, root, pattern, target, node)
        elif resolution == RESOLUTION_ALL:
            confidence = guessed_calls_confidence(resolution_split)

        if detail_level == "minimal":
            result_fields: tuple[str, ...] = ("name", "kind", "file_path", "indirect")
            if pattern in ("inheritors_of", "tests_for"):
                # Present only on the rows that are guesses, so a traced row
                # pays nothing for it.
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


class _SourceBudget:
    """Hands out source snippets until a shared line budget runs out.

    One budget is shared by the hits and by any neighbours expanded from
    them, so a response cannot grow without bound no matter how many edges a
    hot symbol has. Files are read once and reused.
    """

    def __init__(
        self, root: Path, total: int, per_node: int, followed: set[str] | None = None,
    ) -> None:
        self._root = root
        self._followed = followed or set()
        self._left = total
        self._chars_left = _MAX_SEARCH_SOURCE_CHARS
        self._per_node = per_node
        self._files: dict[str, list[str]] = {}
        self.omitted = 0

    @property
    def exhausted(self) -> bool:
        return self._left <= 0 or self._chars_left <= 0

    def _lines(self, raw_path: str) -> list[str]:
        lines = self._files.get(raw_path)
        if lines is not None:
            return lines
        path = Path(raw_path)
        if not path.is_absolute():
            path = self._root / path
        try:
            # The graph stores paths it parsed from inside the repo, but a
            # stale row must never be able to read outside it. A file behind
            # a followed symlinked folder is read through its in-repo path.
            if not path_within_repo(self._root, path, self._followed):
                raise ValueError("outside the repository")
            text = path.read_text(encoding="utf-8", errors="replace")
            # Notebook nodes are numbered in the parser's joined-up code cells,
            # so slicing the file by those numbers returns the wrong text.
            if path.suffix.lower() == ".ipynb" or (
                text.split("\n", 1)[0] == "# Databricks notebook source"
            ):
                lines = []
            else:
                # Not splitlines(): it also breaks on form feeds and U+2028,
                # which the parser does not count, so later lines would shift.
                lines = text.split("\n")
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            logger.warning(
                "Could not read %s for a source snippet: %s", raw_path, exc,
            )
            lines = []
        self._files[raw_path] = lines
        return lines

    def attach(
        self, target: dict[str, Any], raw_path: Any, start: Any, end: Any,
    ) -> bool:
        """Attach ``source`` to *target*; return whether anything was added."""
        if not raw_path or not start or not end or end < start:
            return False
        if self.exhausted:
            self.omitted += 1
            return False
        lines = self._lines(raw_path)
        if not lines:
            return False
        span = min(end - start + 1, self._per_node, self._left)
        if span <= 0:
            self.omitted += 1
            return False
        snippet = lines[start - 1:start - 1 + span]
        if not snippet:
            return False
        text = "\n".join(snippet)
        char_cap = min(_MAX_SEARCH_SOURCE_CHARS_PER_NODE, self._chars_left)
        cut = len(text) > char_cap
        if cut:
            text = text[:char_cap]
        target["source"] = text
        target["source_lines"] = [start, start + text.count("\n")]
        if cut or end - start + 1 > len(snippet):
            target["source_truncated"] = True
        self._left -= len(snippet)
        self._chars_left -= len(text)
        return True


def _neighbor_entry(node: GraphNode) -> dict[str, Any]:
    return {
        "name": _sanitize_name(node.name),
        "qualified_name": _sanitize_name(node.qualified_name),
        "kind": node.kind,
        "file_path": node.file_path,
        "line_start": node.line_start,
        "line_end": node.line_end,
    }


def _stored_qualified_name(store: GraphStore, hit: dict[str, Any]) -> str | None:
    """Return the name edges are keyed on; a hit's own may be cut for display."""
    qname = hit.get("qualified_name")
    if not qname or len(qname) < _SANITIZED_NAME_MAX:
        return qname
    for node in store.get_nodes_by_file(hit.get("file_path") or ""):
        if (
            node.line_start == hit.get("line_start")
            and _sanitize_name(node.qualified_name) == qname
        ):
            return node.qualified_name
    return qname


def _attach_neighbors(
    results: list[dict[str, Any]],
    store: GraphStore,
    budget: _SourceBudget,
) -> None:
    """Attach each top hit's direct callers and callees, with their source.

    A question about how a symbol works is usually answered by the symbol
    plus what calls it and what it calls, so returning those here is what
    stops the caller opening the same files by hand.
    """
    for hit in results[:_MAX_EXPANDED_HITS]:
        qname = _stored_qualified_name(store, hit)
        if not qname:
            continue
        try:
            incoming = store.get_edges_by_target(qname)
            outgoing = store.get_edges_by_source(qname)
        except Exception as exc:  # noqa: BLE001 - a bad row must not fail search
            logger.warning("Neighbour lookup failed for %s: %s", qname, exc)
            continue

        for key, edges, attr in (
            ("callers", incoming, "source_qualified"),
            ("callees", outgoing, "target_qualified"),
        ):
            seen: set[str] = set()
            entries: list[dict[str, Any]] = []
            for edge in edges:
                if edge.kind != "CALLS":
                    continue
                if len(entries) >= _MAX_NEIGHBORS_PER_HIT:
                    break
                if budget.exhausted:
                    # A hot symbol can have thousands of edges; stop looking
                    # them up once nothing more can be returned.
                    budget.omitted += 1
                    break
                other = getattr(edge, attr, None)
                if not other or other in seen or other == qname:
                    continue
                seen.add(other)
                node = store.get_node(other)
                if node is None:
                    continue
                entry = _neighbor_entry(node)
                if budget.attach(
                    entry, node.file_path, node.line_start, node.line_end,
                ):
                    entries.append(entry)
            if entries:
                hit[key] = entries


def semantic_search_nodes(
    query: str,
    kind: str | None = None,
    limit: int = 20,
    repo_root: str | None = None,
    context_files: list[str] | None = None,
    model: str | None = None,
    provider: str | None = None,
    detail_level: str = "standard",
    include_source: bool = False,
    expand_neighbors: bool = False,
    max_source_lines: int = _MAX_SEARCH_SOURCE_LINES_PER_NODE,
) -> dict[str, Any]:
    """Search for nodes by name, keyword, or semantic similarity.

    Uses hybrid search (FTS5 BM25 + vector embeddings merged via Reciprocal
    Rank Fusion) as the primary search path, with graceful fallback to
    keyword matching.

    Args:
        query: Search string to match against node names and qualified names.
        kind: Optional filter by node kind (File, Class, Function, Type, Test).
        limit: Maximum results to return (default: 20, at most 100, or 5
            with detail_level "minimal").
        repo_root: Repository root path. Auto-detected if omitted.
        context_files: Optional list of file paths. Nodes in these files
            receive a relevance boost.
        detail_level: "standard" (full output) or "minimal" (summary only).
        include_source: Attach the source of each hit, so the caller can
            answer from the response instead of opening the files itself.
            Off by default: it costs tokens, and a caller that only wants
            locations should not pay for them.
        expand_neighbors: With include_source, also return the direct callers
            and callees of the top hits, each with their own source. This is
            what lets a caller answer "how does X work" from one response
            instead of opening those files itself.
        max_source_lines: Per-node snippet cap when include_source is set.
            The response is additionally capped in total.

    Returns:
        Ranked list of matching nodes. ``total`` and ``result_count`` count the
        matches that were ranked, ``results_omitted`` those not shown, and
        ``total_is_lower_bound`` says there may be more than were ranked.
    """
    _validate_positive_int(limit, "limit")
    shown_limit = min(
        limit,
        _MAX_SEARCH_RESULTS_MINIMAL if detail_level == "minimal"
        else _MAX_SEARCH_RESULTS_STANDARD,
    )
    window = shown_limit * _SEARCH_WINDOW_FACTOR

    store, root = _get_store(repo_root)
    try:
        mode_out: list[str] = []
        ranked = hybrid_search(
            store, query, kind=kind, limit=window, context_files=context_files,
            model=model, provider=provider, _out_mode=mode_out, repo_root=root,
        )
        results, total, truncated = _bounded(ranked, shown_limit, shown_limit)
        lower_bound = total >= window
        bounds: dict[str, Any] = {
            "result_count": total,
            "total": total,
            "truncated": truncated,
            "results_omitted": total - len(results),
        }
        if lower_bound:
            bounds["total_is_lower_bound"] = True

        search_mode = mode_out[0] if mode_out else "keyword"

        source_omitted = 0
        if include_source and results and detail_level != "minimal":
            per_node = min(
                max(int(max_source_lines), 1),
                _MAX_SEARCH_SOURCE_LINES_PER_NODE,
            )
            budget = _SourceBudget(
                root,
                _MAX_EXPANDED_SOURCE_LINES if expand_neighbors
                else _MAX_SEARCH_SOURCE_LINES,
                per_node,
                followed_files(store),
            )
            # Expansion was asked for, so lower-ranked hits must not spend the
            # budget before the top hits' callers and callees get any of it.
            top = results[:_MAX_EXPANDED_HITS] if expand_neighbors else results
            for hit in top:
                budget.attach(
                    hit, hit.get("file_path"),
                    hit.get("line_start"), hit.get("line_end"),
                )
            if expand_neighbors:
                _attach_neighbors(results, store, budget)
            for hit in results[len(top):]:
                budget.attach(
                    hit, hit.get("file_path"),
                    hit.get("line_start"), hit.get("line_end"),
                )
            source_omitted = budget.omitted

        total_text = f"{total}+" if lower_bound else str(total)
        summary = f"Found {total_text} node(s) matching '{query}'" + (
            f" (kind={kind})" if kind else ""
        ) + _shown_of(len(results), total)

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
                for r in results
            ]
            minimal_response: dict[str, Any] = {
                "status": "ok",
                "query": query,
                "search_mode": search_mode,
                "summary": summary,
                "results": minimal_results,
                **bounds,
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
            **bounds,
        }
        if source_omitted:
            result["source_omitted"] = source_omitted
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
    detail_level: str = "standard",
) -> dict[str, Any]:
    """Find functions, classes, or files exceeding a line-count threshold.

    Useful for identifying decomposition targets, code-quality audits,
    and enforcing size limits during code review.

    Args:
        min_lines: Minimum line count to flag (default: 50).
        kind: Filter by node kind: Function, Class, File, or Test.
        file_path_pattern: Filter by file path substring (e.g. "components/").
        limit: Maximum results (default: 50, at most 100, or 25 with
            detail_level "minimal").
        repo_root: Repository root path. Auto-detected if omitted.
        detail_level: "standard" (full rows) or "minimal" (fewer, smaller rows).

    Returns:
        Oversized nodes with line counts, ordered largest first. ``total``,
        ``total_found`` and ``result_count`` are the count before the cut.
    """
    _validate_positive_int(limit, "limit")
    minimal = detail_level == "minimal"
    shown_limit = min(
        limit,
        _MAX_LARGE_FUNCTIONS_MINIMAL if minimal else _MAX_LARGE_FUNCTIONS_STANDARD,
    )

    store, root = _get_store(repo_root)
    try:
        total = store.count_nodes_by_size(
            min_lines=min_lines,
            kind=kind,
            file_path_pattern=file_path_pattern,
        )
        nodes = store.get_nodes_by_size(
            min_lines=min_lines,
            kind=kind,
            file_path_pattern=file_path_pattern,
            limit=shown_limit,
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
            f"Found {total} node(s) with >= {min_lines} lines"
            + (f" (kind={kind})" if kind else "")
            + (f" matching '{file_path_pattern}'" if file_path_pattern else "")
            + _shown_of(len(results), total)
            + ":",
        ]
        for r in results[:10]:
            summary_parts.append(
                f"  {r['line_count']:>4} lines | {r['kind']:>8} | "
                f"{r['name']} ({r['relative_path']}:{r['line_start']})"
            )
        if len(results) > 10:
            summary_parts.append(f"  ... and {len(results) - 10} more")

        if minimal:
            results = [
                {
                    key: row[key]
                    for key in ("name", "kind", "relative_path", "line_start", "line_count")
                    if key in row
                }
                for row in results
            ]

        return {
            "status": "ok",
            "summary": "\n".join(summary_parts),
            "total_found": total,
            "total": total,
            "result_count": total,
            "results_omitted": total - len(results),
            "truncated": total > len(results),
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
        token_budget: Approximate token limit for results, at most 10,000. The
            walk stops when it runs out, so a small budget does little work and
            ``truncated`` means there was more.
        repo_root: Repository root path.
    """
    _validate_positive_int(token_budget, "token_budget")
    token_budget = min(token_budget, _MAX_TRAVERSAL_TOKEN_BUDGET)

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

            # DFS can reach a node late by a long path first. Reaching it again
            # by a shorter one must still expand it, or what lies past it is
            # missed, but it is only listed once.
            seen_at = visited.get(current_qn)
            if seen_at is not None and seen_at <= cur_depth:
                continue
            if cur_depth > depth:
                continue

            visited[current_qn] = cur_depth
            node = store.get_node(current_qn)
            if not node:
                continue

            if seen_at is None:
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
            next_depth = cur_depth + 1
            for e in out_edges:
                tgt = e.target_qualified
                if visited.get(tgt, next_depth + 1) > next_depth:
                    queue.append((tgt, next_depth))
            for e in in_edges:
                src = e.source_qualified
                if visited.get(src, next_depth + 1) > next_depth:
                    queue.append((src, next_depth))

        return {
            "start_node": start_qn,
            "mode": mode,
            "max_depth": depth,
            "token_budget": token_budget,
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
