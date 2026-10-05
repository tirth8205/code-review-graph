"""Tests for the daily promotion: scripts/auto_promote.py and its workflow.

The workflow merges a pull request into a protected branch without a person
in the room, every day, so the decision that lets it do so has to be driven
from a test rather than trusted. Everything here is offline: the payloads are
the shapes GitHub really returns, recorded from this repository.

Two groups matter more than the rest.

*It can never promote to the release branch.* Asserted against the script,
against its command line and against the YAML, not merely intended. And not
only against what the job *asks for*: a pull request's base branch is mutable
by its author and changing it re-runs no workflow, so the pull request the
job is about to merge is re-read and re-checked at the door.

*The pull request it merges is the one it decided on.* `gh pr list --head
staging` matches a branch called `staging` in any of this repository's
thousands of forks, and a pull request head tracks a branch rather than a
commit, so both "whose pull request is this" and "which commit is it at" are
questions with wrong answers available.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "auto_promote.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "auto-promote.yml"

_spec = importlib.util.spec_from_file_location("auto_promote", SCRIPT)
assert _spec is not None and _spec.loader is not None
promote = importlib.util.module_from_spec(_spec)
# @dataclass resolves annotations through sys.modules, so the module has to be
# registered before it is executed.
sys.modules["auto_promote"] = promote
_spec.loader.exec_module(promote)


# ---------------------------------------------------------------------------
# helpers: the payload shapes GitHub actually returns
# ---------------------------------------------------------------------------

REQUIRED = (
    "lint",
    "type-check",
    "security",
    "schema-sync",
    "test (3.10)",
    "test (3.11)",
    "test (3.12)",
    "test (3.13)",
)

HEAD_SHA = "a" * 40
BASE_SHA = "b" * 40


def rules(contexts: tuple[str, ...] = REQUIRED) -> list[dict]:
    """The body of GET /repos/O/R/rules/branches/testing."""
    return [
        {"type": "deletion", "ruleset_id": 23449895},
        {"type": "non_fast_forward", "ruleset_id": 23449895},
        {
            "type": "pull_request",
            "parameters": {
                "required_approving_review_count": 0,
                "require_last_push_approval": False,
                "require_extra_approval_for_unattributed_changes": True,
                "allowed_merge_methods": ["merge"],
            },
            "ruleset_id": 23449895,
        },
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": False,
                "required_status_checks": [{"context": name} for name in contexts],
            },
            "ruleset_id": 23449895,
        },
    ]


def run(name: str, conclusion: str | None = "success", status: str = "completed", when: str = "5"):
    return {"name": name, "status": status, "conclusion": conclusion, "completed_at": when}


def all_green(extra: list[dict] | None = None) -> list[dict]:
    runs = [run(name) for name in REQUIRED]
    runs.extend(extra or [])
    return promote.normalise_reports({"check_runs": runs})


def pr(**overrides) -> dict:
    """One entry of `gh pr list --json ...`, as this workflow asks for it.

    The default is the pull request the workflow itself opened yesterday:
    this repository's own branch, correctly aimed, carrying the marker label,
    and at the commit whose checks were read.
    """
    entry = {
        "number": 991,
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "headRefOid": HEAD_SHA,
        "baseRefName": "testing",
        "headRefName": "staging",
        "isCrossRepository": False,
        "headRepositoryOwner": {"login": "tirth8205"},
        "state": "OPEN",
        "labels": [{"name": "promotion"}, {"name": promote.AUTO_LABEL}],
    }
    entry.update(overrides)
    return entry


def gate(state: str = promote.GATE_PASSED) -> promote.GateVerdict:
    return promote.GateVerdict(state, "https://example.invalid/run/1", "")


def decide(**kwargs):
    base = {
        "commits": 12,
        "rules": rules(),
        "reports": all_green(),
        "pull_request": None,
        "head_sha": HEAD_SHA,
        "base_sha": BASE_SHA,
    }
    base.update(kwargs)
    return promote.decide(**base)


# ---------------------------------------------------------------------------
# reading the payloads
# ---------------------------------------------------------------------------


def test_required_contexts_come_from_the_ruleset_not_from_a_hardcoded_list():
    assert promote.required_contexts(rules()) == REQUIRED
    # A renamed CI job changes the answer, which is the entire point.
    assert promote.required_contexts(rules(("lint", "test (3.14)"))) == ("lint", "test (3.14)")


def test_required_contexts_is_empty_when_there_is_no_such_rule():
    assert promote.required_contexts([{"type": "deletion"}]) == ()
    assert promote.required_contexts(None) == ()


def test_commit_statuses_satisfy_a_required_context_too():
    reports = promote.normalise_reports(
        {"statuses": [{"context": "lint", "state": "success", "updated_at": "9"}]}
    )
    assert promote.classify(("lint",), reports)[0].state == promote.GREEN


def test_a_rerun_supersedes_the_older_failing_run():
    # A re-run leaves the failed check run on the commit. So does opening a
    # pull request on a commit that already had a push run: the head SHA then
    # carries two of everything. The newest by timestamp is the current one.
    reports = promote.normalise_reports(
        {
            "check_runs": [
                run("lint", "failure", when="1"),
                run("lint", "success", when="2"),
            ]
        }
    )
    assert promote.classify(("lint",), reports)[0].state == promote.GREEN


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------


def test_ready_when_staging_is_ahead_and_every_required_check_is_green():
    decision = decide()
    assert decision.state == promote.READY
    assert decision.ready and not decision.escalate
    assert decision.commits == 12
    assert [item.state for item in decision.contexts] == [promote.GREEN] * len(REQUIRED)


def test_a_check_that_is_green_but_not_required_cannot_hold_a_promotion():
    decision = decide(reports=all_green([run("Visualization browser tests", "failure")]))
    assert decision.state == promote.READY


def test_nothing_to_promote_is_not_ready_and_not_blocked():
    decision = decide(commits=0)
    assert decision.state == promote.NOT_READY
    assert not decision.escalate
    assert "no commits that `testing` lacks" in decision.reason


def test_identical_tips_are_nothing_to_promote():
    assert decide(head_sha=BASE_SHA).state == promote.NOT_READY


def test_unreadable_required_contexts_block_rather_than_promote_blind():
    decision = decide(rules=[])
    assert decision.state == promote.BLOCKED
    assert "Promoting blind is not an option" in decision.reason


def test_a_failing_required_check_blocks_and_names_it():
    reports = promote.normalise_reports(
        {"check_runs": [run(name) for name in REQUIRED[:-1]] + [run(REQUIRED[-1], "failure")]}
    )
    decision = decide(reports=reports)
    assert decision.state == promote.BLOCKED
    assert "test (3.13)" in decision.reason
    # A red required check is already a red CI run on staging. Reporting it
    # red again every morning is how a daily mail gets ignored.
    assert not decision.escalate


def test_a_pending_required_check_is_not_ready_because_tomorrow_fixes_it():
    reports = promote.normalise_reports(
        {
            "check_runs": [run(name) for name in REQUIRED[:-1]]
            + [run(REQUIRED[-1], None, status="in_progress")]
        }
    )
    decision = decide(reports=reports)
    assert decision.state == promote.NOT_READY
    assert "still running" in decision.reason


def test_a_required_context_absent_from_the_head_sha_blocks():
    reports = promote.normalise_reports({"check_runs": [run(name) for name in REQUIRED[:-1]]})
    decision = decide(reports=reports)
    assert decision.state == promote.BLOCKED
    assert "never reported" in decision.reason
    assert "test (3.13)" in decision.reason


def test_an_absent_context_is_only_not_ready_while_something_is_still_running():
    reports = promote.normalise_reports(
        {
            "check_runs": [run(name) for name in REQUIRED[:-2]]
            + [run(REQUIRED[-2], None, status="queued")]
        }
    )
    decision = decide(reports=reports)
    assert decision.state == promote.NOT_READY


@pytest.mark.parametrize("conclusion", ["skipped", "neutral"])
def test_a_skipped_required_check_is_not_treated_as_a_pass(conclusion: str):
    # GitHub's own required-status-check evaluation accepts both. This gate
    # does not: a job that did not run verified nothing.
    reports = promote.normalise_reports(
        {"check_runs": [run(name) for name in REQUIRED[:-1]] + [run(REQUIRED[-1], conclusion)]}
    )
    decision = decide(reports=reports)
    assert decision.state == promote.BLOCKED
    assert "skipped rather than run" in decision.reason


def test_a_failing_check_is_reported_before_a_blocked_pull_request():
    # GitHub would call this "the pull request is blocked". The failing check
    # is the useful sentence, so it wins.
    reports = promote.normalise_reports(
        {"check_runs": [run(name) for name in REQUIRED[:-1]] + [run(REQUIRED[-1], "failure")]}
    )
    decision = decide(reports=reports, pull_request=pr(mergeStateStatus="DIRTY"))
    assert "test (3.13)" in decision.reason


# ---------------------------------------------------------------------------
# an already-open promotion pull request
# ---------------------------------------------------------------------------


def test_an_open_mergeable_pull_request_is_resumed_not_duplicated():
    decision = decide(pull_request=pr())
    assert decision.state == promote.READY
    assert decision.pr_number == 991


def test_an_open_pull_request_with_conflicts_blocks():
    decision = decide(pull_request=pr(mergeable="CONFLICTING", mergeStateStatus="DIRTY"))
    assert decision.state == promote.BLOCKED
    assert "conflicts" in decision.reason
    assert decision.pr_number == 991
    # This one is the automation stuck on its own pull request, so it is red.
    assert decision.escalate


def test_mergeability_not_yet_computed_is_not_ready():
    decision = decide(pull_request=pr(mergeable=None, mergeStateStatus="UNKNOWN"))
    assert decision.state == promote.NOT_READY
    assert not decision.escalate


def test_a_draft_promotion_pull_request_blocks():
    decision = decide(pull_request=pr(isDraft=True))
    assert decision.state == promote.BLOCKED
    assert "draft" in decision.reason


def test_the_rest_api_spelling_of_mergeability_is_understood_too():
    conflicting = pr(mergeable=False, mergeStateStatus=None, mergeable_state="dirty")
    assert decide(pull_request=conflicting).state == promote.BLOCKED
    clean = pr(mergeable=True, mergeStateStatus=None, mergeable_state="clean")
    assert decide(pull_request=clean).state == promote.READY


# ---------------------------------------------------------------------------
# which pull request is ours
#
# `gh pr list --base testing --head staging` matches a branch called
# `staging` in ANY repository; `gh pr list --help` says outright that
# "<owner>:<branch>" syntax is not supported. On a public repository with
# thousands of forks that makes "the open promotion pull request" an
# attacker-supplied value.
# ---------------------------------------------------------------------------


def test_a_pull_request_from_a_fork_is_never_the_one_that_gets_merged():
    fork = pr(number=1031, isCrossRepository=True, headRepositoryOwner={"login": "stranger"})
    selection = promote.select_promotion_pr([fork])
    assert selection.chosen is None
    assert selection.rejected and selection.rejected[0][0] == 1031

    decision = decide(pull_request=promote.select_promotion_pr([fork]))
    assert decision.state == promote.BLOCKED
    assert decision.pr_number is None
    assert "#1031" in decision.reason and "another repository" in decision.reason
    # Loud, not quiet: a stranger parking a fork branch here must not be able
    # to stall the promotion behind a green run for ever.
    assert decision.escalate


def test_a_fork_pull_request_cannot_silently_stall_the_promotion():
    # The denial-of-service form of the same defect: a fork pull request that
    # is merely conflicting used to make every run decide BLOCKED and exit 0.
    fork = pr(number=1031, isCrossRepository=True, mergeStateStatus="DIRTY")
    decision = decide(pull_request=promote.select_promotion_pr([fork]))
    assert decision.state == promote.BLOCKED and decision.escalate


def test_a_pull_request_aimed_somewhere_else_is_never_chosen():
    # A pull request's base is mutable by whoever can write to its head
    # branch, and changing it starts no `pull_request` workflow run, so the
    # green checks stay put. Merging by number alone would merge into it.
    for base in ("main", "master", "some-branch"):
        selection = promote.select_promotion_pr([pr(baseRefName=base)])
        assert selection.chosen is None, base
    assert promote.select_promotion_pr([pr(headRefName="patch-1")]).chosen is None


def test_a_promotion_pull_request_a_person_opened_is_left_alone():
    # `promote.yml`'s whole contract is that a person merges what it opens.
    # Without the marker label the daily job cannot tell the two apart, and
    # a pull request opened at 22:00 to read over coffee gets merged before
    # breakfast -- with its body overwritten first.
    mine = pr(number=1040, labels=[{"name": "promotion"}])
    selection = promote.select_promotion_pr([mine])
    assert selection.chosen is None
    decision = decide(pull_request=selection)
    assert decision.state == promote.BLOCKED
    assert "opened by a person" in decision.reason
    assert decision.pr_number is None


def test_the_marker_label_is_what_makes_a_pull_request_ours():
    assert promote.select_promotion_pr([pr()]).chosen is not None
    assert promote.AUTO_LABEL != promote.PROMOTION_LABEL
    assert promote.pr_labels(pr()) == {"promotion", promote.AUTO_LABEL}
    assert promote.pr_labels({"labels": ["promotion"]}) == {"promotion"}


def test_select_promotion_pr_reads_the_gh_array():
    assert promote.select_promotion_pr([]).chosen is None
    assert promote.select_promotion_pr([]).number is None
    assert promote.select_promotion_pr([pr(number=3)]).number == 3


def test_a_second_valid_promotion_pull_request_is_reported_not_silently_ignored():
    selection = promote.select_promotion_pr([pr(number=9), pr(number=8)])
    assert selection.number == 9
    assert selection.rejected[0][0] == 8


# ---------------------------------------------------------------------------
# the commit whose checks were read is the commit that gets merged
# ---------------------------------------------------------------------------


def test_a_pull_request_whose_head_moved_is_not_ready_rather_than_merged():
    # The head tracks a branch, so a merge into `staging` mid-run moves it.
    # Those commits were verified by nothing.
    decision = decide(pull_request=pr(headRefOid="c" * 40))
    assert decision.state == promote.NOT_READY
    assert "moved" in decision.reason
    assert not decision.escalate


def test_the_door_check_refuses_a_pull_request_that_changed_under_the_run():
    promote.assert_pr_is_ours(pr(), HEAD_SHA)  # the good case does not raise
    for bad in (
        pr(baseRefName="main"),
        pr(headRefName="evil"),
        pr(isCrossRepository=True),
        pr(labels=[{"name": "promotion"}]),
        pr(state="CLOSED"),
    ):
        with pytest.raises(promote.ForeignPullRequestError):
            promote.assert_pr_is_ours(bad, HEAD_SHA)
    with pytest.raises(promote.ForeignPullRequestError):
        promote.assert_pr_is_ours(pr(headRefOid="d" * 40), HEAD_SHA)


def test_the_door_check_names_the_release_branch_when_that_is_where_it_was_pointed():
    with pytest.raises(promote.ForeignPullRequestError) as raised:
        promote.assert_pr_is_ours(pr(baseRefName="main"), HEAD_SHA)
    assert "`main`" in str(raised.value)


def test_verify_tells_a_moved_head_apart_from_a_tampered_pull_request(tmp_path: Path):
    def check(payload: dict, expect_sha: str = HEAD_SHA) -> int:
        target = tmp_path / "pr.json"
        target.write_text(json.dumps(payload), encoding="utf-8")
        return promote.main(
            [
                "verify",
                "--pull-request", str(target),
                "--expect-number", str(payload.get("number", 991)),
                "--expect-head-sha", expect_sha,
                "--summary", str(tmp_path / "out.md"),
            ]
        )

    assert check(pr()) == promote.VERIFY_OK
    # Somebody moved the pull request: red run.
    assert check(pr(baseRefName="main")) == promote.VERIFY_FOREIGN
    assert check(pr(isCrossRepository=True)) == promote.VERIFY_FOREIGN
    # `staging` simply moved: quiet stop, tomorrow promotes the newer commit.
    assert check(pr(headRefOid="e" * 40)) == promote.VERIFY_MOVED
    assert "Stopped before merging" in (tmp_path / "out.md").read_text(encoding="utf-8")


def test_verify_refuses_a_pull_request_that_is_not_the_one_it_was_given(tmp_path: Path):
    target = tmp_path / "pr.json"
    target.write_text(json.dumps(pr(number=4242)), encoding="utf-8")
    code = promote.main(
        ["verify", "--pull-request", str(target), "--expect-number", "991"]
    )
    assert code == promote.VERIFY_FOREIGN
    target.write_text("[]", encoding="utf-8")
    assert (
        promote.main(["verify", "--pull-request", str(target), "--expect-number", "991"])
        == promote.VERIFY_FOREIGN
    )


# ---------------------------------------------------------------------------
# the release gate on the branch being promoted into
# ---------------------------------------------------------------------------


def test_a_failed_release_gate_on_testing_stops_tomorrows_promotion():
    # Before this workflow existed, every landing on `testing` was a
    # maintainer's decision, and that is what gave the gate's "blocks"
    # verdict teeth. Reading it here is what replaces that.
    decision = decide(gate=gate(promote.GATE_FAILED))
    assert decision.state == promote.BLOCKED
    assert "promotion gate" in decision.reason
    assert "re-run" in " ".join(decision.notes)


def test_a_running_release_gate_waits_for_its_verdict():
    assert decide(gate=gate(promote.GATE_RUNNING)).state == promote.NOT_READY


def test_a_passing_or_unknown_release_gate_does_not_hold_the_promotion():
    assert decide(gate=gate(promote.GATE_PASSED)).state == promote.READY
    assert decide(gate=gate(promote.GATE_MISSING)).state == promote.READY
    assert decide(gate=None).state == promote.READY


def test_the_gate_verdict_is_read_for_the_current_tip_and_no_other_commit():
    runs = {
        "workflow_runs": [
            {"head_sha": BASE_SHA, "status": "completed", "conclusion": "failure", "html_url": "u"},
            {"head_sha": "z" * 40, "status": "completed", "conclusion": "success"},
        ]
    }
    assert promote.gate_verdict(runs, BASE_SHA).state == promote.GATE_FAILED
    # A verdict about an earlier commit says nothing about this one.
    assert promote.gate_verdict(runs, "y" * 40).state == promote.GATE_MISSING
    assert promote.gate_verdict({}, BASE_SHA).state == promote.GATE_MISSING
    running = {"workflow_runs": [{"head_sha": BASE_SHA, "status": "in_progress"}]}
    assert promote.gate_verdict(running, BASE_SHA).state == promote.GATE_RUNNING
    cancelled = {
        "workflow_runs": [{"head_sha": BASE_SHA, "status": "completed", "conclusion": "cancelled"}]
    }
    assert promote.gate_verdict(cancelled, BASE_SHA).state == promote.GATE_MISSING


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def test_the_body_matches_the_manual_promote_workflow():
    manual = (REPO_ROOT / ".github" / "workflows" / "promote.yml").read_text(encoding="utf-8")
    body = promote.render_body(
        ["abc1234 Fix a thing (#910) (Someone)"], run_url="https://x/1", head_sha=HEAD_SHA
    )
    assert body.startswith("## Promote `staging` -> `testing`")
    for heading in ("### Included", "### Pull requests referenced"):
        assert heading in body and heading in manual
    assert "Merge with a **merge commit** (not squash)" in body
    assert "- abc1234 Fix a thing (#910) (Someone)" in body
    assert "- #910" in body
    assert "Promotion to `main` is never automatic." in body
    # The list is only the truth for one commit, so the body says which.
    assert HEAD_SHA[:12] in body


def test_the_body_sorts_pull_request_references_numerically_and_caps_the_list():
    body = promote.render_body([f"aaa{n} Subject (#{n}) (A)" for n in range(9, 260)])
    assert "... and 51 more commit(s)." in body
    numbers = [int(n) for n in re.findall(r"^- #(\d+)$", body, re.M)]
    assert numbers == sorted(numbers)
    assert numbers[:3] == [9, 10, 11]


def test_the_summary_is_written_even_when_nothing_happened():
    summary = promote.render_summary(decide(commits=0), {"event": "schedule"})
    assert "## Auto promote" in summary
    assert "**Nothing to do.**" in summary
    assert "never automatic" in summary


def test_the_summary_says_would_promote_on_a_dry_run():
    summary = promote.render_summary(decide(), {"event": "workflow_dispatch", "dry_run": "true"})
    assert "**Would promote**" in summary
    assert "| `lint` | pass |" in summary


def test_the_summary_says_stuck_when_the_automation_is_the_thing_that_is_stuck():
    decision = decide(pull_request=pr(mergeStateStatus="DIRTY"))
    assert "**Stuck.**" in promote.render_summary(decision, {"event": "schedule"})


def test_the_refusal_names_the_causes_in_the_order_they_actually_happen():
    # It used to assert one cause -- the `testing` ruleset's extra approval
    # for unattributed changes -- which has never been observed to fire here:
    # promotion pull requests have merged under that ruleset with zero
    # reviews. The cause that does happen is that opening the pull request
    # re-queues the required checks.
    text = promote.render_refusal(991, "Pull request is not mergeable: ```oops")
    assert "#991" in text
    assert "Pull request is not mergeable" in text
    # Nothing quoted out of gh may close the fence early.
    assert "```oops" not in text
    assert text.index("re-queued") < text.index("require_extra_approval")
    assert "not yet been observed to fire" in text


def test_the_refusal_names_the_checks_that_were_not_green_when_it_can():
    text = promote.render_refusal(
        991,
        "not mergeable",
        contexts=(promote.ContextState("test (3.12)", promote.PENDING, "still queued"),),
        merge_state="BLOCKED",
    )
    assert "test (3.12)" in text and "still queued" in text
    assert "`BLOCKED`" in text


def test_the_create_denied_report_gives_the_setting_and_the_click_path():
    # `gh pr create` with GITHUB_TOKEN is refused unless the repository
    # allows it, and it is refused on this repository today.
    text = promote.render_create_denied(
        "pull request create failed: GraphQL: GitHub Actions is not permitted to create "
        "or approve pull requests (createPullRequest)"
    )
    assert "Settings -> Actions -> General -> Workflow permissions" in text
    assert "Allow GitHub Actions to create and approve pull requests" in text
    assert "not permitted to create or approve pull requests" in text


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------


def test_decide_writes_the_verdict_the_body_the_summary_and_the_step_outputs(tmp_path: Path):
    (tmp_path / "rules.json").write_text(json.dumps(rules()), encoding="utf-8")
    (tmp_path / "checks.json").write_text(
        json.dumps({"check_runs": [run(name) for name in REQUIRED]}), encoding="utf-8"
    )
    (tmp_path / "pr.json").write_text("[]", encoding="utf-8")
    (tmp_path / "gate.json").write_text(
        json.dumps({"workflow_runs": [{"head_sha": BASE_SHA, "status": "completed",
                                       "conclusion": "success"}]}),
        encoding="utf-8",
    )
    (tmp_path / "commits.txt").write_text("abc1234 Subject (#42) (A)\n", encoding="utf-8")
    outputs = tmp_path / "outputs.txt"

    code = promote.main(
        [
            "decide",
            "--rules", str(tmp_path / "rules.json"),
            "--checks", str(tmp_path / "checks.json"),
            "--open-pr", str(tmp_path / "pr.json"),
            "--gate", str(tmp_path / "gate.json"),
            "--commits", str(tmp_path / "commits.txt"),
            "--head-sha", HEAD_SHA,
            "--base-sha", BASE_SHA,
            "--out", str(tmp_path / "verdict.json"),
            "--body", str(tmp_path / "body.md"),
            "--summary", str(tmp_path / "summary.md"),
            "--github-output", str(outputs),
        ]
    )
    assert code == 0
    verdict = json.loads((tmp_path / "verdict.json").read_text(encoding="utf-8"))
    assert verdict["state"] == promote.READY
    assert verdict["head"] == "staging" and verdict["base"] == "testing"
    assert verdict["commits"] == 1
    assert verdict["escalate"] is False
    assert "- #42" in (tmp_path / "body.md").read_text(encoding="utf-8")
    assert "## Auto promote" in (tmp_path / "summary.md").read_text(encoding="utf-8")
    written = outputs.read_text(encoding="utf-8")
    assert "state=READY" in written
    assert "escalate=false" in written
    assert "gate=passed" in written


def test_decide_exits_one_only_when_the_automation_itself_is_stuck(tmp_path: Path):
    # A red required check is a red CI run on `staging` already; a daily red
    # run for it trains the maintainer to ignore the mail. A promotion pull
    # request nobody can merge is reported nowhere else.
    (tmp_path / "rules.json").write_text(json.dumps(rules()), encoding="utf-8")
    (tmp_path / "checks.json").write_text(
        json.dumps({"check_runs": [run(name) for name in REQUIRED]}), encoding="utf-8"
    )
    (tmp_path / "commits.txt").write_text("abc1234 Subject (A)\n", encoding="utf-8")

    def go(open_pr: list[dict]) -> int:
        (tmp_path / "pr.json").write_text(json.dumps(open_pr), encoding="utf-8")
        return promote.main(
            [
                "decide",
                "--rules", str(tmp_path / "rules.json"),
                "--checks", str(tmp_path / "checks.json"),
                "--open-pr", str(tmp_path / "pr.json"),
                "--commits", str(tmp_path / "commits.txt"),
                "--head-sha", HEAD_SHA,
                "--base-sha", BASE_SHA,
                "--out", str(tmp_path / "verdict.json"),
            ]
        )

    assert go([]) == 0
    assert go([pr()]) == 0
    assert go([pr(mergeStateStatus="DIRTY")]) == 1
    assert go([pr(isCrossRepository=True)]) == 1


def test_decide_survives_payload_files_that_are_missing_or_not_json(tmp_path: Path):
    # A gh call that failed must not crash the run into a stack trace; it has
    # to come out as a verdict a person can read.
    (tmp_path / "rules.json").write_text("not json", encoding="utf-8")
    (tmp_path / "commits.txt").write_text("abc1234 Subject (A)\n", encoding="utf-8")
    code = promote.main(
        [
            "decide",
            "--rules", str(tmp_path / "rules.json"),
            "--checks", str(tmp_path / "nope.json"),
            "--commits", str(tmp_path / "commits.txt"),
            "--out", str(tmp_path / "verdict.json"),
        ]
    )
    assert code == 0
    assert json.loads((tmp_path / "verdict.json").read_text())["state"] == promote.BLOCKED


def test_refused_always_exits_one(tmp_path: Path):
    (tmp_path / "log.txt").write_text("GraphQL: Pull Request is not mergeable", encoding="utf-8")
    code = promote.main(
        [
            "refused",
            "--pr-number", "991",
            "--message", str(tmp_path / "log.txt"),
            "--summary", str(tmp_path / "refusal.md"),
        ]
    )
    assert code == 1
    assert "not mergeable" in (tmp_path / "refusal.md").read_text(encoding="utf-8")


def test_create_denied_always_exits_one(tmp_path: Path):
    (tmp_path / "log.txt").write_text(
        "GitHub Actions is not permitted to create or approve pull requests", encoding="utf-8"
    )
    code = promote.main(
        [
            "create-denied",
            "--message", str(tmp_path / "log.txt"),
            "--summary", str(tmp_path / "denied.md"),
        ]
    )
    assert code == 1
    assert "Workflow permissions" in (tmp_path / "denied.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# it can never target the release branch
# ---------------------------------------------------------------------------


def test_the_branches_are_constants_and_the_release_branch_is_refused():
    assert (promote.HEAD_BRANCH, promote.BASE_BRANCH) == ("staging", "testing")
    for head, base in (("testing", "main"), ("staging", "main"), ("main", "testing")):
        with pytest.raises(promote.UnsafeTargetError):
            promote.assert_safe_targets(head, base)
    promote.assert_safe_targets()


def test_the_command_line_offers_no_way_to_name_a_branch():
    parser = promote.build_parser()
    flags = {
        option
        for action in parser._subparsers._group_actions[0].choices["decide"]._actions
        for option in action.option_strings
    }
    assert not flags & {"--head", "--base", "--branch", "--target", "--into"}


def executable_yaml() -> str:
    """The workflow with its comment lines removed.

    Comments explain the rules; only the rest is what the runner does, and
    several of these assertions are about what it must never do.
    """
    return "\n".join(
        line
        for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def test_the_workflow_never_names_the_release_branch_outside_a_comment():
    # Comments may explain that promotion to it is manual. Nothing the runner
    # executes may mention it at all.
    assert not re.search(r"\b(main|master)\b", executable_yaml())


def test_the_workflow_hardcodes_the_two_branches_and_takes_no_branch_input(workflow: dict):
    job = workflow["jobs"]["promote"]
    assert job["env"]["HEAD_BRANCH"] == "staging"
    assert job["env"]["BASE_BRANCH"] == "testing"
    inputs = (workflow.get("on") or workflow.get(True))["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"dry_run"}


def test_the_pull_request_is_re_checked_at_the_door_before_being_merged(workflow: dict):
    # Naming the branches constrains only what the job ASKS FOR. The pull
    # request it merges is a number, and a pull request's base branch is
    # mutable by its author -- so the control that matters is on the other
    # side of the door.
    steps = workflow["jobs"]["promote"]["steps"]
    order = [step.get("id") for step in steps]
    assert order.index("check") < order.index("merge")
    verify = next(step for step in steps if step.get("id") == "check")
    assert "auto_promote.py verify" in verify["run"]
    for field in ("baseRefName", "headRefName", "isCrossRepository", "headRefOid", "labels"):
        assert field in verify["run"], field
    merge = next(step for step in steps if step.get("id") == "merge")
    assert merge["if"] == "steps.check.outputs.ok == 'true'"


# ---------------------------------------------------------------------------
# the workflow itself
# ---------------------------------------------------------------------------


@pytest.fixture
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_workflow_parses_and_has_no_dangling_needs(workflow: dict):
    jobs = workflow["jobs"]
    assert jobs, "the workflow defines no job"
    for name, job in jobs.items():
        needs = job.get("needs") or []
        needs = [needs] if isinstance(needs, str) else needs
        for dependency in needs:
            assert dependency in jobs, f"{name} needs {dependency}, which does not exist"


def test_every_step_that_refers_to_another_step_refers_to_one_that_exists(workflow: dict):
    steps = workflow["jobs"]["promote"]["steps"]
    known = {step["id"] for step in steps if step.get("id")}
    for step in steps:
        for referenced in re.findall(r"steps\.([A-Za-z0-9_-]+)\.", json.dumps(step)):
            assert referenced in known, f"{step.get('name')} refers to steps.{referenced}"


def test_it_runs_daily_and_by_hand_and_on_nothing_else(workflow: dict):
    triggers = workflow.get("on") or workflow.get(True)
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert len(triggers["schedule"]) == 1
    # A push trigger here would promote several times a day, and a
    # pull_request trigger would run it from an untrusted branch.
    assert "push" not in triggers and "pull_request" not in triggers


def test_a_hand_started_run_is_a_dry_run_unless_the_box_is_ticked(workflow: dict):
    dry_run = (workflow.get("on") or workflow.get(True))["workflow_dispatch"]["inputs"]["dry_run"]
    assert dry_run["type"] == "boolean"
    assert dry_run["default"] is True


def test_the_scheduled_run_is_not_a_dry_run(workflow: dict):
    # If it were, the workflow would never promote anything, which is the
    # whole feature. The expression is true only for a ticked manual run.
    expression = workflow["jobs"]["promote"]["env"]["DRY_RUN"]
    assert "workflow_dispatch" in expression and "inputs.dry_run" in expression


def test_two_runs_can_never_race(workflow: dict):
    concurrency = workflow["concurrency"]
    assert concurrency["group"]
    # Cancelling between "open the pull request" and "merge it" would leave a
    # pull request open with nobody reporting why.
    assert concurrency["cancel-in-progress"] is False


def test_permissions_are_read_at_the_top_and_only_widened_where_merging_needs_it(workflow: dict):
    assert workflow["permissions"] == {"contents": "read"}
    # contents: merge. pull-requests: open and edit. actions: start the
    # release gate the merge could not trigger. Nothing else.
    assert workflow["jobs"]["promote"]["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
        "actions": "write",
    }


def test_the_merge_waits_on_the_field_that_knows_the_checks_were_re_queued(workflow: dict):
    # Opening the promotion pull request re-queues every required context on
    # a commit that already had them green from the push run, and GitHub
    # reports the pull request as blocked until they finish -- about fourteen
    # minutes, measured. `mergeable` is only the conflict computation and
    # says MERGEABLE throughout, so waiting on it merges nothing and goes red.
    wait = next(
        step for step in workflow["jobs"]["promote"]["steps"] if step.get("id") == "wait"
    )
    assert "mergeStateStatus" in wait["run"]
    assert "seq 1 60" in wait["run"] and "sleep 30" in wait["run"]
    assert workflow["jobs"]["promote"]["timeout-minutes"] >= 40


def test_the_release_gate_is_started_but_the_per_pull_request_checks_are_not(workflow: dict):
    # A GITHUB_TOKEN merge starts no `push` run, so promotion-gate.yml has to
    # be dispatched by hand. ci.yml does not: opening the manual
    # `testing -> main` pull request runs it on that commit and satisfies the
    # required contexts. Dispatching it would also switch on its manual-only
    # 45-minute upgrade-path job, which is not a required context, so a flake
    # in it would be a red run a day about nothing.
    text = executable_yaml()
    assert "gh workflow run promotion-gate.yml" in text
    assert "gh workflow run ci.yml" not in text
    followup = next(
        step for step in workflow["jobs"]["promote"]["steps"] if step.get("id") == "followup"
    )
    assert followup["if"] == "steps.merge.outputs.merged == 'true'"
    # `gh workflow run` exits 0 for a dispatch it merely handed over, so the
    # step checks that a run actually appeared.
    assert "gh run list --workflow promotion-gate.yml" in followup["run"]
    gate_workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "promotion-gate.yml").read_text(encoding="utf-8")
    )
    assert "workflow_dispatch" in (gate_workflow.get("on") or gate_workflow.get(True))


def test_a_target_tip_the_gate_never_ran_on_is_repaired(workflow: dict):
    # A run cancelled or timed out after the merge leaves `testing` carrying
    # no gate verdict, and nothing would ever notice. The next daily run is
    # what heals it.
    step = next(
        step
        for step in workflow["jobs"]["promote"]["steps"]
        if step.get("name", "").startswith("Repair a target tip")
    )
    assert "steps.decide.outputs.gate == 'missing'" in step["if"]
    # But not on a run that is about to promote: the merge moves the tip and
    # the follow-up step starts the gate on the new one.
    assert "steps.decide.outputs.state != 'READY'" in step["if"]
    assert "gh workflow run promotion-gate.yml" in step["run"]


def test_the_outcome_is_reported_even_if_the_run_is_cancelled(workflow: dict):
    step = next(
        step
        for step in workflow["jobs"]["promote"]["steps"]
        if step.get("name", "").startswith("Say what actually happened")
    )
    assert step["if"].startswith("always()")
    assert "gh pr view" in step["run"]


def test_the_job_has_a_timeout_and_does_not_run_in_a_fork(workflow: dict):
    job = workflow["jobs"]["promote"]
    assert isinstance(job["timeout-minutes"], int)
    assert "github.repository ==" in job["if"]


def test_the_merge_is_a_merge_commit_pinned_to_a_commit_and_deletes_nothing():
    text = executable_yaml()
    assert "gh pr merge" in text
    assert "--merge" in text
    assert "--squash" not in text and "--rebase" not in text
    # The pull request head tracks a branch, so without this the commits that
    # get merged need not be the commits whose checks were read.
    assert "--match-head-commit" in text
    # --delete-branch here would delete a long-lived branch.
    assert "--delete-branch" not in text
    # Never bypass the ruleset.
    assert "--admin" not in text


def test_the_promotion_pull_request_is_labelled_like_the_manual_one_and_marked_as_ours():
    text = WORKFLOW.read_text(encoding="utf-8")
    manual = (REPO_ROOT / ".github" / "workflows" / "promote.yml").read_text(encoding="utf-8")
    assert "--label promotion" in text and "--label promotion" in manual
    # And the marker that keeps it from merging a promotion pull request a
    # person opened. The label has to be created, or `gh pr create --label`
    # fails on an unknown one.
    assert f'AUTO_LABEL: {promote.AUTO_LABEL}' in text
    assert 'gh label create "$AUTO_LABEL" --force' in text


def test_the_pull_request_number_comes_from_what_gh_printed():
    # Re-listing after `gh pr create` races GitHub's own index, and an empty
    # answer used to be written to $GITHUB_OUTPUT as "no pull request",
    # skipping the merge and the refusal report while the run stayed green
    # and the summary said "Promoting".
    text = executable_yaml()
    create_step = text.split("Open or update the promotion pull request")[1]
    assert "grep -oE 'https://[^ ]+/pull/[0-9]+'" in create_step
    assert "gh pr list" not in create_step
    assert "could not read its number" in create_step


def test_a_refused_pull_request_creation_names_the_repository_setting():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "not permitted to create or approve pull requests" in text
    assert "auto_promote.py create-denied" in text


def test_every_decision_is_made_by_the_script_not_by_the_yaml(workflow: dict):
    text = WORKFLOW.read_text(encoding="utf-8")
    for command in ("decide", "verify", "refused", "create-denied"):
        assert f"scripts/auto_promote.py {command}" in text
    # The summary is written on every run, including the ones that do nothing.
    summary_step = next(
        step
        for step in workflow["jobs"]["promote"]["steps"]
        if step.get("name") == "Publish the verdict to the job summary"
    )
    assert summary_step["if"] == "always()"
    # And a decision step that crashed must not read as "nothing to do".
    guard = next(
        step
        for step in workflow["jobs"]["promote"]["steps"]
        if step.get("name", "").startswith("Fail if no verdict")
    )
    assert guard["if"] == "steps.decide.outputs.state == ''"
