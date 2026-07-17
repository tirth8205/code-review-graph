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
# GitHub rejects comment bodies over 65,536 characters; leave headroom.
_MAX_BODY = 60_000


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
    # Split off a trailing "::symbol" qualifier (e.g. "path::MyClass.method")
    # so only the path portion is relativized; the symbol is reattached as-is.
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
    # Preferred path: GitHub Actions' checkout action sets GITHUB_WORKSPACE
    # to the absolute repo root, so a simple prefix strip is exact.
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

    # Neither strategy matched — signal "leave it alone" to the caller.
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
    # Newlines/carriage returns would break a markdown table row; flatten them.
    text = text.replace("\r", " ").replace("\n", " ")
    # Escape the backslash itself first, then markdown/table special characters,
    # so escaping one doesn't accidentally clobber another.
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
    """Build the "Risk-scored changes" markdown table.

    Renders one row per changed symbol (up to ``max_functions``), showing its
    risk score, risk level, qualified name, source location, and whether it
    has direct test coverage (looked up via membership in ``gap_names``).
    Returns a list of markdown lines (not yet joined) so the caller can
    splice them into the overall comment body.
    """
    lines = [
        "### Risk-scored changes",
        "",
        "| Risk | Level | Symbol | Location | Tested |",
        "| ---: | :--- | :--- | :--- | :---: |",
    ]
    for entry in priorities[:max_functions]:
        score = float(entry.get("risk_score") or 0.0)
        name = entry.get("qualified_name") or entry.get("name") or "?"
        # "Tested" column: test functions themselves are marked distinctly
        # from production code, which is either covered ("yes") or listed
        # in the test-gaps set ("no").
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
    # If there are more changed symbols than the row cap, note how many were omitted.
    if len(priorities) > max_functions:
        lines.append("")
        lines.append(f"...and {len(priorities) - max_functions} more changed symbol(s).")
    return lines


def _flows_section(flows: list[dict[str, Any]], max_flows: int) -> list[str]:
    """Build the "Affected execution flows" markdown bullet list.

    Renders up to ``max_flows`` flows, each showing its name, criticality
    score (or "n/a" when absent), and how many nodes/files it spans.
    Returns a list of markdown lines to splice into the comment body.
    """
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
    # Note any flows beyond the display cap rather than silently dropping them.
    if len(flows) > max_flows:
        lines.append(f"- ...and {len(flows) - max_flows} more affected flow(s)")
    return lines


def _gaps_section(gaps: list[dict[str, Any]], max_gaps: int = 5) -> list[str]:
    """Build the "Test gaps" markdown bullet list.

    De-duplicates gaps by qualified/plain name (the same symbol can appear
    more than once in the raw ``gaps`` list), then renders up to
    ``max_gaps`` entries with their file:line location. Returns a list of
    markdown lines to splice into the comment body.
    """
    lines = ["### Test gaps", ""]
    seen: set[str] = set()
    shown: list[dict[str, Any]] = []
    # First pass: collect up to max_gaps unique-by-name entries.
    for gap in gaps:
        name = str(gap.get("qualified_name") or gap.get("name") or "?")
        if name in seen:
            continue
        seen.add(name)
        shown.append(gap)
        if len(shown) >= max_gaps:
            break
    # Second pass: render the collected entries as bullet points.
    for gap in shown:
        name = gap.get("qualified_name") or gap.get("name") or "?"
        lines.append(f"- {md_escape(relativize_path(name))} ({_location(gap)})")
    # `remaining` counts against the raw (non-deduplicated) gap list, so it
    # reflects "how many more gap records exist" rather than unique symbols.
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
    # review_priorities is the risk-sorted list; fall back to raw changed
    # functions if the report doesn't include it (older CLI versions).
    priorities = report.get("review_priorities") or changed
    gap_names = {
        str(g.get("qualified_name") or g.get("name") or "") for g in gaps
    }

    # Hidden marker line lets the CI action locate and update this exact
    # comment on subsequent pushes instead of posting duplicates.
    lines: list[str] = [MARKER, "", "## code-review-graph review", ""]
    lines.append(
        f"**Overall risk: {score:.2f} ({risk_level(score).upper()})** — "
        f"{len(changed)} changed function(s)/class(es), "
        f"{len(flows)} affected flow(s), {len(gaps)} test gap(s)"
    )

    # Each section is optional and only rendered when the report has data for it.
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
    body = "\n".join(lines)
    # Enforce GitHub's comment body size limit as a final safety net, even
    # though the per-section max_functions/max_flows caps should normally
    # keep bodies well under this.
    if len(body) > _MAX_BODY:
        body = body[:_MAX_BODY] + "\n\n*Report truncated.*\n\n" + FOOTER
    return body


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


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser for this script.

    Defines all supported flags (input/output locations, the risk gate,
    display caps, and quiet mode) and their defaults/help text. Kept
    separate from ``main`` so the parser can be introspected or reused
    (e.g. in tests) without invoking the full program.
    """
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
    """Entry point: parse args, render the comment, write it, apply the risk gate.

    Flow:
      1. Read detect-changes JSON from ``--input`` (file or stdin).
      2. Parse it; fall back to a "no changes" comment if it's not valid JSON.
      3. Render the markdown body and write it to ``--output`` (unless ``--quiet``).
      4. If ``--fail-on-risk`` is set and the report's risk score meets/exceeds
         the corresponding threshold, return exit code 3 to fail the CI step.

    Returns a process exit code: 0 on success, 2 on I/O failure, 3 on a
    breached risk gate.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = build_arg_parser().parse_args(argv)

    # Step 1: load the raw detect-changes output from stdin or a file.
    if args.input == "-":
        text = sys.stdin.read()
    else:
        try:
            text = Path(args.input).read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("Cannot read input file %s: %s", args.input, exc)
            return 2

    # Step 2: parse it into a report dict, or treat non-JSON as "no changes".
    report = load_report(text)
    if report is None:
        body = render_no_changes()
    else:
        body = render_markdown(
            report,
            max_functions=args.max_functions,
            max_flows=args.max_flows,
        )

    # Step 3: write the rendered markdown, unless --quiet (gate-only mode).
    if not args.quiet:
        if args.output == "-":
            sys.stdout.write(body + "\n")
        else:
            try:
                Path(args.output).write_text(body + "\n", encoding="utf-8")
            except OSError as exc:
                logger.error("Cannot write output file %s: %s", args.output, exc)
                return 2

    # Step 4: apply the risk gate, if configured and a report was parsed.
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