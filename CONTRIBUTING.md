# Contributing to code-review-graph

## Development Setup

```bash
git clone https://github.com/tirth8205/code-review-graph.git
cd code-review-graph
uv sync --extra dev                  # requires uv
uv run pytest tests/ --tb=short -q   # check the setup
```

## Running Tests

```bash
uv run pytest tests/ --tb=short -q                                                    # all tests
uv run pytest --cov=code_review_graph --cov-report=term-missing --cov-fail-under=65   # with the CI coverage gate
uv run pytest tests/test_parser.py -v                                                 # one file
```

CI runs the suite on Python 3.10, 3.11, 3.12 and 3.13 and fails below 65% coverage.

## Linting and Type Checking

```bash
uv run ruff check code_review_graph/
uv run mypy code_review_graph/ --ignore-missing-imports --no-strict-optional
```

CI also runs `bandit -r code_review_graph/ -c pyproject.toml` and checks that `LATEST_VERSION` in `code_review_graph/migrations.py` equals `SUPPORTED_SCHEMA_VERSION` in the VS Code extension.

## Code Style

- Line length: 100 characters.
- Target: Python 3.10+.
- Linter: ruff with rules E, F, I, N, W. Imports are sorted by ruff.
- SQL: parameterised queries (`?` placeholders) only.

## Making Changes

1. Fork the repository and create a branch from `staging`: `git checkout -b feature/your-feature origin/staging`.
2. Make the change and add tests for new behaviour.
3. Run the tests, ruff and mypy as above.
4. Update docs where behaviour changed (README, `docs/`, docstrings).
5. Open a pull request against `staging` (the default branch, so GitHub picks it for you). The template asks for the linked issue, what changed and why, and the commands you ran.

## Branching and promotion

The repository keeps three long-lived branches. Code moves in one direction only:

```
feature branch --PR--> staging --PR--> testing --PR--> main --tag--> PyPI
```

| Branch    | Purpose                                                        | Who merges into it                      |
| --------- | -------------------------------------------------------------- | --------------------------------------- |
| `staging` | Default branch. Every feature and fix PR lands here first.     | Maintainers, once CI is green.          |
| `testing` | Candidate for the next release. Gets a longer soak and manual QA. | The `Auto promote` workflow, once a day, when `staging` is green. |
| `main`    | Released code. Nothing reaches `main` without passing QA on `testing`. | Maintainer only, by hand, via a promotion PR from `testing`. |

Rules that apply to all three branches (enforced by repository rulesets):

- Changes arrive only through a pull request. Direct pushes, force-pushes and branch
  deletion are blocked for everyone, admins included.
- The same status checks must pass on every PR: `lint`, `type-check`, `security`,
  `schema-sync`, and `test` on Python 3.10 through 3.13. Feature PRs into `staging`
  must also be up to date with `staging` before merging; promotion PRs are exempt from
  the up-to-date rule because a merge-commit promotion always leaves the target one
  commit ahead of its source.
- Repository admins may bypass the check requirements only when merging a pull
  request, never by pushing.

**Feature PRs** target `staging`. Squash-merging is fine for a single-author PR; use a
merge commit when a PR has several authors so nobody loses attribution. Rebase-merge is
also allowed on `staging`.

**Promotion PRs** move everything on `staging` to `testing`, and later everything on
`testing` to `main`. They are always merged with a **merge commit**, never squashed, so
every contributor stays the author of their commits; the `testing` and `main` rulesets
allow no other merge method. Opening a promotion PR re-queues every required check on a
commit that already had them green from the push run, so the PR sits blocked for a
quarter of an hour or so before it can be merged. That is normal.

`staging` → `testing` happens by itself. `testing` → `main` never does.

### Automatic promotion to `testing`

`.github/workflows/auto-promote.yml` runs once a day. If `staging` has commits `testing`
does not, every required status check is green on the `staging` tip, and the promotion
gate has not failed on the current `testing` tip, it opens the promotion PR and merges it
with a merge commit. It writes a summary on every run, including the runs that decide to
do nothing, so "nothing to promote" is never indistinguishable from "did not run".

**One repository setting is required.** Settings → Actions → General → Workflow
permissions → tick **Allow GitHub Actions to create and approve pull requests** → Save.
Without it `gh pr create` is refused and no promotion PR can be opened — by this workflow
or by the manual `Promote` one. Leave *Workflow permissions* itself on **Read repository
contents and packages permissions**: both workflows ask for the writes they need in their
own `permissions:` block. If the tick is missing, the run goes red and the job summary
gives that click path.

It merges only a PR it opened itself: same repository, `staging` → `testing`, carrying the
`auto-promotion` label, and at the commit whose checks were read. `gh pr list --head
staging` matches a branch of that name in any of this repository's forks, and a PR's base
branch can be changed by its author at any time without re-running a single check, so all
of that is verified again immediately before the merge, and the merge itself is pinned
with `--match-head-commit`. **A promotion PR you opened by hand is never touched** — it
has no `auto-promotion` label, so the daily run refuses it by number and says so.

