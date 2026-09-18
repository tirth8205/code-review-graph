"""Seeded k-hop neighbourhood extraction and path-between-symbols queries.

The whole-repository force layout is only viable while the rendered graph
stays inside the budget in :mod:`code_review_graph.visualization`
(``DEFAULT_MAX_FULL_NODES`` / ``DEFAULT_MAX_FULL_EDGES``).  Past that the page
aggregates into one bubble per community, which is fine for an architecture
sketch and useless for review.  This module builds the other view: pick a
seed, keep the *k* hops around it, and leave the rest of the graph out of the
payload entirely.

Hop semantics
-------------
A hop is any edge that is not containment: ``CALLS``, ``IMPORTS_FROM``,
``INHERITS``, ``IMPLEMENTS``, ``TESTED_BY``, ``DEPENDS_ON``, ``REFERENCES``,
and whatever a language pack adds next.  The rule is an exclusion rather than
an allow-list on purpose: an allow-list silently drops a whole relationship
class the day a parser starts emitting it, and the neighbourhood then quietly
omits nodes a reviewer needed.  ``CONTAINS`` is structural and is deliberately
*not* a hop — otherwise the file of any seed would be one hop
away and every sibling symbol in that file two hops away, so a depth-2
neighbourhood of one function would swallow its whole module.  Containing
``File`` nodes are still attached to the payload (at the hop of their closest
member) because the renderer's collapse/expand and file clustering are built
on ``CONTAINS``; they simply do not act as transit.

Everything here operates on the already-exported, already-name-resolved data
produced by :func:`code_review_graph.visualization.export_graph_data`, so a
neighbourhood is a strict subset of what the whole-repo view would have drawn.
"""

from __future__ import annotations

import fnmatch
from collections import deque
from dataclasses import dataclass
from typing import Iterable, Sequence


def _sanitize_name(value: str) -> str:
    """Delegate to :func:`code_review_graph.graph._sanitize_name`.

    Imported lazily so that ``cli.py`` can pull this module in at import time
    for its flag defaults without paying for ``graph`` (and therefore
    ``networkx``) on every invocation, including ``--version``.
    """
    from .graph import _sanitize_name as _impl

    return _impl(value)


__all__ = [
    "DEFAULT_DEPTH",
    "DEFAULT_MAX_NODES",
    "DEFAULT_RENDER_DEPTH",
    "PATH_EDGE_KINDS",
    "STRUCTURAL_EDGE_KINDS",
    "NeighbourhoodSpec",
    "SeedResolutionError",
    "extract",
    "hop_distances",
    "shortest_path",
]

# Structural containment: rendered, never traversed for hop counting.
STRUCTURAL_EDGE_KINDS: tuple[str, ...] = ("CONTAINS",)

# Edge kinds the path-between-two-symbols query walks.
PATH_EDGE_KINDS: tuple[str, ...] = ("CALLS", "IMPORTS_FROM", "INHERITS")

DEFAULT_DEPTH = 2
DEFAULT_RENDER_DEPTH = 1
DEFAULT_MAX_NODES = 1500

# A path query needs a whole graph to search, but the *answer* is a handful of
# nodes; refuse pathological inputs rather than walking forever.
_MAX_PATH_VISITS = 2_000_000


class SeedResolutionError(ValueError):
    """Raised when a seed symbol, file or flow matches no node in the graph."""


