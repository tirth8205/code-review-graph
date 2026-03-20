<h1 align="center">code-review-graph</h1>

<p align="center">
  <strong>Stop burning tokens. Start reviewing smarter.</strong>
</p>

<p align="center">
  <a href="https://github.com/tirth8205/code-review-graph/stargazers"><img src="https://img.shields.io/github/stars/tirth8205/code-review-graph?style=flat-square" alt="Stars"></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square" alt="MIT Licence"></a>
  <a href="https://github.com/tirth8205/code-review-graph/actions/workflows/ci.yml"><img src="https://github.com/tirth8205/code-review-graph/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.10%2B-blue.svg?style=flat-square" alt="Python 3.10+"></a>
  <a href="https://modelcontextprotocol.io/"><img src="https://img.shields.io/badge/MCP-compatible-green.svg?style=flat-square" alt="MCP"></a>
  <a href="#"><img src="https://img.shields.io/badge/version-1.8.4-purple.svg?style=flat-square" alt="v1.8.4"></a>
</p>

<br>

Claude Code re-reads your entire codebase on every task. `code-review-graph` fixes that. It builds a structural map of your code with [Tree-sitter](https://tree-sitter.github.io/tree-sitter/), tracks changes incrementally, and gives Claude precise context so it reads only what matters.

<p align="center">
  <img src="diagrams/diagram1_before_vs_after.png" alt="The Token Problem: 6.8x fewer tokens with higher review quality" width="85%" />
</p>

---

## Quick Start

**Claude Code Plugin** (recommended)

```bash
claude plugin marketplace add tirth8205/code-review-graph
claude plugin install code-review-graph@code-review-graph
```

**pip**

```bash
pip install code-review-graph
code-review-graph install
```

Restart Claude Code after either method. Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

Then open your project and tell Claude:

```
Build the code review graph for this project
```

The initial build takes ~10 seconds for a 500-file project. After that, the graph updates automatically on every file edit and git commit.

---

## How It Works

Your repository is parsed into an AST with Tree-sitter, stored as a graph of nodes (functions, classes, imports) and edges (calls, inheritance, test coverage), then queried at review time to compute the minimal set of files Claude needs to read.

<p align="center">
  <img src="diagrams/diagram2_architecture_pipeline.png" alt="Architecture pipeline: Repository to Tree-sitter Parser to SQLite Graph to Blast Radius to Minimal Review Set" width="100%" />
</p>

<details>
<summary><strong>Blast-radius analysis</strong></summary>
<br>

When a file changes, the graph traces every caller, dependent, and test that could be affected. This is the "blast radius" of the change. Claude reads only these files instead of scanning the whole project.

<p align="center">
  <img src="diagrams/diagram3_blast_radius.png" alt="Blast radius visualization showing how a change to login() propagates to callers, dependents, and tests" width="70%" />
</p>

</details>

<details>
<summary><strong>Incremental updates in &lt; 2 seconds</strong></summary>
<br>

On every git commit or file save, a hook fires. The graph diffs changed files, finds their dependents via SHA-256 hash checks, and re-parses only what changed. A 2,900-file project re-indexes in under 2 seconds.

<p align="center">
  <img src="diagrams/diagram4_incremental_update.png" alt="Incremental update flow: git commit triggers diff, finds dependents, re-parses only 5 files while 2,910 are skipped" width="90%" />
</p>

</details>

<details>
<summary><strong>15 supported languages</strong></summary>
<br>

Python, TypeScript, JavaScript, Vue, Go, Rust, Java, C#, Ruby, Kotlin, Swift, PHP, Solidity, C/C++, R

Each language has full Tree-sitter grammar support for functions, classes, imports, call sites, inheritance, and test detection.

</details>

---

## Benchmarks

All figures come from real tests on three production open-source repositories.

<p align="center">
  <img src="diagrams/diagram5_benchmark_board.png" alt="Benchmarks: httpx 27.3x, FastAPI 6.3x, Next.js 4.9x token reduction with higher review quality" width="90%" />
</p>

<details>
<summary><strong>Code review benchmark details (6.8x average reduction)</strong></summary>
<br>

Tested across 6 real git commits. The graph replaces reading entire source files with a compact structural summary (156-207 tokens) covering blast radius, test coverage gaps, and dependency chains.

| Repo | Size | Standard Approach | With Graph | Reduction | Review Quality |
|------|-----:|------------------:|-----------:|----------:|:-:|
| [httpx](https://github.com/encode/httpx) | 125 files | 12,507 tokens | 458 tokens | 26.2x | 9.0 vs 7.0 |
| [FastAPI](https://github.com/fastapi/fastapi) | 2,915 files | 5,495 tokens | 871 tokens | 8.1x | 8.5 vs 7.5 |
| [Next.js](https://github.com/vercel/next.js) | 27,732 files | 21,614 tokens | 4,457 tokens | 6.0x | 9.0 vs 7.0 |
| **Average** | | **13,205** | **1,928** | **6.8x** | **8.8 vs 7.2** |

Standard approach: reading all changed files plus the diff. Quality scored on accuracy, completeness, bug-catching potential, and actionable insight (1-10 scale).

</details>

<details>
<summary><strong>Live coding task details (14.1x average, 49x peak)</strong></summary>
<br>

An agent performed 6 real coding tasks (adding features, fixing bugs) across the same repositories. The graph directed it to the right files and away from everything else.

| Task | Repo | With Graph | Without Graph | Reduction | Files Skipped |
|------|------|--------:|-----------:|----------:|---:|
| Add rate limiter | httpx | 14,090 | 64,666 | 4.6x | 58 |
| Fix streaming bug | httpx | 14,090 | 64,666 | 4.6x | 59 |
| Add rate limiter | FastAPI | 37,217 | 138,585 | 3.7x | 1,120 |
| Fix streaming bug | FastAPI | 36,986 | 138,585 | 3.7x | 1,121 |
| Add rate limiter | Next.js | 15,049 | 739,352 | 49.1x | ~16,000 |
| Fix streaming bug | Next.js | 16,135 | 739,352 | 45.8x | ~16,000 |

The graph identified the correct files in every case. Savings scale with repository size.

</details>

<details>
<summary><strong>Monorepo scale: the 49x case</strong></summary>
<br>

Large repositories benefit most. In the Next.js monorepo (27,732 files, 739K tokens), the graph narrows the review context to ~15 files and 15K tokens, a 49x reduction with 27,700+ files excluded entirely.

<p align="center">
  <img src="diagrams/diagram6_monorepo_funnel.png" alt="Next.js monorepo: 27,732 files funneled down to ~15 files, 49x fewer tokens" width="75%" />
</p>

</details>

---

## Usage

<details>
<summary><strong>Slash commands</strong></summary>
<br>

| Command | Description |
|---------|-------------|
| `/code-review-graph:build-graph` | Build or rebuild the code graph |
| `/code-review-graph:review-delta` | Review changes since last commit |
| `/code-review-graph:review-pr` | Full PR review with blast-radius analysis |

</details>

<details>
<summary><strong>CLI reference</strong></summary>
<br>

```bash
code-review-graph install     # Register MCP server with Claude Code
code-review-graph build       # Parse entire codebase
code-review-graph update      # Incremental update (changed files only)
code-review-graph status      # Graph statistics
code-review-graph watch       # Auto-update on file changes
code-review-graph visualize   # Generate interactive HTML graph
code-review-graph serve       # Start MCP server
```

</details>

<details>
<summary><strong>MCP tools</strong></summary>
<br>

Claude uses these automatically once the graph is built.

| Tool | Description |
|------|-------------|
| `build_or_update_graph_tool` | Build or incrementally update the graph |
| `get_impact_radius_tool` | Blast radius of changed files |
| `get_review_context_tool` | Token-optimised review context with structural summary |
| `query_graph_tool` | Callers, callees, tests, imports, inheritance queries |
| `semantic_search_nodes_tool` | Search code entities by name or meaning |
| `embed_graph_tool` | Compute vector embeddings for semantic search |
| `list_graph_stats_tool` | Graph size and health |
| `get_docs_section_tool` | Retrieve documentation sections |
| `find_large_functions_tool` | Find functions/classes exceeding a line-count threshold |

</details>

---

## Features

| Feature | Details |
|---------|---------|
| **Incremental updates** | Re-parses only changed files. Subsequent updates complete in under 2 seconds. |
| **15 languages** | Python, TypeScript, JavaScript, Vue, Go, Rust, Java, C#, Ruby, Kotlin, Swift, PHP, Solidity, C/C++, R |
| **Blast-radius analysis** | Shows exactly which functions, classes, and files are affected by any change |
| **Auto-update hooks** | Graph updates on every file edit and git commit without manual intervention |
| **Semantic search** | Optional vector embeddings via sentence-transformers |
| **Interactive visualisation** | D3.js force-directed graph with edge-type toggles and search |
| **Local storage** | SQLite file in `.code-review-graph/`. No external database, no cloud dependency. |
| **Watch mode** | Continuous graph updates as you work |

<details>
<summary><strong>Configuration</strong></summary>
<br>

To exclude paths from indexing, create a `.code-review-graphignore` file in your repository root:

```
generated/**
*.generated.ts
vendor/**
node_modules/**
```

For semantic search, install the optional embeddings dependencies:

```bash
pip install code-review-graph[embeddings]
```

</details>

---

## Contributing

```bash
git clone https://github.com/tirth8205/code-review-graph.git
cd code-review-graph
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

<details>
<summary><strong>Adding a new language</strong></summary>
<br>

Edit `code_review_graph/parser.py` and add your extension to `EXTENSION_TO_LANGUAGE` along with node type mappings in `_CLASS_TYPES`, `_FUNCTION_TYPES`, `_IMPORT_TYPES`, and `_CALL_TYPES`. Include a test fixture and open a PR.

</details>

## Licence

MIT. See [LICENSE](LICENSE).

<p align="center">
<br>
<code>pip install code-review-graph && code-review-graph install</code>
</p>
