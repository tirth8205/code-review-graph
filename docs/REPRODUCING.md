# Reproducing the Benchmarks

This document gives the exact commands to reproduce every benchmark number
shown in the README and the `diagrams/`. Two people running the recipe below
on different machines on different days should produce identical numbers,
within float rounding.

If you get different numbers, that's a bug — please file an issue.

## Verifying the "saved tokens" number

The CLI's `Token Savings` panel uses a `chars / 4` approximation labelled
`estimated: true`, not a model-specific tokenizer. The approximation is
designed to be both fast (no model load, no inference) and conservative.

### How to verify against a real tokenizer

```bash
pip install tiktoken
code-review-graph detect-changes --brief --verify
```

The panel grows a `Verified (tiktoken)` row showing the same calculation
done with OpenAI's `cl100k_base` tokenizer (the GPT-4 family). If the
estimate is significantly off, you'll see it immediately:

```text
┌───────────────────────── Token Savings ─────────────────────────┐
│ Full context would be:     12,921 tokens                        │
│ Graph context used:           762 tokens                        │
│ Saved:                     12,159 tokens (~94%)                 │
│ Verified (tiktoken):       10,835 tokens (~93%)  [11,611 → 776] │
│ Breakdown: Functions 244 · Tests 191 · Risk 244 · Other 83      │
└─────────────────────────────────────────────────────────────────┘
```

### Calibration result (committed)

A one-time calibration across 222 files / 2.2 MB of mixed source
(Python, JS, TS, Go, Rust, RST, MD) pulled from the 6 test repos:

| Repo | sample files | bytes | chars/4 estimate | tiktoken real | ratio est/real |
|---|---:|---:|---:|---:|---:|
| flask | 46 | 470,179 | 117,559 | 109,969 | 1.069 |
| fastapi | 38 | 156,224 | 39,072 | 34,897 | 1.120 |
| gin | 30 | 471,793 | 117,962 | 132,296 | 0.892 |
| express | 23 | 296,805 | 74,207 | 83,575 | 0.888 |
| httpx | 38 | 254,184 | 63,556 | 62,909 | 1.010 |
| code-review-graph | 47 | 539,206 | 134,820 | 120,760 | 1.116 |
| **OVERALL** | **222** | **2,188,391** | **547,176** | **544,406** | **1.005** |

`chars / 4` is within **+0.5%** of real GPT-4 tokens in aggregate. Per-repo
it swings between **-11%** (gin: lots of short Go identifiers) and **+12%**
(fastapi: heavy docstrings and type hints), but the **ratio** stabilizes
because both sides of the divide are equally biased.

Reproduce the calibration with the snippet in this commit's
`code_review_graph/context_savings.py:verify_with_tiktoken`, or
inline-run the `--verify` flag on any commit.

## What is and isn't deterministic

| Reproducible | Reason |
|---|---|
| Tree-sitter parsing | Pure function of input bytes |
| Node / edge counts | Deterministic upserts keyed by `qualified_name` |
| FTS5 BM25 scores | Deterministic |
| Embeddings via `all-MiniLM-L6-v2` on CPU | Model weights cache-pinned by SHA in HuggingFace cache |
| Leiden community IDs | Seeded — `_LEIDEN_SEED=42` in `communities.py`, override with `CRG_LEIDEN_SEED` env var |
| `naive_corpus_tokens` | Deterministic for a fixed git checkout |
| `git clone` at a pinned SHA | Determines the source-of-truth byte stream |

What used to make it **non**-reproducible (now fixed):

- `commit: HEAD` in every `code_review_graph/eval/configs/*.yaml` — replaced with the pinned latest test-commit SHA per repo
- `git clone --depth 50` silently fell back to wrong commits when the pinned SHAs were beyond the shallow window — now uses full clones with explicit `returncode` checks
- Leiden ran with an unseeded RNG — now seeded
- `nextjs.yaml` was a misnamed config evaluating this repo — renamed to `code-review-graph.yaml`
- FTS5 was created but never populated by the eval framework's `full_build` call — `code_review_graph/eval/runner.py` now calls `postprocessing.run_post_processing` directly

## Prerequisites