@dataclass(frozen=True)
class NeighbourhoodSpec:
    """What to seed the view with and how far to grow it.

    Attributes:
        symbols: Symbol queries (qualified name, ``file::name``, or bare name).
        files: File path queries; a file seeds itself and its direct members.
        flow: Execution flow name or id; every node on the flow is a seed.
        path_from: Start symbol of a shortest-path query.
        path_to: End symbol of a shortest-path query.
        depth: Hops to include in the payload.
        render_depth: Hops drawn before the user expands; defaults to
            ``min(DEFAULT_RENDER_DEPTH, depth)``.
        max_nodes: Hard cap on payload nodes.  The outermost hop is trimmed
            first, and the seed set itself is trimmed once the outer hops are
            gone -- the cap is a cap, not a suggestion.
    """

    symbols: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    flow: str | None = None
    path_from: str | None = None
    path_to: str | None = None
    depth: int = DEFAULT_DEPTH
    render_depth: int | None = None
    max_nodes: int = DEFAULT_MAX_NODES

    @property
    def is_active(self) -> bool:
        """True when any seed was supplied, i.e. this is a neighbourhood view."""
        return bool(self.symbols or self.files or self.flow or self.path_from
                    or self.path_to)

    @property
    def effective_render_depth(self) -> int:
        if self.render_depth is None:
            return min(DEFAULT_RENDER_DEPTH, self.depth)
        return max(0, min(self.render_depth, self.depth))

    def validate(self) -> "NeighbourhoodSpec":
        """Reject contradictory flag combinations.  Returns *self*."""
        if (self.path_from is None) != (self.path_to is None):
            raise ValueError(
                "--path-from and --path-to must be given together"
            )
        if self.depth < 0:
            raise ValueError(f"depth must be >= 0, got {self.depth}")
        if self.render_depth is not None and self.render_depth < 0:
            raise ValueError(
                f"render-depth must be >= 0, got {self.render_depth}"
            )
        if self.render_depth is not None and self.render_depth > self.depth:
            # Drawing hop 3 of a payload that stops at hop 2 asks for nodes
            # that were never exported.  Silently clamping it hid the typo.
            raise ValueError(
                f"render-depth {self.render_depth} exceeds depth "
                f"{self.depth}; the payload stops at hop {self.depth}"
            )
        if self.max_nodes < 1:
            raise ValueError(f"max-nodes must be >= 1, got {self.max_nodes}")
        return self

    @classmethod
    def from_args(
        cls,
        *,
        seed_symbols: Sequence[str] | None = None,
        seed_files: Sequence[str] | None = None,
        seed_flow: str | None = None,
        path_from: str | None = None,
        path_to: str | None = None,
        depth: int = DEFAULT_DEPTH,
        render_depth: int | None = None,
        max_nodes: int = DEFAULT_MAX_NODES,
    ) -> "NeighbourhoodSpec":
        return cls(
            symbols=tuple(seed_symbols or ()),
            files=tuple(seed_files or ()),
            flow=seed_flow,
            path_from=path_from,
            path_to=path_to,
            depth=depth,
            render_depth=render_depth,
            max_nodes=max_nodes,
        )


# ---------------------------------------------------------------------------
# Adjacency and traversal
# ---------------------------------------------------------------------------


def build_adjacency(
    edges: Iterable[dict],
    kinds: Sequence[str] | None = None,
    *,
    exclude: Sequence[str] = (),
    directed: bool = False,
) -> dict[str, set[str]]:
    """Adjacency map over *edges*, restricted to *kinds* and minus *exclude*."""
    allowed = set(kinds) if kinds is not None else None
    denied = set(exclude)
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        kind = edge.get("kind")
        if allowed is not None and kind not in allowed:
            continue
        if kind in denied:
            continue
        source = edge.get("source")
        target = edge.get("target")
        if not source or not target:
            continue
        adjacency.setdefault(source, set()).add(target)
        if directed:
            adjacency.setdefault(target, set())
        else:
            adjacency.setdefault(target, set()).add(source)
    return adjacency


def hop_distances(
    adjacency: dict[str, set[str]],
    seeds: Iterable[str],
    depth: int,
) -> dict[str, int]:
    """Breadth-first hop distance from *seeds*, capped at *depth*."""
    hops: dict[str, int] = {seed: 0 for seed in seeds}
    queue: deque[str] = deque(hops)
    while queue:
        current = queue.popleft()
        distance = hops[current]
        if distance >= depth:
            continue
        for neighbour in adjacency.get(current, ()):
            if neighbour not in hops:
                hops[neighbour] = distance + 1
                queue.append(neighbour)
    return hops


def shortest_path(
    edges: Iterable[dict],
    source: str,
    target: str,
    kinds: Sequence[str] = PATH_EDGE_KINDS,
) -> tuple[list[str], bool]:
    """Shortest path from *source* to *target* through *kinds*.

    Tries the directed graph first (``source`` calls/imports/inherits its way
    to ``target``), then falls back to the undirected graph so a pair that is
    only *related* still shows a route.  Returns ``(path, directed)``; the path
    is empty when the two symbols are not connected through *kinds* at all.
    """
    edges = list(edges)
    if source == target:
        return ([source], True)
    directed_adjacency = build_adjacency(edges, kinds, directed=True)
    path = _bfs_path(directed_adjacency, source, target)
    if path:
        return (path, True)
    undirected_adjacency = build_adjacency(edges, kinds, directed=False)
    return (_bfs_path(undirected_adjacency, source, target), False)


