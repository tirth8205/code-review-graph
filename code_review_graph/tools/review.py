"""Tools 4, 12, 16: review context, affected flows, detect changes."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..changes import analyze_changes, parse_diff_ranges, parse_git_diff_ranges  # noqa: F401
from ..context_savings import attach_context_savings, estimate_file_tokens, estimate_tokens
from ..flows import get_affected_flows as _get_affected_flows
from ..graph import edge_to_dict, node_to_dict
from ..hints import generate_hints, get_session
from ..incremental import get_changed_files, get_staged_and_unstaged
from ._common import _get_store, _resolve_graph_file_paths

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool 4: get_review_context
# ---------------------------------------------------------------------------


def get_review_context(
    changed_files: list[str] | None = None,
    max_depth: int = 2,
    include_source: bool = True,
    max_lines_per_file: int = 200,
    repo_root: str | None = None,
    base: str = "HEAD~1",
    detail_level: str = "standard",
    max_tokens: int = 0,
) -> dict[str, Any]:
    """Generate a focused review context from changed files.

    Builds a token-optimized subgraph + source snippets for code review.

    Args:
        changed_files: Files to review (auto-detected from git diff if omitted).
        max_depth: Impact radius depth (default: 2).
        include_source: Whether to include source code snippets (default: True).
        max_lines_per_file: Max source lines per file in output (default: 200).
        repo_root: Repository root path. Auto-detected if omitted.
        base: Git ref for change detection (default: HEAD~1).
        detail_level: Output detail level.  "standard" returns full context;
            "minimal" returns summary, risk level, changed/impacted file counts,
            top 5 key entity names, test gap count, and next tool suggestions.
            Default: "standard".
        max_tokens: Token budget for the standard response (estimated via the
            chars/4 heuristic).  When the source snippets would exceed it, the
            lowest-risk files are dropped first and an honest ``omitted`` note
            is reported.  ``0`` (default) disables budgeting.

    Returns:
        Structured review context with subgraph, source snippets, and
        review guidance.
    """
    store, root = _get_store(repo_root)
    try:
        # Get impact radius first
        if changed_files is None:
            changed_files = get_changed_files(root, base)
            if not changed_files:
                changed_files = get_staged_and_unstaged(root)

        if not changed_files:
            return {
                "status": "ok",
                "summary": "No changes detected. Nothing to review.",
                "context": {},
            }

        graph_files = _resolve_graph_file_paths(store, root, changed_files)
        original_tokens = estimate_file_tokens(root, changed_files)
        impact = store.get_impact_radius(graph_files, max_depth=max_depth)

        if detail_level == "minimal":
            impacted_count = len(impact["impacted_nodes"])
            if impacted_count > 20:
                risk = "high"
            elif impacted_count > 5:
                risk = "medium"
            else:
                risk = "low"

            key_entities = [
                n.name for n in impact["changed_nodes"][:5]
            ]

            # Count test gaps among changed functions.
            changed_funcs = [
                n for n in impact["changed_nodes"]
                if n.kind == "Function" and not n.is_test
            ]
            test_edges = [
                e for e in impact["edges"] if e.kind == "TESTED_BY"
            ]
            tested_qualified = {e.source_qualified for e in test_edges}
            test_gap_count = sum(
                1 for f in changed_funcs
                if f.qualified_name not in tested_qualified
            )

            summary_parts = [
                f"Review context for {len(changed_files)} changed file(s):",
                f"  - Risk: {risk}",
                f"  - {len(impact['impacted_nodes'])} impacted nodes"
                f" in {len(impact['impacted_files'])} files",
            ]

            result = {
                "status": "ok",
                "summary": "\n".join(summary_parts),
                "risk": risk,
                "changed_file_count": len(changed_files),
                "impacted_file_count": len(impact["impacted_files"]),
                "key_entities": key_entities,
                "test_gaps": test_gap_count,
                "next_tool_suggestions": [
                    "detect_changes",
                    "get_affected_flows",
                    "get_impact_radius",
                ],
            }
            attach_context_savings(result, original_tokens=original_tokens)
            return result

        # Build review context
        context: dict[str, Any] = {
            "changed_files": changed_files,
            "impacted_files": impact["impacted_files"],
            "graph": {
                "changed_nodes": [
                    node_to_dict(n) for n in impact["changed_nodes"]
                ],
                "impacted_nodes": [
                    node_to_dict(n) for n in impact["impacted_nodes"]
                ],
                "edges": [edge_to_dict(e) for e in impact["edges"]],
            },
        }

        # Add source snippets for changed files
        if include_source:
            snippets = {}
            for rel_path in changed_files:
                full_path = root / rel_path
                if full_path.is_file():
                    try:
                        lines = full_path.read_text(
                            errors="replace"
                        ).splitlines()
                        if len(lines) > max_lines_per_file:
                            # Include only the relevant functions/classes
                            relevant_lines = _extract_relevant_lines(
                                lines,
                                impact["changed_nodes"],
                                str(full_path),
                            )
                            snippets[rel_path] = relevant_lines
                        else:
                            snippets[rel_path] = "\n".join(
                                f"{i+1}: {line}"
                                for i, line in enumerate(lines)
                            )
                    except (OSError, UnicodeDecodeError):
                        snippets[rel_path] = "(could not read file)"
            context["source_snippets"] = snippets

        # Generate review guidance
        guidance = _generate_review_guidance(impact, changed_files)
        context["review_guidance"] = guidance

        # Enforce the token budget by dropping the lowest-risk source snippets
        # first.  Without this guard a large review can return 85k-400k tokens
        # and break the token promise.  The snippets dominate the payload, so
        # we budget them; the structural subgraph + guidance stay intact.
        budget_note = None
        if max_tokens and include_source and context.get("source_snippets"):
            budget_note = _budget_source_snippets(
                context, impact["changed_nodes"], max_tokens
            )

        summary_parts = [
            f"Review context for {len(changed_files)} changed file(s):",
            f"  - {len(impact['changed_nodes'])} directly changed nodes",
            f"  - {len(impact['impacted_nodes'])} impacted nodes"
            f" in {len(impact['impacted_files'])} files",
            "",
            "Review guidance:",
            guidance,
        ]
        if budget_note:
            summary_parts.append("")
            summary_parts.append(budget_note["note"])

        result = {
            "status": "ok",
            "summary": "\n".join(summary_parts),
            "context": context,
        }
        if budget_note:
            result["omitted"] = budget_note
        attach_context_savings(result, original_tokens=original_tokens)
        return result
    finally:
        store.close()


def _file_risk_rank(changed_nodes: list, snippet_files: list[str]) -> list[str]:
    """Order snippet files highest-risk first.

    Risk proxy: the number of directly-changed nodes in each file (more
    changed entities -> more to review).  Files with no node mapping sort
    last but keep their original order for stability.
    """
    counts: dict[str, int] = {}
    for n in changed_nodes:
        fp = getattr(n, "file_path", None)
        if fp:
            counts[fp] = counts.get(fp, 0) + 1

    def _key(rel_path: str) -> int:
        # ``snippet`` keys are repo-relative; node file_paths are absolute.
        # Match on suffix so either representation ranks correctly.
        for fp, c in counts.items():
            if fp.endswith(rel_path) or rel_path.endswith(fp):
                return c
        return 0

    return sorted(snippet_files, key=_key, reverse=True)


def _budget_source_snippets(
    context: dict[str, Any], changed_nodes: list, max_tokens: int
) -> dict[str, Any] | None:
    """Trim ``context['source_snippets']`` to fit ``max_tokens``.

    Keeps the highest-risk files (most changed nodes) and drops the rest.
    Returns an ``omitted`` summary dict, or ``None`` if nothing was dropped.
    Mutates ``context`` in place.
    """
    snippets: dict[str, str] = context["source_snippets"]
    # Budget against everything except the snippets, so structural context is
    # never sacrificed for snippets.
    scaffold = dict(context)
    scaffold.pop("source_snippets", None)
    overhead = estimate_tokens(scaffold)
    remaining = max_tokens - overhead

    ordered = _file_risk_rank(changed_nodes, list(snippets))
    kept: dict[str, str] = {}
    used = 0
    omitted_files: list[str] = []
    for rel_path in ordered:
        snippet = snippets[rel_path]
        # +1 token slack for the JSON key/quoting overhead per entry.
        cost = estimate_tokens(snippet) + estimate_tokens(rel_path) + 1
        if remaining > 0 and used + cost <= remaining:
            kept[rel_path] = snippet
            used += cost
        else:
            omitted_files.append(rel_path)

    if not omitted_files:
        return None

    context["source_snippets"] = kept
    note = (
        f"Token budget ({max_tokens}) reached: omitted source for "
        f"{len(omitted_files)} lower-risk file(s). Re-run with a higher "
        f"max_tokens or pass changed_files explicitly to see them."
    )
    return {
        "source_files": len(omitted_files),
        "omitted_file_names": omitted_files[:20],
        "max_tokens": max_tokens,
        "note": note,
    }


def _extract_relevant_lines(
    lines: list[str], nodes: list, file_path: str
) -> str:
    """Extract only the lines relevant to changed nodes."""
    ranges = []
    for n in nodes:
        if n.file_path == file_path:
            start = max(0, n.line_start - 3)  # 2 lines context before
            end = min(len(lines), n.line_end + 2)  # 1 line context after
            ranges.append((start, end))

    if not ranges:
        # Show first N lines as fallback
        return "\n".join(
            f"{i+1}: {line}" for i, line in enumerate(lines[:50])
        )

    # Merge overlapping ranges
    ranges.sort()
    merged = [ranges[0]]
    for start, end in ranges[1:]:
        if start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    parts: list[str] = []
    for start, end in merged:
        if parts:
            parts.append("...")
        for i in range(start, end):
            parts.append(f"{i+1}: {lines[i]}")

    return "\n".join(parts)


def _generate_review_guidance(
    impact: dict, changed_files: list[str]
) -> str:
    """Generate review guidance based on the impact analysis."""
    guidance_parts = []

    # Check for test coverage
    changed_funcs = [
        n for n in impact["changed_nodes"] if n.kind == "Function"
    ]
    test_edges = [e for e in impact["edges"] if e.kind == "TESTED_BY"]
    tested_funcs = {e.source_qualified for e in test_edges}

    untested = [
        f for f in changed_funcs
        if f.qualified_name not in tested_funcs and not f.is_test
    ]
    if untested:
        guidance_parts.append(
            f"- {len(untested)} changed function(s) lack test coverage: "
            + ", ".join(n.name for n in untested[:5])
        )

    # Check for wide blast radius
    if len(impact["impacted_nodes"]) > 20:
        guidance_parts.append(
            f"- Wide blast radius: {len(impact['impacted_nodes'])} "
            "nodes impacted. "
            "Review callers and dependents carefully."
        )

    # Check for inheritance changes
    inheritance_edges = [
        e for e in impact["edges"]
        if e.kind in ("INHERITS", "IMPLEMENTS")
    ]
    if inheritance_edges:
        guidance_parts.append(
            f"- {len(inheritance_edges)} inheritance/implementation "
            "relationship(s) affected. "
            "Check for Liskov substitution violations."
        )

    # Check for cross-file impact
    impacted_file_count = len(impact["impacted_files"])
    if impacted_file_count > 3:
        guidance_parts.append(
            f"- Changes impact {impacted_file_count} other files."
            " Consider splitting into smaller PRs."
        )

    if not guidance_parts:
        guidance_parts.append(
            "- Changes appear well-contained with minimal blast radius."
        )

    return "\n".join(guidance_parts)


# ---------------------------------------------------------------------------
# Tool 12: get_affected_flows  [REVIEW]
# ---------------------------------------------------------------------------


def get_affected_flows_func(
    changed_files: list[str] | None = None,
    base: str = "HEAD~1",
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Find execution flows affected by changed files.

    [REVIEW] Identifies which execution flows pass through nodes in the
    changed files.  Useful during code review to understand which user-facing
    or critical paths are affected by a change.

    Args:
        changed_files: List of changed file paths (relative to repo root).
                       Auto-detected from git diff if omitted.
        base: Git ref for auto-detecting changes (default: HEAD~1).
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Affected flows sorted by criticality, with step details.
    """
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
                "affected_flows": [],
                "total": 0,
            }

        # Convert to absolute paths for graph lookup
        abs_files = [str(root / f) for f in changed_files]
        result = _get_affected_flows(store, abs_files)

        total = result["total"]
        out = {
            "status": "ok",
            "summary": (
                f"{total} flow(s) affected by changes "
                f"in {len(changed_files)} file(s)"
            ),
            "changed_files": changed_files,
            "affected_flows": result["affected_flows"],
            "total": total,
        }
        out["_hints"] = generate_hints(
            "get_affected_flows", out, get_session()
        )
        return out
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 16: detect_changes  [REVIEW]
# ---------------------------------------------------------------------------


