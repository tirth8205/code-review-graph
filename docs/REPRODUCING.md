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
  --benchmark token_efficiency,impact_accuracy,agent_baseline,multi_hop_retrieval
```

`--embed` is required for `agent_baseline` and `multi_hop_retrieval`; without it their
natural-language questions hit FTS5 only and return nothing. `code-review-graph eval --help`
lists the remaining benchmarks (`flow_completeness`, `search_quality`, `build_performance`,
`incremental_fidelity`) and flags.

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

print(f"{'Repo':<22}{'naive_tokens':>16}{'avg_graph_tokens':>20}{'avg_ratio':>14}")
print("-" * 72)
for name, out in sorted(results.items(), key=lambda x: -x[1]["average_reduction_ratio"]):
    pq = out["per_question"]
    avg_graph = int(sum(r["graph_tokens"] for r in pq) / max(len(pq), 1))
    print(f"{name:<22}{out['naive_corpus_tokens']:>16,}"
          f"{avg_graph:>20,}{out['average_reduction_ratio']:>13.1f}x")

Path("evaluate/standalone_token_benchmark.json").write_text(json.dumps(results, indent=2))
PY
```

## Canonical numbers

<!-- BEGIN canonical-stats -->
Captured 2026-08-02 on macOS arm64 (Apple M4 Pro, 14 cores, 24 GB), Python 3.13.12,
code-review-graph 2.3.7, sentence-transformers 5.6.1, `all-MiniLM-L6-v2`,
`CRG_LEIDEN_SEED=42`, from clean clones at the pinned SHAs.

### Standalone token benchmark (`code_review_graph/token_benchmark.py`)

Each row averages the 5 sample questions (`how does authentication work`, `what is the main
entry point`, `how are database connections managed`, `what error handling patterns are
used`, `how do tests verify core functionality`).

| Repo | snapshot SHA | naive_corpus_tokens | avg graph_tokens | avg ratio |
|---|---|---:|---:|---:|
| fastapi | `22381558` | 948,793 | 2,653 | **375.6x** |
| flask | `a29f88ce` | 143,594 | 2,196 | **71.0x** |
| code-review-graph | `84bde354` | 208,821 | 3,190 | **68.1x** |
| gin | `5c00df8a` | 166,868 | 2,766 | **61.9x** |
| httpx | `b55d4635` | 142,356 | 2,661 | **60.6x** |
| express | `b4ab7d65` | 136,052 | 3,936 | **36.0x** |

`avg ratio` is the benchmark's `average_reduction_ratio`: the mean of the five per-question
`naive_total / graph_tokens` ratios. It is not `naive_corpus_tokens / avg graph_tokens`, and
it always reads higher than that division, so the three columns do not divide out. Range
across the 6 repos on this measure: 36x to 376x, median about 65x. The README divides the
two token columns instead and reports 35x to 358x, median about 63x.

The JSON written by Step 4 for this capture is not checked in; the same
`naive_corpus_tokens` and `avg graph_tokens` figures appear in the README. They replace the 2026-05-25 capture and every ratio is lower, for two reasons
confirmed by re-running from clean clones: `avg graph_tokens` rose in every repo because the
per-node embedding text grew, so a 5-hit response carries more text; and fastapi is measured
at its current pin `22381558` instead of the retired `0227991a`. `naive_corpus_tokens` is
unchanged for `code-review-graph` and `gin`, so the movement is on the graph-response side.

### Formal `token_efficiency` benchmark (`code_review_graph/eval/benchmarks/token_efficiency.py`)

A different denominator: the changed-file content of each commit against the full
`get_review_context()` JSON. For small commits the response is larger than the input (it
carries impact-radius edges and source snippets), so ratios below 1.0 are expected here.
Per-commit rows are in `evaluate/results/<repo>_token_efficiency_*.csv`.

### Impact accuracy (`code_review_graph/eval/benchmarks/impact_accuracy.py`)

13 commits across the 6 repos, in `evaluate/results/<repo>_impact_accuracy_2026-08-02.csv`.
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
`evaluate/results/<repo>_multi_hop_retrieval_2026-05-25.csv`. Each task is a two-step chain:

1. `hybrid_search(nl_query, limit=k)` looks for an anchor node (default `k` = 10).
2. `query_graph(<traversal_pattern>, target=<anchor>)` walks one hop (`callers_of`,
   `callees_of`, `tests_for`, ...).

A task scores `int(anchor_found) * neighbor_recall`, so 1.0 only when the anchor is in the
top-k and every expected neighbour name comes back. Inspect `anchor_found` and
`neighbor_recall` in the CSV to tell a search miss from a traversal miss.

