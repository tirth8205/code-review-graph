# Agent Instructions

## Zazz Methodology

Methodology docs root: `.zazz`

This fork is being developed as a Zazz methodology repository. Use `AGENTS.md` as the
entry point, then load only the methodology docs, standards, features, proposals, or
specifications that are relevant to the task at hand.

Authoritative standards index:

- `.zazz/standards/index.yaml`

Required behavior:

1. Read `.zazz/standards/index.yaml` before creating, modifying, reviewing, or validating
   code when standards may apply.
2. Match standards by `applies_to.paths` and `applies_to.activities`.
3. Load only the relevant standards and companion documents.
4. Prefer an applicable standard over incidental legacy patterns.
5. If no applicable standard exists, prefer the most recent intentional project pattern.

Feature context:

- Feature index: `.zazz/features/index.yaml`
- Proposals: `.zazz/proposals/`
- Architecture docs: `.zazz/architecture/`
- Deliverable specifications: `.zazz/specifications/`
- Execution records and handoff notes: `.zazz/execution/`

Use proposals for exploratory direction, feature docs for durable product capability
context, and specifications for approved bounded implementation work. The initial
token-efficiency and review-axis proposal lives under `.zazz/proposals/`.

## Zazz Skills

This repo vendors the applicable Zazz workflow skills under `.agents/skills/`:

- `proposal-builder`
- `feature-doc-builder`
- `architecture-doc-builder`
- `spec-builder`
- `standard-builder`
- `conformance`
- `qa-testing`
- `doc-check`
- `pr-builder`
- `pr-review`
- `gh-stack`
- `worktree`

Tool-specific helper skills such as Jira, PostgreSQL, SQL Server, and Zazz Board API are
not vendored unless this repo later declares those systems.

## Worktree Policy

Use the Zazz bare-repo plus sibling-worktree layout:

```text
code-review-graph/
├── .bare/
├── main/
└── <feature-or-deliverable-worktree>/
```

Rules:

- `main/` is the integration worktree for `main`.
- The container directory is not an active checkout.
- Do implementation work in feature, proposal, docs, or deliverable worktrees.
- Use flat branch names that can also be sibling directory names.
- Prefer Worktrunk for routine worktree operations:
  - from the container, `wt -C .bare list`
  - from the container, `wt -C main switch --create <flat-branch-name>`
  - from the container, `wt -C main switch <branch-or-pr>`

The checked-in Worktrunk project config lives at `main/.config/wt.toml`. Invoking
Worktrunk with `-C main` loads that shared config while still managing the same bare
worktree set. `wt -C .bare list` is the cleanest way to list worktrees because it omits
the integration worktree's duplicate bare-repo row.

## Shared-File Coordination

No external locking service is declared for this fork. Use isolated worktrees for parallel
work and serialize overlapping-file edits when worktrees alone do not prevent conflicts.

## Agent Execution Discipline

Default execution guidance lives in `.zazz/agent-execution-discipline.md`.

Repo-specific defaults:

- Integration branch: `main`
- Durable docs root: `.zazz`
- Worktree setup reference: `.zazz/worktree-setup.md`
- Worktrunk cheat sheet: `.zazz/docs/wt-cheat-sheet.md`
- Human review remains the merge authority; agents should not merge PRs.
- No issue tracker is declared for this fork. Use the active deliverable
  specification, execution run log, and PR body as the coordination surfaces.
  Do not create or update issue-tracker records unless a future repo instruction
  declares one explicitly.

## Non-Interactive Shell Commands

**ALWAYS use non-interactive flags** with file operations to avoid hanging on confirmation prompts.

Shell commands like `cp`, `mv`, and `rm` may be aliased to include `-i` (interactive) mode on some systems, causing the agent to hang indefinitely waiting for y/n input.

**Use these forms instead:**
```bash
# Force overwrite without prompting
cp -f source dest           # NOT: cp source dest
mv -f source dest           # NOT: mv source dest
rm -f file                  # NOT: rm file

# For recursive operations
rm -rf directory            # NOT: rm -r directory
cp -rf source dest          # NOT: cp -r source dest
```

**Other commands that may prompt:**
- `scp` - use `-o BatchMode=yes` for non-interactive
- `ssh` - use `-o BatchMode=yes` to fail instead of prompting
- `apt-get` - use `-y` flag
- `brew` - use `HOMEBREW_NO_AUTO_UPDATE=1` env var

<!-- code-review-graph MCP tools -->
## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the
code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore
the codebase.** The graph is faster, cheaper (fewer tokens), and gives
you structural context (callers, dependents, test coverage) that file
scanning cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes_tool` or `query_graph_tool` instead of Grep
- **Understanding impact**: `get_impact_radius_tool` instead of manually tracing imports
- **Code review**: `detect_changes_tool` + `get_review_context_tool` instead of reading entire files
- **Finding relationships**: `query_graph_tool` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview_tool` + `list_communities_tool`

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

### Key Tools

| Tool | Use when |
|------|----------|
| `detect_changes_tool` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context_tool` | Need source snippets for review — token-efficient |
| `get_impact_radius_tool` | Understanding blast radius of a change |
| `get_affected_flows_tool` | Finding which execution paths are impacted |
| `query_graph_tool` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes_tool` | Finding functions/classes by name or keyword |
| `get_architecture_overview_tool` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes_tool` for code review.
3. Use `get_affected_flows_tool` to understand impact.
4. Use `query_graph_tool` pattern="tests_for" to check coverage.
