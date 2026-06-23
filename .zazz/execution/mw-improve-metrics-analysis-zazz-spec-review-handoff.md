# Spec Review Handoff — Improve CRG Metrics, Review Quality, And Zazz Skill Integration

## Purpose

This handoff is for a reviewer or follow-on agent who will review and improve the specification at:

`.zazz/specifications/mw-improve-metrics-analysis-zazz.md`

The current spec started as a deliverable for token-efficient Code Review Graph output, then expanded to capture the larger Zazz review-quality objective: CRG should help `pr-review` agents behave less like generic LLM best-practice reviewers and more like principal engineers with codebase memory, standards awareness, graph evidence, and disciplined engineering taste.

The immediate purpose is to make the spec stronger and clearer before handing it off for overnight implementation. The reviewer should not implement product code. They should improve the specification, resolve contradictions, sharpen acceptance criteria or scope language where needed, and leave the implementation agent with an unambiguous, runnable spec.

## Current Worktree

- Worktree: `/Users/michael/Dev/zazzcode/code-review-graph/mw-improve-metrics-analyis-zazz`
- Branch: `mw-improve-metrics-analyis-zazz`
- Primary spec: `.zazz/specifications/mw-improve-metrics-analysis-zazz.md`
- Graph visualization guidance: `.zazz/execution/crg-graph-visualization-guidance.md`
- Strict critique / improvement candidates: `.zazz/execution/crg-approach-critical-review.md`

## What The Spec Currently Covers

The implementation slice is still scoped to P1, P2, P3, P4, P5, and P7 from the fork-improvement analysis:

- repo-relative deterministic review paths
- compact budgeted `--for-review` output
- one-shot `review-context`
- section-scoped review packets
- low-signal test-gap suppression
- scope-honest token savings reporting

The spec intentionally does not implement P6 sibling-template divergence in this deliverable.

Spec review has now resolved the unattended-implementation blockers:

- the four open questions are resolved as explicit defaults in §10 of the primary spec
- the shared run log has been created and bootstrapped
- the appendix includes a fresh overnight implementation prompt
- future review-quality features are marked as design guidance or follow-on deliverables, not current acceptance criteria

The spec now also documents a review-quality direction for future CRG product work and companion skills:

- a dedicated `code-review-graph` skill that `pr-review` can invoke
- standards-index context packets
- standards-vs-codebase drift detection
- review-topology planning to control sub-agent count and scope
- read-first plans combining graph, source lines, tests, and standards
- whole-worktree consolidation and generated-code/source-of-truth signals
- language-agnostic duplication and LOC reduction guidance
- agent slop and low-value test detection
- the "engineering taste" layer: standards provide rules, CRG provides the evidence map, and the skill applies disciplined judgment

## Review Goals

When reviewing or improving the spec, pressure-test these questions:

1. Is the current implementation scope still clear enough to build without accidentally absorbing the future skill/product work?
2. Are the review-quality objectives concrete enough for a later deliverable specification?
3. Does the spec clearly distinguish graph evidence from review findings?
4. Does it preserve Zazz review discipline: findings target changed code, while unchanged code supplies context and evidence?
5. Are standards-index context packets described strongly enough to prevent agents from globbing all standards docs?
6. Does the "engineering taste" language avoid vague preference while still empowering useful simplification and consolidation findings?
7. Does the generated-code / cross-report guidance explain when whole-worktree evidence is useful but nonblocking?
8. Are agent slop and low-value tests tied to indexed code-hygiene and testing standards, not free-floating LLM opinion?
9. Should any review-quality guidance move from "future design guidance" into the current implementation ACs, or remain separate?
10. Is there enough detail to write a follow-on spec for the new `code-review-graph` skill?

## Important Background Materials

### Core Specification And Execution Docs

- `.zazz/specifications/mw-improve-metrics-analysis-zazz.md`
- `.zazz/execution/crg-approach-critical-review.md`
- `.zazz/execution/crg-graph-visualization-guidance.md`
- `.zazz/execution/mw-improve-metrics-analysis-zazz-run-log.md`

### Original Fork Improvement Analysis

- `/Users/michael/Dev/zazzcode/code-review-graph-fork-improvements.md`

This is the source of the P1-P7 improvement list. The current spec includes P1, P2, P3, P4, P5, and P7, and omits P6.

### CRG Product Documentation

- `docs/FEATURES.md`
- `docs/USAGE.md`
- `docs/COMMANDS.md`
- `docs/architecture.md`
- `docs/schema.md`
- Public features page: `http://code-review-graph.com/features`

Key product positioning from these docs:

- local-first graph intelligence
- SQLite-backed repository graph
- MCP and CLI review context
- changed-file, dependency, flow, community, risk, and test-gap analysis
- visualization support
- context reduction claims that should be reported honestly

### Zazz Methodology And Standards

- `AGENTS.md`
- `.zazz/agent-execution-discipline.md`
- `.zazz/code-review-graph.md`
- `.zazz/standards/index.yaml`
- `.zazz/standards/spec-hygiene.md`
- `.zazz/standards/docs-hygiene.md`
- `.zazz/standards/pr-process.md`
- `.zazz/standards/code-structure.md`
- `.zazz/standards/python-testing.md`
- `.zazz/standards/database-testing.md`

Important standards lesson: standards selection should come from the index, not from globbing all standards files.

### `pr-review` Skill Materials

- `.agents/skills/pr-review/SKILL.md`
- `.agents/skills/pr-review/README.md`
- `.agents/skills/pr-review/code-review-graph.md`
- `.agents/skills/pr-review/shared-rules.md`
- `.agents/skills/pr-review/standards-axis.md`
- `.agents/skills/pr-review/spec-axis.md`

Important review-shape context:

