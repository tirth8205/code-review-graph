#!/usr/bin/env python3
"""Run one promotion-gate check, then collate every check into one report.

``.github/workflows/promotion-gate.yml`` calls this twice per run:

``run``
    Wraps a single pytest invocation. It runs pytest with ``--junitxml``,
    reads the report back, applies that check's canary rules, and writes a
    small JSON verdict the summary job later picks up as an artifact.

``summarize``
    Reads every verdict written by the ``run`` steps plus the GitHub
    ``needs`` context, renders one markdown report, and decides whether the
    blocking set passed.

Why a wrapper instead of a bare ``pytest`` step:

* A check that is selected but silently skipped is the failure mode this
  whole gate exists to prevent. Three of the four release checks are opt-in
  through a ``pytest_collection_modifyitems`` hook in ``tests/conftest.py``,
  and those hooks arrive on four separate branches that overwrite one
  another on merge. A bare ``pytest -m corpus`` step exits 0 when every
  corpus test was skipped by a clobbered hook. ``run`` fails instead, and
  says which reason the skip carried.
* A check whose test module never landed on the branch is likewise a green
  step and no coverage at all. ``run`` asserts the module is on disk first.
* The report has to say *what moved*, not just that something did. The
  junit report carries each failing test's assertion message, which for
  these checks is already the interesting sentence: the corpus check names
  the property, the baseline, the measurement and the delta.

The blocking policy lives in ``CHECKS`` below and nowhere else, so the
workflow cannot disagree with the report and ``tests/test_promotion_gate.py``
can assert on it directly.

Exit codes for both subcommands: 0 when nothing blocking failed, 1 when
something blocking failed, 2 on a usage problem.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

# Verdicts. "missing" means no verdict file reached the summary job at all:
# the runner was cancelled, timed out, or died before writing one.
PASSED = "passed"
FAILED = "failed"
MISSING = "missing"

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_LINE = 240
_MAX_DETAIL_LINES = 3
_MAX_FAILURES_SHOWN = 12
_TAIL_LINES = 25


@dataclass(frozen=True)
class Check:
    """One check in the gate, and whether it may stop a release."""

    key: str
    title: str
    blocking: bool
    policy: str
    modules: tuple[str, ...] = ()
    min_tests: int = 1
    min_passed: int = 1
    # Skip reasons that mean "this check did not actually run". Matched
    # case-insensitively as substrings against the junit skip message.
    forbidden_skips: tuple[str, ...] = ()
    # Matrix legs this check is expected to produce a verdict for. Listing
    # them is what lets the summary notice that one leg of a matrix died
    # without a verdict while its siblings passed.
    variants: tuple[str, ...] = ("",)
    # Per-variant overrides of ``blocking``.
    variant_blocking: dict[str, bool] = field(default_factory=dict)


# Phrases the opt-in hooks in tests/conftest.py put on their skip reasons.
_OPT_IN_SKIPS = ("run it with", "run with", "gate: run with")

CHECKS: dict[str, Check] = {
    "upgrade-path": Check(
        key="upgrade-path",
        title="Upgrade from released versions",
        blocking=True,
        policy=(
            "Blocks. It is the only check that proves a graph built by a released "
            "version survives being opened by this code. A migration that corrupts "
            "an existing .code-review-graph/graph.db cannot be undone by a later "
            "patch release, and the user's database is not ours to lose."
        ),
        modules=("tests/test_upgrade_path.py",),
        min_tests=30,
        min_passed=25,
        forbidden_skips=("crg_upgrade_test", *_OPT_IN_SKIPS),
    ),
    "packaging": Check(
        key="packaging",
        title="Wheel and sdist from a clean install",
        blocking=True,
        policy=(
            "Blocks. It drives the artefact users actually receive, installed into "
            "a clean environment with the checkout out of reach. A wheel missing a "
            "data file is broken for every user at once and is only fixable by "
            "cutting another release. It installs from PyPI, but so does every "
            "other job in this workflow, so it adds no network dependency the gate "
            "does not already carry."
        ),
        modules=("tests/test_packaging.py",),
        min_tests=12,
        min_passed=12,
        forbidden_skips=("crg_run_packaging_tests", "packaging gate", *_OPT_IN_SKIPS),
    ),
    "determinism": Check(
        key="determinism",
        title="Rebuild determinism",
        blocking=True,
        policy=(
            "Blocks. It needs no network and no third-party checkout, so a failure "
            "is a real difference and not an outage. If the same tree stops "
            "producing the same graph then every incremental update and every "
            "detect-changes answer is partly measuring build noise, and that "
            "failure is invisible in normal use. The properties known to be broken "
            "today are pinned as strict xfails, so this is green now and turns red "
            "only on a regression, or on a fix whose xfail marker was left behind."
        ),
        modules=("tests/test_determinism.py",),
        min_tests=12,
        min_passed=10,
        forbidden_skips=("determinism gate", *_OPT_IN_SKIPS),
    ),
    "suite": Check(
        key="suite",
        title="Full suite with coverage",
        blocking=True,
        policy=(
            "Blocks. The same suite and the same 65% coverage floor the "
            "pull-request CI enforces, re-run across 3.10 to 3.13 against the "
            "merged state of testing, which no single pull request ever tested."
        ),
        min_tests=2500,
        min_passed=2500,
        variants=("3.10", "3.11", "3.12", "3.13"),
    ),
    "e2e": Check(
        key="e2e",
        title="End-to-end MCP client",
        blocking=True,
        policy=(
            "Blocks on Linux and macOS, reports on Windows. It drives the real MCP "
            "server over stdio, the interface every editor integration speaks, so a "
            "break there is total for the people it affects. The Windows leg "
            "reports because process spawning and file-handle timing on the Windows "
            "runner is the flakiest surface in this repository, and a runner hiccup "
            "must not hold a release."
        ),
        modules=("tests/test_e2e_mcp_client.py", "tests/test_e2e_real_repo.py"),
        min_tests=20,
        min_passed=20,
        forbidden_skips=("mcp client library", "git is required"),
        variants=("ubuntu-latest", "macos-latest", "windows-latest"),
        variant_blocking={"windows-latest": False},
    ),
    "corpus": Check(
        key="corpus",
        title="Pinned real-repository corpus",
        blocking=False,
        policy=(
            "Reports. It shallow-clones eight third-party repositories at pinned "
            "SHAs from GitHub. A rate limit, an outage or an upstream force-push "
            "makes it fail for a reason that has nothing to do with this code, and "
            "a failed clone must not stop a release. Read the numbers it prints: a "
            "property that moved is a real regression and should be fixed before "
            "promoting. A person decides that, not the gate."
        ),
        modules=("tests/test_real_repo_corpus.py",),
        min_tests=16,
        min_passed=16,
        forbidden_skips=("corpus", *_OPT_IN_SKIPS),
    ),
    "browser": Check(
        key="browser",
        title="Visualization in a real browser",
        blocking=False,
        policy=(
            "Reports. It downloads a Chromium build at run time and drives a page "
            "in it, the classic flaky combination, and a visualization that fails "
            "to render damages nobody's data and can ship a day later. The canary "
            "still fails the check if Playwright was absent and the tests were "
            "skipped rather than run, so 'reports' never means 'ignored'."
        ),
        modules=("tests/test_visualization_browser.py",),
        min_tests=6,
        min_passed=5,
        forbidden_skips=("playwright", "could not import"),
    ),
}

# The order the report renders in: blocking checks first, then reporting.
REPORT_ORDER = (
    "upgrade-path",
    "packaging",
    "determinism",
    "suite",
    "e2e",
    "corpus",
    "browser",
)


def is_blocking(key: str, variant: str = "") -> bool:
    """Whether a failure of ``key`` (optionally one matrix leg) stops a release."""
    check = CHECKS[key]
    if variant and variant in check.variant_blocking:
        return check.variant_blocking[variant]
    return check.blocking


def clean(text: object) -> str:
    """Strip control characters and clamp a single line for display."""
    flat = _CONTROL_CHARS.sub("", str(text)).strip()
    if len(flat) > _MAX_LINE:
        flat = flat[: _MAX_LINE - 3] + "..."
    return flat


def _first_lines(text: object, limit: int = _MAX_DETAIL_LINES) -> list[str]:
    lines = [clean(line) for line in str(text).splitlines()]
    return [line for line in lines if line][:limit]


def _fenced(value: object) -> list[str]:
    """Lines safe to drop inside a markdown code fence.

    Verdicts arrive as build artefacts, so nothing read out of them is put
    into the report untouched: control characters go, long lines are
    clamped, and a line that would close the fence early cannot.
    """
    out: list[str] = []
    for raw in str(value).splitlines():
        stripped = _CONTROL_CHARS.sub("", raw).replace("```", "'''")
        indent = len(stripped) - len(stripped.lstrip(" "))
        body = clean(stripped)
        if body:
            out.append(" " * min(indent, 8) + body)
    return out


@dataclass
class Totals:
    """Test counts pulled out of one junit report."""

    tests: int = 0
    passed: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    xfailed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "tests": self.tests,
            "passed": self.passed,
            "failures": self.failures,
            "errors": self.errors,
            "skipped": self.skipped,
            "xfailed": self.xfailed,
        }


class ReportError(Exception):
    """The junit report could not be trusted or read."""


def parse_junit(path: Path) -> tuple[Totals, list[str], list[tuple[str, str]]]:
    """Read a pytest junit report.

    Returns ``(totals, moved, skips)``. ``moved`` holds one entry per failing
    or erroring test: the test id followed by the first lines of its
    assertion message, which is what "exactly what moved" means for these
    checks. ``skips`` pairs each genuine skip with its reason; the id matters
    because a marker-filtered run also reports the collection skip of an
    unrelated module, and that must not be read as this check being skipped.
    An xfail is recorded separately, because pytest writes it as a
    ``skipped`` element too and counting it as a skip would make every
    deliberately recorded bug look like a check that did not run.

    The file is written by pytest on the same runner moments earlier, so it
    is not untrusted input, but a report carrying a document type definition
    is rejected rather than parsed: that is the only way an entity-expansion
    or external-entity payload could reach the parser, and pytest never
    emits one.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    lowered = text[:4096].lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise ReportError("the junit report declares a DTD; refusing to parse it")
    totals = Totals()
    moved: list[str] = []
    skips: list[tuple[str, str]] = []
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise ReportError(f"the junit report is not well formed: {exc}") from exc
    for case in root.iter("testcase"):
        totals.tests += 1
        name = f"{case.get('classname', '')}::{case.get('name', '')}".strip(":")
        bad: tuple[str, ElementTree.Element] | None = None
        for tag in ("failure", "error"):
            element = case.find(tag)
            if element is not None:
                bad = (tag, element)
                break
        if bad is not None:
            tag, element = bad
            if tag == "failure":
                totals.failures += 1
            else:
                totals.errors += 1
            message = element.get("message") or element.text or ""
            detail = _first_lines(message)
            entry = clean(name)
            if detail:
                entry += "\n    " + "\n    ".join(detail)
            moved.append(entry)
            continue
        skipped = case.find("skipped")
        if skipped is not None:
            if (skipped.get("type") or "") == "pytest.xfail":
                totals.xfailed += 1
            else:
                totals.skipped += 1
                # A collection skip puts "collection skipped" in the message
                # and the real reason in the element text, so read both.
                reason = " ".join(
                    part for part in (skipped.get("message"), skipped.text) if part
                )
                skips.append((clean(name), clean(reason)))
            continue
        totals.passed += 1
    return totals, moved, skips


