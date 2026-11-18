#!/usr/bin/env python3
"""Render a risk-scored PR comment from ``code-review-graph detect-changes`` JSON.

Reads the JSON document printed by ``code-review-graph detect-changes
--base <ref>`` (the full, non ``--brief`` output) and emits GitHub-flavoured
markdown suitable for a sticky pull-request comment. Also implements the
risk gate behind the composite action's ``fail-on-risk`` input.

The first line of the rendered body is a hidden HTML marker so the action
can find and update its own comment instead of posting a new one each run.

Exit codes:
    0  rendered successfully (gate passed or disabled)
    2  the input file could not be read
    3  risk gate breached (``--fail-on-risk high|critical``)
    4  detect-changes did not produce an analysis at all

Exit 4 exists because "no changes" and "could not determine the changes"
must not render as the same comment. ``detect-changes`` prints exactly
``No changes detected.`` for a genuinely clean tree and exits 0; anything
else non-JSON on its stdout means the analysis never happened, and a review
gate that rendered a reassuring comment for that would be waving through a
pull request nobody looked at.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger("render_pr_comment")

MARKER = "<!-- code-review-graph-report -->"
REPO_URL = "https://github.com/tirth8205/code-review-graph"
FOOTER = (
    f"*Powered by [code-review-graph]({REPO_URL}) — "
    "local-first analysis; no code leaves the CI runner.*"
)

# Risk-level cutoffs over analyze_changes' 0.0-1.0 risk_score.
RISK_THRESHOLDS: dict[str, float] = {"critical": 0.85, "high": 0.7, "medium": 0.4}

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_CELL = 120
# GitHub rejects comment bodies over 65,536 characters, and the trusted
# workflow that publishes this report (.github/workflows/pr-review-comment.yml)
# rejects an artifact larger than MAX_REPORT_BYTES. This is that same budget,
# and it is a budget for the *finished* body: the truncation notice and the
# footer have to fit inside it, or every truncated report is rejected by the
# consumer and the largest pull requests get no comment at all. Measured in
# UTF-8 bytes, because that is what the consumer measures.
_MAX_BODY = 60_000
# main() writes the body followed by a newline, and the consumer measures the
# file on disk, not the string this module returns. That newline is therefore
# part of what is measured and one byte of the budget belongs to it: without
# this reservation a report sized at exactly _MAX_BODY becomes a 60,001-byte
# artifact and is rejected by the very check the budget exists to satisfy.
_ARTIFACT_NEWLINE_BYTES = len(b"\n")
#: What the body itself may occupy, once the artifact's newline is reserved.
_MAX_BODY_TEXT = _MAX_BODY - _ARTIFACT_NEWLINE_BYTES

#: Opt-in graph context sections (``--details``), in render order. The same
#: tuple as ``code_review_graph.change_details.DETAIL_SECTIONS``; this script
#: runs on the standard library alone, so it keeps a copy, and a test holds
#: the two equal.
DETAIL_SECTIONS: tuple[str, ...] = (
    "risk", "communities", "hubs", "coupling", "impact", "gaps", "graph",
)
#: Rows shown per list in a graph context section.
_DETAIL_ROWS = 5
#: Callers named per symbol in the impact section.
_DETAIL_CALLERS = 3
#: Hard cap, in UTF-8 bytes, on the whole graph context block: the wrapper,
#: every section and the note saying what was cut. The block is also never
#: given more room than the body budget has left, so the report it follows
#: is never cut to make space for it.
_MAX_DETAILS_BYTES = 12_000


def risk_level(score: float) -> str:
    """Map a 0.0-1.0 risk score to a named level."""
    if score >= RISK_THRESHOLDS["critical"]:
        return "critical"
    if score >= RISK_THRESHOLDS["high"]:
        return "high"
    if score >= RISK_THRESHOLDS["medium"]:
        return "medium"
    return "low"


def relativize_path(value: Any) -> str:
    """Strip CI-runner absolute prefixes so paths render repo-relative.

    detect-changes emits paths exactly as the graph stored them; in CI the
    repo is checked out under an absolute prefix
    (``/home/runner/work/<repo>/<repo>/...``), which is ugly in a PR comment
    and leaks the runner layout. We strip that prefix so the reader sees
    ``code_review_graph/embeddings.py`` instead.

    Handles both bare paths and ``path::symbol`` qualified names (the
    ``::symbol`` suffix is preserved). Already-relative paths and non-path
    tokens (``?``) are returned unchanged.

    Strategy, in order:
      1. ``GITHUB_WORKSPACE`` prefix (set by Actions ``checkout``).
      2. The ``<repo>/<repo>/`` doubled-segment that Actions uses under
         ``/work/`` (derived from ``GITHUB_REPOSITORY`` when present).
      3. Otherwise return the input untouched — never guess-mangle a path.
    """
    text = str(value)
    path_part, sep, symbol = text.partition("::")
    # Only touch absolute, POSIX-style runner paths; leave everything else.
    if not path_part.startswith("/"):
        return text

    rel = _strip_workspace_prefix(path_part)
    if rel is None:
        return text
    return f"{rel}{sep}{symbol}" if sep else rel


def _strip_workspace_prefix(path_part: str) -> str | None:
    """Return ``path_part`` made repo-relative, or None when nothing matches."""
    workspace = os.environ.get("GITHUB_WORKSPACE", "").strip()
    if workspace:
        prefix = workspace.rstrip("/") + "/"
        if path_part.startswith(prefix):
            return path_part[len(prefix):]

    # Fallback: Actions checks repos out at /work/<name>/<name>/<files...>.
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    name = repo.split("/")[-1] if "/" in repo else ""
    if name:
        marker = f"/{name}/{name}/"
        idx = path_part.find(marker)
        if idx != -1:
            return path_part[idx + len(marker):]

    return None


def md_escape(value: Any, limit: int = _MAX_CELL) -> str:
    """Escape a value for safe inclusion in markdown tables and lists.

    Strips control characters, collapses newlines, escapes table/markup
    characters, and caps the length. Graph node names are already sanitized
    by ``_sanitize_name`` server-side; this is the defensive second layer
    for fields (like file paths) that are not.
    """
    text = str(value)
    text = _CONTROL_CHARS.sub("", text)
    text = text.replace("\r", " ").replace("\n", " ")
    text = text.replace("\\", "\\\\")
    for ch in ("|", "`", "*", "_", "[", "]", "<", ">"):
        text = text.replace(ch, "\\" + ch)
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


def _location(entry: dict[str, Any]) -> str:
    """Format ``file:line`` for a node-ish dict (file_path/file + line_start).

    Paths are relativized first so CI-runner absolute prefixes don't leak.
    """
    file_path = relativize_path(entry.get("file_path") or entry.get("file") or "?")
    line_start = entry.get("line_start")
    if line_start:
        return f"{md_escape(file_path)}:{line_start}"
    return md_escape(file_path)


def _functions_table(
    priorities: list[dict[str, Any]],
    gap_names: set[str],
    max_functions: int,
    indirect_names: set[str] | None = None,
    gaps_truncated: bool = False,
) -> list[str]:
    indirect_names = indirect_names or set()
    lines = [
        "### Risk-scored changes",
        "",
        "| Risk | Level | Symbol | Location | Tested |",
        "| ---: | :--- | :--- | :--- | :---: |",
    ]
    for entry in priorities[:max_functions]:
        score = float(entry.get("risk_score") or 0.0)
        name = entry.get("qualified_name") or entry.get("name") or "?"
        if entry.get("is_test"):
            tested = "(test)"
        elif name in indirect_names:
            # Distinct from both "no" and "yes": a test runs through this
            # symbol but nothing asserts on it directly.
            tested = "indirect"
        elif name in gap_names:
            tested = "no"
        elif gaps_truncated:
            # Absence from a truncated gap list proves nothing. Claiming "yes"
            # here is the strongest possible overclaim: a symbol the report
            # itself classified as a gap renders as one with its own tests.
            tested = "?"
        else:
            tested = "yes"
        lines.append(
            f"| {score:.2f} | {risk_level(score)} | {md_escape(relativize_path(name))} "
            f"| {_location(entry)} | {tested} |"
        )
    if len(priorities) > max_functions:
        lines.append("")
        lines.append(f"...and {len(priorities) - max_functions} more changed symbol(s).")
    return lines


def _flows_section(flows: list[dict[str, Any]], max_flows: int) -> list[str]:
    lines = ["### Affected execution flows", ""]
    for flow in flows[:max_flows]:
        name = md_escape(flow.get("name") or "?")
        criticality = flow.get("criticality")
        crit_txt = (
            f"criticality {float(criticality):.2f}"
            if criticality is not None
            else "criticality n/a"
        )
        node_count = flow.get("node_count", "?")
        file_count = flow.get("file_count", "?")
        lines.append(
            f"- **{name}** — {crit_txt}, {node_count} node(s) across {file_count} file(s)"
        )
    if len(flows) > max_flows:
        lines.append(f"- ...and {len(flows) - max_flows} more affected flow(s)")
    return lines


def _removed_section(
    removed: list[dict[str, Any]], total: int, max_rows: int = 10,
) -> list[str]:
    """Symbols the diff deleted, and what the graph says still uses them."""
    lines = [
        "### Removed symbols",
        "",
        "| Risk | Level | Symbol | Was at | Still used by |",
        "| ---: | :--- | :--- | :--- | :--- |",
    ]
    for entry in removed[:max_rows]:
        name = entry.get("qualified_name") or entry.get("name") or "?"
        location = md_escape(relativize_path(entry.get("file") or "?"))
        if entry.get("old_line_start"):
            location += f":{entry['old_line_start']}"
        users = [
            md_escape(relativize_path(u.get("qualified_name") or u.get("name") or "?"), 80)
            for u in entry.get("used_by") or []
        ]
        users_total = entry.get("used_by_total")
        if not isinstance(users_total, int):
            users_total = len(users)
        if users_total:
            cell = ", ".join(users)
            if users_total > len(users):
                cell += f" +{users_total - len(users)} more"
        else:
            cell = "nothing found"
        score = float(entry.get("risk_score") or 0.0)
        lines.append(
            f"| {score:.2f} | {risk_level(score)} | {md_escape(relativize_path(name))} "
            f"| {location} | {cell} |"
        )
    if total > min(len(removed), max_rows):
        lines.append("")
        lines.append(f"...and {total - min(len(removed), max_rows)} more removed symbol(s).")
    return lines


def _dedup_gaps(gaps: list[dict[str, Any]], max_gaps: int) -> list[dict[str, Any]]:
    seen: set[str] = set()
    shown: list[dict[str, Any]] = []
    for gap in gaps:
        name = str(gap.get("qualified_name") or gap.get("name") or "?")
        if name in seen:
            continue
        seen.add(name)
        shown.append(gap)
        if len(shown) >= max_gaps:
            break
    return shown


def _gaps_section(
    gaps: list[dict[str, Any]],
    max_gaps: int = 5,
    uncovered_total: int | None = None,
    indirect_total: int | None = None,
) -> list[str]:
    """Render the gap list, keeping the two coverage claims apart.

    A symbol with no tested caller anywhere near it and one a test only reaches
    through its caller are different asks -- write a test versus add an
    assertion -- so they get separate headings rather than one undifferentiated
    "untested" list. Entries from an older report with no ``coverage`` key fall
    into the unreached group, which is what they meant before the field existed.

    The totals come from the report when it supplies them, because ``gaps`` is
    bounded by every consumer and counting the rendered rows understates a
    truncated report.
    """
    unreached = [g for g in gaps if g.get("coverage") != "indirect"]
    indirect = [g for g in gaps if g.get("coverage") == "indirect"]
    if not isinstance(uncovered_total, int):
        uncovered_total = len(unreached)
    if not isinstance(indirect_total, int):
        indirect_total = len(indirect)

    lines: list[str] = []
    if unreached or not indirect:
        lines.extend(["### Test gaps", ""])
        shown = _dedup_gaps(unreached, max_gaps)
        for gap in shown:
            name = gap.get("qualified_name") or gap.get("name") or "?"
            lines.append(f"- {md_escape(relativize_path(name))} ({_location(gap)})")
        # "no direct test", not "no test in reach". The graph knows its own
        # TESTED_BY edges and CALLS paths; it does not know the test suite. A
        # test that reaches production code through importlib, a fixture or a
        # subprocess leaves no edge, and two such symbols sit in this very
        # list on the delta that motivated the split.
        remaining = uncovered_total - len(shown)
        if remaining > 0:
            lines.append(f"- ...and {remaining} more with no direct test")

    if indirect:
        if lines:
            lines.append("")
        lines.extend([
            "### Reached only through a caller",
            "",
            "No test names these directly, but a tested caller reaches them "
            "along the call path shown. That is a path in the call graph, not "
            "a record of execution -- the caller's tests may never take this "
            "branch. Treat it as where to look, not as coverage: these count "
            "as gaps and are scored as untested.",
            "",
        ])
        shown = _dedup_gaps(indirect, max_gaps)
        for gap in shown:
            name = gap.get("qualified_name") or gap.get("name") or "?"
            via = str(gap.get("covered_via") or "?")
            depth = gap.get("covered_depth")
            hops = f"{depth} hop(s)" if depth else "caller"
            lines.append(
                f"- {md_escape(relativize_path(name))} ({_location(gap)}) — via "
                f"{md_escape(relativize_path(via))}, {hops}"
            )
        remaining = indirect_total - len(shown)
        if remaining > 0:
            lines.append(f"- ...and {remaining} more reached only through a caller")
    elif indirect_total:
        # The headline promised this class; say where it went rather than
        # letting the section vanish and the two disagree.
        if lines:
            lines.append("")
        lines.append(
            f"_{indirect_total} more gap(s) are reached only through a caller; "
            "the list above was truncated before them._"
        )
    return lines


def render_markdown(
    report: dict[str, Any],
    *,
    max_functions: int = 10,
    max_flows: int = 5,
    details: Sequence[str] = (),
) -> str:
    """Render the detect-changes JSON report as a markdown PR comment.

    *details* names the opt-in graph context sections to add (see
    :data:`DETAIL_SECTIONS`). Empty, the default, renders exactly the
    comment this function rendered before the option existed, whatever the
    report carries.
    """
    score = float(report.get("risk_score") or 0.0)
    changed = report.get("changed_functions") or []
    flows = report.get("affected_flows") or []
    gaps = report.get("test_gaps") or []
    priorities = report.get("review_priorities") or changed
    gap_names = {
        str(g.get("qualified_name") or g.get("name") or "") for g in gaps
    }
    indirect_names = {
        str(g.get("qualified_name") or g.get("name") or "")
        for g in gaps
        if g.get("coverage") == "indirect"
    }
    # The counts come from the report when it supplies them: every consumer
    # bounds ``test_gaps``, so counting the rendered list would understate a
    # truncated report.
    indirect_total = report.get("test_gaps_indirect")
    if not isinstance(indirect_total, int):
        indirect_total = len(indirect_names)
    uncovered_total = report.get("test_gaps_uncovered")
    if not isinstance(uncovered_total, int):
        uncovered_total = len(gaps) - indirect_total
    # The headline total has to come from the same place as the split, or a
    # truncated report prints "25 test gap(s) (73 ..., 9 ...)" -- a number
    # beside its own parts, which do not add up to it. ``test_gaps`` is
    # bounded by every consumer; these counts are not.
    gaps_total = report.get("test_gaps_total")
    if not isinstance(gaps_total, int):
        gaps_total = uncovered_total + indirect_total
    truncated = gaps_total > len(gaps)

    removed = report.get("removed_symbols") or []
    removed_total = report.get("removed_symbols_total")
    if not isinstance(removed_total, int):
        removed_total = len(removed)

    lines: list[str] = [MARKER, "", "## code-review-graph review", ""]
    gap_text = f"{gaps_total} test gap(s)"
    if indirect_total:
        gap_text += (
            f" ({uncovered_total} with no tested caller found, "
            f"{indirect_total} reached only through a caller)"
        )
    lines.append(
        f"**Overall risk: {score:.2f} ({risk_level(score).upper()})** — "
        f"{len(changed)} changed function(s)/class(es), "
        # The score can come from a removal alone, so say there was one.
        + (f"{removed_total} removed, " if removed_total else "")
        + f"{len(flows)} affected flow(s), {gap_text}"
    )

    if priorities:
        lines.append("")
        lines.extend(
            _functions_table(
                priorities, gap_names, max_functions, indirect_names, truncated
            )
        )
    if removed:
        lines.append("")
        lines.extend(_removed_section(removed, removed_total))
    if flows:
        lines.append("")
        lines.extend(_flows_section(flows, max_flows))
    if gaps:
        lines.append("")
        lines.extend(
            _gaps_section(
                gaps,
                uncovered_total=uncovered_total,
                indirect_total=indirect_total,
            )
        )

    savings = report.get("context_savings") or {}
    saved_tokens = savings.get("saved_tokens")
    saved_percent = savings.get("saved_percent")
    if saved_tokens and saved_percent is not None:
        lines.append("")
        lines.append(
            f"**Token savings:** this graph-backed report used ~{int(saved_tokens):,} "
            f"fewer tokens (~{int(saved_percent)}%) than reading every changed file in "
            "full (estimated, chars/4 approximation)."
        )

    if report.get("functions_truncated"):
        lines.append("")
        lines.append(
            f"> Note: analysis covered {len(changed)} of {report['changed_functions_total']} "
            "changed functions (set `CRG_MAX_CHANGED_FUNCS` to adjust)."
        )

    tail = ["", "---", "", FOOTER]
    if details:
        # The block joins as "\n\n<block>", two bytes more than its text. It
        # gets whatever the body budget has left and never more than its own
        # cap, so it can never push the report itself into truncation.
        base = len("\n".join(lines + tail).encode("utf-8"))
        room = min(_MAX_DETAILS_BYTES, _MAX_BODY_TEXT - base - 2)
        block = _details_block(report, details, room)
        if block:
            lines.append("")
            lines.extend(block)
    lines.extend(tail)
    return _fit_to_budget("\n".join(lines))


# ---------------------------------------------------------------------------
# Opt-in graph context (``--details``)
# ---------------------------------------------------------------------------


def parse_details(value: str) -> tuple[str, ...]:
    """Parse a comma-separated section list; ``all`` means every section.

    Mirrors ``code_review_graph.change_details.parse_detail_sections``: the
    result follows :data:`DETAIL_SECTIONS`, an empty value is no sections,
    and an unknown name raises :class:`ValueError`.
    """
    names = [part.strip().lower() for part in str(value).split(",") if part.strip()]
    unknown = [name for name in names if name != "all" and name not in DETAIL_SECTIONS]
    if unknown:
        raise ValueError(
            f"unknown detail section(s): {', '.join(unknown)}; "
            f"choose from {', '.join(DETAIL_SECTIONS)}, or all"
        )
    if "all" in names:
        return DETAIL_SECTIONS
    return tuple(name for name in DETAIL_SECTIONS if name in names)


def _details_arg(value: str) -> tuple[str, ...]:
    try:
        return parse_details(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _utf8_len(lines: list[str]) -> int:
    return len("\n".join(lines).encode("utf-8"))


def _num(value: Any) -> str:
    """An integer with thousands separators, or ``?`` for anything else."""
    if isinstance(value, bool) or not isinstance(value, int):
        return "?"
    return f"{value:,}"


def _name(value: Any) -> str:
    return md_escape(relativize_path(value or "?"))


def _rows(items: Any) -> list[dict[str, Any]]:
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _communities(count: int) -> str:
    return f"{count:,} other {'community' if count == 1 else 'communities'}"


def _more(lines: list[str], total: Any, shown: int, what: str) -> None:
    if isinstance(total, int) and not isinstance(total, bool) and total > shown:
        lines.append(f"- ...and {total - shown:,} more {what}")


_SIGNAL_ORDER = ("flows", "cross_community", "tests", "security", "callers", "churn")


def _signal_label(key: str, count: int) -> str:
    if key == "flows":
        return f"in {count} flow(s)"
    if key == "cross_community":
        return f"{count} caller(s) from other communities"
    if key == "tests":
        if count == 0:
            return "no tests"
        return f"only {count} test(s)" if count < 5 else f"{count} tests"
    if key == "security":
        return "security-related name"
    if key == "callers":
        return f"called from {count} place(s)"
    if key == "churn":
        return f"{count} recent commit(s) to its file"
    return f"{md_escape(key, limit=40)} ({count})"


def _section_risk(data: Any) -> list[str]:
    rows = _rows(data)
    lines = [
        "#### Why these risk scores",
        "",
        "The riskiest changed symbols. Each score is the sum of these signals, "
        "largest first.",
        "",
    ]
    if not rows:
        lines.append("_No changed symbol to explain._")
        return lines
    for entry in rows[:_DETAIL_ROWS]:
        signals = entry.get("signals") if isinstance(entry.get("signals"), dict) else {}
        parts: list[tuple[float, int, str]] = []
        total = 0.0
        for key, value in signals.items():
            if not isinstance(value, dict):
                continue
            try:
                score = float(value.get("score") or 0.0)
                count = int(value.get("count") or 0)
            except (TypeError, ValueError):
                continue
            total += score
            if score > 0:
                order = _SIGNAL_ORDER.index(key) if key in _SIGNAL_ORDER else len(_SIGNAL_ORDER)
                parts.append((-score, order, f"{_signal_label(str(key), count)} +{score:.2f}"))
        parts.sort()
        try:
            risk = float(entry.get("risk_score") or 0.0)
        except (TypeError, ValueError):
            risk = 0.0
        reasons = ", ".join(text for _, _, text in parts) or "no signal"
        capped = (
            f" (the signals add up to {total:.2f}, capped at 1.00)"
            if total > 1.0 + 1e-9 else ""
        )
        lines.append(f"- **{_name(entry.get('qualified_name'))}** ({risk:.2f}): {reasons}{capped}")
    return lines


def _section_communities(data: Any) -> list[str]:
    data = data if isinstance(data, dict) else {}
    rows = _rows(data.get("touched"))
    total = data.get("touched_total")
    lines = [
        "#### Communities touched",
        "",
        f"The change touches {_num(total)} of {_num(data.get('in_graph'))} communities.",
        "",
    ]
    for entry in rows[:_DETAIL_ROWS]:
        lines.append(
            f"- **{md_escape(entry.get('name') or '?')}** ({_num(entry.get('size'))} "
            f"symbol(s)): {_num(entry.get('changed'))} changed"
        )
    shown = min(len(rows), _DETAIL_ROWS)
    known = total if isinstance(total, int) and not isinstance(total, bool) else len(rows)
    _more(lines, max(known, len(rows)), shown, "communities")
    unassigned = data.get("unassigned")
    if isinstance(unassigned, int) and not isinstance(unassigned, bool) and unassigned > 0:
        lines.extend(["", f"_{unassigned:,} changed symbol(s) belong to no community._"])
    return lines


def _section_hubs(data: Any) -> list[str]:
    data = data if isinstance(data, dict) else {}
    rows = _rows(data.get("nodes"))
    lines = [
        "#### Hubs and chokepoints",
        "",
        "Changed code by connections (edges in and out), ranked against all "
        f"{_num(data.get('ranked'))} connected symbols in the graph. Tests are left out.",
        "",
    ]
    if not rows:
        lines.append("_No changed symbol has a connection._")
        return lines
    for entry in rows[:_DETAIL_ROWS]:
        text = (
            f"- **{_name(entry.get('qualified_name'))}** ({_location(entry)}): "
            f"{_num(entry.get('degree'))} connections, rank #{_num(entry.get('rank'))}"
        )
        linked = entry.get("communities_linked")
        if isinstance(linked, int) and not isinstance(linked, bool) and linked > 0:
            text += f", callers and callees in {_communities(linked)}"
        lines.append(text)
    total = data.get("total")
    known = total if isinstance(total, int) and not isinstance(total, bool) else len(rows)
    _more(lines, max(known, len(rows)), min(len(rows), _DETAIL_ROWS), "symbol(s)")
    return lines


def _section_coupling(data: Any) -> list[str]:
    data = data if isinstance(data, dict) else {}
    rows = _rows(data.get("edges"))
    lines = [
        "#### Coupling across boundaries",
        "",
        f"{_num(data.get('cross_community'))} link(s) from or to the changed symbols "
        f"cross a community boundary and {_num(data.get('cross_language'))} cross a "
        "language boundary. Links to or from tests are left out.",
        "",
    ]
    for entry in rows[:_DETAIL_ROWS]:
        reasons = []
        for reason in entry.get("reasons") or []:
            if reason == "cross-language":
                reasons.append(
                    f"cross-language ({md_escape(entry.get('source_language') or '?', 40)} → "
                    f"{md_escape(entry.get('target_language') or '?', 40)})"
                )
            elif reason == "cross-community":
                reasons.append(
                    f"cross-community ({md_escape(entry.get('source_community') or '?', 60)} → "
                    f"{md_escape(entry.get('target_community') or '?', 60)})"
                )
        lines.append(
            f"- {_name(entry.get('source'))} → {_name(entry.get('target'))} "
            f"({md_escape(entry.get('kind') or '?', 40)}): {', '.join(reasons) or '?'}"
        )
    total = data.get("total")
    known = total if isinstance(total, int) and not isinstance(total, bool) else len(rows)
    _more(lines, max(known, len(rows)), min(len(rows), _DETAIL_ROWS), "link(s)")
    return lines


def _section_impact(data: Any) -> list[str]:
    data = data if isinstance(data, dict) else {}
    truncated = bool(data.get("truncated"))
    lead = "At least " if truncated else ""
    lines = [
        "#### Impact radius",
        "",
        f"{lead}{_num(data.get('impacted_nodes'))} symbol(s) in "
        f"{_num(data.get('impacted_files'))} file(s) depend on or test the changed "
        f"files, within {_num(data.get('depth'))} hop(s).",
    ]
    files = _rows(data.get("top_files"))
    if files:
        lines.extend(["", "Most affected files:", ""])
        for entry in files[:_DETAIL_ROWS]:
            lines.append(f"- {_location(entry)} ({_num(entry.get('nodes'))} symbol(s))")
        total = data.get("impacted_files")
        known = total if isinstance(total, int) and not isinstance(total, bool) else len(files)
        _more(lines, max(known, len(files)), min(len(files), _DETAIL_ROWS), "file(s)")
    callers = _rows(data.get("callers"))
    if callers:
        lines.extend(["", "Direct callers of the riskiest symbols:", ""])
        for entry in callers[:_DETAIL_ROWS]:
            names = [c for c in entry.get("callers") or [] if isinstance(c, str)]
            shown = names[:_DETAIL_CALLERS]
            total = entry.get("total")
            known = total if isinstance(total, int) and not isinstance(total, bool) else len(names)
            text = ", ".join(_name(c) for c in shown) or "?"
            if known > len(shown):
                text += f" and {known - len(shown):,} more"
            lines.append(f"- **{_name(entry.get('qualified_name'))}** is called by {text}")
    return lines


def _section_gaps(data: Any) -> list[str]:
    data = data if isinstance(data, dict) else {}
    rows = _rows(data.get("hotspots"))
    lines = [
        "#### Untested hotspots",
        "",
        f"Test gaps ranked by how many callers and flows depend on them "
        f"({_num(data.get('total'))} gap(s) in all).",
        "",
    ]
    if not rows:
        lines.append("_No test gaps._")
        return lines
    for entry in rows[:_DETAIL_ROWS]:
        coverage = (
            "reached only through a caller"
            if entry.get("coverage") == "indirect"
            else "no tested caller found"
        )
        lines.append(
            f"- **{_name(entry.get('qualified_name'))}** ({_location(entry)}): "
            f"{_num(entry.get('callers'))} caller(s), {_num(entry.get('flows'))} "
            f"flow(s), {coverage}"
        )
    total = data.get("total")
    known = total if isinstance(total, int) and not isinstance(total, bool) else len(rows)
    _more(lines, max(known, len(rows)), min(len(rows), _DETAIL_ROWS), "gap(s)")
    return lines


def _section_graph(data: Any) -> list[str]:
    data = data if isinstance(data, dict) else {}
    languages = [
        md_escape(lang, limit=40) for lang in data.get("languages") or [] if isinstance(lang, str)
    ]
    detector = data.get("community_detector")
    igraph = data.get("igraph_installed")
    if detector == "leiden":
        method = "found by Leiden (igraph)"
    elif detector == "file":
        method = "found by file-based grouping"
        if igraph is False:
            method += " (igraph is not installed)"
    elif detector:
        method = f"found by {md_escape(detector, limit=40)}"
    else:
        method = "method not recorded (graph built before it was tracked)"
        if igraph is True:
            method += "; igraph is installed"
        elif igraph is False:
            method += "; igraph is not installed"
    lines = [
        "#### Graph",
        "",
        f"- {_num(data.get('nodes'))} nodes, {_num(data.get('edges'))} edges, "
        f"{_num(data.get('files'))} files"
        + (f"; languages: {', '.join(languages)}" if languages else ""),
        f"- {_num(data.get('communities'))} communities, {method}",
        f"- {_num(data.get('flows'))} execution flows",
    ]
    build = md_escape(data.get("build_type") or "", limit=20)
    when = md_escape(data.get("last_updated") or "?", limit=40)
    lines.append(
        f"- Last {build + ' ' if build else ''}build {when}; "
        f"code-review-graph {md_escape(data.get('version') or '?', limit=40)}, "
        f"schema {_num(data.get('schema_version'))}"
    )
    return lines


_SECTION_RENDERERS = {
    "risk": _section_risk,
    "communities": _section_communities,
    "hubs": _section_hubs,
    "coupling": _section_coupling,
    "impact": _section_impact,
    "gaps": _section_gaps,
    "graph": _section_graph,
}

_SECTION_HEADINGS = {
    "risk": "Why these risk scores",
    "communities": "Communities touched",
    "hubs": "Hubs and chokepoints",
    "coupling": "Coupling across boundaries",
    "impact": "Impact radius",
    "gaps": "Untested hotspots",
    "graph": "Graph",
}


def _details_block(report: dict[str, Any], sections: Sequence[str], cap: int) -> list[str]:
    """The graph context block for *sections*, at most *cap* UTF-8 bytes.

    Sections are cut on line boundaries, in order, and a note at the end
    names every section that was cut short or left out. Returns ``[]`` when
    not even the wrapper and that note fit.
    """
    head = [
        "<details>",
        f"<summary>Graph context: {', '.join(sections)}</summary>",
        "",
    ]
    tail = ["", "</details>"]
    data = report.get("details")
    if not isinstance(data, dict):
        body = [
            "_This report has no graph context. It is added by "
            "`code-review-graph detect-changes --details ...`; the installed "
            "code-review-graph may be older than that option._"
        ]
        block = head + body + tail
        return block if _utf8_len(block) <= cap else []

    items: list[tuple[str, str]] = []
    for name in sections:
        section = data.get(name)
        if section is None:
            lines = [
                f"#### {_SECTION_HEADINGS[name]}",
                "",
                "_This section is not in the report._",
            ]
        else:
            lines = _SECTION_RENDERERS[name](section)
        if items:
            items.append((name, ""))
        items.extend((name, line) for line in lines)

    full = head + [line for _, line in items] + tail
    if _utf8_len(full) <= cap:
        return full

    def note(missing: Sequence[str]) -> str:
        return (
            f"> Graph context cut at {cap:,} bytes. Cut short or not shown: "
            f"{', '.join(missing)}."
        )

    reserve = ["", note(list(sections))]
    out = list(head)
    added: list[str] = []
    for name, line in items:
        if _utf8_len(out + [line] + reserve + tail) > cap:
            break
        out.append(line)
        added.append(name)
    complete = [
        name for name in sections
        if added.count(name) == sum(1 for owner, _ in items if owner == name)
    ]
    # Do not leave a heading or a blank line dangling above the note.
    while len(out) > len(head) and (not out[-1] or out[-1].startswith("####")):
        out.pop()
    missing = [name for name in sections if name not in complete]
    block = out + ["", note(missing)] + tail
    return block if _utf8_len(block) <= cap else []


def _fit_to_budget(body: str) -> str:
    """Return *body* trimmed so the written artifact fits ``_MAX_BODY``.

    Everything the consumer measures is inside the budget: the truncation
    notice, the footer, and the newline ``main`` writes after the body. The
    notice and footer are subtracted before the report is cut, and the cut
    lands on a line boundary so the last row of a markdown table is never
    left half-written.
    """
    encoded = body.encode("utf-8")
    if len(encoded) <= _MAX_BODY_TEXT:
        return body
    suffix = "\n\n*Report truncated.*\n\n" + FOOTER
    budget = _MAX_BODY_TEXT - len(suffix.encode("utf-8"))
    head = encoded[:budget].decode("utf-8", "ignore")
    newline = head.rfind("\n")
    if newline > 0:
        head = head[:newline]
    return head + suffix


def _artifact_text(body: str) -> str:
    """The exact bytes written out for *body*.

    The trailing newline is what a text file ends with, and it is also the
    byte ``_MAX_BODY_TEXT`` reserves. Both callers below go through here so
    the reservation and the write cannot drift apart.
    """
    return body + "\n"


def render_no_changes() -> str:
    """Fallback comment for when detect-changes finds nothing analyzable."""
    return "\n".join(
        [
            MARKER,
            "",
            "## code-review-graph review",
            "",
            "No analyzable code changes detected against the base branch.",
            "",
            "---",
            "",
            FOOTER,
        ]
    )


def render_not_analyzed(detail: str) -> str:
    """Comment for output that is neither an analysis nor a clean tree."""
    return "\n".join(
        [
            MARKER,
            "",
            "## code-review-graph review",
            "",
            "**The change analysis did not run, so this pull request has not "
            "been reviewed by code-review-graph.** This is not an all-clear.",
            "",
            "```",
            md_escape(detail, limit=400) or "(no output)",
            "```",
            "",
            "---",
            "",
            FOOTER,
        ]
    )


#: The exact line ``detect-changes`` prints for a genuinely unchanged tree.
NO_CHANGES_MARKER = "No changes detected."


def load_report(text: str) -> dict[str, Any] | None:
    """Parse detect-changes output; None when it is not a JSON object.

    ``detect-changes`` prints the plain string ``No changes detected.``
    instead of JSON when the diff is empty, so non-JSON input is expected.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def is_clean_tree(text: str) -> bool:
    """True only for detect-changes' own "nothing changed" line.

    Anything else that failed to parse as JSON — an error message, a partial
    write, an empty file because the command died — is the analysis not
    having happened, which is a different thing entirely.
    """
    return text.strip() == NO_CHANGES_MARKER


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--input",
        default="-",
        help="Path to detect-changes JSON output, or '-' for stdin (default).",
    )
    parser.add_argument(
        "--output",
        default="-",
        help="Path to write the markdown comment, or '-' for stdout (default).",
    )
    parser.add_argument(
        "--fail-on-risk",
        choices=("none", "high", "critical"),
        default="none",
        help="Exit 3 when the overall risk score reaches this level "
        "(high >= 0.70, critical >= 0.85). Default: none.",
    )
    parser.add_argument(
        "--max-functions",
        type=int,
        default=10,
        help="Maximum rows in the risk table (default: 10).",
    )
    parser.add_argument(
        "--max-flows",
        type=int,
        default=5,
        help="Maximum affected flows listed (default: 5).",
    )
    parser.add_argument(
        "--details",
        type=_details_arg,
        default=(),
        metavar="SECTIONS",
        help="Add opt-in graph context sections from the report's 'details' "
        "(from `detect-changes --details`): a comma-separated list of "
        f"{', '.join(DETAIL_SECTIONS)}, or all. Capped at "
        f"{_MAX_DETAILS_BYTES:,} bytes. Default: none.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Skip writing the markdown body (gate-only mode).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = build_arg_parser().parse_args(argv)

    if args.input == "-":
        text = sys.stdin.read()
    else:
        try:
            text = Path(args.input).read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("Cannot read input file %s: %s", args.input, exc)
            return 2

    report = load_report(text)
    not_analyzed = report is None and not is_clean_tree(text)
    if not_analyzed:
        logger.error(
            "detect-changes produced no analysis; this is NOT an all-clear: %s",
            text.strip()[:400] or "(no output)",
        )
        body = render_not_analyzed(text)
    elif report is None:
        body = render_no_changes()
    else:
        body = render_markdown(
            report,
            max_functions=args.max_functions,
            max_flows=args.max_flows,
            details=args.details,
        )

    if not args.quiet:
        if args.output == "-":
            sys.stdout.write(_artifact_text(body))
        else:
            try:
                Path(args.output).write_bytes(_artifact_text(body).encode("utf-8"))
            except OSError as exc:
                logger.error("Cannot write output file %s: %s", args.output, exc)
                return 2

    if not_analyzed:
        # Distinct from the risk gate: the risk is unknown, not low.
        return 4

    if args.fail_on_risk != "none" and report is not None:
        score = float(report.get("risk_score") or 0.0)
        threshold = RISK_THRESHOLDS[args.fail_on_risk]
        if score >= threshold:
            logger.error(
                "Risk gate breached: overall risk %.2f >= %s threshold %.2f",
                score,
                args.fail_on_risk,
                threshold,
            )
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
