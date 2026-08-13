---
name: code-review-graph
description: Use the local code-review-graph knowledge graph for compact repository exploration, architecture questions, code review, debugging, dependency tracing, impact analysis, and refactoring safety checks. Use when the user mentions CRG/code-review-graph, asks to use its graph, or an existing graph can materially narrow repository work; do not invoke it repeatedly for an ordinary repository with no graph and no graph request.
---

# Code Review Graph

Use CRG as a local structural index, not as a replacement for source inspection. The workflow is safe by default: read-only inspection may use an existing graph, while graph creation or maintenance always requires explicit user intent.

## Decide whether to invoke CRG

- Invoke this skill when the user names CRG/code-review-graph, asks for graph-backed exploration/review/debugging/impact analysis, or the repository already contains a usable CRG graph and graph context would reduce broad scanning.
- For an ordinary repository with no graph and no request to use or build CRG, do not call the CLI or MCP merely to keep checking. A single first-task health check is allowed only after this skill has been triggered by the conditions above.
- “Use the graph” and “understand this code” do not authorize building a missing graph. Ask before any mutating CRG command.

## Entry workflow

1. Resolve the active repository root from the workspace or `git rev-parse --show-toplevel`. Never embed the checkout used during installation.
2. Inspect the current tool inventory for CRG MCP tools. Prefer MCP when its tools are actually exposed.
3. On the first CRG task for a repository, perform exactly one read-only health check. With MCP, use `get_minimal_context_tool(task=<short task>, repo_root=<root>)` or the available graph-stats tool. Without MCP, run the bundled helper’s `status --json` command. Reuse the result; do not poll status on every turn.
4. If the graph is healthy and fresh, use the smallest relevant MCP query or CLI read-only query to narrow the work. If an MCP call fails, retry at most once, then use the CLI helper.
5. If the graph is missing, empty, or stale, report that once and stop using CRG for this task unless the user explicitly authorizes maintenance. Continue with the smallest useful source/test inspection.

## Health and mutation boundary

- A graph is not ready when its database is missing, `nodes` or `files` is zero, or `last_updated` is null. It is stale when the built commit does not match the current repository commit.
- Never run `build`, `update`, `postprocess`, `embed`, or `watch` unless the user explicitly asks to create/maintain the graph or has already granted that authority for this task. Verify status once after an authorized mutation.
- Never launch `serve` as a one-shot query or implicitly use `uvx`; `serve` is a long-running MCP process. Use the installed CLI executable for fallback queries.
- Keep all CRG operations local. Do not send credentials, private keys, or unrelated source to external services.
- Graph results only select a smaller reading scope. Read the actual implementation and relevant tests before making or reporting behavioral conclusions; if graph and source disagree, trust source.

## Route by task

- Explore/architecture: start with minimal context or `architecture --detail-level minimal`, then search symbols and trace only relevant callers, callees, imports, tests, communities, or flows.
- Review changes: use change detection, affected flows, impact radius, and `tests_for`; request source snippets only for changed/high-risk areas.
- Debug: search the suspected symbols/terms, trace callers and callees, inspect one relevant flow, and verify hypotheses against source, logs, and tests.
- Refactor/rename: preview impact and tests first; never apply a graph-backed refactor without the user’s explicit edit request.

## CLI fallback

Use the bundled read-only wrapper (replace `<skill-root>` with this skill’s directory):

```bash
python3 <skill-root>/scripts/crg_readonly.py status --repo "<repo-root>"
python3 <skill-root>/scripts/crg_readonly.py architecture --repo "<repo-root>"
python3 <skill-root>/scripts/crg_readonly.py search "<query>" --repo "<repo-root>"
python3 <skill-root>/scripts/crg_readonly.py query callers_of "<symbol>" --repo "<repo-root>"
python3 <skill-root>/scripts/crg_readonly.py impact --repo "<repo-root>"
python3 <skill-root>/scripts/crg_readonly.py detect-changes --brief --repo "<repo-root>"
```

The wrapper resolves/validates the repository, passes arguments without shell interpolation, and exposes only read-only commands. Set `CRG_BIN` only when the installed executable has a non-standard name/path. Preserve and report its errors; do not turn a failed fallback into permission to build.

## WSL and Windows scope

Codex processes in WSL and Windows have separate homes and configuration/tool inventories. Resolve the `CODEX_HOME` of the runtime that is actually running the task, and restart or refresh Codex after installing a skill or MCP entry. A Windows-mounted path such as `/mnt/e/...` is not by itself a failure: verify that the selected runtime can execute the CLI and access that path. Do not assume a WSL MCP registration is visible to a Windows Codex session, or vice versa.

## Output discipline

State whether evidence came from CRG MCP or the CLI fallback, include the repository root and graph readiness/freshness when material, and keep graph-derived relationships concise. Do not claim CRG was used if its tools and fallback both failed.
