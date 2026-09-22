#!/usr/bin/env python3
"""Decide whether `staging` may be promoted to `testing`, and render the PR.

``.github/workflows/auto-promote.yml`` fetches payloads with ``git`` and
``gh``, hands them to ``decide``, and then does exactly what the verdict says.
Every judgement lives here so it can be driven from a test with no network:
the workflow contains no rule of its own.

The rule, in the maintainer's words: *once a day, when staging is green and
has something testing does not*. Three verdicts come out of it:

``READY``
    Open or update the promotion pull request and merge it with a merge
    commit.
``NOT_READY``
    A state the repository reaches by itself and gets out of by itself:
    nothing to promote, CI still running, the head moved mid-run, the release
    gate still running. Tomorrow's run will look again. Not a failure.
``BLOCKED``
    A state that needs a person. Some of those are the repository's business
    and are reported elsewhere (a red required check is a red CI run); those
    stay green here. Some are the automation being stuck on its own pull
    request, and those set ``Decision.escalate`` so the workflow goes red.

Design notes worth keeping:

* **The required contexts are read from the ruleset at run time**, through
  ``GET /repos/{owner}/{repo}/rules/branches/testing``, not hardcoded. That
  endpoint needs only read access to the repository, unlike the
  ``/rulesets/{id}`` one, so the workflow can stay on a read-scoped token for
  the decision. Hardcoding the eight strings would mean a renamed CI job
  silently drops out of the gate.
* **A required context that was skipped or neutral is not green here**, even
  though GitHub's own required-status-check evaluation accepts both. This
  gate is deliberately stricter than the ruleset: it may refuse a promotion
  GitHub would have allowed, and it can never allow one GitHub would refuse.
  A job that skipped tested nothing, and an unattended daily merge is the
  wrong place to be generous. ``scripts/promotion_gate.py`` takes the same
  line about skipped checks for the same reason.
* **A pull request is identified by what it is, not by its branch name.**
  ``gh pr list --head staging`` matches pull requests opened from *any*
  repository's branch called ``staging``, forks included -- ``gh pr list
  --help`` says outright that ``owner:branch`` syntax is not supported. On a
  public repository with thousands of forks, "the open staging -> testing
  pull request" is an attacker-supplied value unless it is checked.
  :func:`select_promotion_pr` therefore requires the pull request to be
  same-repository, to have exactly the expected head and base, and to carry
  the marker label this workflow puts on the ones it opened. Anything else
  is refused by name and blocks the run rather than being merged.
* **The commit whose checks were read is the commit that gets merged.** The
  pull request head tracks a *branch*, so it moves under the run. The chosen
  pull request's ``headRefOid`` is compared against the SHA the checks were
  classified on, ``verify`` re-reads the pull request immediately before the
  merge, and the merge itself passes ``--match-head-commit``. Three
  independent chances to notice; the last one is enforced by GitHub.
* **Nothing is asserted about why GitHub refused a merge.** The refusal
  renderer quotes what GitHub said and lists the causes in the order they
  actually occur here. The first one, by a distance, is that opening the
  pull request re-queued the required checks on the head SHA and they had
  not finished.

This script can never target ``main``. The branches are the two constants
below, no argument, environment variable or payload field can change them,
and ``assert_safe_targets`` refuses anything else.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The only promotion this script performs. Promotion to `main` is a manual
# step and stays one: see CONTRIBUTING.md "Branching and promotion".
HEAD_BRANCH = "staging"
BASE_BRANCH = "testing"

# Branches no automated merge may ever write to.
NEVER_AUTOMATED = ("main", "master")

# The label the manual `Promote` workflow uses, kept so the two pull requests
# read the same in the list, and the marker that says this one is the daily
# job's to merge. A promotion pull request a person opened does not carry the
# marker, so this workflow will not touch it.
PROMOTION_LABEL = "promotion"
AUTO_LABEL = "auto-promotion"

# Verdicts.
READY = "READY"
NOT_READY = "NOT_READY"
BLOCKED = "BLOCKED"

# Per-context outcomes.
GREEN = "green"
RED = "red"
PENDING = "pending"
ABSENT = "absent"
NOT_RUN = "not-run"

_GREEN_CONCLUSIONS = frozenset({"success"})
# "skipped" and "neutral" are treated as passing by GitHub's required status
# checks. They are not treated as passing here; see the module docstring.
_NOT_RUN_CONCLUSIONS = frozenset({"skipped", "neutral"})

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PR_REF = re.compile(r"#(\d+)")
_MAX_LINE = 240
_MAX_COMMITS_SHOWN = 200

# gh and the REST API spell mergeability differently; both are accepted.
_CONFLICTING = {"false", "conflicting", "dirty"}
_UNKNOWN = {"none", "null", "unknown", ""}

# mergeStateStatus values that mean "GitHub will take the merge now".
# CLEAN is the ordinary one. HAS_HOOKS is CLEAN with pre-receive hooks.
# UNSTABLE means a check that is *not* required is failing or pending; the
# required ones are classified here from the check runs themselves, so this
# adds nothing and must not hold a promotion.
MERGE_NOW = frozenset({"CLEAN", "HAS_HOOKS", "UNSTABLE"})
# States that pass on their own once something finishes.
MERGE_WAIT = frozenset({"", "UNKNOWN", "BEHIND"})


class UnsafeTargetError(Exception):
    """A branch this script is not allowed to touch was asked for."""


class ForeignPullRequestError(Exception):
    """The pull request about to be merged is not the one that was decided."""


def assert_safe_targets(head: str = HEAD_BRANCH, base: str = BASE_BRANCH) -> None:
    """Refuse any promotion other than `staging` -> `testing`.

    The constants above are the only values the command line can produce, so
    this cannot fire in normal use. It exists so that a future edit which
    threads a branch through from somewhere else fails loudly and in a test,
    rather than quietly merging something into `main` at 06:00.
    """
    for name, role in ((head, "head"), (base, "base")):
        if name in NEVER_AUTOMATED:
            raise UnsafeTargetError(
                f"{name!r} was given as the {role} branch. Promotion to "
                f"{name!r} is never automatic; the maintainer opens and merges "
                "that pull request by hand."
            )
    if (head, base) != (HEAD_BRANCH, BASE_BRANCH):
        raise UnsafeTargetError(
            f"this workflow promotes {HEAD_BRANCH!r} to {BASE_BRANCH!r} only, "
            f"not {head!r} to {base!r}."
        )


def clean(text: object) -> str:
    """Strip control characters and clamp one line for display."""
    flat = _CONTROL_CHARS.sub("", str(text)).strip()
    if len(flat) > _MAX_LINE:
        flat = flat[: _MAX_LINE - 3] + "..."
    return flat


# ---------------------------------------------------------------------------
# reading the payloads
# ---------------------------------------------------------------------------


def required_contexts(rules: Any) -> tuple[str, ...]:
    """Pull the required status check contexts out of the branch rules.

    ``rules`` is the body of ``GET /repos/{owner}/{repo}/rules/branches/{branch}``:
    a flat list of the rules that apply, whichever ruleset they came from.
    An empty result means the caller could not establish what has to be green,
    which is a refusal, not a pass.
    """
    found: list[str] = []
    for rule in rules if isinstance(rules, list) else []:
        if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
            continue
        parameters = rule.get("parameters") or {}
        for entry in parameters.get("required_status_checks") or []:
            context = (entry or {}).get("context") if isinstance(entry, dict) else None
            if isinstance(context, str) and context.strip():
                found.append(context.strip())
    seen: set[str] = set()
    ordered: list[str] = []
    for context in found:
        if context not in seen:
            seen.add(context)
            ordered.append(context)
    return tuple(ordered)


def normalise_reports(payload: Any) -> list[dict[str, Any]]:
    """Flatten check runs and commit statuses into one shape.

    A required context can be satisfied by either, and a repository that
    later adds a third-party status check should not need this script
    changed. Each entry comes back as ``{name, status, conclusion, when}``.
    """
    reports: list[dict[str, Any]] = []
    runs: Any = []
    statuses: Any = []
    if isinstance(payload, dict):
        runs = payload.get("check_runs") or []
        statuses = payload.get("statuses") or []
    elif isinstance(payload, list):
        runs = payload
    for run in runs if isinstance(runs, list) else []:
        if not isinstance(run, dict):
            continue
        name = run.get("name")
        if not isinstance(name, str):
            continue
        reports.append(
            {
                "name": name,
                "status": str(run.get("status") or ""),
                "conclusion": run.get("conclusion"),
                "when": str(run.get("completed_at") or run.get("started_at") or ""),
            }
        )
    for status in statuses if isinstance(statuses, list) else []:
        if not isinstance(status, dict):
            continue
        context = status.get("context")
        if not isinstance(context, str):
            continue
        state = str(status.get("state") or "").lower()
        reports.append(
            {
                "name": context,
                "status": "completed" if state != "pending" else "in_progress",
                "conclusion": {"success": "success", "pending": None}.get(state, "failure"),
                "when": str(status.get("updated_at") or status.get("created_at") or ""),
            }
        )
    return reports


def latest_by_name(reports: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Keep only the most recent report per context name.

    A re-run leaves the old check run on the commit, still carrying its old
    conclusion. So does opening a pull request on a commit that already had a
    push run: the head SHA then carries two `lint`, two `test (3.12)` and so
    on. Taking the newest by timestamp is what makes both "I re-ran the flaky
    job and it is green now" and "the pull request re-queued everything"
    resolve to the state that is actually current.
    """
    newest: dict[str, dict[str, Any]] = {}
    for report in reports:
        name = report["name"]
        current = newest.get(name)
        if current is None or str(report.get("when") or "") >= str(current.get("when") or ""):
            newest[name] = report
    return newest


