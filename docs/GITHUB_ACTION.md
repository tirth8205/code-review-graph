# GitHub Action: Risk-Scored PR Review

code-review-graph ships a composite GitHub Action (`action.yml` at the repo
root) that posts a risk-scored review comment on each pull request. The
analysis is local-first: the knowledge graph is built and queried on your CI
runner, and no source code is sent to an external service.

On each run the action:

1. Installs `code-review-graph` from PyPI.
2. Restores the cached `.code-review-graph/` SQLite graph and re-parses the
   files changed by the PR, or builds the graph from scratch on a cache miss.
3. Runs `code-review-graph detect-changes --base origin/<base-branch>` to get
   risk-scored functions, affected execution flows and test gaps.
4. Renders a markdown report with `scripts/render_pr_comment.py` and upserts
   one sticky PR comment. The same comment is updated on every push.
5. Optionally fails the job when the overall risk score reaches a threshold
   (`fail-on-risk`).

`detect-changes` resolves local and remote branch refs to their merge base
with `HEAD`, which matches GitHub's **Files changed** scope on divergent
branches when the common ancestor is available locally. In a shallow clone
where Git cannot find that ancestor, it falls back to the branch ref as
given. Commit ids and revision expressions are used exactly. The default
`actions/checkout` depth of 1 is such a shallow clone; set `fetch-depth: 0`
on the checkout step if you need merge-base scoping.

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
      - uses: actions/checkout@v7
      - uses: tirth8205/code-review-graph@v2.3.8
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

The default `GITHUB_TOKEN` is enough. No PAT, API key or third-party service
is needed.

The action's own steps use `actions/setup-python@v7` and `actions/cache@v6`,
which run on Node 24. Self-hosted runners must be version `2.327.1` or newer.

To turn the review into a merge gate:

```yaml
      - uses: tirth8205/code-review-graph@v2.3.8
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
          fail-on-risk: high
```

## Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `github-token` | yes | none | Token used to post the sticky PR comment via the GitHub API. The workflow's default `GITHUB_TOKEN` works when the job has `pull-requests: write`. A personal access token or a GitHub App installation token also works; see [Which comment gets updated](#which-comment-gets-updated). |
| `comment` | no | `true` | Post (and keep updated) the sticky PR comment. Set to `false` to run analysis and gating without commenting. |
| `fail-on-risk` | no | `none` | Fail the job when the overall risk score reaches a level: `none` (never fail), `high` (risk >= 0.70), `critical` (risk >= 0.85). |
| `python-version` | no | `3.12` | Python version used to run code-review-graph (3.10 or newer). |

## Outputs

| Output | Description |
|--------|-------------|
| `comment-file` | Runner-local path to the rendered markdown report. Use with `comment: false` when a separate trusted workflow will publish it. |

### Risk levels

`detect-changes` produces an overall risk score from 0.0 to 1.0: the maximum
across changed functions. `compute_risk_score` in
`code_review_graph/changes.py` adds up flow participation, community
crossing, test coverage, security-sensitive names and caller count. The
action maps the score to levels:

| Level | Score |
|-------|-------|
| low | < 0.40 |
| medium | 0.40 to 0.69 |
| high | 0.70 to 0.84 |
| critical | >= 0.85 |

## What the comment contains

- **Overall risk** score and level, with counts of changed functions,
  affected flows and test gaps.
- **Risk-scored changes**: a table of the top 10 changed symbols by risk,
  with `file:line` location and whether the symbol has a test.
- **Affected execution flows**: up to 5 entry-point flows the change
  touches, ordered by criticality, with node and file counts.
- **Test gaps**: up to 5 changed functions with no direct test coverage.
- **Token savings**: how many tokens the graph-backed report saved compared
  with reading every changed file in full. This is the same
  `context_savings` estimate the CLI's Token Savings panel shows (a
  `chars / 4` approximation labelled `estimated: true`; see
  [REPRODUCING.md](REPRODUCING.md) for the calibration method).
- A `Powered by code-review-graph` footer.

If `detect-changes` capped the analysed functions (`CRG_MAX_CHANGED_FUNCS`,
default 500), the comment says so. A body over 60,000 UTF-8 bytes is cut on a
line boundary and marked `Report truncated`; that limit is the finished body,
notice and footer included, so a truncated report still fits the cap the
trusted commenting workflow enforces (`MAX_REPORT_BYTES`).

The comment starts with a hidden HTML marker
(`<!-- code-review-graph-report -->`). On each run the action looks up a
comment that both starts with the marker and was written by the token's own
account, and PATCHes that one instead of creating a new comment. Both
conditions matter: the marker is documented here, so a pull request
participant can post a comment carrying it, and an author filter is what
stops the action adopting it. When the token's identity cannot be
established the action posts a comment of its own rather than editing
someone else's.

## Cache behavior

The action caches the `.code-review-graph/` directory (the SQLite graph
database) with `actions/cache`:

- **Key**: `code-review-graph-schema13-<runner.os>-<hashFiles(lockfiles)>`.
  The lockfile hash covers `uv.lock`, `poetry.lock`, `requirements*.txt`,
  `Pipfile.lock`, `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`,
  `go.sum`, `Cargo.lock`, `Gemfile.lock` and `composer.lock`.
