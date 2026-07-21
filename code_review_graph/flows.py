"""Execution flow detection, tracing, and criticality scoring.

Detects entry points in the codebase (functions with no incoming CALLS edges,
framework-decorated handlers, and conventional name patterns), traces execution
paths via forward BFS through CALLS edges, scores each flow for criticality,
and persists results to the ``flows`` / ``flow_memberships`` tables.
"""

from __future__ import annotations

import json
import logging
import re
from collections import deque
from pathlib import Path
from typing import Iterable, Optional

from .constants import SECURITY_KEYWORDS as _SECURITY_KEYWORDS
from .graph import FlowAdjacency, GraphNode, GraphStore, _sanitize_name

logger = logging.getLogger(__name__)

_SQL_BATCH = 450


def expand_changed_file_paths(
    store: GraphStore,
    changed_files: Iterable[str],
    repo_root: str | Path | None = None,
) -> list[str]:
    """Expand *changed_files* to every path form present in the graph.

    ``git diff --name-only`` yields repo-relative paths, while parsed nodes
    store absolute ``file_path`` values (and tests often use relative paths).
    Matching must accept both. See #569.
    """
    root = Path(repo_root).resolve() if repo_root is not None else None
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(path: str) -> None:
        if path and path not in seen:
            seen.add(path)
            candidates.append(path)

    for raw in changed_files:
        text = str(raw).replace("\\", "/")
        _add(text)
        path = Path(raw)
        if path.is_absolute():
            _add(str(path))
            try:
                resolved = path.resolve()
                _add(str(resolved))
                if root is not None:
                    _add(str(resolved.relative_to(root)).replace("\\", "/"))
            except (OSError, ValueError):
                pass
        else:
            if root is not None:
                abs_path = (root / path).resolve()
                _add(str(abs_path))
                _add(str(root / path))

    # Include any graph paths that end with a candidate suffix so mixed
    # absolute/relative stores still match.
    matched: list[str] = []
    matched_seen: set[str] = set()
    for candidate in candidates:
        if candidate not in matched_seen:
            matched.append(candidate)
            matched_seen.add(candidate)
        for graph_path in store.get_files_matching(candidate):
            if graph_path not in matched_seen:
                matched.append(graph_path)
                matched_seen.add(graph_path)
    return matched


def capture_affected_flow_entry_points(
    store: GraphStore,
    file_paths: list[str],
) -> set[str]:
    """Return entry-point qualified names for flows touching *file_paths*.

    Must run **before** node replacement: after ``remove_file_data`` the old
    node IDs are gone and membership JOINs can no longer find those flows.
    """
    if not file_paths:
        return set()

    node_ids = store.get_node_ids_by_files(file_paths)
    if not node_ids:
        return set()

    flow_ids = store.get_flow_ids_by_node_ids(node_ids)
    if not flow_ids:
        return set()

    conn = store._conn
    entry_qns: set[str] = set()
    for i in range(0, len(flow_ids), _SQL_BATCH):
        batch = flow_ids[i:i + _SQL_BATCH]
        placeholders = ",".join("?" * len(batch))
        rows = conn.execute(
            f"SELECT n.qualified_name FROM flows f "  # nosec B608
            f"JOIN nodes n ON n.id = f.entry_point_id "
            f"WHERE f.id IN ({placeholders})",
            batch,
        ).fetchall()
        entry_qns.update(r[0] for r in rows if r[0])
    return entry_qns