@dataclass(frozen=True)
class ContextState:
    """One required context and what the head commit says about it."""

    context: str
    state: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"context": self.context, "state": self.state, "detail": self.detail}


def classify(required: tuple[str, ...], reports: list[dict[str, Any]]) -> tuple[ContextState, ...]:
    """Say, for each required context, whether the head commit clears it."""
    newest = latest_by_name(reports)
    out: list[ContextState] = []
    for context in required:
        report = newest.get(context)
        if report is None:
            out.append(
                ContextState(
                    context,
                    ABSENT,
                    "no check run or status with this name reported on the commit",
                )
            )
            continue
        status = str(report.get("status") or "").lower()
        conclusion = str(report.get("conclusion") or "").lower()
        if status != "completed":
            out.append(ContextState(context, PENDING, f"still {status or 'queued'}"))
        elif conclusion in _GREEN_CONCLUSIONS:
            out.append(ContextState(context, GREEN, "success"))
        elif conclusion in _NOT_RUN_CONCLUSIONS:
            out.append(
                ContextState(
                    context,
                    NOT_RUN,
                    f"{conclusion}: the job did not run, so it verified nothing",
                )
            )
        else:
            out.append(ContextState(context, RED, conclusion or "completed with no conclusion"))
    return tuple(out)


def _mergeability(pull_request: dict[str, Any]) -> tuple[str, str]:
    """Normalise gh's and the REST API's two spellings of mergeability."""
    raw = pull_request.get("mergeable")
    mergeable = str(raw).strip().lower() if raw is not None else "none"
    state = str(pull_request.get("mergeStateStatus") or pull_request.get("mergeable_state") or "")
    return mergeable, state.strip().upper()


# ---------------------------------------------------------------------------
# which pull request is ours
# ---------------------------------------------------------------------------