- **Schema segment**: `schema13` tracks the database schema version
  (`LATEST_VERSION` in `code_review_graph/migrations.py`). It is bumped when
  the schema changes so a stale cache is not restored across incompatible
  versions.
- **Restore keys**: fall back to any cache for the same OS and schema, so a
  lockfile change still reuses the previous graph.
- **On cache hit**: the action runs `code-review-graph update --base
  origin/<base-branch>`, which re-parses only the files that differ from the
  PR's base. If the restored database is unusable, it falls back to a full
  `build`; `build` discards a `graph.db` SQLite cannot read and rebuilds from
  scratch, so a corrupt cache costs one slow run rather than turning every
  run red until someone clears the cache by hand.
- **On cache miss**: a full `code-review-graph build` runs. Later runs are
  incremental.

## Security notes

- **Token scope**: direct commenting needs `contents: read` for checkout and
  `pull-requests: write` to post the comment. In the split fork-safe setup,
  the analysis workflow needs only `contents: read`; the trusted commenter
  needs only `actions: read` and `pull-requests: write`. Grant exactly those
  permissions in each workflow.
- **Local-first**: analysis runs on the runner. No code, diff or metadata
  leaves GitHub's infrastructure; there is no external API, account or key.
- **Untrusted input**: dynamic values (`github.base_ref`, the PR number,
  action inputs) reach scripts through environment variables, never by
  interpolation into shell commands. The markdown renderer escapes table and
  markup characters and strips control characters from symbol names and file
  paths, on top of the server-side `_sanitize_name()` step.
- **Pinning**: when consuming the action from another repository, pin
  `uses:` to a release tag or commit SHA rather than `@main`.
- **Fork PRs**: `pull_request` runs from forks get a read-only
  `GITHUB_TOKEN`, so they cannot post the comment directly. Use an
  unprivileged `pull_request` workflow with `comment: false`, upload the
  `comment-file` as an artifact, and publish it from a separate trusted
  `workflow_run` workflow. See
  [`.github/workflows/pr-review.yml`](../.github/workflows/pr-review.yml) and
  [`.github/workflows/pr-review-comment.yml`](../.github/workflows/pr-review-comment.yml).
  GitHub loads the `workflow_run` workflow from the default branch (`staging`
  in this repository), so the trusted commenting half becomes active only
  after that workflow is merged there.
  The privileged workflow must verify the source event and analysed commit,
  extract only under `runner.temp`, cap and validate the artifact, and add its
  own sticky marker before posting. Avoid `pull_request_target` with a checkout
  of PR code because it can execute untrusted code with a privileged token
  ([details](https://securitylab.github.com/resources/github-actions-preventing-pwn-requests/)).

## Which comment gets updated

The Action keeps one comment per pull request and rewrites it on every push.
It finds that comment by the hidden marker `<!-- code-review-graph-report -->`
plus the comment's author, because the marker is published here and anyone can
paste it into a comment of their own.

How the author is established depends on the token:

- A personal access token answers `GET /user`, so the Action matches its own
  login exactly.
- An installation token (the workflow's default `GITHUB_TOKEN`, or a GitHub
  App's) cannot call `GET /user`, and its comments are authored by a bot
  account whose login the Action cannot learn: `github-actions[bot]` for the
  default token, `<app-slug>[bot]` for an App. There the Action matches a
  marker comment written by a bot. Pull request participants are never bots,
  so a pasted marker is still not adopted.

If several bots post marker comments on the same pull request under
installation tokens, give the Action a personal access token so its identity
is exact.

## Dogfooding

This repository runs the action on its own PRs via
[`.github/workflows/pr-review.yml`](../.github/workflows/pr-review.yml),
which runs the local `action.yml` without write permissions and uploads the
rendered report. The trusted
[`pr-review-comment.yml`](../.github/workflows/pr-review-comment.yml) workflow
validates that artifact and posts the sticky comment without checking out or
executing PR-controlled code.

## Rendering script

Markdown rendering and the risk gate live in
[`scripts/render_pr_comment.py`](../scripts/render_pr_comment.py) (standard
library only, tested in `tests/test_action_render.py`):

```bash
code-review-graph detect-changes --base origin/main | \
  python scripts/render_pr_comment.py            # markdown to stdout

python scripts/render_pr_comment.py --input report.json \
  --fail-on-risk high --quiet                    # gate only: exit 3 on breach
```

Options: `--input` (JSON file or `-` for stdin, default `-`), `--output`
(file or `-` for stdout, default `-`), `--fail-on-risk none|high|critical`,
`--max-functions` (default 10), `--max-flows` (default 5), `--quiet` (skip
writing the body). Exit codes: 0 rendered and gate passed or disabled, 2 the
input file could not be read, 3 risk gate breached, 4 `detect-changes`
produced no analysis at all.

Exit 4 is deliberately distinct from 0. `detect-changes` prints exactly
`No changes detected.` for a tree it read and found unchanged; anything else
that is not JSON means the analysis never ran. The rendered comment then says
so, and the action fails the job, because a reassuring comment on a pull
request nobody analyzed is worse than no comment at all. This is not
something `fail-on-risk: none` can switch off: an unknown risk is not a low
one.