def clear_flows_for_files(store: GraphStore, file_paths: list[str]) -> set[str]:
    """Delete flows that touch *file_paths* before those nodes are replaced.

    Returns the entry-point qualified names that should be re-traced after
    replacement (stable across node ID churn).
    """
    entry_qns = capture_affected_flow_entry_points(store, file_paths)
    if not file_paths:
        return entry_qns

    conn = store._conn
    node_ids = list(store.get_node_ids_by_files(file_paths))
    flow_ids = store.get_flow_ids_by_node_ids(set(node_ids)) if node_ids else []

    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for i in range(0, len(flow_ids), _SQL_BATCH):
            batch = flow_ids[i:i + _SQL_BATCH]
            placeholders = ",".join("?" * len(batch))
            conn.execute(
                f"DELETE FROM flow_memberships WHERE flow_id IN ({placeholders})",  # nosec B608
                batch,
            )
            conn.execute(
                f"DELETE FROM flows WHERE id IN ({placeholders})",  # nosec B608
                batch,
            )
        # Drop memberships that still point at nodes about to be deleted so
        # replacement cannot leave dangling flow_memberships rows.
        for i in range(0, len(node_ids), _SQL_BATCH):
            batch = node_ids[i:i + _SQL_BATCH]
            placeholders = ",".join("?" * len(batch))
            conn.execute(
                f"DELETE FROM flow_memberships WHERE node_id IN ({placeholders})",  # nosec B608
                batch,
            )
            conn.execute(
                f"DELETE FROM flows WHERE entry_point_id IN ({placeholders})",  # nosec B608
                batch,
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return entry_qns


def purge_orphan_flow_data(store: GraphStore) -> int:
    """Remove flow rows/memberships that reference missing nodes.

    Returns the number of orphan memberships deleted.
    """
    conn = store._conn
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "DELETE FROM flow_memberships WHERE node_id NOT IN (SELECT id FROM nodes)"
        )
        deleted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
        conn.execute(
            "DELETE FROM flows WHERE entry_point_id NOT IN (SELECT id FROM nodes)"
        )
        # Flows whose memberships were fully purged.
        conn.execute(
            "DELETE FROM flows WHERE id NOT IN ("
            "SELECT DISTINCT flow_id FROM flow_memberships)"
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return deleted

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Decorator patterns that indicate a function is a framework entry point.
_FRAMEWORK_DECORATOR_PATTERNS: list[re.Pattern[str]] = [
    # Python web frameworks
    re.compile(r"app\.(get|post|put|delete|patch|route|websocket|on_event)", re.IGNORECASE),
    re.compile(r"router\.(get|post|put|delete|patch|route)", re.IGNORECASE),
    re.compile(r"blueprint\.(route|before_request|after_request)", re.IGNORECASE),
    re.compile(r"(before|after)_(request|response)", re.IGNORECASE),
    # CLI frameworks
    re.compile(r"click\.(command|group)", re.IGNORECASE),
    re.compile(r"\w+\.(command|group)\b", re.IGNORECASE),  # Click subgroups: @mygroup.command()
    # Pydantic validators/serializers
    re.compile(r"(field|model)_(serializer|validator)", re.IGNORECASE),
    # Task queues
    re.compile(r"(celery\.)?(task|shared_task|periodic_task)", re.IGNORECASE),
    # Django
    re.compile(r"receiver", re.IGNORECASE),
    re.compile(r"api_view", re.IGNORECASE),
    re.compile(r"\baction\b", re.IGNORECASE),
    # Testing
    re.compile(r"pytest\.(fixture|mark)"),
    re.compile(r"(override_settings|modify_settings)", re.IGNORECASE),
    # SQLAlchemy / event systems
    re.compile(r"(event\.)?listens_for", re.IGNORECASE),
    # Java Spring
    re.compile(r"(Get|Post|Put|Delete|Patch|RequestMapping)Mapping", re.IGNORECASE),
    re.compile(r"(Scheduled|EventListener|Bean|Configuration)", re.IGNORECASE),
    re.compile(r"KafkaListener", re.IGNORECASE),
    # Temporal Java callbacks are invoked by the workflow runtime.
    re.compile(r"(WorkflowMethod|ActivityMethod)", re.IGNORECASE),
    # JS/TS frameworks
    re.compile(r"(Component|Injectable|Controller|Module|Guard|Pipe)", re.IGNORECASE),
    re.compile(r"(Subscribe|Mutation|Query|Resolver)", re.IGNORECASE),
    # Express / Koa / Hono route handlers
    re.compile(r"(app|router)\.(get|post|put|delete|patch|use|all)\b"),
    # Android lifecycle
    re.compile(r"@(Override|OnLifecycleEvent|Composable)", re.IGNORECASE),
    # Kotlin coroutines / Android ViewModel
    re.compile(r"(HiltViewModel|AndroidEntryPoint|Inject)", re.IGNORECASE),
    # AI/agent frameworks (pydantic-ai, langchain, etc.)
    re.compile(r"\w+\.(tool|tool_plain|system_prompt|result_validator)\b", re.IGNORECASE),
    re.compile(r"^tool\b"),  # bare @tool (LangChain, etc.)
    # Middleware and exception handlers (Starlette, FastAPI, Sanic)
    re.compile(r"\w+\.(middleware|exception_handler|on_exception)\b", re.IGNORECASE),
    # Generic route decorator (Flask blueprints: @bp.route, @auth_bp.route, etc.)
    re.compile(r"\w+\.route\b", re.IGNORECASE),
]

# Name patterns that indicate conventional entry points.
_ENTRY_NAME_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^main$"),
    re.compile(r"^__main__$"),
    re.compile(r"^test_"),
    re.compile(r"^Test[A-Z]"),
    re.compile(r"^on_"),
    re.compile(r"^handle_"),
    # Lambda / serverless handler functions (wired via config, not code calls)
    re.compile(r"^handler$"),
    re.compile(r"^handle$"),
    re.compile(r"^lambda_handler$"),
    # Alembic migration entry points
    re.compile(r"^upgrade$"),
    re.compile(r"^downgrade$"),
    # FastAPI lifecycle / dependency injection
    re.compile(r"^lifespan$"),
    re.compile(r"^get_db$"),
    # Android Activity/Fragment lifecycle
    re.compile(r"^on(Create|Start|Resume|Pause|Stop|Destroy|Bind|Receive)"),
    # Servlet / JAX-RS
    re.compile(r"^do(Get|Post|Put|Delete)$"),
    # Python BaseHTTPRequestHandler
    re.compile(r"^do_(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)$"),
    re.compile(r"^log_message$"),
    # Express middleware signature
    re.compile(r"^(middleware|errorHandler)$"),
    # Angular lifecycle hooks
    re.compile(
        r"^ng(OnInit|OnChanges|OnDestroy|DoCheck"
        r"|AfterContentInit|AfterContentChecked|AfterViewInit|AfterViewChecked)$"
    ),
    # Angular Pipe / ControlValueAccessor / Guards / Resolvers
    re.compile(r"^(transform|writeValue|registerOnChange|registerOnTouched|setDisabledState)$"),
    re.compile(r"^(canActivate|canDeactivate|canActivateChild|canLoad|canMatch|resolve)$"),
    # React class component lifecycle
    re.compile(
        r"^(componentDidMount|componentDidUpdate|componentWillUnmount"
        r"|shouldComponentUpdate|render)$"
    ),
]