def pr_labels(pull_request: dict[str, Any]) -> set[str]:
    """The label names on a pull request, from gh's or the REST API's shape."""
    names: set[str] = set()
    for label in pull_request.get("labels") or []:
        if isinstance(label, dict) and isinstance(label.get("name"), str):
            names.add(label["name"])
        elif isinstance(label, str):
            names.add(label)
    return names


def pr_identity_problems(pull_request: dict[str, Any]) -> tuple[str, ...]:
    """Everything about this pull request that makes it not ours to merge.

    This is the whole defence, so it is written to fail closed: a field that
    is missing from the payload counts against the pull request rather than
    being waved through, except where gh genuinely omits it.

    The checks, and why each one is here:

    ``isCrossRepository``
        ``gh pr list --head staging`` matches a branch called ``staging`` in
        *any* fork. An outsider cannot open a same-repository pull request --
        that needs write access -- so refusing cross-repository ones removes
        the whole fork attack, and with it the ability of an outsider to
        later repoint the base branch at ``main``.
    ``baseRefName`` / ``headRefName``
        A pull request's base is mutable by whoever can write to its head
        branch, and changing it does not re-run ``pull_request`` workflows.
        Read at selection time *and* again immediately before the merge.
    ``AUTO_LABEL``
        Tells "the pull request this workflow opened yesterday" apart from
        "the promotion pull request a person opened deliberately and did not
        merge". Without it the daily job silently merges the second.
    ``state``
        gh only lists open ones, but ``verify`` re-reads a specific number.
    """
    problems: list[str] = []
    if pull_request.get("isCrossRepository") or pull_request.get("is_cross_repository"):
        owner = pull_request.get("headRepositoryOwner") or {}
        who = owner.get("login") if isinstance(owner, dict) else None
        problems.append(
            f"it comes from another repository ({clean(who) or 'a fork'}), and this "
            "workflow only ever merges a branch of this repository"
        )
    base = pull_request.get("baseRefName") or pull_request.get("base_ref")
    if base is not None and str(base) != BASE_BRANCH:
        problems.append(f"its base branch is `{clean(base)}`, not `{BASE_BRANCH}`")
    head = pull_request.get("headRefName") or pull_request.get("head_ref")
    if head is not None and str(head) != HEAD_BRANCH:
        problems.append(f"its head branch is `{clean(head)}`, not `{HEAD_BRANCH}`")
    state = pull_request.get("state")
    if state is not None and str(state).upper() not in {"OPEN"}:
        problems.append(f"it is {clean(state).lower()}, not open")
    if AUTO_LABEL not in pr_labels(pull_request):
        problems.append(
            f"it does not carry the `{AUTO_LABEL}` label, so it was opened by a "
            "person or by the manual `Promote` workflow and is theirs to merge"
        )
    return tuple(problems)


def assert_pr_is_ours(pull_request: dict[str, Any], expected_head_sha: str = "") -> None:
    """Raise unless this pull request is the one the decision was made about.

    Called from ``verify``, straight before the merge, on a pull request read
    fresh from the API. Everything :func:`pr_identity_problems` checks can be
    changed by its author *after* the daily job looked, and changing it does
    not re-run any workflow, so looking once is looking too early.
    """
    number = pull_request.get("number")
    problems = list(pr_identity_problems(pull_request))
    actual = str(pull_request.get("headRefOid") or pull_request.get("head_sha") or "")
    if expected_head_sha and actual and actual != expected_head_sha:
        problems.append(
            f"its head is now `{clean(actual)[:12]}`, not the `{clean(expected_head_sha)[:12]}` "
            "the required checks were read on"
        )
    if problems:
        raise ForeignPullRequestError(
            f"refusing to merge #{clean(number)}: " + "; ".join(problems) + "."
        )


@dataclass(frozen=True)
class PrSelection:
    """Which open pull request is ours, and which ones were turned away."""

    chosen: dict[str, Any] | None = None
    rejected: tuple[tuple[Any, tuple[str, ...]], ...] = ()

    @property
    def number(self) -> int | None:
        if not self.chosen:
            return None
        raw = self.chosen.get("number")
        return int(raw) if isinstance(raw, (int, str)) else None


def parse_commits(text: str) -> list[str]:
    """One promoted commit per line, as `git log --format` wrote them."""
    return [clean(line) for line in (text or "").splitlines() if line.strip()]


def select_promotion_pr(payload: Any) -> PrSelection:
    """Pick the open promotion pull request this workflow is allowed to merge.

    ``gh pr list --json`` yields an array. Every entry is a candidate only;
    :func:`pr_identity_problems` decides. Entries that fail are kept, with
    their reasons, so the run can say *which* pull request it refused and why
    instead of silently doing nothing -- an outsider parking a fork branch
    called ``staging`` on this base must be loud, not quiet.
    """
    entries = payload if isinstance(payload, list) else (payload or {}).get("items") or []
    chosen: dict[str, Any] | None = None
    rejected: list[tuple[Any, tuple[str, ...]]] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or not entry.get("number"):
            continue
        problems = pr_identity_problems(entry)
        if problems:
            rejected.append((entry.get("number"), problems))
        elif chosen is None:
            chosen = entry
        else:
            rejected.append((entry.get("number"), ("a newer promotion pull request is open",)))
    return PrSelection(chosen=chosen, rejected=tuple(rejected))


# ---------------------------------------------------------------------------
# the release gate on the base branch
# ---------------------------------------------------------------------------

GATE_PASSED = "passed"
GATE_FAILED = "failed"
GATE_RUNNING = "running"
GATE_MISSING = "missing"


@dataclass(frozen=True)
class GateVerdict:
    """What `promotion-gate.yml` last said about the tip of `testing`."""

    state: str = GATE_MISSING
    url: str = ""
    conclusion: str = ""


