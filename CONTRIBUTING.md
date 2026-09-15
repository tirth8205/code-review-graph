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
| `testing` | Candidate for the next release. Gets a longer soak and manual QA. | Maintainer, via a promotion PR from `staging`. |
| `main`    | Released code. Nothing reaches `main` without passing QA on `testing`. | Maintainer, via a promotion PR from `testing`. |

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
`testing` to `main`. Open one from the Actions tab (`Promote` workflow, pick the step) or
by hand with `gh pr create --base testing --head staging`. They are always merged with a
**merge commit**, never squashed, so every contributor stays the author of their commits;
the `testing` and `main` rulesets allow no other merge method. A promotion is the
maintainer's sign-off: CI green is necessary but not sufficient. A promotion PR opened by
the workflow shows an "Approve workflows to run" banner; the required checks are already
satisfied by the CI run on the source branch's tip, so the banner can be approved or
ignored.

**Hotfixes** for a released version follow the same path. If a fix is urgent, open the PR
against `staging` and promote twice in a row; do not open PRs against `main`.

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
