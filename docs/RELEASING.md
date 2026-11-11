# Releasing

How a new version gets from `staging` to PyPI. Only the maintainer does this.
The branch rules behind it are in
[CONTRIBUTING.md](../CONTRIBUTING.md#branching-and-promotion).

```
release/vX.Y.Z --PR--> staging --PR--> testing --PR--> main --tag vX.Y.Z--> GitHub release --> PyPI
```

Nothing is ever released from `staging` or `testing`. `publish.yml` stops
before it builds anything if the tagged commit is not on `main`.

## 1. Make the release branch

Branch off an up-to-date `staging`:

```bash
git fetch origin
git switch -c release/vX.Y.Z origin/staging
```

## 2. Move the version

Change every place below to the new version in one commit.
`tests/test_documentation.py::test_release_version_is_current_everywhere_it_is_written_down`
fails if one is missed.

- `pyproject.toml`: `version = "X.Y.Z"`.
- `code_review_graph/__init__.py`: `__version__ = "X.Y.Z"`.
- `uv.lock`: run `uv lock` so the lock file carries the new version.
- `CHANGELOG.md`: rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`, then
  add a new, empty `## [Unreleased]` above it.
- `docs/FEATURES.md`: add `## vX.Y.Z (Current)` with the highlights, and take
  `(Current)` off the old heading. Only one heading may say it.
- `docs/USAGE.md`: `Applies to code-review-graph X.Y.Z.`
- The Action pins, `tirth8205/code-review-graph@vX.Y.Z`, in `README.md`, its
  four translations and `docs/GITHUB_ACTION.md`.
- `.github/ISSUE_TEMPLATE/bug_report.yml`: the version `placeholder`.

If `SUPPORTED_SCHEMA_VERSION` in the VS Code extension changed since the last
release, bump the extension's version in `code-review-graph-vscode/package.json`
too, and add its row to the Versions table in
`code-review-graph-vscode/README.md`. The extension ships separately (step 6).

Then run the checks:

```bash
uv run pytest tests/ --tb=short -q
uv run ruff check code_review_graph/
uv run mypy code_review_graph/ --ignore-missing-imports --no-strict-optional
uv run --python 3.13 python -m pytest tests/test_packaging.py -m packaging
```

## 3. Promote it

1. Open a PR from `release/vX.Y.Z` into `staging` and merge it once CI is green.
2. Open the promotion PR into `testing` (`gh pr create --base testing --head staging`),
   read what it brings in, and merge it yourself with a merge commit. This starts
   the promotion gate.
3. Read the promotion gate's verdict. Then open the promotion PR into `main`
   (`gh pr create --base main --head testing`) and merge it yourself with a merge
   commit.

No workflow merges any of these. A person reviews and merges each one.

## 4. Tag the merge commit on `main`

Tag the merge commit the promotion made on `main`, with an annotated tag:

```bash
git fetch origin
git switch --detach origin/main
git log -1 --oneline        # check this is the promotion's merge commit
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin vX.Y.Z
```

## 5. Publish the GitHub release

Make a GitHub release from tag `vX.Y.Z`, targeting `main`, with the
`CHANGELOG.md` notes for this version. Publishing it starts `publish.yml`, which
checks the tag is on `main`, builds the wheel and sdist, and uploads them to
PyPI. If the check fails, the tag is on the wrong commit: delete the tag and the
release, then tag the merge commit on `main` and publish again.

## 6. Ship the VS Code extension, when needed

Only when step 2 bumped the extension. `publish.yml` does not ship it, and an
older extension cannot read a graph with a newer schema. Package it with
`npm run package` in `code-review-graph-vscode/` and publish it in the same
release.

## 7. Check it

```bash
pip install --upgrade code-review-graph==X.Y.Z
code-review-graph --version
```