# Framework and language conventions that must not pollute other parsers.
_LANGUAGE_ENTRY_NAME_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "php": (
        re.compile(r"^(boot|register)$"),
        re.compile(r"^__invoke$"),
    ),
}


# ---------------------------------------------------------------------------
# Entry-point detection
# ---------------------------------------------------------------------------


def _has_framework_decorator(node: GraphNode) -> bool:
    """Return True if *node* has a decorator matching a framework pattern."""
    decorators = node.extra.get("decorators")
    if not decorators:
        return False
    if isinstance(decorators, str):
        decorators = [decorators]
    for dec in decorators:
        for pat in _FRAMEWORK_DECORATOR_PATTERNS:
            if pat.search(dec):
                return True
    return False


def _matches_entry_name(node: GraphNode) -> bool:
    """Return True if *node*'s name matches a conventional entry-point pattern."""
    for pat in _ENTRY_NAME_PATTERNS:
        if pat.search(node.name):
            return True
    for pat in _LANGUAGE_ENTRY_NAME_PATTERNS.get(node.language, ()):
        if pat.search(node.name):
            return True
    return False


_TEST_FILE_RE = re.compile(
    r"([\\/]__tests__[\\/]|\.spec\.[jt]sx?$|\.test\.[jt]sx?$|[\\/]test_[^/\\]*\.py$)",
)


def _is_test_file(file_path: str) -> bool:
    """Return True if *file_path* looks like a test file."""
    return bool(_TEST_FILE_RE.search(file_path))