def detect_changes_func(
    base: str = "HEAD~1",
    changed_files: list[str] | None = None,
    include_source: bool = False,
    max_depth: int = 2,
    repo_root: str | None = None,
    detail_level: str = "standard",
    max_tokens: int = 0,
) -> dict[str, Any]:
    """Detect changes and produce risk-scored review guidance.

    [REVIEW] Primary tool for code review.  Maps git diffs to affected
    functions, flows, communities, and test coverage gaps.  Returns
    priority-ordered review guidance with risk scores.

    Args:
        base: Git ref to diff against (default: HEAD~1).
        changed_files: Explicit list of changed file paths (relative to repo
            root).  Auto-detected from git diff if omitted.
        include_source: If True, include source code snippets for changed
            functions.  Default: False.
        max_depth: Impact radius depth for BFS traversal.  Default: 2.
        repo_root: Repository root path.  Auto-detected if omitted.
        detail_level: Output detail level.  "standard" returns full analysis;
            "minimal" returns only summary, risk_score, changed_file_count,
            test_gap_count, and top 3 review priorities (text only).
            Default: "standard".
        max_tokens: Token budget for the standard response (estimated via the
            chars/4 heuristic).  When the analysis would exceed it, the
            lowest-risk changed functions and flows are dropped first and an
            honest ``omitted`` note is reported.  ``0`` (default) disables it.

    Returns:
        Risk-scored analysis with changed functions, affected flows,
        test gaps, and review priorities.
    """
    store, root = _get_store(repo_root)
    try:
        # Detect changed files if not provided.
        if changed_files is None:
            changed_files = get_changed_files(root, base)
            if not changed_files:
                changed_files = get_staged_and_unstaged(root)

        if not changed_files:
            return {
                "status": "ok",
                "summary": "No changed files detected.",
                "risk_score": 0.0,
                "changed_functions": [],
                "affected_flows": [],
                "test_gaps": [],
                "review_priorities": [],
            }

        original_tokens = estimate_file_tokens(root, changed_files)

        # Convert to absolute paths for graph lookup.
        abs_files = [str(root / f) for f in changed_files]

        # Parse diff ranges for line-level mapping.
        diff_ranges = parse_diff_ranges(str(root), base)
        # Remap to absolute paths so they match graph file_paths.
        abs_ranges: dict[str, list[tuple[int, int]]] = {}
        for rel_path, ranges in diff_ranges.items():
            abs_path = str(root / rel_path)
            abs_ranges[abs_path] = ranges

        analysis = analyze_changes(
            store,
            changed_files=abs_files,
            changed_ranges=abs_ranges if abs_ranges else None,
            repo_root=str(root),
            base=base,
        )

        # Optionally include source snippets for changed functions.
        if include_source:
            for func in analysis.get("changed_functions", []):
                fp = func.get("file_path")
                ls = func.get("line_start")
                le = func.get("line_end")
                if fp and ls and le:
                    file_path = Path(fp)
                    if file_path.is_file():
                        try:
                            lines = file_path.read_text(
                                errors="replace"
                            ).splitlines()
                            start = max(0, ls - 1)
                            end = min(len(lines), le)
                            func["source"] = "\n".join(
                                f"{i + 1}: {lines[i]}"
                                for i in range(start, end)
                            )
                        except (OSError, UnicodeDecodeError):
                            func["source"] = "(could not read file)"

        if detail_level == "minimal":
            priorities = analysis.get("review_priorities", [])
            top_priorities = [
                p.get("name", p.get("qualified_name", ""))
                for p in priorities[:3]
            ]
            result: dict[str, Any] = {
                "status": "ok",
                "summary": analysis.get("summary", ""),
                "risk_score": analysis.get("risk_score", 0.0),
                "changed_file_count": len(changed_files),
                "test_gap_count": len(analysis.get("test_gaps", [])),
                "review_priorities": top_priorities,
            }
        else:
            result = {
                "status": "ok",
                "changed_files": changed_files,
                **analysis,
            }
            if max_tokens:
                budget_note = _budget_detect_changes(result, max_tokens)
                if budget_note:
                    result["omitted"] = budget_note
                    summary = result.get("summary", "")
                    result["summary"] = (
                        f"{summary}\n{budget_note['note']}" if summary
                        else budget_note["note"]
                    )
        result["_hints"] = generate_hints(
            "detect_changes", result, get_session()
        )
        attach_context_savings(result, original_tokens=original_tokens)
        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()


