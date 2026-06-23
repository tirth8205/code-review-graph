# Critical Review Of The CRG Improvement Approach

This note pressure-tests `.zazz/specifications/mw-improve-metrics-analysis-zazz.md` against the PR #214 with/without-CRG review artifacts supplied by the Owner. It is intentionally strict: the goal is to identify what the current specification improves, what it does not improve, and which additional CRG capabilities would most improve token efficiency or review findings.

## Source Artifacts Reviewed

- Owner-supplied no-graph review: `pr214-review-findings-no-code-review-graph.md`
- Owner-supplied with-graph review: `pr214-review-findings-w-code-review-graph.md`
- Token comparison: `pr214-token-comparison.md`
- Standards companion docs: `pr214-standards-improvements.md` and `pr214-standards-help-no-code-review-graph.md`
- Current spec: `.zazz/specifications/mw-improve-metrics-analysis-zazz.md`

The external PR #214 files are local validation evidence, not durable spec dependencies. Do not hard-code those absolute paths into product docs.

## Executive Judgment

The current spec is directionally right for token efficiency, but it is not sufficient for review quality.

It targets the obvious output waste: absolute paths, oversized JSON, repeated full-context summaries, unscoped payloads, noisy test gaps, and ambiguous savings claims. Those changes are worthwhile and should remain in scope.

However, the PR #214 comparison shows a more serious risk: the with-graph review was cheaper but missed or under-sized the blocking defect that the no-graph review caught. The no-graph review identified the concrete, one-line `@ReturnCode` initializer gap in `qb2_GetAllReturnAddresses`; the with-graph review treated the broader read-sproc return-code pattern as a nonblocking sibling-wide standards issue and concluded "Approvable." That is a finding-quality failure, not a token-efficiency failure.

The uncomfortable conclusion: making the current CRG payload smaller may make a weak graph-assisted review cheaper without making it safer. The spec needs to preserve token improvements while explicitly recognizing that finding quality requires additional graph signals and review workflows.

## What The Current Spec Gets Right

### 1. It Attacks Output Bloat At The Right Layer

The storage analysis is correct: CRG's graph is already SQLite-backed and queryable. The token waste comes from serialized output and repeated ingestion. P1/P3/P4/P7 are correctly aimed at the emitted review payload, not the database engine.

### 2. It Supports Zazz's Section-Agent Model

Zazz review expects Standards and Spec axes, scoped standards, and deliverable boundaries. A compact `--for-review --scope ...` packet is a good fit for section-scoped agents because it lets each agent pull only relevant graph context.

### 3. It Makes Measurement Less Misleading

Renaming savings as "change-analysis token savings" and adding a `savings_record` is necessary. PR #214's token comparison shows how easily graph economics can be misread when session transcript duplication, cache reads, sub-agent count, and graph build/probe cost are mixed together.

### 4. It Avoids P6 Scope Creep

Keeping sibling-template divergence out of this deliverable is reasonable for reviewability. But this should not be mistaken for "not important." It is probably the most important review-quality improvement.

## Critical Problems With The Current Approach

### Problem 1 — It Optimizes Cost More Than Correctness

The current spec's ACs primarily prove payload shape, token budget, scoping, and metadata. They do not prove that graph-assisted review catches more or better findings. A smaller payload can still omit the critical signal.

**Consequence.** The implementation could pass every AC and still reproduce the PR #214 failure mode: a cheap, scoped, polished review packet that misses the actionable defect.

**Recommendation.** Add a future evaluation track that scores finding quality, not only payload size. Use locked A/B review fixtures where the expected findings are known, including PR #214-style "concrete deviation from sibling invariant" cases.

### Problem 2 — The Clean A/B Claim Is Not Yet Supported

The PR #214 token comparison says the with-graph session was cheaper, but also says the comparison was not clean:

- sub-agent count differed: 2 with graph vs 6 without graph
- both sessions included meta-work
- snapshot timing differed
- the with-graph arm paid first-run graph overhead

**Consequence.** Any product claim like "graph made this review 29% cheaper" would be overconfident. The real finding is weaker: graph did not obviously make the review more expensive under the measured conditions.

**Recommendation.** Add a clean comparison harness as a separate improvement: same PR, same model, same sub-agent topology, prebuilt graph, fixed snapshot point, de-duplicated transcript accounting, and finding-quality scoring.

### Problem 3 — Scope Filtering Can Hide Cross-Scope Bugs

P4 section-scoped output is valuable, but too much path filtering can create blind spots. Some defects are visible only when comparing a changed file to sibling files outside the current scope or when a standard references a family-wide convention.

**Consequence.** A section agent may receive only its local slice and lose the sibling/precedent context needed to size a finding correctly.

**Recommendation.** Scoped packets should include a small "external comparison hints" section: sibling files, standards-sensitive peers, or cross-scope precedents that are not themselves part of the section payload. This can be capped separately from the main token budget.

### Problem 4 — Test-Gap Suppression Can Become A Silencer

