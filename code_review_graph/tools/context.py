"""Tool: get_minimal_context — ultra-compact context for token-efficient workflows."""

from __future__ import annotations

import logging
import sqlite3
import subprocess
from typing import Any

from ..errors import ChangeDiscoveryError
from ..incremental import discover_review_changes, get_db_path, resolve_review_base
from ..parser import normalize_file_path
from ._common import _get_store, _resolve_root, compact_response, graph_provenance

logger = logging.getLogger(__name__)


def _not_ready(reason: str, summary: str) -> dict[str, Any]:
    """Return a compact response that directs callers to initialize the graph."""
    return {
        "status": "not_ready",
        "reason": reason,
        "summary": summary,
        "next_tool_suggestions": ["build_or_update_graph"],
    }


def get_minimal_context(
    task: str = "",
    changed_files: list[str] | None = None,
    repo_root: str | None = None,
    base: str = "HEAD~1",
) -> dict[str, Any]:
    """Return minimum context an agent needs to start any task (~100 tokens).

    Combines graph stats, top communities, top flows, risk score,
    and suggested next tools into an ultra-compact response.

    Args:
        task: Natural language description of what the agent is doing
              (e.g. "review PR #42", "debug login timeout").
        changed_files: Explicit changed files. Auto-detected from git if None.
        repo_root: Repository root path. Auto-detected if None.
        base: Git ref for diff comparison.

    Returns:
        Compact graph context, or ``status: not_ready`` when the graph is
        missing, empty, or known to have been built at a different Git commit.
    """
    root = _resolve_root(repo_root)
    db_path = get_db_path(root, read_only=True)
    if not db_path.is_file():
        return _not_ready(
            "missing_graph",
            "No graph database found. Build the graph before requesting context.",
        )

    store, root = _get_store(str(root))
    try:
        # 1. Quick stats
        stats = store.get_stats()
        if stats.total_nodes == 0:
            return _not_ready(
                "empty_graph",
                "The graph database contains no nodes. Build the graph before requesting context.",
            )

        provenance = graph_provenance(str(root))
        if provenance and provenance.get("head_matches_build") is False:
            return _not_ready(
                "stale_graph",
                "The graph was built at a different Git commit. "
                "Update it before requesting context.",
            )

        # 2. Risk from changed files
        risk = "unknown"
        risk_score = 0.0
        top_affected: list[str] = []
        test_gap_count = 0
        churn_status = "off"
        discovery_failed = ""
        # CLAUDE.md tells agents to call this tool first, so its git work is
        # the first thing a slow repository blocks on -- and it used to run
        # resolve_review_base (2 x 30s) + two hardcoded 10s probes +
        # get_changed_files (30s) before answering anything. One
        # discover_review_changes call replaces all five subprocesses with
        # three on the short discovery budget, which is the whole point of
        # #262: this is the entry point, so it cannot be the exception.
        try:
            if changed_files:
                files: list[str] | None = changed_files
                base = resolve_review_base(root, base)
            else:
                files, base = discover_review_changes(root, base)
        except ChangeDiscoveryError as exc:
            # Never silently "no changes": that is the answer an agent acts
            # on. Say the lookup failed and carry on with the other sections.
            discovery_failed = str(exc)
            logger.warning("Change discovery failed in get_minimal_context: %s", exc)
            files = None
        if files:
            try:
                from ..changes import analyze_changes

                abs_files = [normalize_file_path(root / f) for f in files]
                analysis = analyze_changes(
                    store, abs_files, repo_root=str(root), base=base,
                    # Same reason as detect_changes: without this the
                    # change-frequency term is pinned at zero for every
                    # agent-driven review. The git log behind it is
                    # memoised per commit, bounded, and fails soft.
                    include_churn=True,
                )
                risk_score = analysis.get("risk_score", 0.0)
                churn_status = analysis.get("churn_status", "off")
                risk = (
                    "high" if risk_score > 0.7
                    else "medium" if risk_score > 0.4
                    else "low"
                )
                top_affected = [
                    f.get("name", "")
                    for f in analysis.get("changed_functions", [])[:5]
                ]
                test_gap_count = len(analysis.get("test_gaps", []))
            except (
                ImportError, OSError, ValueError,
                sqlite3.Error, subprocess.SubprocessError,
            ):
                logger.debug("Risk analysis failed in get_minimal_context", exc_info=True)

        # 3. Top 3 communities
        communities: list[str] = []
        try:
            rows = store._conn.execute(
                "SELECT name FROM communities ORDER BY size DESC LIMIT 3"
            ).fetchall()
            communities = [r[0] for r in rows]
        except sqlite3.OperationalError:  # nosec B110 — table may not exist yet
            logger.debug("communities table not yet populated")

        # 4. Top 3 critical flows
        flows: list[str] = []
        try:
            rows = store._conn.execute(
                "SELECT name FROM flows ORDER BY criticality DESC LIMIT 3"
            ).fetchall()
            flows = [r[0] for r in rows]
        except sqlite3.OperationalError:  # nosec B110 — table may not exist yet
            logger.debug("flows table not yet populated")

        # 5. Suggest next tools based on task keywords
        task_lower = task.lower()
        if any(w in task_lower for w in ("review", "pr", "merge", "diff")):
            suggestions = ["detect_changes", "get_affected_flows", "get_review_context"]
        elif any(w in task_lower for w in ("debug", "bug", "error", "fix")):
            suggestions = ["semantic_search_nodes", "query_graph", "get_flow"]
        elif any(w in task_lower for w in ("refactor", "rename", "dead", "clean")):
            suggestions = ["refactor", "find_large_functions", "get_architecture_overview"]
        elif any(w in task_lower for w in ("onboard", "understand", "explore", "arch")):
            suggestions = [
                "get_architecture_overview", "list_communities", "list_flows",
            ]
        else:
            suggestions = [
                "detect_changes", "semantic_search_nodes",
                "get_architecture_overview",
            ]

        # Build summary
        summary_parts = [
            f"{stats.total_nodes} nodes, {stats.total_edges} edges"
            f" across {stats.files_count} files.",
        ]
        if risk != "unknown":
            summary_parts.append(f"Risk: {risk} ({risk_score:.2f}).")
        if test_gap_count:
            summary_parts.append(f"{test_gap_count} test gaps.")
        if discovery_failed:
            # Same contract as the churn note below: a degraded run must not
            # read like a healthy one.
            summary_parts.append(f"Degraded: {discovery_failed}")
        if churn_status == "unavailable":
            # The score above is missing a term worth up to 0.15. Saying so
            # costs a handful of tokens; not saying so makes a degraded run
            # indistinguishable from a healthy one.
            summary_parts.append(
                "Degraded: change-frequency risk unavailable (git history "
                "too slow); risk excludes churn."
            )

        return compact_response(
            summary=" ".join(summary_parts),
            key_entities=top_affected or None,
            risk=risk,
            communities=communities or None,
            flows_affected=flows or None,
            next_tool_suggestions=suggestions,
        )
    finally:
        store.close()