def gate_verdict(payload: Any, base_sha: str) -> GateVerdict:
    """Read the promotion gate's verdict for the current tip of `testing`.

    ``payload`` is ``GET /repos/{owner}/{repo}/actions/workflows/
    promotion-gate.yml/runs?branch=testing``. Only runs on the exact tip
    count: a verdict about some earlier commit says nothing about this one.

    This is what stops the automation from pushing more work onto a branch it
    has already made unreleasable. Before this workflow existed, every
    landing on `testing` was a maintainer's decision, and that is what gave
    the gate's "blocks" verdict its teeth; reading it here is what replaces
    that.
    """
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else payload
    best: GateVerdict | None = None
    for run in runs if isinstance(runs, list) else []:
        if not isinstance(run, dict) or str(run.get("head_sha") or "") != base_sha:
            continue
        url = str(run.get("html_url") or "")
        status = str(run.get("status") or "").lower()
        conclusion = str(run.get("conclusion") or "").lower()
        if status != "completed":
            return GateVerdict(GATE_RUNNING, url, conclusion)
        if conclusion == "success":
            return GateVerdict(GATE_PASSED, url, conclusion)
        if conclusion in {"cancelled", "skipped"}:
            best = best or GateVerdict(GATE_MISSING, url, conclusion)
            continue
        best = best or GateVerdict(GATE_FAILED, url, conclusion)
    return best or GateVerdict(GATE_MISSING)


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """The verdict, everything it was based on, and why."""

    state: str
    reason: str
    contexts: tuple[ContextState, ...] = ()
    commits: int = 0
    pr_number: int | None = None
    notes: tuple[str, ...] = ()
    # True when the thing that is stuck is this automation, not the
    # repository. Those, and only those, make the workflow run red: a red
    # required check is already a red CI run on `staging` and a daily red run
    # for it would train the maintainer to ignore the mail, but "the pull
    # request I opened has been sitting unmerged" is reported nowhere else
    # and would otherwise stall the promotion silently and forever.
    escalate: bool = False
    head_sha: str = field(default="", compare=False)

    @property
    def ready(self) -> bool:
        return self.state == READY

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "head": HEAD_BRANCH,
            "base": BASE_BRANCH,
            "head_sha": self.head_sha,
            "commits": self.commits,
            "pr_number": self.pr_number,
            "escalate": self.escalate,
            "contexts": [context.as_dict() for context in self.contexts],
            "notes": list(self.notes),
        }


