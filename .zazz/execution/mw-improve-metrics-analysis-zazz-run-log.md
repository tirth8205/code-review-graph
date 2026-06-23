# Run Log — Improve Review Metrics And Analysis Output

This is the execution log for `.zazz/specifications/mw-improve-metrics-analysis-zazz.md`.

## Current Session Baseline

- Implementation has not started.
- Active implementation slice is P1/P2/P3/P4/P5/P7 only.
- P6 sibling-template divergence remains out of scope.
- Future review-quality features in specification §2.a remain design guidance or follow-on deliverables, not current acceptance criteria.
- `AGENTS.md` declares no issue tracker for this fork. Use the specification, this run log, and the PR body as coordination surfaces.
- Approved optional external CRG validation worktree: `/Users/michael/Victory/Dev/qb-mono-wt/ss-BE-ReturnAddressWireUp`.
- The external validation worktree may be used to clean and rerun derived CRG state such as `.code-review-graph/`; do not make product implementation edits, commits, or pushes from that repo.

## Overnight Scope Expansion

- 2026-06-23 follow-up instruction expanded the implementation slice to include a repo-vendored Code Review Graph skill and external validation against `/Users/michael/Victory/Dev/qb-mono-wt/ss-BE-ReturnAddressWireUp`.
- The user clarified that the new skill should leverage standards documents rather than encode customization inline. Implementation direction: keep `.agents/skills/code-review-graph/SKILL.md` as a thin orchestration layer that requires runtime standards-index matching and source-of-truth Zazz docs.
- New skill scaffolded with the system `skill-creator` initializer under `.agents/skills/code-review-graph/`; validation passed with `python /Users/michael/.codex/skills/.system/skill-creator/scripts/quick_validate.py .agents/skills/code-review-graph`.
- User subsequently approved updating `.agents/skills/pr-review/` to leverage the new Code Review Graph skill and add multiple review axes beyond Standards/Spec. `.agents/skills/pr-review/SKILL.md` now dispatches Standards / Code Quality, Functionality / Spec, Security / Data / Ops, and Test Quality axes. New `security-axis.md` and `test-quality-axis.md` briefs were added; `code-review-graph.md` is now fallback guidance when `$code-review-graph` is unavailable. Validation passed with `python /Users/michael/.codex/skills/.system/skill-creator/scripts/quick_validate.py .agents/skills/pr-review`.

## Standards Verification

- 2026-06-23 startup: Read `.zazz/standards/index.yaml` and matched the specification §3 file list against `applies_to.paths` and `applies_to.activities`.
- Applicable standards loaded: `.zazz/standards/docs-hygiene.md`, `.zazz/standards/spec-hygiene.md`, and `.zazz/standards/pr-process.md`.
- No additional indexed standard matched the current implementation file list. The index does not currently declare `code_review_graph/` or top-level `tests/` source-code standards, so source/test edits will follow current local project patterns.
- Graph-first code exploration attempted per `AGENTS.md`: `tool_search` did not expose CRG MCP tools in this thread. Local CLI fallback with `uv run code-review-graph status --repo .` and `uv run code-review-graph detect-changes --repo . --base main --brief` was attempted with `UV_CACHE_DIR=/private/tmp/crg-uv-cache`, but dependency resolution required restricted network access and failed before graph analysis could run. Proceeding with targeted reads of specification-listed code references.

## OQ Resolutions

The following defaults are resolved in specification §10 and should not be reopened unless one becomes impossible without changing scope, public contract, acceptance criteria, or an invariant.

- **OQ-1:** `--for-review` has no default truncation when `--max-tokens` is omitted. `review-context` examples recommend `--max-tokens 2000`.
- **OQ-2:** Test-gap suppression uses structured `pyproject.toml` rules under `[tool.code-review-graph.test_gap_suppressions]`; path matching reuses `.code-review-graphignore`-style glob semantics, but `.code-review-graphignore` is not overloaded for suppression and no new suppression TOML file is added.
- **OQ-3:** Existing MCP `detect_changes_tool` / `detect_changes_func` receives compact review parameters first. CLI `review-context` is required. A separate `review_context_tool` is optional only if it is a thin wrapper over the same projection path.
- **OQ-4:** Automated verification is required. External qb-mono validation and visualization are optional manual evidence. Run them only after automated verification is green and the local worktree is available without setup churn; otherwise log them as skipped.
- 2026-06-23 startup confirmation: All §10 defaults are implementable within the current scope and public contract. No resolved OQ was reopened.

## Phase Completions