def apply_canaries(
    check: Check, totals: Totals, skips: list[tuple[str, str]], repo_root: Path
) -> list[str]:
    """Return the canary violations for one check. Empty means it really ran.

    Every rule here exists because the matching way of passing without
    testing anything has actually happened: a module that did not land on
    the branch, an opt-in hook that skipped the very tests the run selected,
    a marker filter that matched nothing, or an ``importorskip`` that turned
    an absent dependency into a green run.
    """
    problems: list[str] = []
    for module in check.modules:
        if not (repo_root / module).is_file():
            problems.append(
                f"{module} is not in this checkout, so the check tested nothing. "
                "Its pull request has not landed on this branch."
            )
    if totals.tests < check.min_tests:
        problems.append(
            f"only {totals.tests} test(s) were collected, expected at least "
            f"{check.min_tests}: the selection matched (almost) nothing."
        )
    if totals.passed < check.min_passed:
        problems.append(
            f"only {totals.passed} test(s) passed, expected at least "
            f"{check.min_passed}: the check was selected but did not run."
        )
    # Only this check's own modules count. A marker-filtered run reports the
    # collection skip of every module it could not import, and blaming this
    # check for an unrelated module's missing dependency would make the gate
    # cry wolf until someone turned it off.
    stems = tuple(Path(module).stem for module in check.modules)
    for test_id, reason in skips:
        if stems and not any(stem in test_id for stem in stems):
            continue
        lowered = reason.lower()
        if any(phrase.lower() in lowered for phrase in check.forbidden_skips):
            problems.append(
                "a selected test was skipped rather than run, reason "
                f"{reason!r}: the gate asked for this check and did not get it."
            )
            break
    return problems