It stops, without failing, when there is nothing to promote, when CI is still running,
when a required check failed or was skipped, when the promotion gate is still running on
`testing`, when the gate failed there, or when `staging` moved mid-run. Those states are
reported elsewhere already and tomorrow's run looks again. Three things turn the run red,
because nothing else reports them:

- **GitHub refused the merge.** The PR is left open and the refusal is quoted in the job
  summary, with the causes listed in the order they actually occur; merge it by hand with
  a merge commit.
- **The workflow is stuck on its own PR** — it conflicts, it is a draft, or GitHub holds
  it for a rule with every required check green. A stalled promotion that reported itself
  as a green warning every morning would stay stalled for ever.
- **A PR it was about to merge is not the one it decided on**, or the release gate could
  not be started after the merge.

Every rule lives in `scripts/auto_promote.py` and is covered by
`tests/test_auto_promote.py`; the workflow fetches facts and obeys. The required contexts
are read from the `testing` ruleset at run time, so a renamed CI job cannot silently drop
out of the gate. The workflow has no input, variable or code path that can target `main`,
the PR it merges is re-checked at the door, and the tests assert both.

To see what it would decide without it doing anything, run the `Auto promote` workflow
from the Actions tab: a hand-started run defaults to a dry run.

**Promotion to `main` is never automatic.** Open it from the Actions tab (`Promote`
workflow, pick `testing -> main`) or by hand with
`gh pr create --base main --head testing`, read the promotion gate's verdict first, and
merge it yourself. That is the maintainer's sign-off: CI green is necessary but not
sufficient.

**Hotfixes** for a released version follow the same path. If a fix is urgent, open the PR
against `staging` and promote twice in a row; do not open PRs against `main`.

### The promotion gate

Every time something lands on `testing`, `.github/workflows/promotion-gate.yml` runs the
slow checks that are too expensive for a pull request. It also runs on demand from the
Actions tab. It never runs on a pull request, so no contributor waits for it.

It runs seven checks:

| Check | What it does | If it fails |
| ----- | ------------- | ----------- |
| `upgrade-path` | Installs the last three PyPI releases, builds a real graph with each, then opens and updates that graph with the current code. | Blocks |
| `packaging`    | Builds the wheel and the sdist, installs each into a clean environment with the checkout out of reach, and drives the installed program. | Blocks |
| `determinism`  | Rebuilds one corpus nine times, serial and parallel, thread and process, under two hash seeds, and compares every table. | Blocks |
| `suite`        | The ordinary test suite with the 65% coverage floor, on Python 3.10, 3.11, 3.12 and 3.13. | Blocks |
| `e2e`          | Drives the real MCP server over stdio on Linux, macOS and Windows. | Blocks on Linux and macOS, reports on Windows |
| `corpus`       | Clones eight pinned third-party repositories, builds a graph over each, and compares twelve measured properties against recorded baselines. | Reports |
| `browser`      | Renders the generated visualization page in headless Chromium. | Reports |

**Blocks** means: do not promote `testing` to `main` until it is green or the maintainer
has decided in writing why it does not matter. **Reports** means the failure is recorded
and shown but does not hold a release.

The split is not about how important a check is, it is about whether a failure is
evidence about our code:

- `upgrade-path` blocks because it is the only check that proves a database a user
  already has survives the upgrade. A migration that corrupts it cannot be undone by a
  later patch release.
- `packaging` blocks because a wheel missing a data file is broken for every user at once
  and needs another release to fix. It installs from PyPI, which every other job here
  already does, so it adds no new way to fail.
- `determinism` blocks because it needs no network and no third-party checkout: a
  failure is a real difference, never an outage.
- `suite` blocks because it is the same suite the pull-request CI already requires,
  re-run against the merged state of `testing`, which no single pull request tested.
- `e2e` blocks on Linux and macOS because the stdio MCP interface is what every editor
  integration speaks. The Windows leg reports, because process spawning and file-handle
  timing on the Windows runner is the flakiest surface in this repository and a runner
  hiccup must not hold a release.
- `corpus` reports because it clones eight repositories that belong to other people. A
  rate limit, an outage or an upstream force-push fails it for a reason that has nothing
  to do with this code, and a failed clone must not stop a release. A property that moved
  is still a real regression: read the numbers and decide.
- `browser` reports because it downloads a Chromium build at run time, and a page that
  fails to render damages nobody's data.

Reporting is not the same as ignoring. Every check is wrapped by
`scripts/promotion_gate.py`, which fails it when its test module is not in the checkout,
when the run collected almost nothing, or when the tests were skipped instead of
run, including a `browser` run that skipped because Playwright was missing. A check that
quietly tested nothing is recorded as a failure, not as a pass.

The result is posted twice: to the run's job summary, and as one comment on the open
issue labelled `promotion-gate` (the workflow opens that issue the first time it needs
it). The report names every check, whether it passed, and for a failure the exact
assertion that moved: for example the property, its baseline and the measured delta.

The gate opens no pull request and merges nothing. Promotion to `main` stays the
maintainer's decision, made with the `Promote` workflow as before. `Auto promote` cannot
reach `main` either: it promotes `staging` to `testing` and nothing else.

