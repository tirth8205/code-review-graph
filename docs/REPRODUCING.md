# Reproducing the Benchmarks

This document gives the commands behind every benchmark number in the README and in
`diagrams/`. The pipeline is built to be deterministic: pinned upstream SHAs, a seeded
community detector and CPU embeddings, so two runs on different machines should agree within
float rounding. If your numbers differ by more than that, file an issue.

## Verifying the "saved tokens" number

The CLI's `Token Savings` panel uses a `chars / 4` estimate, labelled `estimated: true`, not
a model tokenizer. To check it against a real tokenizer:

```bash
pip install tiktoken
code-review-graph detect-changes --brief --verify    # also: update --brief --verify
```

The panel gains a `Verified (tiktoken)` row computed with OpenAI's `cl100k_base` tokenizer
(`verify_with_tiktoken()` in `code_review_graph/context_savings.py`). Example output:

```text
┌───────────────────────── Token Savings ─────────────────────────┐
│ Full context would be:     12,921 tokens                        │
│ Graph context used:           762 tokens                        │
│ Saved:                     12,159 tokens (~94%)                 │
│ Verified (tiktoken):       10,835 tokens (~93%)  [11,611 → 776] │
│ Breakdown: Functions 244 · Tests 191 · Risk 244 · Other 83      │
└─────────────────────────────────────────────────────────────────┘
```

### The panel can report a loss

`saved_tokens` and `saved_percent` are **signed**. A negative value means the graph response
cost more tokens than the baseline it replaced, and the panel prints `Cost more:` instead of
`Saved:`. This is not a rare edge case — the `token_efficiency` benchmark's median is 0.6x,
so it is the usual outcome on a small commit. Reproduce it on a trivial change:

```bash
mkdir tiny && cd tiny && git init -q
printf 'def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n' > tiny.py
git add -A && git commit -qm init
code-review-graph build
printf '\n\ndef sub(a, b):\n    return a - b\n' >> tiny.py
code-review-graph detect-changes --brief
```

```text
┌─────────────────────── Token Savings ────────────────────────┐
│ Full context would be:         25 tokens                     │
│ Graph context used:           798 tokens                     │
│ Cost more:                    773 tokens (~3092% over)       │
│ Breakdown: Functions 259 · Tests 208 · Risk 259 · Other 72   │
└──────────────────────────────────────────────────────────────┘
```

Before 2026-09-16, `estimate_context_savings()` computed `saved = max(0, baseline -
returned)`, so this case printed `Saved: 0 tokens (~0%)` and the panel was structurally
incapable of reporting a loss. `verify_with_tiktoken()` had the same clamp and lost it too.

### Calibration table

A one-off calibration over 222 files (2.2 MB of Python, JS, TS, Go, Rust, RST and Markdown)
sampled from the six eval repositories:

| Repo | sample files | bytes | chars/4 estimate | tiktoken real | ratio est/real |
|---|---:|---:|---:|---:|---:|
| flask | 46 | 470,179 | 117,559 | 109,969 | 1.069 |
| fastapi | 38 | 156,224 | 39,072 | 34,897 | 1.120 |
| gin | 30 | 471,793 | 117,962 | 132,296 | 0.892 |
| express | 23 | 296,805 | 74,207 | 83,575 | 0.888 |
| httpx | 38 | 254,184 | 63,556 | 62,909 | 1.010 |
| code-review-graph | 47 | 539,206 | 134,820 | 120,760 | 1.116 |
| **Overall** | **222** | **2,188,391** | **547,176** | **544,406** | **1.005** |

In aggregate `chars / 4` is within +0.5% of `cl100k_base`. Per repo it ranges from -11%
(gin: short Go identifiers) to +12% (fastapi: long docstrings and type hints). The saved
ratio moves less than the absolute counts because both sides of the division carry the same
bias.

The sample list and raw counts behind this table are not stored in the repository.
`--verify` reproduces the comparison on any checkout.

## What is deterministic

