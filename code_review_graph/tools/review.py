"""Tools 4, 12, 16: review context, affected flows, detect changes."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path, PurePath
from typing import Any

from ..changes import (  # noqa: F401
    analyze_changes,
    compute_risk_score,
    parse_diff_ranges,
    parse_git_diff_ranges,
)
from ..context_savings import attach_context_savings, estimate_file_tokens
from ..errors import ChangeDiscoveryError
from ..flows import get_affected_flows as _get_affected_flows
from ..graph import GraphNode, edge_to_dict, node_to_dict
from ..hints import generate_hints, get_session
from ..incremental import (
    discover_review_changes,
    resolve_review_base,
)
from ..parser import is_test_file, normalize_file_path
from ._common import (
    _bounded,
    _error_response,
    _get_store,
    _resolve_graph_file_paths,
    _shown_of,
    _validate_positive_int,
)

logger = logging.getLogger(__name__)

# Hard ceilings shared by the review tools. All three walk the full impact
# radius of a change set, so on a whole-repo diff every list below is
# proportional to the repository, not to the change. The numbers are set
# from measured cost per row against a 5.6k-node graph:
#   node dict ~60 tok, node+risk_score ~118 tok, test gap ~85 tok,
#   source line ~10 tok, flow with full steps ~980 tok, flow metadata ~18 tok.
_MAX_REVIEW_NODES = 100
_MAX_REVIEW_EDGES = 150
_MAX_REVIEW_FILES = 200
_MAX_REVIEW_SOURCE_LINES = 800
_MAX_LINES_PER_FILE = 500
_MAX_CHANGED_FUNCTIONS = 100
_MAX_DETECT_SOURCE_LINES = 600

# The source budget above used to be spent first come first served over the
# changed-file list, which is the order Git happened to emit. One
# alphabetically early 500-line file could take 500 of the 800 lines while the
# riskiest changed function in the pull request got nothing.
#
# Ranking the files by risk fixes who gets served, but not what they get. The
# unit a reviewer reads is a changed region, not a file, and the old snippet
# builder had no idea where the change was: ``changed_nodes`` is every node in
# a changed file, so the merged window ran from the first definition to the
# last and a per-file line cap simply showed the top of it. On a real 7-file,
# 82-hunk diff that spent all 800 lines to show 3 hunks in full. A flat
# per-file floor makes that worse, not better: 20 lines buys a bigger file
# count and a fragment of one hunk per file.
#
# So the budget is allocated per changed region instead:
#   * regions come from the diff hunks when Git can supply them, each widened
#     to its enclosing definition when that definition is small enough to be
#     worth reading whole (``_MAX_WIDEN_SPAN``), and otherwise shown with
#     ``_REGION_CONTEXT`` lines either side;
#   * regions are granted whole, round-robin across the risk-ranked files, so
#     a file with one small change costs one small grant and a file with six
#     hunks gets six turns;
#   * no file may hold more than ``_MAX_SOURCE_SHARE_PER_FILE`` of the total,
#     so one churny file cannot starve the rest;
#   * what is left out is counted and reported, per file, rather than being
#     silently trimmed off the bottom.
_MAX_SOURCE_SHARE_PER_FILE = 0.4
# Lines of context either side of a diff hunk that is not widened to a
# definition. Matches the ``2`` the node-span path has always used, plus
# enough to see the surrounding statement.
_REGION_CONTEXT = 4
# A definition at most this many lines long is shown whole when a hunk lands
# inside it: a 30-line function is worth reading in full, a 400-line one is
# not worth 400 of the 800 shared lines.
_MAX_WIDEN_SPAN = 40
# Ceiling on one granted region, so a single enormous added block cannot take
# the share of every other region in the file.
_MAX_REGION_LINES = 120
# A file the graph knows nothing about and Git reports no hunks for still gets
# its head, exactly as before.
_FALLBACK_HEAD_LINES = 50

# Ranking costs ~6 SQLite queries per node scored, and a whole-repo diff can
# seed thousands of changed nodes. Scoring is therefore round-robin across
# files (every file is scored once before any file is scored twice) under
# both a per-file and a global node budget. Files the budget never reaches
# keep their original position, so the ranking degrades to today's behaviour
# instead of failing.
_MAX_RISK_NODES_PER_FILE = 8
_MAX_RISK_SCORED_NODES = 400
_RISK_SCORED_KINDS = ("Function", "Test", "Class")

# ``get_affected_flows`` in standard mode carries a full ``steps`` list per
# flow (~980 tokens each), so 50 flows is still ~49k tokens — #849 was only
# half-closed by capping the count. The ceiling therefore depends on
# detail_level, the same way query.py caps minimal mode at five results.
_MAX_AFFECTED_FLOWS_STANDARD = 25
_MAX_AFFECTED_FLOWS_MINIMAL = 500
# Flow depth varies hugely between codebases, so a flow *count* alone does
# not bound the response. Steps are filled from the most critical flow
# down until this shared budget runs out; the rest keep their metadata and
# are marked ``steps_omitted``.
_MAX_AFFECTED_FLOW_STEPS = 400
_MAX_DETECT_FLOWS = 200

# ``detect_changes`` embeds affected flows for context, not for flow
# spelunking: every flow carries a full ``steps`` list, which is exactly
# what made get_affected_flows return 247k tokens in #849. Callers who want
# step detail should use get_affected_flows_tool, so the embedded copy keeps
# per-flow metadata only.
_DETECT_FLOW_FIELDS = (
    "id", "name", "criticality", "depth", "node_count", "file_count",
)


def _project(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    """Keep only *fields* on each row, dropping keys the row does not have."""
    return [{k: r[k] for k in fields if k in r} for r in rows]


def _bound_flow_steps(
    flows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Spend a shared step budget across *flows*, most critical first.

    Returns ``(flows, truncated)``. Flows are already sorted by criticality,
    so the ones a reviewer cares about keep their full step list; the tail
    keeps metadata and is marked ``steps_omitted``. Every flow reports
    ``total_steps`` so the untruncated depth is never lost.
    """
    budget = _MAX_AFFECTED_FLOW_STEPS
    truncated = False
    bounded: list[dict[str, Any]] = []
    for flow in flows:
        out = dict(flow)
        steps = out.get("steps") or []
        out["total_steps"] = len(steps)
        if len(steps) > budget:
            out["steps"] = steps[:budget]
            out["steps_omitted"] = True
            truncated = True
        budget -= min(len(steps), budget)
        bounded.append(out)
    return bounded, truncated