P5 suppression is useful because raw missing-`TESTED_BY` lists are noisy. But suppression config can easily hide real review work, especially in data-layer or generated-code-adjacent patterns where integration tests are expected but not obvious.

**Consequence.** A suppressed test gap may disappear from agent attention without a reviewer understanding why.

**Recommendation.** Suppression metadata should include reason, matching rule, count, and optionally the top suppressed examples in debug/standard mode. Suppression should never affect `risk_score` silently; either risk remains unchanged or the payload states exactly how suppression affects scoring.

### Problem 5 — Index-Only Standards Selection Belongs In CRG Output

The standards companion docs show a major correctness issue: globbing `docs/standards/*` pulled superseded/non-indexed docs into review and produced wrong guidance. CRG currently knows code structure but not the Zazz standards index.

**Consequence.** Graph context can route agents to code, but it cannot yet route them to the authoritative standards that govern that code. That leaves a large part of Zazz review outside the graph.

**Recommendation.** Add a standards-index integration mode: given changed paths, return the applicable indexed standards and explicitly exclude non-indexed standards. This is both token-efficient and correctness-improving.

### Problem 6 — The Current Spec Leaves Review-Agent Topology Outside The Product

PR #214's apparent savings were heavily affected by 2 vs 6 sub-agents. CRG does not currently help choose an efficient review topology.

**Consequence.** Users may attribute savings to graph context when the savings mostly came from fewer agents or a shorter prompt topology.

**Recommendation.** Add a `review-plan` or `review-topology` output that suggests section splits based on changed paths, applicable standards, graph communities, and risk hotspots. It should report "recommended sections" and "why not split further."

## Additional Potential CRG Improvements

These are candidates beyond the current spec. They are ordered by expected value for Zazz review.

### A1 — Standards-Index Context Packet

Add a command/tool that maps changed files to applicable standards via `docs/standards/index.yaml`.

Suggested output:

```json
{
  "standards_context": [
    {
      "file": "docs/standards/database-sproc-errors.md",
      "matched_paths": ["backend/database/sql_migrations/stored-procedure/..."],
      "matched_activities": ["modifying stored procedure error handling"],
      "reason": "UPDATE/GETALL sproc error-code behavior"
    }
  ],
  "excluded_non_indexed_standards": ["docs/standards/service-layer-guide.md"]
}
```

Value:

- prevents superseded-standard confusion
- reduces standards context tokens
- aligns CRG directly with Zazz methodology

Tests:

- fixture with indexed and non-indexed standards
- changed paths mapping to multiple standards
- non-indexed docs never cited as authoritative

### A2 — Finding-Quality Evaluation Harness

Add an evaluation mode that compares graph-assisted review output against locked expected findings.

Use cases:

- PR #214 expected blocker: missing `@ReturnCode int = 0`
- service-layer-guide indexed/non-indexed standards trap
- no-leak 422 branch coverage gap
- dead OpenAPI status doc

Value:

- measures whether CRG improves review quality, not just token size
- prevents compact payload changes from deleting critical signal
- creates regression tests for real review misses

Metric ideas:

- expected blocker recall
- severity calibration accuracy
- false-positive count
- token cost per true actionable finding

### A3 — Sibling-Invariant Delta Signal

This is the P6 family, but it can be staged more narrowly than full construct-level divergence.

Stage 1:

- find sibling files/functions by path/name family and community
- compare simple invariant patterns with regex/AST snippets
- emit "changed file differs from sibling majority on safety-critical pattern"

For PR #214, this should have produced:

```text
GetAll return-code initializer: 2/2 sibling GetAll sprocs initialize @ReturnCode to 0; changed GetAll does not.
```

Value:

- directly targets the miss from the with-graph review
- turns sibling comparison into review signal
- improves findings, not just cost

Risk:

- can become noisy if majority practice conflicts with written standards
- must cite both sibling evidence and governing standard

### A4 — Standards-Vs-Codebase Drift Detector

CRG can compare standards claims to codebase majority patterns.

Examples from PR #214:

- standard says empty list GET should be 204; all siblings use `200 {"data": []}`
- standard prescribes eager read-sproc error-code resolution; GetAll family does not comply
- service tree exists in indexed and superseded docs and drifts

Value:

- separates "PR defect" from "standard drift"
- prevents repeated bot-vs-author fights
- creates candidate standards-improvement tickets

Output should mark these as team/process observations, not PR blockers.

### A5 — Review Topology Planner

Add a graph-informed planner that recommends sub-agent splits and prevents uncontrolled A/B comparisons.

Inputs:

- changed files
- communities
- applicable standards
- file count and tiers
- graph risk hotspots

Output:

- recommended number of sub-agents
- section scopes/globs for each
- shared context packet
- per-section context packet
- expected token budget

Value:

- reduces duplicated cache-write across agents
- makes with/without-graph experiments comparable
- helps Zazz keep Standards and Spec axes focused

### A6 — Read-First Plan

Compact output should not only say "here are top priorities"; it should say "read these exact files/lines first, then these standards, then these tests."