- Python 3.10 or newer
- `git` on PATH
- Network access (~600 MB to clone the 6 upstream repos)
- ~3 GB free disk
- For the embedding step: roughly 700 MB extra for `torch` + `sentence-transformers`

## Step 1 — Install with the right extras

```bash
git clone https://github.com/tirth8205/code-review-graph
cd code-review-graph

# eval extras: pyyaml + matplotlib (matplotlib only needed for `--report`)
# embeddings extras: sentence-transformers + numpy
uv sync --extra eval --extra embeddings     # or: pip install -e ".[eval,embeddings]"
```

## Step 2 — Run the formal eval

This step clones 6 upstream repositories at pinned SHAs, builds a full graph
for each (parser + cross-file resolvers + signatures + FTS5 + flows + Leiden
communities), then runs the `token_efficiency`, `impact_accuracy`,
`agent_baseline`, and `multi_hop_retrieval` benchmarks.

```bash
uv run code-review-graph eval \
  --benchmark token_efficiency,impact_accuracy,agent_baseline,multi_hop_retrieval
```

Failure semantics (applies to every benchmark): a thrown tool call is **not**
a measurement. The row is kept in the CSV with `status=error` for forensics,
but excluded from every aggregate. (Two historical bugs made failures look
like wins: a thrown `get_review_context` produced `graph_tokens=0` and a
ratio of `naive/1`, and a thrown `analyze_changes` silently set
`predicted = changed`, guaranteeing recall 1.0. Both are fixed; regression
tests live in `tests/test_eval.py`.)

Expected runtime on an M1/M2 Mac: roughly 8–15 minutes for the build phase,
plus seconds per benchmark.

Outputs:

- `evaluate/test_repos/{express,fastapi,flask,gin,httpx,code-review-graph}/`
- `evaluate/test_repos/<name>/.code-review-graph/graph.db`
- `evaluate/results/<name>_<benchmark>_<date>.csv`

## Step 3 — Generate embeddings (required for the standalone benchmark)

The standalone token benchmark ships with 5 hardcoded natural-language
questions. Without embeddings, hybrid search can't match them and the
benchmark silently returns 0× reduction ratios (a loud warning will print).

```bash
for repo in express fastapi flask gin httpx code-review-graph; do
  uv run code-review-graph embed --repo "evaluate/test_repos/$repo"
done
```

Expected runtime: 2–5 minutes total. Vectors live inside the same `graph.db`.

## Step 4 — Run the standalone token benchmark

This benchmark compares **all source-file tokens** in the repo against
**5 search hits + a few neighbor edges** for each of 5 sample questions. The
ratio answers: *how many tokens does the graph let me skip on a typical
question?*

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

print(f"{'Repo':<22}{'naive_tokens':>16}{'avg_graph_tokens':>20}{'avg_ratio':>14}")
print("-" * 72)
for name, out in sorted(results.items(), key=lambda x: -x[1]["average_reduction_ratio"]):
    pq = out["per_question"]
    avg_graph = int(sum(r["graph_tokens"] for r in pq) / max(len(pq), 1))
    print(f"{name:<22}{out['naive_corpus_tokens']:>16,}"
          f"{avg_graph:>20,}{out['average_reduction_ratio']:>13.1f}×")

