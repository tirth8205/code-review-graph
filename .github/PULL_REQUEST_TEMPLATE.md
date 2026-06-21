# Pull Request

## Linked issue

<!-- Link the issue this PR addresses, e.g. "Closes #123".
     If there is no issue, briefly say why (e.g. trivial typo fix). -->

Closes #

## What & why

<!-- What does this PR change, and why is the change needed? -->

## How it was tested

<!-- Paste the exact commands you ran and summarise their results. Typical commands
     (see CONTRIBUTING.md "Running Tests" and "Linting and Type Checking"): -->

```bash
uv run pytest tests/ --tb=short -q
uv run ruff check code_review_graph/
uv run mypy code_review_graph/ --ignore-missing-imports --no-strict-optional
```

## Checklist

<!-- Mirrors the requirements in CONTRIBUTING.md ("Making Changes" and "Code Style"). -->

- [ ] Tests added for new functionality
- [ ] All tests pass: `uv run pytest tests/ --tb=short -q`
- [ ] Linting passes: `uv run ruff check code_review_graph/`
- [ ] Type checking passes: `uv run mypy code_review_graph/ --ignore-missing-imports --no-strict-optional`
- [ ] Lines are at most 100 characters
- [ ] Docs updated where behavior changed (README, `docs/`, docstrings)