def decide(
    *,
    commits: int,
    rules: Any,
    reports: list[dict[str, Any]],
    pull_request: dict[str, Any] | PrSelection | None,
    head_sha: str = "",
    base_sha: str = "",
    gate: GateVerdict | None = None,
) -> Decision:
    """Decide whether `staging` may be promoted to `testing` right now.

    The order of the guards is the order in which an answer is most useful to
    read. A failed required check is reported as a failed required check, not
    as "the pull request is blocked", even though the second is what GitHub
    would say about the same situation.
    """
    assert_safe_targets()

    # A bare dict is vetted exactly like an entry from `gh pr list`. There is
    # deliberately no way to hand `decide` a pull request it will trust
    # without checking: "the caller already validated it" is how the fork
    # branch called `staging` got merged in the first place.
    selection = (
        pull_request
        if isinstance(pull_request, PrSelection)
        else select_promotion_pr([pull_request])
        if pull_request
        else PrSelection()
    )

    if head_sha and base_sha and head_sha == base_sha:
        return Decision(
            NOT_READY,
            f"`{HEAD_BRANCH}` and `{BASE_BRANCH}` are the same commit.",
            head_sha=head_sha,
        )
    if commits <= 0:
        return Decision(
            NOT_READY,
            f"`{HEAD_BRANCH}` has no commits that `{BASE_BRANCH}` lacks.",
            head_sha=head_sha,
        )

    # An open pull request that is not ours is checked before anything else.
    # It is the one state where doing nothing quietly is the dangerous
    # answer: on a public repository anybody may open `theirfork:staging ->
    # testing`, and a run that shrugged at it would stall the promotion
    # indefinitely while looking perfectly healthy.
    if selection.chosen is None and selection.rejected:
        listed = "; ".join(
            f"#{clean(number)} ({', '.join(clean(problem) for problem in problems)})"
            for number, problems in selection.rejected
        )
        return Decision(
            BLOCKED,
            f"an open pull request into `{BASE_BRANCH}` is not one this workflow may "
            f"merge: {listed}.",
            commits=commits,
            escalate=True,
            head_sha=head_sha,
            notes=(
                "Nothing was merged. If that pull request is a legitimate promotion "
                "somebody opened by hand, merge it by hand with a merge commit. If it "
                "came from a fork, close it: this workflow will not promote it and will "
                "keep saying so until it is gone.",
            ),
        )

    required = required_contexts(rules)
    if not required:
        return Decision(
            BLOCKED,
            f"no required status check could be read for `{BASE_BRANCH}`, so there is "
            "nothing to verify staging against. Promoting blind is not an option.",
            commits=commits,
            head_sha=head_sha,
            notes=(
                "The contexts are read from GET /repos/OWNER/REPO/rules/branches/"
                f"{BASE_BRANCH}. An empty answer means the ruleset changed, or the "
                "token could not read it.",
            ),
        )

    contexts = classify(required, reports)
    red = [item for item in contexts if item.state == RED]
    not_run = [item for item in contexts if item.state == NOT_RUN]
    pending = [item for item in contexts if item.state == PENDING]
    absent = [item for item in contexts if item.state == ABSENT]

    if red:
        return Decision(
            BLOCKED,
            "required check(s) did not pass on the `{}` tip: {}.".format(
                HEAD_BRANCH, ", ".join(f"`{item.context}` ({item.detail})" for item in red)
            ),
            contexts=contexts,
            commits=commits,
            head_sha=head_sha,
        )
    if not_run:
        return Decision(
            BLOCKED,
            "required check(s) were skipped rather than run on the `{}` tip: {}.".format(
                HEAD_BRANCH, ", ".join(f"`{item.context}`" for item in not_run)
            ),
            contexts=contexts,
            commits=commits,
            head_sha=head_sha,
            notes=(
                "GitHub counts a skipped required check as a pass. This workflow "
                "does not: a job that did not run verified nothing. Promote by hand "
                "if the skip was deliberate.",
            ),
        )
    if absent and pending:
        return Decision(
            NOT_READY,
            "CI has not finished on the `{}` tip: {} still running, {} not reported yet.".format(
                HEAD_BRANCH,
                ", ".join(f"`{item.context}`" for item in pending),
                ", ".join(f"`{item.context}`" for item in absent),
            ),
            contexts=contexts,
            commits=commits,
            head_sha=head_sha,
        )
    if absent:
        return Decision(
            BLOCKED,
            "required context(s) never reported on the `{}` tip and nothing is still "
            "running: {}.".format(HEAD_BRANCH, ", ".join(f"`{item.context}`" for item in absent)),
            contexts=contexts,
            commits=commits,
            head_sha=head_sha,
            notes=(
                "A required context with no check run is usually a CI job that was "
                "renamed without the ruleset being updated, or a push-triggered run "
                "that never started. GitHub would refuse the merge too.",
            ),
        )
    if pending:
        return Decision(
            NOT_READY,
            "required check(s) are still running on the `{}` tip: {}.".format(
                HEAD_BRANCH, ", ".join(f"`{item.context}`" for item in pending)
            ),
            contexts=contexts,
            commits=commits,
            head_sha=head_sha,
        )

    # The release gate's verdict on the branch being promoted *into*. Pushing
    # a second day of work onto a `testing` that already cannot be released
    # makes the problem harder to unpick, not easier.
    if gate is not None and gate.state == GATE_FAILED:
        return Decision(
            BLOCKED,
            f"the promotion gate on the current `{BASE_BRANCH}` tip did not pass "
            f"({clean(gate.conclusion) or 'failure'}), so `{BASE_BRANCH}` cannot be "
            "released as it stands and will not be given more to carry.",
            contexts=contexts,
            commits=commits,
            head_sha=head_sha,
            notes=(
                "Fix what the gate reported, or re-run `promotion-gate.yml` on "
                f"`{BASE_BRANCH}` if it was a flake. Tomorrow's run promotes again as "
                "soon as the gate is green on that tip."
                + (f" Gate run: {clean(gate.url)}" if gate.url else ""),
            ),
        )
    if gate is not None and gate.state == GATE_RUNNING:
        return Decision(
            NOT_READY,
            f"the promotion gate is still running on the current `{BASE_BRANCH}` tip; "
            "its verdict comes first.",
            contexts=contexts,
            commits=commits,
            head_sha=head_sha,
        )

    number = selection.number
    chosen = selection.chosen
    if chosen is not None:
        if chosen.get("isDraft") or chosen.get("draft"):
            return Decision(
                BLOCKED,
                f"the open promotion pull request #{number} is a draft.",
                contexts=contexts,
                commits=commits,
                pr_number=number,
                escalate=True,
                head_sha=head_sha,
            )
        pr_head = str(chosen.get("headRefOid") or chosen.get("head_sha") or "")
        if head_sha and pr_head and pr_head != head_sha:
            return Decision(
                NOT_READY,
                f"the open promotion pull request #{number} is at "
                f"`{clean(pr_head)[:12]}` but the required checks were read on "
                f"`{clean(head_sha)[:12]}`; `{HEAD_BRANCH}` moved.",
                contexts=contexts,
                commits=commits,
                pr_number=number,
                head_sha=head_sha,
                notes=(
                    "Nothing is wrong. The next run reads the checks on the newer "
                    "commit and promotes that.",
                ),
            )
        mergeable, merge_state = _mergeability(chosen)
        if mergeable in _CONFLICTING or merge_state == "DIRTY":
            return Decision(
                BLOCKED,
                f"the open promotion pull request #{number} has conflicts with "
                f"`{BASE_BRANCH}` and cannot be merged automatically.",
                contexts=contexts,
                commits=commits,
                pr_number=number,
                escalate=True,
                head_sha=head_sha,
                notes=(
                    f"Something landed on `{BASE_BRANCH}` that `{HEAD_BRANCH}` does not "
                    f"have. Merge `{BASE_BRANCH}` into `{HEAD_BRANCH}` through a normal "
                    "pull request, and this clears by itself.",
                ),
            )
        if merge_state == "BLOCKED":
            # Every required context is green on this SHA -- that was settled
            # above -- so GitHub is holding the pull request for something
            # else. The commonest cause by far is that opening the pull
            # request re-queued those same contexts and the new runs have not
            # finished; the merge step waits that out. Reaching here means it
            # did not clear, which is the automation being stuck.
            return Decision(
                BLOCKED,
                f"GitHub still reports the open promotion pull request #{number} as "
                "blocked, although every required context is green on the commit.",
                contexts=contexts,
                commits=commits,
                pr_number=number,
                escalate=True,
                head_sha=head_sha,
                notes=(
                    "Open the pull request and read the merge box; it names the rule. "
                    "Then merge it by hand with a **merge commit**.",
                ),
            )
        if mergeable in _UNKNOWN or merge_state in MERGE_WAIT:
            return Decision(
                NOT_READY,
                f"GitHub has not finished working out whether #{number} can be merged.",
                contexts=contexts,
                commits=commits,
                pr_number=number,
                head_sha=head_sha,
            )

    return Decision(
        READY,
        "`{}` is {} commit(s) ahead of `{}` and all {} required check(s) are green.".format(
            HEAD_BRANCH, commits, BASE_BRANCH, len(contexts)
        ),
        contexts=contexts,
        commits=commits,
        pr_number=number,
        head_sha=head_sha,
    )


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def render_body(commits: list[str], run_url: str = "", head_sha: str = "") -> str:
    """Render the promotion pull request body.

    Deliberately the same shape as the manual `Promote` workflow's body, so
    the two are indistinguishable in the pull request list and the maintainer
    reads one format, not two. The head SHA is stated, because the list below
    is only the truth for that commit.
    """
    shown = commits[:_MAX_COMMITS_SHOWN]
    lines = [
        f"## Promote `{HEAD_BRANCH}` -> `{BASE_BRANCH}`",
        "",
        "Merge with a **merge commit** (not squash) so every author stays on their commits.",
        "",
        "### Included",
        "",
    ]
    lines.extend(f"- {line}" for line in shown)
    if len(commits) > len(shown):
        lines.append(f"- ... and {len(commits) - len(shown)} more commit(s).")
    lines.extend(["", "### Pull requests referenced", ""])
    numbers = sorted({int(match) for line in commits for match in _PR_REF.findall(line)})
    lines.extend(f"- #{number}" for number in numbers)
    lines.extend(
        [
            "",
            "---",
            "",
            "Opened automatically by `.github/workflows/auto-promote.yml`, which promotes "
            f"`{HEAD_BRANCH}` to `{BASE_BRANCH}` once a day when the required checks are "
            "green. Promotion to `main` is never automatic.",
        ]
    )
    if head_sha:
        lines.append("")
        lines.append(
            f"The list above is `{HEAD_BRANCH}` at `{clean(head_sha)[:12]}`, which is the "
            "commit whose checks were read and the only commit this pull request will be "
            "merged at."
        )
    if run_url:
        lines.append("")
        lines.append(f"[Run log]({clean(run_url)})")
    return "\n".join(lines) + "\n"