- 2026-06-23 Phase 1 complete: Added `code_review_graph/review_projection.py`; compact projection emits repo-relative paths, stable priority ordering, scoped rows, budget metadata, and savings metadata. Evidence: `uv run pytest tests/test_changes.py tests/test_cli.py tests/test_tools.py tests/test_context_savings.py tests/test_main.py -q` passed with 167 tests.
- 2026-06-23 Phase 2 complete: Wired `detect-changes --for-review --max-tokens` through CLI and MCP-facing `detect_changes_func` / `detect_changes_tool`; full JSON behavior remains backward compatible when `for_review` is false. Evidence: same scoped pytest command passed with 167 tests.
- 2026-06-23 Phase 3 complete: Added CLI `review-context --base <ref> --max-tokens N`; command composes existing `build_or_update_graph` then compact projection, reusing `changed_files` from the update result when available. Evidence: `tests/test_cli.py::TestDetectChangesCommand::test_review_context_runs_update_then_compact_projection_once` included in the green scoped pytest run.
- 2026-06-23 Phase 4 complete: Added `--scope` / `path_globs` filtering for compact changed functions, review priorities, test gaps, and affected-flow summaries. Evidence: `tests/test_changes.py::TestChanges::test_for_review_scope_filters_projected_rows_by_path_glob` and `tests/test_tools.py::TestTools::test_detect_changes_scope_returns_only_matching_section` included in the green scoped pytest run.
- 2026-06-23 Phase 5 complete: Added `code_review_graph/test_gap_config.py`; test-gap suppressions load from `pyproject.toml` and are observable through `suppressed_test_gap_count`. Evidence: `tests/test_changes.py::TestChanges::test_test_gap_suppressions_remove_boilerplate_and_report_count` included in the green scoped pytest run.
- 2026-06-23 Phase 6 complete: Added scope-honest `savings_record` and renamed savings panel to `Change-analysis token savings` with an explicit not-whole-session note. Evidence: `tests/test_context_savings.py` passed and scoped pytest command passed with 167 tests.
- 2026-06-23 Phase 7 documentation complete: Updated `docs/COMMANDS.md`, `docs/USAGE.md`, and `docs/FEATURES.md`. Evidence: `rg -n "review-context|--for-review|--max-tokens|--scope|Change-analysis token savings" docs` returned matches in all three docs.

## Deviations

- Did not dispatch the verifier sub-agent because the formal DoD is not green: required full-repo `uv run ruff check .`, required `uv run mypy code_review_graph`, and required `git diff main --stat` scope verification are blocked by baseline/unowned issues listed below. Dispatching before DoD green would violate the verifier sequencing instruction.
- Optional external qb-mono validation skipped because local required verification did not reach a green DoD state.
- Optional graph visualization validation skipped because local required verification did not reach a green DoD state.

## Manual Evidence Locations

- `.zazz/execution/crg-approach-critical-review.md`
- `.zazz/execution/crg-graph-visualization-guidance.md`
- `.zazz/execution/mw-improve-metrics-analysis-zazz-spec-review-handoff.md`

## External Validation

- 2026-06-23 ReturnAddress worktree note: `/Users/michael/Victory/Dev/qb-mono-wt/ss-BE-ReturnAddressWireUp` was inspected as a known-remediated branch. Its current head includes `eae5c603 Feedback, fixed returncode, conforming to standards`, so it is useful as regression/known-miss context but not sufficient as a fresh-finding sample.
- 2026-06-23 Summary Statement Register stack validation: ran `uv run code-review-graph review-context --repo /Users/michael/Victory/Dev/qb-mono-wt/mw-sum-stmt-register-rpt-stack --base HEAD~8 --max-tokens 2000 > /private/tmp/crg-ssr-review-context.json`. Because the branch is already merged, `dev...HEAD` is empty; `HEAD~8...HEAD` was used to preserve the merged stack change set.
- CRG compact output: 45 changed files, 76 changed symbols, 34 reported test gaps, 0 affected flows, 0.55 risk score. Savings metadata reported baseline 169,689 tokens, returned 1,971 tokens, 167,718 saved tokens, 99% change-analysis savings, with 108 rows omitted by the 2k token budget.
- Quality finding confirmed from CRG-guided focused reads: `frontend/src/components/reports/ReportRunnerPage.tsx:129-135` in the external worktree returns to `idle` on dialog cancel when `blobUrl` is false. ZIP bundle reports intentionally have `blob` but no `blobUrl` (`lines 105-107`), so after viewing a Summary Statement Register MONTH bundle, clicking Change Parameters then canceling hides the existing report instead of returning to viewing. The new bundle test covers tab switching but not cancel-from-editing with an existing bundle (`frontend/tests/unit/components/reports/ReportRunnerPage.bundle.test.tsx:124-177`). This is a fresh, user-visible Functionality/Test Quality finding found with compact CRG routing rather than broad 45-file reading.
- Graph signal calibration: CRG's top test-gap rows for `write_report_files`, `write_summary_statement_files`, `write_invoice_register_files`, and `_run_report_or_raise` were inspected and treated as triage, not automatic findings. Focused tests show the writer functions are exercised indirectly through CLI tests, demonstrating the new skill guidance to verify graph test-gap signals before filing.

