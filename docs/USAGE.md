# User Guide

Applies to code-review-graph 2.4.0.

## Installation

```bash
pip install code-review-graph
code-review-graph install    # detect installed AI coding tools and configure each one
code-review-graph build      # parse the codebase
```

`install` detects which AI coding tools you have, writes an MCP server entry for each, installs hooks and skills where the platform supports them, and adds graph instructions to the platform's rules file (`CLAUDE.md`, `AGENTS.md`, and others). `--no-hooks`, `--no-skills` and `--no-instructions` skip those steps. `--dry-run` shows what each file would become, as one diff per file, and writes nothing. Without it, `install` lists the files it will change and asks about the instruction files before it writes anything; `--yes` answers yes for you. Restart the editor or tool afterwards.

`--steering` is off by default and only for Claude Code. It adds a hook that puts graph context, such as callers, callees, flows and tests, beside each `Grep`, `Glob`, `grep` or `rg` command and `Read`. That steers the model towards the graph, but it changes what the model sees on every one of those calls: on Flask, about 0.12 seconds and up to about 220 tokens each. `--no-steering` removes only that hook, and `uninstall` removes it too. A failed lookup never stops the tool call.

`install` always adds `.code-review-graph/` to `.gitignore`. The other files it writes, such as `.claude/`, `.mcp.json` and `CLAUDE.md`, stay visible to git, because a team may want to commit them. Pass `--gitignore` to ignore them as well. It adds a block marked `# >>> code-review-graph install files >>>` that lists only the files and folders that run created and git does not track. A file that was already there, tracked or not, is never listed, and a folder the run created is listed whole. Running install again adds to the same block. `uninstall` removes the block and nothing else; to keep a listed file, delete its line.

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
| Codex | `codex` | `$CODEX_HOME/config.toml` + `$CODEX_HOME/hooks.json` + `$CODEX_HOME/skills/code-review-graph/` (defaults to `~/.codex`) |
| Claude Code | `claude-code` | `.mcp.json` + `.claude/settings.json` |
| CodeBuddy Code | `codebuddy` | `.mcp.json` + `CODEBUDDY.md` + `.codebuddy/settings.json` + `.codebuddy/skills/<name>/SKILL.md` |
| Cursor | `cursor` | `.cursor/mcp.json` |
| Windsurf | `windsurf` | `~/.codeium/windsurf/mcp_config.json` |
| Zed | `zed` | `~/Library/Application Support/Zed/settings.json` (macOS) or `~/.config/zed/settings.json` |
| Continue | `continue` | `~/.continue/config.json` |
| OpenCode | `opencode` | `opencode.jsonc` (preferred) or `opencode.json` |
| MiMo Code | `mimo` | `.mimocode/mimocode.jsonc` if it exists, otherwise `.mimocode/mimocode.json` |
| Antigravity | `antigravity` | `~/.gemini/antigravity/mcp_config.json` |
| Gemini CLI | `gemini-cli` | `.gemini/settings.json` |
| Qwen Code | `qwen` | `~/.qwen/settings.json` |
| Kiro | `kiro` | `.kiro/settings/mcp.json` |
| Qoder | `qoder` | `.qoder/mcp.json` |
| GitHub Copilot | `copilot` | `.vscode/mcp.json` |
| GitHub Copilot CLI | `copilot-cli` | `~/.copilot/mcp-config.json` |
| Hermes Agent | `hermes` | `~/.hermes/config.yaml` (Windows: `%LOCALAPPDATA%\hermes\config.yaml`; or `$HERMES_HOME/config.yaml`) |
| DeepSeek Harness | `dsh` | `~/.dsh/cordis.patch.yml` (or `$DSH_HOME/cordis.patch.yml`) + `AGENTS.md` |

DeepSeek Harness reads `cordis.patch.yml` in its home folder for every profile,
Desktop too. Install adds one `insert` entry for DSH's own MCP client
(`@deepseek-ai/dsh-mcp-client`) and leaves the rest of the file as it is;
uninstall takes that entry out again. The entry pins no repository: the
server starts in the folder DSH was started from. DSH does not pass on environment
variables whose names contain `KEY`, `TOKEN`, `SECRET` or `PASSWORD`, so add
a cloud embedding key to the entry's `env` yourself if you need one.

