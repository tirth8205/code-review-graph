# Troubleshooting

Each entry gives the symptom, the cause and the fix.

## Quick reference for common install/setup problems

### 1. `Hooks use a matcher + hooks array` error in `.claude/settings.json`

**Cause.** Releases before v2.2.3 wrote an invalid hook schema: flat
`{matcher, command, timeout}` entries, timeouts in milliseconds, and a
`PreCommit` event that Claude Code does not have. v2.2.3 (PR #208) rewrote the
generator.

**Fix.**

```bash
pip install --upgrade code-review-graph
cd /path/to/your/project
code-review-graph install
```

`install` merges its entries into the existing `hooks` block and does not
delete entries it did not write. If the old flat entries are still there,
remove them from `.claude/settings.json` by hand (`install` writes a backup to
`.claude/settings.json.bak` first), then run `install` again.

Comments and trailing commas in the file are fine; they are kept. If the file
cannot be merged (it is not valid JSON, a `/*` comment never closes, or `hooks`
has the wrong shape), `install` leaves it byte for byte as it was and prints
`... left unchanged to avoid data loss. Please add the hooks manually.` Fix the
file and run `install` again. The same holds for `.qoder/settings.json`,
`.codebuddy/settings.json`, `.gemini/settings.json`, `~/.codex/hooks.json` and
`~/.cursor/hooks.json`. `install --dry-run` shows each change as a diff and
writes nothing.

The generated config uses two events: `PostToolUse` (runs
`code-review-graph hook-update` after Edit/Write) and `SessionStart`
(runs `code-review-graph status`). Pre-commit checks live in a git hook, not in
Claude Code settings. `install --steering` adds a third, opt-in event,
`PreToolUse`, that runs `code-review-graph enrich` before each search and read;
`install --no-steering` removes it.

`hook-update` updates the graph of the repository that holds the edited file,
not the one the session started in, so it works from a folder of sibling
repos, an outer repo with nested ones, or a main checkout that edits a linked
worktree. It updates that repository only if it already has a graph, or if it
is the repository the session started in (which is built on first edit, as
before). An edit anywhere else on disk changes no graph. A submodule with no
graph of its own passes the edit to its superproject. Under `CRG_DATA_DIR`
every repository points at the same database, so it counts only as the graph
of the repository it was built for.

#### The git pre-commit hook

`install` writes a `pre-commit` hook to the directory reported by
`git rev-parse --git-path hooks`: usually `.git/hooks`, but `core.hooksPath`
(for example `.husky`) and submodules are covered too. An existing shell hook
is appended to, not overwritten. A hook in another language, such as Python or
Node, is left unchanged with a warning, so call the two commands below from it
yourself. If husky or pre-commit manages your hooks, you can add the two
commands there instead:

```sh
code-review-graph update
code-review-graph detect-changes --brief
```

`install` also appends `post-checkout`, `post-merge` and `post-rewrite` hooks
that run `code-review-graph update --quiet` after a branch checkout, a merge
and a rebase. With a hook manager, add that command to the same three hooks;
run it from `post-checkout` only when its third argument is `1`, and from
`post-rewrite` only when its first argument is `rebase`. Do the same from an
existing hook written in a language other than shell, which `install` leaves
unchanged.

The hook skips linked worktrees, so a commit there cannot silently build a
second graph for another branch. It finds the git dir with
`git rev-parse --absolute-git-dir` (Git 2.13 or newer); a `commondir` file in
that directory marks a linked worktree. It then prints this on stderr and lets
the commit continue:

```
code-review-graph: skipping automatic checks in a linked worktree; set CRG_HOOK_WORKTREES=1 to keep a graph for this worktree too.
```

Set `CRG_HOOK_WORKTREES=1` to run the checks in a worktree. If Git cannot
report the git dir or the worktree root, the hook also skips the checks and the
commit proceeds.

Re-running `install` upgrades the exact hook block written by older releases.
A block you have edited is left alone; update it by hand.

### 2. `code-review-graph: command not found` after `pip install`

**Cause.** `pip` put the console script in a `bin/` directory that is not on
your `PATH`.

**Fix.** Pick one:

1. Install with `pipx`:

   ```bash
   pip uninstall code-review-graph
   pipx install code-review-graph
   ```

   If the command is still not found, run `pipx ensurepath` and open a new
   shell.

2. Run it with `uvx` (no install):

   ```bash
   uvx code-review-graph install
   uvx code-review-graph build
   ```

3. Run it as a module with the interpreter you installed into:

   ```bash
   python -m code_review_graph install
   python -m code_review_graph build
   ```

4. Add the script directory to `PATH`. `pip show code-review-graph | grep Location`
   prints the `site-packages` directory; the scripts are in the sibling `bin/`
   (on macOS user installs, typically `~/Library/Python/3.X/bin`):

   ```bash
   echo 'export PATH="$HOME/Library/Python/3.12/bin:$PATH"' >> ~/.zshrc
   source ~/.zshrc
   ```

### 3. Is code-review-graph project-scoped or user-scoped?

Both. The pieces are scoped differently:

| Piece | Scope | Where |
|---|---|---|
| Python package | User | Installed once with `pip`, `pipx` or `uvx` |
| Graph database | Project | `.code-review-graph/graph.db` in each repository (or `--data-dir` / `CRG_DATA_DIR`) |
| MCP server config | Project or user, by platform | Project files such as `.mcp.json`, `.cursor/mcp.json` or `.vscode/mcp.json`; user files for Codex, Windsurf, Zed, Continue, Antigravity, Qwen Code, GitHub Copilot CLI, Hermes Agent and DeepSeek Harness. Most entries carry `cwd=<project>`; OpenCode and MiMo Code pass `--repo`; Hermes Agent and DeepSeek Harness pin no repo |
| Multi-repo registry | User | `~/.code-review-graph/registry.json` (or under `$CRG_HOME`) |

Install the package once, then run
`code-review-graph install && code-review-graph build` in each project.

### 4. Installed in a virtual environment? Re-run `install` from inside it

**Symptom.** The MCP server does not start, or hooks never update the graph,
after you moved the package into a venv.

**Cause.** `install` records a launcher at install time: `uvx`, `uv run` or
`poetry run` when it detects them, otherwise the absolute path of the running
interpreter with `-m code_review_graph serve`. An entry written outside the
venv points at the wrong interpreter. The Claude Code hooks store no path; they
run `code-review-graph` from `PATH` and exit silently when it is not found, so
they do nothing in a session where the venv is not activated.

**Fix.** Activate the venv and run `install` again:

```bash
source .venv/bin/activate
code-review-graph install
```

The fallback entry looks like this:

```json
{
  "mcpServers": {
    "code-review-graph": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "code_review_graph", "serve"],
      "cwd": "/path/to/your/project"
    }
  }
}
```

Start Claude Code from a shell with the venv activated if you want the hooks to
run. Quit and reopen Claude Code after changing the config.

### 5. "I built the graph but Claude Code doesn't see it in a new session"

Likely causes, most common first:

1. Claude Code was not restarted after `install`. It reads `.mcp.json` at
   startup.
2. The new session's working directory is different. The server runs with
   `cwd=<project>` and reads `.code-review-graph/graph.db` from there. A
   session opened in a parent folder or another project will not find your
   graph.
3. You ran `build` but not `install`. `build` writes `graph.db`; `install`
   registers the MCP server.
4. The server crashes on startup. Run `/mcp` in Claude Code to see the server
   status, and run the command from `.mcp.json` in a terminal to see the error.

Checklist:

```bash
cd /path/to/your/project
code-review-graph status    # prints Nodes, Edges and Files for the graph
ls .mcp.json                # must exist
cat .mcp.json               # must contain a code-review-graph entry ending in "serve"
# then quit Claude Code and reopen it in this directory
```

If `status` finds the graph but `/mcp` does not list `code-review-graph`,
`.mcp.json` is not in the session's working directory. Run
`code-review-graph install` from the project root.

### 6. Zed native agent cannot see MCP in an SSH project

**Symptom.** You ran `install --platform zed` and `build` on a remote host,
then restarted Zed locally, but its native agent does not list code-review-graph.

**Cause.** `install --platform zed` writes `context_servers` into the
user-level settings on the machine running the command:
`~/Library/Application Support/Zed/settings.json` on macOS or
`~/.config/zed/settings.json` on Linux. Settings written on the remote host
are not read by your local Zed instance. In the affected Zed SSH setup, MCP
servers are launched locally by Zed, not on the SSH host. The generated entry
also records the checkout path as `cwd`; a remote path is not a local checkout.
An external agent running remotely can read its own remote configuration,
which explains why it may work while Zed's native agent does not.

**Fix.** Install code-review-graph on the machine running Zed and clone the
project there. In that local checkout, run:

```bash
cd /path/to/local/checkout
code-review-graph install --platform zed
code-review-graph build
```

Restart Zed and open that local project. The server's command and `cwd` must
both be usable locally. This graph describes the local checkout, not remote-only
edits; keep the checkout in sync yourself and run `code-review-graph update`
there after changes.

See [#828](https://github.com/tirth8205/code-review-graph/issues/828) for the
reported setup and workaround. Remote MCP behaviour depends on your Zed version;
track upstream support in
[zed#39213](https://github.com/zed-industries/zed/issues/39213) and
[zed#47671](https://github.com/zed-industries/zed/issues/47671).

---

## Database lock errors

The graph is SQLite in WAL mode. If you see `database is locked`:

- Run one `build`, `update` or `watch` at a time per repository.
- Retry; the lock is usually another process that has just finished.
- If the files are corrupt, stop every process that uses the graph, delete
  `.code-review-graph/graph.db`, `graph.db-wal` and `graph.db-shm`, and run
  `code-review-graph build`.

## `Skipped: Another code-review-graph write is already running for this repository`

`build`, `update` and `postprocess` take a per-repository lock before touching
the graph, so a second invocation does not repeat work the first is already
doing. That is expected when a commit hook, an editor save hook and an agent
tool hook all fire at once: one run writes, the others report the overlap and
exit `75` (`EX_TEMPFAIL`).

- `forget` and `embed` take the same lock and exit `75` the same way, but nothing
  else does their work, so the message says so: run them again, or pass `--wait`.
  `forget --dry-run` writes nothing and takes no lock. It reads the graph
  read-only, so a graph from an older release needs an `update` first.
- `watch` takes the lock for each write and lets go between them, so a running
  watcher does not stop a build. If another writer holds the lock, the watcher
  keeps its changes and tries again a second later, and it can still be stopped
  while it waits. A `build` or `update` that lands in one of those writes exits
  `75` as usual.

- The message names the PID that owns the write, so a live holder is
  distinguishable from a leftover lock file. The lock is
  `<data dir>/write.lock` (by default `.code-review-graph/write.lock`), and the
  kernel releases it when the holder dies, including on `SIGKILL` — a killed
  build never blocks the next one.
- Exit `75` rather than `0` on purpose: an overlap is not success, but it is
  not the failure a `1` would report either, so a hook can tell the two apart
  without matching strings.
- Pass `--wait` to block until the in-flight write finishes and then run
  anyway. Use it for a command you are starting by hand; hooks should keep the
  default, where skipping is free because the running write covers the same
  work.
- Two different repositories never block each other, and a graph stored
  outside the working tree (`CRG_DATA_DIR`, `--data-dir`) keeps its lock with
  it.
- External writers that do not acquire this lock themselves can use
  `flock -n .code-review-graph/write.lock external-writer` to exclude CRG
  builds while they work. Invoke `code-review-graph build`, `update`,
  `postprocess`, `forget` and `embed` directly. Wrapping those commands in
  `flock` makes them contend with the wrapper's lock and exit `75`; adding
  `--wait` deadlocks.
- A filesystem that does not support `flock` is not reported as another writer.
  The run logs a warning and goes ahead without the lock, so overlapping runs
  are not skipped there.

## Large repositories

- The first `build` parses every file. Later `update` runs parse only changed
  files and their dependents.
- Only files git tracks, plus new files it does not ignore, are indexed, so
  anything in `.gitignore` is already skipped.
- Exclude generated code and vendored dependencies in `.code-review-graphignore`:

  ```
  generated/**
  vendor/**
  third_party/
  ```

  A directory name without a slash matches at any depth; a leading slash
  anchors the pattern to the repository root.

## Missing nodes after build

- Check the file's language is supported (see [FEATURES.md](FEATURES.md)) or
  added through `languages.toml` (see [CUSTOM_LANGUAGES.md](CUSTOM_LANGUAGES.md)).
- Check git does not ignore the file, and it is not matched by
  `.code-review-graphignore` or the nested build-output detection described
  below.
- Files in a symlinked folder are skipped unless you pass `--follow-symlinks`
  to `build`, `update` or `watch`, or set `CRG_FOLLOW_SYMLINKS=1`.
- Some data files keep only their file node: YAML and JSON translation files,
  JSON test files, JSON under folders such as `docs`, `examples`, `samples`,
  `demo` or `data`, JSON files over 256 KB, and HTML pages over 2 MB.
- Look for a parse warning (next entry) after `update`.
- After upgrading to 2.4.0, run `code-review-graph build` once. `update` parses
  only changed files, so files of the new types (HTML, XSLT, Liquid, JSON,
  plain YAML, `.cjs` and `.kts`) that have not changed since reach the graph
  only on a full build.
- Run `code-review-graph build`, or the MCP tool `build_or_update_graph_tool`
  with `full_rebuild=True`, to re-parse everything.

## `Warning: N file(s) failed to parse and were not updated`

`update` prints this on stderr when a file could not be parsed. The file keeps
the rows from its last successful parse, and the rest of the update is still
recorded as current, so one bad file does not hold the graph back. `build`
prints `Errors: N` for the same case. Fix or ignore the file, then run `update`
again.

## Empty or incomplete graph (poisoned `graph.db`)

**Symptom.** `status` shows far fewer files than the repository has, or zero
nodes.

**Cause.** Older releases created an empty `graph.db` when `status`,
`detect-changes`, `visualize`, `wiki` or `watch` ran before the first `build`.
`update` then re-parsed only changed files, so the graph stayed incomplete.

Current behaviour:

- `status`, `detect-changes`, `visualize`, `wiki`, `watch`, `forget` and
  `dead-code` do not create a database. Without one they exit with:

  ```
  No graph found at <path>. Run `code-review-graph build` first.
  ```

- `update` on a missing or zero-node graph runs a full build and prints
  `Full rebuild (no usable incremental base): ...`.

**Fix.** Run `code-review-graph build`. It always re-parses the whole tree;
there is no `--force` flag. After that, `update`, hooks and `watch` stay
incremental.

## Unusable `graph.db` (corrupt, foreign, or from a newer release)

Every command that opens the graph reports an unusable database in one line
and exits 1, instead of raising a SQLite traceback:

```
Error: the graph database at <path> is unreadable (file is not a database). Run `code-review-graph build` to rebuild it from scratch.
Error: <path> is a SQLite database but not a code-review-graph graph (it holds invoices). Point --data-dir somewhere else, or delete the file and run `code-review-graph build`.
Error: the graph database at <path> was written by a newer code-review-graph (schema v99; this build understands v15). Upgrade code-review-graph, or delete the file and run `code-review-graph build`.
Error: the graph at <path> was built for a different repository root: none of its 12 file(s), such as /other/repo/lib.py, are under /this/repo. Run `code-review-graph build` here, or point --repo at the root it was built for.
```

The last one is the case that used to be silent: a `graph.db` copied between
checkouts, or a CI cache restored into the wrong repository, answered every
question with the other repository's symbols and paths.

**Fix.** Run `code-review-graph build`. For the first case (an unreadable
file, including a valid SQLite file whose tables are the wrong shape) `build`
discards the unusable database and its `-wal`/`-shm` sidecars itself, and
logs one warning saying so; this is what lets the GitHub Action recover from
a restored cache without anyone clearing it by hand. The others are never
discarded for you, because deleting somebody else's SQLite file, or a graph
this build is merely too old to read, is not a recovery: delete the file
yourself, or point `--repo` / `--data-dir` at the pair that belongs together.

A database another process is writing is not in this list at all. Contention
is reported as contention (see *Database lock errors* above) and never
discarded: the graph is healthy and the answer is to try again.

The VS Code extension makes its own check. Extension 0.3.0 and older show
`Database was created with a newer version (schema v15). Update the extension.`
once the CLI has moved the graph to schema v15. Install extension 0.4.0 or
later.

## Unwritable data directory

```
Error: cannot open the graph database for writing at <path> (attempt to write a readonly database). Check the permissions on <dir>, or set CRG_DATA_DIR to a writable directory.
```

**Fix.** Make `.code-review-graph/` writable by the user running the command,
or set `CRG_DATA_DIR` to a directory that is.

## `detect-changes` cannot read the diff

`detect-changes` never reports a clean tree it could not look at. When git is
missing or too slow, it says so and exits 1:

```
Error: could not determine the changes: git could not be run ([Errno 2] No such file or directory: 'git'). Install git and make sure it is on PATH.
Error: could not determine the changes: git timed out after 30s. Raise CRG_GIT_TIMEOUT, or re-run when the repository is not busy.
```

`No changes detected.` with exit 0 means exactly one thing: the diff was read
and it was empty. A CI review gate can rely on that distinction. `build` and
`update` are unaffected — they re-parse the working tree and reconcile by
content hash, so they still succeed without git.

If only the line-level diff is unreadable while the changed-file list is not,
the analysis degrades to whole-file scoring and says so rather than failing:

```
  - Warning: line-level diff unavailable, whole files scored (...)
```

## Invalid numeric environment variable

A `CRG_*` setting that is not a number no longer aborts the process. The
documented default is used and the variable is named once:

```
WARNING: Ignoring invalid CRG_MAX_IMPACT_NODES='' (not a number); using the default 500.
```

## Legacy `.code-review-graph.db` at the repository root

Very old releases stored the database as `.code-review-graph.db` in the
repository root. Every command that opens the graph at its default location,
including `forget` and `dead-code`, moves it to `.code-review-graph/graph.db`
on first use. Nothing else is needed.

## Graph seems stale

- With hooks installed, `hook-update` updates the edited file's repository
  after each Edit/Write (see above for which repositories it touches), the
  pre-commit hook runs `update` before each commit, and the post-checkout,
  post-merge and post-rewrite hooks run it after a branch switch, a merge and
  a rebase (not in linked worktrees; see above).
- Run `code-review-graph update`, or ask the assistant to call
  `build_or_update_graph_tool`, to catch up.
- Check `.claude/settings.json` still has the hooks; `code-review-graph install`
  rewrites them.

## Watcher is running but the graph stopped updating

`crg-daemon status` has a `Watcher` column next to the process `Status`, plus
the age of the last event each watcher processed:

```
  Alias     Status    Watcher   PID       Event   Path
  backend   alive     stalled   48213     3d      /work/backend
```

- `ok`: the filesystem observer is running and publishing a heartbeat.
- `stalled`: the process is up but its observer threads are not, so nothing is
  indexed. Check `crg-daemon logs --repo ALIAS`, then `crg-daemon restart`.
- `partial`: the watcher has reduced coverage. Its health file's `failed_paths`
  lists directories the OS refused to watch. If that list is empty, the watcher
  ran out of watch slots and fell back to one recursive watch: still complete,
  but ignored trees are watched again. Raise `CRG_MAX_WATCH_SCHEDULES` to get
  the filtering back.
- `unknown`: the watcher has not published health yet (it just started, or it
  predates this feature).
- `dead`: the process exited. The daemon restarts it with exponential backoff,
  so a repository that cannot start does not repeat a full initial build every
  30 seconds.

A watcher whose observer dies logs an error and exits non-zero so the daemon
restarts it. Deleting a watched directory, or deleting and recreating one, is
ordinary work, not a dead watcher.

Watch mode registers OS watches only for directories that survive the ignore
patterns, so `node_modules/`, `.git/` and build output generate no events.
Optional settings:

- `CRG_MAX_WATCH_SCHEDULES` (default 24): cap on separate watches; a repository
  needing more falls back to one recursive watch on the root.
- `CRG_WATCH_PLAN_DEPTH` (default 3): how deep the planner may split.
- `CRG_WATCH_SPLIT_MIN_DIRS` (default 4): smallest ignored tree worth its own
  watch.
- `CRG_RESTART_BACKOFF` (default 30s), `CRG_RESTART_BACKOFF_MAX` (default 900s),
  `CRG_RESTART_HEALTHY_AFTER` (default 600s): daemon restart backoff.

## `Identity migration pending for ignored file`

Older builds recorded a C++ file that failed to parse as pending an identity
migration. If you then added the file to `.code-review-graphignore`, every
`update` reported this error and `watch` refused to start until a full rebuild.
Current releases drop the pending entry when the file has no rows in the graph.
If you still see the message, upgrade, or run `code-review-graph build` once.

## A directory disappeared from the graph

Nested `target/`, `build/`, `.next/` and `.nuxt/` directories are treated as
build output when a sibling manifest says so (`pom.xml`, `Cargo.toml`,
`build.sbt`, `build.gradle`, `build.gradle.kts`, `settings.gradle`,
`settings.gradle.kts`, `next.config.*`, `nuxt.config.*`). A nested `vendor/`
is treated as dependencies when a sibling `composer.json`, `go.mod`, `go.work`,
`Gemfile`, `Rakefile`, `Cargo.toml` or `*.gemspec` says so. With one of those
files at the repository root, every `vendor/` folder is skipped; otherwise a
folder named `vendor` stays in the graph unless a sibling file marks it. Add
`vendor/` to `.code-review-graphignore` to leave it out anyway. Each build logs
what it excluded:

```
Excluding 2 nested build-output directories (a sibling manifest marks them as
build output; keep one with '!<path>' in .code-review-graphignore):
moduleA/target, moduleB/target
```

If one of those is source, keep it with a `!` line in `.code-review-graphignore`:

```
!moduleA/target
```

`!` lines only opt a path out of this automatic detection; they do not negate
your explicit ignore patterns. `CRG_NESTED_OUTPUT_SCAN=0` turns the detection
off for the whole repository.

## Embeddings not working

- Install the local provider: `pip install "code-review-graph[embeddings]"`.
- Run `code-review-graph embed`, or the `embed_graph_tool` MCP tool.
- The first local run downloads the `all-MiniLM-L6-v2` model.
- `--embedding-provider` on `build`, `update`, `postprocess` and `watch` only
  refreshes vectors that already exist. On a graph with none yet it warns
  `Embedding refresh skipped: this graph has no embeddings yet.` and embeds
  nothing; run `code-review-graph embed` once with the same provider and model.
- Cloud providers (`--provider openai|google|minimax|voyage`) read their key
  from `CRG_OPENAI_API_KEY`, `GOOGLE_API_KEY`, `MINIMAX_API_KEY` or
  `VOYAGE_API_KEY`. The OpenAI-compatible provider also needs
  `CRG_OPENAI_BASE_URL` and `CRG_OPENAI_MODEL` (or `--model`), and is used by
  itself when the key and base URL are set and no provider is named. Each
  prints an egress warning until `CRG_ACCEPT_CLOUD_EMBEDDINGS=1` is set; a
  localhost base URL gives none.

## MCP server won't start

- Run the command from your MCP config by hand, for example
  `uvx code-review-graph serve`, and read the error.
- `install` writes one of `uvx code-review-graph serve`,
  `uv run code-review-graph serve`, `poetry run code-review-graph serve` or
  `<python> -m code_review_graph serve`, depending on what it detects. If the
  launcher it chose is missing, install it (`pip install uv` or `brew install uv`)
  or re-run `code-review-graph install` from the environment you want to use.
- You can edit the entry yourself, for example
  `uv run --project /path/to/checkout code-review-graph serve` when the server
  lives outside the project you are editing. A later `install` leaves a
  hand-edited entry exactly as you wrote it and says so; it only replaces
  entries whose command line it recognises as one it wrote itself. The same
  holds for hooks: a hook command you wrote is never rewritten or removed.

## Windows / WSL

- Upgrade to v2.3.6 or later if `daemon status` crashes with WinError 87 (#511)
  or CLI `detect-changes` maps 0 functions on Windows (#528).
- Use forward slashes in paths when passing `repo_root` to MCP tools.
- In WSL, install `uv` inside WSL, not the Windows build:
  `curl -LsSf https://astral.sh/uv/install.sh | sh`. If `uv` is not found
  afterwards, add the directory the installer reports to your `PATH`.
- File watching (`code-review-graph watch`) may lag on WSL1; use WSL2.
- On native Windows, long paths may need enabling:
  `git config --system core.longpaths true`.

## Community detection requires igraph

- Install with `pip install "code-review-graph[communities]"`.
- Without igraph, community detection falls back to file-based grouping, which
  is coarser.

## Optional dependency groups

If a tool returns an ImportError, install the relevant group:

- `pip install "code-review-graph[embeddings]"`: local semantic search
  (sentence-transformers).
- `pip install "code-review-graph[google-embeddings]"`: Google Gemini embeddings.
  OpenAI-compatible, MiniMax and Voyage AI embeddings use the standard library
  HTTP client and need only their environment variables.
- `pip install "code-review-graph[communities]"`: igraph-based community
  detection.
- `pip install "code-review-graph[enrichment]"`: Python call resolution
  through Jedi. It is off until you ask for it with `build --enrich`,
  `update --enrich` or `CRG_ENABLE_ENRICHMENT=1`, because it adds about a
  second per Python file.
- `pip install "code-review-graph[eval]"`: evaluation benchmarks and
  `visualize --format svg` (matplotlib, PyYAML).
- `pip install "code-review-graph[wiki]"`: installs the `ollama` client. The
  current wiki generator is structural only and does not call it.
- `pip install "code-review-graph[all]"`: everything above.
