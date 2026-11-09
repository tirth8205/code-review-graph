"""SQLite-backed knowledge graph storage and query engine.

Stores code structure as nodes (File, Class, Function, Type, Test, YamlPath, DocSection,
JsonPath) and
edges (CALLS, IMPORTS_FROM, INHERITS, IMPLEMENTS, CONTAINS, TESTED_BY, DEPENDS_ON, REFERENCES).
Supports impact-radius queries and subgraph extraction.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    import networkx as nx

    from .parser import EdgeInfo, NodeInfo

from .constants import (
    BFS_ENGINE,
    CALLER_TEST_ROUTE_DEPTH,
    CALLER_TEST_ROUTE_MAX_CALLERS,
    CLAIMED_ENDPOINT_KEY,
    IMPACT_DEFAULT_EDGE_DIRECTION,
    IMPACT_DEFAULT_EDGE_WEIGHT,
    IMPACT_DEPTH_DECAY,
    IMPACT_DIRECTION_INCOMING,
    IMPACT_DIRECTION_NONE,
    IMPACT_DIRECTION_OUTGOING,
    IMPACT_EDGE_DIRECTIONS,
    IMPACT_EDGE_WEIGHTS,
    IMPACT_SCORE_FLOOR,
    IMPORT_SCOPE_KEY,
    IMPORT_SCOPE_PACKAGE,
    IMPORT_SCOPE_TREE,
    INHERITED_ENDPOINT_KEY,
    INHERITED_THROUGH_KEY,
    MAX_IMPACT_DEPTH,
    MAX_IMPACT_NODES,
    env_int,
    normalize_file_path,
    read_only_requested,
    release_os_memory,
)
from .errors import GraphStoreError, is_lock_error
from .inheritance import InheritanceIndex
from .migrations import (
    LATEST_VERSION,
    TARGET_RESOLUTION_EXPR,
    TARGET_RESOLUTION_KINDS,
    get_schema_version,
    run_migrations,
    target_resolution_expr,
)

logger = logging.getLogger(__name__)

# Maximum rows bound into a single executemany() call for batched writes.
# Chunking keeps peak memory bounded, while the surrounding transaction and
# single commit mean SQLite performs one WAL commit/checkpoint per batch job
# instead of one per row (issue #721).
_UPDATE_BATCH = 50_000

# These are the canonical language values stored for the JavaScript ecosystem.
# JSX files are stored as ``javascript`` and Astro files as ``typescript`` by
# ``EXTENSION_TO_LANGUAGE``; TSX keeps its own grammar name. Nodes from an HTML
# page's inline scripts are stored as ``html``, and call into script files.
_JAVASCRIPT_LANGUAGE_FAMILY = ("javascript", "typescript", "tsx", "html")
_JAVASCRIPT_LANGUAGE_FAMILY_SET = frozenset(_JAVASCRIPT_LANGUAGE_FAMILY)
_COLLISION_SUFFIX_RE = re.compile(
    r"^[0-9a-f]{12}(?::(?:[2-9]|[1-9][0-9]+))?$",
)
# The same suffix shape, anchored at the end of a full qualified name, so one
# match recovers the base identity a collision row was disambiguated from.
_COLLISION_IDENTITY_RE = re.compile(
    r"^(?P<base>.+):S[0-9a-f]{12}(?::(?:[2-9]|[1-9][0-9]+))?$",
)

# A call to a name two same-file definitions share, with nothing at the call
# to choose one, is not bound to the first of them (#585, #586). It is stored
# the way an unbound target is: the plain name, every definition listed as a
# candidate, an AMBIGUOUS tier. The identity the parser wrote stays under this
# key, so the edge binds again once only one definition is left.
_COLLISION_ENDPOINT_KEY = "collision_endpoint"
_COLLISION_AMBIGUOUS_TIER = "AMBIGUOUS"
_COLLISION_AMBIGUOUS_SCORE = 0.4
_COLLISION_CALL_KINDS = ("CALLS", "REFERENCES")
_COLLISION_CANDIDATE_LIMIT = 20
_AMBIGUITY_KEYS = (
    "ambiguous_targets", "ambiguous_target_count", "ambiguous_targets_truncated",
    "unresolved_targets", "unresolved_target_count", "unresolved_targets_truncated",
)


def _load_collision_extra(raw: Optional[str]) -> dict:
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _plain_collision_name(qualified: str) -> str:
    """The name the source wrote: ``a.py::Shapes.area`` -> ``area``."""
    symbol = qualified.partition("::")[2] or qualified
    return re.split(r"::|\.", symbol)[-1]


def _ambiguous_collision_extra(extra: dict, base: str, candidates: list[str]) -> dict:
    """*extra* for an edge that could mean any of *candidates*."""
    # The unsuffixed definition first, then a stable order, so a re-store and
    # a settle pass write the same list.
    candidates = sorted(candidates, key=lambda candidate: (candidate != base, candidate))
    marked = {key: value for key, value in extra.items() if key not in _AMBIGUITY_KEYS}
    marked.update({
        "ambiguous_targets": candidates[:_COLLISION_CANDIDATE_LIMIT],
        "ambiguous_target_count": len(candidates),
        "ambiguous_targets_truncated": len(candidates) > _COLLISION_CANDIDATE_LIMIT,
        _COLLISION_ENDPOINT_KEY: base,
        "confidence": _COLLISION_AMBIGUOUS_SCORE,
        "confidence_tier": _COLLISION_AMBIGUOUS_TIER,
    })
    return marked


def _bound_collision_extra(extra: dict) -> dict:
    """*extra* once its collision has gone and one definition is left."""
    bound = {
        key: value for key, value in extra.items()
        if key not in _AMBIGUITY_KEYS and key != _COLLISION_ENDPOINT_KEY
    }
    if bound.get("confidence_tier") == _COLLISION_AMBIGUOUS_TIER:
        bound.pop("confidence_tier")
        if bound.get("confidence") == _COLLISION_AMBIGUOUS_SCORE:
            bound.pop("confidence")
    return bound


# Bound on the order-preserving pairing below, in DP steps:
# (shorter list) x (size difference + 1) squared. Past it, pairs are taken in
# order from the start.
_ORDERED_PAIRS_MAX_STEPS = 1_000_000


def _ordered_pairs(new_lines: list[int], old_lines: list[int]) -> list[tuple[int, int]]:
    """Pair two sorted line lists in order, as an edit would have moved them.

    Returns ``(new_position, old_position)`` pairs covering the shorter list,
    never crossing two pairs. Equal lengths pair one-to-one in order. When
    definitions were added or removed, the pairing whose line offsets change
    least is chosen: an insertion shifts everything after it by one amount,
    so ``[10, 20]`` becoming ``[3, 16, 26]`` pairs 16 and 26, not 3 and 16.
    """
    if len(new_lines) == len(old_lines):
        return [(i, i) for i in range(len(new_lines))]
    new_is_longer = len(new_lines) > len(old_lines)
    longer, shorter = (new_lines, old_lines) if new_is_longer else (old_lines, new_lines)
    if not shorter:
        return []
    skips = len(longer) - len(shorter)
    sign = 1 if new_is_longer else -1

    def offset(i: int, j: int) -> int:
        return (longer[i] - shorter[j]) * sign

    pairs: list[tuple[int, int]] = []
    if len(shorter) * (skips + 1) ** 2 > _ORDERED_PAIRS_MAX_STEPS:
        pairs = [(j, j) for j in range(len(shorter))]
    else:
        # cost[k]: shorter[j] paired with longer[j + k]; the cost is the first
        # offset plus every change of offset between consecutive pairs.
        cost = [abs(offset(k, 0)) for k in range(skips + 1)]
        back: list[list[int]] = [[-1] * (skips + 1)]
        for j in range(1, len(shorter)):
            step = [0] * (skips + 1)
            choice = [0] * (skips + 1)
            for k in range(skips + 1):
                here = offset(j + k, j)
                best_cost, best_k = -1, 0
                for previous in range(k + 1):
                    candidate = cost[previous] + abs(
                        here - offset(j - 1 + previous, j - 1),
                    )
                    if best_cost < 0 or candidate < best_cost:
                        best_cost, best_k = candidate, previous
                step[k] = best_cost
                choice[k] = best_k
            back.append(choice)
            cost = step
        k = min(range(skips + 1), key=lambda item: (cost[item], item))
        for j in range(len(shorter) - 1, -1, -1):
            pairs.append((j + k, j))
            k = back[j][k]
        pairs.reverse()
    if new_is_longer:
        return pairs
    return [(short, long_) for long_, short in pairs]


def _compatible_edge_languages(language: str) -> tuple[str, ...]:
    """Return languages that can safely share unresolved bare edge targets."""
    normalized = language.casefold()
    if normalized in _JAVASCRIPT_LANGUAGE_FAMILY_SET:
        return _JAVASCRIPT_LANGUAGE_FAMILY
    return (language,)


def _symbol_of(qualified_name: str) -> str:
    """Return the symbol portion of a qualified name (after the first "::").

    ``src/app.py::Handler.process`` → ``Handler.process``. A qualified name
    without a path component is already a bare symbol.
    """
    _, sep, symbol = qualified_name.partition("::")
    return symbol if sep else qualified_name


# Longest docstring summary mirrored into the searchable ``nodes.docstring``
# column. The parser already caps what it stores in ``extra``; this bound is
# re-applied because existing databases may hold anything.
MAX_INDEXED_DOCSTRING_CHARS = 400
# Bound on the derived ``nodes.name_tokens`` column so a pathological
# identifier cannot grow the FTS index without limit.
MAX_INDEXED_TOKEN_CHARS = 200

_ALNUM_RUN_RE = re.compile(r"[0-9A-Za-z]+")
# Split at lower/digit -> upper ("hashPassword") and at the boundary before
# the final capitalised word of an acronym run ("HTTPServer" -> HTTP Server).
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _index_tokens(*values: str | None) -> str:
    """Return the camelCase/PascalCase word splits of *values*, space joined.

    Only sub-words that the FTS5 ``unicode61`` tokenizer cannot produce on
    its own are emitted. unicode61 already breaks on every non-alphanumeric
    character, so ``get_users`` and ``graph/impact.py`` contribute nothing,
    while ``hashPassword`` contributes ``hash Password``, the tokens that
    make ``password`` match it. Keeping the column minimal matters: repeating
    tokens that are already indexed would inflate their term frequency and
    skew BM25 ranking.
    """
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        for run in _ALNUM_RUN_RE.findall(value):
            parts = _CAMEL_BOUNDARY_RE.split(run)
            if len(parts) < 2:
                continue
            for part in parts:
                lowered = part.lower()
                if lowered and lowered not in seen:
                    seen.add(lowered)
                    out.append(part)
    return " ".join(out)[:MAX_INDEXED_TOKEN_CHARS]


def identifier_words(text: str) -> set[str]:
    """Return the lowercase words *text* is built from.

    Both the whole alphanumeric run and its camelCase parts are returned, so
    ``GraphStore`` yields ``{graphstore, graph, store}``. Callers compare
    these sets to decide whether a symbol is *named after* a query, which is
    the signal BM25 cannot express once long file paths dominate a row's
    token count.
    """
    words: set[str] = set()
    for run in _ALNUM_RUN_RE.findall(text or ""):
        words.add(run.lower())
        for part in _CAMEL_BOUNDARY_RE.split(run):
            if part:
                words.add(part.lower())
    return words


def node_index_tokens(
    kind: str,
    name: str,
    parent_name: str | None,
    file_path: str | None,
) -> str:
    """Return the value for a node's ``name_tokens`` column.

    A File node's ``name`` *is* its path, so splitting it would fold every
    directory segment of the checkout into the index (an absolute path under
    ``.../CloudDocs/`` would add "Cloud Docs" to every file in the graph).
    File nodes contribute their stem only.
    """
    stem = PurePosixPath(file_path or "").stem
    return _index_tokens("" if kind == "File" else name, parent_name, stem)


def _indexed_docstring(extra: dict[str, Any] | None) -> str | None:
    """Return the whitespace-normalized docstring summary held in *extra*.

    Existing databases may carry any JSON value under ``docstring``, so
    non-strings are ignored rather than coerced.
    """
    if not extra:
        return None
    raw = extra.get("docstring")
    if not isinstance(raw, str):
        return None
    summary = " ".join(raw.split())[:MAX_INDEXED_DOCSTRING_CHARS]
    return summary or None


def qualified_name_of(node: NodeInfo) -> str:
    """The qualified name the store gives a parsed node.

    Exposed so code that parses a file without storing it (the old side of a
    diff, for one) names its symbols exactly as the graph does.
    """
    if node.kind == "File":
        return node.file_path
    identity_name = node.identity_name or node.name
    if node.parent_name:
        return f"{node.file_path}::{node.parent_name}.{identity_name}"
    return f"{node.file_path}::{identity_name}"


def _bridge_qualified_name(qualified_name: str) -> str:
    """Return *qualified_name* with its file-path component POSIX-normalized.

    Qualified names embed the file path before the first ``::`` (File nodes
    are just the path). Stored identities always use forward slashes (#774),
    so a Windows-native spelling must be bridged to find the POSIX-keyed row.
    Only the path component is rewritten — the symbol part may legitimately
    contain backslashes (PHP fully-qualified names).
    """
    path_part, sep, symbol_part = qualified_name.partition("::")
    return normalize_file_path(path_part) + sep + symbol_part


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,          -- File, Class, Function, Type, Test
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL UNIQUE,
    file_path TEXT NOT NULL,
    line_start INTEGER,
    line_end INTEGER,
    language TEXT,
    parent_name TEXT,
    params TEXT,
    return_type TEXT,
    modifiers TEXT,
    is_test INTEGER DEFAULT 0,
    file_hash TEXT,
    extra TEXT DEFAULT '{}',
    -- Symbol portion of qualified_name (everything after the first "::").
    -- Stored so a dotted-tail lookup is an indexed equality test instead of a
    -- substr() scan over every node; see search_nodes_by_qualified_tail.
    -- idx_nodes_symbol is created by migration v10, not here: _init_schema runs
    -- before run_migrations, so indexing a column this CREATE TABLE cannot add
    -- to an existing table would break every pre-existing database on open.
    symbol TEXT,
    -- Documentation summary lifted out of extra['docstring'] so the FTS5
    -- external-content index can reach it (FTS columns must be real columns
    -- of the content table). See migration v11.
    docstring TEXT,
    -- camelCase/PascalCase word splits for name, parent_name and the file
    -- stem. unicode61 splits on punctuation but keeps hashPassword as one
    -- token, so "password" could never match it. Added by migration v11.
    name_tokens TEXT,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,           -- CALLS, IMPORTS_FROM, INHERITS, REFERENCES, etc.
    source_qualified TEXT NOT NULL,
    target_qualified TEXT NOT NULL,
    file_path TEXT NOT NULL,
    line INTEGER DEFAULT 0,
    extra TEXT DEFAULT '{}',
    confidence REAL DEFAULT 1.0,
    confidence_tier TEXT DEFAULT 'EXTRACTED',
    -- 'direct' when target_qualified names an indexed node, 'unresolved' when
    -- it is a bare name the read path can only match by name. NULL for edge
    -- kinds where the distinction is meaningless (an IMPORTS_FROM target is a
    -- file path, not a call target) and for rows written since the last
    -- refresh_target_resolution() pass. Same rule as nodes.symbol above:
    -- idx_edges_kind_target_resolution is created by migration v11, not here.
    target_resolution TEXT,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_path);
CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(kind);
CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_edges_kind ON edges(kind);
CREATE INDEX IF NOT EXISTS idx_edges_target_kind ON edges(target_qualified, kind);
CREATE INDEX IF NOT EXISTS idx_edges_source_kind ON edges(source_qualified, kind);
CREATE INDEX IF NOT EXISTS idx_edges_file ON edges(file_path);
"""


#: Accepted values for the ``resolution`` filter shared by the query tools
#: and the impact traversal. "all" keeps today's behaviour; "direct" keeps
#: only attributions the graph bound to an indexed node; "unresolved" keeps
#: only the bare-name guesses (query patterns only).
RESOLUTION_ALL = "all"
RESOLUTION_DIRECT = "direct"
RESOLUTION_UNRESOLVED = "unresolved"
QUERY_RESOLUTIONS = (RESOLUTION_ALL, RESOLUTION_DIRECT, RESOLUTION_UNRESOLVED)
IMPACT_RESOLUTIONS = (RESOLUTION_ALL, RESOLUTION_DIRECT)


def _impact_resolution_guard(resolution: str) -> str:
    """Return the SQL predicate for one validated impact resolution mode."""
    if resolution == RESOLUTION_ALL:
        return ""
    if resolution != RESOLUTION_DIRECT:
        raise ValueError(
            f"resolution must be one of {list(IMPACT_RESOLUTIONS)}, "
            f"got {resolution!r}"
        )
    return (
        " AND (e.kind NOT IN ('CALLS', 'REFERENCES') "
        f"OR COALESCE(e.target_resolution, {target_resolution_expr('e')})"
        " = 'direct')"
    )


@dataclass
class GraphNode:
    id: int
    kind: str
    name: str
    qualified_name: str
    file_path: str
    line_start: int
    line_end: int
    language: str
    parent_name: Optional[str]
    params: Optional[str]
    return_type: Optional[str]
    is_test: bool
    file_hash: Optional[str]
    extra: dict


@dataclass
class GraphEdge:
    id: int
    kind: str
    source_qualified: str
    target_qualified: str
    file_path: str
    line: int
    extra: dict
    confidence: float = 1.0
    confidence_tier: str = "EXTRACTED"
    #: 'direct', 'unresolved', or None where the distinction does not apply
    #: (a non-call edge) or has not been computed yet. See migration v11.
    target_resolution: Optional[str] = None


@dataclass
class FlowAdjacency:
    """In-memory adjacency structure for flow tracing.

    Loaded once via :meth:`GraphStore.load_flow_adjacency` and passed to
    ``trace_flows`` / ``compute_criticality`` to avoid per-edge SQLite
    point queries on large graphs.
    """
    calls_out: dict[str, list[str]]
    has_tested_by: set[str]
    nodes_by_qn: dict[str, "GraphNode"]
    nodes_by_id: dict[int, "GraphNode"]


@dataclass
class GraphStats:
    total_nodes: int
    total_edges: int
    nodes_by_kind: dict[str, int]
    edges_by_kind: dict[str, int]
    languages: list[str]
    files_count: int
    last_updated: Optional[str]


#: How far above a file an ancestor directory may still be the target of a
#: tree-scoped import. A ``require_all`` names a subsystem, not a whole
#: repository, and the walk has to stop somewhere that is not the filesystem
#: root.
IMPORT_SCOPE_MAX_ANCESTORS = 12


def _edge_import_scope(extra: Optional[str]) -> Optional[str]:
    """The ``import_scope`` recorded on one raw ``edges.extra`` value."""
    if not extra or IMPORT_SCOPE_KEY not in extra:
        return None
    try:
        payload = json.loads(extra)
    except (TypeError, ValueError):
        return None
    scope = payload.get(IMPORT_SCOPE_KEY) if isinstance(payload, dict) else None
    return scope if isinstance(scope, str) else None


def _edge_extra_is_unresolved(extra: Optional[str]) -> bool:
    """True when an edge's ``extra`` marks its endpoint as unresolved.

    An edge carrying ``ambiguous_targets`` or ``unresolved_targets`` names a
    set of candidates rather than one node, and one carrying a claimed endpoint
    was written for a symbol the graph does not have, so no traversal may treat
    it as a fact about a specific symbol.
    """
    try:
        payload = json.loads(extra or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and (
        "ambiguous_targets" in payload
        or "unresolved_targets" in payload
        or CLAIMED_ENDPOINT_KEY in payload
    )


#: What an edge's ``confidence_tier`` says about how its endpoint was bound, and
#: the score stored with it. A resolver that knows better records its own score
#: in ``extra`` (AOP 0.7, Spring events 0.95); these are the defaults.
TIER_EXTRACTED = "EXTRACTED"  # The source names a symbol, and the node exists.
TIER_INFERRED = "INFERRED"  # A resolver chose the node from other evidence.
TIER_AMBIGUOUS = "AMBIGUOUS"  # Several nodes fit and nothing chooses between them.
TIER_UNRESOLVED = "UNRESOLVED"  # No node: often external, sometimes just unknown.
_SCORE_EXTRACTED = 1.0
_SCORE_INFERRED = 0.8
_SCORE_AMBIGUOUS = 0.4
_SCORE_UNRESOLVED = 0.2

#: Keys a resolver leaves on an edge it bound. Each means the source did not
#: name this node outright.
_RESOLVER_BOUND_KEYS = (
    "bare_call_target",
    "bare_tested_by_source",
    "cpp_scoped_target",
    INHERITED_ENDPOINT_KEY,
    "rescript_open_module",
    "scoped_resolved",
    "spring_resolved",
    "temporal_resolved",
)

#: Receivers that are the calling object itself, so the call means a method of
#: the caller's class or one it inherits.
_SELF_RECEIVERS = frozenset({"this", "$this", "self"})
#: Receivers that start the lookup at the caller's parent.
_SUPER_RECEIVERS = frozenset({"super", "base"})
#: Languages whose parser records a receiver's declared type, so a receiver
#: with none is truly unknown, and ``this`` is a keyword. PHP and JavaScript
#: are left out: the parser reads no parameter types there, so ``$other`` in
#: ``equals(Uuid $other)`` looks untyped while it is the caller's own class.
_TYPED_RECEIVER_LANGUAGES = frozenset({
    "csharp", "java", "kotlin", "tsx", "typescript",
})

#: The edges whose endpoint is meant to name a node: (kind, column holding that
#: endpoint, key ``_resolve_bare_endpoints`` manages it through).
_NODE_ENDPOINTS = (
    ("CALLS", "target_qualified", "bare_call_target"),
    ("REFERENCES", "target_qualified", "bare_call_target"),
    ("TESTED_BY", "source_qualified", "bare_tested_by_source"),
)

_SYMBOL_SEPARATOR_RE = re.compile(r"::|\.")


def _is_path_qualified(name: str) -> bool:
    """Is *name* ``<absolute file path>::<symbol>``, the form a node's name takes?

    ``Redis::get``, ``crate::a::b`` and ``App\\Mail::send`` also hold ``::``, but
    they are what the source wrote, and they are what the scoped resolvers read.
    """
    head = name.split("::", 1)[0]
    return PurePosixPath(head).is_absolute() or PureWindowsPath(head).is_absolute()


def _plain_symbol_name(qualified: str) -> str:
    """The last name in a path-qualified symbol: ``a.py::Helper.ping`` -> ``ping``."""
    _, _, symbol = qualified.partition("::")
    return _SYMBOL_SEPARATOR_RE.split(symbol)[-1]


def _claimed_owner(edge_extra: dict) -> Optional[str]:
    """The class a settled edge's claimed endpoint put the method on, if any.

    ``a.py::Helper.ping`` gives ``Helper``; ``a.py::ping`` gives None.
    """
    claimed = edge_extra.get(CLAIMED_ENDPOINT_KEY)
    if not isinstance(claimed, str):
        return None
    owner, dot, _ = claimed.rsplit("::", 1)[-1].rpartition(".")
    return owner if dot and owner else None


def _same_owner(parent_name: Optional[str], owner: str) -> bool:
    """Can a method whose class is *parent_name* be the one *owner* named?

    Scopes are compared from the innermost name out, as far as both go.
    ``Alpha.Inner`` is not ``Beta.Inner``, but a side that knew only ``Inner``
    is no evidence against ``Outer.Inner``.
    """
    have = (parent_name or "").split(".")
    want = owner.split(".")
    depth = min(len(have), len(want))
    return have[-depth:] == want[-depth:]


def _load_edge_extra(extra: Optional[str]) -> dict:
    """Parse one ``edges.extra`` value, treating anything unusable as empty."""
    try:
        payload = json.loads(extra or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _certainty_tier_sql(unbound: str) -> str:
    """SQL for the tier an edge has earned, given the test that it is unbound."""
    resolver_bound = " OR ".join(
        f"extra LIKE '%\"{key}\"%'" for key in _RESOLVER_BOUND_KEYS
    )
    return (
        "CASE "
        f"WHEN {unbound} AND extra LIKE '%\"ambiguous_targets\"%' "
        f"THEN '{TIER_AMBIGUOUS}' "
        f"WHEN {unbound} THEN '{TIER_UNRESOLVED}' "
        f"WHEN {resolver_bound} THEN '{TIER_INFERRED}' "
        f"WHEN confidence_tier IN ('{TIER_AMBIGUOUS}', '{TIER_UNRESOLVED}') "
        f"THEN '{TIER_EXTRACTED}' "
        "ELSE confidence_tier END"
    )


#: SQL for the score that goes with a tier already stored on the row.
_CERTAINTY_SCORE_SQL = (
    "CASE confidence_tier "
    f"WHEN '{TIER_UNRESOLVED}' THEN {_SCORE_UNRESOLVED} "
    f"WHEN '{TIER_AMBIGUOUS}' THEN {_SCORE_AMBIGUOUS} "
    "ELSE COALESCE("
    "CASE WHEN extra LIKE '%\"confidence\"%' AND json_valid(extra) "
    "THEN json_extract(extra, '$.confidence') END, "
    f"CASE confidence_tier WHEN '{TIER_INFERRED}' THEN {_SCORE_INFERRED} "
    f"ELSE {_SCORE_EXTRACTED} END"
    ") END"
)


def _parent_dir(path: Optional[str]) -> Optional[str]:
    """The directory holding *path*, as a directory-scoped edge spells it.

    Registered as the SQLite function ``crg_parent_dir``. Returns ``None``
    for anything that is not an absolute POSIX-ish path, so a qualified name
    that is not a file path can never join a directory target by accident.
    """
    if not isinstance(path, str) or "/" not in path:
        return None
    head = path.rsplit("/", 1)[0]
    return head or None


def import_scope_ancestors(file_path: str) -> list[tuple[str, tuple[str, ...]]]:
    """Directory targets that could name *file_path*, nearest first.

    Each entry pairs a directory with the import scopes for which that
    directory legitimately stands for the file. The file's own directory
    qualifies under both scopes; anything higher only under
    :data:`IMPORT_SCOPE_TREE`, because a Go package is exactly one directory
    deep and a subdirectory of it is a different package.
    """
    parent = _parent_dir(file_path)
    if parent is None:
        return []
    out: list[tuple[str, tuple[str, ...]]] = [
        (parent, (IMPORT_SCOPE_PACKAGE, IMPORT_SCOPE_TREE)),
    ]
    current = parent
    for _ in range(IMPORT_SCOPE_MAX_ANCESTORS):
        current = _parent_dir(current) or ""
        if not current:
            break
        out.append((current, (IMPORT_SCOPE_TREE,)))
    return out


#: Fills ``_impact_frontier_dirs`` with the package directory of every File
#: node on the frontier, once per hop. CROSS JOIN drives it from the frontier,
#: which is small, rather than from every File node in the graph.
IMPACT_FRONTIER_DIRS_SQL = """
INSERT INTO _impact_frontier_dirs (dir, score)
SELECT dir, MAX(score) FROM (
    SELECT crg_parent_dir(n.file_path) AS dir, f.score AS score
    FROM _impact_frontier f
    CROSS JOIN nodes n
      ON n.qualified_name = f.node_qn AND n.kind = 'File'
)
WHERE dir IS NOT NULL
GROUP BY dir
"""


#: AOP-derived CALLS edges (advice -> target, tagged ``extra.aop_resolved``)
#: do not follow the plain CALLS impact policy: an advice is not "called by"
#: the method it wraps, so target -> source propagation would answer "the
#: target changed, is the aspect impacted" and miss the relationship that
#: matters -- "the advice changed, what does it impact". These edges always
#: propagate source -> target regardless of CALLS' registered direction.
#: See aop_resolver.py's module docstring and PR #916 review.
_IMPACT_AOP_EXTRA_LIKE = '%"aop_resolved"%'
_IMPACT_AOP_DIRECTION_CASE = (
    "CASE WHEN e.extra LIKE ? THEN ? ELSE COALESCE(p.direction, ?) END"
)


def _impact_candidate_sql(resolution_guard: str) -> str:
    """One hop of the impact relaxation.

    *resolution_guard* is either the empty string or a fixed predicate chosen
    by a validated enum; no caller value reaches the SQL text.

    The first two branches carry the AOP direction override, so each binds
    the ``extra`` LIKE pattern and the forced direction ahead of the policy
    default. See ``_IMPACT_AOP_DIRECTION_CASE``.

    The third branch is the directory-scoped one. A Go import names a
    package, so its edge targets the file's DIRECTORY rather than the file,
    and expanding it here -- at every hop, without spending one -- is what
    one edge per import buys: the alternative is one edge per file in the
    package, which on kubernetes is 73,507 edges for a single directory.

    ``CROSS JOIN`` and ``INDEXED BY`` pin that branch to one index seek per
    frontier package. Left to itself SQLite drove it from the covering index
    on ``kind`` alone and rescanned every IMPORTS_FROM row once per
    directory: 18 seconds of a 19-second kubernetes traversal, against 1.5
    with the plan pinned. ``tests/test_import_scope.py`` asserts the plan.
    """
    return f"""
    INSERT INTO _impact_next (node_qn, score)
    SELECT node_qn, MAX(score)
    FROM (
        SELECT e.target_qualified AS node_qn,
               f.score * COALESCE(p.weight, ?) * ? AS score
        FROM _impact_frontier f
        JOIN edges e ON e.source_qualified = f.node_qn
        LEFT JOIN _impact_policies p ON p.kind = e.kind
        WHERE {_IMPACT_AOP_DIRECTION_CASE} = ?{resolution_guard}
        UNION ALL
        SELECT e.source_qualified AS node_qn,
               f.score * COALESCE(p.weight, ?) * ? AS score
        FROM _impact_frontier f
        JOIN edges e ON e.target_qualified = f.node_qn
        LEFT JOIN _impact_policies p ON p.kind = e.kind
        WHERE {_IMPACT_AOP_DIRECTION_CASE} = ?{resolution_guard}
        UNION ALL
        SELECT e.source_qualified AS node_qn,
               d.score * COALESCE(p.weight, ?) * ? AS score
        FROM _impact_frontier_dirs d
        CROSS JOIN edges e INDEXED BY idx_edges_target_kind
          ON e.target_qualified = d.dir AND e.kind = 'IMPORTS_FROM'
        LEFT JOIN _impact_policies p ON p.kind = e.kind
        WHERE COALESCE(p.direction, ?) = ?
    ) candidates
    WHERE score > ?
    GROUP BY node_qn
    """  # noqa: S608


# ---------------------------------------------------------------------------
# GraphStore
# ---------------------------------------------------------------------------


_RECOVERY_HINT = (
    "Delete it and run `code-review-graph build` to rebuild the graph."
)


def _describe_open_failure(db_path: Path, exc: Exception) -> str:
    """Turn a raw SQLite/OS error into one actionable line.

    ``sqlite3`` reports "file is not a database" for a corrupt or foreign
    file and "attempt to write a readonly database" / "unable to open
    database file" for a directory the process may not write to. Those two
    need opposite fixes, so they get opposite messages instead of one
    generic apology.
    """
    detail = str(exc).strip() or exc.__class__.__name__
    lowered = detail.lower()
    if isinstance(exc, OSError) or "readonly" in lowered or "unable to open" in lowered:
        return (
            f"cannot open the graph database for writing at {db_path} ({detail}). "
            f"Check the permissions on {db_path.parent}, or set CRG_DATA_DIR to a "
            "writable directory."
        )
    return (
        f"the graph database at {db_path} is unreadable ({detail}). "
        f"{_RECOVERY_HINT}"
    )


def _assert_usable_database(conn: sqlite3.Connection, db_path: Path) -> None:
    """Refuse a SQLite file this build cannot honestly answer from.

    Two cases that both used to be accepted in silence:

    * a perfectly valid SQLite file belonging to something else. ``CREATE
      TABLE IF NOT EXISTS`` would graft our schema onto it and every query
      would then answer "nothing found" about a graph that was never there.
    * a file written by a newer release. ``run_migrations`` is a no-op once
      the stored version is at or above ``LATEST_VERSION``, so the mismatch
      only surfaced much later, as a missing column in an unrelated query.
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
    ).fetchall()
    tables = {row[0] for row in rows if not str(row[0]).startswith("sqlite_")}
    if tables and not tables & {"nodes", "metadata"}:
        sample = ", ".join(sorted(tables)[:3])
        raise GraphStoreError(
            f"{db_path} is a SQLite database but not a code-review-graph graph "
            f"(it holds {sample}). Point --data-dir somewhere else, or delete "
            "the file and run `code-review-graph build`."
        )

    version = get_schema_version(conn)
    if version > LATEST_VERSION:
        raise GraphStoreError(
            f"the graph database at {db_path} was written by a newer "
            f"code-review-graph (schema v{version}; this build understands "
            f"v{LATEST_VERSION}). Upgrade code-review-graph, or delete the file "
            "and run `code-review-graph build`."
        )


class CorruptGraphDatabaseError(GraphStoreError):
    """The graph database file exists but cannot be opened as this graph.

    A named subclass of :class:`GraphStoreError`, rather than a bare one, so
    a caller that is about to rebuild the graph from scratch anyway
    (``build``) can discard the unusable file and carry on, and every other
    caller reports the single actionable line every ``GraphStoreError``
    carries instead of a traceback. A restored CI cache is the
    common source: an unusable ``graph.db`` turns every later run red until
    someone clears the cache by hand.

    Deliberately not a ``sqlite3.Error``. Nothing this package can explain
    about itself may reach a caller as a raw SQLite exception; what the
    subclass records is the one shape of unusable that a rebuild fixes by
    itself. A foreign SQLite file and a database written by a newer release
    are excluded for the same reason they are refused up front in
    :func:`_assert_usable_database`: deleting somebody else's data, or a
    graph this build is merely too old to read, is not a recovery.

    Two shapes of unusable reach here, and both are unrecoverable in place:
    a file SQLite cannot read at all (truncated, half-written, not a
    database), and a perfectly valid SQLite file whose tables are the wrong
    shape, which ``CREATE TABLE IF NOT EXISTS`` will not repair.
    """

    def __init__(self, db_path: str | Path, reason: object) -> None:
        self.db_path = Path(db_path)
        self.reason = str(reason)
        super().__init__(
            f"the graph database at {self.db_path} is unreadable "
            f"({self.reason}). Run `code-review-graph build` to rebuild it "
            "from scratch."
        )


_SQLITE_HEADER = b"SQLite format 3\x00"

_CORRUPTION_MESSAGES = (
    "file is not a database",
    "database disk image is malformed",
    "file is encrypted",
    "malformed database schema",
)

#: SQLite's wording when a statement names a table or column that is not
#: shaped the way this schema expects. A file can be perfectly readable and
#: still answer this way: ``_init_schema`` uses ``CREATE TABLE IF NOT
#: EXISTS``, so a ``nodes`` table left behind by something else is never
#: replaced, and the first index or migration that mentions a column it does
#: not have fails.
#:
#: Deliberately narrow, because everything listed here ends in the file being
#: deleted. "database is locked", "attempt to write a readonly database" and
#: "disk I/O error" are environment problems and must never cost anyone their
#: database. "already exists" and "duplicate column name" are excluded for the
#: same reason: every migration guards its DDL with a check first, so those
#: two mean two processes migrated the same healthy database at once, not that
#: the database is wrong.
_INCOMPATIBLE_SCHEMA_MESSAGES = (
    "no such column",
    "no such table",
    "has no column named",
)


def _is_unreadable_database(db_path: Path, exc: sqlite3.DatabaseError) -> bool:
    """Distinguish a corrupt database file from an environment problem.

    A missing directory or a permission error also raises
    ``sqlite3.DatabaseError``; deleting the file would be wrong there, so
    only SQLite's own corruption messages, or a file that does not carry the
    SQLite header, count as corruption.
    """
    message = str(exc).lower()
    if any(known in message for known in _CORRUPTION_MESSAGES):
        return True
    try:
        with open(db_path, "rb") as handle:
            header = handle.read(len(_SQLITE_HEADER))
    except OSError:
        return False
    return bool(header) and not header.startswith(_SQLITE_HEADER)


def _has_incompatible_schema(db_path: Path, exc: sqlite3.DatabaseError) -> bool:
    """True when *db_path* is readable SQLite holding the wrong tables.

    Both halves are required. The message has to name a schema object, which
    rules out locks, read-only filesystems and I/O errors; and the file has
    to still answer a read of ``sqlite_master``, which rules out reporting a
    genuinely broken file as merely mis-shaped.
    """
    message = str(exc).lower()
    if not any(known in message for known in _INCOMPATIBLE_SCHEMA_MESSAGES):
        return False
    if not db_path.is_file():
        # Never let the probe below be the thing that creates the file.
        return False
    try:
        probe = sqlite3.connect(str(db_path), timeout=5)
    except sqlite3.DatabaseError:
        return False
    try:
        probe.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
    except sqlite3.DatabaseError:
        return False
    finally:
        probe.close()
    return True


def discard_corrupt_database(db_path: str | Path) -> bool:
    """Delete an unusable graph database and its WAL sidecars.

    Returns True when at least one file was removed. Only ever call this
    where the graph is about to be rebuilt from scratch: the data is gone.
    """
    path = Path(db_path)
    removed = False
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = path.with_name(path.name + suffix) if suffix else path
        try:
            candidate.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("Could not remove %s: %s", candidate, exc)
            continue
        removed = True
    return removed


class GraphStore:
    """SQLite-backed code knowledge graph."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        read_only: bool | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.read_only = read_only_requested() if read_only is None else read_only
        conn: sqlite3.Connection | None = None
        try:
            if self.read_only:
                if not self.db_path.is_file():
                    raise GraphStoreError(
                        f"No graph found at {self.db_path}. "
                        "Run `code-review-graph build` first."
                    )
                conn = sqlite3.connect(
                    self._read_only_uri(),
                    uri=True,
                    timeout=30,
                    check_same_thread=False,
                    isolation_level=None,
                )
            else:
                # Inside the try on purpose: an unwritable data directory
                # raises here, and it is one of the failures this constructor
                # explains.
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(
                    str(self.db_path), timeout=30, check_same_thread=False,
                    isolation_level=None,  # Disable implicit transactions (#135)
                )
            conn.row_factory = sqlite3.Row
            if not self.read_only:
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA cache_size=-2000")
            # FILE keeps the impact-radius working tables reclaimable; the
            # compile-time default is memory on some SQLite builds.
            conn.execute("PRAGMA temp_store=FILE")
            _assert_usable_database(conn, self.db_path)
            if self.read_only and conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'nodes'"
            ).fetchone() is None:
                raise GraphStoreError(
                    f"No graph found at {self.db_path}. "
                    "Run `code-review-graph build` first."
                )
            if self.read_only and get_schema_version(conn) < LATEST_VERSION:
                # Migrations write, so queries would hit tables and columns
                # this old graph does not have yet.
                raise GraphStoreError(
                    f"The graph at {self.db_path} is schema "
                    f"v{get_schema_version(conn)}, this build needs "
                    f"v{LATEST_VERSION}, and a read-only open cannot upgrade it. "
                    "Run `code-review-graph update` first."
                )
            self._conn = conn
            if not self.read_only:
                self._init_schema()
                # Ensure schema_version is set, then run pending migrations
                if get_schema_version(self._conn) < 1:
                    # Fresh DB — metadata table just created by _init_schema
                    self._conn.execute(
                        "INSERT OR IGNORE INTO metadata (key, value) "
                        "VALUES ('schema_version', '1')"
                    )
                    self._conn.commit()
                run_migrations(self._conn)
        except (sqlite3.Error, OSError, GraphStoreError) as exc:
            # A corrupt file, a foreign SQLite file, or a data directory this
            # process cannot write to used to escape as a raw traceback from
            # every single command. Report the cause and the recovery instead.
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:  # pragma: no cover - dead handle
                    logger.debug("Could not close %s after a failed open",
                                 self.db_path)
            if isinstance(exc, GraphStoreError):
                # Already classified, and already carries its own line:
                # a foreign SQLite file, a newer schema, or (from a nested
                # open) an unusable one.
                raise
            if is_lock_error(exc):
                # Contention, not damage. Another process holds the write
                # lock (migrations retry it, so reaching here means it held
                # on past that); the graph is intact and the answer is to try
                # again in a moment. Describing it as an unreadable database
                # would tell the user to delete a healthy graph, would hide it
                # from the contention report in ``cli.main``, and would send
                # ``build`` to discard a graph that was never broken.
                raise
            if isinstance(exc, sqlite3.DatabaseError) and (
                _is_unreadable_database(self.db_path, exc)
                or _has_incompatible_schema(self.db_path, exc)
            ):
                # Unusable *and* unrecoverable in place, which is the one
                # shape a rebuild can fix by itself. Both classifiers are
                # deliberately narrow: everything they decline keeps the
                # generic message below and nobody's database is deleted.
                raise CorruptGraphDatabaseError(self.db_path, exc) from exc
            raise GraphStoreError(
                _describe_open_failure(self.db_path, exc)
            ) from exc
        # Directory-scoped IMPORTS_FROM targets (a Go package, a Ruby
        # ``require_all`` tree) are expanded on the read path, so the impact
        # traversal needs a file's own directory as a join key. Deterministic
        # so SQLite evaluates it once per row and seeks idx_edges_target_kind
        # rather than scanning. See IMPORT_SCOPE_KEY in constants.py.
        #
        # Registered outside the open/migrate guard above: a failure here is
        # not a corrupt file, and the guard must keep deleting databases only
        # for the two shapes it recognises.
        try:
            self._conn.create_function(
                "crg_parent_dir", 1, _parent_dir, deterministic=True,
            )
        except (sqlite3.NotSupportedError, TypeError):  # pragma: no cover
            self._conn.create_function("crg_parent_dir", 1, _parent_dir)
        self._nxg_cache: nx.DiGraph | None = None
        self._bare_endpoint_context_cache: tuple[
            dict[str, list[tuple[str, str]]],
            dict[str, str | None],
            dict[str, set[str]],
            dict[str, set[str]],
            set[str],
            dict[str, str],
            dict[str, dict[str, str]],
        ] | None = None
        self._cache_lock = threading.Lock()
        self._cache_epoch = 0
        # Identifies the read transaction currently open on this connection,
        # so a reader cannot roll back a transaction a writer took from it.
        self._active_read_txn: object | None = None

    def _read_only_uri(self) -> str:
        """Open SQLite without creating or updating any database sidecar."""
        sidecars = tuple(
            self.db_path.with_name(self.db_path.name + suffix)
            for suffix in ("-wal", "-shm")
        )
        # A checkpointed WAL database has no sidecars. ``immutable`` lets
        # SQLite read it without creating ``-shm`` on a read-only filesystem.
        query = "mode=ro" if any(path.exists() for path in sidecars) else (
            "mode=ro&immutable=1"
        )
        return f"{self.db_path.resolve().as_uri()}?{query}"

    def __enter__(self) -> "GraphStore":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()

    def _invalidate_cache(self) -> None:
        """Invalidate the cached NetworkX graph after write operations."""
        with self._cache_lock:
            self._cache_epoch += 1
            self._nxg_cache = None
            self._bare_endpoint_context_cache = None

    def shrink_memory(self, clear_nx_cache: bool = True) -> None:
        """Release cached in-memory structures and shrink SQLite page cache."""
        if clear_nx_cache:
            self._invalidate_cache()
        try:
            self._conn.execute("PRAGMA shrink_memory")
        except sqlite3.Error:
            pass
        release_os_memory()

    def close(self) -> None:
        # No gc.collect() here: every MCP tool call opens and closes a store,
        # and a full collection costs ~10 ms or more per call. Builds release
        # memory once, through shrink_memory(), when they finish.
        self._invalidate_cache()
        try:
            self._conn.execute("PRAGMA shrink_memory")
        except sqlite3.Error:
            pass
        self._conn.close()

    # --- Write operations ---

    def upsert_node(self, node: NodeInfo, file_hash: str = "") -> int:
        """Insert or update a node. Returns the node ID."""
        now = time.time()
        qualified = self._qualified_for_upsert(node)
        extra = json.dumps(node.extra) if node.extra else "{}"

        self._conn.execute(
            """INSERT INTO nodes
               (kind, name, qualified_name, file_path, line_start, line_end,
                language, parent_name, params, return_type, modifiers, is_test,
                file_hash, extra, symbol, docstring, name_tokens, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(qualified_name) DO UPDATE SET
                 kind=excluded.kind, name=excluded.name,
                 file_path=excluded.file_path,
                 line_start=excluded.line_start,
                 line_end=excluded.line_end,
                 language=excluded.language,
                 parent_name=excluded.parent_name, params=excluded.params,
                 return_type=excluded.return_type, modifiers=excluded.modifiers,
                 is_test=excluded.is_test, file_hash=excluded.file_hash,
                 extra=excluded.extra, symbol=excluded.symbol,
                 docstring=excluded.docstring, name_tokens=excluded.name_tokens,
                 updated_at=excluded.updated_at
            """,
            (
                node.kind, node.name, qualified, node.file_path,
                node.line_start, node.line_end, node.language,
                node.parent_name, node.params, node.return_type,
                node.modifiers, int(node.is_test), file_hash,
                extra, _symbol_of(self._make_qualified(node)), _indexed_docstring(node.extra),
                node_index_tokens(node.kind, node.name, node.parent_name,
                                  node.file_path),
                now,
            ),
        )
        row = self._conn.execute(
            "SELECT id FROM nodes WHERE qualified_name = ?", (qualified,)
        ).fetchone()
        self._invalidate_cache()
        return row["id"]

    def _qualified_for_upsert(self, node: NodeInfo) -> str:
        """Return a stable identity, suffixing genuine same-file collisions.

        A row at the same line, or else the semantically identical row nearest
        to it, is this definition and is updated in place, so a moved line
        never adds a second row. Only a definition that differs from every
        stored one sharing its base becomes a suffixed collision.
        """
        base = self._make_qualified(node)
        rows = self._conn.execute(
            "SELECT id, qualified_name, line_start, kind, name, parent_name, "
            "language, params, return_type, modifiers, is_test FROM nodes "
            "WHERE file_path = ? AND name = ? AND parent_name IS ? ORDER BY id",
            (node.file_path, node.name, node.parent_name),
        ).fetchall()

        candidates = [
            row for row in rows
            if row["qualified_name"] == base
            or self._is_collision_identity(base, row["qualified_name"])
        ]
        for row in candidates:
            if row["line_start"] == node.line_start:
                return row["qualified_name"]
        signature = self._semantic_signature(node)
        same = [row for row in candidates if self._row_semantic_signature(row) == signature]
        if same:
            nearest = min(
                same, key=lambda row: (abs(row["line_start"] - node.line_start), row["id"]),
            )
            return str(nearest["qualified_name"])

        if self.get_node(base) is None:
            return base

        semantic = self._semantic_disambiguator(node)
        qualified_base = f"{base}:S{semantic}"
        qualified = qualified_base
        ordinal = 2
        while self.get_node(qualified) is not None:
            qualified = f"{qualified_base}:{ordinal}"
            ordinal += 1
        return qualified

    @staticmethod
    def _semantic_disambiguator(node: NodeInfo) -> str:
        """Hash definition semantics that survive harmless source line shifts."""
        payload = json.dumps(
            GraphStore._semantic_signature(node),
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _semantic_signature(node: NodeInfo) -> tuple[Any, ...]:
        """Return definition fields that identify a node across line shifts."""
        return (
            node.kind,
            node.name,
            node.parent_name,
            node.language,
            node.params,
            node.return_type,
            node.modifiers,
            node.is_test,
        )

    @staticmethod
    def _row_semantic_signature(row: sqlite3.Row) -> tuple[Any, ...]:
        """Return the semantic signature stored for an existing node row."""
        return (
            row["kind"],
            row["name"],
            row["parent_name"],
            row["language"] or "",
            row["params"],
            row["return_type"],
            row["modifiers"],
            bool(row["is_test"]),
        )

    @staticmethod
    def _is_collision_identity(base: str, qualified_name: str) -> bool:
        """Return whether *qualified_name* has this store's exact suffix shape.

        Checking the full generated suffix avoids treating colon-bearing symbol
        names, such as Objective-C selectors, as collision identities.
        """
        prefix = f"{base}:S"
        if not qualified_name.startswith(prefix):
            return False
        return _COLLISION_SUFFIX_RE.fullmatch(qualified_name[len(prefix):]) is not None

    @staticmethod
    def _collision_base(qualified_name: str) -> str:
        """Return the base identity of a collision row, or the name itself."""
        match = _COLLISION_IDENTITY_RE.fullmatch(qualified_name)
        return match.group("base") if match else qualified_name

    def _plan_file_identities(
        self, file_path: str, nodes: list[NodeInfo],
    ) -> list[str]:
        """Reconcile a replacement file's nodes with their previous identities.

        Semantic matches keep their qualified name across collision-set reorder.
        When definitions are semantically identical, the closest source location
        preserves identity across harmless shifts, growth, and shrink transitions.
        """
        # Rows are stored under the POSIX spelling; a Windows caller passes
        # backslashes, which would find no previous identities at all.
        file_path = normalize_file_path(file_path)
        old_rows = self._conn.execute(
            "SELECT id, kind, name, qualified_name, file_path, line_start, "
            "line_end, language, parent_name, params, return_type, modifiers, "
            "is_test FROM nodes WHERE file_path = ? ORDER BY id",
            (file_path,),
        ).fetchall()
        new_by_base: dict[str, list[int]] = {}
        for index, node in enumerate(nodes):
            new_by_base.setdefault(self._make_qualified(node), []).append(index)
        # One pass over the old rows: rescanning them for every new base made
        # replacing a file quadratic in its definition count.
        old_by_base: dict[str, list[sqlite3.Row]] = {}
        for row in old_rows:
            old_by_base.setdefault(
                self._collision_base(row["qualified_name"]), [],
            ).append(row)

        planned: list[str | None] = [None] * len(nodes)
        reserved = {row["qualified_name"] for row in old_rows}
        assigned: set[str] = set()
        for base, indices in new_by_base.items():
            candidates = old_by_base.get(base, [])
            used_old_ids: set[int] = set()
            # Semantically identical definitions are told apart only by their
            # order. Nearest-line pairing swaps twins once enough lines are
            # inserted above them, so pair each signature group in order.
            new_by_signature: dict[tuple[Any, ...], list[int]] = {}
            for index in indices:
                if planned[index] is None:
                    new_by_signature.setdefault(
                        self._semantic_signature(nodes[index]), [],
                    ).append(index)
            old_by_signature: dict[tuple[Any, ...], list[sqlite3.Row]] = {}
            for row in candidates:
                if row["id"] not in used_old_ids:
                    old_by_signature.setdefault(
                        self._row_semantic_signature(row), [],
                    ).append(row)
            for signature, group in new_by_signature.items():
                old_group = old_by_signature.get(signature)
                if not old_group:
                    continue
                group.sort(key=lambda i: (nodes[i].line_start, i))
                old_group.sort(key=lambda r: (r["line_start"], r["id"]))
                for new_pos, old_pos in _ordered_pairs(
                    [nodes[i].line_start for i in group],
                    [r["line_start"] for r in old_group],
                ):
                    row = old_group[old_pos]
                    planned[group[new_pos]] = row["qualified_name"]
                    assigned.add(row["qualified_name"])
                    used_old_ids.add(row["id"])

            # Definitions edited in place match nothing semantically. When as
            # many old as new are left (a lone definition whose signature and
            # line both changed, or one edited beside an unchanged twin), pair
            # them in order instead of suffixing and stranding their callers.
            left_new = sorted(
                (index for index in indices if planned[index] is None),
                key=lambda index: (nodes[index].line_start, index),
            )
            left_old = sorted(
                (row for row in candidates if row["id"] not in used_old_ids),
                key=lambda row: (row["line_start"], row["id"]),
            )
            if left_new and len(left_new) == len(left_old):
                for index, row in zip(left_new, left_old):
                    planned[index] = row["qualified_name"]
                    assigned.add(row["qualified_name"])
                    used_old_ids.add(row["id"])

            for index in indices:
                if planned[index] is not None:
                    continue
                exact_location = [
                    row
                    for row in candidates
                    if row["id"] not in used_old_ids
                    and row["line_start"] == nodes[index].line_start
                ]
                if len(exact_location) == 1:
                    row = exact_location[0]
                    planned[index] = row["qualified_name"]
                    assigned.add(row["qualified_name"])
                    used_old_ids.add(row["id"])

            for index in indices:
                if planned[index] is not None:
                    continue
                if base not in reserved and base not in assigned:
                    qualified = base
                else:
                    semantic = self._semantic_disambiguator(nodes[index])
                    qualified_base = f"{base}:S{semantic}"
                    qualified = qualified_base
                    ordinal = 2
                    while qualified in reserved or qualified in assigned:
                        qualified = f"{qualified_base}:{ordinal}"
                        ordinal += 1
                planned[index] = qualified
                assigned.add(qualified)

        return [qualified for qualified in planned if qualified is not None]

    def upsert_edge(self, edge: EdgeInfo) -> int:
        """Insert or update an edge."""
        now = time.time()
        edge = self._resolve_edge_identities(edge)
        extra_dict = edge.extra if edge.extra else {}
        confidence = float(extra_dict.get("confidence", 1.0))
        confidence_tier = str(extra_dict.get("confidence_tier", "EXTRACTED"))
        extra = json.dumps(extra_dict)

        # A parser can supply a byte offset when distinct scopes/calls share a
        # line. Without it, two C# namespace bodies can overwrite a using edge.
        existing = self._conn.execute(
            """SELECT id FROM edges
               WHERE kind=? AND source_qualified=? AND target_qualified=?
                     AND file_path=? AND line=?
                     AND json_extract(extra, '$.source_offset') IS ?""",
            (edge.kind, edge.source, edge.target, edge.file_path, edge.line,
             extra_dict.get("source_offset")),
        ).fetchone()

        if existing:
            self._conn.execute(
                "UPDATE edges SET line=?, extra=?, confidence=?, confidence_tier=?,"
                " updated_at=? WHERE id=?",
                (edge.line, extra, confidence, confidence_tier, now, existing["id"]),
            )
            self._invalidate_cache()
            return existing["id"]

        self._conn.execute(
            """INSERT INTO edges
               (kind, source_qualified, target_qualified, file_path, line, extra,
                confidence, confidence_tier, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (edge.kind, edge.source, edge.target, edge.file_path, edge.line, extra,
             confidence, confidence_tier, now),
        )
        edge_id = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        self._invalidate_cache()
        return edge_id

    def _resolve_stored_identity(self, qualified_name: str) -> str:
        """Resolve a bare edge endpoint to one lone historical suffix."""
        candidates = self._stored_identity_candidates(qualified_name)
        return candidates[0] if len(candidates) == 1 else qualified_name

    def _stored_identity_candidates(self, qualified_name: str) -> list[str]:
        """Every stored definition a base edge endpoint can mean.

        Only a qualified (``path::symbol``) name can be a collision base. The
        index range seek is tried first because it is almost always empty;
        the exact lookup runs only when a suffixed row exists. A name with no
        collision rows is returned alone, whether or not it has a node.
        """
        if "::" not in qualified_name:
            return [qualified_name]
        lower = f"{qualified_name}:S"
        upper = f"{qualified_name}:T"
        matches = [
            row["qualified_name"]
            for row in self._conn.execute(
                "SELECT qualified_name FROM nodes "
                "WHERE qualified_name >= ? AND qualified_name < ? "
                "ORDER BY qualified_name",
                (lower, upper),
            )
            if self._is_collision_identity(qualified_name, row["qualified_name"])
        ]
        if not matches:
            return [qualified_name]
        if self._conn.execute(
            "SELECT 1 FROM nodes WHERE qualified_name = ?", (qualified_name,),
        ).fetchone() is not None:
            return [qualified_name, *matches]
        return matches

    def _resolve_edge_identities(
        self,
        edge: EdgeInfo,
        local_prefix: str | None = None,
        cache: dict[str, list[str]] | None = None,
    ) -> EdgeInfo:
        """Settle both edge endpoints against the stored collision identities.

        Endpoints under *local_prefix* (``"<file>::"``) belong to the file
        being stored, whose identities the caller has already aligned.
        """
        # The parser loads on first use (#1139), so the module global is unset.
        from .parser import EdgeInfo

        # The endpoint that names what was called: an unevidenced one on a
        # name several definitions share lists them all (#585).
        called = (
            1 if edge.kind in _COLLISION_CALL_KINDS
            else 0 if edge.kind == "TESTED_BY" else None
        )
        resolved: list[str] = []
        extra = edge.extra
        for index, endpoint in enumerate((edge.source, edge.target)):
            if local_prefix is not None and endpoint.startswith(local_prefix):
                resolved.append(endpoint)
                continue
            if cache is None:
                candidates = self._stored_identity_candidates(endpoint)
            else:
                if endpoint not in cache:
                    cache[endpoint] = self._stored_identity_candidates(endpoint)
                candidates = cache[endpoint]
            if len(candidates) == 1:
                resolved.append(candidates[0])
            elif index == called:
                resolved.append(_plain_collision_name(endpoint))
                extra = _ambiguous_collision_extra(extra or {}, endpoint, candidates)
            else:
                resolved.append(endpoint)
        source, target = resolved
        if source == edge.source and target == edge.target:
            return edge
        return EdgeInfo(
            kind=edge.kind, source=source, target=target,
            file_path=edge.file_path, line=edge.line, extra=extra,
        )

    def _settle_collision_endpoints(self) -> int:
        """Keep every edge into a duplicated name honest after a store pass.

        A file is stored on its own, so a call from another file can be written
        before, or after, the duplicates it names: bound to the first
        definition, or left on a collision that has since gone. Settled here:
        an unevidenced ``CALLS``/``REFERENCES`` target, or ``TESTED_BY`` source,
        on a name several definitions share lists them all instead (#585); one
        whose duplicates are gone binds to the definition left. A recursive
        call is evidence and keeps its binding.

        Returns the number of edges changed.
        """
        conn = self._conn
        identities: dict[str, list[str]] = {}
        for (qualified,) in conn.execute(
            "SELECT qualified_name FROM nodes WHERE qualified_name GLOB '*:S[0-9a-f]*'"
        ):
            match = _COLLISION_IDENTITY_RE.fullmatch(qualified)
            if match:
                identities.setdefault(match.group("base"), []).append(qualified)

        known: dict[str, list[str]] = {}

        def current(base: str) -> list[str]:
            if base not in known:
                found = sorted(identities.get(base, []))
                exists = conn.execute(
                    "SELECT 1 FROM nodes WHERE qualified_name = ?", (base,),
                ).fetchone()
                known[base] = [base, *found] if exists else found
            return known[base]

        updates: list[tuple[str, str, str, float, str, str | None, int]] = []

        def settle(row: sqlite3.Row, column: str, extra: dict, base: str) -> None:
            candidates = current(base)
            endpoint = row[column]
            if len(candidates) > 1:
                new_endpoint = _plain_collision_name(base)
                new_extra = _ambiguous_collision_extra(extra, base, candidates)
                score, tier, resolution = (
                    _COLLISION_AMBIGUOUS_SCORE, _COLLISION_AMBIGUOUS_TIER, "unresolved",
                )
            elif len(candidates) == 1:
                new_endpoint = candidates[0]
                new_extra = _bound_collision_extra(extra)
                score, tier, resolution = 1.0, "EXTRACTED", "direct"
            else:
                # Every definition went; what is left is an ordinary bare name.
                new_endpoint = _plain_collision_name(base)
                new_extra = _bound_collision_extra(extra)
                score, tier, resolution = 1.0, "EXTRACTED", "unresolved"
            if new_endpoint == endpoint and new_extra == extra:
                return
            updates.append((
                column, new_endpoint, json.dumps(new_extra), score, tier,
                resolution if row["kind"] in _COLLISION_CALL_KINDS else None,
                row["id"],
            ))

        endpoints = (
            *((kind, "target_qualified", "source_qualified") for kind in _COLLISION_CALL_KINDS),
            ("TESTED_BY", "source_qualified", "target_qualified"),
        )
        handled: set[int] = set()
        for row in conn.execute(
            "SELECT id, kind, source_qualified, target_qualified, extra FROM edges "
            "WHERE extra LIKE ?",
            (f'%"{_COLLISION_ENDPOINT_KEY}"%',),
        ).fetchall():
            extra = _load_collision_extra(row["extra"])
            base = extra.get(_COLLISION_ENDPOINT_KEY)
            column = next((c for k, c, _ in endpoints if k == row["kind"]), None)
            if isinstance(base, str) and column is not None:
                handled.add(row["id"])
                settle(row, column, extra, base)
        for base in identities:
            for kind, column, other in endpoints:
                # Only fixed column names reach the SQL text.
                for row in conn.execute(
                    "SELECT id, kind, source_qualified, target_qualified, extra "  # nosec B608
                    f"FROM edges WHERE kind = ? AND {column} = ? AND {other} != ?",
                    (kind, base, base),
                ).fetchall():
                    if row["id"] not in handled:
                        settle(row, column, _load_collision_extra(row["extra"]), base)

        if not updates:
            return 0
        self._begin_immediate()
        try:
            for column in ("source_qualified", "target_qualified"):
                rows = [update[1:] for update in updates if update[0] == column]
                conn.executemany(
                    f"UPDATE edges SET {column} = ?, extra = ?, confidence = ?, "  # nosec B608
                    "confidence_tier = ?, target_resolution = COALESCE(?, target_resolution) "
                    "WHERE id = ?",
                    rows,
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        self._invalidate_cache()
        return len(updates)

    def remove_file_data(self, file_path: str) -> None:
        """Remove all nodes and edges associated with a file."""
        file_path = normalize_file_path(file_path)
        self._conn.execute("DELETE FROM nodes WHERE file_path = ?", (file_path,))
        self._conn.execute("DELETE FROM edges WHERE file_path = ?", (file_path,))
        self._invalidate_cache()

    def remove_file_permanently(self, file_path: str) -> int:
        """Remove one deleted file and every graph reference to its nodes."""
        return self.remove_files_permanently([file_path])

    def remove_files_permanently(
        self, file_paths: list[str], *, stored_paths: bool = False,
    ) -> int:
        """Atomically remove deleted files and graph references to their nodes.

        Reconciliation passes exact inventory spellings with ``stored_paths``.
        Normalising legacy rows would miss them and could delete a different,
        current row that already uses the canonical spelling (#911).
        """
        if not stored_paths:
            file_paths = [normalize_file_path(p) for p in file_paths]
        changed = 0
        has_embeddings = self._conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'embeddings'",
        ).fetchone()
        self._begin_immediate()
        try:
            for file_path in dict.fromkeys(file_paths):
                exists = self._conn.execute(
                    "SELECT EXISTS(SELECT 1 FROM nodes WHERE file_path = ?) OR "
                    "EXISTS(SELECT 1 FROM edges WHERE file_path = ?)",
                    (file_path, file_path),
                ).fetchone()[0]
                if not exists:
                    continue
                changed += 1
                if has_embeddings is not None:
                    self._conn.execute(
                        "DELETE FROM embeddings WHERE qualified_name IN "
                        "(SELECT qualified_name FROM nodes WHERE file_path = ?)",
                        (file_path,),
                    )
                self._conn.execute(
                    "DELETE FROM edges WHERE file_path = ? OR source_qualified IN "
                    "(SELECT qualified_name FROM nodes WHERE file_path = ?) OR "
                    "target_qualified IN "
                    "(SELECT qualified_name FROM nodes WHERE file_path = ?)",
                    (file_path, file_path, file_path),
                )
                self._conn.execute("DELETE FROM nodes WHERE file_path = ?", (file_path,))
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        self._invalidate_cache()
        return changed

    @contextmanager
    def _read_transaction(self) -> Iterator[None]:
        """Hold one snapshot for the duration of a multi-statement read.

        The connection runs with ``isolation_level=None``, so without this
        every statement gets its own snapshot and a writer committing between
        two of them yields numbers that contradict each other — ``get_stats``
        could report a ``total_nodes`` taken before a watcher's commit and a
        per-kind breakdown taken after it.

        ``BEGIN DEFERRED`` takes no lock and, under WAL, never blocks the
        writer: it only pins the snapshot this connection reads from. Nested
        use is a no-op so an enclosing write transaction keeps its own scope,
        and a failure to begin degrades to today's per-statement reads rather
        than failing the query.
        """
        if self._conn.in_transaction:
            yield
            return
        try:
            self._conn.execute("BEGIN DEFERRED")
        except sqlite3.OperationalError as exc:  # pragma: no cover - defensive
            logger.debug("Could not open a read transaction: %s", exc)
            yield
            return
        token = object()
        self._active_read_txn = token
        try:
            yield
        finally:
            # A writer thread sharing this store (the MCP server's auto-watch
            # does) calls _begin_immediate, which rolls back whatever is open
            # and starts its own transaction. Rolling back here would then
            # discard that writer's rows. The token says whether the
            # transaction being closed is still ours.
            if self._active_read_txn is token:
                self._active_read_txn = None
                try:
                    # Read-only: rollback and commit are equivalent, and
                    # rollback cannot fail on a transaction that wrote nothing.
                    self._conn.rollback()
                except sqlite3.Error as exc:  # pragma: no cover - defensive
                    logger.debug("Could not close the read transaction: %s", exc)

    def _begin_immediate(self) -> None:
        """Start an IMMEDIATE transaction, rolling back any prior uncommitted
        transaction first (regression guard for #135 / #489).
        """
        if self._conn.in_transaction:
            if self._active_read_txn is None:
                logger.warning(
                    "Rolling back uncommitted transaction before BEGIN IMMEDIATE"
                )
            else:
                # A concurrent multi-statement read on this connection; taking
                # it over costs that reader its snapshot, nothing more.
                logger.debug("Taking over an open read transaction for a write")
            self._active_read_txn = None
            self._conn.rollback()
        self._conn.execute("BEGIN IMMEDIATE")

    def store_file_nodes_edges(
        self, file_path: str, nodes: list[NodeInfo], edges: list[EdgeInfo], fhash: str = ""
    ) -> None:
        """Atomically replace all data for a file."""
        self._begin_immediate()
        try:
            self._replace_file_data(file_path, nodes, edges, fhash)
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        self._invalidate_cache()

    def store_file_batch(
        self, batch: list[tuple[str, list[NodeInfo], list[EdgeInfo], str]]
    ) -> None:
        """Atomically replace data for a batch of files in one transaction."""
        self._begin_immediate()
        try:
            for file_path, nodes, edges, fhash in batch:
                self._replace_file_data(file_path, nodes, edges, fhash)
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        self._invalidate_cache()

    def store_file_edges(self, file_path: str, edges: list[EdgeInfo]) -> None:
        """Atomically replace a file's edges and keep its node rows.

        For a file whose bytes did not change. Its symbols are the same, and
        flows and communities point at their node ids, so those ids must stay.
        """
        normalized = normalize_file_path(file_path)
        self._begin_immediate()
        try:
            # The edges come from a fresh parse and name base identities; align
            # them with the collision identities this file's rows already hold.
            stored = self._conn.execute(
                "SELECT qualified_name, line_start, line_end FROM nodes "
                "WHERE file_path = ?",
                (normalized,),
            ).fetchall()
            collisions = self._collision_map(
                (
                    self._collision_base(row["qualified_name"]),
                    row["line_start"], row["line_end"], row["qualified_name"],
                )
                for row in stored
            )
            self._conn.execute("DELETE FROM edges WHERE file_path = ?", (normalized,))
            self._insert_edges(
                self._aligned_edges(normalized, edges, collisions), time.time(),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        self._invalidate_cache()

    def relocate_edge_targets(self, old_prefix: str, new_prefix: str) -> int:
        """Rewrite edge targets that start with *old_prefix* to *new_prefix*.

        Used for files behind a followed symlinked folder outside the
        repository: a resolver that resolves symlinks names the outside real
        path, and the graph stores those files under the in-repo path (#269).
        """
        cursor = self._conn.execute(
            "UPDATE edges SET target_qualified = ? || substr(target_qualified, ?) "
            "WHERE substr(target_qualified, 1, ?) = ?",
            (new_prefix, len(old_prefix) + 1, len(old_prefix), old_prefix),
        )
        self._conn.commit()
        if cursor.rowcount:
            self._invalidate_cache()
        return cursor.rowcount

    def _replace_file_data(
        self, file_path: str, nodes: list[NodeInfo], edges: list[EdgeInfo], fhash: str = ""
    ) -> None:
        """Delete and re-insert one file's nodes/edges with batched statements.

        The previous implementation ran :meth:`upsert_node` /
        :meth:`upsert_edge` once per symbol, i.e. a SELECT + INSERT (or
        SELECT + UPDATE) round trip for every row. On large graphs (10^5+
        nodes, 10^6+ edges) that per-row Python/SQLite round trip dominates
        the whole build and can take tens of minutes (issue #721). This bulk
        path deduplicates rows in Python and inserts everything with
        ``executemany``, so each statement is prepared once and the file
        lands in a single transaction.

        Must be called inside an open transaction (BEGIN IMMEDIATE).
        """
        normalized = normalize_file_path(file_path)
        # Same-file definitions that share a base qualified name keep their
        # own rows (#585). Plan identities before the old rows are deleted.
        identities = self._plan_file_identities(normalized, nodes)
        self._conn.execute("DELETE FROM nodes WHERE file_path = ?", (normalized,))
        self._conn.execute("DELETE FROM edges WHERE file_path = ?", (normalized,))

        now = time.time()
        if nodes:
            # Planned identities are unique within the file. ON CONFLICT keeps
            # upsert_node()'s semantics for any row another spelling left.
            node_rows: list[tuple] = []
            for node, qualified in zip(nodes, identities):
                extra = json.dumps(node.extra) if node.extra else "{}"
                node_rows.append((
                    node.kind, node.name, qualified, node.file_path,
                    node.line_start, node.line_end, node.language,
                    node.parent_name, node.params, node.return_type,
                    node.modifiers, int(node.is_test), fhash,
                    extra, _symbol_of(self._make_qualified(node)), _indexed_docstring(node.extra),
                    node_index_tokens(node.kind, node.name, node.parent_name,
                                      normalized),
                    now,
                ))
            self._conn.executemany(
                """INSERT INTO nodes
                   (kind, name, qualified_name, file_path, line_start, line_end,
                    language, parent_name, params, return_type, modifiers, is_test,
                    file_hash, extra, symbol, docstring, name_tokens, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(qualified_name) DO UPDATE SET
                     kind=excluded.kind, name=excluded.name,
                     file_path=excluded.file_path,
                     line_start=excluded.line_start,
                     line_end=excluded.line_end,
                     language=excluded.language,
                     parent_name=excluded.parent_name, params=excluded.params,
                     return_type=excluded.return_type, modifiers=excluded.modifiers,
                     is_test=excluded.is_test, file_hash=excluded.file_hash,
                     extra=excluded.extra, symbol=excluded.symbol,
                     docstring=excluded.docstring, name_tokens=excluded.name_tokens,
                     updated_at=excluded.updated_at
                """,
                node_rows,
            )
        collisions = self._collision_map(
            (self._make_qualified(node), node.line_start, node.line_end, qualified)
            for node, qualified in zip(nodes, identities)
        )
        self._insert_edges(self._aligned_edges(normalized, edges, collisions), now)
        self._invalidate_cache()

    def _insert_edges(self, edges: list[EdgeInfo], now: float) -> None:
        """Insert one file's edges; its previous edge rows are already deleted."""
        if not edges:
            return
        # upsert_edge() collapses duplicate call sites (same kind, source,
        # target, file, line and optional offset) into a row carrying the *last*
        # extra/confidence values. Replicate that collapse in Python so a
        # plain INSERT batch is safe — the file's previous rows were
        # deleted above and an identical edge cannot come from another
        # file (file_path is part of the identity).
        edge_by_site: dict[tuple[str, str, str, str, int, int | None], tuple] = {}
        for edge in edges:
            extra_dict = edge.extra if edge.extra else {}
            confidence = float(extra_dict.get("confidence", 1.0))
            confidence_tier = str(extra_dict.get("confidence_tier", "EXTRACTED"))
            extra = json.dumps(extra_dict)
            site = (
                edge.kind, edge.source, edge.target, edge.file_path, edge.line,
                extra_dict.get("source_offset"),
            )
            edge_by_site[site] = (
                edge.kind, edge.source, edge.target, edge.file_path, edge.line,
                extra, confidence, confidence_tier, now,
            )
        self._conn.executemany(
            """INSERT INTO edges
               (kind, source_qualified, target_qualified, file_path, line, extra,
                confidence, confidence_tier, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            list(edge_by_site.values()),
        )

    def _aligned_edges(
        self,
        file_path: str,
        edges: list[EdgeInfo],
        collisions: dict[str, list[tuple[int, int, str]]],
    ) -> list[EdgeInfo]:
        """Align one file's parsed edges with its own and stored identities.

        The file's own endpoints are settled by *collisions*; only endpoints
        in other files can need the index lookup, once per distinct name.
        """
        local_prefix = f"{file_path}::"
        cache: dict[str, list[str]] = {}
        return [
            self._resolve_edge_identities(
                self._align_edge_with_collisions(edge, collisions)
                if collisions else edge,
                local_prefix,
                cache,
            )
            for edge in edges
        ]

    @staticmethod
    def _collision_map(
        entries: Iterable[tuple[str, int, int, str]],
    ) -> dict[str, list[tuple[int, int, str]]]:
        """Group ``(base, line_start, line_end, identity)`` by colliding base.

        A base is kept when it has several definitions, or one whose identity
        is suffixed (the survivor of a collision that shrank).
        """
        by_base: dict[str, list[tuple[int, int, str]]] = {}
        for base, line_start, line_end, qualified in entries:
            by_base.setdefault(base, []).append((line_start, line_end, qualified))
        return {
            base: found for base, found in by_base.items()
            if len(found) > 1 or found[0][2] != base
        }

    def _align_edge_with_collisions(
        self,
        edge: EdgeInfo,
        collisions: dict[str, list[tuple[int, int, str]]],
    ) -> EdgeInfo:
        """Point an edge at the colliding definition evidenced by its line."""
        # The parser loads on first use (#1139), so the module global is unset.
        from .parser import EdgeInfo

        if edge.kind == "TESTED_BY":
            # The parser mints TESTED_BY as a test's CALLS edge reversed, on
            # the same line, so align it as that call and reverse it back.
            call = self._align_edge_with_collisions(
                EdgeInfo(kind="CALLS", source=edge.target, target=edge.source,
                         file_path=edge.file_path, line=edge.line, extra=edge.extra),
                collisions,
            )
            if (
                call.source == edge.target and call.target == edge.source
                and call.extra is edge.extra
            ):
                return edge
            return EdgeInfo(
                kind=edge.kind, source=call.target, target=call.source,
                file_path=edge.file_path, line=edge.line, extra=call.extra,
            )
        source = edge.source
        source_candidates = collisions.get(source, [])
        if len(source_candidates) == 1:
            source = source_candidates[0][2]
        containing_sources = [
            (line_start, line_end, qualified)
            for line_start, line_end, qualified in source_candidates
            if line_start <= edge.line <= line_end
        ]
        if containing_sources:
            source = min(containing_sources, key=lambda item: item[1] - item[0])[2]

        target = edge.target
        target_candidates = collisions.get(target, [])
        if len(target_candidates) == 1:
            target = target_candidates[0][2]
        elif edge.kind == "CONTAINS" and target_candidates:
            starting_targets = [
                qualified
                for line_start, _, qualified in target_candidates
                if line_start == edge.line
            ]
            if len(starting_targets) == 1:
                target = starting_targets[0]
        elif edge.source == edge.target and any(
            source == qualified for _, _, qualified in target_candidates
        ):
            # A recursive call inside a collided definition resolves to that
            # definition, the one whose lines hold the call.
            target = source
        elif target_candidates and edge.kind in _COLLISION_CALL_KINDS:
            # Nothing at the call says which definition it means, and the
            # first one is no better a guess than the rest.
            return EdgeInfo(
                kind=edge.kind, source=source,
                target=_plain_collision_name(edge.target),
                file_path=edge.file_path, line=edge.line,
                extra=_ambiguous_collision_extra(
                    edge.extra or {}, edge.target,
                    [qualified for _, _, qualified in target_candidates],
                ),
            )

        if source == edge.source and target == edge.target:
            return edge
        return EdgeInfo(
            kind=edge.kind,
            source=source,
            target=target,
            file_path=edge.file_path,
            line=edge.line,
            extra=edge.extra,
        )

    def set_metadata(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", (key, value)
        )
        self._conn.commit()

    def get_metadata(self, key: str) -> Optional[str]:
        row = self._conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def get_repo_root(self) -> Optional[str]:
        """Absolute root of the repository this graph describes, if known.

        ``file_path`` values are absolute, so any consumer that reads a path
        convention out of them (``tests/``, ``src/test/``) has to know where
        the repository starts, or it reads the directories above the checkout
        instead. Builds record the root; for a graph built before they did,
        the default database location ``<root>/.code-review-graph/graph.db``
        gives the same answer. ``None`` means neither applies, and callers
        must degrade explicitly rather than assume the whole path is inside
        the repository.
        """
        recorded = self.get_metadata("repo_root")
        if recorded:
            return recorded
        parent = self.db_path.parent
        if parent.name == ".code-review-graph":
            return str(parent.parent)
        return None

    def has_nodes(self) -> bool:
        row = self._conn.execute("SELECT 1 FROM nodes LIMIT 1").fetchone()
        return row is not None

    def has_nodes_for_language(self, language: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM nodes WHERE language = ? LIMIT 1",
            (language,),
        ).fetchone()
        return row is not None

    def commit(self) -> None:
        self._conn.commit()
        self._invalidate_cache()

    def rollback(self) -> None:
        """Rollback the current transaction."""
        self._conn.rollback()
        self._invalidate_cache()

    # --- Read operations ---

    def get_node(self, qualified_name: str) -> Optional[GraphNode]:
        row = self._conn.execute(
            "SELECT * FROM nodes WHERE qualified_name = ?", (qualified_name,)
        ).fetchone()
        if row is None:
            # Bridge Windows-native path spellings to the stored POSIX
            # identity (#774), mirroring the file-keyed lookups above.
            bridged = _bridge_qualified_name(qualified_name)
            if bridged != qualified_name:
                row = self._conn.execute(
                    "SELECT * FROM nodes WHERE qualified_name = ?", (bridged,)
                ).fetchone()
        return self._row_to_node(row) if row else None

    def get_nodes_by_file(self, file_path: str) -> list[GraphNode]:
        return list(self.iter_nodes_by_file(file_path))

    def iter_nodes_by_file(self, file_path: str) -> Iterator[GraphNode]:
        """Yield file nodes without first materializing the complete row set."""
        rows = self._conn.execute(
            "SELECT * FROM nodes WHERE file_path = ?", (normalize_file_path(file_path),)
        )
        for row in rows:
            yield self._row_to_node(row)

    def get_all_nodes(self, exclude_files: bool = True) -> list[GraphNode]:
        """Return all nodes, optionally excluding File nodes."""
        if exclude_files:
            rows = self._conn.execute(
                "SELECT * FROM nodes WHERE kind != 'File'"
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM nodes").fetchall()
        return [self._row_to_node(r) for r in rows]

    def get_edges_by_source(self, qualified_name: str) -> list[GraphEdge]:
        return list(self.iter_edges_by_source(qualified_name))

    def iter_edges_by_source(self, qualified_name: str) -> Iterator[GraphEdge]:
        """Yield outgoing edges without first materializing the complete set."""
        # ORDER BY: the index lists rows by kind first, and a capped answer or a
        # traversal would then depend on which index SQLite happened to use.
        rows = self._conn.execute(
            "SELECT * FROM edges WHERE source_qualified = ? ORDER BY id",
            (qualified_name,),
        )
        for row in rows:
            yield self._row_to_edge(row)

    def get_edges_by_target(self, qualified_name: str) -> list[GraphEdge]:
        return list(self.iter_edges_by_target(qualified_name))

    def get_direct_dependent_files(self, file_path: str) -> set[str]:
        """Files that import *file_path* or point an edge at a node in it.

        One indexed query instead of one lookup per node in the file. The
        node edges counted are CALLS, IMPORTS_FROM, INHERITS and IMPLEMENTS;
        *file_path* itself is never its own dependent.
        """
        norm_fp = normalize_file_path(file_path)
        rows = self._conn.execute(
            "SELECT file_path FROM edges INDEXED BY idx_edges_target_kind "
            "WHERE target_qualified IN (?, ?) AND kind = 'IMPORTS_FROM' "
            "UNION "
            "SELECT e.file_path FROM nodes n "
            "JOIN edges e INDEXED BY idx_edges_target_kind "
            "ON e.target_qualified = n.qualified_name "
            "WHERE n.file_path = ? "
            "AND e.kind IN ('CALLS', 'IMPORTS_FROM', 'INHERITS', 'IMPLEMENTS')",
            (file_path, norm_fp, norm_fp),
        ).fetchall()
        dependents = {row[0] for row in rows}
        dependents.discard(file_path)
        dependents.discard(norm_fp)
        return dependents

    def iter_edges_by_target(self, qualified_name: str) -> Iterator[GraphEdge]:
        """Yield incoming edges without first materializing the complete set."""
        rows = self._conn.execute(
            "SELECT * FROM edges WHERE target_qualified = ? ORDER BY id",
            (qualified_name,),
        )
        for row in rows:
            yield self._row_to_edge(row)

    def get_config_consumers(self, key: str) -> list[GraphEdge]:
        """Find direct and ConfigurationProperties-prefix consumers of a key."""
        parts = key.split(".")
        targets = [f"config:{key}", f"config:{key}.*"]
        targets.extend(
            f"config:{'.'.join(parts[:index])}.*"
            for index in range(1, len(parts))
        )
        rows = []
        for target in dict.fromkeys(targets):
            rows.extend(self._conn.execute(
                "SELECT * FROM edges WHERE kind = 'DEPENDS_ON_CONFIG' "
                "AND target_qualified = ? ORDER BY id",
                (target,),
            ).fetchall())
        return [self._row_to_edge(row) for row in rows]

    def get_edges_claiming(self, qualified_name: str) -> list[GraphEdge]:
        """Edges whose settled endpoint once named *qualified_name*.

        ``_settle_unbound_endpoints`` turns an endpoint that names no node back
        into the plain name and keeps the qualified one under
        ``CLAIMED_ENDPOINT_KEY``, so a symbol a diff deleted is still found by
        the calls written against it.
        """
        rows = self._conn.execute(
            "SELECT * FROM edges WHERE extra LIKE ? "
            "AND json_valid(extra) AND json_extract(extra, ?) = ? ORDER BY id",
            (f'%"{CLAIMED_ENDPOINT_KEY}"%', f"$.{CLAIMED_ENDPOINT_KEY}", qualified_name),
        ).fetchall()
        return [self._row_to_edge(row) for row in rows]

    def search_edges_by_target_name(
        self, name: str, kind: str = "CALLS", language: str | None = None,
    ) -> list[GraphEdge]:
        """Search for edges where target_qualified matches an unqualified name.

        CALLS edges often store unqualified target names (e.g. ``generateTestCode``)
        rather than fully qualified ones (``file.ts::generateTestCode``).  This
        method finds those edges by exact match on the plain function name so that
        reverse call tracing (callers_of) works even when qualified-name lookup
        returns nothing.

        When ``language`` is given, only edges whose source node has a compatible
        language are returned. JavaScript, TypeScript, TSX and HTML pages form
        one family because calls and inheritance routinely cross those source
        types (JSX is stored as JavaScript; Astro as TypeScript). Other languages require an
        exact match. Bare names are ambiguous across the whole graph, so without
        this filter a common method name like ``clone`` can match a same-named
        method in an unrelated language (#708).
        """
        return list(self.iter_edges_by_target_name(name, kind=kind, language=language))

    def iter_edges_by_target_name(
        self, name: str, kind: str = "CALLS", language: str | None = None,
    ) -> Iterator[GraphEdge]:
        """Yield exact bare-target edges without materializing all matches."""
        if language:
            languages = _compatible_edge_languages(language)
            placeholders = ", ".join("?" for _ in languages)
            rows = self._conn.execute(
                "SELECT edges.* FROM edges "
                "JOIN nodes ON nodes.qualified_name = edges.source_qualified "
                "WHERE edges.target_qualified = ? AND edges.kind = ? "
                f"AND nodes.language IN ({placeholders})",
                (name, kind, *languages),
            )
        else:
            rows = self._conn.execute(
                "SELECT * FROM edges WHERE target_qualified = ? AND kind = ?",
                (name, kind),
            )
        for row in rows:
            yield self._row_to_edge(row)

    def get_transitive_tests(
        self,
        qualified_name: str,
        max_depth: int = 1,
        max_frontier: int | None = None,
        *,
        include_helper_routes: bool = True,
    ) -> list[dict]:
        """Find tests covering a node, including indirect (transitive) coverage.

        TESTED_BY edges are stored as source=production, target=test by
        the parser, so look them up by source_qualified. See: #515

        1. Direct: TESTED_BY edges originating at this node (+ bare-name fallback).
        2. Indirect: follow outgoing CALLS edges up to *max_depth* hops,
           then collect TESTED_BY edges on each callee.
        3. Follow incoming CALLS through test helpers to actual Test nodes,
           allowing *max_depth* helpers between the symbol and a test. Pass
           ``include_helper_routes=False`` to leave this step out: a call path
           is a pointer to a test, not a record of coverage, so risk scoring
           must not count it.

        Returns a list of dicts with node fields plus ``indirect: bool``.

        ``max_frontier`` caps the CALLS fan-out per BFS hop to prevent O(N*M)
        query explosion on hub functions in large graphs. Defaults to
        ``CRG_MAX_TRANSITIVE_FRONTIER`` env var (50 if unset). The callees
        kept are the first by name, so one graph gives one answer.
        """
        if max_frontier is None:
            max_frontier = env_int("CRG_MAX_TRANSITIVE_FRONTIER", 50)
        conn = self._conn
        seen: set[str] = set()
        results: list[dict] = []

        # If the input is a class or file, expand to the production symbols it
        # contains first. File targets are accepted by the public query tool,
        # so tests_for("src/Foo.php") must cover methods nested under classes
        # as well as top-level functions.
        input_qns = [qualified_name]
        row = conn.execute(
            "SELECT kind, file_path, parent_name FROM nodes "
            "WHERE qualified_name = ?",
            (qualified_name,),
        ).fetchone()
        if row and row["kind"] == "Class":
            for mrow in conn.execute(
                "SELECT target_qualified FROM edges "
                "WHERE source_qualified = ? AND kind = 'CONTAINS'",
                (qualified_name,),
            ).fetchall():
                input_qns.append(mrow["target_qualified"])
        elif row and row["kind"] == "File":
            for symbol in conn.execute(
                "SELECT qualified_name FROM nodes "
                "WHERE file_path = ? AND qualified_name != ? "
                "AND kind IN ('Class', 'Function', 'Method')",
                (row["file_path"], qualified_name),
            ).fetchall():
                input_qns.append(symbol["qualified_name"])
        target_file = row["file_path"] if row else ""
        target_parent = row["parent_name"] if row else None

        def _node_dict(qn: str, indirect: bool) -> dict | None:
            row = conn.execute(
                "SELECT * FROM nodes WHERE qualified_name = ?", (qn,)
            ).fetchone()
            if not row:
                return None
            return {
                "name": row["name"],
                "qualified_name": row["qualified_name"],
                "file_path": row["file_path"],
                "kind": row["kind"],
                "indirect": indirect,
            }

        # Direct TESTED_BY (source=production, target=test). See: #515
        for qn in input_qns:
            for row in conn.execute(
                "SELECT target_qualified, extra FROM edges "
                "WHERE source_qualified = ? AND kind = 'TESTED_BY'",
                (qn,),
            ).fetchall():
                if _edge_extra_is_unresolved(row["extra"]):
                    continue
                tgt = row["target_qualified"]
                if tgt not in seen:
                    seen.add(tgt)
                    d = _node_dict(tgt, indirect=False)
                    if d:
                        results.append(d)

        # Evidence-gated bare-name fallback for old/minimal graphs that have
        # not run endpoint resolution yet. A matching name alone is not enough.
        bare = qualified_name.rsplit("::", 1)[-1] if "::" in qualified_name else qualified_name
        candidate_cache: dict[str, list[tuple[str, str]]] = {}
        import_cache: dict[str, set[str]] = {}
        # Same dotted-module evidence the endpoint resolver uses, for graphs
        # queried before that pass has run. Built on first use: most queries
        # never reach this fallback, and the index scans every .py path.
        # See: #903
        module_files: dict[str, str] | None = None

        def _candidate_for_context(name: str, context_file: str) -> str | None:
            nonlocal module_files
            if name not in candidate_cache:
                candidate_cache[name] = [
                    (candidate["qualified_name"], candidate["file_path"])
                    for candidate in conn.execute(
                        "SELECT qualified_name, file_path FROM nodes "
                        "WHERE name = ? "
                        "AND kind IN ('Function', 'Test', 'Class')",
                        (name,),
                    ).fetchall()
                ]
            if context_file not in import_cache:
                if module_files is None:
                    module_files = self._python_module_file_index(conn)
                imported_files: set[str] = set()
                for imported in conn.execute(
                    "SELECT target_qualified FROM edges "
                    "WHERE kind = 'IMPORTS_FROM' AND file_path = ?",
                    (context_file,),
                ).fetchall():
                    target = imported["target_qualified"]
                    target_file = (
                        target.split("::", 1)[0] if "::" in target else target
                    )
                    imported_files.add(target_file)
                    resolved_module = module_files.get(target_file)
                    if resolved_module:
                        imported_files.add(resolved_module)
                import_cache[context_file] = imported_files
            return self._select_evidence_backed_candidate(
                candidate_cache[name],
                context_file,
                import_cache[context_file],
            )

        for row in conn.execute(
            "SELECT target_qualified, file_path, extra FROM edges "
            "WHERE source_qualified = ? AND kind = 'TESTED_BY'",
            (bare,),
        ).fetchall():
            if _edge_extra_is_unresolved(row["extra"]):
                continue
            if _candidate_for_context(bare, row["file_path"]) != qualified_name:
                continue
            # TESTED_BY is minted from the test's own CALLS edge and inherits
            # its metadata, so a bare source here carries the same receiver
            # evidence a bare call target does. An import that merely brings a
            # same-named symbol into the test file is not proof the test
            # exercised this node — `some_dict.get(...)` in a test is still
            # not a test of `ConnectionPool.get`. See: #997
            try:
                tested_by_extra = json.loads(row["extra"] or "{}")
            except (TypeError, json.JSONDecodeError):
                tested_by_extra = {}
            if isinstance(tested_by_extra, dict) and not (
                self._receiver_evidence_admits_candidate(
                    tested_by_extra, qualified_name, target_file, target_parent,
                )
            ):
                continue
            tgt = row["target_qualified"]
            if tgt not in seen:
                seen.add(tgt)
                d = _node_dict(tgt, indirect=False)
                if d:
                    results.append(d)

        # Transitive: follow CALLS edges, then collect TESTED_BY on callees.
        # Each hop is sorted before it is cut: a set's order changes with the
        # string hash seed, so cutting it unsorted kept different callees in
        # every new process. Callees with a node go first, since the rest are skipped.
        frontier = sorted(set(input_qns))
        for _ in range(max_depth):
            next_frontier: set[str] = set()
            for qn in frontier:
                for row in conn.execute(
                    "SELECT target_qualified, extra FROM edges "
                    "WHERE source_qualified = ? AND kind = 'CALLS'",
                    (qn,),
                ).fetchall():
                    if _edge_extra_is_unresolved(row["extra"]):
                        continue
                    next_frontier.add(row["target_qualified"])
            # A callee with no node has no stable identity, be it a bare name
            # or a scoped one like ``String::new``. Endpoint resolution binds it
            # when graph evidence exists; otherwise following TESTED_BY here
            # would attribute every same-named test. One query per hop.
            bound_callees = self._existing_qualified_names(next_frontier)
            frontier = sorted(
                next_frontier, key=lambda name: (name not in bound_callees, name),
            )[:max_frontier]
            for callee in frontier:
                if callee not in bound_callees:
                    continue
                for row in conn.execute(
                    "SELECT target_qualified, extra FROM edges "
                    "WHERE source_qualified = ? AND kind = 'TESTED_BY'",
                    (callee,),
                ).fetchall():
                    if _edge_extra_is_unresolved(row["extra"]):
                        continue
                    tgt = row["target_qualified"]
                    if tgt not in seen:
                        seen.add(tgt)
                        d = _node_dict(tgt, indirect=True)
                        if d:
                            results.append(d)

        if max_depth > 0 and include_helper_routes:
            for route in self._get_test_helper_routes(
                input_qns, max_depth + 1, max_frontier, self.get_repo_root(),
            ).values():
                for test in route["tests"]:
                    if test not in seen:
                        seen.add(test)
                        d = _node_dict(test, indirect=True)
                        if d:
                            results.append(d)

        return results

    def get_ambiguous_call_tests(self, qualified_name: str) -> list[str]:
        """Tests with a call that may mean this PHP symbol, but was never pinned.

        A Laravel test reaches a class through the container, as in
        ``app(Service::class)->run()``, so nothing in the file says which class
        ``run`` belongs to. When more than one class the test file imports (or
        the test file itself) defines ``run``, the call stays unresolved and the
        resolver keeps those classes' methods on the edge as
        ``ambiguous_targets``. This returns each test whose list holds
        *qualified_name*. That is a guess, never coverage: risk scoring and the
        test-gap report do not read it.

        A call made on a class the test builds, as in
        ``(new UpdateBooks)->handle()``, is taken to mean that class's method,
        so it is not a guess for the others. A class built on the call's own
        line settles only that call. A class built on a line with no such
        call, as in ``$job = new UpdateBooks()`` and a later
        ``$job->withBatchId($id)->handle()``, settles the test's other calls.

        Only PHP is answered (#850). A class or file target is expanded to its
        methods, as :meth:`get_transitive_tests` does. A list cut at 20
        entries can leave a guess out, never put a wrong one in.
        """
        conn = self._conn
        built_by_test: dict[str, set[tuple[str, int]]] = {}

        def owner(method_qn: str) -> str:
            path, _, tail = method_qn.rpartition("::")
            return f"{path}::{tail.rpartition('.')[0]}"

        def built(test: str) -> set[tuple[str, int]]:
            # ``new Cart()`` in a test is a traced call to the class node, so
            # its line says where the class was built.
            if test not in built_by_test:
                built_by_test[test] = {
                    (r["source_qualified"], r["line"]) for r in conn.execute(
                        "SELECT e.source_qualified, e.line FROM edges e "
                        "JOIN nodes n ON n.qualified_name = e.source_qualified "
                        "WHERE e.kind = 'TESTED_BY' AND e.target_qualified = ? "
                        "AND n.kind = 'Class'",
                        (test,),
                    ).fetchall()
                }
            return built_by_test[test]

        row = conn.execute(
            "SELECT qualified_name, kind, name, file_path, language FROM nodes "
            "WHERE qualified_name = ?",
            (qualified_name,),
        ).fetchone()
        if not row or row["language"] != "php":
            return []
        if row["kind"] == "Class":
            symbol_rows = conn.execute(
                "SELECT n.qualified_name, n.name FROM edges e "
                "JOIN nodes n ON n.qualified_name = e.target_qualified "
                "WHERE e.source_qualified = ? AND e.kind = 'CONTAINS' "
                "AND n.kind = 'Function'",
                (qualified_name,),
            ).fetchall()
        elif row["kind"] == "File":
            symbol_rows = conn.execute(
                "SELECT qualified_name, name FROM nodes "
                "WHERE file_path = ? AND kind = 'Function'",
                (row["file_path"],),
            ).fetchall()
        elif row["kind"] == "Function":
            symbol_rows = [row]
        else:
            return []

        tests: list[str] = []
        for symbol_qn, symbol_name in (
            (r["qualified_name"], r["name"]) for r in symbol_rows
        ):
            # The edge's source stays the bare name while it is ambiguous. The
            # text match only narrows the rows; the parsed list decides.
            edges = conn.execute(
                "SELECT target_qualified, line, extra FROM edges "
                "WHERE kind = 'TESTED_BY' AND source_qualified = ? "
                "AND instr(extra, ?) > 0",
                (symbol_name, json.dumps(symbol_qn)),
            ).fetchall()
            call_lines: dict[str, set[int]] = {}
            for edge in edges:
                call_lines.setdefault(edge["target_qualified"], set()).add(edge["line"])
            for edge in edges:
                try:
                    extra = json.loads(edge["extra"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                candidates = extra.get("ambiguous_targets") if isinstance(extra, dict) else None
                test = edge["target_qualified"]
                if (
                    not isinstance(candidates, list)
                    or symbol_qn not in candidates
                    or test == symbol_qn
                    or test in tests
                ):
                    continue
                possible = {owner(c) for c in candidates if isinstance(c, str)}
                made = built(test)
                owners = {qn for qn, at in made if at == edge["line"]} & possible
                if not owners:
                    owners = {
                        qn for qn, at in made if at not in call_lines[test]
                    } & possible
                if owners and owner(symbol_qn) not in owners:
                    continue
                tests.append(test)
        return tests

    def _get_test_helper_routes(
        self, seeds: list[str], max_depth: int, max_callers: int, repo_root: str | None,
    ) -> dict[str, dict[str, Any]]:
        """Resolve CALLS paths through test helpers, never helper TESTED_BY edges.

        Helpers are marked as test code after parsing, so they need not have
        TESTED_BY edges. Only reaching a real Test declaration supplies evidence;
        a fixture's is_test flag alone cannot vouch for production code.
        """
        from .parser import is_test_file

        routes: dict[str, dict[str, Any]] = {}
        frontier = {qn: {qn} for qn in seeds if "::" in qn}
        expanded = {qn: set(origins) for qn, origins in frontier.items()}
        for depth in range(1, max_depth + 1):
            keys = sorted(frontier)
            if not keys:
                break
            hubs: set[str] = set()
            for start in range(0, len(keys), 450):
                batch = keys[start:start + 450]
                placeholders = ",".join("?" for _ in batch)
                for row in self._conn.execute(  # nosec B608
                    "SELECT target_qualified, COUNT(*) AS n FROM edges "
                    f"WHERE kind = 'CALLS' AND target_qualified IN ({placeholders}) "
                    "GROUP BY target_qualified", batch,
                ).fetchall():
                    if row["n"] > max_callers:
                        hubs.add(row["target_qualified"])
            keys = [key for key in keys if key not in hubs]
            next_frontier: dict[str, set[str]] = {}
            for start in range(0, len(keys), 450):
                batch = keys[start:start + 450]
                placeholders = ",".join("?" for _ in batch)
                rows = self._conn.execute(  # nosec B608
                    "SELECT e.source_qualified, e.target_qualified, e.extra, "
                    "n.kind, n.file_path, n.is_test FROM edges e "
                    "JOIN nodes n ON n.qualified_name = e.source_qualified "
                    f"WHERE e.kind = 'CALLS' AND e.target_qualified IN ({placeholders}) "
                    "ORDER BY e.source_qualified, e.target_qualified", batch,
                ).fetchall()
                for row in rows:
                    caller = row["source_qualified"]
                    if "::" not in caller or _edge_extra_is_unresolved(row["extra"]):
                        continue
                    origins = frontier[row["target_qualified"]]
                    fresh = origins - expanded.get(caller, set()) - {caller}
                    if row["kind"] == "Test":
                        for origin in sorted(fresh):
                            route = routes.setdefault(origin, {
                                "via": caller, "depth": depth, "tests": [],
                            })
                            if caller not in route["tests"]:
                                route["tests"].append(caller)
                    elif row["is_test"] or is_test_file(row["file_path"], repo_root):
                        next_frontier.setdefault(caller, set()).update(fresh)
            for caller, origins in next_frontier.items():
                expanded.setdefault(caller, set()).update(origins)
            frontier = next_frontier
        return routes

    def get_directly_tested(self, qualified_names: Iterable[str]) -> set[str]:
        """Which of *qualified_names* carry a TESTED_BY edge of their own.

        One batched query per 450 names instead of one edge fetch per symbol;
        a test-gap report asks this about every changed symbol at once.
        """
        names = [qn for qn in dict.fromkeys(qualified_names) if qn]
        tested: set[str] = set()
        for start in range(0, len(names), 450):
            batch = names[start:start + 450]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(  # nosec B608
                "SELECT DISTINCT source_qualified FROM edges "
                f"WHERE kind = 'TESTED_BY' AND source_qualified IN ({placeholders})",
                batch,
            ).fetchall()
            tested.update(row["source_qualified"] for row in rows)
        return tested

    def get_caller_test_routes(
        self,
        qualified_names: Iterable[str],
        max_depth: int = CALLER_TEST_ROUTE_DEPTH,
        max_callers_per_node: int | None = None,
        repo_root: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Find, for each input symbol, a tested *caller* that reaches it.

        This walks *upwards* over incoming CALLS edges, unlike the outgoing
        callee traversal in :meth:`get_transitive_tests`: a
        private helper that no test names directly is still run by the tests of
        the public function that calls it, and reporting it as flatly
        "untested" is a false alarm the author cannot act on (#1047).

        **What a route proves, and what it does not.** A route is a static
        CALLS path from a tested caller down to the symbol. It does not show
        that any test executes the symbol: a helper can sit under two
        well-tested callers on a branch none of their tests take. Measured
        against line coverage over this repository's own suite, about 6% of
        the symbols this walk reaches are never executed by any test, and no
        shape of the call graph separates them -- the discriminating fact is
        which branch a test takes, which the graph does not record. Tightening
        the hop limit, the caller fan-out or the file boundary was tried and
        each halves the number of symbols reached while leaving that ~6%
        roughly where it was. So the result is reported as reachability and
        never scored as coverage: ``analyze_changes`` keeps an indirectly
        reached symbol in the gap report at full untested risk, and only adds
        the route as a pointer to where a test already runs nearby.

        Args:
            qualified_names: Symbols with no direct TESTED_BY edge of their own.
            max_depth: Maximum CALLS hops to walk upwards. The default of
                ``CALLER_TEST_ROUTE_DEPTH`` (2) is measured, not guessed: on
                this repository depths 3-5 reach no additional symbol while
                each concedes another ~8% of the codebase to "reached".
            max_callers_per_node: Skip expanding any one node with more
                incoming CALLS than this (default
                ``CALLER_TEST_ROUTE_MAX_CALLERS``). Deliberately a per-node
                limit rather than a budget shared across the inputs: a shared
                budget makes one hub in a change set delete the routes of
                every other symbol in it, which is the false alarm this walk
                exists to remove. It is applied before the callers are read,
                so it bounds the work rather than the leftovers.
            repo_root: Repository root, used to recognise callers that live in
                test files. Such a caller's TESTED_BY edges point at other
                symbols in its own test file, so crediting a production symbol
                through one would let a test fixture vouch for the code it
                sets up. Resolved CALLS paths through helpers to actual Test
                nodes are accepted. Defaults to the graph's recorded root.

        Returns:
            ``{qualified_name: {"via", "depth", "tests"}}`` holding only the
            inputs that were reached. ``via`` is the tested caller, ``depth``
            the hop count (1 = a direct caller), ``tests`` up to three of that
            caller's test symbols. Inputs absent from the mapping have no
            tested caller within *max_depth*.
        """
        if max_callers_per_node is None:
            max_callers_per_node = CALLER_TEST_ROUTE_MAX_CALLERS
        if repo_root is None:
            repo_root = self.get_repo_root()
        seeds = [qn for qn in dict.fromkeys(qualified_names) if qn]
        if not seeds or max_depth < 1:
            return {}
        routes = self._get_test_helper_routes(seeds, max_depth, max_callers_per_node, repo_root)
        for route in routes.values():
            route["tests"] = route["tests"][:3]

        # node -> the seeds whose route currently passes through it. The walk
        # is level-synchronous and batched over every seed at once: one CALLS
        # query and one TESTED_BY query per hop, rather than a BFS per symbol.
        # On a 248-symbol delta that is 4 queries instead of several hundred.
        frontier: dict[str, set[str]] = {qn: {qn} for qn in seeds if qn not in routes}
        # node -> seeds already expanded through it, so a call cycle
        # terminates instead of re-walking the same edges each hop.
        expanded: dict[str, set[str]] = {qn: set(origins) for qn, origins in frontier.items()}

        for depth in range(1, max_depth + 1):
            callers: dict[str, set[str]] = {}
            keys = sorted(frontier)
            # Drop hubs before reading their edges, not after. Counting is one
            # aggregate per batch; materialising 50,000 caller rows to throw
            # most of them away is what used to make this slow, and throwing
            # them away by name let one hub evict unrelated symbols' routes.
            hubs: set[str] = set()
            for start in range(0, len(keys), 450):
                batch = keys[start:start + 450]
                placeholders = ",".join("?" for _ in batch)
                for row in self._conn.execute(  # nosec B608
                    "SELECT target_qualified, COUNT(*) AS n FROM edges "
                    f"WHERE kind = 'CALLS' AND target_qualified IN ({placeholders}) "
                    "GROUP BY target_qualified",
                    batch,
                ).fetchall():
                    if row["n"] > max_callers_per_node:
                        hubs.add(row["target_qualified"])
            keys = [key for key in keys if key not in hubs]
            for start in range(0, len(keys), 450):
                batch = keys[start:start + 450]
                placeholders = ",".join("?" for _ in batch)
                rows = self._conn.execute(  # nosec B608
                    "SELECT source_qualified, target_qualified, extra FROM edges "
                    f"WHERE kind = 'CALLS' AND target_qualified IN ({placeholders})",
                    batch,
                ).fetchall()
                for row in rows:
                    if _edge_extra_is_unresolved(row["extra"]):
                        continue
                    caller = row["source_qualified"]
                    # A bare caller name has no stable identity, so its
                    # TESTED_BY edges belong to no particular node. Crediting
                    # coverage through one would attribute every same-named
                    # function's tests to this symbol.
                    if "::" not in caller:
                        continue
                    origins = frontier.get(row["target_qualified"])
                    if not origins:
                        continue
                    fresh = origins - expanded.get(caller, frozenset())
                    fresh.discard(caller)
                    if fresh:
                        callers.setdefault(caller, set()).update(fresh)
            if not callers:
                break

            # A caller that lives in a test file is test code. Its own
            # TESTED_BY edges point at tests in that same file, so crediting a
            # production symbol through one lets a fixture vouch for the code
            # it sets up -- 20% of this graph's TESTED_BY edges start at a
            # test-file symbol, so the laundering is not hypothetical.
            from .parser import is_test_file

            caller_names = sorted(callers)
            for start in range(0, len(caller_names), 450):
                batch = caller_names[start:start + 450]
                placeholders = ",".join("?" for _ in batch)
                for row in self._conn.execute(  # nosec B608
                    "SELECT qualified_name, file_path, is_test FROM nodes "
                    f"WHERE qualified_name IN ({placeholders})",
                    batch,
                ).fetchall():
                    if row["is_test"] or is_test_file(row["file_path"], repo_root):
                        callers.pop(row["qualified_name"], None)
            if not callers:
                break

            tests: dict[str, list[str]] = {}
            caller_names = sorted(callers)
            for start in range(0, len(caller_names), 450):
                batch = caller_names[start:start + 450]
                placeholders = ",".join("?" for _ in batch)
                rows = self._conn.execute(  # nosec B608
                    "SELECT source_qualified, target_qualified, extra FROM edges "
                    f"WHERE kind = 'TESTED_BY' AND source_qualified IN ({placeholders})",
                    batch,
                ).fetchall()
                for row in rows:
                    if _edge_extra_is_unresolved(row["extra"]):
                        continue
                    bucket = tests.setdefault(row["source_qualified"], [])
                    if len(bucket) < 3 and row["target_qualified"] not in bucket:
                        bucket.append(row["target_qualified"])

            for caller in caller_names:
                covered = tests.get(caller)
                if not covered:
                    continue
                for origin in callers[caller]:
                    if origin not in routes:
                        routes[origin] = {
                            "via": caller,
                            "depth": depth,
                            "tests": list(covered),
                        }

            next_frontier: dict[str, set[str]] = {}
            for caller in caller_names:
                remaining = {o for o in callers[caller] if o not in routes}
                expanded.setdefault(caller, set()).update(callers[caller])
                if remaining:
                    next_frontier[caller] = remaining
            frontier = next_frontier
            if not frontier:
                break

        return routes

    @staticmethod
    def _python_module_file_index(conn: sqlite3.Connection) -> dict[str, str]:
        """Map each dotted Python module to the one indexed file defining it.

        Built from indexed ``.py`` paths rather than the filesystem, so it works
        on a graph queried away from the source tree. A module maps to a file
        only when exactly one file answers to it; ambiguous modules are dropped
        so they cannot manufacture evidence. Matching is on whole path segments,
        which keeps ``mypkg.core`` away from ``.../notmypkg/core.py``.
        """
        modules: dict[str, str | None] = {}
        for row in conn.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE file_path LIKE '%.py'"
        ).fetchall():
            file_path = row["file_path"]
            path = PurePosixPath(file_path).with_suffix("")
            # `.parts` leads with the root ("/"), which is never part of a
            # module name.
            segments = path.parts[1:] if path.is_absolute() else path.parts
            if segments and segments[-1] == "__init__":
                # `pkg/__init__.py` is imported as `pkg`, never `pkg.__init__`.
                segments = segments[:-1]
            for start in range(len(segments)):
                module = ".".join(segments[start:])
                if not module:
                    continue
                if modules.setdefault(module, file_path) != file_path:
                    modules[module] = None
        return {
            module: path for module, path in modules.items() if path is not None
        }

    @staticmethod
    def _select_evidence_backed_candidate(
        candidates: list[tuple[str, str]],
        context_file: str,
        imported_files: set[str],
    ) -> str | None:
        """Return the sole same-file/import-backed candidate, if one exists."""
        supported = [
            qualified
            for qualified, candidate_file in candidates
            if candidate_file == context_file or candidate_file in imported_files
        ]
        return supported[0] if len(supported) == 1 else None

    @staticmethod
    def _receiver_backed_candidates(
        candidates: list[tuple[str, str]],
        edge_extra: dict,
        parent_lookup: dict[str, str | None],
    ) -> list[tuple[str, str]]:
        """Narrow bare-name candidates to what the receiver can actually hold.

        ``x.get(...)`` calls the ``get`` belonging to whatever ``x`` is. The
        call-site file's import list says nothing about that, so a name match
        plus an import is not evidence — it is how a plain ``some_dict.get()``
        ended up recorded as a call into ``ConnectionPool.get``. The parser
        records what the file itself says the receiver is; honour it:

        ``module``
            the name is bound to a module file, so the method has to be a
            top-level name in that file — ``agent_baseline.run(...)`` means
            ``agent_baseline.py::run`` and nothing else.
        ``class``
            the name is annotated, constructed, or is itself an imported
            symbol, so the method has to belong to a class of that name.
        anything else
            a container literal, or a local with no evidence at all. There is
            nothing to attribute the call to, so nothing is attributed and the
            target stays bare for the unresolved path to explain.
        """
        binding = edge_extra.get("receiver_binding")
        if binding == "module":
            module_file = edge_extra.get("receiver_module")
            if not isinstance(module_file, str) or not module_file:
                return []
            return [
                candidate for candidate in candidates
                if candidate[1] == module_file
                and parent_lookup.get(candidate[0]) is None
            ]
        if binding == "class":
            class_name = edge_extra.get("receiver_class")
            if not isinstance(class_name, str) or not class_name:
                return []
            class_file = edge_extra.get("receiver_class_file")
            if class_file is not None and not isinstance(class_file, str):
                return []
            # A relative Ruby constant is looked up in the modules written
            # around it first: ``Cart`` inside ``module Shop`` is
            # ``Shop::Cart`` when that exists, and the top-level one if not.
            scopes = edge_extra.get("receiver_scopes")
            lookup = [
                *(
                    f"{scope}.{class_name}"
                    for scope in (scopes if isinstance(scopes, list) else [])
                    if isinstance(scope, str) and scope
                ),
                class_name,
            ]
            for path in lookup:
                matched = [
                    candidate for candidate in candidates
                    if parent_lookup.get(candidate[0]) == path
                    and (not class_file or candidate[1] == class_file)
                ]
                if matched:
                    return matched
            return []
        return []

    @staticmethod
    def _class_backed_candidates(
        edge_extra: dict,
        bare_name: str,
        caller_class: str,
        caller_language: str,
        context_file: str,
        inheritance: InheritanceIndex,
    ) -> Optional[tuple[list[str], Optional[str]]]:
        """(candidates, class inherited through) when the call names its class.

        ``pet.getName()`` on a ``Pet pet`` can only mean ``Pet.getName`` or the
        ``getName`` that ``Pet`` inherits; a same-named method elsewhere is no
        answer, not even in the same file. ``$this->run()`` and Java's
        ``this.run()`` name the caller's own class the same way, and
        ``super.run()`` its parent. See: #984

        None when the receiver says nothing about a class, so the usual file
        and import evidence applies.
        """
        receiver = edge_extra.get("receiver")
        owner: Optional[str]
        if edge_extra.get("receiver_resolution") == "typed_receiver_unresolved":
            receiver_type = edge_extra.get("receiver_type")
            owner = (
                inheritance.resolve_type(receiver_type, context_file, caller_language)
                if isinstance(receiver_type, str) and receiver_type
                else None
            )
            if owner is None:
                # Declared outside the graph, or two classes fit. Either way
                # it is not a same-named method nearby.
                return [], None
        elif (
            receiver in _SELF_RECEIVERS or receiver in _SUPER_RECEIVERS
        ) and "receiver_binding" not in edge_extra:
            if not caller_class:
                return None
            owner = caller_class
        elif (
            caller_language == "java"
            and isinstance(receiver, str)
            and receiver[:1].isupper()
            and "receiver_type" not in edge_extra
        ):
            # ``Helper.make()``: a class in the same package needs no import.
            owner = inheritance.resolve_type(receiver, context_file, "java")
            if owner is None:
                return None
        else:
            return None

        if receiver not in _SUPER_RECEIVERS:
            own = inheritance.method_of(owner, bare_name)
            if own is not None:
                return [own], None
        inherited = inheritance.inherited_method(owner, bare_name)
        if inherited is not None:
            return [inherited], owner
        if receiver in _SELF_RECEIVERS or receiver in _SUPER_RECEIVERS:
            # A trait, or a parent the graph cannot name, may hold it; leave
            # the call to the usual evidence, as before.
            return None
        return [], None

    @staticmethod
    def _own_method_excluded(
        edge_extra: dict,
        bare_name: str,
        caller_class: str,
        caller_language: str,
        inheritance: InheritanceIndex,
    ) -> Optional[str]:
        """The caller's own method that a call on another object cannot mean.

        ``h.equals(x)`` inside ``Widget.equals`` calls ``equals`` on whatever
        ``h`` is. With nothing known about ``h``, sharing a file is no evidence
        that it is a ``Widget``, and choosing ``Widget.equals`` makes the method
        call itself.
        """
        receiver = edge_extra.get("receiver")
        if (
            not caller_class
            or not isinstance(receiver, str)
            or receiver in _SELF_RECEIVERS
            or receiver in _SUPER_RECEIVERS
            or caller_language not in _TYPED_RECEIVER_LANGUAGES
            or "receiver_binding" in edge_extra
            or "receiver_type" in edge_extra
        ):
            return None
        return inheritance.method_of(caller_class, bare_name)

    @classmethod
    def _receiver_evidence_admits_candidate(
        cls,
        edge_extra: dict,
        qualified_name: str,
        file_path: str,
        parent_name: str | None,
    ) -> bool:
        """Row-level form of :meth:`receiver_evidence_admits`."""
        if "receiver_binding" not in edge_extra:
            owner = _claimed_owner(edge_extra)
            if (
                owner is None
                and edge_extra.get("receiver_resolution") == "typed_receiver_unresolved"
                and isinstance(edge_extra.get("receiver_type"), str)
            ):
                # ``pet.getName()`` on a ``Pet`` the resolver could not place:
                # still only a ``Pet`` method by that name.
                owner = edge_extra["receiver_type"]
            if owner is None:
                return True
            # The call named a class: ``h.hashCode()`` on a ``Helper``, or
            # ``GlobBuilder::new()``. That class's method is missing, and a
            # same-named method of another class is no better a match.
            return _same_owner(parent_name, owner)
        return bool(cls._receiver_backed_candidates(
            [(qualified_name, file_path)],
            edge_extra,
            {qualified_name: parent_name or None},
        ))

    @classmethod
    def receiver_evidence_admits(
        cls, edge_extra: dict, node: GraphNode,
    ) -> bool:
        """Can this edge's receiver evidence mean *node*?

        The read path keeps a bare-name fallback: a CALLS edge whose target is
        the plain method name still answers ``callers_of`` for a node of that
        name. That fallback applies exactly the rule
        ``_resolve_bare_endpoints`` refuses to apply — ``some_dict.get(...)``
        writes the target ``get`` and says nothing whatever about
        ``ConnectionPool.get`` — so leaving the two paths disagreeing means
        every attribution the resolver honestly declined is handed back by the
        tool. Reuse the resolver's own test here, over the single candidate the
        caller is asking about.

        An edge carrying no ``receiver_binding`` at all is a plain
        ``foo(...)`` call with no receiver to consult. That is the case the
        fallback exists for, and it is admitted unchanged.
        """
        return cls._receiver_evidence_admits_candidate(
            edge_extra, node.qualified_name, node.file_path, node.parent_name,
        )

    def resolve_bare_call_targets(self) -> int:
        """Resolve bare CALLS targets backed by same-file or import evidence.

        After parsing, some CALLS edges have bare targets (no ``::`` separator)
        because the parser couldn't resolve cross-file. A globally unique name
        is not sufficient evidence: unrelated repositories often contain one
        matching helper by coincidence. The candidate must be in the call-site
        file or in exactly one file imported by that file.

        Returns the number of resolved edges.
        """
        # This pass and resolve_bare_tested_by_sources share one evidence
        # context. Fill it fresh here so a write that bypassed the store's
        # invalidation (a resolver using the raw connection) is never missed.
        self._drop_bare_endpoint_context()
        return self._resolve_bare_endpoints("CALLS", "target_qualified")

    # -- Target-resolution certainty --------------------------------------

    def refresh_target_resolution(self) -> int:
        """Settle every edge endpoint, then recompute what each edge claims.

        Run after every resolver pass: those passes rewrite bare targets into
        qualified ones, so a classification taken at insert time would be
        stale for exactly the edges the distinction is about. Set-based
        UPDATEs over the edge table, not a per-row query.

        Returns the number of rows whose classification changed; rows that
        are already right are not rewritten.
        """
        # The inherited walk first: it needs the name the parser wrote, and it
        # takes up edges an earlier pass settled.
        self._resolve_inherited_endpoints()
        self._settle_unbound_endpoints()
        placeholders = ", ".join("?" for _ in TARGET_RESOLUTION_KINDS)
        call_tier = _certainty_tier_sql(f"target_resolution = '{RESOLUTION_UNRESOLVED}'")
        tested_by_tier = _certainty_tier_sql(
            "NOT EXISTS (SELECT 1 FROM nodes crg_cert_n "
            "WHERE crg_cert_n.qualified_name = edges.source_qualified)"
        )
        self._begin_immediate()
        try:
            cursor = self._conn.execute(
                "UPDATE edges SET target_resolution = "  # noqa: S608
                f"{TARGET_RESOLUTION_EXPR} WHERE kind IN ({placeholders}) "
                f"AND target_resolution IS NOT ({TARGET_RESOLUTION_EXPR})",
                TARGET_RESOLUTION_KINDS,
            )
            # Only rows that change are written. The stored certainty is read by
            # nothing on the traversal paths, so this must not cost a rewrite of
            # the whole table on every incremental update.
            self._conn.execute(
                f"UPDATE edges SET confidence_tier = {call_tier} "  # noqa: S608
                f"WHERE kind IN ({placeholders}) AND confidence_tier IS NOT {call_tier}",
                TARGET_RESOLUTION_KINDS,
            )
            self._conn.execute(
                f"UPDATE edges SET confidence_tier = {tested_by_tier} "  # noqa: S608
                f"WHERE kind = 'TESTED_BY' AND confidence_tier IS NOT {tested_by_tier}",
            )
            self._conn.execute(
                f"UPDATE edges SET confidence = {_CERTAINTY_SCORE_SQL} "  # noqa: S608
                f"WHERE kind IN ({placeholders}, 'TESTED_BY') "
                f"AND confidence IS NOT {_CERTAINTY_SCORE_SQL}",
                TARGET_RESOLUTION_KINDS,
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        self._settle_collision_endpoints()
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    def _resolve_inherited_endpoints(self) -> int:
        """Move a call on a class that only inherits the method to the ancestor.

        ``this.handle()`` in ``Leaf`` is written ``<leaf>::Leaf.handle``, and
        ``pet.getName()`` on an imported ``Pet`` is ``<pet>::Pet.getName``. When
        the class does not declare the method, that names no node. If one
        ancestor surely declares it (see ``InheritanceIndex``), the endpoint
        moves there, and ``extra`` keeps the name the parser wrote. Every pass
        checks the move again from that name, so after an update that adds the
        method nearer, removes it, or leaves the answer unsure, the edge is
        where a fresh parse and this pass would put it. TESTED_BY mirrors take
        the same path, so ``tests_for`` on the ancestor finds the test.

        Runs before ``_settle_unbound_endpoints`` and also takes up edges that
        pass settled earlier. Returns the number of edges changed. See: #984
        """
        conn = self._conn
        work: list[tuple[str, sqlite3.Row, dict, str, str, str]] = []
        for kind, column in (
            ("CALLS", "target_qualified"), ("TESTED_BY", "source_qualified"),
        ):
            # Only a fixed column name reaches the SQL text.
            for row in conn.execute(
                f"SELECT e.id, e.{column} AS endpoint, e.extra FROM edges e "  # noqa: S608
                f"WHERE e.kind = ? AND (e.extra LIKE ? OR e.extra LIKE ? OR ("
                f"e.{column} LIKE '%::%' AND NOT EXISTS ("
                f"SELECT 1 FROM nodes n WHERE n.qualified_name = e.{column})))",
                (
                    kind,
                    f'%"{CLAIMED_ENDPOINT_KEY}"%',
                    f'%"{INHERITED_ENDPOINT_KEY}"%',
                ),
            ).fetchall():
                extra = _load_edge_extra(row["extra"])
                managed = extra.get(INHERITED_ENDPOINT_KEY)
                claimed = extra.get(CLAIMED_ENDPOINT_KEY)
                if isinstance(managed, str):
                    written = managed
                elif any(key in extra for key in _RESOLVER_BOUND_KEYS) or (
                    "ambiguous_targets" in extra or "unresolved_targets" in extra
                ):
                    # Another resolver owns this endpoint.
                    continue
                elif isinstance(claimed, str):
                    written = claimed
                else:
                    written = row["endpoint"]
                if not _is_path_qualified(written):
                    continue
                file_part, _, symbol = written.partition("::")
                class_part, dot, method = symbol.rpartition(".")
                if not dot or not class_part or not method:
                    continue
                work.append((
                    column, row, extra, written, f"{file_part}::{class_part}", method,
                ))
        if not work:
            return 0

        # Most settled edges name a class with no parent; then there is nothing
        # to walk, and the full index need not be read.
        with_parents = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT source_qualified FROM edges WHERE kind = 'INHERITS'"
            )
        }
        work = [
            item for item in work
            if item[4] in with_parents or INHERITED_ENDPOINT_KEY in item[2]
        ]
        if not work:
            return 0
        import_targets, _, raw_imports = self._import_evidence()
        inheritance = InheritanceIndex(conn, import_targets, raw_imports)

        moves: dict[str, list[tuple[str, str, int]]] = {}
        reverts: dict[str, list[tuple[str, str, str, float, int]]] = {}
        for column, row, extra, written, owner, method in work:
            target: Optional[str] = None
            if (
                inheritance.is_class(owner)
                and inheritance.method_of(owner, method) is None
            ):
                target = inheritance.inherited_method(owner, method)
            if target is not None:
                desired = dict(extra)
                desired[INHERITED_ENDPOINT_KEY] = written
                desired.pop(CLAIMED_ENDPOINT_KEY, None)
                if row["endpoint"] != target or desired != extra:
                    moves.setdefault(column, []).append(
                        (target, json.dumps(desired), row["id"]),
                    )
            elif INHERITED_ENDPOINT_KEY in extra:
                # The move no longer holds: put back what a fresh parse writes.
                # The settle pass then treats it as it treats any other edge.
                restored = dict(extra)
                del restored[INHERITED_ENDPOINT_KEY]
                reverts.setdefault(column, []).append((
                    written,
                    json.dumps(restored),
                    str(restored.get("confidence_tier", TIER_EXTRACTED)),
                    float(restored.get("confidence", _SCORE_EXTRACTED)),
                    row["id"],
                ))

        changed = sum(len(v) for v in moves.values()) + sum(
            len(v) for v in reverts.values()
        )
        if not changed:
            return 0
        self._begin_immediate()
        try:
            for column, updates in moves.items():
                move_sql = f"UPDATE edges SET {column} = ?, extra = ? WHERE id = ?"  # noqa: S608
                for start in range(0, len(updates), _UPDATE_BATCH):
                    conn.executemany(move_sql, updates[start:start + _UPDATE_BATCH])
            for column, restores in reverts.items():
                revert_sql = (
                    f"UPDATE edges SET {column} = ?, extra = ?, "  # noqa: S608
                    "confidence_tier = ?, confidence = ? WHERE id = ?"
                )
                for start in range(0, len(restores), _UPDATE_BATCH):
                    conn.executemany(revert_sql, restores[start:start + _UPDATE_BATCH])
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return changed

    def _settle_unbound_endpoints(self) -> int:
        """Stop an edge naming a qualified symbol the graph does not have.

        The parser sees one file. It qualifies a call whenever it can tell the
        file, so ``h.hashCode()`` on a ``Helper`` declared in the same file is
        written ``One.java::Helper.hashCode`` whether or not ``Helper`` has that
        method, and ``self.shared()`` names the enclosing class when ``shared``
        is inherited. A path in front of a name passes every check a real name
        does, and the bare-name path skips it, so it reads as fact while pointing
        at nothing. See: #984

        Such an endpoint goes back to the plain name the source wrote. The
        qualified form stays in ``extra``, because the file that adds the method
        may be the only one parsed next: the edge returns to it when the node
        appears, and is written exactly as a fresh parse writes it, so an
        incremental update and a rebuild agree. A receiver was on the call, so
        the plain name is not evidence for a same-named symbol nearby, and
        ``_resolve_bare_endpoints`` leaves these edges alone.

        Returns the number of edges changed.
        """
        conn = self._conn
        changes: dict[str, list[tuple[str, str, int]]] = {}
        for kind, column, managed_key in _NODE_ENDPOINTS:
            updates = changes.setdefault(column, [])
            # Only a fixed column name reaches the SQL text.
            for row in conn.execute(
                f"SELECT e.id, e.{column} AS endpoint, e.extra FROM edges e "  # noqa: S608
                f"WHERE e.kind = ? AND e.{column} LIKE '%::%' AND NOT EXISTS ("
                f"SELECT 1 FROM nodes n WHERE n.qualified_name = e.{column})",
                (kind,),
            ).fetchall():
                endpoint = row["endpoint"]
                if not _is_path_qualified(endpoint):
                    continue
                extra = _load_edge_extra(row["extra"])
                managed = extra.get(managed_key)
                if isinstance(managed, str) and managed:
                    # The bare resolver owns this edge and will try it again.
                    updates.append((managed, json.dumps(extra), row["id"]))
                    continue
                plain = _plain_symbol_name(endpoint)
                if not plain:
                    continue
                extra[CLAIMED_ENDPOINT_KEY] = endpoint
                updates.append((plain, json.dumps(extra), row["id"]))
            for row in conn.execute(
                f"SELECT e.id, e.{column} AS endpoint, e.extra FROM edges e "  # noqa: S608
                "WHERE e.kind = ? AND e.extra LIKE ?",
                (kind, f'%"{CLAIMED_ENDPOINT_KEY}"%'),
            ).fetchall():
                extra = _load_edge_extra(row["extra"])
                claimed = extra.get(CLAIMED_ENDPOINT_KEY)
                if not isinstance(claimed, str):
                    continue
                if self._node_exists(claimed):
                    bound = claimed
                elif self._node_exists(row["endpoint"]):
                    # Something else bound the edge since; the claim is spent.
                    bound = row["endpoint"]
                else:
                    continue
                del extra[CLAIMED_ENDPOINT_KEY]
                updates.append((bound, json.dumps(extra), row["id"]))

        changed = sum(len(updates) for updates in changes.values())
        if not changed:
            return 0
        self._begin_immediate()
        try:
            for column, updates in changes.items():
                update_sql = f"UPDATE edges SET {column} = ?, extra = ? WHERE id = ?"  # noqa: S608
                for start in range(0, len(updates), _UPDATE_BATCH):
                    conn.executemany(update_sql, updates[start:start + _UPDATE_BATCH])
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return changed

    def _existing_qualified_names(self, names: Iterable[str]) -> set[str]:
        """The members of *names* that are a node's qualified name."""
        wanted = sorted(set(names))
        found: set[str] = set()
        for start in range(0, len(wanted), 450):
            batch = wanted[start:start + 450]
            placeholders = ",".join("?" for _ in batch)
            found.update(
                row[0] for row in self._conn.execute(  # nosec B608
                    f"SELECT qualified_name FROM nodes WHERE qualified_name IN ({placeholders})",
                    batch,
                )
            )
        return found

    def _node_exists(self, qualified_name: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM nodes WHERE qualified_name = ? LIMIT 1", (qualified_name,),
        ).fetchone() is not None

    def count_edges_by_resolution(self, kind: str = "CALLS") -> dict[str, int]:
        """Split one edge kind into certain and guessed target attributions.

        The first query is an index-only group-by over
        ``idx_edges_kind_target_resolution``; that is what the stored column
        is for. Only rows the column has not classified yet -- a store written
        directly by a test, or a build that skipped post-processing -- pay for
        re-deriving the answer, so the count is never silently short and a
        refreshed graph never pays for a scan.
        """
        split = {"direct": 0, "unresolved": 0}
        unclassified = 0
        for resolution, count in self._conn.execute(
            "SELECT target_resolution, COUNT(*) FROM edges "
            "WHERE kind = ? GROUP BY target_resolution",
            (kind,),
        ):
            if resolution in split:
                split[resolution] = count
            elif resolution is None:
                unclassified = count
        if unclassified:
            for resolution, count in self._conn.execute(
                f"SELECT {TARGET_RESOLUTION_EXPR} AS resolution, "  # noqa: S608
                "COUNT(*) FROM edges WHERE kind = ? "
                "AND target_resolution IS NULL GROUP BY resolution",
                (kind,),
            ):
                if resolution in split:
                    split[resolution] += count
        return split

    def resolve_cpp_scoped_call_targets(self) -> int:
        """Resolve cross-file C++ ``Scope::call`` targets by stable scope identity.

        An explicit C++ scope is stronger evidence than a globally unique bare
        name. Resolve it when exactly one signature-bearing node matches; keep
        overload sets explicit and bounded when more than one node matches.
        """
        rows = self._conn.execute(
            "SELECT e.id, e.source_qualified, e.target_qualified, e.file_path, "
            "e.line, e.extra, s.parent_name, t.id target_id "
            "FROM edges e "
            "JOIN nodes s ON s.qualified_name = e.source_qualified "
            "LEFT JOIN nodes t ON t.qualified_name = e.target_qualified "
            "WHERE e.kind = 'CALLS' AND s.language = 'cpp' "
            "AND ((t.id IS NULL AND e.target_qualified LIKE '%::%') "
            "OR e.extra LIKE '%\"cpp_scoped_target\"%')",
        ).fetchall()
        if not rows:
            return 0

        candidates_by_name: dict[str, list[sqlite3.Row]] = {}
        for candidate in self._conn.execute(
            "SELECT name, qualified_name, parent_name FROM nodes "
            "WHERE language = 'cpp' AND kind IN ('Function', 'Test') "
            "ORDER BY qualified_name",
        ).fetchall():
            candidates_by_name.setdefault(candidate["name"], []).append(candidate)

        resolved = 0
        # Collect every mutation and apply them in one transaction at the end
        # (see below); the previous loop autocommitted per row (issue #721).
        call_updates: list[tuple[str, str, int]] = []
        mirror_updates: list[tuple[str, str, int]] = []

        def sync_tested_by(
            call_edge: sqlite3.Row,
            original_target: str,
            source_qualified: str,
            desired_extra: dict,
            serialized_extra: str,
        ) -> None:
            """Keep parser-generated TESTED_BY mirrors aligned with CALLS."""
            mirrors = self._conn.execute(
                "SELECT id, source_qualified, extra FROM edges "
                "WHERE kind = 'TESTED_BY' AND target_qualified = ? "
                "AND file_path = ? AND line = ?",
                (
                    call_edge["source_qualified"],
                    call_edge["file_path"],
                    call_edge["line"],
                ),
            ).fetchall()
            for mirror in mirrors:
                try:
                    mirror_extra = json.loads(mirror["extra"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    mirror_extra = {}
                mirror_original = (
                    mirror_extra.get("cpp_scoped_target")
                    if isinstance(mirror_extra, dict)
                    else None
                )
                if (
                    mirror["source_qualified"]
                    not in (call_edge["target_qualified"], original_target)
                    and mirror_original != original_target
                ):
                    continue
                if (
                    mirror["source_qualified"] == source_qualified
                    and mirror_extra == desired_extra
                ):
                    continue
                mirror_updates.append(
                    (source_qualified, serialized_extra, mirror["id"]),
                )

        for edge in rows:
            try:
                extra = json.loads(edge["extra"] or "{}")
            except (TypeError, json.JSONDecodeError):
                extra = {}
            if not isinstance(extra, dict):
                extra = {}
            previous_extra = dict(extra)

            original_target = extra.get("cpp_scoped_target")
            target = (
                original_target
                if isinstance(original_target, str)
                else edge["target_qualified"]
            )
            global_scope = target.startswith("::")
            normalized = target.lstrip(":")
            if "::" not in normalized:
                continue
            explicit_scope, bare_name = normalized.rsplit("::", 1)
            explicit_scope = explicit_scope.replace("::", ".")

            preferred_scopes: list[str] = []
            source_scope = edge["parent_name"]
            if not global_scope:
                while source_scope:
                    preferred_scopes.append(f"{source_scope}.{explicit_scope}")
                    source_scope = (
                        source_scope.rsplit(".", 1)[0]
                        if "." in source_scope
                        else None
                    )
            preferred_scopes.append(explicit_scope)

            candidates: list[str] = []
            for preferred_scope in preferred_scopes:
                candidates = [
                    candidate["qualified_name"]
                    for candidate in candidates_by_name.get(bare_name, [])
                    if candidate["parent_name"] == preferred_scope
                ]
                if candidates:
                    break

            if len(candidates) == 1:
                extra["cpp_scoped_target"] = target
                for key in (
                    "ambiguous_targets",
                    "ambiguous_target_count",
                    "ambiguous_targets_truncated",
                    "unresolved_targets",
                    "unresolved_target_count",
                    "unresolved_targets_truncated",
                ):
                    extra.pop(key, None)
                serialized_extra = json.dumps(extra, sort_keys=True)
                if (
                    edge["target_qualified"] != candidates[0]
                    or previous_extra != extra
                ):
                    call_updates.append(
                        (candidates[0], serialized_extra, edge["id"]),
                    )
                    resolved += 1
                sync_tested_by(
                    edge, target, candidates[0], extra, serialized_extra,
                )
            elif len(candidates) > 1:
                for key in (
                    "unresolved_targets",
                    "unresolved_target_count",
                    "unresolved_targets_truncated",
                ):
                    extra.pop(key, None)
                extra.update({
                    "cpp_scoped_target": target,
                    "ambiguous_targets": candidates[:20],
                    "ambiguous_target_count": len(candidates),
                    "ambiguous_targets_truncated": len(candidates) > 20,
                })
                serialized_extra = json.dumps(extra, sort_keys=True)
                if edge["target_qualified"] != target or previous_extra != extra:
                    call_updates.append((target, serialized_extra, edge["id"]))
                sync_tested_by(
                    edge, target, target, extra, serialized_extra,
                )
            else:
                extra["cpp_scoped_target"] = target
                for key in (
                    "ambiguous_targets",
                    "ambiguous_target_count",
                    "ambiguous_targets_truncated",
                ):
                    extra.pop(key, None)
                extra.update({
                    "unresolved_targets": [],
                    "unresolved_target_count": 0,
                    "unresolved_targets_truncated": False,
                })
                serialized_extra = json.dumps(extra, sort_keys=True)
                if edge["target_qualified"] != target or previous_extra != extra:
                    call_updates.append((target, serialized_extra, edge["id"]))
                sync_tested_by(
                    edge, target, target, extra, serialized_extra,
                )

        if call_updates or mirror_updates:
            # Same batching rationale as _resolve_bare_endpoints: one prepared
            # statement, one transaction, one commit/checkpoint instead of one
            # autocommitted UPDATE (+ fsync) per edge (issue #721).
            self._begin_immediate()
            try:
                call_sql = (
                    "UPDATE edges SET target_qualified = ?, extra = ? WHERE id = ?"
                )
                for start in range(0, len(call_updates), _UPDATE_BATCH):
                    self._conn.executemany(
                        call_sql, call_updates[start:start + _UPDATE_BATCH]
                    )
                mirror_sql = (
                    "UPDATE edges SET source_qualified = ?, extra = ? WHERE id = ?"
                )
                for start in range(0, len(mirror_updates), _UPDATE_BATCH):
                    self._conn.executemany(
                        mirror_sql, mirror_updates[start:start + _UPDATE_BATCH]
                    )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        return resolved

    def resolve_bare_tested_by_sources(self) -> int:
        """Resolve bare TESTED_BY sources backed by graph evidence.

        TESTED_BY edges copy the target of a test's CALLS edge, so unresolved
        cross-file calls also leave a bare production source. The test call-site
        file must import the candidate file (or contain the candidate itself)
        before this method qualifies that source.

        Returns the number of resolved edges.
        """
        try:
            return self._resolve_bare_endpoints("TESTED_BY", "source_qualified")
        finally:
            # The pair is done: do not keep a copy of every node name alive
            # for the rest of the store's life (watch mode keeps one open).
            self._drop_bare_endpoint_context()

    def _drop_bare_endpoint_context(self) -> None:
        with self._cache_lock:
            self._cache_epoch += 1
            self._bare_endpoint_context_cache = None

    @staticmethod
    def _directory_member_files(
        conn: sqlite3.Connection, scoped_dirs: dict[str, str],
    ) -> dict[str, set[str]]:
        """Member files of each directory-scoped import target.

        One pass over the File nodes, walking each file's ancestors against
        the directories that are actually import targets. A ``package``
        directory owns only the files directly in it; a ``tree`` directory
        owns every file below it.
        """
        members: dict[str, set[str]] = {}
        for row in conn.execute(
            "SELECT file_path FROM nodes WHERE kind = 'File'"
        ):
            file_path = row["file_path"]
            for directory, scopes in import_scope_ancestors(file_path):
                if scoped_dirs.get(directory) in scopes:
                    members.setdefault(directory, set()).add(file_path)
        return members

    def _import_evidence(
        self,
    ) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, dict[str, str]]]:
        """What each file imports: (files, ambiguous candidates, raw names).

        One pass over the IMPORTS_FROM edges, with the expansions every
        resolver needs: C# namespaces, Go packages and Python dotted modules
        mapped back to the files that declare them.
        """
        conn = self._conn
        # call-site file -> explicitly imported files
        import_targets: dict[str, set[str]] = {}
        # Python's repository-suffix resolver keeps a raw import when multiple
        # indexed modules match and records the candidate files in edge metadata.
        # Carry that evidence into bare CALLS / TESTED_BY endpoints so a graph
        # first built in the ambiguous state cannot fall back to a name-only
        # caller match for every candidate.
        ambiguous_import_targets: dict[str, set[str]] = {}
        # Directory-scoped targets: a Go import names a package, not a file.
        # The edge stays one row; the member files are expanded here, once
        # per post-processing pass, rather than stored one edge per member.
        scoped_dirs: dict[str, str] = {}
        # call-site file -> {short name: full name} for imports that name no
        # file: ``java.util.Date``, a Java class under another source root, a
        # PHP ``use`` the file resolver could not place.
        raw_imports: dict[str, dict[str, str]] = {}
        for row in conn.execute(
            "SELECT DISTINCT file_path, target_qualified, extra FROM edges "
            "WHERE kind = 'IMPORTS_FROM'"
        ).fetchall():
            target = row["target_qualified"]
            target_file = target.split("::", 1)[0] if "::" in target else target
            import_targets.setdefault(row["file_path"], set()).add(target_file)
            if not (
                PurePosixPath(target_file).is_absolute()
                or PureWindowsPath(target_file).is_absolute()
            ):
                short = target_file.rpartition("\\")[2].rpartition(".")[2]
                if short:
                    raw_imports.setdefault(row["file_path"], {})[short] = target_file
            raw_extra = row["extra"]
            if not raw_extra or raw_extra == "{}":
                continue
            scope = _edge_import_scope(raw_extra)
            if scope is not None:
                scoped_dirs[target] = scope
            if "ambiguous" in raw_extra:
                try:
                    import_extra = json.loads(raw_extra)
                except (TypeError, json.JSONDecodeError):
                    import_extra = {}
                if (
                    isinstance(import_extra, dict)
                    and import_extra.get("import_resolution") == "ambiguous"
                    and isinstance(import_extra.get("import_candidates"), list)
                ):
                    ambiguous_import_targets.setdefault(
                        row["file_path"], set(),
                    ).update(
                        candidate
                        for candidate in import_extra["import_candidates"]
                        if isinstance(candidate, str)
                    )

        # C# `using X.Y;` directives store IMPORTS_FROM targets as raw
        # namespace strings rather than file paths, so the path-keyed
        # evidence above never matches a candidate's defining file. Map
        # declared namespaces back to the files declaring them and add
        # those files as import evidence. See: #310, #792
        namespace_files: dict[str, set[str]] = {}
        for row in conn.execute(
            "SELECT file_path, extra FROM nodes "
            "WHERE kind = 'File' AND extra LIKE '%\"csharp_namespaces\"%'"
        ).fetchall():
            try:
                node_extra = json.loads(row["extra"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(node_extra, dict):
                continue
            declared = node_extra.get("csharp_namespaces")
            if not isinstance(declared, list):
                continue
            for ns in declared:
                if isinstance(ns, str) and ns:
                    namespace_files.setdefault(ns, set()).add(
                        row["file_path"],
                    )
        if namespace_files:
            for imported in import_targets.values():
                expanded: set[str] = set()
                for target in imported:
                    expanded |= namespace_files.get(target, set())
                imported |= expanded

        if scoped_dirs:
            members = self._directory_member_files(conn, scoped_dirs)
            for imported in import_targets.values():
                grown: set[str] = set()
                for target in imported:
                    grown |= members.get(target, set())
                imported |= grown

        # Python imports the repository-suffix resolver could not map to a file
        # keep their raw dotted module as the IMPORTS_FROM target — the standard
        # `src` layout, or framework-mediated packages such as Odoo's
        # `odoo.addons.*`. Path-keyed evidence can never match a dotted module,
        # so map each dotted module back to the file that could define it. Only
        # an unambiguous match counts: a module answered by two indexed files is
        # no more evidence than a bare name. See: #903
        module_files = self._python_module_file_index(conn)
        if module_files:
            for imported in import_targets.values():
                expanded = set()
                for target in imported:
                    resolved_file = module_files.get(target)
                    if resolved_file:
                        expanded.add(resolved_file)
                imported |= expanded

        return import_targets, ambiguous_import_targets, raw_imports

    def _get_bare_endpoint_context(
        self, conn: sqlite3.Connection
    ) -> tuple[
        dict[str, list[tuple[str, str]]],
        dict[str, str | None],
        dict[str, set[str]],
        dict[str, set[str]],
        set[str],
        dict[str, str],
        dict[str, dict[str, str]],
    ]:
        """The node lookups and import evidence bare resolution reads, cached.

        (names, owning classes, imported files, ambiguous imports, Ruby nodes,
        function languages, raw import names). A write drops the cache.
        """
        cached = getattr(self, "_bare_endpoint_context_cache", None)
        if cached is not None:
            return cached
        epoch = self._cache_epoch

        # bare_name -> [(qualified_name, defining_file)]
        node_lookup: dict[str, list[tuple[str, str]]] = {}
        # qualified_name -> owning class, so a receiver known to be an
        # instance of one class cannot be answered by another class's method.
        parent_lookup: dict[str, str | None] = {}
        # A Ruby constant can only name a Ruby class.
        ruby_nodes: set[str] = set()
        # function -> its language, to read what a receiver-less call means
        language_lookup: dict[str, str] = {}
        for row in conn.execute(
            "SELECT name, qualified_name, file_path, parent_name, kind, language "
            "FROM nodes WHERE kind IN ('Function', 'Test', 'Class')"
        ).fetchall():
            node_lookup.setdefault(row["name"], []).append(
                (row["qualified_name"], row["file_path"]),
            )
            parent_lookup[row["qualified_name"]] = row["parent_name"]
            if row["language"] == "ruby":
                ruby_nodes.add(row["qualified_name"])
            if row["kind"] != "Class":
                language_lookup[row["qualified_name"]] = row["language"] or ""

        import_targets, ambiguous_import_targets, raw_imports = self._import_evidence()
        result = (
            node_lookup,
            parent_lookup,
            import_targets,
            ambiguous_import_targets,
            ruby_nodes,
            language_lookup,
            raw_imports,
        )
        with self._cache_lock:
            # Publish only if no write invalidated the context meanwhile.
            if self._cache_epoch == epoch:
                self._bare_endpoint_context_cache = result
        return result

    def _resolve_bare_endpoints(self, kind: str, endpoint: str) -> int:
        """Resolve a bare edge endpoint only when one candidate has evidence."""
        if endpoint == "target_qualified":
            raw_key = "bare_call_target"
            endpoint_column = "target_qualified"
            bare_condition = (
                "target_qualified NOT LIKE '%::%' "
                "OR extra LIKE '%\"bare_call_target\"%'"
            )
            select_sql = (
                "SELECT id, source_qualified, target_qualified, file_path, extra "
                "FROM edges WHERE kind = ? AND (" + bare_condition + ")"
            )
        elif endpoint == "source_qualified":
            raw_key = "bare_tested_by_source"
            endpoint_column = "source_qualified"
            bare_condition = (
                "source_qualified NOT LIKE '%::%' "
                "OR extra LIKE '%\"bare_tested_by_source\"%'"
            )
            select_sql = (
                "SELECT id, source_qualified, target_qualified, file_path, extra "
                "FROM edges WHERE kind = ? AND (" + bare_condition + ")"
            )
        else:
            raise ValueError(f"Invalid edge endpoint column: {endpoint!r}")

        conn = self._conn

        # Cheap no-op guard: avoid materialising the full candidate set when
        # nothing needs resolving. EXISTS stops at the first matching row, so
        # the common case exits immediately while the empty case pays a single
        # scan instead of materialising an empty list (issue #721).
        has_bare = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM edges WHERE kind = ? AND ("
            + bare_condition
            + "))",
            (kind,),
        ).fetchone()[0]
        if not has_bare:
            return 0

        (
            node_lookup,
            parent_lookup,
            import_targets,
            ambiguous_import_targets,
            ruby_nodes,
            language_lookup,
            raw_imports,
        ) = self._get_bare_endpoint_context(conn)

        # Stream candidate rows lazily instead of materialising the full set
        # into memory (millions of rows on large graphs, issue #721).
        bare_edges = conn.execute(select_sql, (kind,))
        inheritance = InheritanceIndex(conn, import_targets, raw_imports)
        # The function making the call: the edge's source for CALLS, and its
        # target for TESTED_BY, whose source is the callee.
        caller_column = (
            "source_qualified" if endpoint == "target_qualified" else "target_qualified"
        )

        resolved = 0
        # Collect every mutation, then apply them in one transaction at the
        # end of the loop (see below).
        updates: list[tuple[str, str, int]] = []
        for edge in bare_edges:
            raw_extra = edge["extra"]
            if not raw_extra or raw_extra == "{}":
                bare_name = edge[endpoint]
                if not isinstance(bare_name, str):
                    continue
                candidates = node_lookup.get(bare_name)
                if not candidates:
                    continue
                edge_extra = {}
                receiver_constant = False
            else:
                try:
                    edge_extra = json.loads(raw_extra)
                except (TypeError, json.JSONDecodeError):
                    edge_extra = {}
                if not isinstance(edge_extra, dict):
                    edge_extra = {}
                if raw_key not in edge_extra and (
                    "ambiguous_targets" in edge_extra
                    or "unresolved_targets" in edge_extra
                    or CLAIMED_ENDPOINT_KEY in edge_extra
                ):
                    continue
                if INHERITED_ENDPOINT_KEY in edge_extra:
                    # ``_resolve_inherited_endpoints`` owns this edge.
                    continue
                bare_name = edge_extra.get(raw_key, edge[endpoint])
                if not isinstance(bare_name, str):
                    continue
                # ``Shop::Cart.new`` runs ``Shop::Cart#initialize``.
                receiver_constant = edge_extra.get("receiver_constant") is True
                lookup_name = (
                    "initialize" if receiver_constant and bare_name == "new" else bare_name
                )
                candidates = node_lookup.get(lookup_name, [])
                if receiver_constant:
                    candidates = [
                        candidate for candidate in candidates if candidate[0] in ruby_nodes
                    ]
                if "receiver_binding" in edge_extra:
                    candidates = self._receiver_backed_candidates(
                        candidates, edge_extra, parent_lookup,
                    )

            context_file = edge["file_path"]
            caller = edge[caller_column]
            caller_language = language_lookup.get(caller, "")
            caller_class = caller.rpartition(".")[0] if parent_lookup.get(caller) else ""
            if not inheritance.is_class(caller_class):
                caller_class = ""
            class_backed = self._class_backed_candidates(
                edge_extra, bare_name, caller_class, caller_language, context_file,
                inheritance,
            )
            if class_backed is not None:
                # The receiver names a class, so only that class or the
                # ancestor it inherits the method from can answer the call.
                supported, inherited_through = class_backed
                candidates = [(qualified, "") for qualified in supported]
                ambiguity_supported: list[str] = []
            else:
                inherited_through = None
                imported_files = import_targets.get(context_file, set())
                # A module receiver names its file outright; when that module
                # only re-exports the name, the parser stamps the defining
                # file, which the call site never imports itself.
                receiver_file = (
                    edge_extra.get("receiver_module")
                    if edge_extra.get("receiver_binding") == "module"
                    else None
                )
                receiver_class_file = edge_extra.get("receiver_class_file")
                java_package = (
                    inheritance.java_package(context_file)
                    if caller_language == "java" else None
                )
                supported = [
                    qualified
                    for qualified, candidate_file in candidates
                    if candidate_file == context_file
                    or candidate_file in imported_files
                    or candidate_file == receiver_file
                    or candidate_file == receiver_class_file
                    # Java needs no import for a class in the same package.
                    # That says which class ``new Helper()`` builds, not which
                    # method a plain ``ping()`` calls.
                    or (
                        java_package is not None
                        and inheritance.is_class(qualified)
                        and inheritance.java_package(candidate_file) == java_package
                    )
                    # A Ruby constant path names its class wherever it is
                    # defined, and the receiver check above already held the
                    # candidate to it.
                    or receiver_constant
                ]
                own_method = self._own_method_excluded(
                    edge_extra, bare_name, caller_class, caller_language,
                    inheritance,
                )
                if own_method is not None and supported == [own_method]:
                    # Not a match on its own. Next to other candidates it
                    # still makes the name ambiguous, so it stays there:
                    # dropping it would crown the other one on no evidence.
                    supported = []
                ambiguous_files = ambiguous_import_targets.get(context_file, set())
                ambiguity_supported = [
                    qualified
                    for qualified, candidate_file in candidates
                    if candidate_file in ambiguous_files
                ]
            managed = raw_key in edge_extra
            if (
                len(supported) != 1
                and not managed
                and len(supported) < 2
                and not ambiguity_supported
            ):
                continue
            desired_extra = dict(edge_extra)
            desired_extra[raw_key] = bare_name
            if inherited_through and len(supported) == 1:
                desired_extra[INHERITED_THROUGH_KEY] = inherited_through
            else:
                desired_extra.pop(INHERITED_THROUGH_KEY, None)
            if len(supported) == 1:
                desired_endpoint = supported[0]
                for key in (
                    "ambiguous_targets",
                    "ambiguous_target_count",
                    "ambiguous_targets_truncated",
                    "unresolved_targets",
                    "unresolved_target_count",
                    "unresolved_targets_truncated",
                ):
                    desired_extra.pop(key, None)
            else:
                desired_endpoint = bare_name
                if len(supported) > 1:
                    resolution = "ambiguous"
                    resolution_candidates = supported
                    other = "unresolved"
                elif ambiguity_supported:
                    resolution = (
                        "ambiguous"
                        if len(ambiguity_supported) > 1
                        else "unresolved"
                    )
                    resolution_candidates = ambiguity_supported
                    other = (
                        "unresolved"
                        if resolution == "ambiguous"
                        else "ambiguous"
                    )
                else:
                    resolution = "unresolved"
                    resolution_candidates = [
                        qualified for qualified, _ in candidates
                    ]
                    other = "ambiguous"
                for key in (
                    f"{other}_targets",
                    f"{other}_target_count",
                    f"{other}_targets_truncated",
                ):
                    desired_extra.pop(key, None)
                desired_extra.update({
                    f"{resolution}_targets": resolution_candidates[:20],
                    f"{resolution}_target_count": len(resolution_candidates),
                    f"{resolution}_targets_truncated": (
                        len(resolution_candidates) > 20
                    ),
                })

            if (
                edge[endpoint] == desired_endpoint
                and edge_extra == desired_extra
            ):
                continue
            serialized_extra = json.dumps(desired_extra, sort_keys=True)
            updates.append((desired_endpoint, serialized_extra, edge["id"]))
            if len(supported) == 1 and edge[endpoint] != desired_endpoint:
                resolved += 1

        bare_edges.close()

        if updates:
            # Apply every mutation as one prepared statement inside a single
            # transaction. The previous code relied on autocommit
            # (isolation_level=None), so each row became its own WAL commit +
            # fsync — effectively a hang once a graph has 10^5+ bare edges
            # (issue #721). Chunked executemany keeps peak memory bounded
            # while preserving a single commit/checkpoint.
            self._begin_immediate()
            try:
                update_sql = (
                    f"UPDATE edges SET {endpoint_column} = ?, extra = ? WHERE id = ?"
                )
                for start in range(0, len(updates), _UPDATE_BATCH):
                    conn.executemany(update_sql, updates[start:start + _UPDATE_BATCH])
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        if resolved:
            endpoint_label = (
                "sources" if endpoint == "source_qualified" else "targets"
            )
            logger.info(
                "Resolved %d evidence-backed bare %s %s",
                resolved,
                kind,
                endpoint_label,
            )
        return resolved

    def get_all_files(self) -> list[str]:
        # A partially failed file update can leave Function/Class nodes or
        # edges behind after their File marker is gone. Reconciliation must see
        # every represented path so those orphans can be purged, while explicit
        # virtual nodes (such as Spring Event markers) remain outside the file
        # inventory and are managed by their owning resolver.
        # A killed writer can leave extra='' (or truncated text), which makes
        # json_extract raise SQLite 'malformed JSON' and crash every caller of
        # get_all_files. json_valid gates the extraction, and the truthiness
        # comparison keeps extra='{"virtual": false}' visible instead of
        # making the node immortal to reconciliation (#864).
        rows = self._conn.execute(
            "SELECT file_path FROM nodes "
            "WHERE extra IS NULL "
            "OR json_valid(extra) = 0 "
            "OR COALESCE(json_extract(extra, '$.virtual'), 0) != 1 "
            "UNION "
            "SELECT file_path FROM edges "
            "ORDER BY file_path"
        ).fetchall()
        return [r["file_path"] for r in rows]

    def get_file_hashes(self) -> dict[str, str]:
        """Return indexed file paths mapped to the hash last stored for each file."""
        rows = self._conn.execute(
            "SELECT file_path, file_hash FROM nodes WHERE kind = 'File'"
        ).fetchall()
        return {
            normalize_file_path(row["file_path"]): row["file_hash"] or ""
            for row in rows
        }

    def get_file_marker_paths(self) -> list[str]:
        """Return paths that have authoritative File nodes.

        Reconciliation uses this to distinguish a graph anchored to another
        repository from orphan Function/Edge rows left by a failed update.
        """
        rows = self._conn.execute(
            "SELECT file_path FROM nodes WHERE kind = 'File' ORDER BY file_path"
        ).fetchall()
        return [row["file_path"] for row in rows]

    def search_nodes_by_qualified_tail(
        self, tail: str, limit: int = 20,
    ) -> list[GraphNode]:
        """Return nodes whose qualified symbol portion exactly matches *tail*.

        Both predicates are indexed equality tests (``idx_nodes_symbol`` and the
        ``qualified_name`` unique index), so this stays a bounded lookup rather
        than a scan of every node on large graphs.
        """
        rows = self._conn.execute(
            "SELECT * FROM nodes WHERE symbol = ? OR qualified_name = ? LIMIT ?",
            (tail, tail, limit),
        ).fetchall()
        return [self._row_to_node(row) for row in rows]

    def count_nodes_by_qualified_tail(self, tail: str) -> int:
        """Count nodes whose qualified symbol portion exactly matches *tail*."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS count FROM nodes "
            "WHERE symbol = ? OR qualified_name = ?",
            (tail, tail),
        ).fetchone()
        return int(row["count"])

    def search_nodes(self, query: str, limit: int = 20) -> list[GraphNode]:
        """Keyword search across node names.

        Tries FTS5 first (fast, tokenized matching), then falls back to
        LIKE-based substring search when FTS5 returns no results. Liquid
        ``Output`` nodes are left out: their names are the text a template
        prints, and on a theme they would crowd out every real match.
        """
        words = query.split()
        if not words:
            return []

        # Phase 1: FTS5 search (uses the indexed nodes_fts table)
        try:
            if len(words) == 1:
                fts_query = '"' + query.replace('"', '""') + '"'
            else:
                fts_query = " AND ".join(
                    '"' + w.replace('"', '""') + '"' for w in words
                )
            rows = self._conn.execute(
                "SELECT n.* FROM nodes_fts f "
                "JOIN nodes n ON f.rowid = n.id "
                "WHERE nodes_fts MATCH ? AND n.kind != 'Output' LIMIT ?",
                (fts_query, limit),
            ).fetchall()
            if rows:
                return [self._row_to_node(r) for r in rows]
        except Exception:  # nosec B110 - FTS5 table may not exist on older schemas
            pass

        # Phase 2: LIKE fallback (substring matching)
        conditions: list[str] = []
        params: list[str | int] = []
        for word in words:
            w = word.lower()
            conditions.append(
                "(LOWER(name) LIKE ? OR LOWER(qualified_name) LIKE ?)"
            )
            params.extend([f"%{w}%", f"%{w}%"])

        where = " AND ".join(conditions)
        sql = (  # nosec B608
            f"SELECT * FROM nodes WHERE {where} AND kind != 'Output' LIMIT ?"
        )
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_node(r) for r in rows]

    def count_search_nodes(self, query: str) -> int:
        """Count nodes using the same FTS-first semantics as ``search_nodes``."""
        words = query.split()
        if not words:
            return 0

        try:
            if len(words) == 1:
                fts_query = '"' + query.replace('"', '""') + '"'
            else:
                fts_query = " AND ".join(
                    '"' + word.replace('"', '""') + '"' for word in words
                )
            count = self._conn.execute(
                "SELECT COUNT(*) FROM nodes_fts f "
                "JOIN nodes n ON f.rowid = n.id "
                "WHERE nodes_fts MATCH ? AND n.kind != 'Output'",
                (fts_query,),
            ).fetchone()[0]
            if count:
                return int(count)
        except Exception:  # nosec B110 - FTS5 may not exist on older schemas
            pass

        conditions: list[str] = []
        params: list[str] = []
        for word in words:
            value = word.lower()
            conditions.append(
                "(LOWER(name) LIKE ? OR LOWER(qualified_name) LIKE ?)"
            )
            params.extend([f"%{value}%", f"%{value}%"])
        where = " AND ".join(conditions)
        sql = (  # nosec B608
            f"SELECT COUNT(*) FROM nodes WHERE {where} AND kind != 'Output'"
        )
        return int(self._conn.execute(sql, params).fetchone()[0])

    def count_nodes_by_name(
        self,
        name: str,
        language: str | None = None,
        kinds: tuple[str, ...] = (),
    ) -> int:
        """Count exact-name nodes with optional language and kind filters."""
        conditions = ["name = ?"]
        params: list[Any] = [name]
        if language is not None:
            conditions.append("language = ?")
            params.append(language)
        if kinds:
            placeholders = ", ".join("?" for _ in kinds)
            conditions.append(f"kind IN ({placeholders})")
            params.extend(kinds)
        where = " AND ".join(conditions)
        sql = f"SELECT COUNT(*) FROM nodes WHERE {where}"  # nosec B608
        return int(self._conn.execute(sql, params).fetchone()[0])

    def get_nodes_by_name(self, name: str) -> list[GraphNode]:
        """Return every node with exactly this name, by qualified name."""
        rows = self._conn.execute(
            "SELECT * FROM nodes WHERE name = ? ORDER BY qualified_name",
            (name,),
        ).fetchall()
        return [self._row_to_node(r) for r in rows]

    # --- Impact / Graph traversal ---

    def _impact_seed_qns(self, changed_files: list[str]) -> set[str]:
        """Seed qualified names for the impact traversal.

        Includes every node in the changed files plus, for changed C#
        files, the namespaces they declare. C# ``using X.Y;`` directives
        store IMPORTS_FROM targets as raw namespace strings rather than
        file paths, so without these bridge seeds the traversal can never
        reach importers of a changed .cs file. The namespace strings have
        no node rows, so they act purely as bridges and never surface in
        results. See: #310

        Tree-scoped import targets are bridged the same way. A Ruby
        ``require_all "jekyll/converters"`` names a DIRECTORY, and a file
        two levels below it is still one of the files it loads, so the
        directory is seeded for the changed file. The one-level case -- a Go
        package, which is exactly one directory -- is not seeded here: the
        traversal expands it at every hop, so it also works for a file
        reached indirectly rather than only for a changed one.
        """
        seeds: set[str] = set()
        files: list[str] = []
        for f in changed_files:
            for n in self.get_nodes_by_file(f):
                seeds.add(n.qualified_name)
                if n.kind == "File":
                    files.append(n.file_path)
                    if n.language == "csharp":
                        for ns in n.extra.get("csharp_namespaces") or []:
                            if isinstance(ns, str) and ns:
                                seeds.add(ns)
        seeds.update(self.tree_scope_bridges(files))
        return seeds

    def tree_scope_bridges(self, file_paths: Iterable[str]) -> set[str]:
        """Ancestor directories that a tree-scoped import target names.

        Only ancestors ABOVE each file's own directory: the own-directory
        case is handled by the traversal itself, which is what keeps it
        working at every hop instead of only for a seed.
        """
        wanted: dict[str, str] = {}
        for file_path in file_paths:
            for directory, scopes in import_scope_ancestors(file_path)[1:]:
                if IMPORT_SCOPE_TREE in scopes:
                    wanted[directory] = IMPORT_SCOPE_TREE
        if not wanted:
            return set()
        found: set[str] = set()
        directories = list(wanted)
        for start in range(0, len(directories), 450):
            batch = directories[start:start + 450]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(  # nosec B608
                "SELECT DISTINCT target_qualified, extra FROM edges "
                "WHERE kind = 'IMPORTS_FROM' "
                f"AND target_qualified IN ({placeholders})",
                batch,
            ).fetchall()
            for row in rows:
                if _edge_import_scope(row["extra"]) == IMPORT_SCOPE_TREE:
                    found.add(row["target_qualified"])
        return found

    def import_scope_edges(
        self, file_paths: Iterable[str], source_qns: set[str],
    ) -> list[GraphEdge]:
        """Directory-scoped IMPORTS_FROM edges that connect the given files.

        ``get_edges_among`` matches source and target against the same set of
        qualified names, and a directory target is in no such set, so these
        edges would otherwise be missing from the answer that rests on them.
        """
        wanted: dict[str, tuple[str, ...]] = {}
        for file_path in file_paths:
            for directory, scopes in import_scope_ancestors(file_path):
                wanted[directory] = tuple(
                    sorted(set(wanted.get(directory, ())) | set(scopes)),
                )
        if not wanted or not source_qns:
            return []
        out: list[GraphEdge] = []
        directories = list(wanted)
        for start in range(0, len(directories), 450):
            batch = directories[start:start + 450]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(  # nosec B608
                "SELECT * FROM edges WHERE kind = 'IMPORTS_FROM' "
                f"AND target_qualified IN ({placeholders})",
                batch,
            ).fetchall()
            for row in rows:
                edge = self._row_to_edge(row)
                if edge.source_qualified not in source_qns:
                    continue
                scope = edge.extra.get(IMPORT_SCOPE_KEY)
                if scope in wanted.get(edge.target_qualified, ()):
                    out.append(edge)
        return out

    def _import_scope_importer_index(self) -> dict[str, list[str]]:
        """Directory target -> the qualified names that import it.

        Only the NetworkX engine needs this materialised. The SQL engine
        seeks the same rows one directory at a time through
        ``idx_edges_target_kind``.
        """
        index: dict[str, list[str]] = {}
        rows = self._conn.execute(
            "SELECT source_qualified, target_qualified, extra FROM edges "
            "WHERE kind = 'IMPORTS_FROM' AND extra LIKE ?",
            (f"%{IMPORT_SCOPE_KEY}%",),
        ).fetchall()
        for row in rows:
            if _edge_import_scope(row["extra"]) is None:
                continue
            index.setdefault(row["target_qualified"], []).append(
                row["source_qualified"],
            )
        return index

    def _file_node_directories(self) -> dict[str, str]:
        """File node qualified name -> the directory that holds it."""
        out: dict[str, str] = {}
        for row in self._conn.execute(
            "SELECT qualified_name, file_path FROM nodes WHERE kind = 'File'"
        ):
            directory = _parent_dir(row["file_path"])
            if directory:
                out[row["qualified_name"]] = directory
        return out

    def count_unresolved_call_sites(self, names: set[str]) -> int:
        """Count call sites that name *names* but were never bound to a node.

        These are the blast radius's blind spot: a CALLS/REFERENCES edge whose
        target is a bare name cannot join a qualified seed, so the traversal
        never follows it. The count is deliberately not receiver-filtered --
        that check needs a specific candidate node -- so it is an upper bound
        on missed impact, not a claim that every one of these is a real call.
        """
        if not names:
            return 0
        self._conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _impact_seed_names (name TEXT PRIMARY KEY)"
        )
        self._conn.execute("DELETE FROM _impact_seed_names")
        name_list = list(names)
        for start in range(0, len(name_list), 450):
            batch = name_list[start:start + 450]
            placeholders = ",".join("(?)" for _ in batch)
            self._conn.execute(  # nosec B608
                f"INSERT OR IGNORE INTO _impact_seed_names (name) VALUES {placeholders}",
                batch,
            )
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM edges "  # noqa: S608
                "JOIN _impact_seed_names s ON s.name = edges.target_qualified "
                "WHERE edges.kind IN ('CALLS', 'REFERENCES') "
                f"AND COALESCE(edges.target_resolution, {TARGET_RESOLUTION_EXPR}) "
                "= 'unresolved'",
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            try:
                self._conn.execute("DELETE FROM _impact_seed_names")
            except sqlite3.Error:
                pass

    def get_impact_radius(
        self,
        changed_files: list[str],
        max_depth: int = MAX_IMPACT_DEPTH,
        max_nodes: int = MAX_IMPACT_NODES,
        resolution: str = "all",
    ) -> dict[str, Any]:
        """Calculate the impact radius (dependents and tests) of changed files within depth N.

        Delegates to ``get_impact_radius_sql()`` by default (faster for
        large graphs).  Set ``CRG_BFS_ENGINE=networkx`` to use the legacy
        Python-side BFS via NetworkX.

        Dependency-shaped edges propagate from target to source, while
        TESTED_BY propagates from production source to test target. CONTAINS
        does not expand the traversal because every node in a changed file is
        already seeded.

        Returns dict with:
          - changed_nodes: nodes in changed files
          - impacted_nodes: reachable nodes ordered by best-path impact score
          - impacted_files: unique set of affected files
          - edges: connecting edges
          - impact_scores: qualified name to best-path score

        ``resolution="direct"`` refuses to traverse a CALLS/REFERENCES edge
        whose target was never bound to an indexed node, so the answer rests
        only on attributions the graph can prove. It is a guard, not a
        reachability change: a bare target cannot equal a qualified seed, so
        an unresolved hop is already unreachable under both engines. The
        guard keeps that true if a future resolver leaves a dangling
        qualified target behind. ``IMPORTS_FROM`` is never filtered -- its
        targets are file paths and C# namespace bridges, for which
        "unresolved" would be a false claim.
        """
        if BFS_ENGINE == "networkx":
            return self._get_impact_radius_networkx(
                changed_files, max_depth=max_depth, max_nodes=max_nodes,
                resolution=resolution,
            )
        return self.get_impact_radius_sql(
            changed_files, max_depth=max_depth, max_nodes=max_nodes,
            resolution=resolution,
        )

    # -- Bounded SQLite relaxation version (default) ----------------------

    def get_impact_radius_sql(
        self,
        changed_files: list[str],
        max_depth: int = MAX_IMPACT_DEPTH,
        max_nodes: int = MAX_IMPACT_NODES,
        resolution: str = "all",
    ) -> dict[str, Any]:
        """Impact radius via bounded best-score relaxation in SQLite.

        Faster than NetworkX for large graphs because it avoids
        materialising the full graph in Python.
        """
        # Validate before the empty-input short circuits so a bad mode is an
        # error rather than a silently accepted no-op.
        resolution_guard = _impact_resolution_guard(resolution)
        max_depth = max(0, int(max_depth))
        max_nodes = max(0, int(max_nodes))
        if not changed_files:
            return {
                "changed_nodes": [],
                "impacted_nodes": [],
                "impacted_files": [],
                "edges": [],
                "truncated": False,
                "total_impacted": 0,
                "impact_scores": {},
            }

        # Seed qualified names
        seeds = self._impact_seed_qns(changed_files)

        if not seeds:
            return {
                "changed_nodes": [],
                "impacted_nodes": [],
                "impacted_files": [],
                "edges": [],
                "truncated": False,
                "total_impacted": 0,
                "impact_scores": {},
            }

        # Use a temp table for the seed set to keep the query plan efficient
        # and stay under SQLite variable limits.
        self._conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _impact_seeds "
            "(qn TEXT PRIMARY KEY)"
        )
        self._conn.execute("DELETE FROM _impact_seeds")
        batch_size = 450
        seed_list = list(seeds)
        for i in range(0, len(seed_list), batch_size):
            batch = seed_list[i:i + batch_size]
            placeholders = ",".join("(?)" for _ in batch)
            self._conn.execute(  # nosec B608
                f"INSERT OR IGNORE INTO _impact_seeds (qn) VALUES {placeholders}",
                batch,
            )

        # Keep one best score per endpoint rather than enumerating every path
        # in a recursive CTE. Dense cyclic graphs can contain exponentially
        # many paths; these three bounded temp tables contain at most one row
        # per qualified name and each iteration scans the edge table once.
        self._conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _impact_policies "
            "(kind TEXT PRIMARY KEY, weight REAL NOT NULL, "
            "direction TEXT NOT NULL)"
        )
        self._conn.execute("DELETE FROM _impact_policies")
        self._conn.executemany(
            "INSERT INTO _impact_policies "
            "(kind, weight, direction) VALUES (?, ?, ?)",
            [
                (
                    kind,
                    weight,
                    IMPACT_EDGE_DIRECTIONS.get(
                        kind, IMPACT_DEFAULT_EDGE_DIRECTION,
                    ),
                )
                for kind, weight in IMPACT_EDGE_WEIGHTS.items()
            ],
        )
        for table in ("_impact_best", "_impact_frontier", "_impact_next"):
            self._conn.execute(
                f"CREATE TEMP TABLE IF NOT EXISTS {table} "  # nosec B608
                "(node_qn TEXT PRIMARY KEY, score REAL NOT NULL)"
            )
            self._conn.execute(f"DELETE FROM {table}")  # nosec B608
        # The frontier's package directories, refilled once per hop. Keeping
        # them in a table rather than computing crg_parent_dir inside the
        # candidate query is what keeps the directory branch an index seek:
        # inlined, SQLite drove the join from nodes-by-kind and scanned every
        # IMPORTS_FROM row on every hop (82s on kubernetes, against 6s here).
        self._conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _impact_frontier_dirs "
            "(dir TEXT PRIMARY KEY, score REAL NOT NULL)"
        )
        self._conn.execute("DELETE FROM _impact_frontier_dirs")

        self._conn.execute(
            "INSERT INTO _impact_best (node_qn, score) "
            "SELECT qn, 1.0 FROM _impact_seeds"
        )
        self._conn.execute(
            "INSERT INTO _impact_frontier (node_qn, score) "
            "SELECT qn, 1.0 FROM _impact_seeds"
        )

        candidate_sql = _impact_candidate_sql(resolution_guard)
        candidate_params = (
            IMPACT_DEFAULT_EDGE_WEIGHT,
            IMPACT_DEPTH_DECAY,
            _IMPACT_AOP_EXTRA_LIKE,
            IMPACT_DIRECTION_OUTGOING,
            IMPACT_DEFAULT_EDGE_DIRECTION,
            IMPACT_DIRECTION_OUTGOING,
            IMPACT_DEFAULT_EDGE_WEIGHT,
            IMPACT_DEPTH_DECAY,
            _IMPACT_AOP_EXTRA_LIKE,
            IMPACT_DIRECTION_OUTGOING,
            IMPACT_DEFAULT_EDGE_DIRECTION,
            IMPACT_DIRECTION_INCOMING,
            IMPACT_DEFAULT_EDGE_WEIGHT,
            IMPACT_DEPTH_DECAY,
            IMPACT_DEFAULT_EDGE_DIRECTION,
            IMPACT_DIRECTION_INCOMING,
            IMPACT_SCORE_FLOOR,
        )
        for _ in range(max_depth):
            self._conn.execute("DELETE FROM _impact_next")
            self._conn.execute("DELETE FROM _impact_frontier_dirs")
            self._conn.execute(IMPACT_FRONTIER_DIRS_SQL)
            self._conn.execute(candidate_sql, candidate_params)
            self._conn.execute(
                "DELETE FROM _impact_next "
                "WHERE score <= COALESCE(("
                "SELECT score FROM _impact_best b "
                "WHERE b.node_qn = _impact_next.node_qn"
                "), 0.0)"
            )
            if self._conn.execute(
                "SELECT 1 FROM _impact_next LIMIT 1"
            ).fetchone() is None:
                break
            self._conn.execute(
                "INSERT OR REPLACE INTO _impact_best (node_qn, score) "
                "SELECT node_qn, score FROM _impact_next"
            )
            self._conn.execute("DELETE FROM _impact_frontier")
            self._conn.execute(
                "INSERT INTO _impact_frontier (node_qn, score) "
                "SELECT node_qn, score FROM _impact_next"
            )

        # Fetch one sentinel beyond the public cap. Ghost endpoints remain in
        # the frontier as bridges but cannot consume a result slot because the
        # final selection joins the canonical nodes table.
        try:
            rows = self._conn.execute(
                "SELECT b.node_qn, b.score "
                "FROM _impact_best b "
                "JOIN nodes n ON n.qualified_name = b.node_qn "
                "LEFT JOIN _impact_seeds s ON s.qn = b.node_qn "
                "WHERE s.qn IS NULL "
                "AND n.extra NOT LIKE '%\"verilog_kind\"%' "
                "ORDER BY b.score DESC, b.node_qn "
                "LIMIT ?",
                (max_nodes + 1,),
            ).fetchall()
            truncated = len(rows) > max_nodes
            if truncated:
                total_impacted = self._conn.execute(
                    "SELECT COUNT(*) "
                    "FROM _impact_best b "
                    "JOIN nodes n ON n.qualified_name = b.node_qn "
                    "LEFT JOIN _impact_seeds s ON s.qn = b.node_qn "
                    "WHERE s.qn IS NULL "
                    "AND n.extra NOT LIKE '%\"verilog_kind\"%'"
                ).fetchone()[0]
            else:
                total_impacted = len(rows)
        finally:
            for _tbl in (
                "_impact_seeds",
                "_impact_policies",
                "_impact_best",
                "_impact_frontier",
                "_impact_next",
                "_impact_frontier_dirs",
            ):
                try:
                    self._conn.execute(f"DELETE FROM {_tbl}")  # nosec B608
                except sqlite3.Error:
                    pass
        kept_rows = rows[:max_nodes]
        score_by_qn = {row[0]: float(row[1]) for row in kept_rows}

        changed_nodes = self._batch_get_nodes(seeds)
        impacted_nodes = self._batch_get_nodes(set(score_by_qn))
        impacted_nodes.sort(
            key=lambda node: (
                -score_by_qn.get(node.qualified_name, 0.0),
                node.qualified_name,
            )
        )

        impacted_files = list({n.file_path for n in impacted_nodes})

        relevant_edges: list[GraphEdge] = []
        all_qns = seeds | {n.qualified_name for n in impacted_nodes}
        if all_qns:
            relevant_edges = self.get_edges_among(all_qns)
            # A directory-scoped import edge connects two files in the answer
            # without either endpoint naming the other, so it is invisible to
            # the set-membership match above.
            relevant_edges.extend(self.import_scope_edges(
                {n.file_path for n in changed_nodes + impacted_nodes},
                all_qns,
            ))
            if resolution == RESOLUTION_DIRECT:
                relevant_edges = [
                    edge for edge in relevant_edges
                    if edge.target_resolution != RESOLUTION_UNRESOLVED
                ]

        return {
            "changed_nodes": changed_nodes,
            "impacted_nodes": impacted_nodes,
            "impacted_files": impacted_files,
            "edges": relevant_edges,
            "truncated": truncated,
            "total_impacted": total_impacted,
            "impact_scores": {
                node.qualified_name: round(
                    score_by_qn.get(node.qualified_name, 0.0), 4,
                )
                for node in impacted_nodes
            },
        }

    # -- NetworkX BFS version (legacy) ------------------------------------

    def _get_impact_radius_networkx(
        self,
        changed_files: list[str],
        max_depth: int = MAX_IMPACT_DEPTH,
        max_nodes: int = MAX_IMPACT_NODES,
        resolution: str = "all",
    ) -> dict[str, Any]:
        """BFS via NetworkX (legacy). Used when CRG_BFS_ENGINE=networkx."""
        if resolution not in IMPACT_RESOLUTIONS:
            raise ValueError(
                f"resolution must be one of {list(IMPACT_RESOLUTIONS)}, "
                f"got {resolution!r}"
            )
        direct_only = resolution == RESOLUTION_DIRECT
        max_depth = max(0, int(max_depth))
        max_nodes = max(0, int(max_nodes))
        nxg = self._build_networkx_graph()

        seeds = self._impact_seed_qns(changed_files)

        # Parity with the SQL engine's directory-scoped branch: a Go import
        # edge targets the package directory, so a file is reached through
        # its own directory rather than by name.
        scope_importers = self._import_scope_importer_index()
        file_directories = (
            self._file_node_directories() if scope_importers else {}
        )
        scope_weight = IMPACT_EDGE_WEIGHTS.get(
            "IMPORTS_FROM", IMPACT_DEFAULT_EDGE_WEIGHT,
        )

        best: dict[str, float] = dict.fromkeys(seeds, 1.0)
        frontier = dict(best)

        for _ in range(max_depth):
            if not frontier:
                break
            next_frontier: dict[str, float] = {}
            for qn, score in frontier.items():
                directory = file_directories.get(qn)
                scoped = [
                    (importer, scope_weight)
                    for importer in scope_importers.get(directory or "", ())
                ]
                if qn not in nxg:
                    if not scoped:
                        continue
                    neighbors = scoped
                else:
                    neighbors = [
                        (target, data["impact_outgoing_weight"])
                        for _, target, data in nxg.out_edges(qn, data=True)
                        if "impact_outgoing_weight" in data
                        and not (direct_only and data.get("unresolved_target"))
                    ] + [
                        (source, data["impact_incoming_weight"])
                        for source, _, data in nxg.in_edges(qn, data=True)
                        if "impact_incoming_weight" in data
                        and not (direct_only and data.get("unresolved_target"))
                    ] + scoped
                for other_qn, weight in neighbors:
                    new_score = score * weight * IMPACT_DEPTH_DECAY
                    if new_score <= IMPACT_SCORE_FLOOR:
                        continue
                    if new_score > best.get(other_qn, 0.0):
                        best[other_qn] = new_score
                        next_frontier[other_qn] = new_score
            frontier = next_frontier

        changed_nodes = self._batch_get_nodes(seeds)
        impacted_qns = set(best) - seeds
        impacted_nodes = self._batch_get_nodes(impacted_qns)
        impacted_nodes = [
            node for node in impacted_nodes
            if not node.extra.get("verilog_kind")
        ]
        impacted_nodes.sort(
            key=lambda node: (
                -best.get(node.qualified_name, 0.0),
                node.qualified_name,
            )
        )

        total_impacted = len(impacted_nodes)
        truncated = total_impacted > max_nodes
        if truncated:
            impacted_nodes = impacted_nodes[:max_nodes]

        impacted_files = list({n.file_path for n in impacted_nodes})

        relevant_edges: list[GraphEdge] = []
        all_qns = seeds | {n.qualified_name for n in impacted_nodes}
        if all_qns:
            relevant_edges = self.get_edges_among(all_qns)
            # A directory-scoped import edge connects two files in the answer
            # without either endpoint naming the other, so it is invisible to
            # the set-membership match above.
            relevant_edges.extend(self.import_scope_edges(
                {n.file_path for n in changed_nodes + impacted_nodes},
                all_qns,
            ))
            if resolution == RESOLUTION_DIRECT:
                relevant_edges = [
                    edge for edge in relevant_edges
                    if edge.target_resolution != RESOLUTION_UNRESOLVED
                ]

        return {
            "changed_nodes": changed_nodes,
            "impacted_nodes": impacted_nodes,
            "impacted_files": impacted_files,
            "edges": relevant_edges,
            "truncated": truncated,
            "total_impacted": total_impacted,
            "impact_scores": {
                node.qualified_name: round(
                    best.get(node.qualified_name, 0.0), 4,
                )
                for node in impacted_nodes
            },
        }

    def get_subgraph(self, qualified_names: list[str]) -> dict[str, Any]:
        """Extract a subgraph containing the specified nodes and their connecting edges."""
        nodes = []
        for qn in qualified_names:
            node = self.get_node(qn)
            if node:
                nodes.append(node)

        edges = []
        qn_set = set(qualified_names)
        for qn in qualified_names:
            for e in self.get_edges_by_source(qn):
                if e.target_qualified in qn_set:
                    edges.append(e)

        return {"nodes": nodes, "edges": edges}

    def get_stats(self) -> GraphStats:
        """Return aggregate statistics about the graph.

        Every statement below reads from one snapshot, so the per-kind
        breakdowns still sum to the totals when a watcher commits partway
        through (issue: `status` printing numbers that do not add up).
        """
        with self._read_transaction():
            return self._get_stats_locked()

    def _get_stats_locked(self) -> GraphStats:
        total_nodes = self._conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        total_edges = self._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

        nodes_by_kind: dict[str, int] = {}
        for row in self._conn.execute(
            "SELECT CASE WHEN extra LIKE '%\"verilog_kind\"%' THEN 'Signal' "
            "ELSE kind END AS display_kind, COUNT(*) AS cnt FROM nodes "
            "GROUP BY CASE WHEN extra LIKE '%\"verilog_kind\"%' "
            "THEN 'Signal' ELSE kind END"
        ):
            nodes_by_kind[row["display_kind"]] = row["cnt"]

        edges_by_kind: dict[str, int] = {}
        for row in self._conn.execute("SELECT kind, COUNT(*) as cnt FROM edges GROUP BY kind"):
            edges_by_kind[row["kind"]] = row["cnt"]

        # Derive languages from the live File inventory, not from every node
        # row: virtual or leftover rows without a backing File node (e.g. the
        # synthetic Spring Event nodes) must not keep a language alive in
        # `status` after its last real file left the graph (issue #474).
        languages = [
            r["language"] for r in self._conn.execute(
                "SELECT DISTINCT language FROM nodes WHERE kind = 'File' "
                "AND language IS NOT NULL AND language != '' ORDER BY language"
            )
        ]

        files_count = self._conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE kind = 'File'"
        ).fetchone()[0]

        last_updated = self.get_metadata("last_updated")

        return GraphStats(
            total_nodes=total_nodes,
            total_edges=total_edges,
            nodes_by_kind=nodes_by_kind,
            edges_by_kind=edges_by_kind,
            languages=languages,
            files_count=files_count,
            last_updated=last_updated,
        )

    @staticmethod
    def _nodes_by_size_filter(
        min_lines: int,
        max_lines: int | None,
        kind: str | None,
        file_path_pattern: str | None,
    ) -> tuple[str, list]:
        """Build the WHERE clause and parameters for a line-count range."""
        conditions = [
            "line_start IS NOT NULL",
            "line_end IS NOT NULL",
            "(line_end - line_start + 1) >= ?",
            "extra NOT LIKE '%\"verilog_kind\"%'",
        ]
        params: list = [min_lines]

        if max_lines is not None:
            conditions.append("(line_end - line_start + 1) <= ?")
            params.append(max_lines)
        if kind:
            conditions.append("kind = ?")
            params.append(kind)
        if file_path_pattern:
            conditions.append("file_path LIKE ?")
            params.append(f"%{file_path_pattern}%")
        return " AND ".join(conditions), params

    def count_nodes_by_size(
        self,
        min_lines: int = 50,
        max_lines: int | None = None,
        kind: str | None = None,
        file_path_pattern: str | None = None,
    ) -> int:
        """Count what :meth:`get_nodes_by_size` would return without a limit."""
        where, params = self._nodes_by_size_filter(
            min_lines, max_lines, kind, file_path_pattern,
        )
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM nodes WHERE {where}",  # nosec B608
            params,
        ).fetchone()
        return int(row[0])

    def get_nodes_by_size(
        self,
        min_lines: int = 50,
        max_lines: int | None = None,
        kind: str | None = None,
        file_path_pattern: str | None = None,
        limit: int = 50,
    ) -> list[GraphNode]:
        """Find nodes within a line-count range, ordered largest first.

        Args:
            min_lines: Minimum line count threshold (inclusive).
            max_lines: Maximum line count threshold (inclusive). None = no upper bound.
            kind: Filter by node kind (Function, Class, File, etc.).
            file_path_pattern: SQL LIKE pattern to filter by file path.
            limit: Maximum results to return.

        Returns:
            List of GraphNode objects, ordered by line count descending.
        """
        where, params = self._nodes_by_size_filter(
            min_lines, max_lines, kind, file_path_pattern,
        )
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM nodes WHERE {where} "  # nosec B608
            "ORDER BY (line_end - line_start + 1) DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._row_to_node(r) for r in rows]

    # --- Public query helpers (used by flows, changes, communities, etc.) ---

    def get_node_by_id(self, node_id: int) -> Optional[GraphNode]:
        """Fetch a single node by its integer primary key."""
        row = self._conn.execute(
            "SELECT * FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        return self._row_to_node(row) if row else None

    def get_nodes_by_kind(
        self,
        kinds: list[str],
        file_pattern: str | None = None,
    ) -> list[GraphNode]:
        """Return nodes matching any of *kinds*, optionally filtered by file.

        Args:
            kinds: List of node kind strings (e.g. ``["Function", "Test"]``).
            file_pattern: If provided, only nodes whose ``file_path``
                contains *file_pattern* (SQL LIKE ``%pattern%``) are
                returned.
        """
        if not kinds:
            return []
        placeholders = ",".join("?" for _ in kinds)
        conditions = [f"kind IN ({placeholders})"]
        params: list[str] = list(kinds)
        if file_pattern:
            conditions.append("file_path LIKE ?")
            params.append(f"%{file_pattern}%")
        where = " AND ".join(conditions)
        rows = self._conn.execute(  # nosec B608
            f"SELECT * FROM nodes WHERE {where}", params,
        ).fetchall()
        return [self._row_to_node(r) for r in rows]

    def count_flow_memberships(self, node_id: int) -> int:
        """Return the number of flows a node participates in."""
        row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM flow_memberships "
            "WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        return row["cnt"] if row else 0

    def get_flow_criticalities_for_node(self, node_id: int) -> list[float]:
        """Return criticality values for all flows a node participates in."""
        rows = self._conn.execute(
            "SELECT f.criticality FROM flows f "
            "JOIN flow_memberships fm ON fm.flow_id = f.id "
            "WHERE fm.node_id = ?",
            (node_id,),
        ).fetchall()
        return [r["criticality"] for r in rows]

    def get_flow_entry_point_ids(self, node_ids: set[int]) -> set[int]:
        """Entry-point node ids of the flows that contain any of *node_ids*."""
        flow_ids = self.get_flow_ids_by_node_ids(node_ids)
        result: set[int] = set()
        for i in range(0, len(flow_ids), 450):
            batch = flow_ids[i:i + 450]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(  # nosec B608
                f"SELECT entry_point_id FROM flows WHERE id IN ({placeholders})",
                batch,
            ).fetchall()
            result.update(r["entry_point_id"] for r in rows)
        return result

    def get_node_community_id(self, node_id: int) -> int | None:
        """Return the ``community_id`` for a node, or ``None``."""
        row = self._conn.execute(
            "SELECT community_id FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        if row and row["community_id"] is not None:
            return row["community_id"]
        return None

    def get_community_ids_by_qualified_names(
        self, qns: list[str],
    ) -> dict[str, int | None]:
        """Batch-fetch ``community_id`` for a list of qualified names.

        Returns a mapping from qualified name to community_id (may be
        ``None`` if the node has no assigned community).
        """
        result: dict[str, int | None] = {}
        batch_size = 450
        for i in range(0, len(qns), batch_size):
            batch = qns[i:i + batch_size]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(  # nosec B608
                "SELECT qualified_name, community_id FROM nodes "
                f"WHERE qualified_name IN ({placeholders})",
                batch,
            ).fetchall()
            for r in rows:
                result[r["qualified_name"]] = r["community_id"]
        return result

    def get_files_matching(self, pattern: str) -> list[str]:
        """Return distinct ``file_path`` values ending with *pattern*.

        Paths are normalized to POSIX separators first. ``%`` and ``_`` in
        *pattern* are treated as literal characters, not SQL ``LIKE``
        wildcards.
        """
        normalized = normalize_file_path(pattern)
        escaped = (
            normalized.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        rows = self._conn.execute(
            "SELECT DISTINCT file_path FROM nodes "
            "WHERE file_path LIKE ? ESCAPE '\\'",
            (f"%{escaped}",),
        ).fetchall()
        return [r["file_path"] for r in rows]

    def get_nodes_without_signature(self) -> list[sqlite3.Row]:
        """Return raw rows for nodes that have no signature yet."""
        return self._conn.execute(
            "SELECT id, name, kind, params, return_type "
            "FROM nodes WHERE signature IS NULL"
        ).fetchall()

    def update_node_signature(
        self, node_id: int, signature: str,
    ) -> None:
        """Set the ``signature`` column for a single node."""
        self._conn.execute(
            "UPDATE nodes SET signature = ? WHERE id = ?",
            (signature, node_id),
        )

    def update_node_signatures(self, signature_rows: list[tuple[str, int]]) -> None:
        """Bulk-set ``signature`` for many nodes in one transaction.

        Hot paths (postprocessing, review builds) should use this instead of
        looping :meth:`update_node_signature`: the per-row loop autocommits
        one UPDATE per node (``isolation_level=None``), i.e. one WAL commit +
        fsync per row, which dominates runtime on graphs with 10^5+ nodes
        (issue #721).

        Args:
            signature_rows: ``(signature, node_id)`` pairs.
        """
        if not signature_rows:
            return
        self._begin_immediate()
        try:
            for start in range(0, len(signature_rows), _UPDATE_BATCH):
                self._conn.executemany(
                    "UPDATE nodes SET signature = ? WHERE id = ?",
                    signature_rows[start:start + _UPDATE_BATCH],
                )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def get_all_community_ids(self) -> dict[str, int | None]:
        """Return a mapping of *all* qualified names to their community_id.

        Used primarily by the visualization exporter.
        """
        try:
            rows = self._conn.execute(
                "SELECT qualified_name, community_id FROM nodes"
            ).fetchall()
            return {
                r["qualified_name"]: r["community_id"]
                for r in rows
            }
        except sqlite3.OperationalError as exc:
            # community_id column may not exist yet on pre-v6 schemas
            logger.debug("Community IDs unavailable (schema not yet migrated): %s", exc)
            return {}

    def get_node_ids_by_files(
        self, file_paths: list[str],
    ) -> set[int]:
        """Return node IDs belonging to the given file paths."""
        if not file_paths:
            return set()
        file_paths = [normalize_file_path(p) for p in file_paths]
        result: set[int] = set()
        batch_size = 450
        for i in range(0, len(file_paths), batch_size):
            batch = file_paths[i:i + batch_size]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(  # nosec B608
                "SELECT id FROM nodes "
                f"WHERE file_path IN ({placeholders})",
                batch,
            ).fetchall()
            result.update(r["id"] for r in rows)
        return result

    def get_flow_ids_by_node_ids(
        self, node_ids: set[int],
    ) -> list[int]:
        """Return distinct flow IDs that contain any of *node_ids*."""
        if not node_ids:
            return []
        nids = list(node_ids)
        result: list[int] = []
        batch_size = 450
        for i in range(0, len(nids), batch_size):
            batch = nids[i:i + batch_size]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(  # nosec B608
                "SELECT DISTINCT flow_id FROM flow_memberships "
                f"WHERE node_id IN ({placeholders})",
                batch,
            ).fetchall()
            result.extend(r["flow_id"] for r in rows)
        # Deduplicate across batches
        return list(dict.fromkeys(result))

    def get_flow_qualified_names(self, flow_id: int) -> set[str]:
        """Return the set of qualified names for nodes in a flow."""
        rows = self._conn.execute(
            "SELECT n.qualified_name FROM flow_memberships fm "
            "JOIN nodes n ON fm.node_id = n.id WHERE fm.flow_id = ?",
            (flow_id,),
        ).fetchall()
        return {r["qualified_name"] for r in rows}

    def get_node_kind_by_id(self, node_id: int) -> str | None:
        """Return just the ``kind`` column for a node, or ``None``."""
        row = self._conn.execute(
            "SELECT kind FROM nodes WHERE id = ?", (node_id,),
        ).fetchone()
        return row["kind"] if row else None

    def get_all_call_targets(self, include_file_sources: bool = True) -> set[str]:
        """Return the set of all CALLS-edge target qualified names.

        When ``include_file_sources`` is False, CALLS edges whose source is a
        File node (module-scope calls from top-level script glue, CLI
        entrypoints, or notebook cells) are excluded. Callers that treat "has
        an incoming call" as "is not a root" (e.g. entry-point detection)
        should pass ``include_file_sources=False`` — otherwise a script-only
        callee looks called and is hidden from flow analysis.

        The File-node filter joins against ``nodes.kind`` rather than pattern-
        matching ``source_qualified`` so that file paths containing ``::`` or
        any future change to the File-node naming convention cannot silently
        miscategorize edges.
        """
        if include_file_sources:
            rows = self._conn.execute(
                "SELECT DISTINCT target_qualified FROM edges "
                "WHERE kind = 'CALLS'"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT DISTINCT e.target_qualified FROM edges e "
                "LEFT JOIN nodes n ON n.qualified_name = e.source_qualified "
                "WHERE e.kind = 'CALLS' "
                "AND (n.kind IS NULL OR n.kind != 'File')"
            ).fetchall()
        return {r["target_qualified"] for r in rows}

    def get_call_targets_outside_tests(self) -> set[str]:
        """Return CALLS targets with a caller that is neither test code nor a File."""
        rows = self._conn.execute(
            "SELECT DISTINCT e.target_qualified FROM edges e "
            "LEFT JOIN nodes n ON n.qualified_name = e.source_qualified "
            "WHERE e.kind = 'CALLS' "
            "AND (n.kind IS NULL OR (n.kind != 'File' AND n.is_test = 0))"
        ).fetchall()
        return {r["target_qualified"] for r in rows}

    def get_communities_list(
        self,
    ) -> list[sqlite3.Row]:
        """Return raw rows from the ``communities`` table."""
        try:
            return self._conn.execute(
                "SELECT id, name FROM communities"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            # communities table doesn't exist yet on pre-v4 schemas
            logger.debug("Communities list unavailable (table missing): %s", exc)
            return []

    def get_community_member_qns(
        self, community_id: int,
    ) -> list[str]:
        """Return qualified names of nodes in a community."""
        rows = self._conn.execute(
            "SELECT qualified_name FROM nodes "
            "WHERE community_id = ?",
            (community_id,),
        ).fetchall()
        return [r["qualified_name"] for r in rows]

    def get_nodes_by_community_id(
        self, community_id: int,
    ) -> list[GraphNode]:
        """Return all nodes belonging to a community."""
        rows = self._conn.execute(
            "SELECT * FROM nodes WHERE community_id = ?",
            (community_id,),
        ).fetchall()
        return [self._row_to_node(r) for r in rows]

    def get_outgoing_targets(
        self, source_qns: list[str],
    ) -> list[str]:
        """Return ``target_qualified`` for edges sourced from *source_qns*."""
        results: list[str] = []
        batch_size = 450
        for i in range(0, len(source_qns), batch_size):
            batch = source_qns[i:i + batch_size]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(  # nosec B608
                "SELECT target_qualified FROM edges "
                f"WHERE source_qualified IN ({placeholders}) "
                "ORDER BY source_qualified, id",
                batch,
            ).fetchall()
            results.extend(r["target_qualified"] for r in rows)
        return results

    def get_incoming_sources(
        self, target_qns: list[str],
    ) -> list[str]:
        """Return ``source_qualified`` for edges targeting *target_qns*."""
        results: list[str] = []
        batch_size = 450
        for i in range(0, len(target_qns), batch_size):
            batch = target_qns[i:i + batch_size]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(  # nosec B608
                "SELECT source_qualified FROM edges "
                f"WHERE target_qualified IN ({placeholders}) "
                "ORDER BY target_qualified, id",
                batch,
            ).fetchall()
            results.extend(r["source_qualified"] for r in rows)
        return results

    # --- Public edge access (for visualization etc.) ---

    def get_all_edges(self) -> list[GraphEdge]:
        """Return all edges in the graph."""
        rows = self._conn.execute("SELECT * FROM edges").fetchall()
        return [self._row_to_edge(r) for r in rows]

    def get_edges_among(self, qualified_names: set[str]) -> list[GraphEdge]:
        """Return edges where both source and target are in the given set.

        Batches the source-side IN clause to stay under SQLite's default
        SQLITE_MAX_VARIABLE_NUMBER limit, then filters targets in Python.
        """
        if not qualified_names:
            return []
        qns = list(qualified_names)
        results: list[GraphEdge] = []
        batch_size = 450  # Stay well under SQLite's default 999 limit
        for i in range(0, len(qns), batch_size):
            batch = qns[i:i + batch_size]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(  # nosec B608
                f"SELECT * FROM edges WHERE source_qualified IN ({placeholders}) "
                "ORDER BY source_qualified, id",
                batch,
            ).fetchall()
            for r in rows:
                edge = self._row_to_edge(r)
                if edge.target_qualified in qualified_names:
                    results.append(edge)
        return results

    def get_edges_touching(self, names: set[str]) -> list[GraphEdge]:
        """Return edges whose stored source or target is in *names*, oldest first.

        Each edge appears once, in the order a read of the whole table gives.
        """
        if not names:
            return []
        wanted = list(names)
        found: dict[int, GraphEdge] = {}
        batch_size = 450  # Stay well under SQLite's default 999 limit
        for column in ("source_qualified", "target_qualified"):
            for i in range(0, len(wanted), batch_size):
                batch = wanted[i:i + batch_size]
                placeholders = ",".join("?" for _ in batch)
                rows = self._conn.execute(  # nosec B608
                    f"SELECT * FROM edges WHERE {column} IN ({placeholders})",
                    batch,
                ).fetchall()
                for row in rows:
                    if row["id"] not in found:
                        found[row["id"]] = self._row_to_edge(row)
        return [found[edge_id] for edge_id in sorted(found)]

    def iter_edge_end_counts(self, column: str) -> Iterator[tuple[str, int]]:
        """Yield each distinct value of an edge end and how many edges have it.

        Reads an index rather than the edge rows, however large the table is.
        """
        if column not in ("source_qualified", "target_qualified"):
            raise ValueError(f"not an edge end: {column!r}")
        rows = self._conn.execute(  # nosec B608
            f"SELECT {column} AS name, COUNT(*) AS edges FROM edges GROUP BY {column}"
        )
        for row in rows:
            yield row["name"], row["edges"]

    def iter_edge_targets_from(self, sources: Iterable[str]) -> Iterator[str]:
        """Yield the target of every edge leaving one of *sources*."""
        wanted = list(sources)
        batch_size = 450
        for i in range(0, len(wanted), batch_size):
            batch = wanted[i:i + batch_size]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(  # nosec B608
                f"SELECT target_qualified FROM edges WHERE source_qualified IN ({placeholders})",
                batch,
            )
            for row in rows:
                yield row["target_qualified"]

    def get_files_with_edges(self, file_paths: Iterable[str]) -> set[str]:
        """Return which of *file_paths* appear as the file of at least one edge."""
        wanted = list(file_paths)
        found: set[str] = set()
        batch_size = 450
        for i in range(0, len(wanted), batch_size):
            batch = wanted[i:i + batch_size]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(  # nosec B608
                f"SELECT DISTINCT file_path FROM edges WHERE file_path IN ({placeholders})",
                batch,
            ).fetchall()
            found.update(row["file_path"] for row in rows)
        return found

    def has_edge_with_backslash_path(self) -> bool:
        """Return whether any edge's file path still holds a backslash (before #774)."""
        row = self._conn.execute(
            "SELECT 1 FROM edges WHERE instr(file_path, char(92)) > 0 LIMIT 1"
        ).fetchone()
        return row is not None

    def iter_node_identities(self) -> Iterator[sqlite3.Row]:
        """Yield the few columns that identify each node, in export order.

        ``listed`` follows the rule :meth:`get_all_files` uses to keep virtual
        nodes out of the file inventory.
        """
        return iter(self._conn.execute(
            "SELECT id, qualified_name, name, kind, file_path, community_id, "
            "(extra IS NULL OR json_valid(extra) = 0 "
            "OR COALESCE(json_extract(extra, '$.virtual'), 0) != 1) AS listed "
            "FROM nodes ORDER BY file_path, id"
        ))

    def _batch_get_nodes(self, qualified_names: set[str]) -> list[GraphNode]:
        """Batch-fetch nodes by qualified name, staying under SQLite variable limits."""
        if not qualified_names:
            return []
        qns = list(qualified_names)
        results: list[GraphNode] = []
        batch_size = 450
        for i in range(0, len(qns), batch_size):
            batch = qns[i:i + batch_size]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(  # nosec B608
                f"SELECT * FROM nodes WHERE qualified_name IN ({placeholders})",
                batch,
            ).fetchall()
            results.extend(self._row_to_node(r) for r in rows)
        return results

    def get_nodes_by_qualified_names(
        self, qualified_names: Iterable[str],
    ) -> list[GraphNode]:
        """Fetch the nodes with these qualified names; unknown names are skipped."""
        return self._batch_get_nodes(set(qualified_names))

    def get_node_degrees(
        self, *, include_tests: bool = True,
    ) -> dict[str, tuple[int, int]]:
        """``qualified_name -> (in_degree, out_degree)`` for every non-File node.

        Counts edges of every kind, which is the measure
        :func:`~code_review_graph.analysis.find_hub_nodes` ranks hubs by,
        without loading every edge into Python. ``include_tests=False``
        leaves out nodes marked as test code.
        """
        sql = (
            "SELECT n.qualified_name, "
            "(SELECT COUNT(*) FROM edges e WHERE e.target_qualified = n.qualified_name), "
            "(SELECT COUNT(*) FROM edges e WHERE e.source_qualified = n.qualified_name) "
            "FROM nodes n WHERE n.kind != 'File'"
        )
        if not include_tests:
            sql += " AND n.is_test = 0 AND n.kind != 'Test'"
        rows = self._conn.execute(sql).fetchall()
        return {row[0]: (int(row[1]), int(row[2])) for row in rows}

    def get_communities_by_ids(self, ids: Iterable[int]) -> dict[int, sqlite3.Row]:
        """``id -> row`` with ``id``, ``name`` and ``size`` for these communities."""
        wanted = sorted({int(i) for i in ids})
        found: dict[int, sqlite3.Row] = {}
        batch_size = 450
        for i in range(0, len(wanted), batch_size):
            batch = wanted[i:i + batch_size]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(  # nosec B608
                f"SELECT id, name, size FROM communities WHERE id IN ({placeholders})",
                batch,
            ).fetchall()
            for row in rows:
                found[int(row["id"])] = row
        return found

    def count_flows(self) -> int:
        """Number of stored execution flows."""
        return int(self._conn.execute("SELECT COUNT(*) FROM flows").fetchone()[0])

    def load_flow_adjacency(self) -> "FlowAdjacency":
        """Load all nodes and CALLS/TESTED_BY edges into memory for fast traversal.

        Reads the entire ``nodes`` and ``edges`` tables in two streaming
        queries and returns an in-memory adjacency structure suitable for
        flow tracing and criticality scoring.  At ~500k nodes / 3M edges
        this fits in a few hundred MB and eliminates tens of millions of
        single-row SQLite point queries that otherwise dominate
        ``trace_flows`` / ``compute_criticality`` runtime.
        """
        nodes_by_qn: dict[str, GraphNode] = {}
        nodes_by_id: dict[int, GraphNode] = {}
        for row in self._conn.execute("SELECT * FROM nodes"):
            node = self._row_to_node(row)
            nodes_by_qn[node.qualified_name] = node
            nodes_by_id[node.id] = node

        calls_out: dict[str, list[str]] = {}
        has_tested_by: set[str] = set()
        for row in self._conn.execute(
            "SELECT kind, source_qualified, target_qualified FROM edges "
            "WHERE kind IN ('CALLS', 'TESTED_BY')"
        ):
            kind, src, tgt = row["kind"], row["source_qualified"], row["target_qualified"]
            if kind == "CALLS":
                calls_out.setdefault(src, []).append(tgt)
            else:  # TESTED_BY: source is the production node being tested. See: #515
                has_tested_by.add(src)

        return FlowAdjacency(
            calls_out=calls_out,
            has_tested_by=has_tested_by,
            nodes_by_qn=nodes_by_qn,
            nodes_by_id=nodes_by_id,
        )

    # --- Internal helpers ---

    def _build_networkx_graph(self) -> nx.DiGraph:
        """Build a directed graph with impact weights for both policies."""
        import networkx as nx

        with self._cache_lock:
            if self._nxg_cache is not None:
                return self._nxg_cache
            g: nx.DiGraph = nx.DiGraph()
            rows = self._conn.execute("SELECT * FROM edges").fetchall()
            known_qns = {
                row[0] for row in self._conn.execute(
                    "SELECT qualified_name FROM nodes"
                )
            }
            for r in rows:
                source = r["source_qualified"]
                target = r["target_qualified"]
                kind = r["kind"]
                candidate_weight = IMPACT_EDGE_WEIGHTS.get(
                    kind, IMPACT_DEFAULT_EDGE_WEIGHT,
                )
                if not g.has_edge(source, target):
                    g.add_edge(source, target, kind=kind)
                data = g[source][target]
                # Mirror the stored column, falling back to the same
                # node-existence test for a graph built before migration v11.
                # Parallel edges collapse into one networkx edge, so the flag
                # means "every edge here is an unresolved call": one proven
                # relationship of any kind clears it, and a direct-only
                # traversal never drops a hop it could prove.
                unresolved = False
                if kind in TARGET_RESOLUTION_KINDS:
                    stored = (
                        r["target_resolution"]
                        if "target_resolution" in r.keys()
                        else None
                    )
                    unresolved = (
                        stored == "unresolved"
                        if stored is not None
                        else target not in known_qns
                    )
                if unresolved:
                    data.setdefault("unresolved_target", True)
                else:
                    data["unresolved_target"] = False
                existing_weight = IMPACT_EDGE_WEIGHTS.get(
                    data.get("kind", ""), IMPACT_DEFAULT_EDGE_WEIGHT,
                )
                if candidate_weight > existing_weight:
                    data["kind"] = kind

                # AOP-derived CALLS edges always propagate source->target
                # (advice -> target), overriding CALLS' registered direction
                # — see the matching comment in get_impact_radius_sql.
                if '"aop_resolved"' in (r["extra"] or ""):
                    direction = IMPACT_DIRECTION_OUTGOING
                else:
                    direction = IMPACT_EDGE_DIRECTIONS.get(
                        kind, IMPACT_DEFAULT_EDGE_DIRECTION,
                    )
                if direction != IMPACT_DIRECTION_NONE:
                    weight_key = f"impact_{direction}_weight"
                    data[weight_key] = max(
                        data.get(weight_key, 0.0), candidate_weight,
                    )
            self._nxg_cache = g
            return g

    def _make_qualified(self, node: NodeInfo) -> str:
        return qualified_name_of(node)

    def _row_to_node(self, row: sqlite3.Row) -> GraphNode:
        raw_extra = row["extra"]
        return GraphNode(
            id=row["id"],
            kind=row["kind"],
            name=row["name"],
            qualified_name=row["qualified_name"],
            file_path=row["file_path"],
            line_start=row["line_start"],
            line_end=row["line_end"],
            language=row["language"] or "",
            parent_name=row["parent_name"],
            params=row["params"],
            return_type=row["return_type"],
            is_test=bool(row["is_test"]),
            file_hash=row["file_hash"],
            extra=(
                json.loads(raw_extra)
                if raw_extra and raw_extra != "{}"
                else {}
            ),
        )

    def _row_to_edge(self, row: sqlite3.Row) -> GraphEdge:
        raw_extra = row["extra"]
        extra = (
            json.loads(raw_extra)
            if raw_extra and raw_extra != "{}"
            else {}
        )
        keys = row.keys()
        confidence = row["confidence"] if "confidence" in keys else 1.0
        confidence_tier = row["confidence_tier"] if "confidence_tier" in keys else "EXTRACTED"
        target_resolution = (
            row["target_resolution"] if "target_resolution" in keys else None
        )
        return GraphEdge(
            id=row["id"],
            kind=row["kind"],
            source_qualified=row["source_qualified"],
            target_qualified=row["target_qualified"],
            file_path=row["file_path"],
            line=row["line"],
            extra=extra,
            confidence=confidence,
            confidence_tier=confidence_tier,
            target_resolution=target_resolution,
        )


_NAME_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f]")


def _sanitize_name(s: str, max_len: int = 256) -> str:
    """Strip ASCII control characters and truncate to prevent prompt injection.

    Node names extracted from source code could contain adversarial strings
    (e.g. ``IGNORE_ALL_PREVIOUS_INSTRUCTIONS``).  This function removes control
    characters (0x00-0x1F except tab and newline) and enforces a length limit so
    that names flowing through MCP tool responses cannot easily influence AI
    agent behaviour.
    """
    # Almost no name needs changing, and the walk below is the slow part of
    # exporting a large graph.
    if len(s) <= max_len and not _NAME_CONTROL_CHARS.search(s):
        return s
    # Strip control chars 0x00-0x1F except \t (0x09) and \n (0x0A)
    cleaned = "".join(
        ch for ch in s
        if ch in ("\t", "\n") or ord(ch) >= 0x20
    )
    return cleaned[:max_len]


def node_to_dict(n: GraphNode) -> dict:
    result = {
        "id": n.id, "kind": n.kind, "name": _sanitize_name(n.name),
        "qualified_name": _sanitize_name(n.qualified_name), "file_path": n.file_path,
        "line_start": n.line_start, "line_end": n.line_end,
        "language": n.language,
        "parent_name": _sanitize_name(n.parent_name) if n.parent_name else n.parent_name,
        "is_test": n.is_test,
    }
    if n.kind in ("YamlPath", "JsonPath"):
        yaml_metadata: dict[str, Any] = {}
        for key in (
            "schema_path",
            "example_path",
            "value_type",
        ):
            value = n.extra.get(key)
            if isinstance(value, str):
                yaml_metadata[key] = _sanitize_name(value)
        for key in (
            "document_index",
            "occurrence_count",
            "duplicate_key_occurrence_count",
            "duplicate_group_count",
        ):
            value = n.extra.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                yaml_metadata[key] = value
        for key in (
            "duplicate_key",
            "duplicate_groups_truncated",
            "is_alias",
            "anchor_definition",
            "yaml_merge",
            "lines_truncated",
        ):
            value = n.extra.get(key)
            if isinstance(value, bool):
                yaml_metadata[key] = value
        value_types = n.extra.get("value_types")
        if isinstance(value_types, list):
            yaml_metadata["value_types"] = [
                _sanitize_name(value)
                for value in value_types[:20]
                if isinstance(value, str)
            ]
        line_samples = n.extra.get("line_samples")
        if isinstance(line_samples, list):
            yaml_metadata["line_samples"] = [
                line for line in line_samples[:20]
                if isinstance(line, int) and not isinstance(line, bool)
            ]
            occurrence_count = n.extra.get("occurrence_count", len(line_samples))
            if isinstance(occurrence_count, int) and not isinstance(occurrence_count, bool):
                yaml_metadata["line_samples_omitted"] = max(
                    0,
                    occurrence_count - len(yaml_metadata["line_samples"]),
                )
        result["json" if n.kind == "JsonPath" else "yaml"] = yaml_metadata
    elif n.kind == "File" and n.language == "json":
        json_metadata: dict[str, Any] = {
            "truncated": bool(n.extra.get("json_truncated")),
            "values_indexed": False,
        }
        if n.extra.get("json_locale_file"):
            json_metadata["locale_file"] = True
        if n.extra.get("json_data_file"):
            json_metadata["data_file"] = True
        result["json"] = json_metadata
    elif n.kind == "File" and "values_indexed" in n.extra:
        file_yaml_metadata: dict[str, Any] = {
            "document_count": n.extra.get("yaml_document_count", 0),
            "duplicate_key_count": n.extra.get("yaml_duplicate_key_count", 0),
            "unsupported_key_count": n.extra.get("yaml_unsupported_key_count", 0),
            "truncated": bool(n.extra.get("yaml_truncated")),
            "values_indexed": False,
        }
        if n.extra.get("yaml_locale_file"):
            file_yaml_metadata["locale_file"] = True
        duplicate_keys = n.extra.get("yaml_duplicate_keys")
        if isinstance(duplicate_keys, list):
            safe_duplicate_keys: list[dict[str, Any]] = []
            for item in duplicate_keys[:100]:
                if not isinstance(item, dict):
                    continue
                item_lines = item.get("line_samples")
                if not isinstance(item_lines, list):
                    item_lines = []
                safe_duplicate_keys.append({
                    "document_index": item.get("document_index"),
                    "path": _sanitize_name(str(item.get("path", ""))),
                    "occurrence_count": item.get("occurrence_count"),
                    "line_samples": [
                        line for line in item_lines[:20]
                        if isinstance(line, int) and not isinstance(line, bool)
                    ],
                    "lines_truncated": bool(item.get("lines_truncated")),
                })
            file_yaml_metadata["duplicate_keys"] = safe_duplicate_keys
        result["yaml"] = file_yaml_metadata
    return result


def edge_to_dict(e: GraphEdge) -> dict:
    result: dict = {
        "id": e.id, "kind": e.kind,
        "source": _sanitize_name(e.source_qualified),
        "target": _sanitize_name(e.target_qualified),
        "file_path": e.file_path, "line": e.line,
        "confidence": e.confidence, "confidence_tier": e.confidence_tier,
    }
    # Only the uncertain value is spelled out. Its absence is the certain
    # case, which is the common one, so the response does not pay a key per
    # edge to say "as expected".
    if e.target_resolution == RESOLUTION_UNRESOLVED:
        result["target_resolution"] = RESOLUTION_UNRESOLVED
    for key in ("ambiguous_targets", "unresolved_targets"):
        targets = e.extra.get(key)
        if isinstance(targets, list):
            result[key] = [
                _sanitize_name(target)
                for target in targets[:20]
                if isinstance(target, str)
            ]
            resolution = key.removesuffix("_targets")
            count = e.extra.get(f"{resolution}_target_count")
            if not isinstance(count, int):
                count = len(targets)
            result[f"{resolution}_target_count"] = count
            result[f"{resolution}_targets_truncated"] = bool(
                e.extra.get(f"{resolution}_targets_truncated")
                or count > len(result[key])
            )
    return result


def __getattr__(name: str) -> Any:
    if name in ("EdgeInfo", "NodeInfo", "is_test_file"):
        from . import parser as _parser

        val = getattr(_parser, name)
        globals()[name] = val
        return val
    if name == "nx":
        import networkx as _nx

        globals()["nx"] = _nx
        return _nx
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
