---
name: pr-review
description: Review a pull request, branch, or local diff along independent axes — Standards/Code Quality, Functionality/Spec, Security/Data/Ops, and Test Quality — using repo standards, CRG context when useful, and parallel sub-agents; use when the user wants draft-PR self-review, reviewer-side PR feedback, standards-guided findings, graph-assisted review, or review readiness assessment.
---

# PR Review Skill

## Mission

Review a PR, branch, or local diff as an automated review pass along independent axes:

- **Standards / Code Quality** — does the code conform to documented coding standards, architecture conventions, maintainability expectations, and anti-slop guidance?
- **Functionality / Spec** — does the code faithfully implement the originating specification, issue, PR intent, and user-visible behavior?
- **Security / Data / Ops** — does the diff preserve auth/authz, tenant boundaries, persistence safety, operational observability, and failure-mode behavior?
- **Test Quality** — does the evidence prove the behavior and realistic risks without padding the PR with low-value tests?

The axes run as **parallel sub-agents** so they don't pollute each other's context, then this skill aggregates their
findings. Reporting them separately prevents one lens from masking another: code that follows every style rule but
implements the wrong thing is a Functionality failure, not a pass; code that works but violates a security boundary is
a Security failure; tests that inflate coverage without proving behavior are Test Quality findings.

It can be used by the PR author during draft cleanup, or by a human reviewer evaluating someone else's PR.

Actor boundary:

- `pr-builder` drafts or updates the PR title/body from the author's evidence.
- `pr-review` inspects the code, tests, evidence, and standards alignment. It may run on the author's own draft branch
  or on someone else's submitted PR.

This skill does not approve, merge, mark a PR ready, or replace human judgment.

## Startup Sequence

### 1. Read Repo Context And Discover Standards

Read `AGENTS.md` (or `CLAUDE.md`) for repo-specific review workflow, docs root, standards, target branch, and tracking
conventions when available.

**Standards discovery cascade** — resolve the docs root and standards index using this order. Stop at the first hit:

1. `AGENTS.md` or `CLAUDE.md` declares a docs root (e.g., `docs/`) → look for `<docs-root>/standards/index.yaml`.
1. The environment variable `ZAZZ_DOCS_ROOT` is set → use its value as the docs root.
1. Convention: check `docs/standards/index.yaml` at the repo root.
1. If none of the above finds a standards index, ask the user: "I couldn't find a standards index. Where are your
   coding standards (a directory path), or should I run without standards-driven review?"

Record the resolved docs root and standards index path. If no standards are available, the Standards / Code Quality
axis still runs using general engineering judgment but notes the gap as residual risk.

**Integration branch discovery** — resolve using this order:

1. `AGENTS.md` declares the integration/target branch.
1. Convention: check for `dev`, then `main`, then `master`.
1. If ambiguous, ask the user.

### 2. Identify The Review Target

Determine what is being reviewed:

- a GitHub PR URL/number
- the current draft branch against the integration branch
- another author's branch against the integration branch
- a stack branch or dependent PR
- a local diff before a PR exists

### 3. Pin The Comparison Base

Establish the fixed point for the diff. Whatever the user said is the fixed point — a commit SHA, branch name, tag,
`main`, `HEAD~5`, etc. Don't be opinionated; pass it through.

If they didn't specify one, ask: "Review against what — a branch, a commit, or the integration branch?" Don't proceed
until you have it.

Once established, pin the merge base:

```bash
MERGE_BASE=$(git merge-base <fixed-point> HEAD)
```

All diff commands in both sub-agents use this identical pinned base. This prevents drift if the integration branch
advances between when the two sub-agents start. Capture:

- **Diff command:** `git diff $MERGE_BASE...HEAD`
- **Commit list:** `git log $MERGE_BASE..HEAD --oneline`
- **Changed files:** `git diff $MERGE_BASE...HEAD --name-only`
- **Changed file count:** `git diff $MERGE_BASE...HEAD --name-only | wc -l`
- **Head SHA:** `git rev-parse HEAD`

### 4. Size The Review And Prefer Code-Review-Graph For Large Diffs

Use the changed-file count from step 3 as a cheap sizing gate before reading broad file contents.

