# PR Review Skill — User Guide

How to use and adapt the **pr-review** skill for Zazz methodology pull request review.

## What It Does

The PR Review skill reviews a pull request, branch, or local diff along independent axes using parallel sub-agents:

- **Standards / Code Quality axis** — does the code conform to documented coding standards, architecture conventions,
  maintainability expectations, and anti-slop guidance?
- **Functionality / Spec axis** — does the code faithfully implement the originating specification, issue, or stated
  intent?
- **Security / Data / Ops axis** — does the diff preserve auth/authz, data integrity, operational observability, and
  safe failure behavior?
- **Test Quality axis** — does the evidence prove the behavior and realistic risks without adding low-value tests?

Reporting the axes separately prevents one from masking another: code that follows every standard but implements the
wrong thing is caught as a Functionality failure. Code that works but weakens a permission boundary is caught as a
Security failure. Tests that inflate count without proving behavior are caught as Test Quality findings.

The skill also looks for common agent-generated clutter:

- low-value or duplicate tests
- mock-heavy tests that do not prove behavior
- unrealistic edge-case permutations
- redundant helpers or parallel implementations
- speculative abstractions
- noisy comments, formatting churn, and broad unrelated rewrites
- duplicated runtime computation across layers or seams

Actor boundary:

- `pr-builder` drafts or updates the PR title/body from the author's evidence.
- `pr-review` inspects the code, tests, evidence, and standards alignment. It may run on the author's own draft branch
  or on someone else's submitted PR.

The skill does not approve, merge, mark a PR ready, or replace human judgment.

## File Structure

```text
.agents/skills/pr-review/
  SKILL.md              # Orchestrator: startup, pinning, optional utility loading, dispatch
  README.md             # This file
  axis-artifacts.md     # Per-axis packet workflow before final consolidation
  code-review-graph.md  # Fallback graph-context utility workflow
  findings-reporting.md # Canonical final findings artifact structure and block format
  shared-rules.md       # Diff scope, finding sizing, output format, boundaries
  standards-axis.md     # Standards sub-agent brief
  spec-axis.md          # Spec sub-agent brief
  security-axis.md      # Security, data, and ops sub-agent brief
  test-quality-axis.md  # Verification and test-quality sub-agent brief
```

- **SKILL.md** orchestrates the review: reads repo context, pins the comparison base, optionally loads utility
  guidance, gathers governing context, determines spec availability, dispatches the active axis sub-agents in parallel,
  and aggregates their findings.
- **axis-artifacts.md** defines the per-axis packet workflow. Each active or skipped axis writes an intermediate file, and
  the orchestrator builds the final artifact from those files instead of from memory.
- **code-review-graph.md** describes the fallback graph-context workflow when the repo-vendored `$code-review-graph`
  skill is unavailable.
- **findings-reporting.md** defines the consistent final findings structure: must-fix summary, detailed findings by
  file, cross-axis overlap, optional consolidation notes, axis coverage, verification, and summary.
- **shared-rules.md** contains rules both sub-agents need: diff scope discipline, the geological finding-sizing
  taxonomy, security/data/operations escalation, output format, and boundaries.
- **standards-axis.md** is the full brief for Standards / Code Quality: standards-driven review, agentic slop, and
  redundant computation checks.
- **spec-axis.md** is the full brief for Functionality / Spec: methodology checks with three tiers of behavior
  depending on spec availability.
- **security-axis.md** is the full brief for Security / Data / Ops: auth/authz, secrets, injection, persistence,
  migrations, error handling, observability, and deployment risk.
- **test-quality-axis.md** is the full brief for Test Quality: missing evidence, weak tests, redundant tests, brittle
  tests, and verification gaps.

Keep skill modules incrementally discoverable. If a `SKILL.md` or companion module grows past 400 lines, split it by
task or sub-feature; past 600 lines, the split is blocking before review approval. The entry point should stay as
orchestration and load only the task-specific file needed for the current review.

## When To Use It

Use this skill when:

- an implementation branch is ready for author-side review
- a draft PR needs cleanup before the Owner marks it ready
- an AI-generated or agent-assisted PR has grown large enough that the human wants the agent to understand it first
- a human wants a second pass focused on risks and test quality
- a stack branch needs review before submitting or after a rebase
- a PR feels noisy and needs help separating real issues from agentic clutter

Example prompts:

```text
Use pr-review.
Review the current branch against dev and focus on standards conformance, test quality,
and agentic slop.
```

```text
Use pr-review.
Review PR #123 and help me decide what findings to send back to the author.
```

```text
Use pr-review.
This is a backend/database change. Load the relevant standards from docs/standards/index.yaml
and call out any realistic edge cases the tests miss.
```

## How The Skill Chooses Context

The orchestrator starts small, then loads more context only when the diff needs it.

1. `AGENTS.md` for docs-root, integration branch, workflow, and review conventions.
1. The review target: working tree diff, branch diff, PR, or stack branch.
1. **Pin the comparison base** — `git merge-base` against the fixed point, so both sub-agents use an identical diff
   reference even if the integration branch advances.
1. **Size the diff and prefer graph context for large reviews** — count changed files from the pinned diff. If the
   count is greater than 10, or the user requested graph, blast-radius, or token-efficient review, use
   `$code-review-graph` to gather compact graph context and standards-routed review evidence. If that skill is
   unavailable, load `code-review-graph.md` as the local fallback.
1. Governing context: deliverable specification, PR body, linked ticket, and ACs.
1. **Determine spec availability** — full spec, lightweight spec, or no spec.
1. `<DOCS_ROOT>/standards/index.yaml` — select only the standards matching the changed paths and activities.
1. Create an axis artifact directory and dispatch the active axis sub-agents with their respective briefs and packet
   paths.
1. Read the completed axis packets, then consolidate the final file-first findings artifact.

This keeps the skill generic while letting each repo provide its own standards.

Axis packet files are required even when a sub-agent cannot write directly. In that case the sub-agent returns complete
packet text and the orchestrator writes it to the expected packet path before consolidation. If the final report has
materially fewer findings than the packets, include a `Consolidation Notes` section explaining which findings were
merged, downgraded, ruled out, or omitted.

## Optional Graph Utility

The skill can use `$code-review-graph` for graph-derived
blast radius, impacted callers/dependents, affected flows, test-coverage signals, and lower-token review context for
large diffs.

Sizing and context-loading boundary:

- Do not load `code-review-graph.md` during ordinary reviews with 10 or fewer changed files unless graph context is
  requested.
- Use `$code-review-graph` for PRs with more than 10 changed files so the agent can prefer compact graph-derived
  review context before reading broad file contents.
- Keep `code-review-graph.md` as fallback guidance for repos that have not vendored the new skill.
- Keep human install, setup, troubleshooting, and update checks in `docs/code-review-graph.md` or
  `.zazz/docs/code-review-graph.md` when that project doc exists.
- For Zazz review, prefer minimal CLI/MCP setup. Do not install upstream companion skills, hooks, or instruction
  injections unless the user explicitly asks for full upstream integration.
- Treat graph output as advisory context. Findings still need to be verified against the actual diff, source, tests,
  standards, and spec.

Useful public companion skills in the `code-review-graph` repository include `review-pr`, `review-delta`,
`review-changes`, `build-graph`, `explore-codebase`, `debug-issue`, and `refactor-safely`. The PR Review skill does not
replace itself with those skills or install them by default; it borrows their graph-first context-gathering workflow
when available.

### Simple Graph Gate

Use the pinned diff to decide whether graph context should be loaded:

```bash
git diff $MERGE_BASE...HEAD --name-only | wc -l
```

- `0-10` changed files: skip graph context unless the user asks for graph or blast-radius review.
- `11+` changed files: load `code-review-graph.md`; if the tool is unavailable, tell the user it is recommended for a
  PR of this size and ask whether to install/configure it, use an existing install, or continue without it.

## Spec Availability Tiers

The orchestrator classifies the spec situation before dispatching:

- **Tier 1 — Full spec**: a deliverable specification, PRD, or detailed issue with acceptance criteria. The
  Functionality / Spec axis reviews with full methodology checks.
- **Tier 2 — Lightweight spec**: a PR description, brief issue, or user-stated intent only. The Functionality / Spec
  axis reviews against it but flags findings as lower-confidence.
- **Tier 3 — No spec**: nothing found. The Functionality / Spec axis runs in reduced mode (checking for obvious
  contradictions with the PR body) or is skipped entirely if there is no usable context. Noted as residual review risk.

## Customizing Review Guidance