## QA Findings & Rework

- Rework: Initial CLI helper patch accidentally landed inside `_handle_data_dir_option`; fixed immediately and verified with `python -m py_compile`.
- Rework: `uv run ruff check` found lint in touched files (`code_review_graph/cli.py`, `code_review_graph/review_projection.py`, import ordering in new/modified tests/helpers). Fixed touched-file lint. Evidence: `uv run ruff check code_review_graph/changes.py code_review_graph/cli.py code_review_graph/context_savings.py code_review_graph/main.py code_review_graph/review_projection.py code_review_graph/test_gap_config.py code_review_graph/tools/review.py tests/test_changes.py tests/test_cli.py tests/test_tools.py tests/test_context_savings.py` passed.
- Rework: `uv run mypy code_review_graph` found local type inference issues in `code_review_graph/changes.py`, `code_review_graph/tools/review.py`, and `code_review_graph/cli.py`; fixed those. Remaining mypy failures are outside the touched implementation paths or missing-stub baseline listed below.

## Issues & Recoveries

- Sandbox recovery: Initial `uv run` attempts failed under restricted filesystem/network access (`/Users/michael/.cache/uv` denied, then DNS errors). Recovery: ran required `uv` commands with approved escalation; `uv run pytest ... -q` installed dependencies and passed.
- 2026-06-23 verification rerun after skill updates: `uv run pytest tests/test_changes.py tests/test_cli.py tests/test_tools.py tests/test_context_savings.py tests/test_main.py -q` passed with 167 tests.
- Required full `uv run ruff check .` remains blocked by pre-existing lint outside this slice, including `scripts/diagnose_pypi_connectivity.py`, `tests/test_communities.py`, `tests/test_fts_sync.py`, `tests/test_incremental.py`, `tests/test_parser.py`, `tests/test_skills.py`, and `tests/test_transactions.py`. Touched-file ruff is clean.
- Required `uv run mypy code_review_graph` remains blocked by pre-existing package-wide type/stub baseline: missing stubs for `fastmcp`, `tiktoken`, `tree_sitter_language_pack`, `igraph`, `networkx`, `sentence_transformers`, plus existing type errors in `parser.py`, `jedi_resolver.py`, `refactor.py`, `exports.py`, `embeddings.py`, and `tools/build.py`. Touched local inference issues were fixed.
- Required `git diff main --stat` includes `AGENTS.md`, which was already modified before implementation and was not touched by this agent. Per worktree policy, it was not reverted. Product implementation edits remain under the specification's allowed code/docs/tests/.zazz areas.
- P6 absence verified again: `rg -n "sibling_diverg|nearest_sibling|construct.*diverg" code_review_graph tests` returned no matches.
- Docs verification passed: `rg -n "review-context|--for-review|--max-tokens|--scope|Change-analysis token savings" docs` returned expected matches in `docs/FEATURES.md`, `docs/COMMANDS.md`, and `docs/USAGE.md`.
- Whitespace verification passed again: `git diff --check` returned clean.

## Verifier Sub-Agent Report

- 2026-06-23 dispatched verifier sub-agent `019ef25d-048d-7853-81d2-f2f6cfdbe07c` after the user expanded scope to include the Code Review Graph skill and `pr-review` skill updates.
- Verifier result: AC1-AC8 passed; AC10 passed with caveat; skill expansion checks passed; external validation evidence was plausible.
- Verifier AC9 result: failed due to baseline blockers, not implementation-local failures. Evidence: scoped pytest passed with 167 tests and touched-file ruff passed, but required full `uv run ruff check .` fails in pre-existing unrelated files and required `uv run mypy code_review_graph` fails on package-wide missing stubs and older type errors.
- Verifier caveat: new files are untracked until PR packaging/staging, so `git diff main --stat` does not include them even though `git status --short` shows `.agents/skills/code-review-graph/`, `.agents/skills/pr-review/security-axis.md`, `.agents/skills/pr-review/test-quality-axis.md`, `.zazz/` specification/execution files, `code_review_graph/review_projection.py`, and `code_review_graph/test_gap_config.py`.
