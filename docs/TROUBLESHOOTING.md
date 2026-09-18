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

The generated config uses two events: `PostToolUse` (runs
`code-review-graph update --skip-flows` after Edit/Write) and `SessionStart`
(runs `code-review-graph status`). Pre-commit checks live in a git hook, not in
Claude Code settings.

#### The git pre-commit hook

`install` writes a `pre-commit` hook to the directory reported by
`git rev-parse --git-path hooks`: usually `.git/hooks`, but `core.hooksPath`
(for example `.husky`) and submodules are covered too. An existing hook is
appended to, not overwritten. If husky or pre-commit manages your hooks, you
can add the two commands there instead:

```sh
code-review-graph update
code-review-graph detect-changes --brief
```

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
| MCP server config (`.mcp.json` and platform equivalents) | Project | One server per project; the entry carries `cwd=<project>` |
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

---

## Database lock errors

The graph is SQLite in WAL mode. If you see `database is locked`:

- Run one `build`, `update` or `watch` at a time per repository.
- Retry; the lock is usually another process that has just finished.
- If the files are corrupt, stop every process that uses the graph, delete
  `.code-review-graph/graph.db`, `graph.db-wal` and `graph.db-shm`, and run
  `code-review-graph build`.

## Large repositories

- The first `build` parses every file. Later `update` runs parse only changed
  files and their dependents.
- Only files tracked by git are indexed, so anything in `.gitignore` is already
  skipped.
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
- Check the file is tracked by git and not matched by `.code-review-graphignore`
  or the nested build-output detection described below.
- Look for a parse warning (next entry) after `update`.
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
Error: the graph database at <path> was written by a newer code-review-graph (schema v99; this build understands v10). Upgrade code-review-graph, or delete the file and run `code-review-graph build`.
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

- With hooks installed, `update --skip-flows` runs after each Edit/Write and
  the pre-commit hook runs `update` before each commit (not in linked
  worktrees; see above).
- Run `code-review-graph update`, or `/code-review-graph:build-graph` in Claude
  Code, to catch up.
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
- `partial`: the watcher ran out of watch slots and fell back to one recursive
  watch. Still complete, but ignored trees are watched again. Raise
  `CRG_MAX_WATCH_SCHEDULES` to get the filtering back.
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
`build.sbt`, `build.gradle`, `build.gradle.kts`, `next.config.*`,
`nuxt.config.*`). Each build logs what it excluded:

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
- Cloud providers (`--provider openai|google|minimax|voyage`) read their key
  from `CRG_OPENAI_API_KEY`, `GOOGLE_API_KEY`, `MINIMAX_API_KEY` or
  `VOYAGE_API_KEY`, and print an egress warning until
  `CRG_ACCEPT_CLOUD_EMBEDDINGS=1` is set.

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
- `pip install "code-review-graph[enrichment]"`: Python call-resolution
  enrichment through Jedi.
- `pip install "code-review-graph[eval]"`: evaluation benchmarks (matplotlib,
  PyYAML).
- `pip install "code-review-graph[wiki]"`: installs the `ollama` client. The
  current wiki generator is structural only and does not call it.
- `pip install "code-review-graph[all]"`: everything above.
