# User Guide

Applies to code-review-graph 2.3.8.

## Installation

```bash
pip install code-review-graph
code-review-graph install    # detect installed AI coding tools and configure each one
code-review-graph build      # parse the codebase
```

`install` detects which AI coding tools you have, writes an MCP server entry for each, installs hooks and skills where the platform supports them, and adds graph instructions to the platform's rules file (`CLAUDE.md`, `AGENTS.md`, and others). `--no-hooks`, `--no-skills` and `--no-instructions` skip those steps; `--dry-run` shows what would be written. Restart the editor or tool afterwards.

To configure one platform only:

```bash
code-review-graph install --platform codex
code-review-graph install --platform cursor
code-review-graph install --platform claude-code
code-review-graph install --platform codebuddy
```

### Supported Platforms

| Platform | `--platform` | Config file |
|----------|--------------|-------------|
| Codex | `codex` | `~/.codex/config.toml` + `~/.codex/hooks.json` |
| Claude Code | `claude-code` | `.mcp.json` + `.claude/settings.json` |
| CodeBuddy Code | `codebuddy` | `.mcp.json` + `CODEBUDDY.md` + `.codebuddy/settings.json` + `.codebuddy/skills/<name>/SKILL.md` |
| Cursor | `cursor` | `.cursor/mcp.json` |
| Windsurf | `windsurf` | `~/.codeium/windsurf/mcp_config.json` |
| Zed | `zed` | `~/Library/Application Support/Zed/settings.json` (macOS) or `~/.config/zed/settings.json` |
| Continue | `continue` | `~/.continue/config.json` |
| OpenCode | `opencode` | `opencode.jsonc` (preferred) or `opencode.json` |
| Antigravity | `antigravity` | `~/.gemini/antigravity/mcp_config.json` |
| Gemini CLI | `gemini-cli` | `.gemini/settings.json` |
| Qwen Code | `qwen` | `~/.qwen/settings.json` |
| Kiro | `kiro` | `.kiro/settings/mcp.json` |
| Qoder | `qoder` | `.qoder/mcp.json` |
| GitHub Copilot | `copilot` | `.vscode/mcp.json` |
| GitHub Copilot CLI | `copilot-cli` | `~/.copilot/mcp-config.json` |
| Hermes Agent | `hermes` | `~/.hermes/config.yaml` (or `$HERMES_HOME/config.yaml`) |

