# PR219 Review Process Improvements

## Purpose

This note captures the review-process lessons from the PR219 rerun. The goal is to make one orchestrating review agent,
working with customized Code Review Graph context, reliably find the class of issues that were only discovered after
comparing multiple independent agent outputs.

## Recommended PR-Review Skill Changes

1. Keep the four independent axes: Standards / Code Quality, Functionality / Spec, Security / Data / Ops, and Test
   Quality. The independence is valuable because it prevents one lens from suppressing another.
2. Add focused validators after the axes and before aggregation. Use them when the changed-file set touches the relevant
   surfaces:
   - Cross-Seam Contract Validator for SQL/wrapper/service/HTTP/test contracts.
   - Service Evidence Matrix Validator for changed service-layer functions and their required real-dependency evidence.
   - HTTP Contract Validator for route status, error-envelope, OpenAPI, and HTTP-test consistency.
   - Spec Reconciliation Validator for PR-body/spec claims versus implemented and documented behavior.
3. Require the aggregator to read validator packets along with axis packets and explicitly record which candidates were
   carried, merged, downgraded, or rejected.
4. Require Test Quality to produce a service evidence matrix for backend service-layer PRs.

## Recommended Code Review Graph Improvements

CRG should remain an evidence accelerator, not an automated approval engine. The useful next step is repo-aware review
projection:

- Synthetic contract edges for contracts that static imports miss, especially stored procedure -> wrapper -> service ->
  route -> test relationships.
- Policy-aware test gaps that can represent "missing DB integration evidence" separately from generic "no tests_for
  edge" rows.
- SQL result-shape extraction for configured stacks: compare stored-procedure result columns with wrapper row types and
  database tests.
- HTTP contract extraction: compare route returns/aborts, documented statuses, and HTTP tests.
- Standards applicability projection: include matched standards and triggered review checklists in `review-context`.
- Explainable advisory output: include why a row is a gap, which standard or config triggered it, and which files prove
  or refute it.

## PR219 Examples To Use As Regression Cases

- The insert service-charge sproc returned `MovementType` first while the wrapper and tSQLt table expected it sixth. A
  Cross-Seam Contract Validator or SQL result-shape extractor should flag that mismatch.
- `update_service_charge` had unit tests but no happy-path DB integration test. A Service Evidence Matrix Validator or
  policy-aware test-gap projection should flag that as distinct from generic function coverage.
- Several route-level issues required comparing actual abort/return shapes with route docs and HTTP tests. An HTTP
  Contract Validator should produce a compact status/envelope matrix for changed routes.
- A candidate finding about service-charge POST returning a composite key was rejected after checking the current PR
  body. The Spec Reconciliation Validator should make this distinction explicit so informal-spec findings do not drift
  into false positives.

## Current Skill Updates

The PR-review skill in both local worktrees now includes:

- a focused-validator pass in `SKILL.md` before aggregation;
- a Service Evidence Matrix requirement in `test-quality-axis.md`;
- CRG improvement targets in `code-review-graph.md`.

The CRG implementation still needs the actual graph/projection work described in the handoff.