def _stream(command: list[str], cwd: Path) -> tuple[int, list[str]]:
    """Run a command, echo its output live, and keep the tail for the report."""
    tail: list[str] = []
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        sys.stdout.write(line)
        tail.append(line.rstrip("\n"))
        if len(tail) > _TAIL_LINES:
            tail.pop(0)
    sys.stdout.flush()
    return process.wait(), [line for line in tail if line.strip()]


def run(args: argparse.Namespace) -> int:
    """Run one check's pytest command and write its verdict."""
    check = CHECKS[args.check]
    repo_root = Path(args.repo_root).resolve()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    junit = Path(args.junit) if args.junit else out.parent / f"{args.check}-junit.xml"
    junit.parent.mkdir(parents=True, exist_ok=True)
    label = f"{check.key}/{args.variant}" if args.variant else check.key

    command = [sys.executable, "-m", "pytest", f"--junitxml={junit}", *args.pytest_args]
    started = time.monotonic()
    print(f"::group::{label}", flush=True)
    print("running: " + " ".join(command), flush=True)
    returncode, tail = _stream(command, repo_root)
    duration = time.monotonic() - started
    print("::endgroup::", flush=True)

    totals = Totals()
    moved: list[str] = []
    skips: list[tuple[str, str]] = []
    if junit.is_file():
        try:
            totals, moved, skips = parse_junit(junit)
        except (ReportError, OSError) as exc:
            moved.append(clean(str(exc)))
    else:
        moved.append(
            "pytest wrote no junit report, so nothing can be said about what ran. "
            f"Its exit code was {returncode}."
        )

    canaries = apply_canaries(check, totals, skips, repo_root)
    status = FAILED if (returncode != 0 or canaries) else PASSED
    if status == FAILED and not moved:
        # Nothing failed at the test level: a coverage floor, a collection
        # error or a crash. The tail of the log is the only evidence there is.
        moved = [f"pytest exited {returncode} with no failing test. Last lines:"] + tail

    result: dict[str, Any] = {
        "check": check.key,
        "title": check.title,
        "variant": args.variant,
        "blocking": is_blocking(check.key, args.variant),
        "status": status,
        "exit_code": returncode,
        "duration_seconds": round(duration, 1),
        "totals": totals.as_dict(),
        "moved": moved[:_MAX_FAILURES_SHOWN],
        "moved_truncated": max(0, len(moved) - _MAX_FAILURES_SHOWN),
        "canaries": canaries,
    }
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    print(
        f"{label}: {status} ({totals.passed} passed, {totals.failures} failed, "
        f"{totals.errors} errored, {totals.skipped} skipped, {totals.xfailed} xfailed) "
        f"in {duration:.0f}s"
    )
    for problem in canaries:
        print(f"::error title=promotion gate canary::{label}: {problem}")
    if status == FAILED:
        for line in result["moved"]:
            print("  " + line.replace("\n", "\n  "))
    # --always-pass keeps a report-only job green; the verdict file still
    # records the failure and the summary still prints it.
    if status == FAILED and not args.always_pass:
        return 1
    return 0


