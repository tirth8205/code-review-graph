---
name: code-review-graph
description: Use Code Review Graph to prepare token-efficient, standards-routed Zazz PR review context, impact analysis, and validation evidence. Use when reviewing a large or cross-cutting diff, when the user asks for CRG/graph/blast-radius/token-savings help, or when a Zazz review needs higher-signal findings without reading broad files first.
---

# Code Review Graph

## Purpose

Use this skill to make Code Review Graph the review context router for a Zazz-methodology PR review. CRG supplies compact changed-symbol, impact, test-gap, and affected-flow evidence; the repo's standards, specification, architecture, and execution docs supply the customizable review rules.

Do not encode project-specific standards in this skill. Always route through the repository's current standards index and source-of-truth Zazz documents so teams can customize review behavior by editing docs instead of editing skill prose.

## Startup

1. Read `AGENTS.md` or the repo equivalent for worktree rules, docs root, integration branch, and tool constraints.
2. Read the active requirement source when one exists: deliverable specification, PR body, issue, execution run log, feature doc, proposal, or architecture note.
3. Read the standards index before review or validation work. In Zazz repos this is usually `.zazz/standards/index.yaml`; otherwise use the docs root declared by repo instructions.
4. Match standards by `applies_to.paths` and `applies_to.activities`, then read only the matched standard docs. Prefer applicable standards over incidental legacy code patterns.
5. Establish the fixed comparison base with `git merge-base <base> HEAD`. Use the same pinned base for CRG, git diff, tests, and sub-agents.

If no standards index exists, continue with general engineering judgment and report that standards-driven routing was unavailable.

## CRG Context

Use CRG before broad file reading when the diff is large, cross-cutting, unfamiliar, or explicitly graph-related.

Preferred commands:

```bash
uv run code-review-graph review-context --repo . --base <base> --max-tokens 2000
uv run code-review-graph detect-changes --repo . --base <base> --for-review --max-tokens 2000
uv run code-review-graph detect-changes --repo . --base <base> --for-review --scope "path/or/glob/**"
```

Use `review-context` for one-shot build/update plus compact output. Use `detect-changes --for-review` when graph state is already current. Use `--scope` to create section-agent packets for services, packages, migrations, tests, docs, or frontend surfaces.

When MCP tools are available, prefer `detect_changes_tool` with `for_review: true`, `max_tokens`, and `scope` over manual grep-style exploration.

## Standards-Routed Review

Treat CRG output as a triage map, not a replacement for standards or source inspection.

For each compact packet:

1. Map changed paths to applicable standards from the standards index.
2. Map changed paths and affected flows to specification acceptance criteria or PR intent.
3. Read only the smallest source/test/doc slices needed to verify a possible finding.
4. Check whether test gaps are real or intentionally suppressed. Suppression must be observable in CRG output, not silent.
5. Prefer findings that connect changed code, governing docs, impact radius, and user-visible or operational risk.

High-signal review questions:

- Does the diff change a public contract, data shape, persistence rule, or runtime behavior without matching tests?
- Do affected callers or flows show a risk that is outside the edited file?
- Does a changed seam require comparing both sides of a contract, such as SQL result columns to Python wrapper tuples,
  OpenAPI responses to route validation, or permission constants to seed grants?
- Did a generated or mechanical change miss a nearby invariant documented in standards, specs, migrations, fixtures, or sibling patterns?
- Are new tests proving behavior, or only checking mocks, source text, framework behavior, or incidental call order?
- Does the claimed token savings apply only to change-analysis context, or is it incorrectly presented as whole-session savings?

## Zazz Review Shape

For PR review, pair this skill with `pr-review` when available:

- CRG provides the compact graph context and impact map.
- The Standards axis uses the matched standards docs plus CRG impact/test signals.
- The Spec axis uses the active specification or PR intent plus CRG affected flows.
- `qa-testing` verifies acceptance criteria and test quality after implementation.
- `pr-builder` packages the final evidence into a PR body when requested.

Keep Standards and Spec findings separate. Cross-axis overlap is useful evidence; do not collapse it into a single generic issue.

## Evidence And Validation

Record review evidence in the repo's coordination surface: run log, PR body, verification note, or execution record.

Evidence should include:

- CRG command, base ref, scope, and max token budget.
- Compact output token estimate or savings record.
- Matched standards docs and requirement sources.
- Commands run and pass/fail results.
- Confirmed findings with file/line references and why the governing doc or graph impact makes them actionable.
- A file-first findings structure compatible with `pr-review/findings-reporting.md`: must-fix summary grouped by file
  and line, then detailed findings grouped by file with axis, severity, why, and proposed fix. Include all `[boulder]`,
  `[rock]`, and `[big-pebble]` findings in the must-fix summary without repeating full detail text. Do not emit raw HTML
  anchors or fenced code blocks around whole findings in the final artifact.
- When paired with `pr-review`, the axis artifact directory and per-axis packet files used for consolidation. If the
  final findings differ materially from the packet findings, include consolidation notes explaining kept, merged,
  downgraded, ruled-out, or omitted findings.
- Residual risk when CRG, standards, or tests could not be run.

For claims that CRG improves review quality, compare against a concrete baseline:

- What issue was previously missed or required broad manual reading?
- Which compact packet, affected flow, test gap, standard, or spec clue now points to it?
- How many tokens were budgeted for CRG change-analysis context?
- Which command or manual check confirms the finding is real?

Only claim "uses less tokens" for the scoped change-analysis context unless the full review session was measured separately.