def _budget_detect_changes(
    result: dict[str, Any], max_tokens: int
) -> dict[str, Any] | None:
    """Trim a standard detect_changes result to fit ``max_tokens``.

    The big-list fields are trimmed lowest-signal first, in this order:

    1. ``affected_flows``   (kept in upstream criticality order)
    2. ``changed_functions`` (re-ranked highest-risk first)
    3. ``test_gaps``        (a flat list that balloons on large PRs)
    4. ``review_priorities`` (the curated top-N; trimmed last, kept longest)

    The summary scaffold and risk score always survive.  Returns an
    ``omitted`` summary dict, or ``None`` when nothing was dropped.  Mutates
    ``result`` in place.
    """
    if estimate_tokens(result) <= max_tokens:
        return None

    funcs = result.get("changed_functions") or []
    flows = result.get("affected_flows") or []
    gaps = result.get("test_gaps") or []
    prios = result.get("review_priorities") or []
    orig = {
        "changed_functions": len(funcs),
        "affected_flows": len(flows),
        "test_gaps": len(gaps),
        "review_priorities": len(prios),
    }

    # Highest-risk functions first; the other lists keep their upstream order.
    ranked_funcs = sorted(
        funcs, key=lambda f: f.get("risk_score", 0.0), reverse=True
    )

    # Counts kept per field; start with everything, shrink as needed.
    keep = dict(orig)
    sources = {
        "changed_functions": ranked_funcs,
        "affected_flows": flows,
        "test_gaps": gaps,
        "review_priorities": prios,
    }

    def _apply() -> None:
        for f, src in sources.items():
            result[f] = src[: keep[f]]

    def _fits() -> bool:
        _apply()
        return estimate_tokens(result) <= max_tokens

    # Trim each field down to zero in priority order until the payload fits.
    # Binary-search the largest count that still fits to keep this O(log N)
    # per field rather than O(N) decrements on large PRs.
    for field in ("affected_flows", "changed_functions", "test_gaps",
                  "review_priorities"):
        if _fits():
            break
        lo, hi = 0, keep[field]  # hi currently overflows
        while lo < hi:
            mid = (lo + hi + 1) // 2
            keep[field] = mid
            if _fits():
                lo = mid
            else:
                hi = mid - 1
        keep[field] = lo
    _apply()

    dropped = {f: orig[f] - keep[f] for f in orig}
    if not any(dropped.values()):
        return None

    note = (
        f"Token budget ({max_tokens}) reached: omitted "
        f"{dropped['changed_functions']} function(s), "
        f"{dropped['affected_flows']} flow(s), "
        f"{dropped['test_gaps']} test gap(s), and "
        f"{dropped['review_priorities']} review priority(ies). "
        f"Re-run with a higher max_tokens to see them."
    )
    return {**dropped, "max_tokens": max_tokens, "note": note}