def _bfs_path(
    adjacency: dict[str, set[str]], source: str, target: str
) -> list[str]:
    if source not in adjacency or target not in adjacency:
        return []
    previous: dict[str, str | None] = {source: None}
    queue: deque[str] = deque([source])
    visits = 0
    while queue:
        current = queue.popleft()
        visits += 1
        if visits > _MAX_PATH_VISITS:
            return []
        if current == target:
            path = [current]
            while previous[path[-1]] is not None:
                path.append(previous[path[-1]])  # type: ignore[arg-type]
            path.reverse()
            return path
        for neighbour in sorted(adjacency.get(current, ())):
            if neighbour not in previous:
                previous[neighbour] = current
                queue.append(neighbour)
    return []


# ---------------------------------------------------------------------------
# Seed resolution
# ---------------------------------------------------------------------------


def _normalise_path(value: str) -> str:
    cleaned = value.replace("\\", "/").strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.lstrip("/")


def resolve_symbol(nodes: Sequence[dict], query: str) -> list[str]:
    """Resolve a symbol query to qualified names, best match first.

    Accepts a full qualified name, a ``path::name`` suffix, or a bare symbol
    name.  Raises :class:`SeedResolutionError` when nothing matches.
    """
    by_qn = {node["qualified_name"] for node in nodes}
    if query in by_qn:
        return [query]

    normalised = _normalise_path(query)
    if normalised in by_qn:
        return [normalised]

    suffix_matches = sorted(
        qn for qn in by_qn
        if qn.endswith("::" + query) or qn.endswith("/" + normalised)
    )
    if suffix_matches:
        return suffix_matches

    name_matches = sorted(
        node["qualified_name"] for node in nodes if node.get("name") == query
    )
    if name_matches:
        return name_matches

    raise SeedResolutionError(
        f"no node in the graph matches symbol {_sanitize_name(query)!r}; "
        "pass a qualified name such as 'pkg/mod.py::func'"
    )


def resolve_files(
    nodes: Sequence[dict], patterns: Sequence[str]
) -> list[str]:
    """Resolve file patterns to the File nodes *and* their direct members."""
    file_paths = {
        node["file_path"] for node in nodes if node.get("file_path")
    }
    matched: set[str] = set()
    for pattern in patterns:
        normalised = _normalise_path(pattern)
        if not normalised:
            continue
        for file_path in file_paths:
            candidate = _normalise_path(file_path)
            if (
                candidate == normalised
                or candidate.endswith("/" + normalised)
                or fnmatch.fnmatch(candidate, normalised)
            ):
                matched.add(file_path)
    if not matched:
        joined = ", ".join(_sanitize_name(p) for p in patterns[:5])
        raise SeedResolutionError(
            f"no indexed file matches: {joined}"
        )
    seeds = {
        node["qualified_name"]
        for node in nodes
        if node.get("file_path") in matched
    }
    return sorted(seeds)


def resolve_flow(
    flows: Sequence[dict], nodes: Sequence[dict], query: str
) -> list[str]:
    """Resolve a flow name or id to the qualified names on its path."""
    id_to_qn = {
        node["id"]: node["qualified_name"] for node in nodes
        if node.get("id") is not None
    }
    lowered = query.strip().lower()
    chosen: dict | None = None
    for flow in flows:
        if str(flow.get("id")) == query or str(flow.get("name", "")).lower() == lowered:
            chosen = flow
            break
    if chosen is None:
        for flow in flows:
            if lowered and lowered in str(flow.get("name", "")).lower():
                chosen = flow
                break
    if chosen is None:
        raise SeedResolutionError(
            f"no execution flow matches {_sanitize_name(query)!r}; "
            "run 'code-review-graph postprocess' to detect flows"
        )
    seeds = [
        id_to_qn[node_id]
        for node_id in chosen.get("path", [])
        if node_id in id_to_qn
    ]
    if not seeds:
        raise SeedResolutionError(
            f"flow {_sanitize_name(str(chosen.get('name')))!r} has no nodes "
            "present in the exported graph"
        )
    return seeds


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _parent_files(edges: Sequence[dict], members: set[str]) -> dict[str, str]:
    """Map member qualified name -> containing File qualified name."""
    parents: dict[str, str] = {}
    for edge in edges:
        if edge.get("kind") != "CONTAINS":
            continue
        target = edge.get("target")
        if target in members:
            parents.setdefault(target, edge["source"])
    return parents


