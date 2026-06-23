# CRG Graph Visualization Guidance

This guidance supports the `mw-improve-metrics-analyis-zazz` deliverable. It explains how to build and inspect CRG's graph visualization so implementers and reviewers can understand what the review-output changes are summarizing.

## Purpose

Use visualization to answer practical questions:

- Which files, functions, tests, flows, and communities does CRG see?
- Are changed areas clustered by feature/subsystem, or scattered across unrelated communities?
- Which edges explain blast radius: calls, imports, containment, test coverage, or inheritance?
- Does scoped review output correspond to a visually understandable slice of the graph?
- Are graph artifacts generated in a local derived cache rather than committed source?

Visualization is supporting evidence. It does not replace automated tests, `detect-changes --for-review`, or source inspection.

## Build Or Refresh The Graph

From the repo being inspected:

```bash
code-review-graph build --repo .
```

For this development repo, use the local package command shape after implementation is available:

```bash
uv run code-review-graph build --repo .
```

For a large external validation repo, the graph is a derived cache. If a stale `.code-review-graph/` directory already exists and the Owner has approved fresh validation, it is acceptable to remove that cache before rebuilding:

```bash
rm -rf .code-review-graph
uv run code-review-graph build --repo .
```

Record any cache removal and rebuild command in the run log. Do not remove source files, tracked fixtures, or external repo work.

## Generate Visual Outputs

Interactive HTML:

```bash
uv run code-review-graph visualize --repo . --mode auto
open .code-review-graph/graph.html
```

Serve locally when browser security blocks file loading or when sharing within the machine:

```bash
uv run code-review-graph visualize --repo . --mode auto --serve
```

Large graph modes:

```bash
uv run code-review-graph visualize --repo . --mode community
uv run code-review-graph visualize --repo . --mode file
uv run code-review-graph visualize --repo . --mode full
```

Export formats for external tools:

```bash
uv run code-review-graph visualize --repo . --format graphml
uv run code-review-graph visualize --repo . --format cypher
uv run code-review-graph visualize --repo . --format svg
uv run code-review-graph visualize --repo . --format obsidian
```

Use `community` or `file` mode first for large repos. Use `full` only for small graphs or a narrow test fixture.

## What To Inspect

### Overview

Start with counts:

```bash
uv run code-review-graph status --repo .
```

Then compare with the visualization stats bar. Large differences indicate a stale graph or an export bug.

### Changed Area

Run compact review output and keep it beside the graph:

```bash
uv run code-review-graph detect-changes --repo . --base main --for-review --max-tokens 2000
```

For a section agent slice:

```bash
uv run code-review-graph detect-changes --repo . --base main --for-review --max-tokens 2000 --scope 'code_review_graph/**'
```

In the graph UI, search for the repo-relative file names that appear in `review_priorities` and `test_gaps`.

### Edge Interpretation

Interpret edge types conservatively:

- `CONTAINS` explains file/class/function nesting.
- `CALLS` explains local call relationships and likely blast radius.
- `IMPORTS_FROM` explains module dependencies, not necessarily runtime execution.
- `TESTED_BY` explains direct or inferred test coverage edges.
- `INHERITS` and `IMPLEMENTS` mark type hierarchy concerns.
- `REFERENCES`, `INJECTS`, `CONSUMES`, `PRODUCES`, and `TEMPORAL_STUB` may be parser/enrichment-specific and need source verification.

Graph edges are hints. Before making a review finding, verify the actual code.

## How To Compare Visualization With The Deliverable

Use this checklist during manual validation:

- The compact payload uses repo-relative paths while the graph may still store absolute paths internally.
- Equal-risk priorities appear in a stable order across repeated runs.
- `--scope` output matches the graph region for that path glob.
- Suppressed test gaps are absent from `test_gaps` but visible through suppression metadata.
- Affected flows in compact output are summarized by name/criticality and do not dump full `path_json`.
- The savings record says it measures change analysis, not whole review-session cost.
- No sibling-divergence section appears; that is P6 and intentionally out of scope.

## Evidence To Record

When visualization is used as manual evidence, add a run-log entry with:

- repo inspected
- command(s) run
- whether `.code-review-graph/` was removed first
- generated artifact path, such as `.code-review-graph/graph.html`
- mode used: `auto`, `community`, `file`, or `full`
- one short observation tied to an AC, such as "scope output for `backend/**` matched graph file-mode slice"

Do not attach screenshots unless they clarify a specific review question. The generated graph files are derived artifacts and should not be committed unless the Owner explicitly asks for committed evidence.
