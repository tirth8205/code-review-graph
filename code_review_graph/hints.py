"""Context-aware hints system for MCP tool responses.

Tracks session state (in-memory only) and generates intelligent
next-step suggestions after each tool call.  Hints are appended as
``_hints`` to new tool responses so that Claude Code can propose
follow-up actions without the user having to discover them.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

# ---- intent categories and their characteristic public MCP tool names ----
# Keep these keyed on exported MCP tool names only. Do not substitute
# internal helper names from implementation modules here.

_INTENT_TOOLS: dict[str, set[str]] = {
    "reviewing": {
        "detect_changes_tool", "get_review_context_tool", "get_affected_flows_tool", "get_impact_radius_tool",
    },
    "debugging": {
        "query_graph_tool", "get_flow_tool", "semantic_search_nodes_tool",
    },
    "refactoring": {
        "refactor_tool", "apply_refactor_tool",
    },
    "exploring": {
        "list_communities_tool", "get_architecture_overview_tool", "list_flows_tool", "list_graph_stats_tool",
    },
}

# ---- workflow adjacency: for each tool, which tools are useful next ----

_WORKFLOW: dict[str, list[dict[str, str]]] = {
    "list_flows_tool": [
        {
            "tool": "get_flow_tool",
            "suggestion": "Drill into a specific flow for step-by-step details",
        },
        {
            "tool": "get_affected_flows_tool",
            "suggestion": "Check which flows are affected by recent changes",
        },
        {
            "tool": "get_architecture_overview_tool",
            "suggestion": "See the high-level architecture",
        },
    ],
    "get_flow_tool": [
        {
            "tool": "query_graph_tool",
            "suggestion": "Inspect callers/callees of a step in this flow",
        },
        {
            "tool": "get_affected_flows_tool",
            "suggestion": "Check if changes affect this flow",
        },
        {
            "tool": "list_flows_tool",
            "suggestion": "Browse other execution flows",
        },
    ],
    "get_affected_flows_tool": [
        {
            "tool": "detect_changes_tool",
            "suggestion": "Get risk-scored change analysis",
        },
        {
            "tool": "get_flow_tool",
            "suggestion": "Inspect a specific affected flow",
        },
        {
            "tool": "get_review_context_tool",
            "suggestion": "Build a full review context for the changes",
        },
    ],
    "list_communities_tool": [
        {
            "tool": "get_community_tool",
            "suggestion": "Inspect a specific community's members",
        },
        {
            "tool": "get_architecture_overview_tool",
            "suggestion": "See cross-community coupling and warnings",
        },
        {
            "tool": "list_flows_tool",
            "suggestion": "See execution flows across communities",
        },
    ],
    "get_community_tool": [
        {
            "tool": "query_graph_tool",
            "suggestion": "Explore callers/callees of community members",
        },
        {
            "tool": "list_communities_tool",
            "suggestion": "Browse other communities",
        },
        {
            "tool": "get_architecture_overview_tool",
            "suggestion": "See how this community fits the architecture",
        },
    ],
    "get_architecture_overview_tool": [
        {
            "tool": "list_communities_tool",
            "suggestion": "Drill into individual communities",
        },
        {
            "tool": "detect_changes_tool",
            "suggestion": "See how recent changes affect the architecture",
        },
        {
            "tool": "list_flows_tool",
            "suggestion": "Explore execution flows",
        },
    ],
    "detect_changes_tool": [
        {
            "tool": "get_review_context_tool",
            "suggestion": "Build a full review context with source snippets",
        },
        {
            "tool": "get_affected_flows_tool",
            "suggestion": "See which execution flows are affected",
        },
        {
            "tool": "get_impact_radius_tool",
            "suggestion": "Expand the blast radius analysis",
        },
        {
            "tool": "refactor_tool",
            "suggestion": "Look for refactoring opportunities in changed code",
        },
    ],
    "refactor_tool": [
        {
            "tool": "apply_refactor_tool",
            "suggestion": "Apply a reviewed rename preview",
        },
        {
            "tool": "semantic_search_nodes_tool",
            "suggestion": "Find related symbols to also rename",
        },
        {
            "tool": "query_graph_tool",
            "suggestion": "Verify call sites before applying a rename",
        },
    ],
    "apply_refactor_tool": [
        {
            "tool": "detect_changes_tool",
            "suggestion": "Check the impact of the applied refactor",
        },
        {
            "tool": "get_affected_flows_tool",
            "suggestion": "See which execution flows may have changed",
        },
        {
            "tool": "query_graph_tool",
            "suggestion": "Inspect remaining call sites or rename fallout",
        },
    ],
    "semantic_search_nodes_tool": [
        {
            "tool": "query_graph_tool",
            "suggestion": "Inspect callers/callees of a search result",
        },
        {
            "tool": "get_flow_tool",
            "suggestion": "See the execution flow through a matched node",
        },
        {
            "tool": "get_impact_radius_tool",
            "suggestion": "Check the blast radius from matched nodes",
        },
    ],
}

# Maximum items per hints category returned to the caller.
_MAX_PER_CATEGORY = 3

# Session history caps.
_MAX_TOOLS_HISTORY = 100
_MAX_NODES_TRACKED = 1000


# ---------------------------------------------------------------------------
# SessionState
# ---------------------------------------------------------------------------


class SessionState:
    """In-memory session state for a single MCP connection."""

    def __init__(self) -> None:
        self.tools_called: deque[str] = deque(maxlen=_MAX_TOOLS_HISTORY)
        self.nodes_queried: set[str] = set()
        self.files_touched: set[str] = set()
        self.inferred_intent: str | None = None
        self.last_tool_time: float = 0.0

    def record_tool_call(self, tool_name: str) -> None:
        """Record a tool invocation (FIFO, capped at 100)."""
        self.tools_called.append(tool_name)
        self.last_tool_time = time.time()

    def record_nodes(self, node_ids: list[str]) -> None:
        """Record queried node identifiers (capped at 1000)."""
        for nid in node_ids:
            if len(self.nodes_queried) >= _MAX_NODES_TRACKED:
                break
            self.nodes_queried.add(nid)

    def record_files(self, files: list[str]) -> None:
        """Record touched file paths."""
        self.files_touched.update(files)


# ---------------------------------------------------------------------------
# Intent inference
# ---------------------------------------------------------------------------


def infer_intent(session: SessionState) -> str:
    """Classify the user's likely intent from their tool-call history.

    Returns one of: ``"reviewing"``, ``"debugging"``, ``"refactoring"``,
    ``"exploring"`` (default).
    """
    if not session.tools_called:
        return "exploring"

    # Score each intent by how many of the last N calls match its tools.
    recent = list(session.tools_called)[-10:]
    scores: dict[str, int] = {intent: 0 for intent in _INTENT_TOOLS}
    for tool in recent:
        for intent, tools in _INTENT_TOOLS.items():
            if tool in tools:
                scores[intent] += 1

    best = max(scores, key=lambda k: scores[k])
    if scores[best] == 0:
        return "exploring"
    return best


# ---------------------------------------------------------------------------
# Hints generation
# ---------------------------------------------------------------------------


def generate_hints(
    tool_name: str,
    result: dict[str, Any],
    session: SessionState,
) -> dict[str, Any]:
    """Build context-aware hints for a tool response.

    Returns::

        {
            "next_steps": [{"tool": ..., "suggestion": ...}, ...],
            "related": [...],
            "warnings": [...],
        }

    At most ``_MAX_PER_CATEGORY`` items per list.  Tools already called
    in this session are suppressed from ``next_steps``.
    """
    # Update session state.
    session.record_tool_call(tool_name)
    session.inferred_intent = infer_intent(session)

    next_steps = _build_next_steps(tool_name, session)
    warnings = _extract_warnings(result)
    # Build related BEFORE tracking, so that the current result's files
    # are not yet in files_touched and can appear as suggestions.
    related = _build_related(tool_name, result, session)

    # Collect files/nodes from result for session tracking.
    _track_result(result, session)

    return {
        "next_steps": next_steps[:_MAX_PER_CATEGORY],
        "related": related[:_MAX_PER_CATEGORY],
        "warnings": warnings[:_MAX_PER_CATEGORY],
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _track_result(result: dict[str, Any], session: SessionState) -> None:
    """Extract node IDs and file paths from a tool result and record them."""
    # Files
    for key in ("changed_files", "impacted_files"):
        files = result.get(key)
        if isinstance(files, list):
            session.record_files([f for f in files if isinstance(f, str)])

    # Nodes — look in common result shapes
    node_ids: list[str] = []
    for key in ("results", "changed_nodes", "impacted_nodes"):
        items = result.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    qn = item.get("qualified_name")
                    if qn:
                        node_ids.append(qn)
    if node_ids:
        session.record_nodes(node_ids)


def _build_next_steps(
    tool_name: str, session: SessionState
) -> list[dict[str, str]]:
    """Return next-step suggestions, filtering already-called tools."""
    called = set(session.tools_called)
    candidates = _WORKFLOW.get(tool_name, [])
    out: list[dict[str, str]] = []
    for c in candidates:
        if c["tool"] not in called:
            out.append(c)
    return out


def _extract_warnings(result: dict[str, Any]) -> list[str]:
    """Pull warning signals from a tool result."""
    warnings: list[str] = []

    # Test gaps
    test_gaps = result.get("test_gaps")
    if isinstance(test_gaps, list) and test_gaps:
        names = [g.get("name", g) if isinstance(g, dict) else str(g) for g in test_gaps[:5]]
        warnings.append(
            f"Test coverage gaps: {', '.join(names)}"
        )

    # High risk score
    risk = result.get("risk_score")
    if isinstance(risk, (int, float)) and risk > 0.7:
        warnings.append(f"High risk score ({risk:.2f}) — review carefully")

    # Coupling warnings from architecture overview
    arch_warnings = result.get("warnings")
    if isinstance(arch_warnings, list):
        for w in arch_warnings[:3]:
            if isinstance(w, str):
                warnings.append(w)
            elif isinstance(w, dict) and "message" in w:
                warnings.append(w["message"])

    return warnings


def _build_related(
    tool_name: str,
    result: dict[str, Any],
    session: SessionState,
) -> list[str]:
    """Suggest related node/file identifiers from the result."""
    related: list[str] = []
    seen: set[str] = set()

    # Suggest impacted files the user hasn't touched yet
    impacted = result.get("impacted_files")
    if isinstance(impacted, list):
        for f in impacted:
            if isinstance(f, str) and f not in session.files_touched and f not in seen:
                related.append(f)
                seen.add(f)
                if len(related) >= _MAX_PER_CATEGORY:
                    break

    return related


# ---------------------------------------------------------------------------
# Module-level session singleton
# ---------------------------------------------------------------------------

_session = SessionState()


def get_session() -> SessionState:
    """Return the global in-memory session state."""
    return _session


def reset_session() -> None:
    """Reset the global session (useful for testing)."""
    global _session
    _session = SessionState()