| Reproducible | Reason |
|---|---|
| Tree-sitter parsing | Pure function of the input bytes |
| Node and edge counts | Upserts keyed by `qualified_name` |
| FTS5 BM25 scores | Deterministic |
| Embeddings via `all-MiniLM-L6-v2` on CPU | Model weights pinned by hash in the Hugging Face cache |
| Leiden community IDs | Seeded: `_LEIDEN_SEED = 42` in `communities.py`; override with `CRG_LEIDEN_SEED` |
| `naive_corpus_tokens` | Fixed for a given checkout |
| Upstream repositories | Cloned in full and checked out at the SHA pinned in each config |

## Prerequisites

- Python 3.10 or newer
- `git` on PATH
- Network access to clone the six upstream repositories
- For embeddings, `torch` and `sentence-transformers`, installed by the `embeddings` extra

## Step 1: install with the eval and embeddings extras

```bash
git clone https://github.com/tirth8205/code-review-graph
cd code-review-graph
uv sync --extra eval --extra embeddings     # or: pip install -e ".[eval,embeddings]"
```

`eval` is `pyyaml` and `matplotlib` (matplotlib is only used by `--report`); `embeddings` is
`sentence-transformers` and `numpy`.

## Step 2: run the eval

This clones the six repositories at the SHAs pinned in `code_review_graph/eval/configs/*.yaml`,
builds a full graph for each (parser, resolvers, signatures, FTS5, flows, Leiden
communities), embeds it, and runs the benchmarks.

```bash
uv run code-review-graph eval --embed \
  --benchmark token_efficiency,impact_accuracy,agent_baseline,multi_hop_retrieval,search_quality
```

That is the exact command behind the 2026-09-16 capture (`search_quality` was run in a second
pass over the same graphs; the results are identical either way).

`--embed` is required for `agent_baseline` and `multi_hop_retrieval`; without it their
natural-language questions hit FTS5 only and return nothing. `code-review-graph eval --help`
lists the remaining benchmarks (`flow_completeness`, `search_quality`, `build_performance`)
and flags.

A thrown tool call is not a measurement. The row stays in the CSV with `status=error` and is
excluded from every aggregate. Regression tests for this are in `tests/test_eval.py`.

Outputs:

- `evaluate/test_repos/<name>/`, each with its own `.code-review-graph/graph.db`
- `evaluate/results/<name>_<benchmark>_<date>.csv`

## Step 3: embeddings for the standalone benchmark

The standalone token benchmark asks five natural-language questions and needs vector
embeddings; without them hybrid search matches nothing, the benchmark prints a warning and
reports 0x. If Step 2 ran with `--embed`, the graphs already have them. Otherwise:

```bash
for repo in express fastapi flask gin httpx code-review-graph; do
  uv run code-review-graph embed --repo "evaluate/test_repos/$repo"
done
```

Vectors are stored in the same `graph.db`.

## Step 4: run the standalone token benchmark

This benchmark compares the tokens of all source files in the repository against the tokens
of 5 search hits plus up to 5 neighbour edges per hit, for each of the 5 sample questions.

```bash
uv run python <<'PY'
import json
from pathlib import Path
from code_review_graph.graph import GraphStore
from code_review_graph.token_benchmark import run_token_benchmark

results = {}
for repo in sorted(Path("evaluate/test_repos").iterdir()):
    db = repo / ".code-review-graph" / "graph.db"
    if not db.exists():
        continue
    store = GraphStore(str(db))
    try:
        results[repo.name] = run_token_benchmark(store, repo)
    finally:
        store.close()

print(f"{'Repo':<22}{'naive_tokens':>16}{'avg_graph_tokens':>20}{'ratio':>12}{'ans':>6}")
print("-" * 76)
for name, out in sorted(results.items(), key=lambda x: -x[1]["average_reduction_ratio"]):
    print(f"{name:<22}{out['naive_corpus_tokens']:>16,}"
          f"{out['avg_graph_tokens']:>20,}{out['average_reduction_ratio']:>11.1f}x"
          f"{out['answered_questions']:>4}/"
          f"{out['answered_questions'] + out['unanswered_questions']}")

Path("evaluate/standalone_token_benchmark.json").write_text(json.dumps(results, indent=2))
PY
```