def _nodes_by_rel_path(
    root: Path, rel_files: list[str], changed_nodes: list[GraphNode],
) -> dict[str, list[GraphNode]]:
    """Group *changed_nodes* under the relative path the caller asked about.

    The graph stores absolute normalized paths while the tool's inputs and
    outputs are repo-relative, and the two spellings have to be bridged in
    exactly one place or the ranking and the snippets disagree about which
    nodes belong to a file.
    """
    by_path: dict[str, list[GraphNode]] = {}
    for node in changed_nodes:
        if node.file_path:
            by_path.setdefault(node.file_path, []).append(node)

    grouped: dict[str, list[GraphNode]] = {}
    for rel in rel_files:
        joined = root / rel
        grouped[rel] = (
            by_path.get(normalize_file_path(joined))
            or by_path.get(str(joined))
            or by_path.get(normalize_file_path(rel))
            or []
        )
    return grouped


def _risk_by_file(
    store: Any,
    rel_files: list[str],
    nodes_by_rel: dict[str, list[GraphNode]],
) -> dict[str, float]:
    """Score each changed file by the riskiest changed node it contains.

    Uses ``changes.compute_risk_score`` -- the same score ``detect_changes``
    reports -- so the review context and the risk report agree on what
    matters. Scoring is bounded (see ``_MAX_RISK_SCORED_NODES``); unscored
    files keep a score of 0.0 and therefore their original order.
    """
    risks: dict[str, float] = {rel: 0.0 for rel in rel_files}
    if not rel_files:
        return risks

    candidates: dict[str, list[GraphNode]] = {}
    for rel in rel_files:
        scorable: list[GraphNode] = []
        for node in nodes_by_rel.get(rel, ()):
            if node.kind not in _RISK_SCORED_KINDS:
                continue
            if isinstance(node.extra, dict) and node.extra.get("verilog_kind"):
                continue
            scorable.append(node)
            if len(scorable) >= _MAX_RISK_NODES_PER_FILE:
                break
        candidates[rel] = scorable

    remaining = _MAX_RISK_SCORED_NODES
    try:
        for depth in range(_MAX_RISK_NODES_PER_FILE):
            if remaining <= 0:
                break
            progressed = False
            for rel in rel_files:
                if remaining <= 0:
                    break
                nodes = candidates[rel]
                if depth >= len(nodes):
                    continue
                score = compute_risk_score(store, nodes[depth])
                remaining -= 1
                progressed = True
                if score > risks[rel]:
                    risks[rel] = score
            if not progressed:
                break
    except (sqlite3.Error, ValueError, AttributeError):
        # Ranking is an optimisation, never a reason to fail the review
        # context. A partially scored ranking is still better than none.
        logger.warning("Risk ranking failed; falling back to diff order",
                       exc_info=True)
    return risks


