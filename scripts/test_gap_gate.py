#!/usr/bin/env python3
"""Fail when code promoted to `testing` changed functions that no test covers.

Dogfoods the project's own analysis: ask ``detect_changes`` what changed
against the last released state and report every changed function with no
``TESTED_BY`` edge.

The gate is only worth blocking a promotion on if its rows survive a sceptic
reading them, so it does three things the first version did not:

* **It fails closed.** A missing or stale graph is reported as an analysis
  failure (exit 2), not as "no gaps". Before, deleting the database made the
  gate pass with 135 changed files and zero findings.
* **It counts honestly.** The rendered rows, the totals and the "N more"
  footer all come from the same numbers, and the analysis caps are raised so
  the total is the real total rather than the first 500 changed nodes.
* **It says what it cannot see.** Coverage here means "a test reaches this
  function", which a skipped test or a test with no assertion also satisfies.
  Those are found and listed, but they do not block by default; see
  ``--strict-assertions``.

Exit codes:
    0  no gaps
    1  gaps found; the report is written to --out and echoed to stdout
    2  the analysis could not be trusted (no graph, stale graph, or an error)
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Any

# Raised before the analysis imports, because ``analyze_changes`` reads this
# at call time to cap the changed-node scan. The default of 500 silently drops
# changed functions on a release-sized diff: on the delta this gate was built
# for it reported 177 gaps where the uncapped count was 212. A gate that
# under-reports is worse than one that is slow.
_DEFAULT_MAX_CHANGED_FUNCS = "20000"

# Upper bound on the indirect-coverage annotation pass, which costs one
# bounded graph traversal per row.
_MAX_INDIRECT_SCAN = 500

# Every assertion spelling the gate recognises. Deliberately generous: a false
# "this test asserts" is a quiet pass, a false "this test asserts nothing" is a
# maintainer chasing a phantom.
_ASSERTION_PATTERNS = [
    re.compile(r"\bassert\b"),
    re.compile(r"\.assert[A-Za-z_]*\("),
    re.compile(r"\bassert[A-Za-z_]*\("),
    re.compile(r"\bpytest\.raises\b"),
    re.compile(r"\bpytest\.warns\b"),
    re.compile(r"\bexpect\s*\("),
    re.compile(r"\bshould[A-Z.]"),
    re.compile(r"\bverify\s*\("),
    re.compile(r"\b(?:EXPECT|ASSERT)_[A-Z]"),
    re.compile(r"\bt\.(?:Error|Fatal|Fail)"),
]

# Only *unconditional* skips. ``skipif``/``skipUnless`` carry a runtime
# condition — "skipped when igraph is absent" is a real test on the machines
# that have igraph — and calling those dead would be the same overstatement
# this gate exists to remove.
_SKIP_PATTERNS = [
    re.compile(r"\bpytest\.mark\.skip(?!if)\b"),
    re.compile(r"\bpytest\.mark\.xfail\b"),
    re.compile(r"\bunittest\.skip(?!If|Unless)\b"),
    re.compile(r"^skip(?:\(|$)"),
    re.compile(r"^(?:Ignore|Disabled)\b"),
]


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _check_graph_is_current(repo: Path, changed_files: list[str]) -> str | None:
    """Return a reason string when the graph cannot answer for *changed_files*.

    ``GraphStore`` creates an empty database on demand, so "no graph" and "a
    graph with nothing in it" are indistinguishable to the analysis and both
    produce zero gaps. Check before trusting the answer: the database has to
    exist, and every changed file the indexer would have picked up has to be
    in it under the hash it currently has on disk.
    """
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import collect_all_files, get_db_path
    from code_review_graph.parser import CodeParser, normalize_file_path

    db_path = get_db_path(repo, read_only=True)
    if not db_path.exists():
        return f"no graph at {db_path}; run `code-review-graph build` first"

    # ``collect_all_files`` yields repo-relative paths; the graph keys on the
    # normalized absolute path.
    indexable = {normalize_file_path(repo / p) for p in collect_all_files(repo)}
    store = GraphStore(db_path)
    try:
        indexed = store.get_file_hashes()
    finally:
        store.close()
    if not indexed:
        return f"the graph at {db_path} indexes no files"

    parser = CodeParser(repo)
    missing: list[str] = []
    stale: list[str] = []
    for rel in changed_files:
        abs_path = repo / rel
        key = normalize_file_path(abs_path)
        if key in indexed:
            on_disk = _sha256(abs_path)
            if on_disk is None:
                # Indexed, but gone from the working tree.
                stale.append(rel)
            elif indexed[key] and on_disk != indexed[key]:
                stale.append(rel)
            continue
        if key not in indexable or not abs_path.is_file():
            # Not a file the indexer collects at all (a lockfile, an image, a
            # deleted path). Nothing to be stale about.
            continue
        # Collected but absent from the graph. Many collected files yield no
        # symbols at all — a plain YAML workflow, for instance — and the graph
        # is right to hold nothing for them. Only a file that does parse into
        # nodes is evidence the graph was never built for this revision.
        try:
            nodes, _edges = parser.parse_file(abs_path)
        except Exception:  # pragma: no cover - a parse crash is not staleness
            continue
        if nodes:
            missing.append(rel)

    if missing or stale:
        detail = []
        if missing:
            detail.append(f"{len(missing)} changed file(s) not in the graph "
                          f"(e.g. {missing[0]})")
        if stale:
            detail.append(f"{len(stale)} changed file(s) indexed at a different "
                          f"revision (e.g. {stale[0]})")
        return "; ".join(detail) + "; rebuild the graph before gating"
    return None


def _read_lines(path: Path, cache: dict[Path, list[str]]) -> list[str]:
    if path not in cache:
        try:
            cache[path] = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            cache[path] = []
    return cache[path]


def _test_is_weak(node: Any, cache: dict[Path, list[str]]) -> str | None:
    """Return why *node* proves little, or None when it looks like a real test.

    Two shapes clear a "does a test reach this function?" gate without
    checking anything: a skipped test, and a test that calls the function and
    asserts nothing. Both are found by reading the test's own source lines.
    """
    extra = node.extra or {}
    decorators = extra.get("decorators")
    if isinstance(decorators, (list, tuple)):
        for decorator in decorators:
            text = str(decorator)
            if any(p.search(text) for p in _SKIP_PATTERNS):
                return "skipped"

    lines = _read_lines(Path(node.file_path), cache)
    if not lines:
        return None  # cannot read it; do not accuse it
    start = max(0, (node.line_start or 1) - 1)
    end = min(len(lines), node.line_end or len(lines))
    body = "\n".join(lines[start:end])
    if any(p.search(body) for p in _ASSERTION_PATTERNS):
        return None
    return "no assertion"


def _find_weak_coverage(repo: Path, analysis: dict[str, Any]) -> list[dict[str, str]]:
    """List changed functions whose every covering test is skipped or empty."""
    from code_review_graph.changes import _TEST_GAP_EXEMPT_NAMES
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import get_db_path
    from code_review_graph.parser import _is_test_file, repo_relative_path

    gap_names = {g.get("qualified_name") for g in analysis.get("test_gaps") or []}
    store = GraphStore(get_db_path(repo, read_only=True))
    cache: dict[Path, list[str]] = {}
    weak: list[dict[str, str]] = []
    try:
        for func in analysis.get("changed_functions") or []:
            qualified = func.get("qualified_name")
            if not qualified or qualified in gap_names:
                continue
            if func.get("name") in _TEST_GAP_EXEMPT_NAMES:
                continue
            if _is_test_file(repo_relative_path(func.get("file_path") or "", repo)):
                continue
            tests = store.get_transitive_tests(qualified, max_depth=0)
            if not tests:
                continue
            reasons = set()
            for test in tests:
                node = store.get_node(test["qualified_name"])
                if node is None:
                    reasons.add("")
                    break
                reason = _test_is_weak(node, cache)
                if reason is None:
                    reasons.add("")
                    break
                reasons.add(reason)
            if "" in reasons or not reasons:
                continue
            weak.append({
                "name": str(func.get("name") or qualified),
                "file": str(func.get("file_path") or ""),
                "reason": ", ".join(sorted(reasons)),
            })
    finally:
        store.close()
    return weak


def _mark_indirect_coverage(repo: Path, gaps: list[dict[str, Any]]) -> int:
    """Annotate each gap with whether a test reaches it through call edges.

    The gap list answers "does a test call this function?". On this repository
    41% of the answers are "no, but a test runs it two calls away" — a helper
    like ``_parse_vue`` has no test of its own and is executed by every Vue
    parsing test. Saying "no test covers this" about those rows is the kind of
    overstatement a reader checks and disbelieves, so the report says which is
    which. Returns how many rows are reached indirectly.
    """
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import get_db_path

    store = GraphStore(get_db_path(repo, read_only=True))
    reached = 0
    try:
        for gap in gaps[:_MAX_INDIRECT_SCAN]:
            qualified = gap.get("qualified_name")
            if not qualified:
                continue
            try:
                indirect = bool(store.get_transitive_tests(qualified, max_depth=2))
            except Exception:  # pragma: no cover - advisory annotation only
                continue
            gap["indirect"] = indirect
            reached += int(indirect)
    finally:
        store.close()
    return reached


def _relative(path: str, repo: Path) -> str:
    if not path:
        return "?"
    try:
        return str(Path(path).resolve().relative_to(repo))
    except (ValueError, OSError):
        return path


def _render(
    gaps: list[dict[str, Any]],
    total: int,
    changed: int,
    base: str,
    repo: Path,
    max_rows: int,
    weak: list[dict[str, str]],
    indirect: int = 0,
) -> str:
    shown = gaps[:max_rows]
    lines = [
        "## Changed functions with no test of their own",
        "",
        f"`detect_changes` against `{base}` found **{total}** changed "
        f"function(s) that no test calls directly, across {changed} changed "
        f"file(s).",
    ]
    if indirect:
        # Scoped to the rows actually inspected, never to the whole total: the
        # annotation pass only sees the rows the analysis returned.
        lines += [
            "",
            f"Of the {len(shown)} listed below, {indirect} are executed by a "
            "test through at most two call hops — run, but not checked. Those "
            "are marked `indirect`.",
        ]
    lines += [
        "",
        "| Function | File | Lines | Reached |",
        "| --- | --- | --- | --- |",
    ]
    for gap in shown:
        name = gap.get("name") or gap.get("qualified_name") or "?"
        path = _relative(str(gap.get("file") or ""), repo)
        start = gap.get("line_start")
        end = gap.get("line_end")
        span = f"{start}-{end}" if start and end and end != start else (start or "")
        reach = "indirect" if gap.get("indirect") else "not at all"
        lines.append(f"| `{name}` | `{path}` | {span} | {reach} |")
    remaining = total - len(shown)
    if remaining > 0:
        lines.append(f"| ... | _{remaining} more not listed_ | | |")
    if weak:
        lines += [
            "",
            "### Covered, but by a test that checks nothing",
            "",
            "These are not counted above. Each has a test edge, and every test "
            "behind it is skipped or contains no assertion, so the edge proves "
            "the function runs and nothing more.",
            "",
            "| Function | File | Why |",
            "| --- | --- | --- |",
        ]
        for item in weak[:max_rows]:
            lines.append(
                f"| `{item['name']}` | `{_relative(item['file'], repo)}` "
                f"| {item['reason']} |"
            )
        if len(weak) > max_rows:
            lines.append(f"| ... | _{len(weak) - max_rows} more not listed_ | |")
    lines += [
        "",
        "Add the tests on a branch off `staging`; the change is promoted here",
        "again automatically once they pass.",
        "",
        "```bash",
        "code-review-graph get-review-context --base origin/main   # what to test",
        "```",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
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
    parser.add_argument(
        "--max-rows",
        type=int,
        default=50,
        help="Rows rendered in the report; the footer names the remainder",
    )
    parser.add_argument(
        "--strict-assertions",
        action="store_true",
        help="Also fail when a changed function's only tests are skipped or "
             "assert nothing (reported either way)",
    )
    parser.add_argument(
        "--allow-missing-graph",
        action="store_true",
        help="Report 'nothing to analyse' instead of failing when the graph "
             "is absent or stale. Off by default: the gate fails closed.",
    )
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    sys.path.insert(0, str(repo))
    os.environ.setdefault("CRG_MAX_CHANGED_FUNCS", _DEFAULT_MAX_CHANGED_FUNCS)

    try:
        from code_review_graph.incremental import get_changed_files, resolve_review_base
        from code_review_graph.tools.review import detect_changes_func as detect_changes
    except Exception as exc:  # pragma: no cover - import guard
        print(f"cannot import detect_changes: {exc}", file=sys.stderr)
        return 2

    try:
        base = resolve_review_base(repo, args.base)
        changed_files = get_changed_files(repo, base)
    except Exception as exc:
        print(f"cannot determine changed files against {args.base}: {exc}",
              file=sys.stderr)
        return 2

    if not changed_files:
        print(f"base={args.base} changed_files=0 — nothing to analyse")
        return 0

    reason = _check_graph_is_current(repo, changed_files)
    if reason is not None:
        message = (
            f"{len(changed_files)} changed file(s) but the graph cannot answer "
            f"for them: {reason}"
        )
        if args.allow_missing_graph:
            print(f"warning: {message}", file=sys.stderr)
            return 0
        print(f"error: {message}", file=sys.stderr)
        return 2

    try:
        result = detect_changes(
            repo_root=str(repo),
            base=args.base,
            detail_level="standard",
            max_results=args.max_rows,
        )
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
    changed = result.get("changed_file_count", len(changed_files))
    funcs = result.get("changed_functions_total", 0)

    try:
        weak = _find_weak_coverage(repo, result)
    except Exception as exc:  # pragma: no cover - advisory signal only
        print(f"warning: weak-coverage scan failed: {exc}", file=sys.stderr)
        weak = []

    indirect = _mark_indirect_coverage(repo, gaps)

    print(
        f"base={args.base} changed_files={changed} changed_functions={funcs} "
        f"test_gaps={total} reached_indirectly={indirect}/{len(gaps)}_inspected "
        f"weak_coverage={len(weak)}"
    )

    failing = total > args.max_gaps or (args.strict_assertions and weak)
    if not failing:
        if weak:
            print(
                f"No changed function is without a test. {len(weak)} are covered "
                "only by tests that are skipped or assert nothing (advisory)."
            )
        else:
            print("No changed function is without a test.")
        return 0

    report = _render(
        gaps, total, changed, args.base, repo, args.max_rows, weak, indirect,
    )
    Path(args.out).write_text(report + "\n", encoding="utf-8")
    print(report)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
