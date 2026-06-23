# Run CRG PR Reviews Handoff

Date: 2026-06-23

This handoff captures how to run the updated Code Review Graph branch and copied Zazz review skills against additional
Quality Bank worktree branches for PR-review trials.

## Current Source Branch

Use this CRG implementation worktree as the tool source:

```text
/Users/michael/Dev/zazzcode/code-review-graph/mw-improve-metrics-analyis-zazz
```

The updated local skills live here:

```text
.agents/skills/code-review-graph
.agents/skills/pr-review
```

The CRG CLI should be run with `uv run --project` pointed at this implementation worktree so target repos execute this
branch's code instead of any globally installed or target-local CRG version.

## Proven Trial

Validated target:

```text
/Users/michael/Victory/Dev/qb-mono-wt/ss-BE-ServiceChargeAndQBPipelinesWireUp
```

Evidence from the trial:

- Copied updated `code-review-graph` and `pr-review` skills into the target worktree.
- Removed and rebuilt target `.code-review-graph/` derived state.
- Used target standards only, discovered from `docs/standards/index.yaml`.
- Pinned the comparison base to merge-base `49f3baa35eec33d59b628921b8ec7d18143b063d`.
- Ran compact CRG review context with `--max-tokens 4000`.
- CRG reported 45 changed files and risk score `0.60`.
- CRG's emitted `savings_record` reported `estimated: true`, `measurement_scope: change_analysis`,
  `baseline_tokens: 92831`, `returned_tokens: 3893`, `saved_tokens: 88938`, and `saved_percent: 96`.
  These are CRG-estimated change-analysis packet savings, not a measured full-review session token count.
- Focused target HTTP tests passed: `59 passed`.
- Review found a real standards/test-quality issue: the service-charge list endpoint returns `200 {"data":[]}` for an
  empty successful GET, while the target HTTP standard requires `204 No Content`; the new branch test currently locks in
  the drift.

The review artifact was written in the target worktree at:

```text
docs/execution/crg-pr-review-servicecharge-qbpipelines-20260623.md
```

Note: in the Quality Bank worktree set, `docs/execution/` and `.code-review-graph/` may be ignored by the shared bare
repo excludes, so generated artifacts can exist without appearing in ordinary `git status`.

## Guardrails For More Trials

- Do not read existing findings markdown in the target worktree.
- Do not modify target standards files.
- Do not modify existing target findings markdown files.
- It is okay to overwrite target `.code-review-graph/` derived state.
- It is okay to copy this branch's updated `code-review-graph` and `pr-review` skills into target `.agents/skills/` for
  the trial.
- Use only the target worktree's standards index for standards routing, normally `docs/standards/index.yaml`.
- Pin the merge-base once and use the same pinned SHA for CRG, `git diff`, tests, and review evidence.
- Record the CRG `savings_record` as, at most, a one-line CRG estimate. State that it is not measured Codex token usage.
- Do not claim reduced Codex token consumption unless the review run records exact Codex token telemetry.

## Exact Codex Token Measurement

The CRG `savings_record` is not exact Codex usage. It is a CRG-local estimate based on changed-file sizes and compact
payload size. Use it only as a routing/context-size clue.

For a measured Codex review trial, prefer running the review through Codex CLI non-interactive JSON output, because
`codex exec --json` emits `turn.completed` events with exact usage fields such as `input_tokens`,
`cached_input_tokens`, `output_tokens`, and `reasoning_output_tokens`.

Example measured shape:

```bash
codex exec --json "Run the CRG PR-review trial described in .zazz/execution/run-crg -pr-reviews-HANDOFF.md for TARGET_WORKTREE=..." \
  2>/tmp/codex-review-stderr.log \
  | tee /tmp/codex-review-events.jsonl

jq -s '
  [ .[] | select(.type == "turn.completed") | .usage ] as $u
  | {
      turns: ($u | length),
      input_tokens: ($u | map(.input_tokens // 0) | add // 0),
      cached_input_tokens: ($u | map(.cached_input_tokens // 0) | add // 0),
      output_tokens: ($u | map(.output_tokens // 0) | add // 0),
      reasoning_output_tokens: ($u | map(.reasoning_output_tokens // 0) | add // 0)
    }
' /tmp/codex-review-events.jsonl
```

For an interactive Codex CLI/TUI run, `/status` displays session configuration and token usage, and `/usage` shows
account token usage. Those are useful for live inspection, but a saved `codex exec --json` event stream is easier to
audit and compare after the fact.

For Codex app or desktop runs, use exact telemetry only when the surface exposes it for the thread or goal. In this
desktop agent channel, the prior trial did not expose exact completed-session token totals to the agent, so no exact
Codex usage can be recovered retroactively.

If measuring inside an environment that exposes goal/session telemetry to the agent, use this fallback:

1. Before doing review work, explicitly start a measurable Codex goal for the trial. Example user instruction:
   `Create a goal for this review trial and track token usage until the review artifact is written.`
2. Immediately call the available Codex usage/goal telemetry tool, such as `get_goal`, and record the starting token
   counters it reports.
3. Run the CRG-assisted review without mixing unrelated tasks into the same thread or goal.
4. Immediately before the final response, call the same telemetry tool again and record the ending token counters.
5. Compute exact review usage as `ending_tokens - starting_tokens`, preserving input/output/cache fields separately
   when the telemetry exposes them.
6. If the Codex environment does not expose exact token counters, write `Exact Codex token usage: unavailable in this
   environment` and do not make a measured savings claim.

For an A/B comparison, run two fresh measured trials against the same target and pinned merge-base:

- **CRG-assisted review**: use `review-context` / `detect-changes --for-review` before focused source reads.
- **Manual baseline review**: do not use CRG; inspect the diff/files with normal search and file reads.

Only compare token consumption when both runs have exact Codex telemetry from the same measurement source and similar
review scope. The CRG estimate can be reported separately, but it must not be used as the measured result.

## Copy-Paste Command Template

Run from the target Quality Bank worktree. Replace `TARGET_WORKTREE` and `INTEGRATION_BRANCH` first.

```bash
CRG_WORKTREE=/Users/michael/Dev/zazzcode/code-review-graph/mw-improve-metrics-analyis-zazz
TARGET_WORKTREE=/Users/michael/Victory/Dev/qb-mono-wt/<target-branch-worktree>
INTEGRATION_BRANCH=dev

cd "$TARGET_WORKTREE"

mkdir -p .agents/skills docs/execution
rm -rf .agents/skills/code-review-graph .agents/skills/pr-review .code-review-graph
cp -R "$CRG_WORKTREE/.agents/skills/code-review-graph" .agents/skills/code-review-graph
cp -R "$CRG_WORKTREE/.agents/skills/pr-review" .agents/skills/pr-review

python /Users/michael/.codex/skills/.system/skill-creator/scripts/quick_validate.py .agents/skills/code-review-graph
python /Users/michael/.codex/skills/.system/skill-creator/scripts/quick_validate.py .agents/skills/pr-review

MERGE_BASE=$(git merge-base "$INTEGRATION_BRANCH" HEAD)
echo "merge base: $MERGE_BASE"

git diff --name-only "$MERGE_BASE"...HEAD > /tmp/crg-target-changed-files.txt
wc -l /tmp/crg-target-changed-files.txt

uv run --project "$CRG_WORKTREE" code-review-graph review-context \
  --repo . \
  --base "$MERGE_BASE" \
  --max-tokens 4000 \
  > /tmp/crg-target-review-context.json
```

Optional scoped packets:

```bash
uv run --project "$CRG_WORKTREE" code-review-graph detect-changes \
  --repo . \
  --base "$MERGE_BASE" \
  --for-review \
  --max-tokens 2000 \
  --scope "backend/src/http_api/**"

uv run --project "$CRG_WORKTREE" code-review-graph detect-changes \
  --repo . \
  --base "$MERGE_BASE" \
  --for-review \
  --max-tokens 2000 \
  --scope "backend/src/data/sprocs/**"

uv run --project "$CRG_WORKTREE" code-review-graph detect-changes \
  --repo . \
  --base "$MERGE_BASE" \
  --for-review \
  --max-tokens 2000 \
  --scope "backend/tests/**"
```

## Execution Prompt For Future Review Trials

Use this prompt in a fresh Codex thread after replacing the target path:

```text
You are running a real-worktree PR-review trial using the updated Code Review Graph branch at:

  /Users/michael/Dev/zazzcode/code-review-graph/mw-improve-metrics-analyis-zazz

Target Quality Bank worktree:

  /Users/michael/Victory/Dev/qb-mono-wt/<target-branch-worktree>

Use the updated CRG branch code and copied skills to review the target branch against its integration branch, normally
`dev`.

Required workflow:

1. Read the target worktree's `AGENTS.md` and standards index, normally `docs/standards/index.yaml`.
2. Do not read existing findings markdown in the target worktree.
3. Do not modify target standards files or existing findings markdown files.
4. Copy this CRG branch's `.agents/skills/code-review-graph` and `.agents/skills/pr-review` into the target
   `.agents/skills/`, replacing only those two skill directories.
5. Validate both copied skills with
   `python /Users/michael/.codex/skills/.system/skill-creator/scripts/quick_validate.py`.
6. Remove and rebuild target `.code-review-graph/` derived state as needed.
7. Pin `MERGE_BASE=$(git merge-base dev HEAD)` and use that exact SHA for all CRG, git diff, and verification evidence.
8. Run:

   uv run --project /Users/michael/Dev/zazzcode/code-review-graph/mw-improve-metrics-analyis-zazz code-review-graph review-context --repo . --base "$MERGE_BASE" --max-tokens 4000

9. Record the CRG `savings_record` as an estimate only. State that it is not measured Codex token usage.
10. If exact Codex token telemetry is available, record start/end token counters for this review trial. If it is not
    available, state that exact Codex token usage was unavailable and do not claim measured token savings.
11. Use only target standards matched from `docs/standards/index.yaml` for review rules.
12. Review along these axes: Standards/Code Quality, Functionality/Spec, Security/Data/Ops, and Test Quality.
13. Read only the smallest source/test/standard slices needed to prove or disprove findings.
14. Run focused target tests where practical.
15. Write a new review artifact under target `docs/execution/` with:
    - target path, branch, integration branch, and pinned merge-base
    - CRG command and compact savings metadata
    - exact Codex token usage if available, otherwise an explicit unavailable note
    - copied-skill validation evidence
    - standards index used and matched standards
    - tests or checks run
    - findings grouped by axis with file/line evidence
    - explicit note that existing findings markdown was not read

Goal:

Show whether this CRG branch helps find higher-quality PR-review issues. Separately measure exact Codex token usage
when Codex telemetry is available. Do not use CRG's estimated `savings_record` as proof of actual Codex token savings.
```

## Suggested Artifact Naming

Use a unique, target-specific file name under target `docs/execution/`, for example:

```text
docs/execution/crg-pr-review-<short-branch-topic>-20260623.md
```

If `docs/execution/` is ignored, still write the file there for local trial evidence and report that it is ignored in
the final summary.