def _source_share_cap(total_budget: int, per_file_limit: int) -> int:
    """The most source lines any single file may hold of the shared budget."""
    return max(
        1, min(per_file_limit, int(total_budget * _MAX_SOURCE_SHARE_PER_FILE)),
    )


def _diff_hunks(root: Path, base: str) -> dict[str, list[tuple[int, int]]]:
    """Changed line ranges per repo-relative path, or ``{}`` if Git cannot say.

    ``get_review_context`` seeds its subgraph from whole files, so its
    ``changed_nodes`` list is every node in a changed file and cannot say
    where inside the file the change is. One ``git diff --unified=0`` buys
    that, and it is the difference between spending the source budget on the
    changed code and spending it on whatever happens to sit at the top of the
    file. Failure is not an error: the regions fall back to node spans.
    """
    try:
        return parse_diff_ranges(str(root), base)
    except (OSError, ValueError) as exc:  # pragma: no cover - defensive
        logger.warning("Diff hunk lookup failed: %s", exc)
        return {}


def _file_regions(
    line_count: int,
    hunks: list[tuple[int, int]],
    nodes: list[GraphNode],
    region_cap: int,
) -> list[tuple[int, int]]:
    """The regions of one file worth showing, as 0-based ``[start, end)``.

    Preference order, because each source is better than the next but not
    always available:

    1. *hunks* -- the lines the diff actually changed. A hunk inside a
       definition no longer than ``_MAX_WIDEN_SPAN`` is widened to that whole
       definition; otherwise it gets ``_REGION_CONTEXT`` lines either side.
    2. the changed nodes' own spans, for a file the caller named but the diff
       does not cover (an explicit ``changed_files`` list, a non-Git tree).
    3. the head of the file, for a file the graph does not know either.

    Overlapping regions merge, but only while the merged span stays within
    *region_cap*: merging past that is what turned a multi-hunk file into one
    window whose top ``max_lines_per_file`` lines were all a reviewer saw.
    """
    if line_count <= 0:
        return []
    # A file small enough to fit one region's share is worth showing whole:
    # full context costs no more than the keyhole would.
    if line_count <= region_cap:
        return [(0, line_count)]

    spans: list[tuple[int, int]] = []
    if hunks:
        node_spans = [
            (n.line_start, n.line_end)
            for n in nodes
            if n.line_start and n.line_end and n.line_end >= n.line_start
        ]
        for h_start, h_end in hunks:
            enclosing = [
                (end - start, start, end)
                for start, end in node_spans
                if start <= h_end and end >= h_start
                and end - start + 1 <= _MAX_WIDEN_SPAN
            ]
            if enclosing:
                _, start, end = min(enclosing)
                spans.append((min(h_start, start), max(h_end, end)))
            else:
                spans.append((h_start - _REGION_CONTEXT, h_end + _REGION_CONTEXT))
    else:
        spans = [
            (n.line_start, n.line_end)
            for n in nodes
            if n.line_start and n.line_end and n.line_end >= n.line_start
        ]

    if not spans:
        head = min(_FALLBACK_HEAD_LINES, line_count)
        return [(0, head)] if head > 0 else []

    regions = sorted(
        (start, end)
        for start, end in (
            # 1-based inclusive to 0-based half-open, with two lines of
            # padding either side (what the node-span path always used).
            (max(0, s - 3), min(line_count, e + 2))
            for s, e in spans
        )
        if end > start
    )
    if not regions:
        return []

    merged = [regions[0]]
    for start, end in regions[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + 1 and max(end, last_end) - last_start <= region_cap:
            merged[-1] = (last_start, max(end, last_end))
        else:
            merged.append((start, end))
    return merged


def _allocate_regions(
    ranked_files: list[str],
    regions_by_file: dict[str, list[tuple[int, int]]],
    total_budget: int,
    per_file_limit: int,
) -> tuple[dict[str, list[tuple[int, int]]], dict[str, int]]:
    """Grant whole regions from *total_budget*, round-robin in rank order.

    Round-robin rather than depth-first: every file is offered its first
    region before any file is offered its second, so a one-hunk file costs one
    small grant and a six-hunk file gets six turns. Within a round the
    riskiest file goes first, which is what decides who loses out when the
    budget runs dry.

    A region is granted whole or not at all -- half a hunk is what the old
    per-file line cap delivered and it is not reviewable. The single
    exception is a region larger than the cap on its own, which is truncated
    to the cap because nothing else can be done with it.

    Returns ``({file: granted regions in source order}, {file: regions
    omitted})``.
    """
    granted: dict[str, list[tuple[int, int]]] = {}
    if ranked_files and total_budget > 0 and per_file_limit > 0:
        share_cap = _source_share_cap(total_budget, per_file_limit)
        region_cap = min(_MAX_REGION_LINES, share_cap)
        used: dict[str, int] = dict.fromkeys(ranked_files, 0)
        remaining = total_budget
        depth = 0
        deepest = max(
            (len(regions_by_file.get(rel, ())) for rel in ranked_files),
            default=0,
        )
        while depth < deepest and remaining > 0:
            for rel in ranked_files:
                regions = regions_by_file.get(rel, ())
                if depth >= len(regions):
                    continue
                start, end = regions[depth]
                cost = min(end - start, region_cap)
                if cost <= 0 or cost > min(remaining, share_cap - used[rel]):
                    continue
                granted.setdefault(rel, []).append((start, start + cost))
                used[rel] += cost
                remaining -= cost
            depth += 1
        for regions in granted.values():
            regions.sort()

    omitted = {
        rel: len(regions_by_file.get(rel, ())) - len(granted.get(rel, ()))
        for rel in ranked_files
        if len(regions_by_file.get(rel, ())) > len(granted.get(rel, ()))
    }
    return granted, omitted


def _render_regions(
    lines: list[str],
    regions: list[tuple[int, int]],
    omitted: int,
) -> str:
    """Number and join *regions*, marking the gaps and what was left out."""
    parts: list[str] = []
    previous_end: int | None = None
    for start, end in regions:
        if previous_end is not None and start > previous_end:
            parts.append("...")
        parts.extend(f"{i + 1}: {lines[i]}" for i in range(start, end))
        previous_end = end
    if omitted > 0:
        parts.append(f"... ({omitted} more changed region(s) not shown)")
    return "\n".join(parts)


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
    max_results: int = 50,
    max_files: int = 25,
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
        max_results: Maximum graph nodes per list and edges to return
            (default 50; nodes capped at 200, edges at 300). Each list
            carries its untruncated ``*_total`` count.
        max_files: Maximum files to list and to emit source snippets for
            (default 25, capped at 200). A whole-repo diff otherwise inlined
            every tracked file's source. Snippets additionally share an
            800-line budget and ``max_lines_per_file`` is capped at 500.

    Returns:
        Structured review context with subgraph, source snippets, and
        review guidance, plus a ``truncated`` flag. ``changed_files`` is
        ordered by the risk score ``detect_changes`` reports (highest
        first, reported per file in ``file_risk``), and both the file cap
        and the shared source-line budget are spent in that order. The
        budget buys whole changed regions rather than a per-file line
        quota, and ``source_regions`` reports how many regions each file
        has and how many were shown.
    """
    _validate_positive_int(max_results, "max_results")
    _validate_positive_int(max_files, "max_files")
    _validate_positive_int(max_lines_per_file, "max_lines_per_file")

    store, root = _get_store(repo_root)
    try:
        # The base is resolved on both branches, for the file list and for the
        # hunk lookup below: an explicit ``changed_files`` list still needs a
        # usable base, because the snippets are cut to the regions that base
        # changed. Discovery resolves it as part of the chain, on the short
        # discovery budget, so it is never resolved twice.
        if changed_files is None:
            changed_files, base = discover_review_changes(root, base)
        else:
            base = resolve_review_base(root, base)

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

            # Count test gaps among changed functions. The file path is
            # checked as well as the stored flag so a graph built before
            # the parser marked every test-file node (#1014) does not count
            # test helpers as untested production code. ``root`` makes that
            # check read the path inside the repository: stored paths are
            # absolute, and a checkout under a directory named "test" would
            # otherwise suppress every gap in the repository (#1023).
            changed_funcs = [
                n for n in impact["changed_nodes"]
                if n.kind == "Function"
                and not n.is_test
                and not is_test_file(n.file_path, root)
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
                    "detect_changes_tool",
                    "get_affected_flows_tool",
                    "get_impact_radius_tool",
                ],
            }
            attach_context_savings(result, original_tokens=original_tokens)
            return result

        # Build review context. Every list below scales with the change set,
        # so each is bounded and reports its untruncated total.
        #
        # Rank first, bound second: when ``max_files`` cuts the list it must
        # keep the riskiest files, not the ones Git listed first.
        nodes_by_rel = _nodes_by_rel_path(
            root, changed_files, impact["changed_nodes"],
        )
        file_risk = _risk_by_file(store, changed_files, nodes_by_rel)
        ranked_files = [
            rel for _, rel in sorted(
                ((-file_risk.get(rel, 0.0), i), rel)
                for i, rel in enumerate(changed_files)
            )
        ]
        shown_files, files_total, files_cut = _bounded(
            ranked_files, max_files, _MAX_REVIEW_FILES,
        )
        impacted_files, impacted_total, impacted_cut = _bounded(
            impact["impacted_files"], max_files, _MAX_REVIEW_FILES,
        )
        changed_nodes, changed_nodes_total, cn_cut = _bounded(
            impact["changed_nodes"], max_results, _MAX_REVIEW_NODES,
        )
        impacted_nodes, impacted_nodes_total, in_cut = _bounded(
            impact["impacted_nodes"], max_results, _MAX_REVIEW_NODES,
        )
        edges, edges_total, edges_cut = _bounded(
            impact["edges"], max_results, _MAX_REVIEW_EDGES,
        )
        truncated = (
            files_cut or impacted_cut or cn_cut or in_cut or edges_cut
        )

        context: dict[str, Any] = {
            "changed_files": shown_files,
            "changed_files_total": files_total,
            "file_risk": {
                rel: round(file_risk.get(rel, 0.0), 4) for rel in shown_files
            },
            "impacted_files": impacted_files,
            "impacted_files_total": impacted_total,
            "graph": {
                "changed_nodes": [node_to_dict(n) for n in changed_nodes],
                "changed_nodes_total": changed_nodes_total,
                "impacted_nodes": [node_to_dict(n) for n in impacted_nodes],
                "impacted_nodes_total": impacted_nodes_total,
                "edges": [edge_to_dict(e) for e in edges],
                "edges_total": edges_total,
            },
            "truncated": truncated,
        }

        # Add source snippets for the bounded file list, spending a shared
        # line budget. Snippets were 109k of a 134k-token worst case: without
        # a total budget, ``max_lines_per_file`` alone lets N files each
        # contribute a whole file. The budget now buys whole changed regions,
        # allocated round-robin over the risk-ranked list, so what a reviewer
        # receives is complete hunks rather than the top of a merged window.
        if include_source:
            per_file = min(max_lines_per_file, _MAX_LINES_PER_FILE)
            region_cap = min(
                _MAX_REGION_LINES,
                _source_share_cap(_MAX_REVIEW_SOURCE_LINES, per_file),
            )
            hunks = _diff_hunks(root, base)
            file_lines: dict[str, list[str]] = {}
            regions_by_file: dict[str, list[tuple[int, int]]] = {}
            snippets: dict[str, str] = {}
            for rel_path in shown_files:
                full_path = root / rel_path
                if not full_path.is_file():
                    continue
                try:
                    lines = full_path.read_text(
                        encoding="utf-8", errors="replace",
                    ).splitlines()
                except (OSError, UnicodeDecodeError):
                    snippets[rel_path] = "(could not read file)"
                    continue
                file_lines[rel_path] = lines
                regions_by_file[rel_path] = _file_regions(
                    len(lines),
                    # Git always names paths with forward slashes; the
                    # caller's list may not (Windows, or an explicit
                    # ``changed_files``). A miss is not fatal -- the regions
                    # fall back to node spans -- but it costs the reviewer
                    # the hunks, so try the POSIX spelling too.
                    hunks.get(rel_path) or hunks.get(
                        PurePath(rel_path).as_posix(), [],
                    ),
                    nodes_by_rel.get(rel_path, []),
                    region_cap,
                )

            granted, omitted = _allocate_regions(
                shown_files, regions_by_file,
                _MAX_REVIEW_SOURCE_LINES, per_file,
            )
            shown_regions = 0
            total_regions = 0
            clipped = False
            incomplete: dict[str, list[int]] = {}
            for rel_path in shown_files:
                if rel_path not in file_lines:
                    continue
                regions = regions_by_file.get(rel_path, [])
                held = granted.get(rel_path, [])
                have = len(held)
                want = len(regions)
                shown_regions += have
                total_regions += want
                if have < want:
                    incomplete[rel_path] = [have, want]
                # A region too big for the cap is served clipped rather than
                # dropped, which is still a cut and still has to be declared.
                clipped = clipped or any(r not in set(regions) for r in held)
                if not have:
                    continue
                snippets[rel_path] = _render_regions(
                    file_lines[rel_path],
                    granted[rel_path],
                    omitted.get(rel_path, 0),
                )
            context["source_snippets"] = snippets
            # What the budget could not buy is reported, not hidden: a
            # reviewer who can see that 38 of 41 regions are missing knows to
            # ask for the rest instead of assuming they read the change.
            context["source_regions"] = {
                "shown": shown_regions,
                "total": total_regions,
                "incomplete": incomplete,
            }
            if shown_regions < total_regions or clipped:
                context["source_truncated"] = True
                context["truncated"] = True

        # Generate review guidance
        guidance = _generate_review_guidance(impact, changed_files, root)
        context["review_guidance"] = guidance

        summary_parts = [
            f"Review context for {files_total} changed file(s)"
            + _shown_of(len(shown_files), files_total) + ":",
            f"  - {changed_nodes_total} directly changed nodes"
            + _shown_of(len(changed_nodes), changed_nodes_total),
            f"  - {impacted_nodes_total} impacted nodes"
            f" in {impacted_total} files"
            + _shown_of(len(impacted_nodes), impacted_nodes_total),
            "",
            "Review guidance:",
            guidance,
        ]

        result = {
            "status": "ok",
            "summary": "\n".join(summary_parts),
            "context": context,
        }
        attach_context_savings(result, original_tokens=original_tokens)
        return result
    except ChangeDiscoveryError as exc:
        # Distinct from the "no changed files" answer above, and deliberately
        # so: that one is an all-clear a client will act on. Git that could
        # not be run, or that overran the discovery budget, says nothing
        # about the working tree (#262).
        return _error_response(str(exc))
    finally:
        store.close()


def _generate_review_guidance(
    impact: dict, changed_files: list[str], repo_root: "str | Path | None" = None,
) -> str:
    """Generate review guidance based on the impact analysis.

    *repo_root* is what makes the test-file check read a project path rather
    than an absolute one. Without it, directory conventions are skipped for
    absolute paths, which can leave a test helper in the untested list but
    never hides a production gap. See #1023.
    """
    guidance_parts = []

    # Check for test coverage
    changed_funcs = [
        n for n in impact["changed_nodes"] if n.kind == "Function"
    ]
    test_edges = [e for e in impact["edges"] if e.kind == "TESTED_BY"]
    tested_funcs = {e.source_qualified for e in test_edges}

    untested = [
        f for f in changed_funcs
        if f.qualified_name not in tested_funcs
        and not f.is_test
        and not is_test_file(f.file_path, repo_root)
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
    detail_level: str = "standard",
    max_flows: int = 50,
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
        detail_level: "standard" for full step details, "minimal" for
            per-flow metadata only (name, criticality, depth, counts).
            Every flow carries a full ``steps`` list in standard mode, so
            large change sets can exceed 200k tokens without a bound (#849).
        max_flows: Maximum flows to return (default: 50). ``total`` always
            reports the untruncated count; 0 means "no caller limit".
            Standard mode additionally caps the visible flows at 25 and
            minimal mode at 500, because one standard flow costs ~980
            tokens against ~18 for a minimal one. This mirrors the way
            query.py caps minimal-mode results at five. Standard mode also
            spends a shared 400-step budget across the returned flows, so
            a codebase with very deep call chains cannot blow the budget
            with a legal flow count.

    Returns:
        Affected flows sorted by criticality; ``truncated`` is set when
        ``max_flows``, the per-detail-level ceiling, or the step budget cut
        the response.
    """
    store, root = _get_store(repo_root)
    try:
        if changed_files is None:
            changed_files, base = discover_review_changes(root, base)

        if not changed_files:
            return {
                "status": "ok",
                "summary": "No changed files detected.",
                "affected_flows": [],
                "total": 0,
                "truncated": False,
            }

        # Convert to absolute paths for graph lookup. Graph identity uses
        # POSIX separators (#774), so normalize the joined paths.
        abs_files = [normalize_file_path(root / f) for f in changed_files]
        result = _get_affected_flows(store, abs_files)

        total = result["total"]
        flows = result["affected_flows"]
        ceiling = (
            _MAX_AFFECTED_FLOWS_MINIMAL if detail_level == "minimal"
            else _MAX_AFFECTED_FLOWS_STANDARD
        )
        # ``max_flows=0`` keeps its documented "no caller limit" meaning, but
        # the ceiling still applies -- an escape hatch that can return 250k
        # tokens is the bug #849 reported, not a feature.
        limit = ceiling if max_flows <= 0 else min(max_flows, ceiling)
        truncated = total > limit
        flows = flows[:limit]
        if detail_level == "minimal":
            flows = _project(flows, _DETECT_FLOW_FIELDS)
        else:
            flows, steps_cut = _bound_flow_steps(flows)
            truncated = truncated or steps_cut
        out = {
            "status": "ok",
            "summary": (
                f"{total} flow(s) affected by changes "
                f"in {len(changed_files)} file(s)"
                + _shown_of(len(flows), total)
            ),
            "changed_files": changed_files,
            "affected_flows": flows,
            "total": total,
            "truncated": truncated,
        }
        out["_hints"] = generate_hints(
            "get_affected_flows_tool", out, get_session()
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
    max_results: int = 25,
    max_flows: int = 20,
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
        max_results: Maximum changed functions and test gaps to return
            (default 25, capped at 200). ``changed_functions_total`` and
            ``test_gaps_total`` report the untruncated counts.
        max_flows: Maximum affected flows to embed (default 20, capped at
            200). The embedded flows carry per-flow metadata only; use
            get_affected_flows_tool for step detail. See #849.

    Returns:
        Risk-scored analysis with changed functions, affected flows,
        test gaps, and review priorities, plus ``truncated``.
    """
    _validate_positive_int(max_results, "max_results")
    _validate_positive_int(max_flows, "max_flows")

    store, root = _get_store(repo_root)
    try:
        # Detect changed files if not provided.
        if changed_files is None:
            # discover_review_changes carries require_vcs through the whole
            # chain: the "no changed files" answer below is an all-clear, and
            # a git that could not be run (or overran the discovery budget)
            # must not produce it. The ChangeDiscoveryError becomes
            # {"status": "error"} instead, so a client can tell "nothing to
            # review" from "could not look".
            changed_files, base = discover_review_changes(root, base)
        else:
            base = resolve_review_base(root, base)

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

        # Convert to absolute paths for graph lookup. Graph identity uses
        # POSIX separators (#774), so normalize the joined paths.
        abs_files = [normalize_file_path(root / f) for f in changed_files]

        # Parse diff ranges for line-level mapping.
        # Lenient on purpose: the changed-file list above is already known
        # to be non-empty, so an unreadable line-level diff costs precision,
        # not honesty. analyze_changes records the degradation.
        diff_ranges = parse_diff_ranges(str(root), base)
        # Remap to absolute paths so they match graph file_paths.
        abs_ranges: dict[str, list[tuple[int, int]]] = {}
        for rel_path, ranges in diff_ranges.items():
            abs_path = normalize_file_path(root / rel_path)
            abs_ranges[abs_path] = ranges

        analysis = analyze_changes(
            store,
            changed_files=abs_files,
            changed_ranges=abs_ranges if abs_ranges else None,
            repo_root=str(root),
            base=base,
            # Agent-driven reviews used to score the change-frequency term at
            # zero because only the CLI passed this. The underlying git log is
            # memoised per commit, capped, and fails soft, so a cold first run
            # costs one bounded walk and every later call is free.
            include_churn=True,
        )

        # Optionally include source snippets for changed functions, spending a
        # shared line budget. Inlining every changed function body turned a
        # whole-repo diff into a 30k-token ``changed_functions`` list.
        if include_source:
            budget = _MAX_DETECT_SOURCE_LINES
            for func in analysis.get("changed_functions", []):
                if budget <= 0:
                    break
                fp = func.get("file_path")
                ls = func.get("line_start")
                le = func.get("line_end")
                if fp and ls and le:
                    file_path = Path(fp)
                    if file_path.is_file():
                        try:
                            lines = file_path.read_text(encoding="utf-8",
                                errors="replace"
                            ).splitlines()
                            start = max(0, ls - 1)
                            end = min(len(lines), le, start + budget)
                            func["source"] = "\n".join(
                                f"{i + 1}: {lines[i]}"
                                for i in range(start, end)
                            )
                            budget -= max(0, end - start)
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
                # Minimal mode still has to say when the score it reports is
                # missing the change-frequency term.
                "churn_status": analysis.get("churn_status", "off"),
            }
        else:
            funcs, funcs_total, funcs_cut = _bounded(
                analysis.get("changed_functions", []),
                max_results, _MAX_CHANGED_FUNCTIONS,
            )
            gaps, gaps_total, gaps_cut = _bounded(
                analysis.get("test_gaps", []),
                max_results, _MAX_CHANGED_FUNCTIONS,
            )
            flows, flows_total, flows_cut = _bounded(
                analysis.get("affected_flows", []),
                max_flows, _MAX_DETECT_FLOWS,
            )
            files, files_total, files_cut = _bounded(
                changed_files, max_results, _MAX_REVIEW_FILES,
            )
            any_cut = funcs_cut or gaps_cut or flows_cut or files_cut
            summary = analysis.get("summary", "")
            if any_cut:
                summary += (
                    "\n  - Response bounded: "
                    f"{len(funcs)} of {funcs_total} changed function(s), "
                    f"{len(gaps)} of {gaps_total} test gap(s), "
                    f"{len(flows)} of {flows_total} flow(s) shown"
                )
            result = {
                "status": "ok",
                **analysis,
                "summary": summary,
                "changed_files": files,
                "changed_file_count": files_total,
                "changed_functions": funcs,
                "changed_functions_total": funcs_total,
                "test_gaps": gaps,
                "test_gaps_total": gaps_total,
                "affected_flows": _project(flows, _DETECT_FLOW_FIELDS),
                "affected_flows_total": flows_total,
                "truncated": any_cut,
            }
        result["_hints"] = generate_hints(
            "detect_changes_tool", result, get_session()
        )
        attach_context_savings(result, original_tokens=original_tokens)
        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()
