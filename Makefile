SHELL := /bin/sh

.PHONY: setup gen upg lint fix types mypy mod test test-release lint-fork-workflows

setup:
	uv sync --locked --extra dev

gen:
	uv run --locked --extra dev python scripts/generate_documentary_skills.py
	uv run --locked --extra dev python -c 'from pathlib import Path; from code_review_graph.skills import generate_skills; generate_skills(Path.cwd(), Path("skills"))'

upg:
	uv lock --upgrade

lint:
	uv run --locked --extra dev ruff check code_review_graph tests

fix:
	uv run --locked --extra dev ruff check --fix code_review_graph tests

types:
	uv run --locked --extra dev pyrefly check code_review_graph

mypy:
	uv run --locked --extra dev mypy code_review_graph --ignore-missing-imports --no-strict-optional

mod:
	flext-infra refactor mod --repository-root .

test:
	uv run --locked --extra dev pytest tests

lint-fork-workflows:
	mise exec actionlint@1.7.12 -- actionlint .github/workflows/sync-upstream.yml .github/workflows/release-assets.yml

test-release:
	uv run --locked --extra dev pytest tests/test_release_provenance.py tests/test_documentation.py -q --tb=short

.PHONY: test-notebooks
test-notebooks:
	uv run --locked --extra dev pytest tests/test_notebook.py -q --tb=short

.PHONY: test-skills
test-skills:
	uv run --locked --extra dev pytest tests/test_skill_template_schema_sync.py tests/test_skills.py -q --tb=short

.PHONY: test-daemon
test-daemon:
	uv run --locked --extra dev pytest tests/test_daemon.py tests/test_daemon_external_graph.py tests/test_prune.py -q --tb=short

.PHONY: test-worktrees
test-worktrees:
	uv run --locked --extra dev pytest tests/test_multi_worktree.py tests/test_repo_root_identity.py -q --tb=short

.PHONY: test-watch-locks
test-watch-locks:
	uv run --locked --extra dev pytest tests/test_write_lock.py tests/test_write_lock_all_writers.py tests/test_watch_debouncer_wakeup.py -q --tb=short

.PHONY: test-freshness
test-freshness:
	uv run --locked --extra dev pytest tests/test_freshness_metadata.py tests/test_tools.py -q --tb=short

.PHONY: test-install
test-install:
	uv run --locked --extra dev pytest tests/test_cli_install.py tests/test_steering_hook.py tests/test_installer_ownership.py tests/test_install_outputs_are_ignored.py -q --tb=short

.PHONY: test-read-guard
test-read-guard:
	uv run --locked --extra dev pytest tests/test_not_built_guard.py tests/test_uncertainty.py -q --tb=short

.PHONY: test-review-budget
test-review-budget:
	uv run --locked --extra dev pytest tests/test_complete_change_analysis.py -q --tb=short

.PHONY: test-update-contract
test-update-contract:
	uv run --locked --extra dev pytest tests/test_integration_git.py tests/test_update_in_a_graph_only_project.py tests/test_hook_update.py tests/test_codex_hook_never_builds_a_new_graph.py -q --tb=short

.PHONY: test-registry
test-registry:
	uv run --locked --extra dev pytest tests/test_registry.py tests/test_unread_tables_removed.py tests/test_dependency_floors.py tests/test_cli_reconciliation.py tests/test_install_outputs_are_ignored.py -q --tb=short

.PHONY: test-mcp-client
test-mcp-client:
	uv run --locked --extra dev pytest tests/test_e2e_mcp_client.py -q --tb=short

.PHONY: test-change-discovery
test-change-discovery:
	uv run --locked --extra dev pytest tests/test_changes.py tests/test_agent_transparency.py -q --tb=short

.PHONY: test-review-entrypoints
test-review-entrypoints:
	uv run --locked --extra dev pytest tests/test_action_render.py tests/test_churn_runtime.py tests/test_cli_help.py tests/test_query_path_to.py -q --tb=short

.PHONY: test-watcher-lifecycle
test-watcher-lifecycle:
	uv run --locked --extra dev pytest tests/test_mcp_watch_store_shutdown.py tests/test_watch_set.py tests/test_windows_path_identity.py tests/test_edit_hook_follows_the_edited_file.py -q --tb=short

.PHONY: test-unmatched-risk
test-unmatched-risk:
	uv run --locked --extra dev pytest tests/test_unmatched_risk.py -q --tb=short

.PHONY: test-review-analysis
test-review-analysis:
	uv run --locked --extra dev pytest tests/test_generic_yaml_edge_cases.py tests/test_pure_deletion_review.py tests/test_token_budget.py -q --tb=short

.PHONY: test-embedding-notices
test-embedding-notices:
	uv run --locked --extra dev pytest tests/test_embedding_refresh_skip_notice.py tests/test_yaml_locale_files.py -q --tb=short

.PHONY: test-baseline-compat
test-baseline-compat:
	uv run --locked --extra dev pytest tests/test_readme_parity.py tests/test_released_shapes.py tests/test_numeric_reports.py tests/test_registered_data_dir_recovery.py -q --tb=short

.PHONY: test-parser-process-locks
test-parser-process-locks:
	uv run --locked --extra dev pytest tests/test_concurrency_and_daemon.py -m concurrency -k TestSingleFlightWriteLock -q --tb=short

.PHONY: test-parser-reuse
test-parser-reuse:
	uv run --locked --extra dev pytest tests/test_parser.py -q --tb=short

.PHONY: test-vscode
test-vscode:
	npm --prefix code-review-graph-vscode run compile
	npm --prefix code-review-graph-vscode run lint
	uv run --locked --extra dev env CRG_TEST_CLI="$(CURDIR)/.venv/bin/code-review-graph" npm --prefix code-review-graph-vscode test

.PHONY: test-upgrade-path
test-upgrade-path:
	CRG_UPGRADE_TEST=1 uv run --locked --extra dev pytest -m upgrade -q -rsxX