`code-review-graph` is the preferred context accelerator for large PRs. Use it to reduce token usage and avoid reading
broad file contents before knowing which files, symbols, flows, and tests matter. If the changed file count is
**greater than 10**, or the user explicitly asked for graph context, blast-radius analysis, token-efficient review, or
graph tooling, use the `$code-review-graph` skill when available. Its job is to produce compact graph context while
routing customization through the repo's standards index and Zazz docs.

If `$code-review-graph` is not installed in this repo, read `code-review-graph.md` from this skill directory as the
fallback local workflow.

If the changed file count is **10 or fewer** and the user did not ask for graph context, do not load the optional
utility file. Record `Graph context: not requested - N changed files` in the preamble.

When the optional helper is loaded, capture its concise graph summary so both review axes can use it. Do not block an
ordinary review on this optional utility unless the user specifically requested it.

### 5. Gather Governing Context

Collect the inputs each sub-agent will need.

**For the Standards / Code Quality axis:**

1. Load the standards index from the resolved docs root (step 1).
1. Match the changed file paths and activity to standards entries using the index's `applies_to` rules.
1. Read only the matched standards files.
1. Note machine-enforced config files (eslint, prettier, tsconfig, editorconfig) — the sub-agent should not re-check
   what tooling already checks.

**For the Functionality / Spec axis**, search for the originating spec in this order:

1. A deliverable specification or external specification record linked in the PR body or branch name.
1. A specification file in `<DOCS_ROOT>/specifications/` (or `docs/specifications/` by convention) matching the branch
   name, feature name, or linked work item.
1. Issue references in the commit messages (`#123`, `Closes #45`, etc.) — fetch via the repo's issue tracker workflow
   if available.
1. A path the user passed as an argument.
1. A PRD/spec file under `docs/`, `specs/`, or a project-specific location matching the branch name or feature.
1. The PR body and linked work item as a lightweight spec substitute.

### 6. Determine Spec Availability

Classify the spec situation into one of three tiers:

- **Tier 1 — Full spec**: a deliverable specification, PRD, or detailed issue with acceptance criteria exists. The
  Functionality / Spec sub-agent reviews against it with full methodology checks.
- **Tier 2 — Lightweight spec**: only a PR description, brief issue, or user-stated intent exists. The Functionality /
  Spec sub-agent reviews against it but frames findings as lower-confidence.
- **Tier 3 — No spec**: nothing found, and the user confirms there isn't one. The Functionality / Spec sub-agent runs
  in reduced mode or is skipped if there is literally no PR body and no issue context.

If no spec source is found, ask the user: "I didn't find a spec, PRD, or linked issue. Is there one I should look at,
or should I review against the PR description / your stated intent only?"

If they say there isn't one and the PR description is too thin to review against, skip the Functionality / Spec axis
and note it as residual risk.

### 7. Preamble Confirmation

Before dispatching, present a short summary of what was discovered so the user can correct any misdetection. This is a
single confirmation, not a multi-step interview:

```
**Review preamble — please confirm or correct:**

- **Target**: <branch/PR> against `<integration-branch>` (merge base: `<short-sha>`)
- **Standards**: <N standards matched from `<docs-root>/standards/index.yaml`> [or "none found — running with general judgment"]
- **Spec**: <tier> — <spec source description> [or "none found — Functionality / Spec axis will be skipped"]
- **Graph context**: <not requested/unavailable/declined/available/recommended> [brief risk/blast-radius, token budget, and scope summary if used]
- **Changed files**: <N files> across <services/areas>
- **Axes**: Standards/Code Quality, Functionality/Spec, Security/Data/Ops, Test Quality

Proceed with review, or should I adjust anything?
```

