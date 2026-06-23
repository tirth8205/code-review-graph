# Code Review Graph For Zazz PR Review

This fallback guide explains how Zazz methodology users should use
[`code-review-graph`](https://github.com/tirth8205/code-review-graph) as optional graph
context during `pr-review`.

When the repo has `.agents/skills/code-review-graph/`, prefer the `$code-review-graph`
skill instead of this file. The skill keeps graph usage standards-routed and records
token-budget evidence. Use this file only as local fallback guidance for older repos that
do not vendor that skill yet.

## Upstream Credit

`code-review-graph` is an open-source project by its upstream maintainers:

- GitHub: [github.com/tirth8205/code-review-graph](https://github.com/tirth8205/code-review-graph)
- PyPI: [pypi.org/project/code-review-graph](https://pypi.org/project/code-review-graph/)

The tool parses a codebase into a structural graph of files, functions, classes, imports,
calls, inheritance, dependencies, and test relationships. It exposes that graph through a
CLI, MCP tools, hooks, and companion skills so AI coding agents can ask targeted
review-impact questions instead of spending tokens reading broad swaths of a repository.

For Zazz, treat it as advisory review context. It can help identify blast radius,
impacted callers/dependents, affected flows, likely test gaps, and context-savings
signals. It does not replace the pinned git diff, the governing spec or issue, repo
standards, or human review judgment.

## Zazz Policy

Use `code-review-graph` as a utility behind the existing Zazz `pr-review` skill.

The purpose is practical: help the agent perform a better and more token-efficient review
on behalf of the human user when a PR is large enough that manual first-pass inspection is
inefficient. For 20, 40, or 60+ changed files, graph context should help the agent find
where to look, what may be affected, and what tests may matter before it spends tokens
reading broad file contents.

Default Zazz behavior:

- use the repo-vendored `$code-review-graph` skill for the first compact graph summary
- if that skill is unavailable, use the CLI fallback described below
- optionally configure MCP for targeted follow-up questions
- do not install upstream companion skills by default
- do not install hooks by default
- do not inject upstream graph-first instructions into repo rule files by default

This keeps the Zazz skill directory small and avoids competing review workflows. When an
upstream companion skill has a useful idea, summarize that behavior in
`.agents/skills/pr-review/code-review-graph.md` instead of copying the upstream skill into
this repo.

## Interpretation Rules

Graph output is review triage, not review truth.

Use graph output for:

- changed-symbol inventory
- caller and dependent hints
- affected-flow hints
- read-first ordering
- context-size estimates
- broad reviewability signals, such as cross-layer changes or shared-contract edits

Verify before trusting:

- stock risk scores
- test-gap counts
- affected-flow counts
- token-savings percentages
- any inferred approval or readiness signal

Token savings reported by the tool may be a self-reported estimate rather than an
independently measured review-session saving. Treat the number as directional unless the
team has measured an A/B comparison for its workflow.

Risk and test-gap numbers can be misleading when a PR is large but cohesive, touches test
harnesses, changes generated files, or crosses API/event boundaries that static call
graphs do not model.


## Review-Process Improvement Targets

PR219 exposed a useful split of responsibility between the PR-review skill and Code Review Graph:

- the skill should orchestrate independent axes plus focused validators;
- CRG should supply compact, repo-aware evidence that tells validators where to look;
- neither should treat a graph score or a passing command as review truth without source verification.

Useful CRG improvements for this workflow:

- **Synthetic contract edges:** connect files that share a contract but not a normal import edge, such as SQL stored
  procedures -> Python wrappers -> service functions -> HTTP routes -> tests.
- **Policy-aware test gaps:** report not only "symbol has no test edge," but also repo-standard gaps such as
  service-layer functions over real DB dependencies lacking happy-path DB integration evidence.
- **Result-shape extraction:** for SQL/Python stacks, extract stored-procedure result columns and compare them with
  wrapper row types and database tests when configured.
- **HTTP contract extraction:** compare route status returns/aborts, OpenAPI response declarations, and tests asserting
  those statuses and envelopes.
- **Standards applicability projection:** include matched standards and triggered review checklists in review-context
  output so focused validators start with the right policy surface.

These signals should be advisory and explainable. A validator or reviewer must still confirm findings in code, tests,
standards, and spec text before carrying them into the final artifact.

## Zazz Fork Plan

The planned Zazz direction is a fork or wrapper that keeps the graph engine and context
extraction, but replaces or overlays the scoring and reviewability model.

The fork should optimize for Zazz review questions:

- Is the PR cohesive around one feature, subsystem, or deliverable?
- Does the change reach outside its declared theme?
- Which sensitive standards govern the changed paths?
- How far does the change reach through callers, dependents, and cross-boundary contracts?
- How many review tiers are crossed, and would a stack or split improve human review?
- Do the Standards / Code Quality, Functionality / Spec, Security / Data / Ops, and
  Test Quality axes agree that risk is low, or did any axis find blocking issues?

Prefer explainable scoring over a single opaque number. A useful Zazz score should
combine:

- **Feature cohesion:** concentrated changes inside one feature/subsystem reduce risk.
- **External blast radius:** callers or dependents outside the theme increase risk.
- **Standards sensitivity:** security, auth, migration, deployment, settings, logging,
  and data-boundary standards raise review sensitivity.
- **Review findings:** open Standards or Spec boulders/rocks/big-pebbles raise risk more than file
  count does.
- **Stack span:** multiple substantial tiers increase review complexity, but this is a
  split/stack advisory signal rather than a defect signal.

Example output shape:

```text
Risk: LOW
Reason: one cohesive feature theme, no external dependents, low-sensitivity standards, Standards and Spec axes clean.
Reviewability: MEDIUM
Reason: service and UI tiers both touched; keep whole if the cross-tier contract is best reviewed end to end, otherwise consider a two-branch stack.
```

The fork should also add theme-coherence checks. Classify changed files as:

- **On theme:** part of the dominant feature or subsystem community.
- **Shared infrastructure:** outside the theme but plausibly required by the theme and
  used elsewhere.
- **Off theme:** part of another feature/subsystem with no clear dependency reason.
- **Governance-sensitive:** standards, skills, workflows, migrations, security, or
  deployment files that may require separate scrutiny.

Off-theme or governance-sensitive changes are prompts for review attention, not automatic
rejection.

Static call graphs often miss contracts that cross transport or generation boundaries. A
Zazz fork should support configurable synthetic edges for:

- HTTP routes and clients
- OpenAPI operation IDs
- generated API clients
- queues and events
- stored procedures and data wrappers
- background jobs and schedulers
- report or plugin registries

For now, use the stock tool for context and token efficiency, treat scores as advisory,
verify all findings in source, and record graph caveats in the review summary.

## When To Use It

Before `pr-review` reads broad file contents, size the pinned diff:

```bash
git diff $MERGE_BASE...HEAD --name-only | wc -l
```

Use this gate:

- `0-10` changed files: skip graph context unless the user asks for graph or blast-radius
  review.
- `11+` changed files: recommend graph context. If the tool is unavailable, the agent
  should ask whether to configure minimal CLI/MCP access, use an existing install, or
  continue without graph context.

Also consider graph context for smaller PRs that touch shared services, public APIs, data
paths, auth, migrations, test infrastructure, or files with many callers/dependents.

## Setup Tiers

Choose the smallest setup that matches the workflow.

### Tier 1: Review-Only CLI/MCP

Recommended default for Zazz PR review:

```bash
uvx code-review-graph --version
uvx code-review-graph install --no-skills --no-hooks --no-instructions
uvx code-review-graph build
```

This uses `uvx` on demand, configures MCP access, builds the graph, and skips upstream
skills, hooks, git hooks, and graph-first instruction injection. It is enough for the
Zazz `pr-review` skill to use CLI output and optional MCP follow-up.

Use a targeted platform only when needed:

```bash
uvx code-review-graph install --platform <name> --no-skills --no-hooks --no-instructions
```

Supported platform keys in the checked version are `codex`, `claude`, `claude-code`,
`cursor`, `windsurf`, `zed`, `continue`, `opencode`, `antigravity`, `gemini-cli`, `qwen`,
`kiro`, `qoder`, `copilot`, `copilot-cli`, and `all`. `warp` and `pi` were not listed by
`code-review-graph install --help` in the checked version; use the CLI directly for those
tools unless upstream adds platform support.

### Tier 2: Persistent CLI

Use this when a user wants a permanent `code-review-graph` command on `PATH`, or when
they are considering hooks:

```bash
uv tool install code-review-graph
code-review-graph --version
code-review-graph install --no-skills --no-hooks --no-instructions
code-review-graph build
```

`uv tool install` is preferred for an isolated persistent command. `pipx install
code-review-graph` is the next best persistent option. `pip install code-review-graph`
should be used only inside an intentional Python environment.

### Tier 3: Hook Automation

Hooks can keep the graph fresh during repeated work on large branches, but they are not
part of default Zazz PR-review setup.

Important upstream detail: generated hook scripts call `code-review-graph` directly, not
`uvx code-review-graph`. If you want hooks, use a persistent install first:

```bash
uv tool install code-review-graph
code-review-graph install --platform <name> --no-skills --no-instructions
code-review-graph build
```

Use `--no-skills --no-instructions` so the team gets freshness automation without adding
upstream skills or graph-first rule text. Target one platform; avoid `--platform all`
unless the user explicitly wants every detected/supported tool configured.

Preview hook-enabled setup before writing files:

```bash
code-review-graph install --platform <name> --dry-run --no-skills --no-instructions
```

Observed upstream hook behavior:

- Claude/Qoder hooks update the graph after edit/write/bash activity and check status on
  session start.
- Codex hooks add similar update/status behavior in `~/.codex/hooks.json`.
- Cursor hooks are user-level scripts for after-file-edit, session-start, and pre-commit
  shell commands.
- Gemini CLI hooks are workspace-level scripts under `.gemini/`.
- OpenCode installs a user-level plugin that updates on file edits and reports
  pre-commit analysis.
- A repo git pre-commit hook may be added for Codex, Claude, and Qoder hook setup.

Because hooks may touch both user-level and repo-level files, enable them only with
explicit user consent.

### Tier 4: Full Upstream Integration

Full integration is opt-in and usually not needed for Zazz methodology repos:

```bash
uv tool install code-review-graph
code-review-graph install --platform <name>
code-review-graph build
```

This may install upstream companion skills, hooks, git hooks, MCP config, and graph-first
instruction text. Use it only when the team explicitly wants `code-review-graph` to be a
proactive development-environment feature rather than an optional Zazz review utility.

## CLI And MCP

`code-review-graph` provides both a CLI and MCP tools.

For Zazz `pr-review`, prefer the CLI for the first graph pass:

```bash
uvx code-review-graph detect-changes --brief
uvx code-review-graph update --brief
```

The CLI is token-efficient because it is one auditable command, produces compact output,
works before MCP tools appear after setup, and is easy to record as review evidence.

Use MCP for targeted follow-up when available:

- `get_minimal_context_tool(task="review changes")`
- `detect_changes_tool(detail_level="minimal")`
- `get_review_context_tool(base="<fixed-point>")`
- `get_impact_radius_tool(base="<fixed-point>")`
- `get_affected_flows_tool(...)`
- `query_graph_tool(pattern="callers_of", target="<function-or-class>")`
- `query_graph_tool(pattern="tests_for", target="<function-or-class>")`

Recommended review flow:

1. Pin the comparison base and count changed files.
2. For `11+` changed files, load `.agents/skills/pr-review/code-review-graph.md`.
3. Prefer CLI output for the first compact graph summary.
4. Use MCP for targeted follow-up if tools are visible.
5. Pass a concise graph summary to both Standards and Spec review axes.
6. For draft PR cleanup, include graph-informed risks and verification in the cleanup
   checklist.

If MCP tools do not appear after setup, restart the AI tool. For the current review, use
CLI output and continue.

## Daily Commands

Run these from the repository being reviewed:

```bash
# Initial graph
uvx code-review-graph build

# Graph stats
uvx code-review-graph status

# Refresh graph and show compact risk panel
uvx code-review-graph update --brief

# Read-only risk panel using existing graph
uvx code-review-graph detect-changes --brief

# Optional watch mode
uvx code-review-graph watch

# Optional visualization
uvx code-review-graph visualize
```

Use `detect-changes --brief` when watch mode or hooks keep the graph fresh. Use
`update --brief` after a rebase, a large change set, or when the graph may be stale.

For persistent installs, remove the `uvx` prefix.

## Updates

Check the current version:

```bash
uvx code-review-graph --version
code-review-graph --version
```

Refresh `uvx` package resolution:

```bash
uvx --refresh code-review-graph --version
```

Upgrade persistent installs:

```bash
uv tool upgrade code-review-graph
pipx upgrade code-review-graph
pip install --upgrade code-review-graph
```

After upgrading, rerun the minimal install command if MCP configuration may have changed:

```bash
uvx code-review-graph install --no-skills --no-hooks --no-instructions
```

Only rerun hook-enabled or full integration commands if the team intentionally uses those
extras.

## Local Checkout

Use the local checkout only when developing or testing `code-review-graph` itself:

```bash
cd /path/to/code-review-graph
uv sync
uv run code-review-graph --help
uv run code-review-graph install --no-skills --no-hooks --no-instructions
```

For normal Zazz PR review, prefer the PyPI package through `uvx`.

## Ignore, Privacy, And Troubleshooting

The tool already ignores many common paths, including `.git`, `.code-review-graph`,
`node_modules`, virtualenvs, build directories, lockfiles, and SQLite files. Add
repo-specific ignores with `.code-review-graphignore`:

```gitignore
generated/**
vendor/**
*.generated.ts
```

Core graph and review workflows are local. The graph database lives under:

```text
.code-review-graph/graph.db
```

Optional semantic embeddings may send source snippets to the configured embedding
provider. Use embeddings only when explicitly desired and allowed by the repo's data
policy.

Quick checks:

```bash
uvx code-review-graph --version
uv tool list
pipx list
python -m pip show code-review-graph
uvx code-review-graph update --brief
uvx code-review-graph build
```

If MCP tools do not appear, rerun the minimal install command, restart the AI tool, and
use CLI output for the current review.