def detect_entry_points(
    store: GraphStore,
    include_tests: bool = False,
) -> list[GraphNode]:
    """Find functions that are entry points in the graph.

    An entry point is a Function/Test node that either:
    1. Has no incoming CALLS edges (true root), or
    2. Has a framework decorator (e.g. ``@app.get``), or
    3. Matches a conventional name pattern (``main``, ``test_*``, etc.).

    When *include_tests* is False (the default), Test nodes are excluded so
    that flow analysis focuses on production entry points.
    """
    # Build a set of all qualified names that are CALLS targets. Exclude
    # edges sourced at File nodes so that script-/notebook-/top-level-only
    # callees (e.g. ``run_job()`` invoked from module scope, a top-level
    # ``<App />`` render) remain detectable as entry points.
    called_qnames = store.get_all_call_targets(include_file_sources=False)

    # Scan all nodes for entry-point candidates.
    candidate_nodes = store.get_nodes_by_kind(["Function", "Test"])

    entry_points: list[GraphNode] = []
    seen_qn: set[str] = set()

    for node in candidate_nodes:
        if not include_tests and (node.is_test or _is_test_file(node.file_path)):
            continue
        if node.extra.get("verilog_kind"):
            continue

        is_entry = False

        # True root: no one calls this function.
        if node.qualified_name not in called_qnames:
            is_entry = True

        # Framework decorator match.
        if _has_framework_decorator(node):
            is_entry = True

        # Conventional name match.
        if _matches_entry_name(node):
            is_entry = True

        if is_entry and node.qualified_name not in seen_qn:
            entry_points.append(node)
            seen_qn.add(node.qualified_name)

    return entry_points


# ---------------------------------------------------------------------------
# Flow tracing (BFS)
# ---------------------------------------------------------------------------


def _trace_single_flow(
    adj: FlowAdjacency,
    ep: GraphNode,
    max_depth: int = 15,
) -> Optional[dict]:
    """Trace a single execution flow from *ep* via forward BFS.

    Returns a flow dict (see :func:`trace_flows` for the schema) or ``None``
    if the flow is trivial (single-node, no outgoing CALLS that resolve).
    """
    path_ids: list[int] = [ep.id]
    path_qnames: list[str] = [ep.qualified_name]
    visited: set[str] = {ep.qualified_name}
    queue: deque[tuple[str, int]] = deque([(ep.qualified_name, 0)])

    actual_depth = 0
    nodes_by_qn = adj.nodes_by_qn
    calls_out = adj.calls_out

    while queue:
        current_qn, depth = queue.popleft()
        if depth > actual_depth:
            actual_depth = depth
        if depth >= max_depth:
            continue

        for target_qn in calls_out.get(current_qn, ()):
            if target_qn in visited:
                continue
            target_node = nodes_by_qn.get(target_qn)
            if target_node is None:
                continue
            visited.add(target_qn)
            path_ids.append(target_node.id)
            path_qnames.append(target_qn)
            queue.append((target_qn, depth + 1))

    # Skip trivial single-node flows.
    if len(path_ids) < 2:
        return None

    files = list({
        n.file_path
        for qn in path_qnames
        if (n := nodes_by_qn.get(qn)) is not None
    })

    flow: dict = {
        "name": _sanitize_name(ep.name),
        "entry_point": ep.qualified_name,
        "entry_point_id": ep.id,
        "path": path_ids,
        "depth": actual_depth,
        "node_count": len(path_ids),
        "file_count": len(files),
        "files": files,
        "criticality": 0.0,
    }
    flow["criticality"] = compute_criticality(flow, adj)
    return flow


def trace_flows(
    store: GraphStore,
    max_depth: int = 15,
    include_tests: bool = False,
) -> list[dict]:
    """Trace execution flows from every entry point via forward BFS.

    Returns a list of flow dicts, each containing:
      - name: human-readable flow name (entry point name)
      - entry_point: qualified name of the entry point
      - entry_point_id: node database id of the entry point
      - path: ordered list of node IDs in the flow
      - depth: maximum BFS depth reached
      - node_count: number of distinct nodes in the path
      - file_count: number of distinct files touched
      - files: list of distinct file paths
      - criticality: computed criticality score (0.0-1.0)
    """
    entry_points = detect_entry_points(store, include_tests=include_tests)
    if not entry_points:
        return []

    adj = store.load_flow_adjacency()
    flows: list[dict] = []

    for ep in entry_points:
        flow = _trace_single_flow(adj, ep, max_depth)
        if flow is not None:
            flows.append(flow)

    # Sort by criticality descending.
    flows.sort(key=lambda f: f["criticality"], reverse=True)
    return flows


# ---------------------------------------------------------------------------
# Criticality scoring
# ---------------------------------------------------------------------------