Using Zed over SSH? See
[Zed troubleshooting](TROUBLESHOOTING.md#6-zed-native-agent-cannot-see-mcp-in-an-ssh-project).

Codex also installs a global Skill that prefers MCP and uses a read-only CLI
fallback when MCP is unavailable. Refresh or restart the active Codex runtime
after installation. Windows and WSL use separate `CODEX_HOME` directories.

The CodeBuddy layout follows its documentation for
[MCP configuration](https://www.codebuddy.ai/docs/cli/mcp),
[skills](https://www.codebuddy.ai/docs/cli/skills), and
[hooks](https://www.codebuddy.ai/docs/cli/hooks). Hook commands resolve the
repository at runtime, so committed settings do not contain one developer's
checkout path.

MiMo Code reads `.mimocode/mimocode.json` and then `.mimocode/mimocode.jsonc`.
`install` adds the server to the `.jsonc` file if there is one, and to the
`.json` file if not, and keeps other servers, settings and comments.
`uninstall` removes it from both.

### Git pre-commit hook

For Codex, Claude Code and Qoder, `install` also appends a `pre-commit` hook in
the repository's hooks directory (found with `git rev-parse --git-path hooks`,
so `core.hooksPath` setups work). The hook runs `update` and
`detect-changes --brief` before each commit. It skips linked worktrees, so an
implicit update does not build a second graph for another branch; set
`CRG_HOOK_WORKTREES=1` to run it there too. `--no-hooks` skips the hook.

The same `install` also appends `post-checkout`, `post-merge` and
`post-rewrite` hooks that run `update` after a branch switch, a merge or a
rebase, including `git pull` in either mode, so the graph follows the checked
out code between commits. A file checkout and `commit --amend` do not trigger
an update, and the checkout a rebase makes before replaying local commits is
skipped in favour of a single update when the rebase finishes; a rebase with
nothing left to replay updates at that checkout instead. They share the
pre-commit hook's worktree rule and `--no-hooks` switch. An existing hook
written in another language, such as Python, is left unchanged with a warning.

Codex installs the CRG Skill in the active user Skill root and uses MCP first,
with a bundled read-only CLI fallback when MCP is unavailable. The Skill checks
graph health once per repository/task; a missing, empty, or stale graph never
causes an implicit build or update. WSL and Windows Codex processes have
separate `CODEX_HOME` directories and tool inventories, so install CRG in the
runtime that will actually run the task and restart/refresh that runtime.
Codex runs the installed hooks only after you mark them as trusted in Codex; until then the
graph does not update on its own there.

## Core Workflow

### 1. Build the graph (first time only)
```bash
code-review-graph build
```
Or ask your assistant: "Build the code review graph for this project".
Parses the whole codebase. Build time scales with repository size; a cold build of a ~3,000-file repository took about 40 seconds on the machine described in [REPRODUCING.md](REPRODUCING.md#incremental-update-latency).

If some files fail to parse, the build or update result has status `partial` and its summary names the files. Their previous graph rows are kept. The CLI also prints a `Warning:` line for them on stderr.

### 2. Review changes (daily use)
```bash
code-review-graph detect-changes --brief
```
Or ask your assistant: "Review my changes". Where `install` wrote skills, this uses the `review-changes` skill.
Reviews the files changed since the last commit plus their graph-derived impact radius. Review and impact responses carry a compact `context_savings` estimate. Across the 6 benchmark repositories, graph queries use about 63x fewer tokens per question (median; range 35x to 358x) than reading the whole corpus. That baseline is an upper bound: against an agent that greps and reads the top 3 files, the median is 6.65x (range 4.2x to 72.6x). See the [README benchmarks](../README.md#benchmarks) and [REPRODUCING.md](REPRODUCING.md).

### 3. Review a PR
```bash
code-review-graph detect-changes --brief --base main
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
Interactive D3.js force-directed graph. Past 2,000 nodes it starts collapsed (File nodes only); click a file to expand its children. Past 3,000 nodes or 9,000 edges, the default `--mode auto` draws one node per community instead, or per file when there are no communities. `--seed-symbol`, `--seed-file`, `--seed-changed` and `--seed-flow` draw only the part of the graph around a symbol, file, change or flow. Use the search bar to filter and click legend edge types to toggle them. `--format json|graphml|svg|obsidian|cypher` writes other formats.

### 6. Semantic search (optional)
```bash
pip install "code-review-graph[embeddings]"
```
Then run `code-review-graph embed` or the `embed_graph_tool` MCP tool to compute vectors. `semantic_search_nodes_tool` uses vector similarity when matching embeddings exist and falls back to keyword/FTS search otherwise. Pass `include_source=True` to get each hit's source lines with it, so you need not open the files. Add `expand_neighbors=True` as well to also get the direct callers and callees of the top three hits. `max_source_lines` (1 to 60, default 60) caps each snippet. Source is not attached when `detail_level="minimal"`.

Providers: local sentence-transformers, OpenAI-compatible endpoints, Google Gemini, MiniMax, and Voyage AI. Local embeddings read `CRG_EMBEDDING_MODEL`; OpenAI-compatible providers read `CRG_OPENAI_BASE_URL`, `CRG_OPENAI_API_KEY` and `CRG_OPENAI_MODEL`; Voyage reads `VOYAGE_API_KEY` and optionally `CRG_VOYAGE_MODEL`. Cloud providers print an egress warning unless `CRG_ACCEPT_CLOUD_EMBEDDINGS=1` is set. The full variable list is in the [README](../README.md#environment-variables).

Embedding text includes the first paragraph of each function or class docstring. For a graph created by an older release, run a full build once before re-embedding so every file gains that metadata.

`build`, `update`, `postprocess` and `watch` never refresh embeddings by default. To refresh an existing index, pass both options:

```bash
code-review-graph build \
  --embedding-provider local \
  --embedding-model all-MiniLM-L6-v2
```

A refresh only updates a previously embedded graph. On a graph with no embeddings yet it embeds nothing: `build`, `update` (also when it finds no changes) and `postprocess` return `embeddings_refresh_skipped: true` with a warning, and `watch` logs that warning after each update and keeps running. Run `code-review-graph embed` once to create the index. A refresh refuses to migrate vectors to a different provider, model or endpoint, removes vectors for deleted nodes, and turns provider or transport failures into build warnings.

### 7. Detect changes with risk scoring
Ask your MCP client: "Review my recent changes with risk scoring". This calls `detect_changes_tool`, which maps the diff to affected functions, YAML and JSON paths, flows, communities and test gaps. A function or class the diff deletes is listed in `removed_symbols` with the callers, importers and tests that still use it.

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
Then use `cross_repo_search_tool` to search every registered repository, or pass `repos=["mylib"]` to search a subset. `code-review-graph serve --http --multi-repo` runs one HTTP server for every registered repository; each tool call must then name one in `repo_root`.

`code-review-graph repos` lists each repository's registered `data_dir`, marking
missing directories with `[MISSING]`. If a registered directory disappears and
the repository still has a local `.code-review-graph/graph.db`, graph commands
warn and use normal resolution (`CRG_DATA_DIR`, then the local directory).
Registering a fresh external directory for a repository with no local graph
still creates that directory on the next write.

### 11. Python call enrichment (optional)
```bash
pip install "code-review-graph[enrichment]"
code-review-graph build --enrich
```
`--enrich` on `build` or `update` uses Jedi to resolve Python method calls on a local variable (`s = make()` then `s.authenticate()`), so `callers_of` and the impact radius find them. It is off by default because it is slow, about a second more per Python file. `CRG_ENABLE_ENRICHMENT=1` turns it on for hooks, the watcher and MCP builds, and `CRG_DISABLE_ENRICHMENT=1` keeps it off even when asked for. Installing the extra alone changes nothing.

## Context Savings

Impact and review-context tools return `risk: "unknown"` when none of the requested file
paths match the graph. Their `unmatched_files` field and summary identify missing paths.
For a mix of indexed and missing files, risk describes the indexed subset; check the
missing paths or rebuild the graph before treating that result as complete.

Review and impact responses include compact `context_savings` metadata (`estimated`, `saved_tokens`, `saved_percent`). The CLI shows the same figures as a boxed `Token Savings` panel on `detect-changes --brief` and `update --brief`, with a breakdown (Functions / Tests / Risk / Other) that sums to the graph response size. Add `--verify` to compare against OpenAI's `cl100k_base` tokenizer (needs `pip install tiktoken`). The figures are labelled estimated because they use a `chars / 4` approximation; the calibration in [REPRODUCING.md](REPRODUCING.md#calibration-table) puts the aggregate estimate within about 1% of real tokens. A small single-file change can use more context than the raw file, because the graph metadata has a fixed overhead.

### Context history

To see how these estimates add up over real use, turn on the local log. It is
off by default:

```bash
export CRG_CONTEXT_HISTORY=1     # or set it in the "env" of your MCP server config
```

Each response that carries `context_savings` (from `get_impact_radius`,
`get_review_context`, `detect_changes`, the compact `get_architecture_overview`,
`detect-changes`, `update --brief`, `impact` and `architecture`) then adds one
line to `context-history.jsonl` next to `graph.db`:

```json
{"v":1,"ts":"2026-11-09T18:59:01Z","tool":"detect_changes_tool","baseline_tokens":12932,"returned_tokens":773,"saved_tokens":12159,"detail_level":"minimal","files":3,"depth":2}
```

A line holds the time (UTC), the tool or command, the three token estimates,
and, where the call has them, `detail_level`, the number of changed files and
the depth. It never holds file names, paths, symbol names or source text, and
nothing is sent anywhere. At 1 MiB the file is renamed to
`context-history.jsonl.1` (replacing the older one), so the log never takes
much more than 2 MiB, about 10,000 calls. Writers share an empty
`context-history.lock` so two of them never roll the log over at once. Delete
the files to clear it. Nothing is written in read-only mode
(`CRG_READ_ONLY=1`).

Read it back as a trend:

```bash
code-review-graph context-history                     # per UTC day
code-review-graph context-history --by tool --days 7  # per tool, last 7 days
code-review-graph context-history --json              # the same numbers as JSON
```

```text
Context history (last 7 day(s)): 7 call(s). Token counts are estimates (about 4 characters per token).
Log: /path/to/flask/.code-review-graph/context-history.jsonl

Tool                             Calls   Full context  Graph context              Saved  Largest reply
detect_changes_tool                  2         74,904          6,393       68,511 (91%)          6,182
detect-changes                       1         37,452         18,533       18,919 (51%)         18,533
get_architecture_overview_tool       1         74,610            443       74,167 (99%)            443
get_impact_radius_tool               1         16,945            258       16,687 (98%)            258
get_review_context_tool              1         21,511         28,180      -6,669 (-31%)         28,180
impact                               1          4,566         31,804    -27,238 (-597%)         31,804
Total                                7        229,988         85,611      144,377 (63%)         31,804

A negative Saved means the graph replies were bigger than the files they stand for.
```

`Full context` is the estimated size of the changed files (or, for the
architecture overview, the full overview), `Graph context` is the size of the
replies, and `Saved` is the difference, so it is negative where a reply cost
more than reading the files. `Largest reply` is the biggest single reply,
which is where to look for a spike. Rows by day use UTC days, and `--days 1`
means today.

To reproduce the benchmark numbers quoted in the README, follow [REPRODUCING.md](REPRODUCING.md). The eval runner is one part of it:

```bash
code-review-graph eval --all --embed
```

## Supported Languages

The parser covers Python, JavaScript/TypeScript/TSX (including `.mjs`, `.cjs`, `.mts` and `.cts`), Go, Rust, Java, C/C++, C#, VB.NET, Ruby, Kotlin (`.kt`, `.kts`), Swift, PHP, Scala, Solidity, Dart, R, Perl, Lua/Luau, Objective-C, shell scripts, Elixir, Zig, PowerShell, Julia, ReScript, GDScript, Nix, Verilog/SystemVerilog, SQL/PL/SQL (Oracle), dbt models (`ref()` and `source()` become imports), Terraform/OpenTofu (`.tf`, and Terraform blocks in `.hcl` files; an `.hcl` file with no Terraform blocks keeps only a file node), Ansible YAML (playbooks, roles, tasks), Spring Boot application config (`application.properties`, `application.yml`, `application.yaml` and their `application-<profile>` variants; key names and value types only, never values), value-free generic YAML structure (translation files in folders such as `locales/` become file nodes only), XSLT stylesheets (`.xsl`, `.xslt`), value-free JSON structure (`.json`; translation, test, docs, example and data files keep only a file node, and lock files are skipped), Vue/Svelte single-file components, HTML pages (`.html`, `.htm`; inline scripts, plus `div`, `section` and `article` blocks with an id as `DocSection` nodes), Astro files (parsed with the TypeScript grammar), Jupyter and Databricks notebooks (`.ipynb` and Databricks `.py` exports), Perl XS files (`.xs`), Robot Framework files (`.robot`, `.resource`), and Liquid templates (`.liquid`). Other `.properties` files are not treated as source code.

Extension-less scripts are detected by shebang for bash/sh/zsh/ksh/dash/ash, Python, Node, Ruby, Perl, Lua, Rscript, and PHP interpreters.

Languages not covered can be added through a `.code-review-graph/languages.toml` file. See [CUSTOM_LANGUAGES.md](CUSTOM_LANGUAGES.md).

## What Gets Indexed

Terraform provider aliases have distinct names (`provider.aws.west`), while the default
provider retains `provider.aws`. Resource/data `provider` selections and module `providers`
map values link to provider nodes; ordinary resource expressions retain resource references.

Members of nested classes use the full enclosing class/module chain (for example,
`Alpha.Same.run`) in Ruby, Java, Kotlin, Swift, Scala and most other languages. Python and
C++ keep the leaf class name, but when Python classes in one file share a leaf name, their
methods use the full chain too (`Alpha.Same.run` and `Beta.Same.run`). C# names carry the
namespace and the full containing-type path. Local calls stay in that lexical class. Rebuild
an existing graph to replace rows created with colliding identities.

Storybook CSF files (`*.stories.js`, `.jsx`, `.ts`, `.tsx`) are indexed as test/demo code.
Their imports, calls, and references alone do not keep application symbols alive in
dead-code reports. Normal application callers and ordinary test evidence still count.

- **Nodes**: Files, Classes, Functions/Methods, Types, Tests, YamlPaths, DocSections, JsonPaths, Variables and Outputs (Liquid templates), plus Endpoints, Schedulers, Events and ConfigProperties where framework enrichment applies
- **Edges**: CALLS, IMPORTS_FROM, INHERITS, IMPLEMENTS, CONTAINS, TESTED_BY, DEPENDS_ON, REFERENCES, plus framework-specific kinds (INJECTS, HANDLES, TRIGGERS, PUBLISHES, CONSUMES/PRODUCES, DEPENDS_ON_CONFIG, TEMPORAL_STUB)
- **Confidence**: each `CALLS`, `REFERENCES` and `TESTED_BY` edge has a `confidence_tier`: `EXTRACTED` (1.0) when the source names a real node, `INFERRED` (0.8, or the resolver's own score) when a resolver chose it, `AMBIGUOUS` (0.4) when several nodes fit, and `UNRESOLVED` (0.2) when none does

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
*.min.js    *.min.css    *.map    *.lock    package-lock.json    **/package-lock.json    npm-shrinkwrap.json    **/npm-shrinkwrap.json
*.lock.json    project.assets.json    **/project.assets.json    *.min.json    pnpm-lock.yaml    **/pnpm-lock.yaml    yarn.lock
*.db    *.sqlite    *.db-journal    *.db-wal
```

A nested `target/`, `build/`, `.next/` or `.nuxt/` directory is also ignored when a sibling manifest (for example `pom.xml`, `build.gradle` or `next.config.js`) shows it is build output.

The default `**/vendor/**` exclusion applies to Composer, Go, Ruby and Cargo projects
identified by root manifests (`composer.json`, `go.mod`, `go.work`, `Gemfile`,
`Rakefile`, `Cargo.toml`, or `*.gemspec`). One of these manifests in a sub-folder also
excludes the `vendor/` beside it. Other repositories retain source packages named
`vendor`; add `vendor/` to `.code-review-graphignore` to exclude them explicitly.

To add patterns, create a `.code-review-graphignore` file in the repository root (same syntax as `.gitignore`):

```
generated/**
vendor/**
*.generated.ts
```

In git repositories, indexing is based on tracked files plus untracked files git does not ignore (`git ls-files` and `git ls-files --others --exclude-standard`), so gitignored files are skipped. Use `.code-review-graphignore` to exclude other files or when git is not available.

## Symlinked Folders

By default, symlinks are not followed: a symlinked file or folder adds nothing to the graph.

To follow symlinked folders, pass `--follow-symlinks` to `build`, `update` or `watch`, or set
`CRG_FOLLOW_SYMLINKS=1`. Set the variable where hooks, the MCP server and the daemon run too,
so every update makes the same choice. `--no-follow-symlinks` turns it off for one run.

This suits a project folder that holds links to code kept elsewhere, for example
`inputs/service-a -> ~/src/service-a`.

When it is on:

- Links to folders inside and outside the repository are both followed.
- Each file is stored once, under a path inside the repository. A file that lives in the
  repository keeps its own path, so a link to a folder the graph already indexes adds no
  copies. A file outside it is stored under the link's path (`inputs/service-a/app.py`),
  never its real path outside the repository.
- A link that points at a folder holding it (such as `app/up -> ..`) or holding the
  repository is skipped, and the log says so. Each real folder is read once, so links that
  point at each other cannot loop.
- At most `CRG_MAX_FOLLOWED_FILES` files (default 10,000) are taken through links. When the
  cap is reached, a warning names it and the rest are left out.
- Files reached through a link are indexed even when Git ignores them. `.code-review-graphignore`
  and the default ignore list still apply, both to the file's own path and to its path through
  the link, so use them to leave files out.
- Symlinked files are still never followed.
- `update` and `watch` add files when a link appears or a file appears behind one, and drop
  them when they go, the same as a full build. `watch` also watches each followed folder
  outside the repository. Turning the option off and running `update` drops them all.
- Tools that read source back (search, review context, flows, rename) read these files
  through their path inside the repository. Any other path outside the repository is still
  refused.

Only turn it on for repositories whose links you trust. A link committed by someone else can
point anywhere on your machine, and with the option on its files are indexed and served to
your AI tools.