If the user says to proceed (or does not object), dispatch immediately. If they correct something (e.g., "the spec is
at docs/specs/foo.md"), update the context and re-confirm only if the correction changes the tier.

When the skill is invoked with enough context to resolve all inputs unambiguously (e.g., the user passed a PR URL, the
repo has AGENTS.md with standards, and the PR body links a spec), the preamble may be compressed to a single line:
"Reviewing PR #123 against dev with 4 matched standards and the linked spec. Dispatching."

### 8. Dispatch Sub-Agents

Send a **single message with one `Agent` tool call per active axis** so axes run in parallel. Use `general-purpose`
subagent type for each.

Read the following files from this skill's directory:

- `shared-rules.md` — every axis receives this
- `findings-reporting.md` — every axis receives this and the aggregator applies it to the final artifact
- `axis-artifacts.md` — every axis receives this and the aggregator uses it for intermediate packet files
- `standards-axis.md` — Standards / Code Quality sub-agent only
- `spec-axis.md` — Functionality / Spec sub-agent only
- `security-axis.md` — Security / Data / Ops sub-agent only
- `test-quality-axis.md` — Test Quality sub-agent only

Before dispatch, follow `axis-artifacts.md` to create a fresh axis artifact directory and determine the expected packet
paths. Pass the axis directory and axis-specific packet path to each active sub-agent. If sub-agents cannot write files,
require them to return complete packet text so the orchestrator can write it verbatim before consolidation.

**Standards / Code Quality sub-agent prompt — include:**

- The pinned merge base, diff command, commit list, and changed-files list from step 3
- The optional code-review-graph summary from step 4, if available, especially blast radius, impacted
  callers/dependents, and test signals
- The list of matched standards files and their contents from step 5
- The full text of `shared-rules.md`
- The full text of `findings-reporting.md`
- The full text of `axis-artifacts.md`
- The full text of `standards-axis.md`
- Instruction: "You are the Standards / Code Quality axis reviewer. Review the diff using the shared rules, matched
  standards, standards-axis brief, and findings reporting contract. Produce findings in the specified output format.
  Focus on standards conformance, maintainability, architecture fit, agentic slop, and redundant computation. Do not
  assess requirement satisfaction, security/data/ops risk, or test evidence quality except where a standards violation
  requires local context. Write your axis packet to the assigned `01-standards-code-quality.md` path; if file writes are
  unavailable, return the complete packet text."

**Functionality / Spec sub-agent prompt — include:**

- The pinned merge base, diff command, commit list, and changed-files list from step 3
- The optional code-review-graph summary from step 4, if available, especially blast radius and affected flows that may
  change acceptance-criteria coverage
- The spec contents or path from step 5, with the tier classification from step 6
- The full text of `shared-rules.md`
- The full text of `findings-reporting.md`
- The full text of `axis-artifacts.md`
- The full text of `spec-axis.md`
- Instruction: "You are the Functionality / Spec axis reviewer. Review the diff using the shared rules and spec-axis
  brief, plus the findings reporting contract. The spec availability tier for this review is: [tier]. Produce findings
  in the specified output format. Focus on stated requirements, user-visible behavior, public contracts, scope drift,
  and acceptance-criteria coverage. Do not assess coding style, security policy, or test quality patterns except where
  needed to prove a functionality finding. Write your axis packet to the assigned `02-functionality-spec.md` path; if
  file writes are unavailable, return the complete packet text."

**Security / Data / Ops sub-agent prompt — include:**

- The pinned merge base, diff command, commit list, and changed-files list from step 3
- The optional code-review-graph summary from step 4, if available, especially affected flows, callers/dependents,
  database objects, route/service boundaries, and shared infrastructure
- The matched standards files related to auth/authz, data, migrations, errors, logging, observability, CI/deploy, and
  operations
- The full text of `shared-rules.md`
- The full text of `findings-reporting.md`
- The full text of `axis-artifacts.md`
- The full text of `security-axis.md`
- Instruction: "You are the Security / Data / Ops axis reviewer. Review the diff using the shared rules, matched
  standards, security-axis brief, and findings reporting contract. Produce findings in the specified output format.
  Focus on auth/authz, secrets, tenant boundaries, injection/path risks, persistence and migration safety, idempotency,
  transactions, error handling, logging/metrics, and operational recovery. Do not assess general code style or
  requirements coverage unless they create security/data/ops risk. Write your axis packet to the assigned
  `03-security-data-ops.md` path; if file writes are unavailable, return the complete packet text."

**Test Quality sub-agent prompt — include:**

- The pinned merge base, diff command, commit list, changed-files list, and tests/static checks already run
- The optional code-review-graph summary from step 4, if available, especially test gaps, affected flows, changed test
  files, and impact radius
- The matched testing standards and requirement evidence expectations
- The full text of `shared-rules.md`
- The full text of `findings-reporting.md`
- The full text of `axis-artifacts.md`
- The full text of `test-quality-axis.md`
- Instruction: "You are the Test Quality axis reviewer. Review the diff using the shared rules, matched testing
  standards, test-quality brief, and findings reporting contract. Produce findings in the specified output format.
  Focus on whether tests and verification prove real behavior and risks, whether required evidence is missing, and
  whether new tests are low-value, redundant, brittle, or mock-only. Do not re-review implementation style or spec
  compliance except where missing evidence makes the risk concrete. Write your axis packet to the assigned
  `04-test-quality.md` path; if file writes are unavailable, return the complete packet text."

If the Functionality / Spec axis is being skipped (tier 3 with no usable context), note the skip when aggregating that
it had no usable requirement source. Security / Data / Ops and Test Quality still run when the diff touches their
surfaces or when the user requested a comprehensive review.

Skipped axes still get packet files per `axis-artifacts.md`.


### 9. Run Focused Validator Passes

After the independent axes finish and before final aggregation, run a small set of focused validator passes whenever the
changed-file set touches the relevant surfaces. These passes are narrower than the axis agents: they exist to catch
cross-seam issues that one-agent-per-axis can miss.

Use focused validators when their trigger matches:

- **Cross-Seam Contract Validator** - Trigger when the PR changes stored procedures, data-layer wrappers, generated
  clients/schemas, OpenAPI docs, service functions, or tests at those seams. Verify the contract end to end: producer
  shape, wrapper/schema shape, consumer behavior, and the test that would fail if they drifted.
- **Service Evidence Matrix Validator** - Trigger when service-layer functions are added or changed. Build a table of
  changed service function -> data wrapper/sproc or external dependency -> unit evidence -> integration evidence ->
  public-boundary evidence. Compare the table to the matched testing standards.
- **HTTP Contract Validator** - Trigger when HTTP route files, schemas, OpenAPI response docs, or HTTP tests change.
  Compare actual returned/aborted statuses and detail envelopes to `@bp.doc(responses=...)` and route tests.
- **Spec Reconciliation Validator** - Trigger when the review uses a PR body, issue, or spec as requirement evidence.
  Compare the current requirement text to route docs, tests, and implemented behavior; classify contradictions
  separately from intentional documented deviations.

Each validator returns one short packet with: trigger, files inspected, findings, rejected candidates, and residual
risk. Validators may be run by sub-agents or by the orchestrator, but their output must be read during aggregation. Do
not let validator findings bypass source verification; every final finding still needs code, test, standard, or spec
evidence.

### 10. Aggregate

Assemble the final review as a file-first action document. Sub-agents may return axis-specific reports, but the final
artifact groups detailed findings by source file and line so an author can address every issue in a file together and
resolve merge conflicts locally. Do **not** hide which axis raised a finding — every finding keeps its axis label,
severity tag, and proposed fix.

Before writing the final artifact, read every active or skipped axis packet file and every focused-validator packet from the axis artifact directory. If a
sub-agent returned packet text instead of writing a file, write that text to the expected packet path first, then read it
back for aggregation. The final artifact is consolidated from packet files and validator packets, not from memory.

Apply `findings-reporting.md` as the source of truth for final artifact structure, must-fix summary, detailed finding
sections, systemic improvement opportunities, cross-axis overlap, axis coverage, verification, and summary.

Apply `axis-artifacts.md` as the source of truth for intermediate packet file names, skipped-axis packet content, and
consolidation notes. Record the axis artifact directory, packet file list, and focused validators run in the final artifact's `Verification`
section.

Use the exact final section order from `findings-reporting.md`:

1. `Must-Fix Findings By File And Line`
1. `Detailed Findings By File`
1. `Systemic Improvement Opportunities` when applicable
1. `Cross-Axis Overlap` when applicable
1. `Consolidation Notes` when applicable
1. `Axis Coverage`
1. `Verification`
1. `Summary`

The final detailed findings are grouped by file path and sorted by line number. Each finding includes axis, severity,
why it matters, and proposed fix. Any open `[boulder]`, `[rock]`, or `[big-pebble]` from any active axis means the PR is
not approvable until resolved.

If the consolidated final artifact has materially fewer actionable findings than the axis packets, add a short
consolidation note explaining which findings were ruled out, merged, downgraded, or omitted and why.

#### Targeted Tests And Static Checks

Run targeted tests or static checks only when they are necessary and reasonable for the review. If not run, state that
clearly in the residual risk. Prefer running checks before dispatch so both sub-agents benefit from the results.