Path("evaluate/standalone_token_benchmark.json").write_text(json.dumps(results, indent=2))
PY
```

## Canonical numbers

<!-- BEGIN canonical-stats -->
Captured **2026-05-25** on macOS arm64, Python 3.11, sentence-transformers 5.5.1,
`all-MiniLM-L6-v2`, `CRG_LEIDEN_SEED=42`. If your numbers differ by more than
rounding, something in the chain has drifted — file an issue.

### Standalone token benchmark (`code_review_graph/token_benchmark.py`)

Each row is the average of 5 sample questions (`how does authentication work`,
`what is the main entry point`, `how are database connections managed`,
`what error handling patterns are used`, `how do tests verify core functionality`).

| Repo | snapshot SHA | naive_corpus_tokens | avg graph_tokens | avg ratio |
|---|---|---:|---:|---:|
| fastapi | `0227991a` | 951,071 | 2,169 | **528.4×** |
| code-review-graph | `84bde354` | 208,821 | 2,495 | **93.0×** |
| gin | `5c00df8a` | 166,868 | 1,990 | **91.8×** |
| flask | `a29f88ce` | 125,022 | 1,986 | **71.4×** |
| express | `b4ab7d65` | 135,955 | 3,465 | **40.6×** |
| httpx | `b55d4635` | 89,492 | 2,438 | **38.0×** |

Range across 6 repos: **38× – 528×**. The numbers shifted down from a
previous capture because (a) the test repos are now wiped/re-cloned from
scratch — no leftover build artifacts or local caches inflate the naive
baseline; and (b) the embedding text per node became richer in this same
release (see `embeddings._node_to_text`), so the graph response itself is
slightly bigger. Both are correctness improvements over the prior numbers.

### Formal `token_efficiency` benchmark (`code_review_graph/eval/benchmarks/token_efficiency.py`)

A different denominator: just the **changed-file content** for each commit,
vs the full `get_review_context()` JSON. For small commits the response is
larger than the input (it carries impact-radius edges + source snippets), so
ratios here are intentionally < 1.0 — that is not a bug, it measures a
different thing than the standalone benchmark.

Raw per-commit CSVs in `evaluate/results/<repo>_token_efficiency_*.csv`.

### Impact accuracy (`code_review_graph/eval/benchmarks/impact_accuracy.py`)

13 commits across 6 repos. The benchmark emits two ground-truth modes side
by side, distinguished by the `ground_truth_mode` CSV column:

| Mode | Ground truth | What it tells you |
|---|---|---|
| `graph-derived (circular — upper bound)` | changed files + files with CALLS/IMPORTS_FROM edges into them — **derived from the same graph the predictor traverses** | An upper bound. Recall 1.0 here is partly true by construction, not independent evidence. |
| `co-change (same commit, seed excluded)` | the *other* files the author actually touched in the same commit, given a single seed file | Independent-ish evidence from git history. Expect substantially lower recall. |

The canonical numbers below were captured **in graph-derived mode only**
(the co-change mode did not exist at capture time). Treat the recall row as
a circular upper bound, not as "100% recall":

| Metric (graph-derived mode — circular upper bound) | Value |
|---|---|
| Recall (mean across 13 commits) | **1.000** (upper bound on every commit) |
| F1 (mean) | **0.714** |
| F1 (median) | 0.667 |
| F1 (min / max) | 0.455 / 1.000 |

Canonical co-change numbers will be added after the next full capture — we
do not quote them before measuring. Single-file commits are recorded with
`status=skipped` in co-change mode (there is nothing independent to grade
against).

The blast-radius analysis over-predicts in some commits (precision ≈ 0.30 in the
worst case, where 34 files are flagged for a 10-file change). That is
intentional: a missed dependency is worse than an extra reviewed file.

### Multi-hop retrieval (`code_review_graph/eval/benchmarks/multi_hop_retrieval.py`)

11 hand-curated tasks across the 6 repos. Each task is a 2-step tool chain:

1. `hybrid_search(nl_query, limit=10)` looks for a starting anchor node.
2. `query_graph(<traversal_pattern>, target=<anchor>)` walks one hop along
   `callers_of` / `callees_of` / `tests_for` / `imports_of` / etc.

The task **scores 1.0** only if both the anchor is found in the top-K *and*
the expected neighbor names are returned by the traversal. **Scores 0.0**
otherwise (which collapses both "search missed the anchor" and "traversal
returned the wrong set" — split those by inspecting `anchor_found` and
`neighbor_recall` in the per-task CSV row).

| Repo | Task | Anchor found | Rank | Neighbor recall | Score |
|---|---|---|---:|---:|---:|
| code-review-graph | crg-parse-file-callers | yes | 0 | 1.00 | **1.00** |
| code-review-graph | crg-upsert-node-callers | yes | 4 | 1.00 | **1.00** |
| express | express-create-application-callees | yes | 1 | 1.00 | **1.00** |
| fastapi | fastapi-route-handler-callers | yes | 6 | 1.00 | **1.00** |
| fastapi | fastapi-get-dependant-callers | no | — | 0.00 | **0.00** |
| flask | flask-dispatch-callers | yes | 3 | 1.00 | **1.00** |
| flask | flask-exception-callers | yes | 5 | 1.00 | **1.00** |
| gin | gin-serve-http-callees | yes | 5 | 1.00 | **1.00** |
| gin | gin-context-next-callers | yes | 0 | 1.00 | **1.00** |
| httpx | httpx-client-request-callers | yes | 0 | 1.00 | **1.00** |
| httpx | httpx-async-request-tests | yes | 7 | 1.00 | **1.00** |

**Average score across 11 tasks: 0.909**. 10/11 tasks pass; the one remaining
miss (`fastapi-get-dependant-callers`) targets a function spelled `get_dependant`
("dependant" with an `a`) from a query phrased as "dependency declarations into
a tree" — there is no lexical overlap and no extractable identifier in the
query for the boosting heuristic to lock onto. Left as an honest miss; the
fix would be either query rewriting or a richer embedding model.

#### How the score went from 0.545 to 0.909 (the same-day fix)

The v1 scaffold first scored **0.545** (6/11). Two changes brought it to
**0.909** (10/11), both deterministic, both small, both committed in this
same session:

1. **`embeddings.py:_node_to_text`** — the embedded text per node used to be
   just `"{name} {kind} in {parent}"`. It now also includes the dotted form
   (`APIRoute.get_route_handler`), the identifier split into words
   (`get route handler`), and the enclosing module directory (`routing`,
   `fastapi`, `dependencies`). All re-embeddings are automatic — the text
   hash changes, `EmbeddingStore.embed_nodes` re-embeds. See
   `_split_identifier` for the casing/separator rules.

2. **`search.py:extract_query_identifiers`** — natural-language queries
   like "Who advances the gin middleware chain via Context.Next" now have
   their dotted / snake_case / CamelCase identifier tokens extracted. Search
   results whose `qualified_name` contains any extracted identifier get a
   2.0× boost. This pushed `Context.Next` from rank 11 to rank 0.

The remaining `fastapi-get-dependant-callers` failure cannot be fixed by
either change because the query doesn't share any identifier or substring
with the target — that's the boundary of the heuristic.

This benchmark is a v1 scaffold (11 tasks). The intent is to track the
**multi-hop tool chain** as the agent's actual usage pattern rather than just
single-shot retrieval. Adding more tasks: append `multi_hop_tasks:` entries
to any config under `code_review_graph/eval/configs/*.yaml` with the schema:

```yaml
multi_hop_tasks:
  - id: my-task-id                # required, unique
    nl_query: "natural language" # required, what an agent would ask
    anchor_qualified_suffix:     # required, lowercased suffix of expected
      "rel/path.py::owner.symbol" #   qualified_name (case-insensitive endswith)
    traversal_pattern: callers_of # one of callers_of|callees_of|imports_of|
                                  # importers_of|tests_for|inheritors_of|children_of
    expected_neighbor_names:      # required, list of bare names that should
      - "expected_one"            #   appear in the traversal result
    k: 10                         # optional, top-K depth for the search step
```

### Build stats

| Repo | Nodes | Edges | Flows | Communities | Embeddings | FTS idx rows |
|---|---:|---:|---:|---:|---:|---:|
| fastapi | 6,292 | 32,081 | 165 | 85 | 5,164 | 127 |
| express | 1,912 | 18,877 | 4 | 7 | 1,771 | 47 |
| gin | 1,589 | 17,237 | 114 | 41 | 1,491 | 29 |
| code-review-graph | 1,418 | 8,877 | 104 | 11 | 1,326 | 38 |
| flask | 1,415 | 8,259 | 78 | 13 | 1,329 | 35 |
| httpx | 1,261 | 8,228 | 128 | 5 | 1,193 | 34 |

Embeddings count is lower than node count because File nodes aren't
embedded. FTS idx rows are far lower than node count because FTS5 stores
inverted-index segments, not one row per indexed document.
<!-- END canonical-stats -->

## Agent baseline benchmark (`code_review_graph/eval/benchmarks/agent_baseline.py`)

The whole-corpus baseline in the standalone token benchmark is an upper
bound no real agent pays. This benchmark simulates what an agent actually
does without the graph:

1. Derive search terms from each question in the config's `agent_questions:`
   list (identifier-shaped tokens via `search.extract_query_identifiers`,
   plus plain keywords; falls back to the `search_queries` query strings
   when absent).
2. Pure-python grep over the corpus (no external `rg`/`grep` binary),
   ranking source files by total case-insensitive match count
   (deterministic; ties break on path).
3. Read the top-3 files and token-count them (`chars/4`) as
   `baseline_tokens`.
4. Compare against the graph-query cost for the same question (5 hybrid
   search hits + up to 5 neighbor edges per hit — the same accounting as the
   standalone benchmark).

Output: `evaluate/results/<repo>_agent_baseline_<date>.csv` with a
`baseline_to_graph_ratio` per question. Rows where either side is zero are
marked `status=no_graph_results` / `status=no_baseline_match` and excluded
from aggregates (`agent_baseline.aggregate`). No canonical capture exists
yet; numbers will be added to the canonical block above once captured —
they are not quoted before being measured.

## Weekly CI run (report-only)

`.github/workflows/eval.yml` runs every Monday at 06:23 UTC (plus manual
`workflow_dispatch`) against the two smallest pinned configs (`httpx`,
`flask`) with the `token_efficiency`, `impact_accuracy`, and
`agent_baseline` benchmarks. It uploads the CSVs as an artifact and writes
a job-summary table. It is deliberately **report-only**: regressions do not
fail the default branch yet.

## Which benchmark measures what

There are four different "token" benchmarks in the repo. They are all valid
but measure different scenarios:

| Benchmark | Naive baseline | Graph cost | Question answered |
|---|---|---|---|
| `code_review_graph/eval/benchmarks/token_efficiency.py` | sum of **changed-file content** for a specific commit | full `get_review_context()` JSON | "Is the graph cheaper than just reading the diffed files?" |
| `code_review_graph/eval/benchmarks/agent_baseline.py` | **grep top-3 files** for the question's identifiers | 5 search hits + 5 neighbor edges per question | "Is the graph cheaper than a realistic grep-and-read agent?" |
| `code_review_graph/eval/token_benchmark.py` | none — absolute per-workflow cost | sum of 5 MCP-tool responses | "How many tokens does a complete agent workflow cost?" |
| `code_review_graph/token_benchmark.py` (standalone) | sum of **all source files** in repo | 5 search hits + 5 neighbor edges per question | "Is the graph cheaper than reading the whole repo?" |

The `code_review_graph/eval/benchmarks/token_efficiency.py` numbers can be **less than 1.0×**
for small commits (`get_review_context` carries impact-radius metadata and
source snippets, which outweigh a tiny changed-file set). The standalone
benchmark numbers are **always large** because the baseline is the entire
repo — that is why the README leads with the median (~82×) and treats 528×
as the max, and why `agent_baseline` exists as the realistic middle ground.
Pick the one that matches the scenario you're talking about.

## Generating diagrams

The 9 diagrams in `diagrams/` are produced from `diagrams/generate_diagrams.py`.
Excalidraw source files (`.excalidraw`) are gitignored (`*.excalidraw` line in
`.gitignore`); only the rendered PNGs are tracked. Regenerate after a
benchmark refresh:

```bash
uv run python diagrams/generate_diagrams.py
# Open each .excalidraw at https://excalidraw.com to render/export
```

## Troubleshooting

**`git clone failed`** — Network or upstream rate-limit. The fix is a clean
retry; the eval doesn't auto-retry by design (loud failures > silent
fallback).

**`git checkout <sha> failed`** — Upstream rewrote history or removed the
SHA. File an issue with the failing config so we can re-pin.

**`No embeddings found in this graph`** warning during the standalone
benchmark — you skipped Step 3. Run it.

**Different community IDs between runs** — Make sure you're on the seeded
`communities.py`. Check `grep _LEIDEN_SEED code_review_graph/communities.py`.
You can override the seed via `CRG_LEIDEN_SEED=<int>` but all collaborators
must agree on the same value.

**Different `naive_corpus_tokens` than the canonical table** — Make sure
`git rev-parse HEAD` inside each `evaluate/test_repos/<name>` matches the
`commit:` field in the corresponding config file. If not, delete the clone
and let Step 2 re-clone at the pinned SHA.
