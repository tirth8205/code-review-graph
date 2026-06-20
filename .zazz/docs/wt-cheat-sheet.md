# Worktrunk Cheat Sheet

This repo uses the Zazz bare-repo plus sibling-worktree layout:

```text
code-review-graph/
├── .bare/
├── main/
└── <branch-worktree>/
```

`main/` is the integration worktree. Do normal feature, proposal, review, and
deliverable work in sibling worktrees.

## Daily Commands

Run these from the container directory:

```bash
cd /Users/michael/Dev/zazzcode/code-review-graph
wt -C .bare list
wt -C main switch --create <flat-branch-name>
wt -C main switch <existing-branch>
wt -C main switch pr:<number>
wt -C main remove <branch-name>
```

Use `wt -C .bare list` for the cleanest list output. Use `wt -C main switch ...` for
create/switch/review flows so Worktrunk loads `main/.config/wt.toml` and runs this
repo's lifecycle hooks.

Use flat branch names that also work as directory names:

```text
proposal-token-efficiency-axes
feature-review-context-slices
deliverable-token-measurement
```

Avoid slash-based names such as `feature/token-efficiency`.

## Integration Branch

- Integration branch: `main`
- Worktree: `main/`
- Do not use `main/` for normal implementation work.
- Open PRs from sibling worktrees into `main`.
- Let human review and merge decide integration.

## Copied Local Files

Worktrunk is configured in `main/.config/wt.toml`.

When a worktree is created, this hook runs first:

```toml
pre-start = "/opt/homebrew/bin/wt step copy-ignored"
```

That copies gitignored local files from the primary worktree into the new worktree.
This is the important bit for files such as:

- `.env`
- `.env.*`
- `.vscode/`
- other local files ignored by `.gitignore` or local Git excludes

Tracked files are never overwritten by `copy-ignored`.

The config excludes heavy, generated, or tool-specific state such as:

- `.venv/`, `venv/`, `env/`
- `.claude/`, `.claude-plugin/`, `.qoder/`
- `node_modules/`
- build and dist outputs
- Python caches and coverage output
- graph database/cache files

This repo uses `.agents/skills`, not `.claude/skills`; do not copy or vendor Claude
personal skills into this repository.

## Post-Create Setup

After a new worktree is created, Worktrunk runs:

```toml
post-start = [
  "git -C {{ worktree_path }} config --worktree core.bare false",
  "git -C {{ worktree_path }} config --worktree core.worktree {{ worktree_path }}",
  "cd {{ worktree_path }} && uv sync --all-extras --group dev",
]
```

The Git config lines mirror the known-good Zazz worktree setup and ensure each worktree
has explicit worktree-local Git settings. The `uv sync` line prepares the Python
environment without copying `.venv/`, because Python virtual environments contain
absolute paths and should be rebuilt per worktree.

## Useful Low-Level Checks

```bash
git --git-dir=.bare rev-parse --is-bare-repository
git --git-dir=.bare worktree list
git -C main status --short --branch
git -C main branch -vv
```

Use low-level `git --git-dir=.bare ...` commands for inspection or repair. Prefer
Worktrunk for routine switching, branch creation, PR checkout, and cleanup.

## Quality Shortcuts

From a worktree:

```bash
wt step test
wt step lint
wt step doc-check
```

Equivalent commands:

```bash
uv run pytest
uv run ruff check .
git diff --check
```
