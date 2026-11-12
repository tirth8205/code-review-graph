# All Available Commands

- [CLI commands](#cli-commands)
  - [Exit codes](#exit-codes)
  - [Notes](#notes)
- [Standalone daemon CLI (`crg-daemon`)](#standalone-daemon-cli-crg-daemon)
- [MCP tools](#mcp-tools)
- [Result bounds](#result-bounds)
- [MCP prompts](#mcp-prompts-5-workflow-templates)
- [Skills and slash commands](#skills-and-slash-commands)

## CLI Commands

Start with `code-review-graph install && code-review-graph build`, then
`code-review-graph detect-changes --brief`. `code-review-graph` on its own
prints every command in groups. Run `code-review-graph <command> --help` for the
full option list. A mistyped command exits 2 and suggests the closest one:

```
code-review-graph: error: unknown command 'biuld'. Did you mean build? Run code-review-graph --help to see every command.
```

```bash
# Setup
code-review-graph --version         # Show the version (or -v)
code-review-graph install           # Set up the AI coding tools found on this machine (alias: init)
code-review-graph install --dry-run # Show each file's diff; write nothing
code-review-graph install --gitignore  # Also add the files this run creates to .gitignore (off by default)
code-review-graph install --platform codex  # Set up one tool, found here or not: Codex MCP, hooks, and global Skill
code-review-graph install --no-hooks        # Skip hooks (also --no-skills, --no-instructions)
code-review-graph install -y                # Answer yes to the question about instruction files
code-review-graph install --steering        # Opt in: graph context beside Claude Code searches and reads
code-review-graph install --no-steering     # Remove only that hook
code-review-graph doctor                    # Check every copy, the MCP entries, the graph and the hooks (read-only)
code-review-graph doctor --json             # The same report as one JSON object (--repo PATH for another repo)
code-review-graph uninstall                 # Remove CRG configs, hooks, skills, and data
code-review-graph uninstall --dry-run       # List what it would remove; change nothing
code-review-graph uninstall --keep-data     # Remove integrations, keep graph databases
code-review-graph uninstall --platform codex  # Unbind one platform (keeps graph data and others)
code-review-graph uninstall --all-repos        # Also clean every repository in the registry
code-review-graph uninstall --keep-user-configs  # Clean repositories only; leave files in your home folder
code-review-graph uninstall -y                 # Apply without asking

# Build and update
code-review-graph build                        # Full build
code-review-graph build --data-dir /mnt/graphs/myrepo  # Keep graph.db outside the repo (or set CRG_DATA_DIR); also on update,
                                                #   postprocess, embed, watch, status, context-history, forget, visualize, wiki, dead-code
code-review-graph build --skip-flows           # Parse + signatures + FTS only
code-review-graph build --skip-postprocess     # Raw parse only
code-review-graph build -q                     # No output (-q/--quiet also on update and status)
code-review-graph build --enrich               # Also resolve Python calls with Jedi (needs [enrichment]);
                                                #   also on update, or CRG_ENABLE_ENRICHMENT=1. Off by default
code-review-graph update                       # Incremental update from the last-built commit
code-review-graph update --base origin/main    # Custom base ref
code-review-graph update --brief               # Update graph, then show the risk panel
code-review-graph update --brief --verify      # ...and cross-check against tiktoken
code-review-graph update --wait                # Wait for a running build to finish, then update
code-review-graph build --follow-symlinks      # Also follow symlinked folders, inside or outside the repo
                                                #   (off by default; also on update and watch, or CRG_FOLLOW_SYMLINKS=1)
code-review-graph postprocess                  # Re-run flows, communities, FTS
code-review-graph postprocess --no-flows       # Skip a step: --no-flows, --no-communities, --no-fts
code-review-graph forget PATH [PATH ...]       # Drop parsed files from the graph (no full rebuild)
code-review-graph forget src/legacy --dry-run  # Preview which files would be forgotten
code-review-graph forget PATH --wait           # Wait for a running write to finish, then forget
code-review-graph embed --provider local       # Compute vector embeddings for semantic search
code-review-graph embed --wait                 # Wait for a running write to finish, then embed
code-review-graph update --embedding-provider local --embedding-model all-MiniLM-L6-v2
                                                # Refresh an existing embedding index (default: off)

# Monitor and inspect
code-review-graph status                       # Graph statistics (no graph: exit 1, no DB created)
code-review-graph status --json                # One JSON object
code-review-graph context-history              # Per-day trend of the opt-in context savings log
code-review-graph context-history --by tool --days 7  # Per tool, last 7 UTC days (--json for JSON)
code-review-graph watch                        # Auto-update on file changes (needs an existing graph)
code-review-graph visualize                    # Interactive HTML graph (needs an existing graph)
code-review-graph visualize --format graphml   # Formats: html, json, graphml, cypher, obsidian, svg
                                                #   Written to the data folder (.code-review-graph/ by default) as
                                                #   graph.<ext>, or obsidian/. svg needs matplotlib:
                                                #   pip install "code-review-graph[eval]" (without it: exit 1)
code-review-graph visualize --mode community   # Modes: auto (default), full, community, file
code-review-graph visualize --serve            # Serve graph.html on localhost:8765

# Neighbourhood view: draw a symbol's surroundings instead of the repository.
# The whole-repo page collapses to one bubble per community past 3000 nodes or
# 9000 edges; a seeded page ships only the nodes within --depth hops.
code-review-graph visualize --seed-symbol login             # depth 2 by default
code-review-graph visualize --seed-symbol login --depth 3   # more context
code-review-graph visualize --seed-file src/auth.py         # a file and its symbols
code-review-graph visualize --seed-changed                  # the files in this review
code-review-graph visualize --seed-changed --seed-changed-base origin/main
code-review-graph visualize --seed-flow "login request"     # an execution flow
code-review-graph visualize --path-from login --path-to audit_log
code-review-graph visualize --seed-symbol login --render-depth 0  # expand on click
code-review-graph visualize --seed-symbol login --max-nodes 300  # hard cap
code-review-graph visualize --seed-symbol login --sidecar   # payload in graph.data.js

# Analysis
code-review-graph detect-changes               # Risk-scored change analysis (read-only): the last commit
                                                #   (HEAD~1) plus uncommitted and new files
code-review-graph detect-changes --base HEAD~3 # Custom base revision (one Git does not know: exit 1)
code-review-graph detect-changes --base origin/main # Branch refs use their merge base with HEAD
code-review-graph detect-changes --brief       # Summary, the top three things to review, and the token-savings panel
code-review-graph detect-changes --brief --verify  # ...and cross-check against tiktoken
code-review-graph detect-changes --churn       # Add opt-in change-frequency risk (CRG_CHURN_WINDOW_DAYS, default 90)
                                                #   The MCP tools (detect_changes_tool, get_minimal_context_tool) always include it.
                                                #   Cached per commit; CRG_CHURN_TIMEOUT (5s) and CRG_CHURN_MAX_COMMITS
                                                #   (2000) bound the git log. A repository that cannot answer in time is
                                                #   recorded as unavailable for the life of the process, so the timeout is
                                                #   paid once, and the response says so: `churn_status` is "unavailable"
                                                #   and the summary carries a "Degraded" line.
code-review-graph detect-changes --details all # Add bounded graph context under "details" (JSON only):
                                                #   risk, communities, hubs, coupling, impact, gaps, graph.
                                                #   Off by default; the GitHub Action's `details` input uses it.
code-review-graph dead-code                    # Functions/classes with no callers or test references
code-review-graph dead-code --kind Function --file-pattern src/ --json
code-review-graph dead-code --limit 20         # 0, the default, means no limit

# Read-only graph queries (CLI mirrors of the MCP tools)
code-review-graph query callers_of <target>    # Takes every query_graph_tool pattern: callers_of,
                                                #   references_to, callees_of, imports_of, importers_of,
                                                #   children_of, tests_for, inheritors_of, triggers_of,
                                                #   triggered_by, publishers_of, listeners_of, advises,
                                                #   advised_by, handlers_of, endpoints_for, consumers_of,
                                                #   file_summary, path_to
code-review-graph query path_to <a> --to <b>  # How a reaches b, hop by hop
code-review-graph impact [--files F ...] [--depth N] [--max-results N] [--base REF] [--detail-level compact] [--max-files N]
code-review-graph search <query> [--kind Function] [--limit N]
code-review-graph flows [--sort criticality] [--limit N] [--kind KIND]
code-review-graph flow --id ID | --name NAME [--source]
code-review-graph communities [--sort size] [--min-size N]
code-review-graph community --id ID | --name NAME [--members]
code-review-graph architecture [--detail-level minimal|standard]
code-review-graph large-functions [--min-lines N] [--kind Function] [--path SUBSTR] [--limit N]
code-review-graph refactor rename --old-name A --new-name B
code-review-graph refactor dead_code [--kind Function] [--path SUBSTR]
code-review-graph refactor suggest

# Wiki
code-review-graph wiki                         # Markdown wiki in .code-review-graph/wiki/ (needs an existing graph)
code-review-graph wiki --force                 # Rewrite every page, even unchanged ones

# Multi-repo
code-review-graph register <path> [--alias name]  # Register a repository
code-review-graph unregister <path_or_alias>       # Remove from registry
code-review-graph repos                            # List registered repositories

# Daemon (multi-repo watcher), installed with the package
code-review-graph daemon start [--foreground]       # Start the watch daemon
code-review-graph daemon stop                       # Stop the daemon
code-review-graph daemon restart [--foreground]     # Restart the daemon
code-review-graph daemon status                     # Daemon status and repos
code-review-graph daemon logs [--repo ALIAS] [--follow] [--lines N]  # Daemon or per-repo logs (default 50 lines)
code-review-graph daemon add <path> [--alias NAME]  # Add a repo to the daemon config
code-review-graph daemon remove <path_or_alias>     # Remove a repo from the daemon config

# Evaluation
code-review-graph eval --all                   # Run every benchmark on every repo config (bare `eval` runs nothing)
code-review-graph eval --benchmark token_efficiency --repo flask  # Pick benchmarks and repos (comma-separated); --repo takes config names, not paths
code-review-graph eval --report                # Write evaluate/reports/summary.md from evaluate/results and print README tables
                                                #   (only each repo's newest run of each benchmark)
                                                #   (--output-dir sets the results folder that runs write and --report reads)
code-review-graph eval --benchmark search_quality --embed [--embed-provider local|openai|google|minimax|voyage] [--embed-model M]  # --embed builds vectors; agent_baseline,
                                                #   search_quality and multi_hop_retrieval need it

# Hook entry points (the hooks install writes run these; they read the hook event JSON on stdin).
# --help and the command list leave them out, but they still run.
code-review-graph hook-update                  # Update the graph of the repo that holds the edited file
code-review-graph hook-update --existing-only  # The same, but never build a new graph (the Codex hook)
code-review-graph hook-status                  # Graph status for the event's cwd
code-review-graph enrich                       # Graph context for a search or read (the --steering hook)

# Server
code-review-graph serve                        # Start MCP server (stdio)
code-review-graph serve --auto-watch           # Also watch for file changes while the server runs
code-review-graph serve --http                 # Streamable HTTP on 127.0.0.1:5555 (--host, --port)
code-review-graph serve --http --multi-repo    # One HTTP server for every registered repository
code-review-graph serve --tools query_graph_tool,detect_changes_tool  # Tool allowlist (or CRG_TOOLS)
code-review-graph serve --multi-worktree --repo /path/to/repo  # One stdio session, one child per linked worktree
code-review-graph mcp                          # Alias for serve; accepts --repo, --auto-watch and --multi-worktree
```

`--multi-repo` lets one `serve --http` process serve several repositories, so
the embedding model is loaded once. It serves the repositories in the registry
(`code-review-graph register <path>`), read again on every call, so `register`
and `unregister` take effect without a restart. Every tool call must pass
`repo_root`, the absolute path of a registered repository; `list_repos_tool`
lists them. A call with no `repo_root`, a relative path or a path that is not
registered is refused with a message saying so. It never falls back to `--repo`
or the server's working directory. The usual project-root check still applies.
`--multi-repo` needs `--http` and cannot be combined with `--repo`,
`--auto-watch`, `--multi-worktree` or `CRG_DATA_DIR`. To keep several graphs
current, run the daemon (`code-review-graph daemon`). Without `--multi-repo`,
`serve --http` keeps the old fallback.

`--multi-worktree` is an opt-in stdio mode for parallel reviews. The server
starts one regular `serve --repo <root>` child lazily for the primary checkout
and each linked worktree requested through `repo_root`. Only paths reported by
`git worktree list` for the same Git common directory are accepted. Each child
uses its worktree's normal `.code-review-graph/graph.db` or its per-repository
Registry `data_dir`, so graphs from different commits cannot be mixed.

The mode rejects a process-wide `CRG_DATA_DIR`, because that would force all
worktrees into one database. Configure external storage with a separate,
non-overlapping Registry `data_dir` entry per worktree instead; the router
rejects two worktrees that resolve to the same graph database. `--multi-worktree`
is stdio only and cannot be combined with `--http`.

### Exit codes

| Code | Meaning | Examples |
|------|---------|----------|
| `0` | Done, including when there was nothing to do. | `No changes detected.`; `uninstall` answered no (`Aborted.`) |
| `1` | It failed, and one `Error:` line (or a line saying what is missing) on stderr says why. | no graph yet; an unusable `graph.db`; git missing or too slow; a `--base` Git does not know; `--repo` that is not a project root; `doctor` found a problem |
| `2` | The command line is wrong, so nothing ran. | an unknown command (with "Did you mean ...?"); an unknown flag; flags that cannot go together, such as `detect-changes --details all --brief`; a `visualize` seed it cannot find |
| `75` | Another write to this repository's graph is running, so this one did nothing. | `build`, `update`, `postprocess`, `forget` or `embed` without `--wait` |
| `130` | Stopped with Ctrl-C. | Ctrl-C at `install`'s question; nothing was written |

A hook or CI job can tell these apart without reading the message. `75` is
"skipped", not "failed": the running write covers the same work.

### Notes

- `update` and `detect-changes` need a Git repository or an SVN working copy.
  In SVN, `--base` takes a revision or range such as `r100:HEAD`; with no
  revision, both commands use the working-copy edits from `svn status`. In Git,
  `update` with no `--base` diffs from the commit the graph was last built at; when that commit is
  missing (fresh graph, rewritten history, shallow clone) it falls back to a
  full rebuild.
- `update` prints a `Warning:` line on stderr naming files that failed to
  parse. Those files keep their previous graph rows.
- `detect-changes --brief` is read-only against the existing graph and takes
  about a second. `update --brief` re-parses changed files first, then prints
  the same summary and panel. Use `update --brief` after a rebase or a large
  change set, or when the graph may be stale.
- `detect-changes --brief` scores risk from 0.00 to 1.00 (high above 0.70,
  medium above 0.40) and lists up to three things to review first, each with
  its file and line, its callers and its tests:

  ```
    - Overall risk score: 0.80 of 1.00 (high)

  Review first (risk out of 1.00):
    1. check_password  app/auth.py:1  risk 0.80 (high)  called by login  no tests found
    2. login  app/auth.py:7  risk 0.80 (high)  called by handle_login  tested by test_login_ok
  ```
- With no `--base`, `detect-changes` diffs against `HEAD~1`, so it reviews the
  last commit plus uncommitted and new files. A new file Git does not track is
  reviewed whole. In a repository with one commit, `HEAD~1` does not exist, so
  it prints this on stderr and reviews uncommitted and new files:

  ```
  Note: this repository has one commit; reviewing uncommitted and new files instead.
  ```

  With no commits at all, the note says `this repository has no commits yet`.

  In a shallow clone, as many CI jobs make, the note says the clone is shallow
  and that `git fetch --deepen=1` brings the last commit back.
- A `--base` Git does not know exits 1 instead of reviewing the working tree.
  The MCP review tools answer `status: "error"` with the same words:

  ```
  Error: base 'nope' is not a known revision. Check it with `git branch -a`, or run `git fetch origin <branch>` in a shallow clone.
  ```
- `status`, `detect-changes`, `visualize`, `wiki`, `watch`, `forget`,
  `dead-code` and the read-only graph queries (`query` to `refactor` above)
  exit 1 when no graph exists and do not create one. `status`,
  `detect-changes`, `visualize`, `wiki`, `watch`, `forget` and `dead-code`
  still move a legacy top-level `.code-review-graph.db` into
  `.code-review-graph/graph.db` before running.
- One write runs per repository at a time. `build`, `update`, `postprocess`,
  `forget` and `embed` take a lock on `write.lock` next to `graph.db`; if
  another one holds it, they say so, do nothing and exit 75. Pass `--wait` to
  wait for it instead. `watch` takes the lock for each write and keeps its
  changes to retry if the lock is held.
- `install` with no `--platform` (the same as `--platform all`) writes MCP
  entries, skills, hooks and instruction files only for the tools it finds on
  this machine, then names the rest:

  ```
  Not found here, so not set up: cursor, windsurf, zed, ...
  Add one anyway with: code-review-graph install --platform NAME
  ```

  `install --platform NAME` sets that tool up whether it is found or not.
- `install` lists the files it will change and asks about the instruction
  files (`CLAUDE.md`, `AGENTS.md` and the rest) before it writes anything;
  `-y` answers yes. Ctrl-C at the question stops with nothing written (exit
  130). With no terminal, as when an AI agent runs it, it takes the default
  and says so; `uninstall` with no terminal takes no:

  ```
  No terminal: taking the default (yes). Use --no-instructions to skip them.
  No terminal: taking the default (no). Use --yes to go ahead.
  ```
- The MCP entry and every hook run the copy of code-review-graph that ran
  `install`: `poetry run code-review-graph` or `uv run code-review-graph`
  inside a Poetry or uv project, `uvx code-review-graph` only when `install`
  itself ran from `uvx` or a `uv tool` install, and otherwise
  `<python> -m code_review_graph`, with the full path of the Python that ran
  `install`. Hooks name that Python (or `uvx`) and fall back to
  `code-review-graph` on PATH when it is gone, as on a teammate's machine.
  Run `install` again after you move or upgrade the package.
- `install` keeps a record of the files it writes inside the repository in
  `.code-review-graph/install-manifest.json`, also with `--data-dir` or
  `CRG_DATA_DIR`. `build` and every review leave out the listed files while
  Git does not track them, so your first review shows your change, not
  `.mcp.json` and hook scripts. Once you commit one, it counts like any other
  file. `uninstall` takes each file it removes or edits off the record, and
  deletes the record when it is empty. A listed file Git does not track that
  holds nothing once our entries are gone, such as `{"mcpServers": {}}`, is
  removed with the empty folders `install` made, not left behind.
- `doctor` changes nothing. It checks every `code-review-graph` on PATH and its
  version, the version each MCP entry in this repository's config files runs
  (read from a `uvx` pin or `uv tool list`, never by starting `uvx`), the
  graph's schema, whether it was built at HEAD and whether the last build
  finished, and the git and editor hooks and the copy each one runs. Each
  failed check has a `Fix:` line. It ends with `All good.` (exit 0) or a count
  such as `1 problem found.` (exit 1), so it also fails when there is no graph
  yet.
  See [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
- `build` ends with a next step. A build that finds no files prints a warning
  on stderr instead:

  ```
  Next: code-review-graph detect-changes --brief, or ask your assistant "Review my changes".
  Warning: no source files found under <root>. Is this the project root?
  ```

  A new graph logs one line, `Created graph database (schema v15).`, not
  every migration step. Upgrading an older graph logs each step, and one
  coming from below schema 15 then logs one line asking for one `build`.
- `visualize` without a seed exports the whole repository, and falls back to
  community (or file) bubbles once the rendered graph passes 3000 nodes or
  9000 edges. With `--seed-symbol`, `--seed-file`, `--seed-changed`,
  `--seed-flow` or `--path-from/--path-to` it exports a neighbourhood instead:
  the nodes within `--depth` hops of the seed and the edges among them, with
  everything else left out of the payload rather than drawn dimmed. A seeded
  page always uses the full renderer and never aggregates.
- A hop is any edge other than `CONTAINS`, such as `CALLS`, `IMPORTS_FROM`,
  `INHERITS`, `REFERENCES` or `TESTED_BY`. `CONTAINS` is structural and is not a hop, so a
  depth-2 neighbourhood of one function does not swallow its whole module; the
  containing `File` nodes are still shipped so clustering and collapse work.
- `--render-depth` (default 1) is how many hops are drawn on open. Clicking a
  node on the edge of what is drawn reveals its next ring, `+1 hop` reveals a
  whole ring, and both read from the payload already in the page.
- `--path-from A --path-to B` highlights the shortest path through `CALLS`,
  `IMPORTS_FROM` and `INHERITS`, following edge direction where one exists and
  falling back to an undirected route otherwise. The page says which it used.
- The default output is still a single self-contained `graph.html` you can
  email. `--sidecar` moves the payload into `graph.data.js` next to it, which
  is only worth it for a large neighbourhood.
- `--max-nodes` (default 1500) is a hard cap on the nodes in the payload, not
  a hint. The outermost hop goes first; once the outer hops are gone the seed
  set itself is trimmed, best-connected first by whole-graph degree, ties
  broken by name so two runs of the same command agree. A `--seed-changed`
  run over a large review is exactly the case where the seed set is the large
  thing, so the command prints how many seed nodes it dropped and the page
  says so too. The nodes on a `--path-from/--path-to` answer are placed
  first and are never dropped; a `--max-nodes` below the path length is
  refused rather than half-answered.
- Flag combinations that cannot mean anything are refused, not ignored:
  `--seed-changed-base` without `--seed-changed`; `--depth`, `--render-depth`
  or `--max-nodes` without a seed; any seed or tuning flag with a
  non-`html` `--format`; `--sidecar` with a non-`html` `--format`;
  `--mode community` or `--mode file` with a seed (they aggregate, which is
  the opposite of a neighbourhood); and a `--render-depth` above `--depth`.
- `install` (with `--platform all`, the default, or `claude-code`, `codex` or
  `qoder`) appends a Git `pre-commit` hook that runs `update --quiet` and then
  `detect-changes --brief` before each commit, so a commit shows only the risk
  summary. The hook skips linked worktrees unless
  `CRG_HOOK_WORKTREES=1` is set, so a worktree does not build a second graph
  for another branch.
- `install` also appends `post-checkout`, `post-merge` and `post-rewrite` Git
  hooks that run `update --quiet` after a branch switch, a merge, a rebase and
  `commit --amend`, with the same worktree rule. An existing hook written in a
  language other than shell is left alone, `--no-hooks` skips all four, and
  `uninstall` removes them.
- For an empty or incomplete graph, run `code-review-graph build`. See
  docs/TROUBLESHOOTING.md, "Empty or incomplete graph".
- `context-history` reads a log that is off by default. Set
  `CRG_CONTEXT_HISTORY=1` where the MCP server or the CLI runs to turn it on.
  See [USAGE.md](USAGE.md#context-history).

## Standalone Daemon CLI (`crg-daemon`)

`crg-daemon` is installed with `code-review-graph` and mirrors the
`code-review-graph daemon` subcommands:

```bash
crg-daemon start [--foreground]       # Start the multi-repo watch daemon
crg-daemon stop                       # Stop the daemon and all watcher processes
crg-daemon restart [--foreground]     # Restart (stop + start)
crg-daemon status                     # Daemon status, repos, and process liveness
crg-daemon logs [--repo ALIAS] [-f] [-n N]  # Tail daemon or per-repo log files
crg-daemon add <path> [--alias NAME]  # Add a repository to watch.toml
crg-daemon remove <path_or_alias>     # Remove a repository from watch.toml
```

### Configuration

The daemon reads `~/.code-review-graph/watch.toml` (or `$CRG_HOME/watch.toml`):

```toml
[daemon]
session_name = "crg-watch"   # logical daemon name
log_dir = "/home/user/.code-review-graph/logs"   # absolute path; ~ is not expanded (default: ~/.code-review-graph/logs, or $CRG_HOME/logs)
poll_interval = 2            # seconds between config file polls

[[repos]]
path = "/home/user/project-a"
alias = "project-a"

[[repos]]
path = "/home/user/project-b"
alias = "project-b"
```

The daemon spawns one `code-review-graph watch` child process per repo with
`subprocess.Popen`. It polls the config file and starts or stops children as
repos are added or removed. A health check every 30 seconds restarts dead
watchers. No tmux or screen is needed.

## MCP Tools

Every `repo_root` parameter is optional and auto-detected from the current
directory when omitted. The one exception is `serve --http --multi-repo`, where
every call must pass the absolute path of a registered repository (see Server
below).

Every tool carries `readOnlyHint: true` except the five that write:
`build_or_update_graph_tool`, `run_postprocess_tool`, `embed_graph_tool`,
`apply_refactor_tool` and `generate_wiki_tool`. A client can use the hint to
skip its approval prompt for the read-only ones.

Where a tool takes `base`, a local or remote branch ref (for example
`origin/main`) resolves to its merge base with `HEAD`, so the diff covers only
this branch's own commits. Commit ids and revision expressions such as `HEAD~3`
are used as given. If Git cannot find the merge base (shallow clone), the ref
is used as given.

### Core Tools

#### `build_or_update_graph_tool`
```
full_rebuild: bool = False           # True re-parses every file
repo_root: str | None
base: str | None = None              # Diff base; None means the commit the graph was last built at
postprocess: str = "full"            # "full", "minimal" (signatures + FTS), or "none"
recurse_submodules: bool | None      # None falls back to CRG_RECURSE_SUBMODULES
embedding_provider: str | None       # Refresh an existing embedding index; needs embedding_model
embedding_model: str | None          # Exact model for embedding_provider
```
If some files fail to parse, `status` is `"partial"` and the `summary` names
them. Their previous graph rows are kept.

One write runs per repository at a time. When another write to the graph is
running, this tool, `run_postprocess_tool` and `embed_graph_tool`
return at once with `status: "contended"`, `skipped: true` and the `holder_pid`;
retry once it ends. An embedding refresh on a graph with no embeddings yet embeds
nothing and sets `embeddings_refresh_skipped: true`; run `embed_graph_tool` once
first.

#### `run_postprocess_tool`
```
flows: bool = True
communities: bool = True
fts: bool = True
repo_root: str | None
embedding_provider: str | None       # Refresh an existing embedding index; needs embedding_model
embedding_model: str | None
```

#### `get_minimal_context_tool`
```
task: str = ""                       # What you are doing
changed_files: list[str] | None      # Auto-detected from VCS when omitted
repo_root: str | None
base: str = "HEAD~1"
```
Answers `status: "not_ready"` with `reason` `missing_graph`, `empty_graph` or
`stale_graph`, and suggests `build_or_update_graph_tool`. `stale_graph` means
the graph was built at another commit and differs from it. A graph the
pre-commit hook updated just before a commit counts as current: when every file
changed since the graph's commit has the same content in the graph, nothing is
stale.

Every `next_tool_suggestions` list, here and in the other tools, names tools as
the server registers them, ending in `_tool`.

#### `get_impact_radius_tool`
```
changed_files: list[str] | None  # Auto-detected from VCS
max_depth: int = 2               # Hops in graph
max_results: int                 # Nodes to traverse to; default CRG_MAX_IMPACT_NODES, or 500
repo_root: str | None
base: str = "HEAD~1"
detail_level: str = "standard"   # "standard", "minimal", or "compact"
max_files: int = 25             # Compact-mode display limit (hard ceiling 200)
resolution: str = "all"          # "all" or "direct" (only calls bound to a node)
```
An impacted node that calls or references the changed code in one hop carries
`call_site` (`line`, plus `file` only when the call is written outside the node's
own `file_path`) and `call_site_count` when there is more than one.
`unresolved_call_sites` counts call sites that name a changed symbol but were
never bound to it, so an empty radius is not read as proof of absence.
Changed files the graph has no nodes for are listed in `unmatched_files`, and
when none of the changed files is in the graph, `risk` is `"unknown"`.
Responses may include estimated `context_savings` metadata.

Use `detail_level="compact"` when the next step is opening affected files rather
than inspecting individual call sites. It returns ranked repository-relative
`changed_files` and `impacted_files`, with `changed_file_count`,
`changed_files_omitted`, `impacted_file_count`, `impacted_files_omitted`,
`total_impacted`, `truncated`, and `unresolved_call_sites`. `unmatched_files`
(with `unmatched_files_total`) lists the changed files the graph has no nodes for.
Paths outside the repository keep their absolute identity.
Files follow the graph's best-first node ranking. Counts describe the traversed
radius; `truncated` remains true when traversal or a file list was bounded.
The mode omits node/edge records and repeated qualified names. `max_files` limits
each displayed file list, independently of the `max_results` traversal budget.
Standard output and its existing limits are unchanged; minimal output remains a
summary without file paths.

The CLI exposes the same controls:

```bash
code-review-graph impact --files src/auth.py --detail-level compact --max-files 20
code-review-graph impact --files src/auth.py --detail-level minimal
```

#### `query_graph_tool`
```
pattern: str    # callers_of, references_to, callees_of, imports_of, importers_of,
                # children_of, tests_for, inheritors_of, triggers_of, triggered_by,
                # publishers_of, listeners_of, advises, advised_by, handlers_of,
                # endpoints_for, consumers_of, file_summary, path_to
target: str     # Node name, qualified name, or file path
repo_root: str | None
detail_level: str = "standard"   # "standard" or "minimal"
max_results: int = 100           # Minimal mode also caps visible results at 5
resolution: str = "all"          # "all", "direct", or "unresolved"
to: str | None                   # path_to only: the node or file to reach
```
`callers_of`, `callees_of` and `references_to` return one row per call site, not
one per node. Each row carries `call_site` (`line`, plus `file` only when the
call is written outside the row's own `file_path`), and rows whose target was
matched by bare name alone carry `target_resolution: "unresolved"`. The response
adds `distinct_nodes` and a `resolution_split` of the whole answer. When some of
the rows are only name matches and `resolution` is `"all"`, a short `confidence`
sentence says how many. Call sites
are ordered so every distinct node appears before any node's second call site,
so truncation never costs a caller. Within that order, calls bound to a node come
before bare-name guesses.

A full name that one real symbol has resolves to it, even when tests or test
helpers share the name; the summary says how many it skipped. When two real
symbols share the name, `status` is `"ambiguous"` and the response lists
qualified names to pick from (five short rows in minimal mode).

`path_to` answers how `target` reaches `to`, with one row per hop through calls,
references, inheritance, imports and containment. It picks the route whose weakest
hop is strongest, and the shortest of those. Each hop's `certainty` says how it was
bound: `direct` to that node, `unique_name` through a bare name only one symbol
has, or `shared_name` through a name several share. `certainty` and `weakest_hop`
on the response give the weakest one, which is the hop to check first. How a hop
was bound ranks first and the edge's stored `confidence` only breaks ties, since
graphs built before #984 was fixed store 1.0 even on an edge that names no node.
In standard mode a hop shows `confidence` when it is below 1.0. When there is no
route, `results` is empty, and `reverse_route_exists` is set if one runs the other
way. If the search stops at its visit limit first, `search_incomplete` is set
instead, as a route may still exist. Callbacks and dynamic dispatch are not in the
graph, so no route does not prove there is none. An unclear end comes back as
`not_found` or `ambiguous`, with `endpoint` saying which. Minimal mode names each
hop by its qualified name from the repository root, and adds `file` only when the
call is written outside the source's own file.

`tests_for` rows that are guesses carry `inferred_by`, in minimal mode too:
`naming_convention` for a test found by its name, and `ambiguous_call` for a PHP
test whose call could mean this method or a same-named one in another class the
test imports, as with `app(Service::class)->run()`. The summary says how many
rows are guesses. A guess is never counted as coverage.

#### `get_review_context_tool`
```
changed_files: list[str] | None
max_depth: int = 2
include_source: bool = True
max_lines_per_file: int = 200    # Capped at 500
repo_root: str | None
base: str = "HEAD~1"
detail_level: str = "standard"   # "standard" or "minimal"
max_results: int = 100           # Graph nodes per list (max 100) and edges (max 150)
max_files: int = 25              # Files listed and given snippets (max 200)
```
`changed_files` is ordered by risk score (highest first, per file in
`context.file_risk`), and both `max_files` and the shared 800-line snippet
budget are spent in that order. The budget buys whole changed regions -- the
diff hunks, each widened to its enclosing definition when that definition is
short enough to read whole -- granted round-robin across the ranked files, so
a file with one small change costs one small grant and a file with six hunks
gets six turns. No single file may hold more than 40% of the budget.
`context.source_regions` reports `shown`, `total`, and an `incomplete` map of
the files whose regions did not all fit. Each list reports its untruncated
`*_total`, and `context.truncated` / `context.source_truncated` mark any cut.
Changed files the graph has no nodes for are listed in `unmatched_files`, and
when none of the changed files is in the graph, `risk` is `"unknown"`.
Responses may include estimated `context_savings` metadata.

#### `traverse_graph_tool`
```
query: str
depth: int = 3                  # Clamped to 1-6
mode: str = "bfs"               # "bfs" or "dfs"
token_budget: int = 2000        # At most 10000 approximate tokens; the walk stops there
repo_root: str | None
```
`truncated` means the budget ran out before the walk did.

#### `semantic_search_nodes_tool`
```
query: str           # Search string
kind: str | None     # File, Class, Function, Type, Test, YamlPath, DocSection,
                     # Variable, Output, JsonPath
limit: int = 20      # At most 100; at most 5 with detail_level "minimal"
repo_root: str | None
model: str | None    # Embedding model (falls back to provider-specific env vars)
provider: str | None # local, openai, google, minimax, voyage
detail_level: str = "standard"
include_source: bool = False     # Attach each hit's source lines
expand_neighbors: bool = False   # With include_source: also the top hits' callers and callees
max_source_lines: int = 60       # Lines per snippet (1-60)
```
Search ranks three times what it shows. `total` and `result_count` count the
matches it ranked, `results_omitted` those not shown, and `total_is_lower_bound`
says there may be more.

`include_source=True` lets you answer from the reply instead of opening the
files. All snippets share one budget of 240 lines, or 700 with
`expand_neighbors=True`, which adds up to four callers and callees for each of
the top three hits. A cut snippet carries `source_truncated`. Source is not
attached when `detail_level="minimal"`.

`kind` is applied before the hits are cut. Test code ranks below production
code unless the query asks about tests (`kind="Test"`, or a word such as
"test", "spec", "fixture" or "mock"). JSON ranks below code unless you ask for
it (`kind="JsonPath"`, a `$` path or a `.json` name). Liquid `Output` nodes are
found only with `kind="Output"`.

#### `embed_graph_tool`
```
repo_root: str | None
model: str | None    # Embedding model name
provider: str | None # local, openai, google, minimax, voyage
```
Local embeddings need `pip install "code-review-graph[embeddings]"`, and Google
needs `pip install "code-review-graph[google-embeddings]"`. The OpenAI-compatible,
MiniMax and Voyage providers use the standard library HTTP client. Every cloud
provider reads its key from environment variables (see the README).

#### `list_graph_stats_tool`
```
repo_root: str | None
```

#### `find_large_functions_tool`
```
min_lines: int = 50                # Minimum line count
kind: str | None                   # File, Class, Function, Test, YamlPath, DocSection, or JsonPath
file_path_pattern: str | None      # File path substring
limit: int = 50                    # At most 100; at most 25 with detail_level "minimal"
repo_root: str | None
detail_level: str = "standard"     # "minimal" gives fewer, smaller rows
```
`total` and `result_count` count every match before the cut.

#### `get_docs_section_tool`
```
section_name: str    # usage, review-delta, review-pr, commands, legal, watch, embeddings, languages, troubleshooting
repo_root: str | None
```

### Flow Tools

#### `list_flows_tool`
```
sort_by: str = "criticality"  # criticality, depth, node_count, file_count, name
limit: int = 50               # Flows returned (max 200)
kind: str | None              # Entry point kind (e.g. "Test", "Function")
repo_root: str | None
detail_level: str = "standard"
```

#### `get_flow_tool`
```
flow_id: int | None          # Database ID from list_flows_tool
flow_name: str | None        # Name to search (partial match); ignored when flow_id is given
include_source: bool = False # Source snippet per step
repo_root: str | None
max_steps: int = 50          # Capped at 200; flow.total_steps reports the full count
max_source_lines: int = 400  # Shared across all steps; capped at 2000
```

#### `get_affected_flows_tool`
```
changed_files: list[str] | None  # Auto-detected from VCS
base: str = "HEAD~1"
repo_root: str | None
detail_level: str = "standard"   # "standard" full step details, "minimal" metadata only
max_flows: int = 50
```
Standard mode carries a full `steps` list per flow, so it caps visible flows
at 25 and spends a shared 400-step budget across them; minimal mode caps at
500. `total` reports the untruncated flow count. See #849.

### Community Tools

#### `list_communities_tool`
```
sort_by: str = "size"    # size, cohesion, name
min_size: int = 0
repo_root: str | None
detail_level: str = "standard"
max_results: int = 50    # Communities returned (max 200)
max_members: int = 10    # Member names per community in standard mode (max 25)
```
`size` reports the true member count; `members_truncated` marks a cut member list.

#### `get_community_tool`
```
community_name: str | None   # Name to search (partial match); ignored when community_id is given
community_id: int | None     # Database ID
include_members: bool = False
repo_root: str | None
max_members: int = 25        # Member entries returned (max 25)
```

#### `get_architecture_overview_tool`
```
repo_root: str | None
detail_level: str = "minimal"    # "minimal" (default) or "standard"
max_results: int = 100           # Cross-community rows and warnings (max 200)
max_members: int = 10            # Member names per community in standard mode (max 25)
```
`cross_community_edges_total` reports the untruncated row count.
Minimal responses may include estimated `context_savings` metadata.

### Graph Health and Architecture Tools

#### `get_hub_nodes_tool`
```
top_n: int = 10                  # Capped at 100
repo_root: str | None
detail_level: str = "standard"   # "minimal" returns name, kind, total_degree
```

#### `get_bridge_nodes_tool`
```
top_n: int = 10                  # Capped at 100
repo_root: str | None
detail_level: str = "standard"   # "minimal" returns name, kind, betweenness
```

#### `get_knowledge_gaps_tool`
```
repo_root: str | None
max_per_category: int = 15       # Entries per gap category (max 50)
detail_level: str = "standard"   # "minimal" drops file paths
```
`summary` maps each category to its untruncated count. The categories are
`isolated_nodes`, `thin_communities`, `untested_hotspots`,
`single_file_communities` and `import_cycles`.

An `import_cycles` row is one group of files that import each other: `size` is
how many files, `name` and `cycle` spell out one loop with its import lines, and
`load_time` says whether that loop can fail as the files load. `false` means every
loop has an import that runs late: inside a function, a dynamic `import()` or a
Ruby `autoload`. Imports that never run are left out: `if TYPE_CHECKING:` in
Python and `import type` in TypeScript, and `import { type X }` unless
`verbatimModuleSyntax` keeps it. Only imports that
name a file are followed, so Go packages and C# namespaces are not. Graphs built
before this release need `code-review-graph build` to tell these imports apart.

#### `get_surprising_connections_tool`
```
top_n: int = 15                  # Capped at 100
repo_root: str | None
detail_level: str = "standard"   # "minimal" returns source, target, kind, score
```

#### `get_suggested_questions_tool`
```
repo_root: str | None
```

### Change Analysis and Refactoring Tools

#### `detect_changes_tool`
```
base: str = "HEAD~1"
changed_files: list[str] | None
include_source: bool = False # Snippets share a 600-line budget
max_depth: int = 2
repo_root: str | None
detail_level: str = "standard"
max_results: int = 25        # Changed functions, test gaps, changed files (max 100)
max_flows: int = 20          # Affected flows embedded (max 200)
```
Main tool for code review. Maps changed files to affected functions, YAML and JSON
paths, flows, communities and test coverage gaps, returning `changed_yaml_paths` and
`changed_json_paths` separately from function risk/test-gap scoring, plus risk scores
and review priorities.
Embedded flows carry per-flow metadata only; use `get_affected_flows_tool` for
step detail. `changed_functions_total`, `test_gaps_total` and `affected_flows_total`
report the untruncated counts. Responses may include estimated `context_savings` metadata.

When the diff only deletes lines, the code around the gap did not change, so it
is not listed as changed. The old version of the file is read instead: a
function or class that lost some lines is listed as changed, and one that is
gone is listed in `removed_symbols` (at most 25; `removed_symbols_total` has the
full count) with up to three of the callers, subclasses, importers or tests that
still use it (`used_by_total` has the full count). A removed symbol that is still
used breaks its users, so it gets a `risk_score` on the same scale as a changed
one (the flows it was part of, callers from other communities, test coverage,
security names, caller count) and counts towards the overall `risk_score`; one
that nothing uses scores 0. The summary names the ones still in use, and minimal
mode reports `removed_symbol_count`. Up to 100 files
are read per call (`CRG_MAX_DELETION_FILES`); the summary says when a file could
not be read. A deleted file is not covered.

#### `refactor_tool`
```
mode: str = "rename"         # "rename", "dead_code", or "suggest"
old_name: str | None         # (rename) Current symbol name
new_name: str | None         # (rename) New name
kind: str | None             # (dead_code) Function or Class
file_pattern: str | None     # (dead_code) File path substring
repo_root: str | None
max_results: int = 50        # Edits/symbols/suggestions returned (max 150)
detail_level: str = "standard"  # "minimal" keeps identifying fields only
```
Truncating a rename preview truncates only the response. The stored preview
keeps every edit, so `apply_refactor_tool` applies the full set.

`old_name` is matched exactly: a name, `Class.method` or a qualified name such as
`pkg/util.py::helper`. When a bare name belongs to several symbols, the first by
file and line is renamed and the preview's `qualified_name` says which; pass a
qualified name to pick another. A name that matches no symbol gives `not_found`.
The preview also edits the import and export lines that name the symbol. Each
edit carries the `column` where the old name starts and its `site` (`definition`,
`call`, `reference`, `import`, `export`, `attribute` or `__all__`), and applying
replaces the name only there. An edit that no longer matches the file is skipped
and counted in `edits_skipped`.

#### `apply_refactor_tool`
```
refactor_id: str             # ID from a prior refactor_tool call
repo_root: str | None
dry_run: bool = False        # Return a diff without writing files
max_diff_files: int = 25     # Per-file diffs in a dry run (max 150)
```

### Wiki Tools

#### `generate_wiki_tool`
```
repo_root: str | None
force: bool = False          # Regenerate all pages even if unchanged
```

#### `get_wiki_page_tool`
```
community_name: str
repo_root: str | None
max_chars: int = 20000       # Page content returned (max 80000)
```
`total_chars` reports the real page length; `truncated` marks a cut.

### Multi-Repo Tools

#### `list_repos_tool`
```
(no parameters)
```

#### `cross_repo_search_tool`
```
query: str
kind: str | None
limit: int = 20              # Results per repo
max_results: int = 50        # Merged results across searched repos (max 100)
repos: list[str] | None      # Aliases or folder names; default: every registered repo
```

`repos` limits the search to named registry entries. Matching is exact and
case-sensitive; paths are not accepted. An alias match wins over a folder-name
match. Results merge by each repo's local rank, with registry order as the
tie-breaker.

Names that match no entry are returned in `unknown`. A name that matches
several entries selects all of them and is listed in `ambiguous`. Both lists
are capped at 20 entries; `unknown_total` / `ambiguous_total` and
`unknown_truncated` / `ambiguous_truncated` report the real counts, and the
summary reports the counts rather than echoing the names.

## Result Bounds

Every tool that returns a list is bounded, so one MCP response cannot fill a
context window (see #849). The contract is the same everywhere:

- Defaults are small. Pass the tool's cap parameter to widen up to its hard
  ceiling, or a smaller value to narrow.
- Truncation is reported: the response carries the untruncated count
  (`total`, or a `*_total` field per list), sets `truncated: true`, and the
  summary says how many of how many are shown.
- Cap parameters reject values below 1 and reject booleans.
- Hard ceilings are enforced in code and pinned by `tests/test_token_budget.py`.

## MCP Prompts (5 workflow templates)

### `review_changes`
Pre-commit review using detect_changes, affected_flows and test gaps.
```
base: str = "HEAD~1"
```

### `architecture_map`
Architecture documentation using communities, flows and Mermaid diagrams.

### `debug_issue`
Guided debugging using search, flow tracing and recent changes.
```
description: str = ""
```

### `onboard_developer`
New developer orientation using stats, architecture and critical flows.

### `pre_merge_check`
PR readiness check with risk scoring, test gaps and dead code detection.
```
base: str = "HEAD~1"
```

## Skills and Slash Commands

The skills `build-graph`, `review-delta` and `review-pr` ship in the repository's `skills/` folder. `install --platform qoder` (or the default `--platform all`, when Qoder is found on this machine) copies them, with the four workflow skills below, to `.qoder/skills/`. Other clients do not get these three.

### `build-graph`
Build or update the knowledge graph.
- First run: full build
- Later runs: incremental update (changed files only)

### `review-delta`
Review the last commit plus uncommitted and new files.
- Changed files come from `git diff` against `HEAD~1`
- Blast radius is changed nodes plus 2-hop neighbours
- Output is a structured review with guidance

### `review-pr`
Review a PR or branch diff.
- Uses `main` (or `master`) as the base
- Covers every commit in the PR
- Output is a structured review with a risk assessment

`install` also writes four workflow skills, `explore-codebase`, `review-changes`,
`debug-issue` and `refactor-safely`, to `.claude/skills/` for Claude Code,
`.gemini/skills/` for Gemini CLI, `.codebuddy/skills/` for CodeBuddy and
`~/.hermes/skills/code-review-graph/` (or `$HERMES_HOME/skills/code-review-graph/`)
for Hermes Agent. Qoder gets all seven skills in `.qoder/skills/`, and Codex gets
one global skill. With no `--platform`, each is written only when that tool is
found on this machine. `--no-skills` skips them all.
