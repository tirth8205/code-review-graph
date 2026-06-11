# GitHub Action: Risk-Scored PR Review

code-review-graph ships a composite GitHub Action (`action.yml` at the repo
root) that posts a risk-scored, graph-aware review comment on every pull
request — think of it as a hosted AI review bot (Greptile-style), except the
analysis is **local-first**: the knowledge graph is built and queried entirely
on your CI runner, and no source code is sent to any external service.

On each PR run the action:

1. Installs `code-review-graph` from PyPI.
2. Restores the cached `.code-review-graph/` SQLite graph (or builds it from
   scratch on a cache miss) and incrementally re-parses the files changed by
   the PR.
3. Runs `code-review-graph detect-changes --base origin/<base-branch>` to get
   risk-scored functions, affected execution flows, and test gaps.
4. Renders a markdown report (via `scripts/render_pr_comment.py`) and upserts
   a single sticky PR comment — the same comment is updated on every push, so
   the PR thread is never spammed.
5. Optionally fails the job when the overall risk score crosses a threshold
   (`fail-on-risk`).

## Quick start (external repositories)

```yaml
# .github/workflows/code-review-graph.yml
name: code-review-graph

on:
  pull_request:

permissions:
  contents: read
  pull-requests: write

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: tirth8205/code-review-graph@v2.3.6
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

That is the whole setup. The default `GITHUB_TOKEN` provided by Actions is
sufficient — no PAT, no API key, no third-party service.

To turn the review into a merge gate:

```yaml
      - uses: tirth8205/code-review-graph@v2.3.6
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
          fail-on-risk: high
```

## Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `github-token` | yes | — | Token used to post the sticky PR comment via the GitHub API. The workflow's default `GITHUB_TOKEN` works when the job has `pull-requests: write`. |
| `comment` | no | `true` | Post (and keep updated) the sticky PR comment. Set to `false` to run analysis/gating without commenting. |
| `fail-on-risk` | no | `none` | Fail the job when the overall risk score reaches a level: `none` (never fail), `high` (risk ≥ 0.70), `critical` (risk ≥ 0.85). |
| `python-version` | no | `3.12` | Python version used to run code-review-graph (3.10+ supported). |

### Risk levels

`detect-changes` produces a 0.0–1.0 overall risk score (max across changed
functions; see `code_review_graph/changes.py:compute_risk_score` for the
scoring factors: flow participation, community crossing, test coverage,
security-sensitive names, caller count). The action maps it to levels:

| Level | Score |
|-------|-------|
| low | < 0.40 |
| medium | 0.40 – 0.69 |
| high | 0.70 – 0.84 |
| critical | ≥ 0.85 |

## What the comment contains

- **Overall risk** score and level, with counts of changed functions,
  affected flows, and test gaps.
- **Risk-scored changes** — a table of the top changed symbols ordered by
  risk, with file:line locations and test-coverage status.
- **Affected execution flows** — which entry-point flows the change touches,
  ordered by criticality.
- **Test gaps** — changed functions with no direct test coverage.
- **Token savings** — how many tokens the graph-backed report saved versus
  reading every changed file in full. This is the same `context_savings`
  estimate the CLI's Token Savings panel shows (a `chars / 4` approximation
  labelled `estimated: true` — see [REPRODUCING.md](REPRODUCING.md) for the
  calibration methodology).
- A `Powered by code-review-graph` footer.

The comment starts with a hidden HTML marker
(`<!-- code-review-graph-report -->`). The action looks the marker up via
`gh api` on each run and PATCHes the existing comment instead of creating a
new one (a "sticky" comment).

## Cache behavior

The action caches the `.code-review-graph/` directory (the SQLite graph
database) with `actions/cache`:

- **Key**: `code-review-graph-schema9-<runner.os>-<hashFiles(lockfiles)>`,
  where the lockfile hash covers common Python/JS/Go/Rust/Ruby/PHP lockfiles
  (`uv.lock`, `poetry.lock`, `requirements*.txt`, `package-lock.json`,
  `go.sum`, `Cargo.lock`, …).
- **Schema segment**: `schema9` tracks the database schema version
  (`LATEST_VERSION` in `code_review_graph/migrations.py`). It is bumped when
  the schema changes so stale caches are never restored across incompatible
  versions.
- **Restore keys**: fall back to any cache for the same OS and schema, so a
  lockfile change still reuses the previous graph.
- **On cache hit**: the action runs `code-review-graph update --base
  origin/<base-branch>`, which re-parses only the files that differ from the
  PR's base ref. If the restored database turns out to be unusable, it falls
  back to a full `build`.
- **On cache miss**: a full `code-review-graph build` runs (one-time cost;
  subsequent PR runs are incremental).

## Security notes

- **Token scope**: the action needs only `pull-requests: write` (to post the
  comment) and `contents: read` (for checkout). Grant exactly that in the
  workflow's `permissions:` block — the examples above do. The token is used
  for nothing except listing/creating/updating the one PR comment.
- **Local-first**: analysis runs entirely on the runner. No code, diff, or
  metadata leaves GitHub's infrastructure; there is no external API, account,
  or key.
- **Untrusted input**: all dynamic values (`github.base_ref`, the PR number,
  action inputs) are passed to scripts through environment variables, never
  interpolated into shell commands. The markdown renderer escapes
  table/markup characters and strips control characters from symbol names
  and file paths before they reach the comment body, on top of the
  server-side `_sanitize_name()` sanitization.
- **Pinning**: when consuming the action from another repository, pin
  `uses:` to a release tag or commit SHA rather than `@main`.
- **Fork PRs**: `pull_request` runs from forks receive a read-only
  `GITHUB_TOKEN`, so the comment step will fail for fork PRs unless you use
  `pull_request_target` — which checks out trusted base-branch workflow
  code; understand [the security implications](https://securitylab.github.com/resources/github-actions-preventing-pwn-requests/)
  before switching, or set `comment: false` for fork PRs.

## Dogfooding

This repository runs the action on its own PRs via
[`.github/workflows/pr-review.yml`](../.github/workflows/pr-review.yml),
which `uses: ./` (the local `action.yml`).

## Rendering script

The markdown rendering and risk gating logic lives in
[`scripts/render_pr_comment.py`](../scripts/render_pr_comment.py) (stdlib
only, unit-tested in `tests/test_action_render.py`) rather than inline YAML,
so it can be tested and reused:

```bash
code-review-graph detect-changes --base origin/main | \
  python scripts/render_pr_comment.py            # markdown to stdout

python scripts/render_pr_comment.py --input report.json \
  --fail-on-risk high --quiet                    # gate only: exit 3 on breach
```