def _context_table(contexts: tuple[ContextState, ...]) -> list[str]:
    if not contexts:
        return []
    mark = {
        GREEN: "pass",
        RED: "FAIL",
        PENDING: "running",
        ABSENT: "DID NOT REPORT",
        NOT_RUN: "DID NOT RUN",
    }
    lines = ["| Required check | Result | Detail |", "| --- | --- | --- |"]
    lines.extend(
        f"| `{clean(item.context)}` | {mark.get(item.state, item.state)} | {clean(item.detail)} |"
        for item in contexts
    )
    return lines


def render_summary(decision: Decision, context: dict[str, str]) -> str:
    """Render the job summary. Written every run, including the quiet ones.

    A run that decided to do nothing has to say so in the same place as a run
    that promoted, or "the workflow did nothing" and "the workflow did not
    run" look identical from the Actions tab.
    """
    headline = {
        READY: f"**Promoting.** `{HEAD_BRANCH}` -> `{BASE_BRANCH}`.",
        NOT_READY: "**Nothing to do.**",
        BLOCKED: "**Held back.** A person needs to look at this.",
    }[decision.state]
    if decision.escalate:
        headline = "**Stuck.** This workflow cannot finish what it started."
    if context.get("dry_run") == "true" and decision.ready:
        headline = f"**Would promote** `{HEAD_BRANCH}` -> `{BASE_BRANCH}`, but this is a dry run."

    lines = ["## Auto promote", "", headline, "", decision.reason, ""]
    if decision.notes:
        lines.extend(f"> {clean(note)}" for note in decision.notes)
        lines.append("")
    lines.append(
        f"`{HEAD_BRANCH}` at `{clean(context.get('head_sha', ''))[:12]}`, "
        f"`{BASE_BRANCH}` at `{clean(context.get('base_sha', ''))[:12]}`, "
        f"{decision.commits} commit(s) in the range, "
        f"trigger {clean(context.get('event', '')) or 'unknown'}."
    )
    if context.get("gate"):
        lines.append("")
        lines.append(f"Promotion gate on `{BASE_BRANCH}`: {clean(context['gate'])}.")
    lines.append("")
    lines.extend(_context_table(decision.contexts))
    if decision.contexts:
        lines.append("")
    if decision.pr_number:
        lines.append(f"Promotion pull request: #{decision.pr_number}.")
        lines.append("")
    lines.append(
        f"Promotion of `{BASE_BRANCH}` to `main` is never automatic and this workflow "
        "cannot perform it."
    )
    return "\n".join(lines) + "\n"


def _quote(message: str) -> list[str]:
    lines = ["```"]
    for raw in str(message).splitlines():
        body = clean(raw.replace("```", "'''"))
        if body:
            lines.append(body)
    lines.append("```")
    return lines


