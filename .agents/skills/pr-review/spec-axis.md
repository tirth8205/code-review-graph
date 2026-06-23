# Spec Axis — Sub-Agent Brief

Review the diff through the lens of specification compliance, methodology, and scope discipline. This axis answers:
**does the code faithfully implement what was asked for?**

You receive this brief alongside `shared-rules.md`, which contains the diff scope discipline, finding sizing taxonomy,
output format, and boundaries. Follow both documents.

The orchestrator will tell you which spec availability tier applies to this review and provide the spec contents. Zazz
methodology repos typically store deliverable specifications in `<DOCS_ROOT>/specifications/`. Your behavior varies by
tier.

## Tier 1 — Full Spec

A deliverable specification, PRD, or detailed issue with acceptance criteria exists. This is the highest-confidence
tier.

Verify:

- each changed behavior maps to scope, decisions, ACs, or an approved revision
- each AC has matching evidence in tests, commands, or manual verification
- no files outside the specification's scope changed without explanation
- implementation stayed inside hard constraints and invariants
- run-log or external execution notes are updated when the specification requires them
- PR body links the governing specification/work item and accurately reports evidence

For each finding, quote the spec line or AC that the code departs from. This frames the finding as traceable to a
requirement, not reviewer opinion.

### Scope Drift

Flag changes that go beyond the specification's declared scope:

- files modified that the spec does not mention or imply
- behavior added that no AC or decision covers
- refactoring or cleanup unrelated to the deliverable, even if beneficial
- new abstractions or patterns introduced that the spec did not call for

Scope drift is typically a `[rock]` when it introduces untested behavior or changes a contract, and a `[pebble]` when
it is harmless but adds review noise.

### Missing And Partial Requirements

Check each AC and stated requirement. For each one, determine whether the diff:

- **fully satisfies** it (evidence in code + tests)
- **partially satisfies** it (code exists but evidence is incomplete)
- **does not address** it (no matching code found in the diff)

Report partial and missing requirements as findings. A missing AC with no code at all is typically a `[rock]`.

## Tier 2 — Lightweight Spec

Only a PR description, brief issue, or user-stated intent exists — no formal specification with structured ACs.

In this tier:

- treat the PR description and linked issue as the requirements source
- check whether the diff matches the stated intent — does it do what the author said it would?
- check for scope drift: files or behaviors that go beyond the stated purpose
- check for intent gaps: stated goals that the diff does not appear to address
- frame methodology findings as **lower-confidence** — note that you are reviewing against informal intent, not formal
  ACs

Do not invent requirements the author did not state. If the PR description is vague, review what you can and note the
reduced confidence as residual risk.

### What Lightweight Spec Review Can Still Catch

Even without formal ACs, this tier reliably catches:

- the diff does something the PR description does not mention (scope creep)
- the PR description promises something the diff does not deliver (missing work)
- files changed that are unrelated to the stated purpose (accidental inclusion)
- the PR body claims evidence that the diff does not support

## Tier 3 — No Spec

No specification, no linked issue with requirements, and the PR description is too thin to review against meaningfully.

In this tier:

- state clearly that no spec or meaningful requirements source was found
- note this as **residual review risk** — the Functionality / Spec axis cannot verify intent alignment without a
  requirements source
- if there is any PR body text at all, check for obvious contradictions between the description and the diff
- do **not** generate findings about missing ACs or methodology compliance — there is nothing to measure against

If the orchestrator tells you the tier is "no spec" and there is literally no PR body and no issue context, produce
only the residual-risk note. Do not pad the report.

### Cross-Axis Note

When the Standards / Code Quality axis raises a redundant-computation finding that also represents a design-level
strategy error, evaluate whether the underlying approach was within scope and whether a specification's design decisions
(if any) anticipated the data-flow shape. If the implementation strategy diverged from the spec's intent, this is a
Functionality / Spec axis finding in addition to a Standards / Code Quality axis finding.
