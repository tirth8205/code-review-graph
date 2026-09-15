#!/usr/bin/env python3
"""Fail when code promoted to `testing` changed functions that no test covers.

Dogfoods the project's own analysis: build (or update) the graph for the
checkout, ask ``detect_changes`` what changed against the last released state,
and report every changed function with no ``TESTED_BY`` edge.

Exit codes:
    0  no gaps (or nothing to analyse)
    1  gaps found; the report is written to --out and echoed to stdout
    2  the analysis itself failed
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="Repository root")
    parser.add_argument("--base", default="origin/main", help="Diff base")
    parser.add_argument("--out", default="test-gaps.md", help="Markdown report path")
    parser.add_argument(
        "--max-gaps",
        type=int,
        default=0,
        help="Number of uncovered functions tolerated before failing",
    )
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    sys.path.insert(0, str(repo))

    try:
        from code_review_graph.tools.review import detect_changes_func as detect_changes
    except Exception as exc:  # pragma: no cover - import guard
        print(f"cannot import detect_changes: {exc}", file=sys.stderr)
        return 2

    try:
        result = detect_changes(repo_root=str(repo), base=args.base, detail_level="standard")
    except Exception as exc:
        print(f"detect_changes failed: {exc}", file=sys.stderr)
        return 2

    if result.get("status") not in (None, "ok"):
        print(
            f"detect_changes status={result.get('status')}: {result.get('summary')}",
            file=sys.stderr,
        )
        return 2

    gaps = result.get("test_gaps") or []
    total = result.get("test_gaps_total", len(gaps))
    changed = result.get("changed_file_count", 0)
    funcs = result.get("changed_functions_total", 0)

    print(f"base={args.base} changed_files={changed} changed_functions={funcs} test_gaps={total}")

    if total <= args.max_gaps:
        print("No uncovered changed functions.")
        return 0

    lines = [
        "## Uncovered changes reached `testing`",
        "",
        f"`detect_changes` against `{args.base}` found **{total}** changed "
        f"function(s) with no test edge, across {changed} changed file(s).",
        "",
        "| Function | File | Line |",
        "| --- | --- | --- |",
    ]
    for gap in gaps[:50]:
        if isinstance(gap, dict):
            name = gap.get("name") or gap.get("qualified_name", "?")
            path = gap.get("file_path", "?")
            line = gap.get("line", "")
        else:
            name, path, line = str(gap), "", ""
        lines.append(f"| `{name}` | `{path}` | {line} |")
    if total > 50:
        lines.append(f"| ... | {total - 50} more | |")
    lines += [
        "",
        "Promotion to `main` is blocked until each of these is covered.",
        "Add the tests on a branch off `staging`; the change is promoted again",
        "automatically once they pass.",
        "",
        "```bash",
        "code-review-graph get-review-context --base origin/main   # what to test",
        "```",
    ]
    report = "\n".join(lines)
    Path(args.out).write_text(report + "\n", encoding="utf-8")
    print(report)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