def render_refusal(
    number: int,
    message: str,
    run_url: str = "",
    contexts: tuple[ContextState, ...] = (),
    merge_state: str = "",
) -> str:
    """Render the one outcome that is a red run: GitHub refused the merge.

    This used to assert a cause. It no longer does. The causes are listed in
    the order they actually happen here, and the one that was believed to be
    first -- the `testing` ruleset's extra approval for unattributed changes
    -- is last, because it has never once fired on this repository: the
    promotion pull requests that merged with zero reviews under that ruleset
    are the evidence. What is quoted below is what GitHub said.
    """
    lines = [
        "## Auto promote",
        "",
        f"**GitHub refused to merge the promotion pull request #{number}.** It is still "
        "open; nothing was merged and nothing was lost.",
        "",
        "What GitHub said:",
        "",
    ]
    lines.extend(_quote(message))
    if merge_state:
        lines.extend(["", f"GitHub's merge state for the pull request: `{clean(merge_state)}`."])
    stale = [item for item in contexts if item.state != GREEN]
    lines.extend(["", "In the order these actually happen:", ""])
    if stale:
        lines.append(
            "1. **A required check is not green on the pull request head right now** -- "
            + ", ".join(f"`{clean(item.context)}` ({clean(item.detail)})" for item in stale)
            + ". Opening a promotion pull request re-queues every required context on a "
            "commit that already had them from the push run, and until those finish "
            "GitHub reports the pull request as blocked. Nothing is wrong; it needs "
            "longer than this run waited."
        )
    else:
        lines.append(
            "1. **A required check was re-queued by opening the pull request.** Every "
            "required context was green when the decision was made, and opening a "
            "promotion pull request starts a fresh `pull_request` run of all of them "
            "on the same commit. Until those finish, GitHub reports the pull request as "
            "blocked. This is the usual cause, and it clears on its own."
        )
    lines.extend(
        [
            f"2. **`{HEAD_BRANCH}` moved during the run.** The merge is pinned with "
            "`--match-head-commit`, so a push mid-run is refused rather than merged "
            "unchecked. Tomorrow's run promotes the newer commit.",
            f"3. **A conflict with `{BASE_BRANCH}`.** Merge `{BASE_BRANCH}` into "
            f"`{HEAD_BRANCH}` through a normal pull request and this clears.",
            "4. **A rule that needs an approving review.** The `testing` ruleset requires "
            "zero approvals, but `require_extra_approval_for_unattributed_changes` is on, "
            "and GITHUB_TOKEN may not approve pull requests. This has not yet been "
            "observed to fire here -- promotion pull requests have merged with zero "
            "reviews under this ruleset -- so check the three above first.",
            "",
            f"Whatever it was: #{number} is open and can be merged by hand with a "
            "**merge commit**.",
            "",
            "This run is red on purpose. Every other outcome of this workflow is a state "
            "the repository reports elsewhere; this one is not reported anywhere but "
            "here.",
        ]
    )
    if run_url:
        lines.extend(["", f"[Run log]({clean(run_url)})"])
    return "\n".join(lines) + "\n"


def render_create_denied(message: str, run_url: str = "") -> str:
    """Render the failure that is a repository setting, not a bug.

    ``gh pr create`` with GITHUB_TOKEN is refused unless *Allow GitHub
    Actions to create and approve pull requests* is on. It is off on this
    repository, and the same refusal has already failed the manual `Promote`
    workflow. A daily job that dies on an unexplained GraphQL error is a job
    the maintainer turns off, so the error names the setting and the clicks.
    """
    lines = [
        "## Auto promote",
        "",
        "**This workflow may not open a pull request.** Nothing was opened, updated or "
        "merged.",
        "",
        "What GitHub said:",
        "",
    ]
    lines.extend(_quote(message))
    lines.extend(
        [
            "",
            "This is a repository setting, not a failure of the promotion. Turn it on at:",
            "",
            "**Settings -> Actions -> General -> Workflow permissions -> tick "
            "_Allow GitHub Actions to create and approve pull requests_ -> Save.**",
            "",
            "That one tick gates *creating* pull requests as well as approving them. "
            "Leave *Workflow permissions* itself on **Read repository contents and "
            "packages permissions**: this workflow asks for the writes it needs in its "
            "own `permissions:` block and does not rely on the default.",
            "",
            "Nothing else in the repository needs changing. Until it is ticked, promote "
            "by hand with the `Promote` workflow, which fails the same way for the same "
            "reason.",
        ]
    )
    if run_url:
        lines.extend(["", f"[Run log]({clean(run_url)})"])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------


def _load_json(path: str, default: Any) -> Any:
    if not path:
        return default
    file = Path(path)
    if not file.is_file():
        return default
    try:
        return json.loads(file.read_text(encoding="utf-8") or "null")
    except (OSError, json.JSONDecodeError):
        return default


def _write(path: str, text: str) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _append_outputs(path: str, values: dict[str, str]) -> None:
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def cmd_decide(args: argparse.Namespace) -> int:
    commit_text = Path(args.commits).read_text(encoding="utf-8") if args.commits else ""
    commit_lines = parse_commits(commit_text)
    selection = select_promotion_pr(_load_json(args.open_pr, []))
    gate = gate_verdict(_load_json(args.gate, []), args.base_sha) if args.gate else None
    decision = decide(
        commits=len(commit_lines),
        rules=_load_json(args.rules, []),
        reports=normalise_reports(_load_json(args.checks, [])),
        pull_request=selection,
        head_sha=args.head_sha,
        base_sha=args.base_sha,
        gate=gate,
    )
    context = {
        "head_sha": args.head_sha,
        "base_sha": args.base_sha,
        "event": args.event,
        "dry_run": "true" if args.dry_run else "false",
        "gate": gate.state if gate else "",
    }
    _write(args.out, json.dumps(decision.as_dict(), indent=2) + "\n")
    _write(args.body, render_body(commit_lines, args.run_url, args.head_sha))
    summary = render_summary(decision, context)
    _write(args.summary, summary)
    _append_outputs(
        args.github_output,
        {
            "state": decision.state,
            "pr_number": str(decision.pr_number or ""),
            "commits": str(decision.commits),
            "escalate": "true" if decision.escalate else "false",
            "gate": gate.state if gate else "",
        },
    )
    print(summary)
    level = "notice" if decision.state != BLOCKED else "warning"
    print(f"::{level} title=auto promote::{decision.state}: {clean(decision.reason)}")
    # A verdict the repository owns stays green; the automation being stuck on
    # its own pull request does not, or a stalled promotion is a green run
    # every morning forever.
    return 1 if decision.escalate else 0


# `verify` exit codes. Two kinds of "do not merge this" with two different
# consequences, because conflating them is how a red run a day gets earned.
VERIFY_OK = 0
VERIFY_FOREIGN = 1  # not our pull request any more: red run, somebody moved it.
VERIFY_MOVED = 3  # `staging` moved under the run: nothing wrong, stop quietly.