`avg_graph_tokens` and `average_reduction_ratio` come straight from the result dict, and
the second is the first divided into `naive_corpus_tokens`. The `ans` column is
`answered_questions / total`; a run that is not `5/5` has questions the search could not
answer, and those are excluded from the average rather than counted as zero-cost.

## Canonical numbers

<!-- BEGIN canonical-stats -->
Captured **2026-09-16** on macOS arm64 (Apple M4 Pro, 14 cores, 24 GB), Python 3.13.12,
code-review-graph 2.3.8, sentence-transformers 5.6.1, torch 2.13.0, `all-MiniLM-L6-v2`,
`CRG_LEIDEN_SEED=42`, from clean clones at the pinned SHAs. Every benchmark below was
re-run in that one session; no figure on this page is carried over from an older capture.

### How a ratio is computed

Every published ratio is a **ratio of totals or of averages**, never an average of ratios.
For the standalone benchmark that means `naive_corpus_tokens / avg graph_tokens`; for the
eval benchmarks it means `sum(baseline) / sum(graph)` over the rows that succeeded. Divide
the two columns printed beside a ratio and you get the ratio.

This matters because `x -> naive/x` is convex, so by Jensen's inequality
`mean(naive/x_i) >= naive/mean(x_i)`. The two are not interchangeable and the average of
ratios is always the larger. Before 2026-09-16 `token_benchmark.py` published the mean of
the per-question ratios next to columns whose quotient was smaller, overstating every row:

| Repo | published (mean of ratios) | its own columns give | overstated by |
|---|---:|---:|---:|
| httpx | 60.6x | 142,356 / 2,661 = 53.5x | 13.3% |
| flask | 71.0x | 143,594 / 2,196 = 65.4x | 8.6% |
| fastapi | 375.6x | 948,793 / 2,653 = 357.6x | 5.0% |
| express | 36.0x | 136,052 / 3,936 = 34.6x | 4.1% |
| code-review-graph | 68.1x | 208,821 / 3,190 = 65.5x | 4.0% |
| gin | 61.9x | 166,868 / 2,766 = 60.3x | 2.6% |

`run_token_benchmark()` now returns `avg_graph_tokens` alongside `average_reduction_ratio`
so the quotient is checkable from the result dict itself, and reports
`answered_questions` / `unanswered_questions` so a question the search could not answer is
excluded rather than counted as a free win. `token_efficiency.aggregate()` and
`agent_baseline.aggregate()` report `pooled_*_ratio` (a ratio of totals) instead of a mean
of per-row ratios; they keep the median, which is an order statistic and not affected.

### Agent baseline (`code_review_graph/eval/benchmarks/agent_baseline.py`) — the headline

The baseline a real agent pays: grep for the question's identifiers, read the top 3 matching
files. 3 questions per repo, 18 in total, all `status=ok`. Rows:
`evaluate/results/<repo>_agent_baseline_2026-09-16.csv`.

| Repo | Questions | baseline_tokens | graph_tokens | pooled | median |
|---|---:|---:|---:|---:|---:|
| fastapi | 3 | 475,680 | 11,488 | **41.4x** | 50.5x |
| flask | 3 | 82,834 | 9,880 | **8.4x** | 6.8x |
| code-review-graph | 3 | 78,403 | 11,590 | **6.8x** | 6.8x |
| gin | 3 | 75,772 | 11,517 | **6.6x** | 5.7x |
| httpx | 3 | 78,521 | 15,018 | **5.2x** | 5.6x |
| express | 3 | 51,258 | 12,571 | **4.1x** | 3.8x |
| **All 6** | **18** | **842,468** | **72,064** | **11.7x** | **5.9x** |

Per-question ratios, sorted: 3.5, 3.7, 3.8, 3.8, 4.5, 5.6, 5.6, 5.7, 5.8, 6.0, 6.8, 6.8,
9.5, 9.9, 13.7, 25.0, 50.5, 60.0.

**The README headline quotes the median, 5.9x ("about 6x"), not the pooled 11.7x.** fastapi's
three questions contribute 476k of the 842k pooled baseline tokens, so pooling across repos
describes this sample's corpus mix more than it describes a question. Within a single repo
the pooled figure is the right summary and is given per row above.