To run any of these by hand:

```bash
CRG_UPGRADE_TEST=1 uv run pytest -m upgrade -q -rxX   # upgrade-path
uv run pytest tests/test_packaging.py -m packaging -q # packaging
uv run pytest -m determinism -q -rxX                  # determinism
uv run pytest -m corpus -q                            # corpus (clones 8 repos)
uv run pytest -m browser -q                           # browser (needs the browser-test extra)
uv run pytest -m e2e -q                               # e2e
```

**Releases** are cut from `main` only: bump the version, tag `vX.Y.Z`, publish a GitHub
release, and the `publish` workflow uploads to PyPI. Nothing is ever released from
`staging` or `testing`.

**Archived branches.** Old branches are not deleted outright. A branch that carried work
not on `main` is kept as a tag under `archive/<branch-name>` (list them with
`git tag -l 'archive/*'`), and local review copies live under the hidden namespace
`refs/archive/local/*` (fetch them with `git fetch origin 'refs/archive/*:refs/archive/*'`).
Nothing anyone contributed has been removed from history.

## Project Structure

The module list is in the Architecture section of `CLAUDE.md`. In outline:

```
code_review_graph/         # core package: parser, graph store, incremental update, MCP server, CLI
  tools/                   # MCP tool implementations
  eval/                    # benchmark runner
tests/                     # pytest suite
  fixtures/                # sample files per language
code-review-graph-vscode/  # VS Code extension (separate package)
docs/                      # user documentation
skills/                    # shipped agent skills, bundled into the wheel
```

## Adding Language Support

If you only need a language for your own repository, you may not need to change this project: add a `.code-review-graph/languages.toml` that maps extensions and node types to any grammar in tree-sitter-language-pack (see [docs/CUSTOM_LANGUAGES.md](docs/CUSTOM_LANGUAGES.md)). To add built-in support:

1. Add the extension mapping to `EXTENSION_TO_LANGUAGE` in `parser.py`.
2. Add the tree-sitter node types to `_CLASS_TYPES`, `_FUNCTION_TYPES`, `_IMPORT_TYPES` and `_CALL_TYPES`.
3. Add a sample file in `tests/fixtures/`.
4. Add parsing tests in `tests/test_multilang.py`.

## Adding a Platform Target

Every supported AI tool is permanent maintenance surface: its config path, schema, install merge, uninstall and tests have to keep working on every release. Some existing targets were merged without evidence that they worked in a released client, and those are the ones that break. New targets are held to the bar below.

Start with a platform request issue (https://github.com/tirth8205/code-review-graph/issues/new/choose) so the client can be discussed before anyone writes code. A pull request that adds a platform will not be reviewed until it includes all of the following.

1. A link to the platform's official MCP configuration documentation. Blog posts, forum replies and screenshots of a settings dialog are not enough.
2. The exact config file path and the exact schema of a server entry: which top-level key holds the servers, whether that value is an object or an array, and whether a `type` field is required.
3. The entry added through the existing `PLATFORMS` table in `code_review_graph/skills.py`, plus `_PLATFORM_CHOICES` in `code_review_graph/cli.py`. Use the fields already there: `name`, `config_path`, `key`, `detect`, `format`, `needs_type`, and where needed `legacy_keys`, `server_type`, `entry_fields`. If the client needs something the table cannot express, say so in the pull request and explain why, rather than adding a separate code path beside it.
4. Preservation of unrelated user settings. Install must merge only the `code-review-graph` server entry and leave every other server, key and top-level setting intact. If the file cannot be parsed, install must skip it rather than rewrite it.
5. A byte-idempotent reinstall. Running install twice must leave the config file and any generated instruction file byte for byte identical.
6. A working uninstall in `code_review_graph/uninstall.py` that removes only what install added, including any legacy keys, and leaves the rest of the file untouched.
7. Lifecycle tests matching the existing ones: an install, reinstall and uninstall test in `tests/test_cli_install.py` shaped like `test_copilot_cli_install_reinstall_uninstall_lifecycle`, and a passing run of the all-platforms sweep in `tests/test_uninstall.py` (`test_uninstall_removes_mcp_entry_for_every_current_platform_spec`), which covers every new entry automatically.
8. Evidence from a real released client: a screenshot or transcript of a session in that client where a code-review-graph tool is invoked and returns a result. A rendered image of text, a mockup or a description of what should happen is not evidence.

If no maintainer can install and run the client, the request may be declined or left open until someone who uses it is willing to own it and respond when it breaks. An existing target may also be removed if it breaks and nobody steps up to fix it.

## Reporting Issues

- Open an issue through the issue forms: https://github.com/tirth8205/code-review-graph/issues/new/choose (bug report, feature request or platform request; blank issues are disabled).
- For questions and ideas, use GitHub Discussions: https://github.com/tirth8205/code-review-graph/discussions
- Include your Python version, OS, steps to reproduce and the error output.

## License

By contributing, you agree that your contributions are licensed under the MIT License.