def _select_within_budget(
    hops: dict[str, int],
    degrees: dict[str, int],
    parents: dict[str, str],
    max_nodes: int,
    pinned: Sequence[str] = (),
) -> tuple[dict[str, int], bool]:
    """Pick the payload node set, never returning more than *max_nodes* nodes.

    A symbol costs its own slot plus, the first time it appears, a slot for
    the ``File`` that contains it, because the renderer needs that File for
    clustering and collapse.  The budget therefore has to be applied to the
    *final* set, not to the semantic hop set alone.  When a symbol fits but
    the pair does not, the symbol is kept and its File is given up: the File
    is a rendering nicety, the symbol is the thing the reviewer asked for.

    *pinned* nodes (the answer to a path query, which is the whole point of
    that query) are placed first, bare, so no other node's containing File
    can crowd the answer out of its own view; the caller refuses a budget
    smaller than the path before it gets here.  Everything else is ranked
    nearest-hop first, then best-connected first by whole-graph degree, ties
    broken by name so the output is deterministic.  Hop 0 is ranked first but
    is **not** exempt: on ``--seed-changed`` the seed set is the large thing,
    so exempting it made ``--max-nodes`` do nothing exactly when it was
    needed.  A seed that does not fit is dropped like any other node, and the
    caller reports how many.
    """
    selected: dict[str, int] = {}
    truncated = False
    for qualified_name in pinned:
        if len(selected) >= max_nodes:
            truncated = True
            break
        if qualified_name in hops and qualified_name not in selected:
            selected[qualified_name] = hops[qualified_name]
    ordered = sorted(
        hops.items(),
        key=lambda item: (item[1], -degrees.get(item[0], 0), item[0]),
    )
    for qualified_name, distance in ordered:
        already = qualified_name in selected
        if already and selected[qualified_name] > distance:
            selected[qualified_name] = distance
        addition = [] if already else [qualified_name]
        parent = parents.get(qualified_name)
        if parent is not None and parent not in selected:
            addition.append(parent)
        if not addition:
            continue
        if len(selected) + len(addition) > max_nodes:
            if len(addition) > 1 and len(selected) + 1 <= max_nodes:
                addition = [qualified_name]
            else:
                truncated = True
                continue
        for member in addition:
            previous = selected.get(member)
            if previous is None or previous > distance:
                selected[member] = distance
    return (selected, truncated)


