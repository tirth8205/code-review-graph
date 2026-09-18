"""Tests for the promotion gate: scripts/promotion_gate.py and its workflow.

These are fast and always on. They are the teeth of the gate itself, which
is otherwise a YAML file nothing exercises until a release is at stake:

* the blocking policy is asserted here, not just written down;
* the canaries that stop a check from passing without running are each
  driven to failure with a real junit report;
* the workflow is parsed and checked against the registry, so a check
  cannot be added to one and forgotten in the other, and it is asserted not
  to run on pull requests.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "promotion_gate.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "promotion-gate.yml"

_spec = importlib.util.spec_from_file_location("promotion_gate", SCRIPT)
assert _spec is not None and _spec.loader is not None
gate = importlib.util.module_from_spec(_spec)
# @dataclass resolves annotations through sys.modules, so the module has to be
# registered before it is executed.
sys.modules["promotion_gate"] = gate
_spec.loader.exec_module(gate)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def junit(cases: str) -> str:
    return f'<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite>{cases}</testsuite></testsuites>'  # noqa: E501


def case(name: str, body: str = "") -> str:
    return f'<testcase classname="tests.test_x" name="{name}">{body}</testcase>'


def verdict(check: str, status: str, variant: str = "", **extra: object) -> dict:
    payload = {
        "check": check,
        "title": gate.CHECKS[check].title,
        "variant": variant,
        "blocking": gate.is_blocking(check, variant),
        "status": status,
        "exit_code": 0 if status == gate.PASSED else 1,
        "duration_seconds": 1.0,
        "totals": {
            "tests": 1,
            "passed": 1,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
            "xfailed": 0,
        },
        "moved": [],
        "moved_truncated": 0,
        "canaries": [],
    }
    payload.update(extra)
    return payload


def all_green() -> list[dict]:
    return [
        verdict(key, gate.PASSED, variant)
        for key, check in gate.CHECKS.items()
        for variant in check.variants
    ]


# ---------------------------------------------------------------------------
# the policy itself
# ---------------------------------------------------------------------------


def test_the_blocking_set_is_exactly_what_contributing_documents():
    blocking = {key for key, check in gate.CHECKS.items() if check.blocking}
    assert blocking == {"upgrade-path", "packaging", "determinism", "suite", "e2e"}
    reporting = {key for key, check in gate.CHECKS.items() if not check.blocking}
    assert reporting == {"corpus", "browser"}


def test_the_network_cloning_check_never_blocks():
    # A failed clone of somebody else's repository must not stop a release.
    assert gate.is_blocking("corpus") is False
    assert gate.is_blocking("corpus", "") is False


def test_the_upgrade_check_always_blocks():
    # A graph a user already has is not ours to lose.
    assert gate.is_blocking("upgrade-path") is True


def test_only_the_windows_leg_of_e2e_reports():
    assert gate.is_blocking("e2e", "ubuntu-latest") is True
    assert gate.is_blocking("e2e", "macos-latest") is True
    assert gate.is_blocking("e2e", "windows-latest") is False


def test_every_check_is_ordered_and_explained():
    assert set(gate.REPORT_ORDER) == set(gate.CHECKS)
    for key, check in gate.CHECKS.items():
        assert check.policy.startswith(("Blocks", "Reports")), key
        assert len(check.policy) > 80, f"{key} has no stated reason"


# ---------------------------------------------------------------------------
# reading a junit report
# ---------------------------------------------------------------------------


def test_an_xfail_is_not_counted_as_a_skip(tmp_path: Path):
    # Three of these checks record known bugs as strict xfails. pytest writes
    # an xfail as a <skipped> element, so counting it as a skip would make
    # every recorded bug look like a check that never ran.
    report = tmp_path / "j.xml"
    report.write_text(
        junit(
            case("ok")
            + case("known_bug", '<skipped type="pytest.xfail" message="bug 1"/>')
            + case("absent", '<skipped type="pytest.skip" message="no igraph"/>')
        ),
        encoding="utf-8",
    )
    totals, moved, skips = gate.parse_junit(report)
    assert (totals.tests, totals.passed, totals.xfailed, totals.skipped) == (3, 1, 1, 1)
    assert moved == []
    assert skips == [("tests.test_x::absent", "no igraph")]


def test_a_failure_is_reported_with_the_message_that_says_what_moved(tmp_path: Path):
    report = tmp_path / "j.xml"
    report.write_text(
        junit(
            case(
                "test_files_parsed",
                '<failure message="AssertionError: files_parsed fell below its band: '
                'baseline=109 measured=11 delta=-98 (-89.9%)">long traceback</failure>',
            )
        ),
        encoding="utf-8",
    )
    totals, moved, _ = gate.parse_junit(report)
    assert totals.failures == 1
    assert "test_files_parsed" in moved[0]
    assert "baseline=109 measured=11 delta=-98" in moved[0]


def test_a_report_carrying_a_dtd_is_refused(tmp_path: Path):
    report = tmp_path / "j.xml"
    report.write_text(
        '<!DOCTYPE t [<!ENTITY a "aaaa">]><testsuites><testsuite/></testsuites>',
        encoding="utf-8",
    )
    with pytest.raises(gate.ReportError):
        gate.parse_junit(report)


# ---------------------------------------------------------------------------
# canaries: a check that did not run must not pass
# ---------------------------------------------------------------------------


def test_a_check_whose_module_is_absent_fails(tmp_path: Path):
    totals = gate.Totals(tests=40, passed=40)
    problems = gate.apply_canaries(gate.CHECKS["upgrade-path"], totals, [], tmp_path)
    assert problems
    assert "tests/test_upgrade_path.py is not in this checkout" in problems[0]


def test_a_check_that_collected_almost_nothing_fails():
    totals = gate.Totals(tests=1, passed=1)
    problems = gate.apply_canaries(gate.CHECKS["corpus"], totals, [], REPO_ROOT)
    assert any("only 1 test(s) were collected" in p for p in problems)


def test_a_check_skipped_by_its_own_opt_in_hook_fails():
    # The exact reason string tests/conftest.py attaches on the corpus branch.
    totals = gate.Totals(tests=24, skipped=24)
    skips = [
        (
            "tests.test_real_repo_corpus::test_corpus[fastapi]",
            "pinned real-repository corpus: run it with `pytest -m corpus`",
        )
    ] * 24
    problems = gate.apply_canaries(gate.CHECKS["corpus"], totals, skips, REPO_ROOT)
    assert any("skipped rather than run" in p for p in problems)


def test_browser_tests_skipped_for_a_missing_playwright_fail():
    totals = gate.Totals(tests=1, skipped=1)
    skips = [
        (
            "tests.test_visualization_browser",
            'collection skipped Playwright is not installed; install the "browser-test" extra',
        )
    ]
    problems = gate.apply_canaries(gate.CHECKS["browser"], totals, skips, REPO_ROOT)
    assert problems, "an absent dependency must not read as a passing browser check"


def test_another_module_s_collection_skip_is_not_blamed_on_this_check(tmp_path: Path):
    # `pytest -m e2e` also reports the collection skip of the browser module
    # when Playwright is absent. Blaming e2e for it would make the gate cry
    # wolf on every green run, which is how a canary gets switched off.
    for module in gate.CHECKS["e2e"].modules:
        (tmp_path / module).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / module).write_text("", encoding="utf-8")
    totals = gate.Totals(tests=27, passed=26, skipped=1)
    skips = [
        (
            "tests.test_visualization_browser",
            "collection skipped Playwright is not installed",
        )
    ]
    assert gate.apply_canaries(gate.CHECKS["e2e"], totals, skips, tmp_path) == []


def test_a_real_run_clears_every_canary(tmp_path: Path):
    # The canary of the canaries: the rules must not fail a check that did
    # run, or the gate would be permanently red and get switched off.
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_upgrade_path.py").write_text("", encoding="utf-8")
    totals = gate.Totals(tests=46, passed=40, xfailed=6)
    assert gate.apply_canaries(gate.CHECKS["upgrade-path"], totals, [], tmp_path) == []


# ---------------------------------------------------------------------------
# the verdict
# ---------------------------------------------------------------------------


def test_a_failing_report_only_check_does_not_block():
    results = all_green()
    for entry in results:
        if entry["check"] == "corpus":
            entry["status"] = gate.FAILED
    ok, reasons = gate.gate_verdict(results)
    assert ok is True
    assert reasons == []


def test_a_failing_blocking_check_blocks():
    results = all_green()
    for entry in results:
        if entry["check"] == "upgrade-path":
            entry["status"] = gate.FAILED
    ok, reasons = gate.gate_verdict(results)
    assert ok is False
    assert reasons == ["upgrade-path failed"]


def test_one_dead_matrix_leg_does_not_hide_behind_its_siblings():
    # suite/3.11 never reports; the other three pass. Without the per-variant
    # expectation this reads as a clean gate.
    results = [entry for entry in all_green() if entry.get("variant") != "3.11"]
    merged = gate.add_missing(results, {"suite": {"result": "cancelled"}})
    ok, reasons = gate.gate_verdict(merged)
    assert ok is False
    assert reasons == ["suite/3.11 missing"]
    body = gate.render_summary(merged, {})
    assert "the job result was cancelled" in body


def test_a_windows_leg_that_never_reported_does_not_block():
    results = [
        entry for entry in all_green() if entry.get("variant") != "windows-latest"
    ]
    merged = gate.add_missing(results, {"e2e": {"result": "failure"}})
    ok, _ = gate.gate_verdict(merged)
    assert ok is True
    assert any(
        entry["variant"] == "windows-latest" and entry["status"] == gate.MISSING
        for entry in merged
    )


# ---------------------------------------------------------------------------
# the report a person reads
# ---------------------------------------------------------------------------


def test_the_summary_says_do_not_promote_and_names_what_moved():
    results = all_green()
    for entry in results:
        if entry["check"] == "determinism":
            entry["status"] = gate.FAILED
            entry["moved"] = ["test_repeat_build\n    2 section(s) differ: nodes, edges"]
    body = gate.render_summary(results, {"sha": "abcdef1234567890", "ref": "testing"})
    assert "Do not promote" in body
    assert "determinism failed" in body
    assert "2 section(s) differ: nodes, edges" in body
    assert "| determinism | blocks | FAIL |" in body


def test_the_summary_says_so_when_only_a_report_only_check_failed():
    results = all_green()
    for entry in results:
        if entry["check"] == "browser":
            entry["status"] = gate.FAILED
    body = gate.render_summary(results, {})
    assert "Every blocking check passed" in body
    assert "did not block: browser" in body


def test_a_failing_windows_leg_is_named_with_its_leg_not_just_its_check():
    results = all_green()
    for entry in results:
        if entry["check"] == "e2e" and entry["variant"] == "windows-latest":
            entry["status"] = gate.FAILED
    body = gate.render_summary(results, {})
    assert "did not block: e2e/windows-latest" in body


def test_a_clean_run_reads_as_clean():
    body = gate.render_summary(all_green(), {"ref": "testing"})
    assert "Every blocking check passed" in body
    assert "### What moved" not in body
    assert "Promotion to `main` stays a manual step" in body


def test_the_summary_states_the_policy_for_every_check():
    body = gate.render_summary(all_green(), {})
    for key in gate.CHECKS:
        assert f"**{key}**" in body


def test_control_characters_never_reach_the_report():
    results = all_green()
    for entry in results:
        if entry["check"] == "corpus":
            entry["status"] = gate.FAILED
            entry["moved"] = ["bad\x00name\x07here"]
    body = gate.render_summary(results, {})
    assert "\x00" not in body and "\x07" not in body
    assert "badnamehere" in body


def test_a_failure_message_cannot_close_the_code_fence_early():
    results = all_green()
    for entry in results:
        if entry["check"] == "corpus":
            entry["status"] = gate.FAILED
            entry["moved"] = ["```\n## injected heading"]
    body = gate.render_summary(results, {})
    assert "## injected heading" in body
    assert body.count("```") % 2 == 0


# ---------------------------------------------------------------------------
# end to end through the command line
# ---------------------------------------------------------------------------


def test_run_records_a_verdict_and_fails_a_blocking_check(tmp_path: Path):
    failing = tmp_path / "test_tiny.py"
    failing.write_text("def test_one():\n    assert 1 == 2, 'one is not two'\n", encoding="utf-8")
    out = tmp_path / "verdict.json"
    code = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "run",
            "--check",
            "suite",
            "--variant",
            "3.12",
            "--out",
            str(out),
            "--repo-root",
            str(tmp_path),
            "--",
            str(failing),
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).returncode
    assert code == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == gate.FAILED
    assert payload["blocking"] is True
    assert any("one is not two" in item for item in payload["moved"])


def test_run_keeps_a_report_only_job_green_but_records_the_failure(tmp_path: Path):
    failing = tmp_path / "test_tiny.py"
    failing.write_text("def test_one():\n    assert 0\n", encoding="utf-8")
    out = tmp_path / "verdict.json"
    code = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "run",
            "--check",
            "corpus",
            "--always-pass",
            "--out",
            str(out),
            "--repo-root",
            str(tmp_path),
            "--",
            str(failing),
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).returncode
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == gate.FAILED
    assert payload["blocking"] is False


def test_summarize_exits_one_when_a_blocking_verdict_is_missing(tmp_path: Path):
    results = tmp_path / "results"
    results.mkdir()
    for entry in all_green():
        if entry["check"] == "packaging":
            continue
        name = f"{entry['check']}-{entry['variant'] or 'only'}.json"
        (results / name).write_text(json.dumps(entry), encoding="utf-8")
    out = tmp_path / "report.md"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "summarize",
            "--results",
            str(results),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert "packaging missing" in out.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# the workflow has to agree with the registry
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_gate_never_runs_on_pull_requests(workflow: dict):
    # Requirement, not taste: every job here takes minutes, and a contributor
    # must not wait for them.
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"]["branches"] == ["testing"]


def test_every_check_has_a_job_that_runs_it(workflow: dict):
    text = WORKFLOW.read_text(encoding="utf-8")
    for key in gate.CHECKS:
        assert key in workflow["jobs"], f"{key} has no job"
        assert f"--check {key}" in text, f"{key} is never run"


def test_every_job_feeds_the_report(workflow: dict):
    needs = workflow["jobs"]["report"]["needs"]
    assert set(needs) == set(gate.CHECKS)
    assert workflow["jobs"]["report"]["if"] == "always()"


def test_every_check_job_has_a_timeout(workflow: dict):
    for name, job in workflow["jobs"].items():
        assert isinstance(job.get("timeout-minutes"), int), f"{name} has no timeout"


def test_every_job_uses_the_pip_cache(workflow: dict):
    for name, job in workflow["jobs"].items():
        setups = [
            step
            for step in job["steps"]
            if isinstance(step.get("uses"), str) and step["uses"].startswith("actions/setup-python")
        ]
        assert setups, f"{name} sets up no Python"
        for step in setups:
            assert step["with"]["cache"] == "pip", name


def test_the_matrix_legs_named_in_the_registry_are_the_ones_the_workflow_runs(workflow: dict):
    jobs = workflow["jobs"]
    assert jobs["e2e"]["strategy"]["matrix"]["os"] == list(gate.CHECKS["e2e"].variants)
    versions = jobs["suite"]["strategy"]["matrix"]["python-version"]
    assert versions == list(gate.CHECKS["suite"].variants)


def test_the_coverage_job_excludes_every_slow_check(workflow: dict):
    steps = workflow["jobs"]["suite"]["steps"]
    command = next(step["run"] for step in steps if step.get("name") == "Run the full suite")
    for marker in ("browser", "upgrade", "packaging", "corpus", "determinism"):
        assert f"not {marker}" in command


def test_the_report_job_posts_to_the_job_summary_and_one_issue(workflow: dict):
    text = yaml.dump(workflow["jobs"]["report"])
    assert "GITHUB_STEP_SUMMARY" in text
    assert "gh issue" in text
    assert "gh pr create" not in text, "the gate must not open a promotion pull request"