def cmd_verify(args: argparse.Namespace) -> int:
    """Re-read one pull request and refuse it unless it is still ours.

    The last thing that happens before ``gh pr merge``. Selection happened
    minutes earlier, and everything it checked -- the base branch above all --
    can be changed by the pull request's author in between without re-running
    a single workflow, so looking once is looking too early.

    A head that moved is told apart from a pull request that was tampered
    with. The first is somebody landing a pull request on `staging` while
    this ran; tomorrow promotes the newer commit and today's run has no
    business going red over it.
    """
    payload = _load_json(args.pull_request, {})
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, dict) or not payload.get("number"):
        print("::error title=auto promote::no pull request to verify.")
        return VERIFY_FOREIGN
    if str(payload.get("number")) != str(args.expect_number):
        print(
            f"::error title=auto promote::expected #{args.expect_number}, "
            f"read #{payload.get('number')}."
        )
        return VERIFY_FOREIGN
    try:
        assert_pr_is_ours(payload)
    except ForeignPullRequestError as error:
        print(f"::error title=auto promote::{clean(error)}")
        _write(
            args.summary,
            "## Auto promote\n\n**Refused to merge.** The pull request is not the one "
            f"this run decided on.\n\n{clean(error)}\n\nNothing was merged. This is a "
            "red run: a promotion pull request that changes shape between being chosen "
            "and being merged is reported nowhere else.\n",
        )
        return VERIFY_FOREIGN
    actual = str(payload.get("headRefOid") or payload.get("head_sha") or "")
    if args.expect_head_sha and actual and actual != args.expect_head_sha:
        moved = (
            f"`{HEAD_BRANCH}` moved during the run: #{payload.get('number')} is now at "
            f"`{clean(actual)[:12]}`, and the required checks were read on "
            f"`{clean(args.expect_head_sha)[:12]}`."
        )
        print(f"::notice title=auto promote::{moved}")
        _write(
            args.summary,
            f"## Auto promote\n\n**Stopped before merging.** {moved}\n\nNothing was "
            "merged, and nothing is wrong: the next run reads the checks on the newer "
            "commit and promotes that. The pull request stays open.\n",
        )
        return VERIFY_MOVED
    print(f"#{payload.get('number')} is the promotion pull request this run decided on.")
    return VERIFY_OK


def cmd_refused(args: argparse.Namespace) -> int:
    message = Path(args.message).read_text(encoding="utf-8") if args.message else ""
    verdict = _load_json(args.verdict, {})
    contexts: tuple[ContextState, ...] = ()
    if isinstance(verdict, dict):
        contexts = tuple(
            ContextState(
                str(item.get("context", "")),
                str(item.get("state", "")),
                str(item.get("detail", "")),
            )
            for item in verdict.get("contexts") or []
            if isinstance(item, dict)
        )
    text = render_refusal(args.pr_number, message, args.run_url, contexts, args.merge_state)
    _write(args.summary, text)
    print(text)
    return 1


def cmd_create_denied(args: argparse.Namespace) -> int:
    message = Path(args.message).read_text(encoding="utf-8") if args.message else ""
    text = render_create_denied(message, args.run_url)
    _write(args.summary, text)
    print(text)
    print(
        "::error title=auto promote::GitHub Actions may not create pull requests here. "
        "Settings -> Actions -> General -> Workflow permissions -> "
        "Allow GitHub Actions to create and approve pull requests."
    )
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="daily staging -> testing promotion")
    sub = parser.add_subparsers(dest="command", required=True)

    # Note what is NOT here: no --head, no --base, no --branch. The branches
    # are constants, so no caller can point this at `main`.
    decider = sub.add_parser("decide", help="decide whether to promote, and render the PR")
    decider.add_argument("--rules", default="", help="GET /repos/O/R/rules/branches/testing")
    decider.add_argument("--checks", default="", help="GET /repos/O/R/commits/SHA/check-runs")
    decider.add_argument("--open-pr", default="", help="gh pr list --json output")
    decider.add_argument("--gate", default="", help="promotion-gate.yml runs on the base branch")
    decider.add_argument("--commits", default="", help="git log output, one commit per line")
    decider.add_argument("--head-sha", default="")
    decider.add_argument("--base-sha", default="")
    decider.add_argument("--event", default="")
    decider.add_argument("--run-url", default="")
    decider.add_argument("--dry-run", action="store_true")
    decider.add_argument("--out", default="", help="verdict JSON to write")
    decider.add_argument("--body", default="", help="pull request body to write")
    decider.add_argument("--summary", default="", help="job summary markdown to write")
    decider.add_argument("--github-output", default="", help="$GITHUB_OUTPUT to append to")

    verifier = sub.add_parser("verify", help="refuse a pull request that is no longer ours")
    verifier.add_argument("--pull-request", required=True, help="gh pr view --json output")
    verifier.add_argument("--expect-number", required=True)
    verifier.add_argument("--expect-head-sha", default="")
    verifier.add_argument("--summary", default="")

    refused = sub.add_parser("refused", help="report a merge GitHub refused; always exits 1")
    refused.add_argument("--pr-number", type=int, required=True)
    refused.add_argument("--message", default="", help="file holding what gh printed")
    refused.add_argument("--merge-state", default="", help="mergeStateStatus at the attempt")
    refused.add_argument("--verdict", default="", help="the verdict JSON decide wrote")
    refused.add_argument("--run-url", default="")
    refused.add_argument("--summary", default="")

    denied = sub.add_parser("create-denied", help="report the create-pull-request setting")
    denied.add_argument("--message", default="")
    denied.add_argument("--run-url", default="")
    denied.add_argument("--summary", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "decide":
        return cmd_decide(args)
    if args.command == "verify":
        return cmd_verify(args)
    if args.command == "create-denied":
        return cmd_create_denied(args)
    return cmd_refused(args)


if __name__ == "__main__":
    sys.exit(main())
