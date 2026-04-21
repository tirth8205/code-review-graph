"""Change impact analysis for code review.

Maps git/svn diffs to affected functions, flows, communities, and test coverage
gaps. Produces risk-scored, priority-ordered review guidance.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .constants import SECURITY_KEYWORDS as _SECURITY_KEYWORDS
from .flows import get_affected_flows
from .graph import GraphNode, GraphStore, _sanitize_name, node_to_dict

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = int(os.environ.get("CRG_GIT_TIMEOUT", "30"))  # seconds, configurable

_SAFE_GIT_REF = re.compile(r"^[A-Za-z0-9_.~^/@{}\-]+$")
_SAFE_SVN_REV = re.compile(r"^r?\d+(:r?\d+|:HEAD|:BASE|:COMMITTED)?$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# 1. parse_git_diff_ranges / parse_svn_diff_ranges
# ---------------------------------------------------------------------------


def parse_git_diff_ranges(
    repo_root: str,
    base: str = "HEAD~1",
) -> dict[str, list[tuple[int, int]]]:
    """Run ``git diff --unified=0`` and extract changed line ranges per file.

    Args:
        repo_root: Absolute path to the repository root.
        base: Git ref to diff against (default: ``HEAD~1``).

    Returns:
        Mapping of file paths to lists of ``(start_line, end_line)`` tuples.
        Returns an empty dict on error.
    """
    if not _SAFE_GIT_REF.match(base):
        logger.warning("Invalid git ref rejected: %s", base)
        return {}
    try:
        result = subprocess.run(
            ["git", "diff", "--unified=0", base, "--"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=repo_root,
            timeout=_GIT_TIMEOUT,
        )
        if result.returncode != 0:
            logger.warning("git diff failed (rc=%d): %s", result.returncode, result.stderr[:200])
            return {}
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("git diff error: %s", exc)
        return {}

    return _parse_unified_diff(result.stdout)


def parse_svn_diff_ranges(
    repo_root: str,
    rev_range: str | None = None,
) -> dict[str, list[tuple[int, int]]]:
    """Run ``svn diff`` and extract changed line ranges per file.

    Args:
        repo_root: Absolute path to the SVN working copy root.
        rev_range: Optional SVN revision range in ``rXXX:HEAD`` format.
            When *None*, diffs the working copy against BASE (local changes).

    Returns:
        Mapping of file paths to lists of ``(start_line, end_line)`` tuples.
        Returns an empty dict on error.
    """
    cmd = ["svn", "diff", "--non-interactive"]
    if rev_range:
        if not _SAFE_SVN_REV.match(rev_range):
            logger.warning("Invalid SVN revision range rejected: %s", rev_range)
            return {}
        cmd.extend(["-r", rev_range])
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=repo_root,
            timeout=_GIT_TIMEOUT,
        )
        if result.returncode != 0:
            logger.warning("svn diff failed (rc=%d): %s", result.returncode, result.stderr[:200])
            return {}
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("svn diff error: %s", exc)
        return {}

    return _parse_unified_diff(result.stdout)


def parse_diff_ranges(
    repo_root: str,
    base: str = "HEAD~1",
) -> dict[str, list[tuple[int, int]]]:
    """Auto-detect VCS and return changed line ranges per file.

    Dispatches to :func:`parse_git_diff_ranges` for Git repositories and
    :func:`parse_svn_diff_ranges` for SVN working copies.

    Args:
        repo_root: Absolute path to the repository/working-copy root.
        base: For Git: the ref to diff against (default ``HEAD~1``).
              For SVN: an optional revision range (e.g. ``"r100:HEAD"``);
              when *base* is not a valid SVN revision, working-copy changes
              (``svn diff``) are used instead.
    """
    root_path = Path(repo_root)
    if (root_path / ".svn").exists():
        rev_range = base if _SAFE_SVN_REV.match(base) else None
        return parse_svn_diff_ranges(repo_root, rev_range)
    return parse_git_diff_ranges(repo_root, base)


def _parse_unified_diff(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    """Parse unified diff output into file -> line-range mappings.

    Handles the ``@@ -old,count +new,count @@`` hunk header format.
    """
    ranges: dict[str, list[tuple[int, int]]] = {}
    current_file: str | None = None

    # Match "+++ b/path/to/file"
    file_pattern = re.compile(r"^\+\+\+ b/(.+)$")
    # Match "@@ ... +start,count @@" or "@@ ... +start @@"
    hunk_pattern = re.compile(r"^@@ .+? \+(\d+)(?:,(\d+))? @@")

    for line in diff_text.splitlines():
        file_match = file_pattern.match(line)
        if file_match:
            current_file = file_match.group(1)
            continue

        hunk_match = hunk_pattern.match(line)
        if hunk_match and current_file is not None:
            start = int(hunk_match.group(1))
            count = int(hunk_match.group(2)) if hunk_match.group(2) else 1
            if count == 0:
                # Pure deletion hunk (no lines added); still note the position.
                end = start
            else:
                end = start + count - 1
            ranges.setdefault(current_file, []).append((start, end))

    return ranges


# ---------------------------------------------------------------------------
# 2. map_changes_to_nodes
# ---------------------------------------------------------------------------


def map_changes_to_nodes(
    store: GraphStore,
    changed_ranges: dict[str, list[tuple[int, int]]],
) -> list[GraphNode]:
    """Find graph nodes whose line ranges overlap the changed lines.

    Args:
        store: The graph store.
        changed_ranges: Mapping of file paths to ``(start, end)`` tuples.

    Returns:
        Deduplicated list of overlapping graph nodes.
    """
    seen: set[str] = set()
    result: list[GraphNode] = []

    for file_path, ranges in changed_ranges.items():
        # Try the path as-is, then also try all nodes to match relative paths.
        nodes = store.get_nodes_by_file(file_path)
        if not nodes:
            # The graph may store absolute paths; try a suffix match.
            matched_paths = store.get_files_matching(file_path)
            for mp in matched_paths:
                nodes.extend(store.get_nodes_by_file(mp))

        for node in nodes:
            if node.qualified_name in seen:
                continue
            if node.line_start is None or node.line_end is None:
                continue
            # Check overlap with any changed range.
            for start, end in ranges:
                if node.line_start <= end and node.line_end >= start:
                    result.append(node)
                    seen.add(node.qualified_name)
                    break

    return result


# ---------------------------------------------------------------------------
# 3. compute_risk_score
# ---------------------------------------------------------------------------


def compute_risk_score(store: GraphStore, node: GraphNode) -> float:
    """Compute a risk score (0.0 - 1.0) for a single node.

    Scoring factors:
      - Flow participation: 0.05 per flow membership, capped at 0.25
      - Community crossing: 0.05 per caller from a different community, capped at 0.15
      - Test coverage: 0.30 (untested) scaling down to 0.05 (5+ TESTED_BY edges)
      - Security sensitivity: 0.20 if name matches security keywords
      - Caller count: callers / 20, capped at 0.10
    """
    score = 0.0

    # --- Flow participation (cap 0.25), weighted by criticality ---
    flow_criticalities = store.get_flow_criticalities_for_node(node.id)
    if flow_criticalities:
        score += min(sum(flow_criticalities), 0.25)
    else:
        flow_count = store.count_flow_memberships(node.id)
        score += min(flow_count * 0.05, 0.25)

    # --- Community crossing (cap 0.15) ---
    callers = store.get_edges_by_target(node.qualified_name)
    caller_edges = [e for e in callers if e.kind == "CALLS"]

    cross_community = 0
    node_cid = store.get_node_community_id(node.id)

    if node_cid is not None and caller_edges:
        caller_qns = [edge.source_qualified for edge in caller_edges]
        cid_map = store.get_community_ids_by_qualified_names(caller_qns)
        for cid in cid_map.values():
            if cid is not None and cid != node_cid:
                cross_community += 1
    score += min(cross_community * 0.05, 0.15)

    # --- Test coverage (direct + transitive) ---
    transitive_tests = store.get_transitive_tests(node.qualified_name)
    test_count = len(transitive_tests)
    score += 0.30 - (min(test_count / 5.0, 1.0) * 0.25)

    # --- Security sensitivity ---
    name_lower = node.name.lower()
    qn_lower = node.qualified_name.lower()
    if any(kw in name_lower or kw in qn_lower for kw in _SECURITY_KEYWORDS):
        score += 0.20

    # --- Caller count (cap 0.10) ---
    caller_count = len(caller_edges)
    score += min(caller_count / 20.0, 0.10)

    return round(min(max(score, 0.0), 1.0), 4)


# ---------------------------------------------------------------------------
# 4. analyze_changes
# ---------------------------------------------------------------------------


def analyze_changes(
    store: GraphStore,
    changed_files: list[str],
    changed_ranges: dict[str, list[tuple[int, int]]] | None = None,
    repo_root: str | None = None,
    base: str = "HEAD~1",
) -> dict[str, Any]:
    """Analyze changes and produce risk-scored review guidance.

    Args:
        store: The graph store.
        changed_files: List of changed file paths.
        changed_ranges: Optional pre-parsed diff ranges. If not provided and
            ``repo_root`` is given, they are computed via the detected VCS
            (Git or SVN).
        repo_root: Repository root (for git/svn diff).
        base: Git ref or SVN revision range to diff against.

    Returns:
        Dict with ``summary``, ``risk_score``, ``changed_functions``,
        ``affected_flows``, ``test_gaps``, and ``review_priorities``.
    """
    # Compute changed ranges if not provided.
    if changed_ranges is None and repo_root is not None:
        changed_ranges = parse_diff_ranges(repo_root, base)

    # Map changes to nodes.
    if changed_ranges:
        changed_nodes = map_changes_to_nodes(store, changed_ranges)
    else:
        # Fallback: all nodes in changed files.
        changed_nodes = []
        for fp in changed_files:
            changed_nodes.extend(store.get_nodes_by_file(fp))

    # Filter to functions/tests for risk scoring (skip File nodes).
    changed_funcs = [
        n for n in changed_nodes
        if n.kind in ("Function", "Test", "Class")
    ]

    # Compute per-node risk scores.
    node_risks: list[dict[str, Any]] = []
    for node in changed_funcs:
        risk = compute_risk_score(store, node)
        node_risks.append({
            **node_to_dict(node),
            "risk_score": risk,
        })

    # Overall risk score: max of individual risks, or 0.
    overall_risk = max((nr["risk_score"] for nr in node_risks), default=0.0)

    # Affected flows.
    affected = get_affected_flows(store, changed_files)

    # Detect test gaps: changed functions without TESTED_BY edges.
    test_gaps: list[dict[str, Any]] = []
    for node in changed_funcs:
        if node.is_test:
            continue
        tested = store.get_edges_by_target(node.qualified_name)
        if not any(e.kind == "TESTED_BY" for e in tested):
            test_gaps.append({
                "name": _sanitize_name(node.name),
                "qualified_name": _sanitize_name(node.qualified_name),
                "file": node.file_path,
                "line_start": node.line_start,
                "line_end": node.line_end,
            })

    # Review priorities: top 10 by risk score.
    review_priorities = sorted(node_risks, key=lambda x: x["risk_score"], reverse=True)[:10]

    # Build summary.
    summary_parts = [
        f"Analyzed {len(changed_files)} changed file(s):",
        f"  - {len(changed_funcs)} changed function(s)/class(es)",
        f"  - {affected['total']} affected flow(s)",
        f"  - {len(test_gaps)} test gap(s)",
        f"  - Overall risk score: {overall_risk:.2f}",
    ]
    if test_gaps:
        gap_names = [g["name"] for g in test_gaps[:5]]
        summary_parts.append(f"  - Untested: {', '.join(gap_names)}")

    return {
        "summary": "\n".join(summary_parts),
        "risk_score": overall_risk,
        "changed_functions": node_risks,
        "affected_flows": affected["affected_flows"],
        "test_gaps": test_gaps,
        "review_priorities": review_priorities,
    }
