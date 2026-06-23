# Standards Axis — Sub-Agent Brief

Review the diff through the lens of project standards, code quality, test value, and engineering judgment. Find
correctness bugs, standards violations, test gaps, agentic slop, and redundant computation. This axis answers: **does
the code conform to how this repo expects code to be written?**

You receive this brief alongside `shared-rules.md`, which contains the diff scope discipline, finding sizing taxonomy,
output format, and boundaries. Follow both documents.

## Standards-Driven Review

Zazz methodology repos are expected to store durable review guidance in `<DOCS_ROOT>/standards/`, indexed by
`<DOCS_ROOT>/standards/index.yaml`. Use that index heavily:

- match changed files to standards by path, language, service, feature area, or activity using the index's declared
  rules
- read only the standards relevant to the diff
- cite the applicable standard when a finding depends on repo policy rather than general engineering judgment
- verify the PR did not miss a standard that should have been prescribed by the deliverable specification
- check that tests follow the repo's test patterns, fixture rules, data-safety rules, and evidence requirements
- distinguish standards violations from optional cleanup

If the standards index is missing, stale, or does not cover a changed area, state that as residual review risk instead
of inventing a policy. General review judgment still applies, but repo standards win when they are explicit.

## Testing Standards Touchpoints

The dedicated Test Quality axis owns the full verification-quality review. In this Standards / Code Quality axis, focus
on whether changed tests violate explicit repo standards, add structural slop, or duplicate code in a way that affects
maintainability. Share any broader evidence gaps with the orchestrator so the Test Quality axis can evaluate them.

The repo's testing standards — loaded via the standards index in the Standards-Driven Review step — are the
authoritative source for test patterns, anti-patterns, fixture conventions, and consolidation rules. Apply those
standards first. The checklist below supplements them with general review heuristics; when a repo standard is more
specific, the standard wins.

When the repo's backend testing standard is present (`<DOCS_ROOT>/standards/python-testing.md` in example repo), cite its
named sections rather than paraphrasing — they give mechanical, greppable review hooks the generic checklist below
cannot:

- `@pytest.mark.db` on a test that exercises only display/formatting logic and touches no real DB rows (the "reserve
  `db` for actual DB-state behavior" rule).
- A committed `@pytest.mark.skip(...)` or `pytest.skip(...)` whose reason is a missing / undeployed migration (the "no
  skip-on-missing-migration" rule). A `pytest.skip` guarding a legitimately-empty fixture table is the allowed
  carve-out, not a finding.
- An HTTP error-path test that asserts only the status code, missing the explicit no-leak assertions and the
  `_without_leaking_internals` method-name convention.
- A test that introspects implementation source via `inspect.getsource`, `ast`, or regex against module bodies instead
  of exercising behavior (the "concrete behaviors, not abstract properties" rule).

Quote the standard's section when raising any of these so the author sees it as policy, not reviewer opinion.

Look for test value, not test volume. Every test runs on every push; test count has a direct cost in CI time and
maintenance. Flag tests that add count without meaningfully increasing confidence in the PR's behavioral claims.

High-value tests usually:

- prove an AC, invariant, public contract, realistic edge case, regression, or named risk
- fail for a bug a reviewer would care about
- exercise concrete behavior through the system's public interfaces with realistic inputs
- assert observable behavior at a stable boundary
- reuse existing fixtures/helpers and mirror nearby patterns
- consolidate related scenarios with shared setup into fewer parametrized or multi-assertion tests rather than one test
  function per case
- cover realistic field boundaries such as invalid, empty, maximum/minimum, unauthorized, cross-tenant, missing-data,
  ordering, time-zone/date, idempotency, or concurrency cases when those risks apply

Low-value tests to flag:

- duplicate coverage already provided by a stronger nearby test
- mock-only tests that mostly assert a collaborator was called
- tests coupled to private helper names, incidental call order, or temporary structure
- snapshots or golden files that create churn without clear human-readable assertions
- tests that assert abstract properties — source-text introspection, type-system tautologies, framework guarantees, or
  trivially-passing conditions — rather than exercising concrete behavior
- synthetic inputs that bypass validation real callers would hit (e.g., passing None to a parameter the HTTP schema
  rejects before it reaches the service layer)
- unreasonable precondition tests, such as checking update behavior without the target record ID when the public update
  route requires that ID to address the record
- broad coverage-padding tests that do not map to a meaningful behavior or risk
- excessive layer duplication where one integration/contract test would cover the same behavior more honestly
- many single-scenario tests that share identical setup and differ only in input and expected output — consolidate into
  fewer tests with equivalent coverage

Recommended review wording:

- "This test appears redundant with `X`; keeping both adds maintenance without covering a distinct contract. Consider
  deleting this one or changing it to cover `Y`."
- "This mostly verifies mocked plumbing. A behavior-level assertion at `X` would better protect the AC and be less
  brittle."
- "The case matrix is useful, but the setup repeats the same fixture world four times; table-driving it would reduce PR
  noise while preserving coverage."
- "These N tests share identical setup and differ only in the mocked error and expected outcome. A single parametrized
  test covers the same mapping surface with less noise."

## Agentic Slop And Redundancy

When the repository contains a code-structure standard in `docs/standards/` or `<ZAZZ_DOCS_ROOT>/standards/`, load it
and treat it as the authoritative policy for file-size thresholds, contextual splitting, incrementally discoverable
skills, agentic slop, and duplicated runtime work. In example repo, that standard is `docs/standards/code-structure.md`.

If no code-structure standard exists, use this fallback rubric and state that the missing standard is residual review
risk. Flag patterns that often appear in agent-generated diffs and make the codebase worse:

- duplicated helpers, constants, fixtures, or type definitions instead of reusing local patterns
- new abstraction layers that wrap one call site without reducing complexity
- generic utility names that hide domain meaning
- defensive branches for impossible states without a caller contract or test
- comments that narrate obvious code rather than explaining a real constraint
- broad rewrites, formatting churn, or import churn unrelated to the deliverable
- parallel implementations of existing behavior
- dead compatibility paths, unused options, or speculative extension points
- error handling that catches too broadly, swallows useful context, or invents inconsistent response shapes

Be specific about why the issue matters: review noise, future maintenance, hidden bug, or divergence from established
project patterns.

## Cross-File Organization Opportunities

Look for improvements that span multiple changed files and would reduce code, simplify review, or make future changes
less error-prone. Do not force these into many duplicate findings. Record them as `Systemic Improvement Opportunities`
for the orchestrator unless the issue is required for approval in a specific file.

Good candidates:

- multiple new test files in the 400-600 line band that share natural operation boundaries;
- repeated HTTP response declarations, error mapping branches, fixtures, payload builders, or mock setup across sibling
  files;
- service functions that repeat filtering, row mapping, or exception translation logic that could be named once;
- sibling files exposing an unsettled convention that should be codified in standards rather than argued case by case.

When the opportunity would remove meaningful duplication, name the files involved, the natural extraction or split
point, and whether it is blocking (`[big-pebble]` or larger) or optional (`[pebble]`/systemic note).

## File Size And Incremental Discoverability

When a code-structure standard is present, cite its file-size and discoverability sections instead of duplicating that
policy. If no such standard exists, apply these fallback thresholds to every added or modified file under review,
including code, tests, scripts, configuration, and agent-facing markdown guides:

- More than 400 lines: raise a `[big-pebble]` when the file is new or substantially expanded by the current PR and no
  concrete cohesion exception is documented; ask the author to split the file into smaller modules or document why this
  file is intentionally cohesive. Use `[pebble]` only when the issue is clearly optional or should be handled in a
  separate cleanup PR.
- More than 600 lines: raise a `[rock]`; the PR is not approvable until the file is split or the reviewer accepts a
  concrete exception.

When raising this finding, include the measured line count and name the natural split points. The only potential
exception is a large data file used as a test harness; flag that as a `[pebble]` so reviewers still see the maintenance
and reviewability cost.

## Redundant And Duplicated Computation

When a code-structure standard is present, cite its compute-once guidance. If no such standard exists, use the fallback
rule: duplicated runtime work is separate from duplicated code. Flag one logical request that evaluates the same
expensive source more than once, especially a query, view, function, stored procedure, external API call, or large
scan/aggregation. The preferred fix is **compute once, return many**: have the single expensive operation return every
shape its consumers need, then compose those outputs.

This pattern often sizes large:

- it is frequently both a standards concern (duplicated access that bypasses an established seam) and a performance
  concern (doubled expensive work) — and a finding that spans axes escalates (see Finding Sizing); on a hot path it can
  reach `[boulder]`
- the honest remedy changes the implementation *strategy* — where the data is produced and how the contract is shaped —
  not a single line, so raise it early rather than as a late nit
- it is a predictable blind spot when work is split by layer or across branches/agents: the cheapest fix often lives on
  the *other* side of the seam the work was divided along, so neither side naturally reaches for it

When raising one, name the specific operation being repeated, where each execution happens, and the single-execution
shape that would serve every consumer — not just "this looks redundant."

### Cross-Axis Note

Redundant-computation findings that also represent a design-level strategy error — where the implementation approach
itself chose the wrong data-flow shape — may have methodology implications. When you raise such a finding, note whether
it suggests the implementation strategy diverged from what a specification's design decisions would have anticipated.
The orchestrator will surface this to the Functionality / Spec axis if relevant.