def extract(data: dict, spec: NeighbourhoodSpec) -> dict:
    """Reduce exported graph *data* to the neighbourhood described by *spec*.

    Returns a payload of the same shape as :func:`export_graph_data` with an
    extra ``"neighbourhood"`` block describing the seeds, the hop label of
    every node, the render depth and (for a path query) the highlighted path.
    Nodes outside the neighbourhood are absent, not dimmed.
    """
    spec.validate()
    nodes: list[dict] = data["nodes"]
    edges: list[dict] = data["edges"]

    seeds: list[str] = []
    seed_kind = "symbol"
    path: list[str] = []
    path_directed = True
    path_error = ""

    if spec.path_from is not None and spec.path_to is not None:
        seed_kind = "path"
        source = resolve_symbol(nodes, spec.path_from)[0]
        target = resolve_symbol(nodes, spec.path_to)[0]
        path, path_directed = shortest_path(edges, source, target)
        if path:
            seeds.extend(path)
        else:
            path_error = (
                f"no path from {_sanitize_name(source)} to "
                f"{_sanitize_name(target)} through "
                f"{'/'.join(PATH_EDGE_KINDS)}"
            )
            seeds.extend([source, target])

    if spec.symbols:
        if seed_kind == "path":
            seed_kind = "mixed"
        for query in spec.symbols:
            seeds.extend(resolve_symbol(nodes, query))
    if spec.files:
        seed_kind = "file" if seed_kind == "symbol" else "mixed"
        seeds.extend(resolve_files(nodes, spec.files))
    if spec.flow:
        seed_kind = "flow" if seed_kind == "symbol" else "mixed"
        seeds.extend(resolve_flow(data.get("flows", []), nodes, spec.flow))

    # Preserve first-seen order while de-duplicating.
    seeds = list(dict.fromkeys(seeds))

    # A path query whose own answer does not fit the budget cannot be
    # satisfied at all: say so rather than shipping half a path.
    if len(path) > spec.max_nodes:
        raise ValueError(
            f"max-nodes {spec.max_nodes} is below the {len(path)} nodes on "
            f"the path from {_sanitize_name(path[0])} to "
            f"{_sanitize_name(path[-1])}; raise it to at least {len(path)}"
        )

    adjacency = build_adjacency(edges, exclude=STRUCTURAL_EDGE_KINDS)
    hops = hop_distances(adjacency, seeds, spec.depth)

    degrees: dict[str, int] = {}
    for edge in edges:
        degrees[edge["source"]] = degrees.get(edge["source"], 0) + 1
        degrees[edge["target"]] = degrees.get(edge["target"], 0) + 1

    # Containing files are attached so collapse/expand and clustering still
    # work.  They are rendered, not traversed, so they carry the hop of their
    # closest member.
    parents = _parent_files(edges, set(hops))
    hops, truncated = _select_within_budget(
        hops, degrees, parents, spec.max_nodes, pinned=path
    )

    selected = set(hops)
    # The cap can eat into the seed set itself. Report that rather than let a
    # "neighbourhood of the changed files" quietly cover a third of them.
    seeds_requested = len(seeds)
    seeds = [seed for seed in seeds if seed in selected]
    seeds_dropped = seeds_requested - len(seeds)
    # One record per qualified name. The exporter de-duplicates on the raw
    # name but emits the _sanitize_name-truncated one, so two nodes whose
    # paths differ only past 256 characters reach here as a duplicate key --
    # which would then be double-counted against max_nodes and bound twice by
    # D3. Keep the first.
    out_nodes = []
    emitted: set[str] = set()
    for node in nodes:
        qualified_name = node["qualified_name"]
        if qualified_name in selected and qualified_name not in emitted:
            emitted.add(qualified_name)
            out_nodes.append(node)
    out_edges = [
        e for e in edges
        if e["source"] in selected and e["target"] in selected
    ]

    kept_ids = {n["id"] for n in out_nodes if n.get("id") is not None}
    out_flows = [
        f for f in data.get("flows", [])
        if any(nid in kept_ids for nid in f.get("path", []))
    ]
    out_communities = []
    for community in data.get("communities", []):
        members = [m for m in community.get("members", []) if m in selected]
        if members:
            trimmed = dict(community)
            trimmed["members"] = members
            out_communities.append(trimmed)

    frontier = sorted(qn for qn, d in hops.items() if d >= spec.depth)

    return {
        "nodes": out_nodes,
        "edges": out_edges,
        "stats": data.get("stats", {}),
        "flows": out_flows,
        "communities": out_communities,
        "mode": "neighbourhood",
        "neighbourhood": {
            "seeds": seeds,
            "seeds_requested": seeds_requested,
            "seeds_dropped": seeds_dropped,
            "seed_kind": seed_kind,
            "seed_query": [
                _sanitize_name(q)
                for q in (*spec.symbols, *spec.files)
            ] or ([_sanitize_name(spec.flow)] if spec.flow else []),
            "depth": spec.depth,
            "render_depth": spec.effective_render_depth,
            "hops": hops,
            "frontier": frontier,
            "path": path,
            "path_directed": path_directed,
            "path_edge_kinds": list(PATH_EDGE_KINDS),
            "path_error": path_error,
            "structural_edge_kinds": list(STRUCTURAL_EDGE_KINDS),
            "truncated": truncated,
            "max_nodes": spec.max_nodes,
            "total_nodes": len(nodes),
            "total_edges": len(edges),
        },
    }
