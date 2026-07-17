# Changelog

## [Unreleased]

### Added

- Added `code-review-graph uninstall` as a safe, symmetric counterpart to
  `install` (#482, replacing PR #491). It derives MCP cleanup from the live
  platform specifications, preserves unrelated shared configuration and JSONC
  comments, commits shared-file edits with atomic replacement, removes only
  CRG-owned hook/skill files, requires and normalizes Git/SVN repository roots,
  enforces repository/home boundaries, and supports dry-run,
  registered-repository, data-retention, and user-config-retention modes.

### Fixed

- Corrected TESTED_BY edge direction across graph, refactor, and transitive-test
  consumers, with a parser-to-store-to-query regression (#527/#559/#598 class).
- C# receiver calls now capture bare, chained, member, and null-conditional
  invocations with caller attribution (#612); Kotlin/C# annotations and C#
  namespace importer resolution—including nested namespaces—are also preserved
  (#295/#310, PR #353).
- Restored advertised Zig structure, calls, imports, and test nodes, including
  TESTED_BY edges for test blocks embedded in ordinary source files (PR #393).
- Hardened generated skills/configuration: uppercase `SKILL.md` (PR #563),
  string-safe JSONC plus top-level and nested-container data-preservation guards
  (#553, PR #354), and portable PATH-aware hooks (PR #565).
- Packaged documentation remains reachable through the MCP wrapper (#613),
  Action comments render repository-relative paths, and both visualization
  templates select the graph SVG specifically (PR #564).
- **PHP `use` imports now resolve to files** (`importers_of`, impact radius,
  call disambiguation): PHP `use` statements had no branch in the parser's
  import extraction and fell through to the raw-text fallback, storing the whole
  `use ...;` statement as the `IMPORTS_FROM` edge target (e.g.
  `"use App\Domain\Entity\Job;"`). As a result `importers_of` / `tests_for` /
  `inheritors_of` and the upstream side of `get_impact_radius` returned nothing
  for PHP classes, and the unresolved targets also degraded cross-file `CALLS`
  disambiguation in `resolve_bare_call_targets`. PHP imports are now recorded as
  fully-qualified names (handling `as` aliases, grouped `use A\{B, C}`, and
  `use function` / `use const`) and resolved to absolute `.php` paths by walking
  up from the importing file, mirroring the existing Java resolver. Vendor/global
  classes with no local file stay as the bare FQN.

## [2.3.6] - 2026-06-10

**Community-response release.** Built from a full audit of every open PR,
issue, and discussion: community fixes merged with credit, verified defects
fixed (including two open Windows bugs), benchmark claims made independently
checkable, and the project's first self-hosted PR review bot — this repo now
reviews its own pull requests with its own graph. No breaking changes.

### Added

- **Custom languages without forking** (#320): drop a
  `.code-review-graph/languages.toml` into your repo to index any grammar
  shipped by tree-sitter-language-pack (extension map + node-type lists,
  validated and capped, built-ins always win). See docs/CUSTOM_LANGUAGES.md.
- **GitHub Action** for risk-scored PR review comments: composite `action.yml`
  builds/restores the graph from CI cache, runs `detect-changes` against the
  PR base, and upserts a sticky comment with risk table, affected flows, test
  gaps, and the Token Savings line. Dogfooded on this repo via
  `.github/workflows/pr-review.yml`. See docs/GITHUB_ACTION.md.
- **`agent_baseline` eval benchmark**: compares graph queries against a
  realistic grep-and-read-top-k agent baseline instead of the whole-corpus
  strawman; wired into all six pinned eval configs.
- **Co-change ground truth for `impact_accuracy`**: predictions are now also
  graded against files actually co-changed in the same commit; the legacy
  metric is explicitly labelled "graph-derived (circular — upper bound)".
- **Weekly eval CI** (`.github/workflows/eval.yml`): report-only cron run of
  the two smallest pinned configs with CSV artifacts and a job summary.
- **docs/FAQ.md**: how CRG compares to LSP, RAG, grep/agentic search, and
  adjacent tools; when NOT to use it; verification steps; monorepo/worktree
  and registry guidance. Linked from the README.
- GitHub issue forms (bug/feature/platform), a PR template mirroring the
  CONTRIBUTING checklist, and dependabot config for pip + GitHub Actions.

### Fixed

- `store_file_batch` is now guarded against open transactions like its sibling
  (#489, merged from community PR #529 by @Devilthelegend — thank you).
- **Windows: `daemon status` no longer crashes with WinError 87** (#511):
  PID liveness now uses `OpenProcess`/`WaitForSingleObject` on win32 instead
  of `os.kill(pid, 0)`.
- **Windows: CLI `detect-changes` mapped 0 functions** (#528): diff paths are
  now remapped to absolute native paths before node lookup, matching the MCP
  tool's behavior; also prevents the misleading "~100% token savings" line on
  an empty result.
- Eval benchmarks no longer record failed runs as inflated wins: thrown
  `get_review_context`/`analyze_changes` calls are marked `status=error` and
  excluded from aggregates instead of producing naive/1 ratios or recall=1.0.
- Unknown embedding provider names now raise a clear error listing valid
  providers instead of silently falling back to the local model.
- The five analysis MCP tools and the wiki-page tool no longer leak SQLite
  connections (try/finally `store.close()`).
- `install` git hooks now resolve the real hooks directory via
  `git rev-parse --git-path hooks`, so linked worktrees and `core.hooksPath`
  (husky) setups get a working pre-commit hook (#313 residue).
- Shipped `hooks/hooks.json` and `hooks/session-start.sh` now drain stdin,
  matching the generated configs (#493 class).
- `fastmcp` is now capped `<4` so the next major cannot silently break the
  server (the #488 failure mode).

### Changed

- README benchmarks section now leads with the ~82x median per-question
  reduction (528x presented as the best case, not the headline), the
  limitations block is visible instead of collapsed, and "100% impact recall"
  is reframed as a graph-derived upper bound alongside the new co-change
  metric.
- Stale translated READMEs (zh-CN, ja-JP, ko-KR, hi-IN) carry a staleness
  banner; the zh-CN benchmark captions and docs/USAGE.md no longer contradict
  the English README.
- SECURITY.md now points to GitHub private vulnerability reporting as the
  canonical channel.

## [2.3.5] - 2026-05-25

**Real-time token savings, visible to humans.** The estimated context-savings
metric introduced in 2.3.4 was JSON-only. In 2.3.5 it surfaces as a clean
boxed panel on the CLI and is verifiable against a real tokenizer in one
flag — so when you reach for `code-review-graph` to review a change, you
can immediately *see* how much of your context window the graph just kept
out. No breaking changes.

### Added — Token Savings (headline feature)

- **Boxed `Token Savings` panel on every `--brief` CLI call.** Both
  `code-review-graph detect-changes --brief` and the new
  `code-review-graph update --brief` print a four-line panel: the full-context
  baseline, the graph response size, total saved tokens with percent, and a
  per-category breakdown (Functions / Tests / Risk / Other) that **sums
  exactly** to the graph response size — no padding, no rounding magic.

  ```text
  ┌─────────────────────── Token Savings ────────────────────────┐
  │ Full context would be:     12,921 tokens                     │
  │ Graph context used:           762 tokens                     │
  │ Saved:                     12,159 tokens (~94%)              │
  │ Breakdown: Functions 244 · Tests 191 · Risk 244 · Other 83   │
  └──────────────────────────────────────────────────────────────┘
  ```

- **`--verify` flag** cross-checks the displayed numbers against OpenAI's
  `cl100k_base` tokenizer (the GPT-4 family). Adds a second
  `Verified (tiktoken)` row to the panel showing the real token counts.
  Requires `pip install tiktoken`. A one-time calibration across 222 mixed
  source files (Python/JS/TS/Go/Rust/RST/MD) committed in
  `docs/REPRODUCING.md` shows the `chars/4` approximation stays within
  **+0.5%** of real tokens in aggregate; per-repo bias is bounded to ±12%
  and the **ratio** stays stable because both sides of the divide are
  equally biased.

- **`code-review-graph update --brief`** — incremental update plus the same
  risk + Token Savings panel in one command. Distinct from
  `detect-changes --brief` (which is read-only against the existing graph).
  Use `update --brief` when the graph might be stale (post-rebase, large
  change set); use `detect-changes --brief` when hooks/`crg-daemon` have
  already kept the graph fresh.

### Added — Reproducible benchmarks

- **`docs/REPRODUCING.md`** — end-to-end reproduction recipe with canonical
  numbers, the tiktoken calibration table, and an explicit explanation of
  the three different "token" benchmarks in the codebase and what each
  measures. Two people running the recipe on different machines on
  different days now produce **identical** numbers, within float rounding.
- **`multi_hop_retrieval` benchmark** — 11 hand-curated 2-step tool-chain
  tasks (semantic_search → query_graph) across the 6 test repos. Average
  score **0.909**. Per-task CSV in `evaluate/results/`.
- **`code-review-graph embed` CLI subcommand** — explicit shell-level access
  to embedding generation. Previously only reachable via MCP, which made
  the benchmark recipe awkward.

### Changed — Deterministic eval pipeline

- **Every config under `code_review_graph/eval/configs/*.yaml` now pins an
  upstream SHA.** Previously every config used `commit: HEAD`, which made
  benchmarks drift whenever upstream pushed. Pinned SHAs: express
  `b4ab7d65`, fastapi `0227991a`, flask `a29f88ce`, gin `5c00df8a`, httpx
  `b55d4635`, code-review-graph `84bde354`.
- **`nextjs.yaml` renamed to `code-review-graph.yaml`.** The historical
  "nextjs" entry pointed at this repo, not a Next.js codebase. Renamed to
  match reality.
- **`eval/runner.py` uses full clones with explicit `returncode` checks.**
  Previously `--depth 50` silently fell back to `HEAD~1..HEAD` whenever a
  pinned test-commit SHA was past the shallow window, producing benchmark
  numbers tied to whichever HEAD the clone happened to grab.
- **Leiden community detection seeded** (`CRG_LEIDEN_SEED`, default `42`).
  Previously unseeded — community IDs and sizes drifted run-to-run on the
  same graph, breaking benchmark comparability.
- **`eval/runner.py` resolves repo paths absolutely before storing.** The
  parser previously stored file_path as the path you passed in, so eval
  builds and CLI/MCP builds could disagree, producing duplicate nodes for
  the same source location. Fixed by `.resolve()` in the runner.
- **`eval/runner.py` calls `run_post_processing` after `full_build`.**
  Previously the eval framework left FTS5 unpopulated (shadow tables
  `nodes_fts_idx` and `nodes_fts_docsize` empty), so downstream search and
  multi-hop benchmarks silently returned no results.

### Changed — Search and embeddings

- **`embeddings._node_to_text` is richer.** Embedded text per node now
  includes the dotted form (`Parent.name`, e.g. `APIRoute.get_route_handler`),
  the identifier split into words (`get route handler`), and the enclosing
  module directory (`routing`, `dependencies`). Forces an automatic
  re-embedding because the text hash changes. Lifts multi-hop benchmark
  accuracy from **0.545 → 0.818**.
- **Identifier-aware search boost** (`search.extract_query_identifiers`).
  Natural-language queries like *"Who advances the gin middleware chain
  via Context.Next"* now have their dotted / snake_case / CamelCase tokens
  extracted and used to boost matching qualified-names by 2.0× in hybrid
  search. Combined with the richer embed text, multi-hop accuracy reaches
  **0.909** (10 of 11 tasks pass).

### Fixed

- **Test-gap dedup in the brief summary.** If duplicate `qualified_names`
  ever slip into the graph (e.g. after a path-normalization mismatch),
  the `Untested:` line in the human summary now collapses to unique names.
  The underlying `test_gaps` list still carries every entry.
- **`token_benchmark.py` warns when embeddings are missing.** The standalone
  benchmark's default NL questions need semantic search to match anything;
  without embeddings the benchmark used to silently report 0× reduction
  ratios. Now logs an explicit warning pointing users to `embed`.

### Documentation

- **`docs/REPRODUCING.md`** (new). End-to-end recipe, canonical numbers,
  the tiktoken calibration table, and a side-by-side explanation of the
  three "token" benchmarks in the codebase.
- **`README.md` Token Savings section** (new collapsible block under
  Usage). Plain-English explanation of `detect-changes --brief` vs
  `update --brief` — read-only vs re-parses-first — with a side-by-side
  decision table.
- **`docs/COMMANDS.md`** lists the new `--brief`, `--verify`, and `embed`
  forms with an inline "which one?" comment block on the analysis pair.
- **Updated benchmark headline** to reflect today's pinned-SHA snapshot:
  range **38× – 528×** (median ~82×) across the 6 repos, **100% impact
  recall**, **F1 0.71** across 13 commits. Old numbers (73× – 895×)
  reflected pre-fix conditions (leftover build artifacts, smaller graph
  responses) and have been superseded.
- **9 Excalidraw diagrams** regenerated with current canonical numbers
  (`diagrams/*.excalidraw`, source kept locally; PNG re-exports manual).

### Demo

- **`diagrams/context-savings-demo.gif`** — 44 s screencast showing both
  CLI surfaces and the `--verify` cross-check. Rendered from
  `diagrams/context-savings-demo.tape` (regenerable with `vhs`).

## [2.3.4] - 2026-05-25

Focused reliability and token-efficiency release for MCP/CLI review workflows. No breaking changes.

### Added

- **Estimated context savings metadata** for graph-filtered review/impact/architecture responses. The new `context_savings` field is intentionally compact (`estimated`, `saved_tokens`, `saved_percent`) and uses the existing conservative character-count approximation rather than claiming exact tokenization.
- **CLI estimated savings line** for `code-review-graph detect-changes --brief`; full JSON output includes the same compact `context_savings` metadata.

### Changed

- **Architecture overview is compact by default**: `get_architecture_overview_tool` now defaults to `detail_level="minimal"`, dropping per-community member lists and aggregating cross-community edges by community pair. Full per-edge output remains available with `detail_level="standard"`.
- **Bounded change analysis**: `detect_changes_tool` can now cap very large changed-function and transitive-test frontiers with `CRG_MAX_CHANGED_FUNCS` and `CRG_MAX_TRANSITIVE_FRONTIER`, and can return a structured timeout error via `CRG_TOOL_TIMEOUT`.

### Fixed

- **Windows semantic search deadlock** (#508/#507): local embedding models are pre-warmed on the main thread on Windows before FastMCP starts worker dispatch.
- **Rust test detection** (#503/#502): Rust `#[test]` and common async test attributes now produce `Test` nodes.
- **Generated hook stdin handling** (#494/#493): Codex and Claude hook commands drain stdin to avoid caller-side broken pipes on large hook payloads.
- **Cross-file callers** (#486/#472): `callers_of` now returns cross-file callers even when same-file callers exist.
- **Graph path lookup** (#469): review, impact, and file-summary tools resolve user-facing paths to the path format stored in the graph.
- **Bundled MCP docs** (#485/#480): `get_docs_section` can load the packaged `LLM-OPTIMIZED-REFERENCE.md` from installed wheels.
- **Local embedding provider availability** (#484/#448): missing `sentence-transformers` now reports local provider unavailability instead of silently producing zero embeddings.
- **Dead-code response fields** (#481/#447): dead-code results now include `file_path`, `relative_path`, and `language` while preserving the legacy `file` key.
- **SVN root validation** (#456): MCP/daemon/registry root validation now accepts `.svn` working copies consistently.
- **CLI postprocess flags** (#487): `build --skip-postprocess` and `update --skip-flows` no longer run an extra full post-processing pass.

### Documentation

- Updated stale release-facing version references for 2.3.4.
- Replaced fragile language-count wording with current broad language and notebook support wording.
- Added the missing VS Code extension `0.2.2` changelog entry without changing the extension package version.

### Tests

- Added regression coverage for compact architecture overview output and #476 mitigation.
- Added tests for estimated context savings calculation, compact metadata shape, MCP metadata, CLI brief/JSON output, Rust test parsing, hook stdin draining, graph path resolution, dead-code fields, SVN root validation, CLI postprocess flags, embedding availability, and bounded detect-changes behavior.

## [2.3.3] - 2026-05-08

Large additive release accumulated since v2.3.2 — 141 non-merge commits, 8 new languages/extensions, 5 new platform install targets, 6 new framework call resolvers, comprehensive Windows hardening, VS Code accessibility pass, and a full sweep of community PRs.

### Added

#### Languages and extensions

- **Nix support** (flake-aware): `.nix` files are parsed via the `nix` tree-sitter grammar shipped with `tree-sitter-language-pack`. Top-level and nested attrset bindings become `Function` nodes with flattened dotted names (e.g. `packages.default`, `devShells.default`). In `flake.nix`, `inputs.<name>.url = "..."` strings emit `IMPORTS_FROM` edges to the URL; `import <path>` and `callPackage <path> <args>` applications in any `.nix` file emit `IMPORTS_FROM` edges (relative paths are resolved against the caller's directory). Adds 7 tests (`TestNixParsing`) and fixtures `tests/fixtures/sample.nix`, `tests/fixtures/sample_module.nix`.
- **GDScript support** (Godot, PR #316): `.gd` files are parsed via the `gdscript` tree-sitter grammar. Extracts inner classes (`class Name:`), the file-level `class_name` identity, functions (including `static func`), `extends` parent class as an IMPORTS_FROM edge, direct calls and method calls. Adds 10 tests and `tests/fixtures/sample.gd`.
- **Verilog / SystemVerilog support** (PR #428): `.v`, `.sv`, `.svh` files parse modules, classes, packages, interfaces, programs, functions, and tasks via the `verilog` tree-sitter grammar. Per-construct extractors with dedicated unit tests.
- **SQL support** (PR #398): `.sql` files parse `CREATE FUNCTION`, `CREATE PROCEDURE`, `CREATE TABLE`, and `CREATE VIEW` statements; emits CALLS edges for function invocations.
- **ReScript support** (PR #309/323): `.res`/`.resi` parsing for modules, let-bindings, and external declarations.
- **`.hh` extension support**: C++ header variants now resolve into the C++ parser path.
- **`.ksh` extension and shebang-based detection** (PR #276): `.ksh` files parsed as shell; extension-less scripts detected via `#!/usr/bin/env <lang>` shebang lines.
- **Julia improvements**: parametric constructors, `@enum` declarations, and `public` module exports now produce graph nodes.

#### Platforms and install targets

- **GitHub Copilot platform support** (PR #445): `code-review-graph install --platform copilot` writes Copilot-CLI-compatible MCP config without generating Claude-specific skill artifacts.
- **Gemini CLI platform support** (PR #391): `--platform gemini-cli` skips Claude skills and writes Gemini-native MCP config.
- **Qoder platform support** (PR #245): `--platform qoder` adds MCP server registration for Qoder.
- **OpenCode plugin support** (PR #198 via #366): `--platform opencode` registers the MCP server with the OpenCode plugin manifest.
- **Cursor hooks support** (PR #196): `install` now writes Cursor hook entries (gated behind `~/.cursor` detection so non-Cursor users are not affected).
- **Codex install alignment**: native Codex integration path; no Claude skill files generated for Codex targets.

#### MCP server and CLI features

- **`crg-daemon`**: new multi-repo watch daemon that supervises per-repo file watchers via `subprocess.Popen` child processes. Documented in README, COMMANDS.md, and ROADMAP.md. 35 dedicated tests.
- **Streamable HTTP transport** (PR #277): MCP server can now run over streamable HTTP in addition to stdio.
- **`serve --tools` flag and `CRG_TOOLS` env var**: MCP tool filtering at startup so callers can expose only the subset they need.
- **`--repo` precedence and validation in `get_docs_section`** (PR #378): honors `serve --repo` and validates path containment before returning section content.
- **Search enrichment via PreToolUse hooks** (PR #248): hook-driven search index enrichment ahead of tool calls.
- **External database directory support**: graph DB can now live on a network filesystem via the existing `CRG_DATA_DIR` mechanism, with the file locking path adjusted accordingly.
- **SVN support** (PR #255): basic Subversion working-copy detection alongside git for change analysis.

#### Parser and resolver improvements

- **Spring DI call resolution** (PR #413): receiver method calls (`this.userService.find(...)`) resolve through `@Autowired`/constructor-injected fields to the concrete `InjectedType.method`. Emits `INJECTS` edges and stereotype metadata (`@Service`, `@Component`, `@Repository`, `@Controller`); writes fully-qualified `target_qualified` so `callers_of` queries work.
- **Temporal workflow/activity call resolution**: `WorkflowStub.start(...)` and `ActivityStub.execute(...)` resolve to their concrete workflow/activity implementations.
- **Kafka consumer/producer detection**: `@KafkaListener`-annotated methods and `KafkaTemplate.send(...)` calls emit `CONSUMES` and `PRODUCES` edges keyed on topic.
- **Jedi-based Python call resolution** (PR #247): improved cross-file Python call resolution using the Jedi static-analysis library.
- **Python callback REFERENCES edges** (PR #363): function names passed as callback arguments (`schedule(my_handler)`) now emit `REFERENCES` edges instead of being dropped.
- **Mocha TDD `suite()` recognition** (PR #423): files using Mocha's TDD interface now classify as tests.
- **Bun test runtime support** (PR #421): files importing `bun:test` are detected as tests.
- **`__tests__/` directory detection** (PR #422): all files under `__tests__/` are classified as test files regardless of name.

#### Embeddings

- **OpenAI-compatible embedding provider** (PR #321): pluggable provider supporting OpenAI, Azure OpenAI, and any OpenAI-API-compatible endpoint, with configurable batch size.
- **Localized embedding READMEs**: provider docs translated for non-English users.

#### Visualization, accessibility, and VS Code extension

- **WCAG 2.1 AA contrast pass**: 4.5:1 minimum text contrast across the standalone HTML and VS Code webview.
- **Distinct `d3.symbol` shapes per node kind**: colorblind-friendly differentiation in both the standalone visualization and the VS Code webview.
- **Keyboard navigation**: tab/arrow/enter/escape navigation across nodes, with focus styles and a skip-link to bypass the legend.
- **ARIA roles and labels**: tooltip, detail panel, legend, search results, communities button, edge-pill keyboard activation, search input label.
- **Help overlay**: interaction guide for both the standalone HTML and the keyboard-help overlay.
- **Empty-state webview** in VS Code with a contextual depth slider and tooltip.
- **Edge filter popover** in the VS Code toolbar — fixes density on narrow panels.
- **Detail panel relocated to the left** so it no longer occludes top controls; close button restyled to match the toolbar.
- **CONTAINS edge opacity** raised from 0.08 → 0.14 for visibility on dense graphs.
- **GitHub Dark palette** unified across the VS Code extension.
- **`IMPLEMENTS`, `TESTED_BY`, `DEPENDS_ON` edge types** rendered in the standalone HTML visualization.

### Fixed

#### `__version__` reporting

- **`code_review_graph.__version__` now matches `pyproject.toml`** (was `2.1.0` since the v2.1.0 release). The User-Agent header that `embeddings.py` sends on cloud HTTP requests is built from this string, so cloud-embedding traffic was being mis-attributed across all releases between v2.1.0 and v2.3.2.

#### C++ / Java / PHP parsing

- **C++ scoped/destructor/operator method names** (PR #371, PR #403): `void Foo::bar()`, `Foo::~Foo()`, `Foo::operator==(...)` now extract the correct member name instead of the qualifier or the operator token.
- **Java method name extraction** (PR #275): method names are now read from the `identifier` child of `method_declaration` rather than the return-type child (which was producing names like `int64`).
- **Java superclass / super-interfaces** (PR #278): `extends Foo` and `implements Bar, Baz` now extract bare type names from the `superclass`/`super_interfaces` AST nodes.
- **Java import resolution to file paths** (PR #280): `import com.example.foo.Bar` resolves through `src/main/java/...` and configured source roots to the actual file.
- **PHP `CALL` extraction** (PR #298): method calls (`$obj->foo()`), static calls (`Foo::bar()`), and unqualified function calls now produce CALLS edges.
- **Module-scope `CALLS` edges** (PR #285): top-level executable statements emit CALLS edges (previously only function/method bodies did).

#### Windows

- **Windows MCP stdio hang on long-running tools** (PR #400, PR #292): thread-pool selection now auto-selects on Windows MCP stdio so build/embed do not deadlock.
- **Windows MCP stdin hang** (PR #425): all `git`/`svn` subprocesses now run with `stdin=DEVNULL`, preventing the FastMCP-stdio buffer from filling on Windows.
- **Windows non-UTF-8 locale**: `subprocess.run` calls now pass `encoding="utf-8"` so cp1252 hosts no longer mis-decode git output.
- **Windows test failures** (PR #274): UTF-8 encoding, CRLF normalization, and `stop_at` boundary handling fixes for Windows CI.

#### Hooks and install

- **Hooks JSON schema** (PR #288): `hooks.json` validation no longer fails on the wrapper layout — `matcher` is required and the wrapper is removed.
- **Hooks merge instead of overwrite** (PR #114, PR #145, PR #203): `install_hooks` now merges into existing hook arrays and creates a `settings.json.bak` backup before modifying user config.
- **Pre-commit hook adds `update` command** (PR #315): generated pre-commit hook runs `code-review-graph update` rather than the obsolete subcommand.
- **Skip hooks gracefully outside git** (PR #293): `install` no longer fails when invoked from a non-git directory.
- **Poetry / uv environment detection** (PR #287): `install` now generates the correct MCP serve command for projects using Poetry or uv.
- **Hook quoting and `docs` repo_root** (PR #192): hook commands now quote repo paths with spaces, and the docs repo path is restored on install.

#### MCP server

- **fastmcp 3.x compatibility**: `_apply_tool_filter` restored on fastmcp ≥3, dependency floor bumped to `fastmcp>=3.2.4` to pick up the upstream Windows stdio EOF fixes.
- **FastMCP banner suppressed for stdio transport** (PR #290): the startup banner no longer corrupts the stdio handshake.
- **MCP config `cwd`, skills path, and JSONC parsing**: install now writes `cwd` into MCP config, points skills at the correct project path, and tolerates JSONC (comments + trailing commas) in existing config files.

#### SQLite and post-processing

- **SQLite transaction safety, FTS5 sync, and atomic operations** (PR #94, PR #279): nested-transaction handling, FTS5 content-table synchronization, and resource cleanup on error paths.
- **CLI build/update/watch run post-processing** (PR #98): signatures, FTS, flows, and communities are now refreshed after every CLI graph mutation (was previously only refreshed by the MCP server).
- **`reconcile()` auto-builds graphs and registers new repos**: cold-start path no longer requires a manual `build` before `reconcile`.
- **Flow trace adjacency in-memory** (PR #296): `trace_flows` loads adjacency once instead of querying SQLite per hop.

#### Other

- **`UnicodeDecodeError` in `read_text`** (PR #303): all text reads now use `errors="replace"`.
- **Dead-code callback references** (PR #424): functions referenced as callbacks no longer mis-classify as dead code.
- **Skills.py table formatting** (PR #302).
- **Search.py duplicate logger** removed.
- **`status` command reports alive/dead** from the persisted state file.

### Security

- **Embeddings RCE hardening** (PR #397): remote code execution paths in the embedding provider are gated behind an explicit env var; cloud HTTP requests now send a versioned User-Agent (PR #390) and refuse to mix indexes built with different providers.

### Documentation

- **MCP tools documentation** (PR #306): catalog of all MCP tools with usage examples.
- **venv usage guide** (PR #307).
- **Windows setup guide** for Claude Code MCP integration.
- **pipx / PyPI failure troubleshooting** with a `diagnose_pypi_connectivity.py` diagnostic script.
- **MseeP.ai badge** added to README (PR #399).

### Maintenance

- **Beads (`bd`) issue tracking** initialized for the project (`bd prime` for workflow context).
- **iCloud sync duplicate files** removed from the working tree.
- **Working spec docs** moved out of git (already in `.gitignore`).
- **CI lint and test failures** swept across multiple merged PRs.

### Upgrade notes

- `uvx --reinstall code-review-graph` or `pip install -U code-review-graph`.
- Re-run `code-review-graph install` once after upgrading to pick up the JSONC-tolerant config writer and the corrected `cwd` / skills path in `.mcp.json`.
- The `__version__` fix changes the User-Agent string emitted by cloud embedding providers from `code-review-graph/2.1.0` to `code-review-graph/2.3.3`. Anyone allow-listing the old User-Agent on a proxy needs to update their rule.
- VS Code extension still ships separately — repackage and republish the `.vsix` if you want the v2.3.3 a11y improvements in the Marketplace build.

## [2.3.2] - 2026-04-14

Major feature release — 15 new capabilities, 6 community PRs merged, 6 new MCP tools, 4 new languages, multi-format export, and graph analysis suite.

### Added

- **Hub node detection** (`get_hub_nodes_tool`): find the most-connected nodes in the codebase (architectural hotspots) by in+out degree, excluding File nodes.
- **Bridge node detection** (`get_bridge_nodes_tool`): find architectural chokepoints via betweenness centrality with sampling approximation for graphs >5000 nodes.
- **Knowledge gap analysis** (`get_knowledge_gaps_tool`): identify structural weaknesses — isolated nodes, thin communities (<3 members), untested hotspots, and single-file communities.
- **Surprise scoring** (`get_surprising_connections_tool`): composite scoring for unexpected architectural coupling (cross-community, cross-language, peripheral-to-hub, cross-test-boundary).
- **Suggested questions** (`get_suggested_questions_tool`): auto-generate prioritized review questions from graph analysis (bridge nodes, untested hubs, surprising connections, thin communities).
- **BFS/DFS traversal** (`traverse_graph_tool`): free-form graph exploration from any node with configurable depth (1-6) and token budget.
- **Edge confidence scoring**: three-tier system (EXTRACTED/INFERRED/AMBIGUOUS) with float confidence scores on all edges. Schema migration v9.
- **Export formats**: GraphML (Gephi/yEd/Cytoscape), Neo4j Cypher statements, Obsidian vault (wikilinks + YAML frontmatter + community pages), SVG static graph. CLI: `visualize --format graphml|cypher|obsidian|svg`.
- **Graph diff**: snapshot/compare graph state over time — new/removed nodes, edges, community membership changes.
- **Token reduction benchmark**: measure naive full-corpus tokens vs graph query tokens with per-question reduction ratios.
- **Memory/feedback loop**: persist Q&A results as markdown for re-ingestion via `save_result` / `list_memories` / `clear_memories`.
- **Oversized community auto-splitting**: communities exceeding 25% of graph are recursively split via Leiden algorithm.
- **4 new languages**: Zig, PowerShell, Julia, Svelte SFC (23 total).
- **Visualization enhancements**: node size scaled by degree, community legend with toggle visibility, improved interactivity.
- **README translations**: Simplified Chinese, Japanese, Korean, Hindi.

### Merged community PRs

- **#127** (xtfer): SQLite compound edge indexes for query performance.
- **#184** (realkotob): batch `_compute_summaries` — fixes build hangs on large repos.
- **#202** (lngyeen): Swift extension detection, inheritance edges, type kind metadata.
- **#249** (gzenz): community detection resolution scaling (21x speedup), expanded framework patterns, framework-aware dead code detection (56 new tests).
- **#253** (cwoolum): automatic graph build for new worktrees in Claude Code.
- **#267** (jindalarpit): Kiro platform support with 9 tests.

### Changed

- MCP tool count: 22 → 28.
- Schema version: 8 → 9 (edge confidence columns).
- Community detection uses resolution scaling for large graphs.
- Risk scoring uses weighted flow criticality and graduated test coverage.
- Dead code detection is framework-aware (ORM models, Pydantic, CDK constructs filtered).
- Flow entry points expanded with 30+ framework decorator patterns.

## [2.3.1] - 2026-04-11

Hotfix for the Windows long-running-MCP-tool hang that v2.2.4 only partially fixed.

### Fixed
- **Windows MCP hang on long-running tools** (PR #231, fixes #46, #136): follow-up to v2.2.4. [@dev-limucc reported on #136](https://github.com/tirth8205/code-review-graph/issues/136) that the `WindowsSelectorEventLoopPolicy` fix from v2.2.4 was necessary but not sufficient — read-only tools worked, but `build_or_update_graph_tool(full_rebuild=True)` and `embed_graph_tool` still hung indefinitely on Windows 11 / Python 3.14. Root cause: FastMCP 2.x dispatches sync handlers inline on the only event-loop thread, so handlers that run for more than a few seconds (especially those that spawn subprocesses or do CPU-bound inference) stop the loop from pumping stdin/stdout. **Fix**: converted the five heavy tools (`build_or_update_graph_tool`, `run_postprocess_tool`, `embed_graph_tool`, `detect_changes_tool`, `generate_wiki_tool`) to `async def` and offloaded the blocking work via `asyncio.to_thread`. The other 19 tools are fast SQLite-read paths and stay sync. Zero config, works on every platform. New regression tests assert the five tools are registered as coroutines AND that each one's source literally contains `asyncio.to_thread` as a defense-in-depth lock-in.

## [2.3.0] - 2026-04-11

Additive feature release — new language parsers, new platform install target, MCP tool UX improvements, and out-of-tree graph storage. No breaking changes from v2.2.4.

### Added

- **Elixir parser** (PR #228, closes #112): `.ex` and `.exs` files now produce modules as Class nodes, `def`/`defp`/`defmacro`/`defmacrop` as Function/Test nodes attached to their enclosing module, `alias`/`import`/`require`/`use` as `IMPORTS_FROM` edges, and everything else as `CALLS` edges. Internal call resolution walks into `do_block` bodies so `MathHelpers.double` correctly resolves its call to `Calculator.compute`.
- **Objective-C parser** (PR #227, closes #88): `.m` files parse classes (`@interface`, `@implementation`, `@protocol`), instance and class methods, `[receiver message:args]` message expressions, C-style `main()`, and `#import`/`#include`. Multi-part selectors like `add:to:` keep `add` as the canonical method name.
- **Bash/Shell parser** (PR #227, closes #197): `.sh`, `.bash`, and `.zsh` files parse functions, `command` invocations as `CALLS`, and `source path` / `. path` as `IMPORTS_FROM` edges with path resolution when the target file exists.
- **Qwen Code as a supported MCP install platform** (PR #227, closes #83): `code-review-graph install --platform qwen` writes a merged `~/.qwen/settings.json` using the same `mcpServers` schema as Cursor/Windsurf — it does not clobber existing Qwen config.
- **`apply_refactor_tool` dry-run mode** (PR #228, closes #176): new `dry_run: bool = False` parameter on the MCP tool and underlying `apply_refactor()` function. When true, returns a unified diff per file without touching disk and leaves the `refactor_id` valid for a follow-up real apply. Multi-edit files now apply sequentially against updated content in both modes (fixes a subtle bug where separate edits on the same file could stomp each other).
- **`CRG_DATA_DIR` environment variable** (PR #228, closes #155): when set, replaces the default `<repo>/.code-review-graph` directory verbatim. Useful for ephemeral workspaces, Docker volumes, shared CI caches, and multi-repo orchestrators. Supported by the CLI, MCP tools, and the registry.
- **`CRG_REPO_ROOT` environment variable** (PR #228, closes #155): `find_project_root()` now checks `CRG_REPO_ROOT` before the usual git-root walk — useful for anyone scripting the CLI from a cwd outside the target repo.
- **`install --no-instructions` and `-y`/`--yes` flags** (PR #228, closes #173): new flags on `code-review-graph install` to opt out of the `CLAUDE.md`/`AGENTS.md`/`.cursorrules`/`.windsurfrules` injection entirely (`--no-instructions`) or auto-confirm it without an interactive prompt (`-y`/`--yes`). The CLI also now prints the list of files it will touch before writing, so even without `--dry-run` users see what's coming.
- **Cloud embeddings stderr warning** (PR #228, closes #174): `get_provider()` now prints an explicit warning to stderr before returning a Google Gemini or MiniMax provider, explaining that source code will be sent to an external API. `CRG_ACCEPT_CLOUD_EMBEDDINGS=1` suppresses the warning for scripted workflows. The warning is on stderr only — it never writes to stdout or reads from stdin, so the MCP stdio transport remains uncorrupted.
- **TROUBLESHOOTING quick-reference** (PR #228): new top section in `docs/TROUBLESHOOTING.md` covering the four most common support questions — hook schema errors, `command not found` after pip install, project-vs-user scoping, and "built the graph but Claude Code doesn't see it".

### Fixed

- **Multi-edit refactor correctness** (PR #228): when a single `apply_refactor` call had multiple edits targeting the same file, the previous implementation re-read the file once per edit and could silently stomp earlier changes. The plan-computation step now groups edits by file and applies them sequentially against the updated content; this fix applies to both the real-write and the new dry-run path.

### Changed

- `install` and `init` commands now preview instruction-file targets before writing (no-op if nothing would change). This is always-on and does not require `--dry-run`.
- Default embedding path remains fully local (`sentence-transformers`); no behavior change unless you explicitly opt in to a cloud provider.

### Deprecated

Nothing.

### Security

- The cloud-embedding stderr warning (#174) is a privacy improvement; it does not change the behavior of offline local embeddings, which remain the default.

### Upgrade notes

- Nothing to do beyond `uvx --reinstall code-review-graph` or `pip install -U code-review-graph`. If you're coming from v2.2.2 or earlier, re-run `code-review-graph install` once to pick up the v2.2.3 hook schema rewrite.
- `CRG_DATA_DIR` is optional — if you don't set it, graphs continue to live at `<repo>/.code-review-graph` as before.
- VS Code extension v0.2.2 (from v2.2.4) still needs to be **repackaged and republished** separately; the PyPI `publish.yml` workflow does not cover it.

### Superseded PRs

- PR #204 (install preview, @lngyeen) — reimplemented cleanly in #228 with `isatty()`-guarded confirmation.
- PR #207 (`CRG_DATA_DIR`/`CRG_REPO_ROOT`, @yashmewada9618) — reimplemented cleanly in #228 without `input()`-on-stdio and `mcp._local_only` fragility.
- PR #179 (cloud embeddings warning, @Bakul2006) — reimplemented cleanly in #228 with stderr-only messaging and no stdio reads.

Credit to @lngyeen, @yashmewada9618, and @Bakul2006 for the original designs.

## [2.2.4] - 2026-04-11

Ships the 11 bugs from PR #222 plus the `v2.2.3.1` smoke-test hotfixes, for users upgrading directly from `v2.2.3` or earlier.

### Security
- **fastmcp bumped from 1.0 → ≥2.14.0** (PR #222, fixes #139, #195): closes CVE-2025-62800 (XSS), CVE-2025-62801 (command injection via server_name), CVE-2025-66416 (Confused Deputy). Transitively drops the `docket → fakeredis` chain that was broken by a `FakeConnection` → `FakeRedisConnection` rename in recent fakeredis releases (#195). The FastMCP public API (`FastMCP(name, instructions=...)`, `@mcp.tool()`, `@mcp.prompt()`, `mcp.run(transport="stdio")`) is unchanged across the 1 → 2 bump, so no source changes were needed beyond the pin. All 24 tools verified to register on fastmcp 2.14.6 and round-trip real per-repo data via stdio MCP in a 6-repo smoke test.

### Fixed
- **Windows build/embed hangs** (PR #222, fixes #46, #136): `main()` now sets `WindowsSelectorEventLoopPolicy` before `mcp.run()` on `sys.platform == "win32"`. The default `ProactorEventLoop` on Windows Python 3.8+ deadlocks with `ProcessPoolExecutor` (used by `full_build`) over a stdio MCP transport — producing the silent "Synthesizing…" hangs on `build` and `embed_graph_tool`. This is a no-op on macOS/Linux. **Note**: the fix was applied blind; maintainer could not verify on Windows. Please open a fresh issue if you still see a hang on v2.2.4 Windows with either `sentence-transformers` or Gemini providers.
- **Go method receivers** (PR #222, fixes #190): `func (s *T) Foo()` now attaches `Foo` to `T` as a member (`parent_name="T"`) with the usual `CONTAINS` edge instead of appearing as a top-level function. New `_get_go_receiver_type()` helper walks the method_declaration's first parameter_list to extract the receiver type name.
- **Dart parser — three bugs** (PR #222, fixes #87):
  - Dart `CALLS` edges (`_extract_dart_calls_from_children()`) — tree-sitter-dart doesn't wrap calls in a single `call_expression` node; the pattern is `identifier + selector > argument_part`. New walker handles both direct (`print('x')`) and method-chained (`obj.foo()`) shapes.
  - Dart `package:` URI resolution in `_do_resolve_module()` — `package:<pkgname>/<sub_path>` now walks up to a `pubspec.yaml` whose `name:` declaration matches `<pkgname>` and resolves to `<root>/lib/<sub_path>`.
  - `inheritors_of` bare-vs-qualified name mismatch in `tools/query.py` — falls back to `search_edges_by_target_name(node.name, kind=...)` for `INHERITS`/`IMPLEMENTS` when the qualified-name lookup returns nothing. Affects all languages (INHERITS targets are stored as bare strings for every language), not just Dart.
- **Nested `node_modules` and framework ignore defaults** (PR #222, fixes #91): `_should_ignore()` now treats single-segment `<dir>/**` patterns as "this directory at any depth", so `node_modules/**` also matches `packages/app/node_modules/react/index.js` inside monorepos. Extended `DEFAULT_IGNORE_PATTERNS` with Laravel/Composer (`vendor/**`, `bootstrap/cache/**`, `public/build/**`), Ruby (`.bundle/**`), Gradle (`.gradle/**`, `*.jar`), Flutter/Dart (`.dart_tool/**`, `.pub-cache/**`), and generic `coverage/**`, `.cache/**`. Deliberately did **not** add `packages/**` or `bin/**`/`obj/**` — those are false positives in yarn/pnpm workspace monorepos and .NET source trees respectively.
- **Bare `except Exception` cleanup** (PR #222, fixes #194): Replaced with specific exception classes + `logger.debug(...)` in 11 files (`cli.py`, `graph.py`, `migrations.py`, `parser.py`, `registry.py`, `tools/context.py`, `tsconfig_resolver.py`, `visualization.py`, `wiki.py`, `eval/benchmarks/search_quality.py`). No behavioral change; debuggability improvement.
- **Visualization auto-collapse hiding all edges** (PR #222, fixes #132): `visualization.py` no longer unconditionally auto-collapses every File node on page load. Auto-collapse now only kicks in above 2000 nodes — previously any graph above ~300 nodes would silently hide every CALLS/IMPORTS/INHERITS edge because they connect Functions/Classes nested inside the collapsed Files.
- **`eval` command crashes on `yaml.safe_load`** (PR #222, fixes #212): `eval.runner.load_all_configs()` now calls `_require_yaml()` before reading YAML, so users without `code-review-graph[eval]` installed get `ImportError: pyyaml is required: pip install code-review-graph[eval]` instead of `AttributeError: 'NoneType' object has no attribute 'safe_load'`.

### VS Code extension (0.2.2)
- **`better-sqlite3` bumped 11.x → 12.x** (PR #222, fixes #218): VS Code 1.115 ships Electron 39 / V8 14.2 which removed `v8::Context::GetIsolate()`, the C++ API used by `better-sqlite3@11`. The extension couldn't activate at all — every command was undefined. `better-sqlite3@12.4.1+` (installs 12.8.0) uses the new V8 API and ships Electron 39 prebuilds. `@types/better-sqlite3: ^7.6.8 → ^7.6.13`, plus type-import adjustments in `src/backend/sqlite.ts` for the `Node16` module resolution and the new CJS `export =` types. Extension version bumped to 0.2.2. **Remember to repackage and republish the `.vsix`** — the existing `publish.yml` workflow only covers PyPI.

### Carried forward from 2.2.3.1
- `serve --repo <X>` is now honored by all 24 MCP tools (was only read by `get_docs_section_tool`). See #223.
- Wiki slug collisions no longer silently overwrite pages (~70% data loss on real repos). See #223.

### Upgrade notes
- `uvx --reinstall code-review-graph` or `pip install -U code-review-graph`, then re-run `code-review-graph install` (the 2.2.3 hook-schema rewrite is still a requirement if you're coming from 2.2.2 or earlier).
- VS Code extension needs to be repackaged + republished separately; the Python release does not include it.

## [2.2.3.1] - 2026-04-11

Hotfix on top of 2.2.3 for two bugs surfaced by a full first-time-user smoke test against six real OSS repos (express, fastapi, flask, gin, httpx, next.js).

### Fixed
- **`serve --repo <X>` was ignored by 21 of 24 MCP tools** (PR #223): `main.py` captured the `--repo` CLI flag into `_default_repo_root`, but only `get_docs_section_tool` read it. The other 21 `@mcp.tool()` wrappers all took `repo_root: Optional[str] = None` and passed that straight through to the impl, which fell back to `find_repo_root()` from cwd. The real-world blast radius is small — the `install` command writes `.mcp.json` without a `--repo` flag and Claude Code launches the server with `cwd=<repo>` — but anyone scripting `serve` manually or running a multi-repo orchestrator would silently get the wrong graph. Added a single `_resolve_repo_root()` helper with explicit precedence (client arg > `--repo` flag > `None → cwd`) and threaded it through all 24 wrappers. New unit tests cover the precedence rules.
- **Wiki slug collisions silently overwrote pages** (PR #223): `_slugify()` folds non-alphanumerics to dashes and truncates to 80 chars, so similar community names collided (`"Data Processing"`, `"data processing"`, `"Data  Processing"` all → `data-processing.md`). `generate_wiki()` wrote each community to `<slug>.md` regardless, so later iterations overwrote earlier files while the counter reported them as "updated". On the express smoke test this was **~70% silent data loss** (32 real files vs 107 claimed pages). Fixed by tracking used slugs per-run and appending `-2`, `-3`, … until unique. Every community now gets its own page; the counter matches the physical file count; `get_wiki_page()` still resolves by name via the existing partial-match fallback. New regression test monkey-patches three colliding names and asserts no content loss.

## [2.2.3] - 2026-04-11

### Fixed
- **Claude Code hook schema** (PR #208, fixes #97, #138, #163, #168, #172, #182, #188, #191, #201): `generate_hooks_config()` now emits the valid v1.x+ Claude Code schema — every hook entry has `matcher` + a nested `hooks: [{type, command, timeout}]` array, and timeouts are in seconds. The invalid `PreCommit` event has been removed; pre-commit checks are now installed as a real git hook via `install_git_hook()`. Users upgrading from 2.2.2 must re-run `code-review-graph install` to rewrite `.claude/settings.json`.
- **SQLite transaction nesting** (PR #205, fixes #110, #135, #181): `GraphStore.__init__` now connects with `isolation_level=None`, disabling Python's implicit transactions that were the root cause of `sqlite3.OperationalError: cannot start a transaction within a transaction` on `update`. `store_file_nodes_edges` adds a defensive `in_transaction` flush before `BEGIN IMMEDIATE`.
- **Go method receivers** (PR #166): `_extract_name_from_node` now resolves Go method names from `field_identifier` inside `method_declaration`, fixing method names that were previously picked up as the result type (e.g. `int64`) instead of the method name.
- **UTF-8 decode errors in `detect_changes`** (PR #170, fixes #169): Diff parsing now uses `errors="replace"` so diffs containing binary files no longer crash the tool.
- **`--platform` target scope** (PR #142, fixes #133): `code-review-graph install --platform <target>` now correctly filters skills, hooks, and instruction files so you only get configuration for the requested platform.
- **Large-repo community detection hangs** (PR #213, PR #183): Removed recursive sub-community splitting, capped Leiden at `n_iterations=2`, and batched `store_communities` writes. 100k+ node graphs no longer hang in `_compute_summaries`.
- **CI**: ruff lint + `tomllib` on Python 3.10 (PR #220) — `tests/test_skills.py` now uses a conditional `tomli` backport on 3.10, `N806`/`E501`/`W291` fixes in `skills.py`/`communities.py`/`parser.py`, and the embedded `noqa` reference in `visualization.py` was rephrased so ruff stops parsing it as a directive.
- **Missing dev dependencies** (PR #159): `pytest-cov` added to dev extras, 50 ruff errors swept, one failing test fixed.
- **JSX component CALLS edges** (PR #154): JSX component usage now produces CALLS edges so component-to-component relationships appear in the graph.

### Added
- **Codex platform install support** (PR #177): `code-review-graph install --platform codex` appends a `mcp_servers.code-review-graph` section to `~/.codex/config.toml` without overwriting existing Codex settings.
- **Luau language support** (PR #165, closes #153): Roblox Luau (`.luau`) parsing — functions, classes, local functions, requires, tests.
- **REFERENCES edge type** (PR #217): New edge kind for symbol references that aren't direct calls (map/dispatch lookups, string-keyed handlers), including Python and TypeScript patterns.
- **`recurse_submodules` build option** (PR #215): Build/update can now optionally recurse into git submodules.
- **`.gitignore` default for `.code-review-graph/`** (PR #185): Fresh installs automatically add the SQLite DB directory to `.gitignore` so the database isn't accidentally committed.
- **Clearer gitignore docs** (PR #171, closes #157): Documentation now spells out that `code-review-graph` already respects `.gitignore` via `git ls-files`.

### Changed
- Community detection is now bounded — large repos complete in reasonable time instead of hanging indefinitely.

### Fixed
- **`install_hooks` now merges instead of overwriting** (PR #203, fixes #114): `install_hooks()` previously used `dict.update()` which clobbered any user-defined hooks in `.claude/settings.json`. Now merges new entries into existing hook arrays, preserving user hooks. Creates a backup (`settings.json.bak`) before modification.

## [2.2.2] - 2026-04-08

### Added
- **Kotlin call extraction**: `simple_identifier` + `navigation_expression` support for Kotlin method calls (PR #107)
- **JUnit/Kotlin test detection**: Annotation-based test classification (`@Test`, `@ParameterizedTest`, etc.) for Java/Kotlin/C# (PR #107)

### Fixed
- **Windows encoding crash**: All `write_text`/`read_text` calls in `skills.py` now use `encoding='utf-8'` explicitly (PR #152, fixes #147, #148)
- **Invalid `--quiet` flag in hooks**: Removed non-existent `--quiet` and `--json` flags from generated hook commands (PR #152, fixes #149)

### Housekeeping
- Untracked `.claude-plugin/` directory and added to `.gitignore`
- GitHub issue triage: responded to 30+ issues, closed 14, reviewed 24 PRs

## [2.2.1] - 2026-04-07

### Added
- **Parallel parsing**: `ProcessPoolExecutor` for 3-5x faster builds (`CRG_PARSE_WORKERS`, `CRG_SERIAL_PARSE`)
- **Lazy post-processing**: `postprocess="full"|"minimal"|"none"` parameter, `run_postprocess` MCP tool + CLI command
- **SQLite-native BFS**: Recursive CTE replaces NetworkX for impact analysis (`CRG_BFS_ENGINE`)
- **Configurable limits**: `CRG_MAX_IMPACT_NODES`, `CRG_MAX_IMPACT_DEPTH`, `CRG_MAX_BFS_DEPTH`, `CRG_MAX_SEARCH_RESULTS`
- **Multi-hop dependents**: N-hop `find_dependents()` with `CRG_DEPENDENT_HOPS` (default 2) and 500-file cap
- **Token-efficient output**: `detail_level="minimal"` on 8 tools for 40-60% token reduction
- **`get_minimal_context` tool**: Ultra-compact entry point (~100 tokens) with task-based tool routing
- **Token-efficient prompts**: All 5 MCP prompts rewritten with minimal-first workflows
- **Incremental flow/community updates**: `incremental_trace_flows()`, `incremental_detect_communities()`
- **Visualization aggregation**: Community/file/auto modes with drill-down for large graphs (`--mode`)
- **Token-efficiency benchmarks**: 5 workflow benchmarks in `eval/token_benchmark.py`
- **DB schema v6**: Pre-computed `community_summaries`, `flow_snapshots`, `risk_index` tables
- **Token Efficiency Rules** in all skill templates and CLAUDE.md

### Changed
- CLI `build`/`update` support `--skip-flows`, `--skip-postprocess` flags
- PostToolUse hook uses `--skip-flows` for faster incremental updates
- VS Code extension schema version bumped to v6

### Fixed
- mypy type errors in parallel parsing and context tool
- Bandit false positive on prompt preamble string
- Import sorting in graph.py, main.py, tools/__init__.py
- Unused imports cleaned up in cli.py

### Housekeeping
- Gitignore: untrack `marketing-diagram.excalidraw`, `evaluate/results/`, `evaluate/reports/`
- Updated FEATURES.md, LLM-OPTIMIZED-REFERENCE.md, CHANGELOG.md for v2.2.1

## [2.1.0] - 2026-04-03

### Added
- **Jupyter notebook parsing**: Parse `.ipynb` files — extract functions, classes, imports across Python, R, and SQL cells
- **Databricks notebook parsing**: Parse Databricks `.py` notebook exports with `# COMMAND ----------` cell boundaries
- **Lua language support**: Full parsing for `.lua` files (functions, local functions, method calls, requires) — 20th language
- **Perl XS support**: Parse `.xs` files with improved Perl call detection and test coverage
- **Zero-config onboarding**: `install` now sets up skills, hooks, and CLAUDE.md by default so the graph is used automatically
- **Platform rule injection**: Graph instructions injected into all platform rule files (CLAUDE.md, .cursorrules, etc.) on install
- **Smart install detection**: Auto-detects whether installed via uvx or pip and generates correct `.mcp.json`
- **`--platform claude-code` alias**: Accepts both `claude` and `claude-code` as platform names

### Fixed
- **JS/TS arrow functions indexed**: `const foo = () => {}` and `const bar = function() {}` now correctly appear as nodes (#66)
- **`importers_of` path resolution**: Normalized with `resolve()` to match stored edge targets (#65)
- **Custom embedding models**: Support for custom model architectures and restored model param wiring in search (#79)

## [2.0.0] - 2026-03-27

### Added
- **12 new features**: flows, communities, hybrid search, change analysis, refactoring, hints, prompts, skills, wiki, multi-repo registry, migrations, eval framework
- **14 new modules** (~10,000 lines): `flows.py`, `communities.py`, `search.py`, `changes.py`, `refactor.py`, `hints.py`, `prompts.py`, `skills.py`, `wiki.py`, `registry.py`, `migrations.py`, `eval/`
- **15 new MCP tools**: `list_flows`, `get_flow`, `get_affected_flows`, `list_communities`, `get_community`, `get_architecture_overview`, `detect_changes`, `refactor`, `apply_refactor`, `generate_wiki`, `get_wiki_page`, `list_repos`, `cross_repo_search`, `find_large_functions`, `semantic_search_nodes`
- **5 MCP prompts**: `review_changes`, `architecture_map`, `debug_issue`, `onboard_developer`, `pre_merge_check`
- **7 new CLI commands**: `detect-changes`, `wiki`, `eval`, `register`, `unregister`, `repos`, `install --skills/--hooks/--all`
- **Interactive visualization upgrade**: Detail panel, community coloring, flow path highlighting, search-to-zoom, kind filters

### Security
- Fix path traversal in wiki page reader
- Add regex allowlist for git ref validation
- Add explicit SSL context for MiniMax API

### Fixed
- Fix git diff argument ordering (broke incremental updates)
- Fix `node_qualified_name` schema mismatch in wiki flow query
- Batch N+1 queries in `get_impact_radius` and risk scoring

### Architecture
- Decompose `_extract_from_tree` into 6 focused methods
- Add 17 public query methods to `GraphStore`
- Split `tools.py` into 10 themed sub-modules

## [1.8.4] - 2026-03-20

### Added
- **Vue SFC parsing**: Parse `.vue` Single File Components by extracting `<script>` blocks with automatic `lang="ts"` detection
- **Solidity support**: Full parsing for `.sol` files (functions, events, modifiers, inheritance)
- **`find_large_functions_tool`**: New MCP tool to find functions, classes, or files exceeding a line-count threshold
- **Call target resolution**: Bare call targets resolved to qualified names using same-file definitions (`_resolve_call_targets`)
- **Multi-word AND search**: `search_nodes` now requires all words to match (case-insensitive)
- **Impact radius pagination**: `get_impact_radius` returns `truncated` flag, `total_impacted` count, and accepts `max_results` parameter

### Changed
- Language count updated from 12 to 14 across all documentation
- MCP tool count updated from 8 to 9 across all documentation
- VS Code extension updated to v0.2.0 with 5 new commands documented

### Fixed
- Test assertions updated to handle qualified call targets from `_resolve_call_targets`

## [1.8.3] - 2026-03-20

### Fixed
- **Parser recursion guard**: Added `_MAX_AST_DEPTH = 180` limit to `_extract_from_tree()` preventing stack overflow on deeply nested ASTs
- **Module cache bound**: Added `_MODULE_CACHE_MAX = 15_000` with automatic eviction to prevent unbounded memory growth in `_module_file_cache`
- **Embeddings thread safety**: Added `check_same_thread=False` to `EmbeddingStore` SQLite connection
- **Embeddings retry logic**: Added `_call_with_retry()` with exponential backoff for Google Gemini API calls
- **Visualization XSS hardening**: Added `</` to `<\/` replacement in JSON serialization to prevent script injection
- **CLI error handling**: Split broad `except` into specific `json.JSONDecodeError` and `(KeyError, TypeError)` handlers
- **Git timeout**: Made configurable via `CRG_GIT_TIMEOUT` environment variable (default 30s)

### Added
- **Governance files**: Added CONTRIBUTING.md, SECURITY.md, CODE_OF_CONDUCT.md
- **Project URLs**: Added Homepage, Repository, Issues, Changelog URLs to pyproject.toml metadata

## [1.8.2] - 2026-03-17

### Fixed
- **C# parsing broken**: Renamed language identifier from `c_sharp` to `csharp` to match `tree-sitter-language-pack`'s actual identifier. Previously, all C# files were silently skipped because `_get_parser()` swallowed the `LookupError`.

## [1.8.1] - 2026-03-17

### Fixed
- Add missing `max_nodes` parameter to `get_impact_radius` method signature (caused `NameError` at runtime)
- Fix `.gitignore` test assertion to match expanded comment format

## [1.8.0] - 2026-03-17

### Security
- **Prompt injection mitigation**: Node names are now sanitized (control characters stripped, length capped at 256) before appearing in MCP tool responses, preventing graph-laundered prompt injection attacks
- **Path traversal protection**: `repo_root` parameter now validates that the target directory contains a `.git` or `.code-review-graph` directory, preventing arbitrary file exfiltration via MCP tools
- **VSCode RCE fix**: `cliPath` setting is now scoped to `machine` level only, preventing malicious workspace settings from pointing to attacker-controlled binaries
- **XSS fix in visualization**: `escH()` now escapes quotes and backticks in addition to angle brackets, closing stored XSS via crafted node names in generated HTML
- **SRI for CDN assets**: D3.js script tag now includes `integrity` and `crossorigin` attributes to prevent CDN compromise
- **Secure nonce generation**: VSCode webview CSP nonces now use `crypto.randomBytes()` instead of `Math.random()`
- **Symlink protection**: Build, watch mode, and file collection now skip symbolic links to prevent parsing files outside the repository
- **TOCTOU elimination**: File bytes are now read once, then hashed and parsed from the same buffer, closing the time-of-check-to-time-of-use gap

### Fixed
- **Thread-safe NetworkX cache**: Added `threading.Lock` around graph cache reads/writes to prevent race conditions between watch mode and MCP request handling
- **BFS resource limits**: Impact radius traversal now caps at 500 nodes to prevent memory exhaustion on dense graphs
- **SQL parameter batching**: `get_edges_among` now batches queries to stay under SQLite's variable limit on large node sets
- **Database path leakage**: Improved `.gitignore` inside `.code-review-graph/` with explicit warnings about absolute paths in the database

### Changed
- **Pinned dependency bounds**: All dependencies now have upper-bound version constraints to mitigate supply-chain risks

## [1.7.2] - 2026-03-09

### Fixed
- **Watch mode thread safety**: SQLite connections now use `check_same_thread=False` for Python 3.10/3.11 compatibility with watchdog's background threads
- **Full rebuild stale data**: `full_build` now purges nodes/edges from files deleted since last build
- **Removed unused dependency**: `gitpython` was listed in dependencies but never imported — removed to shrink install footprint
- **Stale Docker reference**: Removed non-existent Docker image suggestion from Python version check

## [1.7.0] - 2026-03-09

### Added
- **`install` command** — primary entry point for new users (`code-review-graph install`). `init` remains as an alias for backwards compatibility.
- **`--dry-run` flag** on `install`/`init` — shows what would be written without modifying files
- **PyPI publish workflow** — GitHub releases now automatically publish to PyPI via API token
- **Professional README** — complete rewrite with real benchmark data:
  - Code reviews: 6.8x average token reduction (tested on httpx, FastAPI, Next.js)
  - Live coding tasks: 14.1x average, up to 49.1x on large repos

### Changed
- README restructured around the install-and-forget user experience
- CLI banner now shows `install` as the primary command

## [1.6.4] - 2026-03-06

### Changed
- **Portable MCP config**: `init` now generates `uvx`-based `.mcp.json` instead of absolute Python paths — works on any machine with `uv` installed
- Removed `_safe_path` symlink workaround (no longer needed with `uvx`)

## [1.6.3] - 2026-03-06

### Added
- **SessionStart hook** — Claude Code now automatically prefers graph MCP tools over full codebase scans at the start of every session, saving tokens on general queries
- `homepage` and `author.url` fields in plugin.json for marketplace discoverability

### Fixed
- plugin.json schema: renamed `tags` to `keywords`, removed invalid `skills` path (auto-discovered from default location)
- Removed screenshot placeholder section from README

## [1.6.2] - 2026-02-27

### Fixed
- **Critical**: Incremental hash comparison bug — `file_hash` read from wrong field, causing every file to re-parse
- Watch mode `on_deleted` handler now filters by ignore patterns
- Removed dead code in `full_build` and duplicate `main()` in `incremental.py`
- `get_staged_and_unstaged` handles git renamed files (`R old -> new`)
- TROUBLESHOOTING.md hook config path corrected

### Added
- **Parser: C/C++ support** — full node extraction (structs, classes, functions, includes, calls, inheritance)
- **Parser: name extraction** fixes for Kotlin/Swift (`simple_identifier`), Ruby (`constant`), C/C++ nested `function_declarator`
- `GraphStore` context manager (`__enter__`/`__exit__`)
- `get_all_edges()` and `get_edges_among()` public methods on `GraphStore`
- NetworkX graph caching with automatic invalidation on writes
- Subprocess timeout (30s) on all git calls
- Progress logging every 50 files in full build
- SHA-256 hashing in embeddings (replaced MD5)
- Chunked embedding search (`fetchmany(500)`)
- Batch edge collection in `get_impact_radius` (single SQL query)
- ARIA labels throughout D3.js visualization
- **CI**: Coverage enforcement (`--cov-fail-under=50`), bandit security scanning, mypy type checking
- **Tests**: `test_incremental.py` (24 tests), `test_embeddings.py` (16 tests)
- **Test fixtures**: C, C++, C#, Ruby, PHP, Kotlin, Swift with multilang test classes
- **Docs**: API response schemas in COMMANDS.md, ignore patterns in USAGE.md

## [1.5.3] - 2026-02-27

### Fixed
- `init` now auto-creates symlinks when paths contain spaces (macOS iCloud, OneDrive, etc.)
- `build`, `status`, `visualize`, `watch` work without a git repository (falls back to cwd)
- Skills discoverable via plugin.json (`name` field added to SKILL.md frontmatter)

## [1.5.0] - 2026-02-26

### Added
- **File organization**: All generated files now live in `.code-review-graph/` directory instead of repo root
  - Auto-created `.gitignore` inside the directory prevents accidental commits
  - Automatic migration from legacy `.code-review-graph.db` at repo root
- **Visualization: start collapsed**: Only File nodes visible on load; click to expand children
- **Visualization: search bar**: Filter nodes by name or qualified name in real-time
- **Visualization: edge type toggles**: Click legend items to show/hide edge types (Calls, Imports, Inherits, Contains)
- **Visualization: scale-aware layout**: Force simulation adapts charge, distance, and decay for large graphs (300+ nodes)

### Changed
- Database path: `.code-review-graph.db` → `.code-review-graph/graph.db`
- HTML visualization path: `.code-review-graph.html` → `.code-review-graph/graph.html`
- `.code-review-graph/**` added to default ignore patterns (prevents self-indexing)

### Removed
- `references/` directory (duplicate of `docs/`, caused stale path references)
- `agents/` directory (unused, not wired into any code)
- `settings.json` at repo root (decorative, not loaded by code)

## [1.4.0] - 2026-02-26

### Added
- `init` command: automatic `.mcp.json` setup for Claude Code integration
- `visualize` command: interactive D3.js force-directed graph visualization
- `serve` command: start MCP server directly from CLI

### Changed
- Comprehensive documentation overhaul across all reference files

## [1.3.0] - 2026-02-26

### Added
- Universal installation: now works with `pip install code-review-graph[embeddings]` on Python 3.10+
- CLI entry point (`code-review-graph` command works after normal pip install)
- Clear Python version check with helpful Docker fallback for older Python users
- Improved README installation section with one-command + Docker option

### Changed
- Minimum Python requirement lowered from 3.11 → 3.10 (covers ~90% of users)

### Fixed
- Installation friction for most developers
