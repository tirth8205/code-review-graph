# Shared Review Rules

These rules apply to every PR-review axis. Each sub-agent receives this file as part of its brief alongside the
axis-specific guidance.

## Diff Scope Discipline

Findings target only code added or modified in the PR diff. Pre-existing code that the PR did not touch is out of scope
— do not comment on it, even if it violates a standard. If pre-existing code adjacent to the diff creates context for a
finding (e.g., the new code copies a bad pattern from a neighbor), note the pattern but frame the ask around the new
code, not the old.

When a finding spans both new and pre-existing code (e.g., a function the PR added follows a convention that
pre-existing siblings also break), state clearly which part is in-scope (the new code) and which is context-only (the
pre-existing pattern). Do not ask the PR author to fix code they did not touch — that belongs in a separate cleanup
task.

The integration branch is always green. Every PR targets the integration branch (`dev` in example repo), and CI blocks merge
unless all tests pass — so the integration branch cannot carry a pre-existing test failure. If a test fails on the PR
branch, this branch introduced it: either in the failing test directly, or via a change to a shared dependency
(fixture, import, config, sproc) the test exercises. Do not dismiss a failure as "pre-existing" or "unrelated" without
proving this branch did not cause it. (Downstream promotion branches like `stage` / `prod` are not feature-PR targets
and never enter a PR's review scope.)

## Review Priorities — Finding Sizing

Lead with actionable findings, not a summary. Tag **every** finding with the team's geological sizing analogy. The tag
encodes the finding's **severity, importance, impact, and blast radius**, and draws a sharp line: a finding either
**must be fixed** (blocks approval) or it is the **author's discretion** (does not block). Largest → smallest:

- **`[boulder]`** — critical: data loss, security vulnerability, production outage, or a broken core flow; large blast
  radius. **Must be fixed to gain approval.**
- **`[rock]`** — significant defect or standards violation: acceptance criterion not met, clear regression, unsafe
  migration, broken contract, missing required auth/authz, or a serious test gap. **Must be fixed to gain approval.**
  Use `[rock]` for correctness, contract, safety, or evidence failures where the PR is not acceptable as-is.
- **`[big-pebble]`** — important non-trivial cleanup, reviewability issue, or standards drift that does not prove a
  runtime defect by itself, but the reviewer expects it to be addressed in the current PR. Examples include oversized
  new files that are still under the hard split threshold, stale comments that could mislead future maintainers,
  speculative API surface, or repeated low-value test structure when the fix is straightforward and within scope.
  **Must be fixed to gain approval when included in a review.** Do not use `[big-pebble]` for true defects or broken
  contracts; those are `[rock]`.
- **`[pebble]`** — recommended improvement or cleanup that improves clarity: maintainability, edge-case, scope, or
  reviewability. **Optional — author's discretion.** Does not block approval and carries no obligation.
- **`[sand]`** — suggestion: style or formatting only. Does not block approval.

**Approval rule:** any open `[boulder]`, `[rock]`, or `[big-pebble]` ⇒ the PR is **not approvable** until resolved.
`[pebble]` and `[sand]` never block approval.

**When in doubt between `[rock]` and `[big-pebble]`:** ask whether the current PR would be behaviorally or contractually
wrong if the finding remained. If yes, use `[rock]`. If the PR could work but the issue is still expected to be cleaned
up before approval, use `[big-pebble]`. If the author may reasonably leave it for later, use `[pebble]`.

**Sizing spans axes — escalate when one finding hits more than one.** The tags above name a finding's worst *single*
dimension, but a defect often lives on several at once: a standards violation that is *also* a performance regression
on a hot path, or a broken contract that is *also* a correctness risk. Size by the combined blast radius, not the most
lenient lens. A useful tell: when the standards-correct fix is *also* the more efficient or simpler one — the
dimensions pull in the same direction — the finding is rarely a `[pebble]`, and is often a `[boulder]`, because it
signals the implementation *strategy* is wrong, not just a line. Findings like that need a rethink of the approach
rather than a local patch; say so explicitly and raise them early, before the structure is settled.

Only include findings with a concrete remediation path. Avoid vague comments such as "consider refactoring" unless the
current code creates a real review or maintenance problem. Prefer silence over a low-confidence `[sand]`.

## Security, Data, And Operations

Escalate findings when the diff touches:

- authentication, authorization, tenant/project boundaries, or secrets
- persistence, migrations, destructive actions, idempotency, or transactions
- external API contracts, webhooks, background jobs, queues, or scheduled work
- logging, metrics, error reporting, or operational recovery paths
- generated artifacts, schema files, lockfiles, or large fixture changes

For these areas, review both code and tests. A passing happy path is not enough.

## Output Format

Use `findings-reporting.md` as the final artifact contract. The rules below define the minimum finding section shape for
axis notes and final reports.

Use the standard code-review shape for axis-level notes and final review artifacts:

1. Findings first. Axis-level notes may order by size, but the final assembled review organizes detailed findings by
   file path and line number so all issues in a file can be addressed together.
1. Open questions or assumptions.
1. Brief summary only after findings — include an approval verdict derived from the tags (not approvable while any
   `[boulder]`, `[rock]`, or `[big-pebble]` is open).
1. Verification performed or not performed.

Emit every final-artifact finding as its own copy-paste-able Markdown subsection so a reviewer can drop it straight
into a PR comment unedited while the artifact remains readable when rendered. Do **not** wrap the whole finding in a
fenced code block. The subsection heading starts with a stable finding ID, then the size-importance tag
(`[boulder]` / `[rock]` / `[big-pebble]` / `[pebble]` / `[sand]`), line number, and one-line problem statement. The body
then lists the axis, severity, location, evidence, why, and proposed fix:

```markdown
#### F-SCQ-001 [rock] line 42 - <one-line problem statement>

- **Axis:** Standards / Code Quality
- **Severity:** rock
- **Location:** `backend/src/foo/bar.py:42`
- **Evidence:** <changed code, related source/test/standard lines, or verification command that proves the issue>
- **Why it matters:** <impact + which standard it violates; cite the index.yaml entry / standard filename>
- **Proposed fix:** <concrete suggested change>
```

Rules for finding sections:

- The heading uses a stable finding ID with an axis prefix: `F-SCQ`, `F-FS`, `F-SDO`, or `F-TQ`.
- Do not emit raw HTML anchors. Use Markdown headings and generated heading anchors for summary links.
- Do not repeat the full file path in the heading when the finding is already under a `### <file>` section; keep the
  complete path in the `Location` field.
- Keep each finding self-contained (no cross-references like "same as above") so it survives being pasted in isolation.
- One finding per section; do not bundle multiple issues under one tag.

Each finding must include: the size tag in the heading, axis, severity, a concise title, a file/line reference when
available, evidence, why it matters, and a suggested remediation. When the final review is assembled, findings MUST be
grouped by file path and sorted by line number so reviewers can work through all issues in one file before moving to the
next.

If there are no findings, say so clearly and mention residual risk or tests not run.

## Boundaries

- Do not approve or merge.
- Do not rewrite the PR unless the user asks for fixes.
- Do not block on missing context if the diff can still be reviewed honestly; state the assumption.
- Do not pad the review with style nits. Prefer silence over low-confidence criticism.
- Do not require more tests by default. Require better evidence where the current evidence does not prove the behavior.
