# Rerun ServiceCharge/QB Pipelines Review Handoff

Date: 2026-06-23

The prior review of `ss-BE-ServiceChargeAndQBPipelinesWireUp` was useful but under-scoped: it used CRG to find a real
standards/test-quality issue, but it did not fully exercise the updated `pr-review` skill shape with independent
Standards, Functionality, Security/Data/Ops, and Test Quality axes. Rerun this in a fresh session so the reviewer is
not carrying implementation context, prior conclusions, or narrowed assumptions from the CRG branch work.

## Copy-Paste Prompt

```text
You are running a fresh, independent CRG-assisted PR review trial.

CRG implementation worktree to use:

  /Users/michael/Dev/zazzcode/code-review-graph/mw-improve-metrics-analyis-zazz

Target Quality Bank worktree:

  /Users/michael/Victory/Dev/qb-mono-wt/ss-BE-ServiceChargeAndQBPipelinesWireUp

Important: do not read existing findings markdown in the target worktree before completing your independent review
artifact. Do not modify target standards files or existing findings markdown files. You may overwrite target
`.code-review-graph/` derived state. You may copy only the updated `code-review-graph` and `pr-review` skills from the
CRG implementation worktree into the target worktree.

Required workflow:

1. Read the CRG implementation worktree's `.agents/skills/code-review-graph/SKILL.md` and
   `.agents/skills/pr-review/SKILL.md`, including all referenced pr-review axis files.
2. Read target `AGENTS.md`.
3. Read target `docs/standards/index.yaml` and load only target standards matched by changed paths and review
   activities. Use target standards only.
4. Copy updated skills into target:

   CRG_WORKTREE=/Users/michael/Dev/zazzcode/code-review-graph/mw-improve-metrics-analyis-zazz
   TARGET_WORKTREE=/Users/michael/Victory/Dev/qb-mono-wt/ss-BE-ServiceChargeAndQBPipelinesWireUp
   cd "$TARGET_WORKTREE"
   mkdir -p .agents/skills docs/execution
   rm -rf .agents/skills/code-review-graph .agents/skills/pr-review .code-review-graph
   cp -R "$CRG_WORKTREE/.agents/skills/code-review-graph" .agents/skills/code-review-graph
   cp -R "$CRG_WORKTREE/.agents/skills/pr-review" .agents/skills/pr-review
   python /Users/michael/.codex/skills/.system/skill-creator/scripts/quick_validate.py .agents/skills/code-review-graph
   python /Users/michael/.codex/skills/.system/skill-creator/scripts/quick_validate.py .agents/skills/pr-review

5. Pin the comparison base:

   MERGE_BASE=$(git merge-base dev HEAD)
   git diff --name-only "$MERGE_BASE"...HEAD
   git log "$MERGE_BASE"..HEAD --oneline

6. Run this branch's CRG, pinned to that merge-base:

   uv run --project "$CRG_WORKTREE" code-review-graph review-context --repo . --base "$MERGE_BASE" --max-tokens 4000 > /tmp/crg-ss-servicecharge-review-context.json

7. Treat the CRG `savings_record` as an estimate only. State clearly that it is not measured Codex usage.
8. Run a full four-axis review:
   - Standards / Code Quality
   - Functionality / Spec
   - Security / Data / Ops
   - Test Quality

   If sub-agents are available, dispatch each axis as an independent reviewer using the copied `pr-review` axis briefs.
   If sub-agents are not available, run the axes sequentially but keep notes separated by axis. Do not collapse every
   issue into a generic standards review.

9. Read only the source/test/SQL/standard slices needed to prove or disprove specific findings, but be thorough within
   each axis. Pay special attention to:
   - HTTP empty-result and error-envelope contracts
   - OpenAPI documented responses
   - auth/permission constants and seed grants
   - stored procedure return-code and `@ErrorMessage` handling
   - service/data-layer exception translation
   - route and service tests that assert drift, mock implementation details, or miss integration behavior
   - database migration and tSQLt coverage for new sprocs

10. Run focused target verification where practical, at minimum:

    cd /Users/michael/Victory/Dev/qb-mono-wt/ss-BE-ServiceChargeAndQBPipelinesWireUp/backend
    uv run pytest tests/http_api/v1/service_charge tests/http_api/v1/quality_bank_pipeline_spec -q

    Add service-layer or data-layer tests if the reviewed findings depend on them and the commands are practical in
    the local environment.

11. Write the new independent findings artifact under the target worktree at exactly this path:

    docs/execution/pr219-findings-crg-clean-run.md

    Include:
    - target worktree, branch, integration branch, and pinned merge-base
    - exact CRG command
    - copied-skill validation evidence
    - target standards index used and matched target standards
    - CRG `savings_record` as an estimate only
    - exact Codex token usage if the run was performed with measured telemetry; otherwise state unavailable
    - verification commands and results
    - PASS/FAIL or findings for each axis
    - file/line evidence for every finding
    - explicit note that existing findings markdown was not read before completing the independent review

12. Final response should summarize:
    - artifact path
    - number of actionable findings by axis
    - tests/checks run
    - whether exact Codex token usage was measured
```

## Measured Codex Run Option

For exact token usage, run the fresh review through Codex CLI JSON output instead of an ordinary interactive thread:

```bash
codex exec --json "<paste the prompt above>" \
  2>/tmp/codex-ss-servicecharge-review-stderr.log \
  | tee /tmp/codex-ss-servicecharge-review-events.jsonl

jq -s '
  [ .[] | select(.type == "turn.completed") | .usage ] as $u
  | {
      turns: ($u | length),
      input_tokens: ($u | map(.input_tokens // 0) | add // 0),
      cached_input_tokens: ($u | map(.cached_input_tokens // 0) | add // 0),
      output_tokens: ($u | map(.output_tokens // 0) | add // 0),
      reasoning_output_tokens: ($u | map(.reasoning_output_tokens // 0) | add // 0)
    }
' /tmp/codex-ss-servicecharge-review-events.jsonl
```

Use the JSONL usage totals as measured Codex usage. Do not treat CRG's `savings_record` as measured usage.