| Repo | Task | Anchor found | Rank | Neighbour recall | Score |
|---|---|---|---:|---:|---:|
| code-review-graph | crg-parse-file-callers | yes | 0 | 1.00 | **1.00** |
| code-review-graph | crg-upsert-node-callers | yes | 4 | 1.00 | **1.00** |
| express | express-create-application-callees | yes | 1 | 1.00 | **1.00** |
| fastapi | fastapi-route-handler-callers | yes | 6 | 1.00 | **1.00** |
| fastapi | fastapi-get-dependant-callers | no | - | 0.00 | **0.00** |
| flask | flask-dispatch-callers | yes | 3 | 1.00 | **1.00** |
| flask | flask-exception-callers | yes | 5 | 1.00 | **1.00** |
| gin | gin-serve-http-callees | yes | 5 | 1.00 | **1.00** |
| gin | gin-context-next-callers | yes | 0 | 1.00 | **1.00** |
| httpx | httpx-client-request-callers | yes | 0 | 1.00 | **1.00** |
| httpx | httpx-async-request-tests | yes | 7 | 1.00 | **1.00** |

**Average score across 11 tasks: 0.909** (10 of 11). The miss,
`fastapi-get-dependant-callers`, targets `get_dependant` ("dependant" with an `a`) from the
query "dependency declarations into a tree"; there is no shared identifier or substring for
the search heuristics to use. It is left as a miss; a fix would need query rewriting or a
richer embedding model.

The first version of this benchmark scored 0.545 (6 of 11). Two changes took it to 0.909:

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

| Repo | Nodes | Edges | Embeddings |
|---|---:|---:|---:|
| fastapi | 6,287 | 32,036 | 5,159 |
| express | 1,990 | 19,492 | 1,849 |
| gin | 1,589 | 17,237 | 1,491 |
| code-review-graph | 1,446 | 9,094 | 1,354 |
| flask | 1,415 | 8,259 | 1,329 |
| httpx | 1,263 | 8,236 | 1,193 |

From the same 2026-08-02 build; the raw output is not checked in. Flow, community and FTS
counts were not captured in that run. Embeddings are fewer than nodes because File nodes are
not embedded.
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
`status=no_baseline_match` and are excluded from `agent_baseline.aggregate()`. No canonical
capture exists yet; numbers will be added above once measured.

## Incremental fidelity (`code_review_graph/eval/benchmarks/incremental_fidelity.py`)

A persistent graph rots invisibly. Nothing errors, caller lists quietly get shorter, and a
reviewer gets a confidently incomplete answer. A clean rebuild of the same tree is a free
and perfect oracle for that drift, so this benchmark uses it.

For each of seven edit kinds the benchmark copies the repository's tracked files into a
throwaway git tree, builds a clean graph, applies the edit, commits it, runs
`incremental_update` plus the post-processing `build_or_update_graph` runs, builds a second
clean graph from the same edited tree, and compares the two databases table by table.

```bash
uv run code-review-graph eval --repo code-review-graph --benchmark incremental_fidelity
```

Row ids are autoincrement and differ between two builds of the same tree, so every
projection is keyed on something stable (a qualified name, a community name, a flow's path
resolved to qualified names) and excludes ids and wall-clock columns. `nodes_fts` is
compared through `fts5vocab`, which reads the FTS index itself rather than the `nodes`
content table behind it.

### Results on this repository

323 files, 6,509 nodes, 57,042 edges, 186 flows. Differing rows per table, incremental
update against clean rebuild:

| Edit | Status | Total | nodes | node_community | edges | communities | flows | flow_memberships | nodes_fts | community_summaries | flow_snapshots | risk_index | metadata | Seconds |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| trailing_comment | known_failure | 7,237 | 0 | 412 | 6 | 0 | 58 | 6,702 | 0 | 1 | 58 | 0 | 0 | 11.0 |
| rename_function | known_failure | 7,233 | 0 | 412 | 6 | 0 | 58 | 6,698 | 0 | 1 | 58 | 0 | 0 | 9.4 |
| delete_file | known_failure | 10,506 | 0 | 0 | 3,377 | 2 | 72 | 6,979 | 0 | 2 | 72 | 2 | 0 | 9.8 |
| add_file | known_failure | 3 | 0 | 1 | 0 | 1 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 6.7 |
| move_function | known_failure | 8,391 | 0 | 519 | 6 | 1 | 93 | 7,678 | 0 | 1 | 93 | 0 | 0 | 12.5 |
| change_import | known_failure | 7,237 | 0 | 412 | 6 | 0 | 58 | 6,702 | 0 | 1 | 58 | 0 | 0 | 10.5 |
| revert | known_failure | 7,237 | 0 | 412 | 6 | 0 | 58 | 6,702 | 0 | 1 | 58 | 0 | 0 | 9.8 |

