# Test Quality Axis — Sub-Agent Brief

Review the diff through the lens of verification quality. This axis answers: **do the tests and checks prove the
changed behavior and realistic risks without adding low-value noise?**

You receive this brief alongside `shared-rules.md`, which contains diff scope discipline, finding sizing, output
format, and boundaries. Follow both documents.

## Standards First

Use matched testing standards from `<DOCS_ROOT>/standards/index.yaml` as the source of truth. Apply repo-specific
fixture, mocking, naming, database, HTTP, frontend, and migration-test guidance before generic heuristics.

When no testing standard covers the changed area, use this brief as fallback guidance and state the standards gap as
residual risk.

## Required Evidence

Check that verification proves:

- each acceptance criterion or stated behavior changed by the PR
- realistic edge cases at public boundaries, such as invalid input, missing data, auth/authz failure, ordering,
  idempotency, duplicate rows, time/date boundaries, external failures, and persistence errors when relevant
- regression behavior for a bug fix
- integration contracts when the diff crosses API, data, generated-client, report, background-job, or UI/service seams
- negative and failure paths when the user would see different behavior or data could be corrupted

Passing tests are evidence only when the assertions cover observable behavior. A command that passes without proving the
risk remains residual risk, not automatic approval.


## Service Evidence Matrix

For backend service-layer PRs, build an explicit evidence matrix before declaring Test Quality clean. At minimum include:

```text
changed service function | real dependency called | unit evidence | DB/integration evidence | HTTP/API evidence | gap
```

Apply the repo's testing standard to each row. In repos with a rule like "unit-only coverage for a service-layer
function is insufficient," a changed service function that wraps a real stored procedure or persistence dependency
needs at least one happy-path test through the real dependency unless the standard gives a narrower exception.

The matrix should distinguish these cases:

- no test at all;
- mock-only unit coverage;
- integration coverage for a sibling function but not the changed function;
- integration coverage that exercises the dependency but does not assert the changed observable behavior;
- public-boundary coverage that patches the service and therefore cannot prove the real seam.

Use the matrix to raise the smallest useful finding. Do not ask for broad coverage by default; ask for the missing row
or assertion that would prove the changed behavior.

## Cross-Seam Contract Verification

When the PR changes stored procedures, data-layer wrappers, generated schemas, OpenAPI docs, or service functions that
compose those layers, verify at least one test or check crosses the seam that can drift. Do not assume each layer is
correct because it looks locally consistent.

For SQL Server stored procedure work, explicitly compare:

- the SQL result-set column names and order;
- the Python wrapper `TypedDict`, return-row tuple, or column validation contract;
- tSQLt `#actualResults` tables or expected result sets;
- service-layer behavior that consumes or discards the wrapper result.

If a changed service function calls a real stored procedure, look for a happy-path integration test that exercises that
function against the real DB unless the repo standards define a narrower exception. Mock-only service tests are not
enough for sproc signature, return-code, or result-column drift.

When practical and safe, run the focused database or tSQLt suite for new or modified sprocs. If it cannot be run, record
that as residual risk and inspect the SQL/wrapper/test contract manually.

## Low-Value Test Signals

Flag tests when they materially harm maintainability or misrepresent confidence:

- source-text, AST, regex, snapshot, or private-helper checks that should be behavior-level tests
- mock-only tests that mostly assert collaborators were called
- tests that duplicate stronger nearby coverage without a distinct behavior or risk
- brittle assertions on incidental order, internal call counts, temporary markup, or implementation structure
- unrealistic inputs that public callers cannot send because schemas/routes/clients reject them first
- broad fixture worlds where a smaller table-driven setup proves the same behavior
- many single-case tests with identical setup that should be parameterized or consolidated
- committed skips, xfails, or environment guards that hide missing migrations, broken fixtures, or incomplete setup

Do not ask for more tests by default. Ask for better evidence where current evidence is missing, weak, duplicated, or
misaligned with the risk.

## CRG Signals

When Code Review Graph context is available, use changed test files, test-gap rows, affected flows, and impacted
callers/dependents to decide where evidence should exist. Treat test-gap suppressions as valid only when they are
observable in the CRG output and scoped to boilerplate or intentionally untestable surfaces.

CRG can point to likely missing evidence; it does not prove the absence of bugs. Verify every finding in the diff,
source, tests, and standards.

## Output

For each finding, name the unproven behavior or low-value test, cite the changed file and line, and state the smallest
useful remediation: add a behavior-level assertion, consolidate redundant tests, use an existing fixture/helper, remove
a brittle test, or run a missing command.

If the tests are sufficient, say so clearly and list commands or evidence reviewed.
