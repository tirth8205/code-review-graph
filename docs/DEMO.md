# Ten-minute live demo

A script for demoing code-review-graph on stage. Every command below was run
end to end on a machine that had never seen the tool, against a fresh clone of
[django](https://github.com/django/django) at `8cbdd4a`. Every number is
measured, not estimated, and the machine is named next to it.

**Measurement machine.** Apple M-series laptop, macOS 27, SSD, Python 3.14.5
(Homebrew), warm home network. django at depth-60 clone: 3,005 parsed files.
Your laptop will not match these to the second. What should hold is the shape:
build in about a minute, every query after that under a second.

**Rehearse on the machine and network you will present from, the morning of.**
Every fallback below exists because one of these steps went wrong at least once
while this script was being written.

---

## Before you walk on stage

Do all of this the night before, and leave the terminal open.

```bash
# 1. The clone the demo runs against.
git clone --depth 60 https://github.com/django/django.git ~/demo/django

# 2. A throwaway environment.
python3 -m venv ~/demo/venv
~/demo/venv/bin/python -m pip install code-review-graph

# 3. Build the graph once, so the live build is a re-run, not a first run.
cd ~/demo/django
~/demo/venv/bin/code-review-graph install --platform claude-code -y
~/demo/venv/bin/code-review-graph build
~/demo/venv/bin/code-review-graph status
```

Then **copy the whole `~/demo` directory to a second location** and keep it
untouched. If anything goes wrong live, `cd` into the copy instead of debugging.

Check the two things that need the network and confirm they still work:

```bash
curl -sI https://pypi.org/simple/code-review-graph/ | head -1   # for pip
curl -sI https://d3js.org/d3.v7.min.js | head -1                # not required, see step 5
```

---

## The ten minutes

### Step 1 — Install, from nothing (60 s)

```bash
python3 -m venv venv
./venv/bin/python -m pip install code-review-graph
./venv/bin/code-review-graph --version
```

**What it shows.** No compiler, no service, no API key, no daemon. One pip
install and a CLI.

**Expected output.** `code-review-graph 2.3.8`.

**Measured.** 15.5 s with a warm pip cache on Python 3.14.5; 14.8 s with
`--no-cache-dir` on Python 3.12, i.e. downloading every wheel fresh over a home
connection. Most of it is the Tree-sitter grammar wheels, so this number is
really a measurement of the venue's uplink, not of the tool.

**Fallback.** Skip this step. Say "I installed it before the talk, it takes
about fifteen seconds" and start at step 2 in the pre-built directory. Never
run pip live on conference wifi if you can avoid it.

---

### Step 2 — Wire it into the agent (10 s)

```bash
cd django
../venv/bin/code-review-graph install --platform claude-code -y
```

**What it shows.** It writes `.mcp.json`, generates skills, installs an editor
hook and a git pre-commit hook, and appends graph instructions to `CLAUDE.md`.
Nothing is uploaded; the config is local files you can read on screen.

**Expected output.** Six lines ending in `Next steps: 1. code-review-graph
build`.

**Measured.** 0.42 s.

**Fallback.** The flag is `--platform`, not `--client`; `--client` exits with a
usage error. `--platform all` covers every detected editor. If the CLAUDE.md
injection prompt appears because you forgot `-y`, answer it and move on.

---

### Step 3 — Build the graph (80 s)

```bash
../venv/bin/code-review-graph build
../venv/bin/code-review-graph status
```

**What it shows.** Tree-sitter parses the whole repository once. Talk over it:
this is the only slow step, it happens once, and after this every answer is a
SQLite query.

**Expected output.**

```text
INFO: Progress: 3000/3005 files parsed
Full build: 3005 files, 46970 nodes, 401029 edges (postprocess=full)
```

and then

```text
Nodes: 45111
Edges: 397762
Files: 2979
Languages: bash, javascript, python
```

**Measured.** Build 78 s. `status` 1.6 s on its first run straight after the
build, 0.5 s warm. The graph database is **1.18 GB** — be ready for that
question; see "What it does not do" below.

**Fallback.** This is the step most likely to make you sweat, because 80
seconds of scrolling logs is a long silence. Two options, in order:

1. Use the pre-built copy and run only `status`, which is instant and shows the
   same node and edge counts.
2. Demo on this repository instead of django: 326 files, **6.2 s** build,
   6,526 nodes, 57,057 edges. Smaller numbers, but the build finishes while you
   are still talking.

---

### Step 4 — Ask the graph a question (90 s)

```bash
../venv/bin/code-review-graph search "password hashing"
../venv/bin/code-review-graph query callers_of \
  'django/contrib/auth/hashers.py::make_password'
../venv/bin/code-review-graph impact --files django/db/models/query.py
```

**What it shows.** Natural-language search lands on `make_password`,
`render_password_as_hash`, `verify_password`, `check_password`. `callers_of`
answers in one hop what a grep for `make_password` would answer in fifty hits.
`impact` names the blast radius of a single-file change.

**Expected output.** `search` returns 5 results with `"search_mode": "fts"`.
`impact` returns a JSON object whose `summary` starts `Blast radius for 1
changed file(s)`.

**Measured**, wall clock including interpreter start: `search` 0.13 s,
`callers_of` 0.12 s, `impact` 0.54 s. In-process, over the MCP transport where
the interpreter is already running, the same queries are 0.01–0.4 s.

**Fallback.** If a query returns nothing, it is almost always the query, not
the tool: this is BM25 over identifiers, not an embedding model (see below).
Fall back to an identifier-shaped query — `make_password`, `QuerySet`,
`csrf_token` — which cannot miss. Do not improvise a query on stage that you
have not run before.

---

### Step 5 — Show the graph (60 s)

```bash
../venv/bin/code-review-graph visualize
open .code-review-graph/graph.html
```

**What it shows.** A D3 force-directed view of the community structure. Good
for one slide's worth of "this is what it built", not for analysis.

**Measured.** 18 s to generate on django. **The generated page is 95 MB** and
will make a browser work hard; on this repository it is 15.8 MB and opens
instantly.

**Fallback.** Generate it beforehand and have the tab already open. If the tab
is blank, the cause used to be the vendored D3 file being blocked when the page
is opened from a `file://` URL; that is fixed in this version, but if you are
demoing an older release use `code-review-graph visualize --serve` (which
serves over `http://localhost:8765` and was never affected). Better still:
**screenshot it in advance** and show the screenshot. A force layout settling
live is not worth the risk.

---

### Step 6 — The actual point: an agent reviewing a diff (3 min)

Make a change, then let the agent use the graph.

```bash
git checkout -b demo
# edit something real, e.g. add a helper to django/contrib/auth/hashers.py
git commit -am "demo change"
../venv/bin/code-review-graph detect-changes --base HEAD~1 | head -40
```

Then, in Claude Code with the MCP server connected, ask:

> Review this change. Use the graph first.

**What it shows.** The agent calls `get_minimal_context`, then
`detect_changes`, then `get_impact_radius` — five calls, all of them cheap —
instead of reading the changed files and everything that imports them.

**Measured over the real MCP stdio transport, against the django graph:**

| Call | Time | Estimated tokens |
|---|---:|---:|
| `get_minimal_context_tool` | 0.25 s | 150 |
| `semantic_search_nodes_tool` | 0.01 s | 443 |
| `get_impact_radius_tool` (`detail_level="minimal"`) | 0.39 s | 191 |
| `query_graph_tool` (`callers_of`, minimal) | 0.01 s | 463 |
| `detect_changes_tool` (minimal) | 0.16 s | 232 |
| **Five-call review workflow** | **0.8 s** | **≈1,479** |

Server handshake: 0.45 s, 30 tools and 5 prompts registered.

**Be precise about this number.** `CLAUDE.md` in this repository sets a target
of "≤5 tool calls, ≤800 total tokens". The measured five-call workflow above is
**1,479 estimated tokens** — under two thousand, not under eight hundred. Quote
the measurement, not the target.

**Do not leave `detail_level` on its default for this.** At
`detail_level="standard"`, `get_impact_radius_tool` alone returns 21,426
estimated tokens for one changed django file. That is the honest default cost,
and it is why the recommended workflow says minimal first.

**Fallback.** If the agent will not connect to the MCP server, run the same
calls from the CLI (`detect-changes`, `impact`, `query`) and narrate what the
agent would have done. The JSON is the same payload; only the transport differs.

---

### Step 7 — In CI (60 s)

Show a real PR comment rather than triggering one live — open any recent
merged PR on <https://github.com/tirth8205/code-review-graph/pulls> and scroll
to the sticky comment from `github-actions[bot]`. Every PR to `main`,
`testing` or `staging` gets one.

**What it shows.** The same analysis as a sticky PR comment: risk-scored
symbols, test gaps, and a token-savings line. The analysis job is
unprivileged and a separate `workflow_run` job posts the comment, so PR code
never runs with write permissions.

**Measured** by running the Action's own commands locally against the django
graph, not on a runner: incremental `update` 7.7 s, `detect-changes` 0.3 s,
render 0.1 s. The runner adds a `pip install` and a graph cache restore on top
of that.

**Fallback.** To render the comment locally without CI:

```bash
../venv/bin/code-review-graph detect-changes --base HEAD~1 > /tmp/r.json
python scripts/render_pr_comment.py --input /tmp/r.json --output /tmp/c.md
cat /tmp/c.md
```

Run it from the repository root — the renderer strips the working-directory
prefix so the output matches what CI produces.

---

## The honest headline numbers

Everything here was measured on the machine described at the top. Nothing is
extrapolated.

| Claim | Measured |
|---|---|
| Build a 3,005-file repository | 78 s, once |
| Incremental update after a one-file edit | 7.7 s |
| `status` | 0.5 s warm, 1.6 s cold |
| Any graph query after the build | 0.12–0.54 s from the CLI, 0.01–0.4 s in-process |
| Five-call agent review workflow, minimal detail | ≈1,479 estimated tokens, 0.8 s |
| `get_impact_radius`, standard detail, one changed django file | 21,426 estimated tokens (was 410,834 before this version) |
| Graph database for django | 1.18 GB |
| Generated `graph.html` for django | 95 MB |

Token counts use the project's `chars / 4` estimate, which
[REPRODUCING.md](REPRODUCING.md) calibrates to within +0.5% of `cl100k_base` in
aggregate and ±12% per repository. Say "estimated" out loud; someone will ask.

---

## What it does not do

Have this ready, because it is the first question from anyone who has built
something similar. code-review-graph is a **structural index, not a semantic
one**. It parses syntax with Tree-sitter and records which symbols exist and
which names reference which, so it answers "what calls this" and "what might
break" well. It does not understand what the code means. Search is BM25 over
identifiers and signatures, so a question phrased in prose that shares no words
with the code — "where do we rate-limit abusive clients" against a codebase
that calls it `throttle` — returns nothing useful unless you install the
optional embeddings extra and run `embed`. Impact analysis is deliberately
conservative: it reports what *could* be affected through a reference edge, not
what *is*, so on a hub file it over-reports, and dynamic dispatch, reflection,
string-keyed dispatch tables and runtime monkey-patching are invisible to it
entirely. Flow detection is reliable for Python and PHP/Laravel entry points
and weak for JavaScript and Go ([FAQ.md](FAQ.md) puts overall flow recall at
33%; that figure was not re-measured for this script). It is not a linter, a type checker or a bug finder, and it will not tell an agent
that a change is wrong — it tells the agent where to look, which is a smaller
claim and the one it can actually keep. On a repository under a few hundred
files, or for a one-line change, reading the files directly is cheaper and you
should say so.

---

## Known rough edges, if someone finds one

- The graph database is large: 1.18 GB for django, dominated by edge indexes
  that store fully-qualified names. It is gitignored and rebuildable, but it is
  not small.
- `visualize` on a large repository produces a very large HTML file (95 MB for
  django). Prefer `--serve`, a smaller repository, or a screenshot.
- Search is lexical unless the optional embeddings extra is installed. Prose
  queries whose words do not appear in the code will miss.
- The GitHub Action installs code-review-graph from PyPI, so it analyses with
  the published release, not with the branch under review.
- File-level results still show absolute paths in a few tools (for example the
  `name` field of File nodes in search results). Impact analysis was fixed in
  this version; the others have not been swept yet.