Benchmark wall time 79.1 s (8 clean builds plus 8 incremental updates).

`nodes` and `nodes_fts` match everywhere: node rows and the search index are faithful. Every
other divergence traces to one of three defects, none of which is fixed here.

**1. `flow_memberships` is orphaned by every re-parse.** `flow_memberships.node_id` is a
bare integer with no foreign key and no cascade, and `GraphStore._replace_file_data` deletes
and re-inserts a re-parsed file's nodes with fresh ids. After appending one comment to
`code_review_graph/parser.py`, 1,595 of the incremental graph's 4,494 membership rows point
at node ids that no longer exist; the rebuild has 0. Because `incremental_trace_flows` finds
affected flows by joining `flow_memberships` to `nodes`, the dangling rows are invisible to
the repair path as well, so a full rebuild is the only way back.

**2. Community assignment is never restored, and the repair path cannot fire.** The same
edit leaves 718 nodes with `community_id IS NULL` against the rebuild's 306.
`incremental_detect_communities` and `incremental_trace_flows` both match `nodes.file_path`
(absolute) against `incremental_update`'s `changed_files` (repo-relative), so both always
count zero affected rows and skip. `community_summaries` inherits the error: after the same
edit its `key_symbols` for `code-review-graph-name` read
`["main", "GraphStore", "close", "upsert_node", "commit"]` against the rebuild's
`["CodeParser", "main", "GraphStore", "close", "NodeInfo"]`.

**3. Deleting a file silently shortens other files' caller lists.** Deleting
`code_review_graph/graph.py` leaves the incremental graph with 52,913 edges against the
rebuild's 56,272, and 3,377 edge rows differ. `code_review_graph/analysis.py::find_bridge_nodes`
has no outgoing `CALLS` edge at all at lines 73 and 100 in the incremental graph; the
rebuild has two (`_build_networkx_graph` and `_sanitize_name`). Those edges had been
resolved into `graph.py`, were removed with it, and `analysis.py` was never re-parsed to
restore them as bare calls. Two edges still target `graph.py::` names that have no node.
`risk_index` inherits it: `parser.py::EdgeInfo` reports `caller_count` 268 against the
rebuild's 267.

The six `edges` rows that differ on every edit are a separate, smaller issue: the
`ambiguous_targets` list inside an edge's `extra` JSON is ordered by node id, so re-parsing a
file reorders it. Same set, different order, so the graph is not byte-reproducible.

Every edit kind is listed in `incremental_fidelity.KNOWN_FAILURES`, which keeps the run from
reporting them as unexpected. `tests/test_incremental_fidelity.py` guards against a kind that
currently passes starting to diverge, and carries two `xfail` tests that document defects 1
and 2 at the level of the defect.

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
| `code_review_graph/eval/benchmarks/incremental_fidelity.py` | a clean rebuild of the same tree | the same graph after `incremental_update` | Does an incremental update equal a rebuild? |

`token_efficiency` can be below 1.0x for small commits. The standalone numbers are always
large because the baseline is the whole repo, which is why the README leads with the median
(about 65x), treats 376x as the maximum, and points to `agent_baseline` as the realistic
middle ground. Quote the one that matches the scenario.

## Generating diagrams

The 9 PNGs in `diagrams/` are produced from `diagrams/generate_diagrams.py`. The
`.excalidraw` sources are gitignored (`*.excalidraw` in `.gitignore`); only the PNGs are
tracked. After a benchmark refresh:

```bash
uv run python diagrams/generate_diagrams.py
# Open each .excalidraw at https://excalidraw.com to render and export
```

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

## The real-repository parser corpus

Every other parser test writes a small fixture that exercises one construct. A regression
that only shows on real code — a resolver that stops resolving, an ignore rule that swallows
a source tree, a grammar that raises on a real file — passes all of them. `pytest -m corpus`
builds the graph over eight projects pinned to exact commits and compares twelve measured
properties against `tests/corpus_baselines.json`.

```bash
pytest -m corpus -q                         # run the check
CRG_CORPUS_CACHE=~/.cache/crg-corpus \
  pytest -m corpus -q                       # keep the clones between runs
python -m tests.real_repo_corpus            # list the pins
python -m tests.real_repo_corpus --record   # re-record after an intentional change
```