Suggested output:

```json
{
  "read_first": [
    {"path": "...GetAllReturnAddresses.sql", "lines": "20-35", "why": "return-code safety invariant"},
    {"path": "docs/standards/database-sproc-errors.md", "section": "Guard against NULL @ReturnCode"}
  ],
  "read_later": [...]
}
```

Value:

- directly reduces source-reading tokens
- makes review more deterministic
- bridges graph data with standards/spec context

### A7 — Prior-Finding Reconciliation Mode

PR #214 involved a prior bot review plus an author disposition table. CRG could help reconcile prior findings against current HEAD.

Capabilities:

- parse prior review findings if provided
- map finding files/lines to current diff
- classify likely fixed, still present, not applicable, or needs human review
- avoid re-litigating fixed issues

Value:

- reduces review tokens in re-review sessions
- catches "declined but narrow defect remains" cases
- turns prior-review text into structured work

### A8 — Evidence Packet For Spec Axis

The Spec axis often needs PR-body contract vs implementation evidence. CRG could emit contract coverage hints:

- PR body claims CREATE response exactly `{return_address_id}`
- changed route schema contains one output field
- test asserts value but not key exclusivity

This likely requires a PR-body input, not just graph data.

Value:

- improves Spec-axis findings
- catches contract-evidence gaps
- complements Standards-axis graph signals

### A9 — Risk Score Explanations And Severity Guardrails

Current risk scoring is too opaque for PR review. Compact payloads should include why a node is high/medium/low and what not to infer.

For example:

```json
{
  "risk_score": 0.72,
  "risk_reasons": ["untested", "flow-critical", "cross-community caller"],
  "severity_guardrail": "Risk score is triage only; do not downsize standard violations solely because siblings share the pattern."
}
```

Value:

- prevents "matches family, therefore nonblocking" from overriding a concrete invariant
- makes risk score auditable

### A10 — Whole-Session Token Accounting Integration

P7's `savings_record` is per change-analysis step. That is necessary but not enough.

Future work should measure:

- graph build/probe overhead
- orchestrator tokens
- sub-agent tokens
- cache write/read/output split
- de-duplication by message id
- cost per actionable finding

Value:

- gives honest product economics
- avoids repeating PR #214's measurement confusion
- lets teams decide when CRG is worth using

### A11 — Whole-Worktree Consolidation And Generated-Code Family Signals

CRG should use the indexed worktree graph to surface consolidation opportunities that are invisible when a reviewer looks only at the files changed by one report or route.

Important distinction:

- the PR diff supplies seed nodes for review
- the graph can include the whole indexed worktree
- unchanged siblings and generated/source-of-truth files provide evidence, but PR findings should still target changed code unless the deliverable owns broader cleanup

Potential signals:

- report-family implementations with repeated API calls, response normalization, filter mapping, or test setup
- generated clients and hand-written wrappers that drift from a shared OpenAPI/schema source
- several changed or unchanged reports that could use one shared adapter/helper after an API shape change
- registry/plugin/report families where a new endpoint or data wrapper can remove repeated per-report conditionals

Value:

- catches "this should be solved once for the report family" opportunities
- keeps single-file report reviews from missing simpler family-wide design moves
- supports Zazz scope discipline by labeling the candidate as blocking, nonblocking, or out-of-scope based on the deliverable spec

Risk:

- can become architecture advice spam if emitted without scope labels
- generated files may be ignored/excluded, so CRG must state whether it analyzed generated artifacts or only their source schemas/templates

## Recommended Changes To The Current Spec

These additions should be considered before implementation starts:

1. Keep the warning in the spec's Invariants: token efficiency work must not be treated as finding-quality proof.
2. Keep the new review-quality design guidance for standards-index context packets, standards-vs-codebase drift, review-topology planning, read-first plans, and whole-worktree consolidation signals.
3. Add an acceptance criterion in a later review-quality spec that compact output includes enough evidence to avoid down-sizing concrete standards violations solely because siblings share a broader pattern.
4. Add optional manual validation that checks not only token payload size but whether a PR #214-style critical invariant appears in the compact payload.

The current implementation PR should not absorb all of these. The strict recommendation is:

- keep the current spec focused on P1/P2/P3/P4/P5/P7
- keep the current implementation PR focused on P1/P2/P3/P4/P5/P7
- schedule standards-index context packets (A1), finding-quality evaluation (A2), sibling-invariant delta (A3), standards drift (A4), topology planning (A5), read-first plans (A6), and whole-worktree consolidation signals (A11) as review-quality deliverables

## Bottom Line

CRG's token-efficiency opportunity is real, but Zazz's value proposition is not "cheaper review at any cost." It is "cheaper review that remains anchored in standards, specifications, and source-verifiable findings."

The current spec improves the cost side and now documents the review-quality direction. The next implementation layer must improve the correctness side: standards-index integration, finding-quality evaluation, sibling-invariant deltas, standards-drift detection, review-topology planning, read-first evidence packets, and whole-worktree consolidation signals.
