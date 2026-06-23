# PR219 Improvements Handoff

## Objective

Implement Code Review Graph and PR-review workflow improvements that make the PR219 misses reproducible test cases for
future review automation. The target worktree is:

```text
/Users/michael/Dev/zazzcode/code-review-graph/mw-improve-metrics-analyis-zazz
```

## Current State

The PR-review skill has been updated in both local CRG worktrees:

- `/Users/michael/Dev/zazzcode/code-review-graph/mw-improve-metrics-analyis-zazz/.agents/skills/pr-review/SKILL.md`
- `/Users/michael/Dev/zazzcode/code-review-graph/mw-improve-metrics-analyis-zazz/.agents/skills/pr-review/test-quality-axis.md`
- `/Users/michael/Dev/zazzcode/code-review-graph/mw-improve-metrics-analyis-zazz/.agents/skills/pr-review/code-review-graph.md`
- matching files under `/Users/michael/Dev/zazzcode/code-review-graph/main/.agents/skills/pr-review/`

The new skill guidance adds focused validators, a backend service evidence matrix, and CRG review-process improvement
targets. The implementation work below should make CRG provide better inputs to those validators.

## Desired Improvements

### 1. Synthetic Contract Edges

Add configurable synthetic edges for contracts that are not normal import/call relationships. For the QualityBank repo
case, useful edges are:

```text
SQL stored procedure file -> Python data wrapper -> service function -> HTTP route -> tests
```

Implementation sketch:

- Add a config section, likely under `[tool.code-review-graph.synthetic_edges]`, that declares matchers by naming
  convention and path glob.
- Start with a generic rule shape rather than hard-coding QualityBank paths.
- Emit synthetic edges into review projection output even if they are not persisted in the core graph at first.
- Include edge kind names that validators can use, such as `CONTRACT_WRAPPER`, `SERVICE_USES_WRAPPER`,
  `HTTP_USES_SERVICE`, and `TEST_EXERCISES_CONTRACT`.

### 2. Policy-Aware Test Gaps

The current CRG test-gap signal is mostly symbol/test-edge based. Add a second class of gaps that comes from repo policy
or config. The first target should be backend service functions over real DB dependencies that lack happy-path DB
integration evidence.

Implementation sketch:

- Add config for policy rules, for example `[tool.code-review-graph.review_policies.service_integration]`.
- Detect changed service functions and their real dependencies from imports/calls and synthetic edges.
- Detect DB/integration tests by marker/name/path patterns from config, not hard-coded pytest assumptions only.
- Emit rows with a clear reason, e.g. `changed service function calls qb2_Update... but no @pytest.mark.db happy-path
  test exercises update_service_charge`.
- Keep generic `test_gaps` for backward compatibility; add a richer `policy_gaps` or `review_gaps` collection.

### 3. SQL Result-Shape Extraction

Add an optional extractor that can compare stored-procedure result columns with wrapper row types and database tests.
This can start as a repo-configured heuristic rather than a full SQL parser.

Implementation sketch:

- For SQL files matching configured globs, extract the success-path `select` aliases or column names for changed sprocs.
- For Python wrappers, extract `TypedDict` annotation order for configured wrapper files.
- For tSQLt tests, extract `#actualResults` table column order when present.
- Emit a `contract_shape_mismatches` list in review context with SQL columns, wrapper columns, test columns, and first
  differing index.
- Regression target: PR219 service-charge insert sproc should report `MovementType` at index 1 in SQL versus index 6 in
  wrapper/test.

### 4. HTTP Contract Extraction

Add a compact changed-route matrix for route files:

```text
route | returns/aborts | documented responses | tests asserting statuses | envelope notes
```

Implementation sketch:

- Extract `HTTPStatus.*` from return statements, `Response(status=...)`, and `apiflask.abort(...)`.
- Extract `@bp.doc(responses=...)` status keys.
- Use configured test path/name conventions to find nearby route tests and statuses asserted.
- Emit mismatches and missing tests as advisory rows, not final findings.

### 5. Standards Applicability Projection

`review-context` should include the standards that match changed paths and review activities when the repo has a Zazz
standards index. This lets validators start with policy instead of rediscovering it.

Implementation sketch:

- Add optional discovery of `.zazz/standards/index.yaml`, `docs/standards/index.yaml`, or user-provided docs root.
- Match changed files and configured activities to standards entries.
- Return a compact `matched_standards` list: file, reason, matched paths/activities, and review prompts.
- Do not inline entire standards files unless explicitly requested; provide paths and short purpose text.

## Regression Fixtures / Acceptance Checks

Use PR219 as the conceptual regression suite. It is fine to encode small synthetic fixtures instead of depending on the
private QualityBank repo.

Minimum tests:

1. A SQL fixture whose result column order differs from a Python wrapper `TypedDict` and tSQLt temp table must produce a
   contract-shape mismatch.
2. A changed service function with only mock-style tests and no configured DB integration test must produce a policy gap.
3. A changed route fixture with returned status not present in `@bp.doc(responses=...)` must produce an HTTP contract
   mismatch.
4. A lightweight-spec text fixture that is ambiguous should not become a hard finding without source corroboration; CRG
   should only return advisory reconciliation evidence.
5. Existing `review-context` callers must continue to receive `test_gaps` and `review_priorities`; new richer fields
   must be additive or gated by a detail level/config option.

## Copy-Paste Execution Block

Run this from the feature worktree after implementation changes:

```bash
cd /Users/michael/Dev/zazzcode/code-review-graph/mw-improve-metrics-analyis-zazz
uv run pytest tests/test_changes.py tests/test_cli.py tests/test_tools.py -q
uv run pytest tests/test_context_savings.py tests/test_action_render.py -q
uv run pytest -q
uv run code-review-graph review-context --repo . --base main --max-tokens 4000 > /tmp/crg-review-context-check.json
python - <<'PY'
import json
from pathlib import Path
payload = json.loads(Path('/tmp/crg-review-context-check.json').read_text())
print('status=', payload.get('status'))
print('changed_file_count=', payload.get('changed_file_count'))
print('keys=', sorted(payload.keys()))
PY
```

## Files Likely To Change

- `code_review_graph/changes.py`
- `code_review_graph/review_projection.py`
- `code_review_graph/tools/review.py`
- `code_review_graph/cli.py`
- new module(s) for synthetic contract extraction, for example `code_review_graph/review_contracts.py`
- tests under `tests/test_changes.py`, `tests/test_cli.py`, `tests/test_tools.py`, and new focused tests if the new
  module gets large
- docs under `.zazz/execution/` or project docs if user-facing command output changes

## Important Constraints

- Keep CRG output advisory. It should identify likely gaps and mismatches, not approve or reject PRs.
- Keep new fields additive unless intentionally versioning the output contract.
- Prefer configurable matchers over QualityBank-only hard-coding.
- Keep token budgets respected by `review_projection.project_for_review`; truncate new lists predictably like existing
  `review_priorities`, `changed_functions`, `test_gaps`, and `affected_flows`.
- Preserve existing local uncommitted work. This worktree already has many modified files and new skill/doc files.