- `pr-review` has Standards and Spec axes.
- CRG is currently optional advisory context.
- Findings still require source, diff, standards, or specification evidence.
- The Standards axis already calls out code quality, test value, agentic slop, and redundant computation.
- The Spec axis focuses on whether the implementation satisfies the deliverable/specification contract.

### PR #214 Comparison Evidence

The user supplied a concrete comparison of review with and without CRG:

- `/Users/michael/Victory/Dev/qb-mono-wt/ss-BE-ReturnAddressWireUp/docs/execution/pr214-review-findings-no-code-review-graph.md`
- `/Users/michael/Victory/Dev/qb-mono-wt/ss-BE-ReturnAddressWireUp/docs/execution/pr214-review-findings-w-code-review-graph.md`
- `/Users/michael/Victory/Dev/qb-mono-wt/ss-BE-ReturnAddressWireUp/docs/execution/pr214-token-comparison.md`
- `/Users/michael/Victory/Dev/qb-mono-wt/ss-BE-ReturnAddressWireUp/docs/execution/pr214-standards-improvements.md`
- `/Users/michael/Victory/Dev/qb-mono-wt/ss-BE-ReturnAddressWireUp/docs/execution/pr214-standards-help-no-code-review-graph.md`

Key finding from the comparison:

- The with-graph review was cheaper and used fewer subagents, but it missed or down-sized a concrete blocker the no-graph review caught.
- The no-graph review identified a missing `@ReturnCode int = 0` initializer in a changed read stored procedure.
- The with-graph review treated the broader family pattern as nonblocking and concluded "Approvable."
- This proves token efficiency is not finding-quality proof.

### Optional Large-Repo Validation Worktree

- `/Users/michael/Victory/Dev/qb-mono-wt/mw-sum-stmt-register-rpt-stack`

If used for CRG validation, local `.code-review-graph/` cache data may be removed and rebuilt because it is derived/ignored graph data. Do not commit or modify external repo source files.

## Design Judgments Already Captured

- CRG's graph should be treated as a graph of the indexed worktree, not just PR files.
- The PR diff supplies seed nodes; the graph can provide whole-worktree context.
- Findings should usually target changed code; unchanged siblings provide evidence unless the spec owns broader cleanup.
- Generated-code analysis should prefer the source schema/template/generator over generated output when available.
- Consolidation is valuable only when it improves clarity, structure, reviewability, and maintenance.
- Reducing LOC is good only when it does not hide domain differences or create opaque machinery.
- Agent slop and low-value test calls should come from indexed standards and source evidence, not generic LLM taste.
- "Engineering taste" should be explicit, disciplined, and subordinate to local standards and source evidence.

## Suggested Next Improvements To The Spec

The next reviewer should consider whether to add one of these as a separate follow-on specification:

1. `code-review-graph` companion skill design for Zazz `pr-review`
2. standards-index context packet product feature
3. read-first packet product feature
4. review-topology planner product feature
5. standards-vs-codebase drift detector
6. finding-quality evaluation harness using PR #214-style expected findings
7. whole-worktree consolidation candidate detector
8. agent slop / low-value test evidence packet

The likely best next deliverable is the companion skill spec, because it can encode review behavior immediately while the CRG product features mature.

## Suggested Reviewer Prompt

```text
You are reviewing and improving a Zazz deliverable specification before it is handed off for overnight implementation.

Primary spec:
.zazz/specifications/mw-improve-metrics-analysis-zazz.md

Handoff:
.zazz/execution/mw-improve-metrics-analysis-zazz-spec-review-handoff.md

Focus on whether the spec cleanly separates the current token-efficiency implementation slice from the future review-quality and code-review-graph skill objectives. Preserve the P1/P2/P3/P4/P5/P7 implementation scope unless you find a contradiction. Strengthen the guidance for Zazz pr-review integration, standards-index usage, engineering taste, whole-worktree graph evidence, generated-code/source-of-truth analysis, consolidation opportunities, and agent slop / low-value test detection.

Read this handoff first, then read the primary spec, critique doc, visualization guidance, pr-review skill files, standards index, code-structure standard, testing standards, and the PR #214 comparison docs linked in the handoff.

Your job is specification improvement only. Do not implement product code. Edit the spec and supporting execution docs if needed so an overnight implementation agent can execute without re-litigating scope.

Spec review checklist:
- Confirm the current implementation slice is still P1/P2/P3/P4/P5/P7 and still excludes P6 sibling-template divergence.
- Confirm future review-quality features are clearly marked as design guidance or future deliverables unless intentionally moved into current ACs.
- Confirm the spec explains that CRG indexes the worktree graph while PR files are seed nodes.
- Confirm standards-index usage is mandatory for standards selection and prevents globbing all standards docs.
- Confirm engineering taste is grounded in local standards and source evidence, not generic LLM preference.
- Confirm agent slop and low-value test guidance points to indexed code-hygiene and testing standards.
- Confirm consolidation guidance is language-agnostic across Python, JavaScript, TypeScript, Go, T-SQL, PL/pgSQL, PL/SQL, and other CRG-supported languages.
- Confirm the overnight implementor has clear required reading, ACs, tests, halt conditions, and final verification commands.
- Run `git diff --check` after edits.

Return:
1. summary of spec improvements made,
2. any remaining open questions or implementation risks,
3. exact files changed,
4. whether the spec is ready for overnight implementation.

Return either:
1. a patch improving the spec, or
2. findings explaining what should change before implementation begins.
```

## Handoff Status

- Handoff created: 2026-06-23
- Spec review completed: 2026-06-23
- Primary spec is ready for unattended overnight implementation after `git diff --check` passes.
- The implementation contract is owned by `.zazz/specifications/mw-improve-metrics-analysis-zazz.md`; this handoff is a navigation aid and historical context.