The CodeBuddy layout follows its documentation for
[MCP configuration](https://www.codebuddy.ai/docs/cli/mcp),
[skills](https://www.codebuddy.ai/docs/cli/skills), and
[hooks](https://www.codebuddy.ai/docs/cli/hooks). Hook commands resolve the
repository at runtime, so committed settings do not contain one developer's
checkout path.

### Git pre-commit hook

For Codex, Claude Code and Qoder, `install` also appends a `pre-commit` hook in
the repository's hooks directory (found with `git rev-parse --git-path hooks`,
so `core.hooksPath` setups work). The hook runs `update` and
`detect-changes --brief` before each commit. It skips linked worktrees, so an
implicit update does not build a second graph for another branch; set
`CRG_HOOK_WORKTREES=1` to run it there too. `--no-hooks` skips the hook.

## Core Workflow

### 1. Build the graph (first time only)
```
/code-review-graph:build-graph
```
Parses the whole codebase. Build time scales with repository size; a cold build of a ~3,000-file repository took about 40 seconds on the machine described in [REPRODUCING.md](REPRODUCING.md#incremental-update-latency).

If some files fail to parse, the build or update result has status `partial` and its summary names the files. Their previous graph rows are kept. The CLI also prints a `Warning:` line for them on stderr.

### 2. Review changes (daily use)
```
/code-review-graph:review-delta
```
Reviews the files changed since the last commit plus their graph-derived impact radius. Review and impact responses carry a compact `context_savings` estimate. Across the 6 benchmark repositories, graph queries use about 65x fewer tokens per question (median; range 36x to 376x) than reading the whole corpus. See the [README benchmarks](../README.md#benchmarks) and [REPRODUCING.md](REPRODUCING.md).

### 3. Review a PR
```
/code-review-graph:review-pr
```
Structural review of a branch diff with blast-radius analysis.

### 4. Watch mode (optional)
```bash
code-review-graph watch
```
Updates the graph on every file save.

### 5. Visualize the graph (optional)
```bash
code-review-graph visualize
open .code-review-graph/graph.html
```
Interactive D3.js force-directed graph. It starts collapsed (File nodes only); click a file to expand its children. Use the search bar to filter and click legend edge types to toggle them. `--format json|graphml|svg|obsidian|cypher` writes other formats.

### 6. Semantic search (optional)
```bash
pip install "code-review-graph[embeddings]"
```
Then run `code-review-graph embed` or the `embed_graph_tool` MCP tool to compute vectors. `semantic_search_nodes_tool` uses vector similarity when matching embeddings exist and falls back to keyword/FTS search otherwise.

Providers: local sentence-transformers, OpenAI-compatible endpoints, Google Gemini, MiniMax, and Voyage AI. Local embeddings read `CRG_EMBEDDING_MODEL`; OpenAI-compatible providers read `CRG_OPENAI_BASE_URL`, `CRG_OPENAI_API_KEY` and `CRG_OPENAI_MODEL`; Voyage reads `VOYAGE_API_KEY` and optionally `CRG_VOYAGE_MODEL`. Cloud providers print an egress warning unless `CRG_ACCEPT_CLOUD_EMBEDDINGS=1` is set. The full variable list is in the [README](../README.md#environment-variables).

Embedding text includes the first paragraph of each function or class docstring. For a graph created by an older release, run a full build once before re-embedding so every file gains that metadata.

`build`, `update`, `postprocess` and `watch` never refresh embeddings by default. To refresh an existing index, pass both options:

```bash
code-review-graph build \
  --embedding-provider local \
  --embedding-model all-MiniLM-L6-v2
```

A refresh only updates a previously embedded graph. It refuses to migrate vectors to a different provider, model or endpoint, removes vectors for deleted nodes, and turns provider or transport failures into build warnings.

### 7. Detect changes with risk scoring
Ask your MCP client: "Review my recent changes with risk scoring". This calls `detect_changes_tool`, which maps the diff to affected functions, flows, communities and test gaps.

From the shell:

```bash
code-review-graph detect-changes --brief              # against HEAD~1
code-review-graph detect-changes --brief --base main
```

When `--base` names a branch, the diff runs against the merge base of that branch and HEAD, which is the file set GitHub shows for a pull request. Commit hashes and other revisions are used as given. `detect-changes` is read-only; use `update --brief` when the graph may be stale.

### 8. Explore architecture
Ask your MCP client: "Show me the architecture of this project". This calls `get_architecture_overview_tool`, which returns a community-based architecture map with coupling warnings.

### 9. Generate a wiki
```bash
code-review-graph wiki
```
Writes one markdown page per detected community, plus an index, to `.code-review-graph/wiki/`.

### 10. Multi-repo search
```bash
code-review-graph register /path/to/other/repo --alias mylib
```
Then use `cross_repo_search_tool` to search every registered repository, or pass `repos=["mylib"]` to search a subset.

## Context Savings

Review and impact responses include compact `context_savings` metadata (`estimated`, `saved_tokens`, `saved_percent`). The CLI shows the same figures as a boxed `Token Savings` panel on `detect-changes --brief` and `update --brief`, with a breakdown (Functions / Tests / Risk / Other) that sums to the graph response size. Add `--verify` to compare against OpenAI's `cl100k_base` tokenizer (needs `pip install tiktoken`). The figures are labelled estimated because they use a `chars / 4` approximation; the calibration in [REPRODUCING.md](REPRODUCING.md#calibration-table) puts the aggregate estimate within about 1% of real tokens. A small single-file change can use more context than the raw file, because the graph metadata has a fixed overhead.

The evaluation runner produces the benchmark numbers quoted in the README:

```bash
code-review-graph eval --all
```

## Supported Languages

The parser covers Python, JavaScript, TypeScript/TSX, Go, Rust, Java, C/C++, C#, VB.NET, Ruby, Kotlin, Swift, PHP, Scala, Solidity, Dart, R, Perl, Lua/Luau, Objective-C, shell scripts, Elixir, Zig, PowerShell, Julia, ReScript, GDScript, Nix, Verilog/SystemVerilog, SQL, Terraform/OpenTofu (`.tf`; other `.hcl` files become file nodes only), Ansible YAML (playbooks, roles, tasks), Vue/Svelte single-file components, Astro files (parsed with the TypeScript grammar), Jupyter and Databricks notebooks (`.ipynb` and Databricks `.py` exports), and Perl XS files (`.xs`). Other YAML is not treated as source code.

Extension-less scripts are detected by shebang for bash/sh/zsh/ksh/dash/ash, Python, Node, Ruby, Perl, Lua, Rscript, and PHP interpreters.

Languages not covered can be added through a `.code-review-graph/languages.toml` file. See [CUSTOM_LANGUAGES.md](CUSTOM_LANGUAGES.md).

## What Gets Indexed

- **Nodes**: Files, Classes, Functions/Methods, Types, Tests, plus Endpoints, Schedulers and ConfigProperties where framework enrichment applies
- **Edges**: CALLS, IMPORTS_FROM, INHERITS, IMPLEMENTS, CONTAINS, TESTED_BY, DEPENDS_ON, REFERENCES, plus framework-specific kinds (INJECTS, HANDLES, TRIGGERS, PUBLISHES, CONSUMES/PRODUCES, DEPENDS_ON_CONFIG, TEMPORAL_STUB)

See [schema.md](schema.md) for details.

## Ignore Patterns

These paths are excluded by default. A leading `/` anchors the pattern at the repository root.

```
**/.code-review-graph/**   **/node_modules/**   **/.git/**       **/.svn/**
**/__pycache__/**          *.pyc                **/.venv/**      **/venv/**
/dist/**    /build/**    /.next/**    /.nuxt/**    /target/**    /bin/**    /obj/**
**/vendor/**    /storage/**    /bootstrap/cache/**    /public/build/**
**/.bundle/**   **/.gradle/**   *.jar   **/.dart_tool/**   **/.pub-cache/**   **/cdk.out/**
/coverage/**    **/.cache/**    /.tmp/**    /tmp/**
*.min.js    *.min.css    *.map    *.lock    package-lock.json    yarn.lock
*.db    *.sqlite    *.db-journal    *.db-wal
```

A nested `target/`, `build/`, `.next/` or `.nuxt/` directory is also ignored when a sibling manifest (for example `pom.xml`, `build.gradle` or `next.config.js`) shows it is build output.

To add patterns, create a `.code-review-graphignore` file in the repository root (same syntax as `.gitignore`):

```
generated/**
vendor/**
*.generated.ts
```

In git repositories, indexing is based on tracked files (`git ls-files`), so gitignored files are skipped. Use `.code-review-graphignore` to exclude tracked files or when git is not available.