Team- and repo-specific review policy should live in `<DOCS_ROOT>/standards/`, not in the generic PR Review skill.

Use the standards directory for rules such as:

- frontend component patterns
- browser accessibility expectations
- API response shape and validation semantics
- auth/authz and tenant-boundary rules
- database migration safety
- fixture and test-data conventions
- logging, metrics, and operational requirements
- generated artifact and schema review rules

The standards index should make those files discoverable by changed path, language, service, domain, or activity. A
useful index entry normally answers:

- which paths or file globs it applies to
- which activity tags it covers, such as `frontend`, `api`, `database`, `auth`, or `testing`
- which standards file to read
- any special review notes or required evidence

Keep standards concrete and repo-specific. The generic skill describes how to review; standards describe what this repo
expects.

## Test Review Philosophy

The skill should push for stronger evidence, not more tests by default.

Good PR review asks:

- Do the tests prove the acceptance criteria?
- Do they cover realistic field edge cases?
- Could a shared setup, shared payload, parameterized test, or table-driven test cover the same scenarios more clearly?
- Is existing coverage already sufficient?
- Are tests asserting observable behavior rather than private mechanics?
- Would these tests fail for bugs the team actually cares about?

Reviewers should flag both under-testing and test clutter. The goal is compact, meaningful coverage. Irrelevant
permutations or coverage-padding tests should be treated as review noise unless they prove a real requirement, defect,
boundary, or risk. This includes unreasonable precondition tests that do not reflect the public contract, such as
testing an update path without the record ID required to address the record.

## Improving The Skill

Improve repo standards first when the desired behavior is repo-specific. Improve the generic skill when the behavior
should apply across Zazz methodology repos.

Good candidates for repo standards:

- "Our React forms use this validation pattern."
- "Our migrations must include rollback notes and data-volume estimates."
- "Our API errors must use this envelope."
- "Our fixtures must come from these builders."

Good candidates for the generic skill:

- better review severity definitions → `shared-rules.md`
- better progressive-loading or dispatch rules → `SKILL.md`
- broader standards-review guidance → `standards-axis.md`
- clearer test-quality heuristics → `standards-axis.md`
- better spec-compliance checks → `spec-axis.md`
- better output formatting expectations → `shared-rules.md`

When adding generic guidance, keep each file focused on its axis. The orchestrator (`SKILL.md`) handles flow control;
the axis briefs handle review substance.

## Output Expectations

`findings-reporting.md` is the source of truth for final artifact structure. The review leads with a
**Must-Fix Findings By File And Line** action queue, then includes **Detailed Findings By File** so all issues in a file
can be addressed together. Each finding carries its axis, severity, why, and proposed fix; axis information is preserved
without using separate long axis sections.

The final artifact should render cleanly in Markdown. Do not emit raw HTML anchors or wrap whole findings in fenced
`text` blocks. Use short Markdown finding headings such as `#### F-SDO-001 [rock] line 75 - ...`, then bullets for
axis, severity, location, evidence, why, and proposed fix.

The must-fix queue is grouped by file path and sorted by line number. It includes every `[boulder]`, `[rock]`, and
`[big-pebble]` finding and links to the detailed file finding instead of repeating the full Why/Fix body.
When optional pebbles appear in the same file as must-fix work, the queue may call them out as related optional cleanup
so the author can address the file efficiently; those optional findings still do not block approval.

Each finding is a copy-paste-able PR-comment Markdown subsection. The heading includes its size tag (`[boulder]` /
`[rock]` / `[big-pebble]` / `[pebble]` / `[sand]`), line, and title; the body includes `Axis`, `Severity`, `Location`,
`Evidence`, `Why it matters`, and `Proposed fix`.

When the review uncovers a theme that spans files — for example oversized service test modules, repeated route
declarations, repeated mock setup, or a convention that should be codified — include a concise
**Systemic Improvement Opportunities** section. Do not use that section to hide blocking findings; anything required
for approval still appears under the affected file.

`[boulder]`, `[rock]`, and `[big-pebble]` block approval; `[pebble]` and `[sand]` are the author's discretion. Any
blocking finding from any active axis means the PR is not approvable.

If multiple axes flag the same `file:line`, the aggregator notes the overlap as a signal that the issue is particularly
important.

The review ends with a summary: per-axis finding counts, a combined approval verdict, and any residual risk (standards
not found, spec gaps, tests not run).
