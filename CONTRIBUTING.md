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
uv run pytest --cov=code_review_graph --cov-report=term-missing --cov-fail-under=50

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
  tools.py             # MCP tool implementations
  incremental.py       # Git diff + file watch logic
  embeddings.py        # Vector embedding support
  visualization.py     # D3.js HTML generator
  cli.py               # CLI entry point
  main.py              # MCP server entry point
tests/                 # Test suite
  fixtures/            # Language sample files
```

## Adding Language Support

1. Add the extension mapping to `EXTENSION_TO_LANGUAGE` in `parser.py`
2. Add tree-sitter node types to `_CLASS_TYPES`, `_FUNCTION_TYPES`, `_IMPORT_TYPES`, `_CALL_TYPES`
3. Add a sample fixture file in `tests/fixtures/`
4. Add parsing tests in `tests/test_multilang.py`

## Reporting Issues

- Use GitHub Issues: https://github.com/tirth8205/code-review-graph/issues
- Include: Python version, OS, steps to reproduce, error output

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