def load_results(directory: Path) -> list[dict[str, Any]]:
    """Read every verdict JSON under ``directory``."""
    results: list[dict[str, Any]] = []
    for path in sorted(directory.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("check") in CHECKS:
            results.append(payload)
    return results


def add_missing(results: list[dict[str, Any]], needs: dict[str, Any]) -> list[dict[str, Any]]:
    """Add a MISSING entry for every expected verdict that never arrived.

    A job that was cancelled, timed out or ran out of disk uploads nothing.
    Without this the summary would simply not mention it, and a blocking
    check that never ran would read as a clean gate. Matrix legs are listed
    individually in ``Check.variants`` for the same reason: one leg dying
    while its siblings pass must not disappear into the siblings' verdicts.
    """
    seen = {(entry.get("check"), entry.get("variant", "")) for entry in results}
    merged = list(results)
    for key, check in CHECKS.items():
        for variant in check.variants:
            if (key, variant) in seen:
                continue
            outcome = (needs.get(key) or {}).get("result", "")
            merged.append(
                {
                    "check": key,
                    "title": check.title,
                    "variant": variant,
                    "blocking": is_blocking(key, variant),
                    "status": MISSING,
                    "exit_code": None,
                    "duration_seconds": None,
                    "totals": Totals().as_dict(),
                    "moved": [
                        "no verdict reached the summary; the job result was "
                        f"{outcome or 'unknown'}. Nothing was verified."
                    ],
                    "moved_truncated": 0,
                    "canaries": [],
                }
            )
    return merged


def gate_verdict(results: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """Decide whether the blocking set passed, and say why when it did not."""
    reasons: list[str] = []
    for entry in results:
        if not entry.get("blocking"):
            continue
        if entry.get("status") == PASSED:
            continue
        label = entry["check"] + (f"/{entry['variant']}" if entry.get("variant") else "")
        reasons.append(f"{label} {entry.get('status', MISSING)}")
    return (not reasons), reasons


def _row(entry: dict[str, Any]) -> str:
    totals = entry.get("totals") or {}
    label = entry["check"] + (f" ({entry['variant']})" if entry.get("variant") else "")
    policy = "blocks" if entry.get("blocking") else "reports"
    status = entry.get("status", MISSING)
    mark = {PASSED: "pass", FAILED: "FAIL", MISSING: "DID NOT RUN"}.get(status, status)
    counts = (
        f"{totals.get('passed', 0)} passed"
        f", {totals.get('failures', 0) + totals.get('errors', 0)} failed"
        f", {totals.get('skipped', 0)} skipped"
        f", {totals.get('xfailed', 0)} xfailed"
    )
    seconds = entry.get("duration_seconds")
    took = f"{seconds:.0f}s" if isinstance(seconds, (int, float)) else "-"
    return f"| {clean(label)} | {policy} | {mark} | {counts} | {took} |"


def _sort_key(entry: dict[str, Any]) -> tuple[int, str]:
    key = entry.get("check", "")
    index = REPORT_ORDER.index(key) if key in REPORT_ORDER else len(REPORT_ORDER)
    return index, str(entry.get("variant", ""))


def render_summary(results: list[dict[str, Any]], context: dict[str, str]) -> str:
    """Render the one report a person reads: job summary and tracking issue."""
    ordered = sorted(results, key=_sort_key)
    ok, reasons = gate_verdict(ordered)
    reporting_failed = [
        entry for entry in ordered if not entry.get("blocking") and entry.get("status") != PASSED
    ]

    lines: list[str] = ["<!-- promotion-gate-report -->", "## Promotion gate", ""]
    if ok:
        lines.append(
            "**Every blocking check passed.** `testing` is clear to be promoted to "
            "`main` by hand."
        )
    else:
        lines.append("**Do not promote.** Blocking check(s) failed: " + ", ".join(reasons) + ".")
    if reporting_failed:
        lines.append("")
        labels = sorted(
            entry["check"] + (f"/{entry['variant']}" if entry.get("variant") else "")
            for entry in reporting_failed
        )
        lines.append(
            "Report-only check(s) failed and did not block: "
            + ", ".join(labels)
            + ". Read what moved below before deciding."
        )
    lines.append("")
    lines.append(
        f"Commit `{clean(context.get('sha', ''))[:12]}` on `{clean(context.get('ref', ''))}`, "
        f"trigger {clean(context.get('event', '')) or 'unknown'}, "
        f"[run log]({clean(context.get('run_url', ''))})."
    )
    lines.append("")
    lines.append("| Check | Policy | Result | Tests | Time |")
    lines.append("| --- | --- | --- | --- | --- |")
    lines.extend(_row(entry) for entry in ordered)
    lines.append("")

    failures = [entry for entry in ordered if entry.get("status") != PASSED]
    if failures:
        lines.append("### What moved")
        lines.append("")
        for entry in failures:
            label = entry["check"] + (f" ({entry['variant']})" if entry.get("variant") else "")
            kind = "blocking" if entry.get("blocking") else "report only"
            lines.append(f"**{clean(label)}**, {kind}:")
            lines.append("")
            lines.append("```")
            for problem in entry.get("canaries") or []:
                lines.extend(_fenced("canary: " + str(problem)))
            for item in entry.get("moved") or []:
                lines.extend(_fenced(item))
            extra = entry.get("moved_truncated") or 0
            if extra:
                lines.append(f"... and {extra} more failing test(s); see the run log.")
            if not (entry.get("canaries") or entry.get("moved")):
                lines.append("no detail was recorded; see the run log.")
            lines.append("```")
            lines.append("")

    lines.append("### Policy")
    lines.append("")
    for key in REPORT_ORDER:
        check = CHECKS[key]
        verb = "blocks" if check.blocking else "reports"
        lines.append(f"- **{check.key}** ({verb}): {check.policy}")
    lines.append("")
    lines.append(
        "Promotion to `main` stays a manual step. This gate opens no pull request "
        "and merges nothing."
    )
    return "\n".join(lines) + "\n"


def summarize(args: argparse.Namespace) -> int:
    """Collate every verdict, render the report, decide the gate."""
    results_dir = Path(args.results)
    results = load_results(results_dir) if results_dir.is_dir() else []
    needs: dict[str, Any] = {}
    if args.needs and Path(args.needs).is_file():
        try:
            loaded = json.loads(Path(args.needs).read_text(encoding="utf-8"))
            needs = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            needs = {}
    results = add_missing(results, needs)
    context = {
        "sha": args.sha or os.environ.get("GITHUB_SHA", ""),
        "ref": args.ref or os.environ.get("GITHUB_REF_NAME", ""),
        "run_url": args.run_url,
        "event": args.event or os.environ.get("GITHUB_EVENT_NAME", ""),
    }
    body = render_summary(results, context)
    Path(args.out).write_text(body, encoding="utf-8")
    print(body)
    ok, _ = gate_verdict(results)
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="promotion gate runner and reporter")
    sub = parser.add_subparsers(dest="command", required=True)

    runner = sub.add_parser("run", help="run one check and write its verdict")
    runner.add_argument("--check", required=True, choices=sorted(CHECKS))
    runner.add_argument("--variant", default="", help="matrix leg, e.g. ubuntu-latest")
    runner.add_argument("--out", required=True, help="verdict JSON to write")
    runner.add_argument("--junit", default="", help="junit report path (default: next to --out)")
    runner.add_argument("--repo-root", default=".", help="checkout to run pytest in")
    runner.add_argument(
        "--always-pass",
        action="store_true",
        help="exit 0 even when the check failed (report-only jobs)",
    )
    runner.add_argument("pytest_args", nargs="*", help="pytest arguments, after a -- separator")

    summary = sub.add_parser("summarize", help="collate verdicts into one report")
    summary.add_argument("--results", required=True, help="directory of verdict JSON files")
    summary.add_argument("--needs", default="", help="JSON file holding the needs context")
    summary.add_argument("--out", required=True, help="markdown file to write")
    summary.add_argument("--sha", default="")
    summary.add_argument("--ref", default="")
    summary.add_argument("--run-url", default="")
    summary.add_argument("--event", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        pytest_args = [arg for arg in args.pytest_args]
        while pytest_args and pytest_args[0] == "--":
            pytest_args = pytest_args[1:]
        if not pytest_args:
            print("::error::no pytest arguments were given", file=sys.stderr)
            return 2
        args.pytest_args = pytest_args
        return run(args)
    return summarize(args)


if __name__ == "__main__":
    sys.exit(main())