The normal suite skips it: `tests/conftest.py` skips every `corpus`-marked item unless the
run's `-m` expression names the marker, so neither `pytest tests/` nor CI's `-m "not browser"`
job pays for it. Clones are shallow single-commit fetches (~115 MB total). A cold run takes
about 1 minute on a warm network and roughly 40 seconds once the clones are cached; budget a
few minutes on CI hardware.

`fastapi` and `gin` reuse the pins in `code_review_graph/eval/configs/`, so the project keeps
one answer to "which commit of fastapi do we measure". `tests/test_real_repo_corpus_guard.py`
fails if those two ever drift apart.

### Initial baseline

Measured on macOS 15 (Apple silicon), CPython 3.13.

| Repo | Language | Commit | Files | Nodes | Edges | Resolved edges | Resolved imports | Parse errors | Files with no node | Dangling CONTAINS | Build |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| fastapi | python | `22381558` | 1179 | 6287 | 32613 | 51.1% | 47.6% | 0 | 51 | 0 | 5.4s |
| gin | go | `5c00df8a` | 109 | 1591 | 17266 | 51.4% | 0.0% | 0 | 11 | 1 | 2.5s |
| zod | typescript | `59bbc03e` | 534 | 7082 | 98833 | 52.9% | 43.4% | 0 | 11 | 0 | 13.9s |
| gson | java | `854c8255` | 272 | 4125 | 47564 | 52.5% | 14.6% | 0 | 8 | 693 | 10.7s |
| newtonsoft-json | csharp | `09bb545d` | 952 | 9094 | 76052 | 60.1% | 0.0% | 0 | 2 | 486 | 20.6s |
| ripgrep | rust | `3fce3b5b` | 125 | 3518 | 29256 | 46.1% | 22.4% | 0 | 5 | 17 | 3.5s |
| sinatra | ruby | `cb22afd7` | 157 | 1224 | 14647 | 19.7% | 0.0% | 0 | 10 | 615 | 2.1s |
| guzzle | php | `93939470` | 141 | 3221 | 43443 | 57.6% | 45.1% | 0 | 4 | 0 | 6.5s |

Counts are deterministic: two recording runs on the same machine produced identical numbers
for every column except `Build`.

### How the bands were chosen

A pinned commit parsed by a fixed build has no measurement noise, so the bands do not model
noise. They model how much intentional change the project may make before someone has to
re-record. The tolerances live in the `bands` block of `tests/corpus_baselines.json` so a
widened band shows up in a diff like any other change.

| Property | Band | Why |
|---|---|---|
| `files_parsed`, `file_nodes` | -2% / +25% | The inventory of a fixed commit moves only when the ignore rules or the supported-extension list change. -2% is "must not lose files"; +25% leaves room for a newly supported extension. |
| `total_nodes` | -8% / +40% | -8% is several hundred nodes on every repo here, far outside any legitimate tidy-up. +40% admits a whole new node kind without a forced re-record while still catching runaway duplication. |
| `total_edges` | -10% / +50% | Wider in both directions because resolvers routinely trade edges for precision. -10% still means one relationship in ten stopped being recorded. |
| `resolved_edge_share`, `imports_resolved_share` | floor at baseline -5pp | A floor in percentage points, not a ratio, so a repo near 0.20 and one near 0.60 get the same absolute protection. 5pp is roughly a thousand edges on the larger repos. |
| `parse_errors`, `files_without_nodes`, `dangling_contains_edges` | ceiling at the recorded value | These are all "the build silently lost something". They may fall, never rise. |
| `control_char_names` | must be 0 | A security invariant, not a trend. |
| `build_seconds` | `max(3x, +20s)` | Wall clock varies with the machine. 3x catches a quadratic resolver; the +20s term keeps the sub-2-second repos from failing on scheduler noise. |
| `primary_language_present` | must hold | A dropped extension mapping otherwise looks like a small node-count change. |

### Reading a failure

Each failure names the property, the recorded value, the measured value, the delta and the
band. Dropping `".go": "go"` from the parser's extension map produces:

```text
gin (go) @ 5c00df8afadd: 6 of 12 properties moved out of band
  - files_parsed fell below its band: baseline=109 measured=11 delta=-98 (-89.9%), allowed [106.8, 136.2] (x0.98..x1.25)
  - total_nodes fell below its band: baseline=1591 measured=0 delta=-1591 (-100.0%), allowed [1463.7, 2227.4] (x0.92..x1.4)
  - primary_language_present dropped: baseline=1 measured=0 delta=-1. gin is a go project but the graph holds languages []
```