This is the first published capture of `agent_baseline`. It was added in v2.3.8 and had
never been run against a committed result set.

### Standalone token benchmark (`code_review_graph/token_benchmark.py`) — upper bound

Whole corpus versus a graph answer. **No real agent reads every file to answer one
question**, so this is a ceiling, quoted as one. Each row averages the 5 sample questions
(`how does authentication work`, `what is the main entry point`, `how are database
connections managed`, `what error handling patterns are used`, `how do tests verify core
functionality`); all 5 were answered in all 6 repos.

| Repo | snapshot SHA | naive_corpus_tokens | avg graph_tokens | ratio |
|---|---|---:|---:|---:|
| fastapi | `22381558` | 948,793 | 2,914 | **325.6x** |
| code-review-graph | `84bde354` | 208,821 | 2,547 | **82.0x** |
| flask | `a29f88ce` | 143,594 | 2,712 | **52.9x** |
| gin | `5c00df8a` | 166,868 | 3,521 | **47.4x** |
| httpx | `b55d4635` | 142,356 | 3,411 | **41.7x** |
| express | `b4ab7d65` | 136,052 | 4,422 | **30.8x** |

Range across the 6 repos: 30.8x to 325.6x. The two middle repos are gin (47.4x) and
flask (52.9x), so the median is **50.15x**.

The JSON written by Step 4 lands at `evaluate/standalone_token_benchmark.json`, which is
gitignored, so it is not checked in; the table above is the record. The per-question CSVs
for every eval benchmark *are* checked in, under `evaluate/results/`.
These figures replace the 2026-08-02 capture for two independent reasons: the arithmetic fix
above, and a changed graph. The relative-import fix on `staging` moved edge counts (flask
8,259 to 8,378; gin 17,237 to 17,266; fastapi 32,036 to 32,613), which changes the
neighbour-edge text attached to each search hit — `avg graph_tokens` rose in flask, gin and
httpx and fell in code-review-graph and express. `naive_corpus_tokens` is unchanged in every
repo, as it must be at a pinned SHA.

### Formal `token_efficiency` benchmark (`code_review_graph/eval/benchmarks/token_efficiency.py`)

A different denominator: the changed-file content of each commit against the full
`get_review_context()` JSON. For small commits the response is larger than the input (it
carries impact-radius edges and source snippets), so ratios below 1.0 are expected here.
Per-commit rows are in `evaluate/results/<repo>_token_efficiency_2026-09-16.csv`.

| Repo | commits | median | pooled |
|---|---:|---:|---:|
| fastapi | 2 | 5.8x | 6.0x |
| flask | 2 | 1.3x | 1.3x |
| gin | 3 | 0.7x | 1.0x |
| httpx | 2 | 0.4x | 0.5x |
| code-review-graph | 2 | 0.4x | 0.4x |
| express | 2 | 0.3x | 0.2x |
| **All 6** | **13** | **0.6x** | **2.1x** (634,905 / 306,152) |

All 13 rows are `status=ok`. The median below 1.0 is the honest reading: on a typical commit
in this sample the review context costs more than the diff. It improved sharply since
2026-05-25 — response bounding cut code-review-graph's `graph_tokens` from 215,154 to 25,478
on commit `528801f8`, taking that row from 0.1x to 0.4x — but it has not crossed 1.0.

### Impact accuracy (`code_review_graph/eval/benchmarks/impact_accuracy.py`)

13 commits across the 6 repos, in `evaluate/results/<repo>_impact_accuracy_2026-09-16.csv`.
Every figure is identical to the 2026-08-02 capture to three decimal places.
Each commit is graded in two ground-truth modes, told apart by the `ground_truth_mode`
column:

| Mode | Ground truth | Meaning |
|---|---|---|
| `graph-derived (circular — upper bound)` | changed files plus files with `CALLS` or `IMPORTS_FROM` edges into them, derived from the same graph the predictor traverses | An upper bound. Recall 1.0 is partly true by construction. |
| `co-change (same commit, seed excluded)` | the other files the author touched in the same commit, given one seed file | Independent evidence from git history. Expect much lower recall. |

The graph-derived rows give:

