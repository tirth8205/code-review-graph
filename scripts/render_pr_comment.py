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
) -> list[str]:
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
        elif name in gap_names:
            tested = "no"
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


def _gaps_section(gaps: list[dict[str, Any]], max_gaps: int = 5) -> list[str]:
    lines = ["### Test gaps", ""]
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
    for gap in shown:
        name = gap.get("qualified_name") or gap.get("name") or "?"
        lines.append(f"- {md_escape(relativize_path(name))} ({_location(gap)})")
    remaining = len(gaps) - len(shown)
    if remaining > 0:
        lines.append(f"- ...and {remaining} more without direct tests")
    return lines


def render_markdown(
    report: dict[str, Any],
    *,
    max_functions: int = 10,
    max_flows: int = 5,
) -> str:
    """Render the detect-changes JSON report as a markdown PR comment."""
    score = float(report.get("risk_score") or 0.0)
    changed = report.get("changed_functions") or []
    flows = report.get("affected_flows") or []
    gaps = report.get("test_gaps") or []
    priorities = report.get("review_priorities") or changed
    gap_names = {
        str(g.get("qualified_name") or g.get("name") or "") for g in gaps
    }

    lines: list[str] = [MARKER, "", "## code-review-graph review", ""]
    lines.append(
        f"**Overall risk: {score:.2f} ({risk_level(score).upper()})** — "
        f"{len(changed)} changed function(s)/class(es), "
        f"{len(flows)} affected flow(s), {len(gaps)} test gap(s)"
    )

    if priorities:
        lines.append("")
        lines.extend(_functions_table(priorities, gap_names, max_functions))
    if flows:
        lines.append("")
        lines.extend(_flows_section(flows, max_flows))
    if gaps:
        lines.append("")
        lines.extend(_gaps_section(gaps))

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
            "> Note: analysis was capped at the configured maximum number of "
            "changed functions (set `CRG_MAX_CHANGED_FUNCS` to adjust)."
        )

    lines.extend(["", "---", "", FOOTER])
    return _fit_to_budget("\n".join(lines))


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
        )

    if not args.quiet:
        if args.output == "-":
            sys.stdout.write(_artifact_text(body))
        else:
            try:
                Path(args.output).write_text(_artifact_text(body), encoding="utf-8")
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