def compute_criticality(flow: dict, adj: FlowAdjacency) -> float:
    """Score a flow from 0.0 to 1.0 based on multiple weighted factors.

    Weights:
      - File spread:         0.30
      - External calls:      0.20
      - Security sensitivity: 0.25
      - Test coverage gap:   0.15
      - Depth:               0.10
    """
    node_ids: list[int] = flow.get("path", [])
    if not node_ids:
        return 0.0

    nodes_by_id = adj.nodes_by_id
    nodes_by_qn = adj.nodes_by_qn
    calls_out = adj.calls_out
    has_tested_by = adj.has_tested_by

    nodes: list[GraphNode] = [
        n for nid in node_ids if (n := nodes_by_id.get(nid)) is not None
    ]
    if not nodes:
        return 0.0

    # --- File spread (0.0 - 1.0) ---
    file_count = len({n.file_path for n in nodes})
    # Normalize: 1 file => 0.0, 5+ files => 1.0
    file_spread = min((file_count - 1) / 4.0, 1.0) if file_count > 1 else 0.0

    # --- External calls (0.0 - 1.0) ---
    # Calls that target nodes NOT in the graph are considered external.
    external_count = 0
    for n in nodes:
        for target_qn in calls_out.get(n.qualified_name, ()):
            if target_qn not in nodes_by_qn:
                external_count += 1
    # Normalize: 0 => 0.0, 5+ => 1.0
    external_score = min(external_count / 5.0, 1.0)

    # --- Security sensitivity (0.0 - 1.0) ---
    security_hits = 0
    for n in nodes:
        name_lower = n.name.lower()
        qn_lower = n.qualified_name.lower()
        for kw in _SECURITY_KEYWORDS:
            if kw in name_lower or kw in qn_lower:
                security_hits += 1
                break  # Count each node at most once.
    security_score = min(security_hits / max(len(nodes), 1), 1.0)

    # --- Test coverage gap (0.0 - 1.0) ---
    tested_count = sum(1 for n in nodes if n.qualified_name in has_tested_by)
    coverage = tested_count / max(len(nodes), 1)
    test_gap = 1.0 - coverage

    # --- Depth (0.0 - 1.0) ---
    depth = flow.get("depth", 0)
    # Normalize: 0 => 0.0, 10+ => 1.0
    depth_score = min(depth / 10.0, 1.0)

    # --- Weighted sum ---
    criticality = (
        file_spread * 0.30
        + external_score * 0.20
        + security_score * 0.25
        + test_gap * 0.15
        + depth_score * 0.10
    )
    return round(min(max(criticality, 0.0), 1.0), 4)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def store_flows(store: GraphStore, flows: list[dict]) -> int:
    """Clear existing flows and persist new ones.

    Returns the number of flows stored.
    """
    # NOTE: store_flows uses _conn directly because it performs
    # multi-statement batch writes (DELETE + INSERT loop) that are
    # tightly coupled to the DB transaction lifecycle.
    conn = store._conn

    if conn.in_transaction:
        logger.warning("Rolling back uncommitted transaction before BEGIN IMMEDIATE")
        conn.rollback()
    # Wrap the full DELETE + INSERT sequence in an explicit transaction
    # so partial writes cannot occur if an exception interrupts the loop.
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM flow_memberships")
        conn.execute("DELETE FROM flows")

        count = 0
        for flow in flows:
            path_json = json.dumps(flow.get("path", []))
            conn.execute(
                """INSERT INTO flows
                   (name, entry_point_id, depth, node_count, file_count,
                    criticality, path_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    flow["name"],
                    flow["entry_point_id"],
                    flow["depth"],
                    flow["node_count"],
                    flow["file_count"],
                    flow["criticality"],
                    path_json,
                ),
            )
            flow_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            # Insert memberships.
            node_ids = flow.get("path", [])
            for position, node_id in enumerate(node_ids):
                conn.execute(
                    "INSERT OR IGNORE INTO flow_memberships (flow_id, node_id, position) "
                    "VALUES (?, ?, ?)",
                    (flow_id, node_id, position),
                )
            count += 1

        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return count


def incremental_trace_flows(
    store: GraphStore,
    changed_files: list[str],
    max_depth: int = 15,
    repo_root: str | Path | None = None,
    entry_point_qns: Iterable[str] | None = None,
) -> int:
    """Re-trace only flows that touch *changed_files*.  Much faster than full trace.

    1. Expand *changed_files* to absolute/relative forms present in the graph (#569).
    2. Purge orphan memberships left behind by node ID replacement.
    3. Find flow IDs whose memberships reference nodes in the expanded paths.
    4. Collect entry-point node IDs / qualified names before deleting those flows.
    5. Delete only the affected flows and their memberships.
    6. Re-detect entry points in expanded paths **or** matching pre-captured
       entry-point qualified names (stable across node replacement).
    7. BFS-trace each relevant entry point via :func:`_trace_single_flow`.
    8. INSERT the new flows (without clearing unrelated flows).

    Returns the number of re-traced flows that were stored.
    """
    if not changed_files and not entry_point_qns:
        return 0

    conn = store._conn
    expanded = expand_changed_file_paths(store, changed_files, repo_root=repo_root)
    changed_file_set = set(expanded)
    preserved_entry_qns = {qn for qn in (entry_point_qns or []) if qn}

    # Safety net for callers that replace nodes without clear_flows_for_files.
    purge_orphan_flow_data(store)

    # ------------------------------------------------------------------
    # 1. Find affected flow IDs (path-expanded)
    # ------------------------------------------------------------------
    affected_ids: list[int] = []
    if expanded:
        for i in range(0, len(expanded), _SQL_BATCH):
            batch = expanded[i:i + _SQL_BATCH]
            placeholders = ",".join("?" * len(batch))
            affected_rows = conn.execute(
                f"SELECT DISTINCT fm.flow_id FROM flow_memberships fm "  # nosec B608
                f"JOIN nodes n ON n.id = fm.node_id "
                f"WHERE n.file_path IN ({placeholders})",
                batch,
            ).fetchall()
            affected_ids.extend(r[0] for r in affected_rows)
        affected_ids = list(dict.fromkeys(affected_ids))

    # ------------------------------------------------------------------
    # 2. Collect old entry-point node IDs / QNs before deletion
    # ------------------------------------------------------------------
    entry_point_ids: set[int] = set()
    if affected_ids:
        for i in range(0, len(affected_ids), _SQL_BATCH):
            batch = affected_ids[i:i + _SQL_BATCH]
            ep_placeholders = ",".join("?" * len(batch))
            ep_rows = conn.execute(
                f"SELECT f.entry_point_id, n.qualified_name FROM flows f "  # nosec B608
                f"LEFT JOIN nodes n ON n.id = f.entry_point_id "
                f"WHERE f.id IN ({ep_placeholders})",
                batch,
            ).fetchall()
            for row in ep_rows:
                if row[0] is not None:
                    entry_point_ids.add(row[0])
                if row[1]:
                    preserved_entry_qns.add(row[1])

    # ------------------------------------------------------------------
    # 3. Delete affected flows and their memberships
    # ------------------------------------------------------------------
    # Wrap in an explicit transaction so a crash mid-loop cannot leave
    # orphaned flow_memberships rows pointing at deleted flows.  See #258.
    if affected_ids:
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            for fid in affected_ids:
                conn.execute(
                    "DELETE FROM flow_memberships WHERE flow_id = ?", (fid,),
                )
                conn.execute("DELETE FROM flows WHERE id = ?", (fid,))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    # ------------------------------------------------------------------
    # 4. Re-detect entry points and filter to relevant ones
    # ------------------------------------------------------------------
    entry_points = detect_entry_points(store)
    relevant_eps = [
        ep for ep in entry_points
        if (
            ep.file_path in changed_file_set
            or ep.id in entry_point_ids
            or ep.qualified_name in preserved_entry_qns
        )
    ]

    # ------------------------------------------------------------------
    # 5. BFS-trace each relevant entry point
    # ------------------------------------------------------------------
    new_flows: list[dict] = []
    if relevant_eps:
        adj = store.load_flow_adjacency()
        for ep in relevant_eps:
            flow = _trace_single_flow(adj, ep, max_depth)
            if flow is not None:
                new_flows.append(flow)

    # ------------------------------------------------------------------
    # 6. INSERT new flows without clearing unrelated ones
    # ------------------------------------------------------------------
    count = 0
    for flow in new_flows:
        path_json = json.dumps(flow.get("path", []))
        conn.execute(
            """INSERT INTO flows
               (name, entry_point_id, depth, node_count, file_count,
                criticality, path_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                flow["name"],
                flow["entry_point_id"],
                flow["depth"],
                flow["node_count"],
                flow["file_count"],
                flow["criticality"],
                path_json,
            ),
        )
        flow_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        node_ids = flow.get("path", [])
        for position, node_id in enumerate(node_ids):
            conn.execute(
                "INSERT OR IGNORE INTO flow_memberships (flow_id, node_id, position) "
                "VALUES (?, ?, ?)",
                (flow_id, node_id, position),
            )
        count += 1

    conn.commit()
    return count


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def get_flows(
    store: GraphStore,
    sort_by: str = "criticality",
    limit: int = 50,
) -> list[dict]:
    """Retrieve stored flows from the database.

    Args:
        store: The graph store.
        sort_by: Column to sort by (``criticality``, ``depth``, ``node_count``).
        limit: Maximum number of flows to return.
    """
    allowed_sort = {"criticality", "depth", "node_count", "file_count", "name"}
    if sort_by not in allowed_sort:
        sort_by = "criticality"

    order = "DESC" if sort_by in ("criticality", "depth", "node_count", "file_count") else "ASC"

    # NOTE: get_flows reads from the flows table which is managed by
    # the flows module; _conn access is documented coupling.
    rows = store._conn.execute(
        f"SELECT * FROM flows ORDER BY {sort_by} {order} LIMIT ?",  # nosec B608
        (limit,),
    ).fetchall()

    results: list[dict] = []
    for row in rows:
        results.append({
            "id": row["id"],
            "name": _sanitize_name(row["name"]),
            "entry_point_id": row["entry_point_id"],
            "depth": row["depth"],
            "node_count": row["node_count"],
            "file_count": row["file_count"],
            "criticality": row["criticality"],
            "path": json.loads(row["path_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })
    return results


def get_flow_by_id(store: GraphStore, flow_id: int) -> Optional[dict]:
    """Retrieve a single flow with full path details.

    Returns a dict with the flow metadata plus a ``steps`` list containing
    each node's name, kind, file, and line info.
    """
    # NOTE: get_flow_by_id reads from the flows table; see store_flows note.
    row = store._conn.execute(
        "SELECT * FROM flows WHERE id = ?", (flow_id,)
    ).fetchone()
    if row is None:
        return None

    path_ids: list[int] = json.loads(row["path_json"])

    # Build detailed step info.
    steps: list[dict] = []
    for nid in path_ids:
        node = store.get_node_by_id(nid)
        if node:
            steps.append({
                "node_id": node.id,
                "name": _sanitize_name(node.name),
                "kind": node.kind,
                "file": node.file_path,
                "line_start": node.line_start,
                "line_end": node.line_end,
                "qualified_name": _sanitize_name(node.qualified_name),
            })

    return {
        "id": row["id"],
        "name": _sanitize_name(row["name"]),
        "entry_point_id": row["entry_point_id"],
        "depth": row["depth"],
        "node_count": row["node_count"],
        "file_count": row["file_count"],
        "criticality": row["criticality"],
        "path": path_ids,
        "steps": steps,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_affected_flows(
    store: GraphStore,
    changed_files: list[str],
) -> dict:
    """Find flows that include nodes from the given changed files.

    Returns::

        {
            "affected_flows": [<flow dicts>],
            "total": <int>,
        }
    """
    if not changed_files:
        return {"affected_flows": [], "total": 0}

    # Find node IDs belonging to changed files.
    node_ids = store.get_node_ids_by_files(changed_files)

    if not node_ids:
        return {"affected_flows": [], "total": 0}

    # Find flow IDs that contain any of these nodes.
    flow_ids = store.get_flow_ids_by_node_ids(node_ids)

    if not flow_ids:
        return {"affected_flows": [], "total": 0}

    affected: list[dict] = []
    for fid in flow_ids:
        flow = get_flow_by_id(store, fid)
        if flow:
            affected.append(flow)

    # Sort by criticality descending.
    affected.sort(key=lambda f: f.get("criticality", 0), reverse=True)

    return {
        "affected_flows": affected,
        "total": len(affected),
    }
