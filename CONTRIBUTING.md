# Contributing to code-review-graph

Thank you for your interest in contributing! This guide will help you get started.

## Development Setup

```bash
# Clone the repository
git clone https://github.com/tirth8205/code-review-graph.git
cd code-review-graph

# Install with dev dependencies (requires uv)
uv sync --extra dev

# Verify setup
uv run pytest tests/ --tb=short -q
```

## Running Tests

```bash
# All tests
uv run pytest tests/ --tb=short -q

# With coverage
uv run pytest --cov=code_review_graph --cov-report=term-missing --cov-fail-under=65

# Single test file
uv run pytest tests/test_parser.py -v
```

## Linting and Type Checking

```bash
uv run ruff check code_review_graph/
uv run mypy code_review_graph/ --ignore-missing-imports --no-strict-optional
```

## Code Style

- **Line length**: 100 characters
- **Target**: Python 3.10+
- **Linter**: ruff (rules: E, F, I, N, W)
- **SQL**: Always parameterized queries (`?` placeholders)
- **Imports**: Sorted by ruff (isort-compatible)

## Making Changes

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Make your changes
4. Add tests for new functionality
5. Ensure all tests pass: `uv run pytest`
6. Ensure linting passes: `uv run ruff check code_review_graph/`
7. Submit a pull request

## Project Structure

```
code_review_graph/     # Core Python package
  parser.py            # Tree-sitter multi-language parser
  graph.py             # SQLite graph store
  tools/               # MCP tool implementations
  context_savings.py   # Compact estimated context-savings metadata
  incremental.py       # Git diff + file watch logic
  embeddings.py        # Vector embedding support
  visualization.py     # D3.js HTML generator
  cli.py               # CLI entry point
  main.py              # MCP server entry point
tests/                 # Test suite
  fixtures/            # Language sample files
```

## Adding Language Support

If you just need a language for your own repo, you may not need to contribute at all: drop a `.code-review-graph/languages.toml` into your project mapping extensions and node types to any grammar in tree-sitter-language-pack — see [docs/CUSTOM_LANGUAGES.md](docs/CUSTOM_LANGUAGES.md). To add built-in support upstream:

1. Add the extension mapping to `EXTENSION_TO_LANGUAGE` in `parser.py`
2. Add tree-sitter node types to `_CLASS_TYPES`, `_FUNCTION_TYPES`, `_IMPORT_TYPES`, `_CALL_TYPES`
3. Add a sample fixture file in `tests/fixtures/`
4. Add parsing tests in `tests/test_multilang.py`

## Adding a Platform Target

Every supported AI tool is permanent maintenance surface. Its config path, schema, install merge,
uninstall, and tests all have to keep working on every release. Some existing targets were merged
without any evidence that the integration worked in a released client, and those are the ones that
break. New targets are held to the bar below.

Start with a platform request issue (https://github.com/tirth8205/code-review-graph/issues/new/choose)
so the client can be discussed before anyone writes code. A pull request that adds a platform will
not be reviewed until it includes all of the following.

1. A link to the platform's official MCP configuration documentation. Blog posts, forum replies,
   and screenshots of a settings dialog are not enough.
2. The exact config file path and the exact schema of a server entry, including which top-level key
   holds the servers, whether that value is an object or an array, and whether a `type` field is
   required.
3. The entry added through the existing `PLATFORMS` table in `code_review_graph/skills.py`, plus
   `_PLATFORM_CHOICES` in `code_review_graph/cli.py`. Use the fields already there: `name`,
   `config_path`, `key`, `detect`, `format`, `needs_type`, and where needed `legacy_keys`,
   `server_type`, `entry_fields`. If the client needs something the table cannot express, say so in
   the pull request and explain why, rather than adding a bespoke code path beside it.
4. Preservation of unrelated user settings. Install must merge only the `code-review-graph` server
   entry and leave every other server, key, and top-level setting intact. If the file cannot be
   parsed, install must skip it rather than rewrite it.
5. A byte-idempotent reinstall. Running install twice must leave the config file and any generated
   instruction file byte for byte identical.
6. A working uninstall in `code_review_graph/uninstall.py` that removes only what install added,
   including any legacy keys, and leaves the rest of the file untouched.
7. Lifecycle tests matching the existing ones: an install, reinstall, and uninstall test in
   `tests/test_cli_install.py` shaped like `test_copilot_cli_install_reinstall_uninstall_lifecycle`,
   and a passing run of the all-platforms sweep in `tests/test_uninstall.py`
   (`test_uninstall_removes_mcp_entry_for_every_current_platform_spec`), which every new entry is
   automatically subject to.
8. Evidence from a real released client: a screenshot or transcript of an actual session in that
   client where a code-review-graph tool is invoked and returns a result. A rendered image of text,
   a mockup, or a description of what should happen is not evidence.

If no maintainer can install and run the client, the request may be declined or left open until
someone who uses it is willing to own it and respond when it breaks. An existing target may also be
removed if it breaks and nobody steps up to fix it.

## Reporting Issues

- Open an issue via the issue forms: https://github.com/tirth8205/code-review-graph/issues/new/choose (bug report, feature request, or platform request — blank issues are disabled)
- For questions and ideas, use GitHub Discussions instead: https://github.com/tirth8205/code-review-graph/discussions
- Include: Python version, OS, steps to reproduce, error output

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