| Metric (graph-derived mode, circular upper bound) | Value |
|---|---|
| Recall (mean across 13 commits) | **1.000** |
| F1 (mean) | **0.693** |
| F1 (median) | 0.667 |
| F1 (min / max) | 0.465 / 1.000 |

The predictor over-predicts on some commits. The worst case is flask `fbb6f0bc`: 33 files
flagged for a 10-file change, precision 0.303. That trade-off is deliberate: a missed
dependency costs more than an extra reviewed file.

The co-change rows are not usable yet: all 11 graded commits came back with
`predicted_files = 0`, so their F1 of 0.000 measures a harness fault, not the predictor. No
co-change number is quoted until that is fixed. The two single-file commits (express) are
recorded with `status=skipped` because there is nothing independent to grade against.

### Multi-hop retrieval (`code_review_graph/eval/benchmarks/multi_hop_retrieval.py`)

11 hand-written tasks across the 6 repos, in
`evaluate/results/<repo>_multi_hop_retrieval_2026-09-16.csv`. Each task is a two-step chain:

1. `hybrid_search(nl_query, limit=k)` looks for an anchor node (default `k` = 10).
2. `query_graph(<traversal_pattern>, target=<anchor>)` walks one hop (`callers_of`,
   `callees_of`, `tests_for`, ...).

A task scores `int(anchor_found) * neighbor_recall`, so 1.0 only when the anchor is in the
top-k and every expected neighbour name comes back. Inspect `anchor_found` and
`neighbor_recall` in the CSV to tell a search miss from a traversal miss.

| Repo | Task | Anchor found | Rank (2026-09-16) | Rank (2026-05-25) | Neighbour recall | Score |
|---|---|---|---:|---:|---:|---:|
| code-review-graph | crg-parse-file-callers | yes | 1 | 0 | 1.00 | **1.00** |
| code-review-graph | crg-upsert-node-callers | yes | 3 | 4 | 1.00 | **1.00** |
| express | express-create-application-callees | yes | 1 | 1 | 1.00 | **1.00** |
| fastapi | fastapi-route-handler-callers | yes | 6 | 6 | 1.00 | **1.00** |
| fastapi | fastapi-get-dependant-callers | no | - (not in top 30) | - | 0.00 | **0.00** |
| flask | flask-dispatch-callers | **no** | **14** | 3 | 0.00 | **0.00** |
| flask | flask-exception-callers | yes | 4 | 5 | 1.00 | **1.00** |
| gin | gin-serve-http-callees | yes | 5 | 5 | 1.00 | **1.00** |
| gin | gin-context-next-callers | yes | 0 | 0 | 1.00 | **1.00** |
| httpx | httpx-client-request-callers | yes | 0 | 0 | 1.00 | **1.00** |
| httpx | httpx-async-request-tests | **no** | **18** | 7 | 0.00 | **0.00** |

**Average score across 11 tasks: 0.727** (8 of 11), down from **0.909** (10 of 11) on
2026-05-25. This is a regression and is published as one.

All three failures are *search* failures, not traversal failures: every traversal that ran
returned `neighbor_recall = 1.0`. Two of them are new. `flask-dispatch-callers` fell from
rank 3 to rank 14 and `httpx-async-request-tests` from rank 7 to rank 18, both outside the
`k = 10` window the benchmark scores. The likely cause is the larger, better-connected graph
produced by the relative-import fix (flask gained 119 edges, httpx 97): more nodes compete
for the same top-10 window and the hybrid score re-ranks. Ranks were re-measured directly
with `hybrid_search(store, nl_query, limit=30)` against the same graphs, which is how the
"not in top 30" entry for `fastapi-get-dependant-callers` was established.

The pre-existing miss, `fastapi-get-dependant-callers`, targets `get_dependant` ("dependant"
with an `a`) from the query "dependency declarations into a tree"; there is no shared
identifier or substring for the search heuristics to use. A fix would need query rewriting or
a richer embedding model.

The first version of this benchmark scored 0.545 (6 of 11). Two changes took it to 0.909 on
2026-05-25:

1. `embeddings.py: _node_to_text()` embeds, per node, the dotted form
   (`APIRoute.get_route_handler`), the identifier split into words (`get route handler`,
   see `_split_identifier()`) and the enclosing module directory, instead of only
   `"{name} {kind} in {parent}"`. Re-embedding is automatic because the text hash changes.
2. `search.py: extract_query_identifiers()` pulls dotted, snake_case and CamelCase tokens
   out of the query; hits whose `qualified_name` contains one are boosted 2.0x. This moved
   `Context.Next` from rank 11 to rank 0.

To add tasks, append `multi_hop_tasks:` entries to a config under
`code_review_graph/eval/configs/`:

```yaml
multi_hop_tasks:
  - id: my-task-id                 # required, unique
    nl_query: "natural language"   # required: what an agent would ask
    anchor_qualified_suffix:       # required: lower-cased suffix of the expected
      "rel/path.py::owner.symbol"  #   qualified_name (case-insensitive endswith)
    traversal_pattern: callers_of  # any query_graph pattern; default callers_of
    expected_neighbor_names:       # required: bare names that must appear
      - "expected_one"
    k: 10                          # optional: top-k depth for the search step
```

### Build stats

| Repo | Nodes | Edges | Embeddings | Edges on 2026-08-02 |
|---|---:|---:|---:|---:|
| fastapi | 6,287 | 32,613 | 5,159 | 32,036 |
| express | 1,990 | 19,492 | 1,849 | 19,492 |
| gin | 1,591 | 17,266 | 1,493 | 17,237 |
| code-review-graph | 1,446 | 9,103 | 1,354 | 9,094 |
| flask | 1,415 | 8,378 | 1,329 | 8,259 |
| httpx | 1,263 | 8,333 | 1,193 | 8,236 |

From the same 2026-09-16 build, read back out of each `graph.db` with
`SELECT count(*) FROM nodes / edges / embeddings`. Flow, community and FTS counts were not
captured in that run. Embeddings are fewer than nodes because File nodes are not embedded.
The edge column moved in five of six repos because of the relative-import fix; express, which
has no Python, is unchanged.
<!-- END canonical-stats -->

## Incremental update latency

The README and diagram 4 quote an incremental-update time. There is no runner for it; it is a
stopwatch on the CLI, so the recipe is written out in full. The timings below are not stored
in the repository.

Corpus: a shallow clone of `django/django` (2,927 `.py` files; the graph indexed 2,998 files,
46,683 nodes, 392,758 edges). Machine: Apple M4 Pro (14 cores, 24 GB), macOS 26.5.2, Python
3.13.12, code-review-graph 2.3.7.

```bash
git clone --depth 1 https://github.com/django/django.git
cd django
/usr/bin/time -p code-review-graph build                 # cold build

/usr/bin/time -p code-review-graph update                # no-op: nothing changed
echo "# edit" >> django/db/models/query.py
echo "# edit" >> django/http/response.py
/usr/bin/time -p code-review-graph update --skip-flows   # the path the hooks run
/usr/bin/time -p code-review-graph update                # full post-processing
```

| Scenario | Wall clock | Files re-parsed |
|---|---:|---:|
| Cold full build | 40.3 s | 2,998 |
| `update`, nothing changed | 1.4 s | 0 |
| `update --skip-flows`, 2 files edited (hook path) | 2.4 to 2.9 s | 2 |
| `update`, 2 files edited (full post-processing) | 9.8 s | 2 |

About 1.4 s of every figure is process start-up (the no-op cost), so a two-file edit on the
hook path costs about 1 s on top. Only the 2 edited files are re-parsed: dependents are
found through import and call edges, but any dependent whose SHA-256 is unchanged is skipped
before parsing (`incremental.py`), so the re-parse count tracks what you edited, not the
size of the dependency cascade.

Earlier versions quoted "under 2 seconds on a ~2,900-file repo". At that size it holds only
for the no-op case; an edit on the hook path is about 2.5 s, and about 10 s with flow and
community detection.

## Agent baseline benchmark (`code_review_graph/eval/benchmarks/agent_baseline.py`)

The whole-corpus baseline in the standalone benchmark is an upper bound no real agent pays.
This benchmark simulates an agent without the graph:

1. Derive search terms from each question in the config's `agent_questions:` list
   (identifier-shaped tokens via `search.extract_query_identifiers()` plus plain keywords;
   falls back to the `search_queries` strings when absent).
2. Grep the corpus in pure Python (no external `rg` or `grep`), ranking files by total
   case-insensitive match count; ties break on path.
3. Read the top 3 files (`agent_baseline_top_k` in the config) and count their tokens
   (`chars / 4`) as `baseline_tokens`.
4. Compare with the graph-query cost for the same question: 5 hybrid search hits plus up to
   5 neighbour edges per hit, the same accounting as the standalone benchmark.

Output: `evaluate/results/<repo>_agent_baseline_<date>.csv` with a `baseline_to_graph_ratio`
per question. Rows where either side is zero get `status=no_graph_results` or
`status=no_baseline_match` and are excluded from `agent_baseline.aggregate()`. The canonical
capture is 2026-09-16; its table is in "Canonical numbers" above, and it is what the README
headline quotes.

## Weekly CI run (report-only)

`.github/workflows/eval.yml` runs every Monday at 06:23 UTC, and on `workflow_dispatch`,
against the two smallest pinned configs (`httpx`, `flask`) with `token_efficiency`,
`impact_accuracy` and `agent_baseline`. It uploads the CSVs as an artifact and writes a
job-summary table. Regressions do not fail the default branch.

## Which benchmark measures what

| Benchmark | Baseline | Graph cost | Question |
|---|---|---|---|
| `code_review_graph/eval/benchmarks/token_efficiency.py` | changed-file content of one commit | full `get_review_context()` JSON | Is the graph cheaper than reading the diffed files? |
| `code_review_graph/eval/benchmarks/agent_baseline.py` | grep, top 3 files for the question's identifiers | 5 search hits + 5 neighbour edges per hit | Is the graph cheaper than a grep-and-read agent? |
| `code_review_graph/eval/token_benchmark.py` | none; absolute cost | sum of the tool responses in simulated review, architecture and debug workflows | What does a complete agent workflow cost? |
| `code_review_graph/token_benchmark.py` (standalone) | all source files in the repo | 5 search hits + 5 neighbour edges per hit | Is the graph cheaper than reading the whole repo? |

`token_efficiency` is below 1.0x on most commits (2026-09-16 median 0.6x). The standalone
numbers are always large because the baseline is the whole repo. The README therefore leads
with `agent_baseline` (median 5.9x, "about 6x"), quotes the whole-corpus figure as a clearly
labelled upper bound (median 50.15x), and quotes `token_efficiency` in Limitations as the case
where the graph costs more than the diff. Quote the one that matches the scenario.

## Generating diagrams

The 9 PNGs in `diagrams/` are produced from `diagrams/generate_diagrams.py`. The
`.excalidraw` sources are gitignored (`*.excalidraw` in `.gitignore`); only the PNGs are
tracked. After a benchmark refresh:

```bash
uv run python diagrams/generate_diagrams.py
# Open each .excalidraw at https://excalidraw.com to render and export
```

**The export step is manual, so the PNGs lag the numbers.** `generate_diagrams.py` carries
the 2026-09-16 figures; diagrams 1, 4, 5 and 6 still show older ones until someone re-exports
them by hand. Until then the README's `alt` text and prose are the authority — they carry the
current numbers — and the PNG is a picture. If you are checking a figure, check the tables,
not the images.

## Troubleshooting

**`git clone failed`**: network or upstream rate limit. Retry; the eval does not retry on
its own.

**`git checkout <sha> failed`**: upstream rewrote history or removed the SHA. File an issue
with the failing config so it can be re-pinned.

**`No embeddings found in this graph`** during the standalone benchmark: run Step 3.

**Different community IDs between runs**: check `grep _LEIDEN_SEED
code_review_graph/communities.py` and that everyone uses the same `CRG_LEIDEN_SEED`.

**Different `naive_corpus_tokens` from the canonical table**: `git rev-parse HEAD` inside
`evaluate/test_repos/<name>` must match the `commit:` field of the config. If not, delete
the clone and let Step 2 re-clone at the pinned SHA.
