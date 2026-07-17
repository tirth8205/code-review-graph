# FAQ — how code-review-graph compares

Honest answers to the questions we get most often. Where another tool is genuinely
better for a job, this page says so.

- [How is this different from LSP and language servers?](#how-is-this-different-from-lsp-and-language-servers)
- [Isn't this just RAG?](#isnt-this-just-rag)
- [Why not just grep?](#why-not-just-grep)
- [How does it compare to Serena, codegraph, claude-context, and repomix?](#how-does-it-compare-to-serena-codegraph-claude-context-and-repomix)
- [When should I not use it?](#when-should-i-not-use-it)
- [Does it phone home?](#does-it-phone-home)
- [How do I verify it is working?](#how-do-i-verify-it-is-working)
- [How big a codebase justifies it?](#how-big-a-codebase-justifies-it)
- [How does it handle monorepos, git worktrees, and multiple repos?](#how-does-it-handle-monorepos-git-worktrees-and-multiple-repos)

---

## How is this different from LSP and language servers?

Language servers and code-review-graph (CRG) both build a structural model of your
code, but they optimize for different things.

**What LSP does better.** A language server is backed by a real compiler frontend (or
something close to it), so it gives you type-aware, semantically precise results:
exact go-to-definition through generics and overloads, find-references that
understands scoping, live diagnostics, completions, and renames that are safe by
construction. If you need a *provably complete* reference list for one symbol in one
language, an LSP server is the gold standard and CRG does not try to replace it.

**What CRG does differently:**

- **One persistent graph instead of per-language daemons.** Language servers run one
  process per language and (with a few exceptions that cache an index on disk) rebuild
  or revalidate state per session. CRG parses once with Tree-sitter, stores nodes and
  edges in a single SQLite file (`.code-review-graph/graph.db`), and answers queries
  across roughly 35 languages plus notebooks from one process — including cross-language
  edges that no single LSP server models.
- **It survives sessions and commits.** The graph is updated incrementally (changed
  files only, under 2 seconds on a ~2,900-file repo) rather than rebuilt per editor
  session.
- **Review-oriented edges.** `tests_for`, execution flows, community membership,
  risk-scored change analysis — relationships LSP does not model because they are not
  needed for editing.

**The honest trade-off:** CRG's call resolution is AST-level and heuristic, not
compiler-backed. Dynamic dispatch, metaprogramming, and duck typing can produce
inferred or ambiguous edges — which is exactly why every edge carries a confidence
tier (`EXTRACTED` / `INFERRED` / `AMBIGUOUS`). LSP is more precise per symbol; CRG is
broader, persistent, and cheaper to query across the whole repo.

## Isn't this just RAG?

No. RAG splits your code into text chunks, embeds them, and retrieves chunks by
similarity to the query. That answers "find code that *talks about* X." It cannot
answer "who *calls* X" — similarity between two functions tells you nothing about
whether one invokes the other.

CRG stores **structural edges parsed from the AST**: calls, imports, inheritance,
test coverage. "Who calls `login()`" is a graph lookup, not a similarity guess.
Embeddings exist in CRG but they are optional and play a supporting role — one input
to hybrid search (FTS5 BM25 keyword + vector) used to find a *starting node*, after
which traversal follows real edges. Currently only function signatures are embedded
(~10 tokens per node), not bodies.

The benchmark that captures the difference is multi-hop retrieval: natural-language
query → anchor node → one-hop traversal (`callers_of`, `tests_for`, ...). CRG scores
0.909 across 11 hand-curated tasks on 6 real repos (see
[REPRODUCING.md](REPRODUCING.md)). Pure similarity retrieval has no equivalent of the
second hop.

**Where RAG-style search is better:** purely conceptual questions ("where is rate
limiting discussed?") over prose, comments, and docs. CRG's own keyword search
ranking is a documented weakness (MRR 0.35 — see the limitations section in the
[README](../README.md#benchmarks)).

## Why not just grep?

Fair question — Anthropic has been explicit that Claude Code deliberately ships
*without* a code index. Agentic search (glob, grep, targeted file reads) is always
exactly as fresh as your working tree, has no chunking or staleness failure modes,
and needs zero setup. For one-hop questions — "where is `parse_file` defined?" —
that approach works well, and CRG will not beat it by much.

The gap appears on **multi-hop structural questions**, where each hop costs the agent
another round of grep + read + reasoning, and token spend compounds:

- **Impact radius** — "what could break if I change this file?" requires callers,
  dependents, *and* their tests. One `get_impact_radius` call returns all three.
- **Callers of callers** — transitive tracing via `traverse_graph` or repeated
  `query_graph(pattern="callers_of")`, instead of N rounds of grepping for each
  intermediate name (and grep matches *text*, so overloaded or re-exported names
  produce false hits the agent must read to rule out).
- **Tests for** — `query_graph(pattern="tests_for")` maps code to covering tests via
  parsed edges plus naming conventions, and `detect_changes` adds transitive test
  coverage. Grep only finds tests that mention the name literally.
- **Affected flows** — "which execution paths does this change touch?" has no grep
  equivalent at all.

The graph also persists: agentic search re-derives the same structure from scratch
every session, while CRG keeps it in SQLite and updates incrementally.

One honest caveat on the numbers: the whole-corpus token-reduction numbers (~82x median,
38x–528x range) compare graph responses against reading the **whole corpus**, not
against a skilled agentic-grep session (see [REPRODUCING.md](REPRODUCING.md) for what
each benchmark measures). For single-hop lookups in a small repo, grep is cheap and
good. The multi-hop review workflow is where the graph earns its keep.

## How does it compare to Serena, codegraph, claude-context, and repomix?

These are good tools solving adjacent problems. Short factual comparison, based on
each project's public documentation (check upstream docs for current behavior):

| Tool | Approach | Persistence | External deps | Review focus |
|---|---|---|---|---|
| **code-review-graph** | Tree-sitter AST → structural graph (calls, imports, inheritance, tests) over MCP + CLI | SQLite in `.code-review-graph/`, incremental updates | None for the core; embeddings optional | Yes — blast radius, risk-scored change analysis, test-gap detection |
| **Serena** | LSP-backed symbol retrieval and editing tools over MCP | Language-server state plus per-project memories | A language server per language | General coding-agent toolkit, not review-specific |
| **codegraph** | AST/call-graph indexing over MCP (several projects share this name; details vary by implementation) | Varies by implementation | Varies by implementation | Generally retrieval-focused |
| **claude-context** | Chunk + embed semantic code search over MCP | Vector index in a vector database | Embedding provider + vector DB (cloud or self-hosted) | Search-focused, not review-specific |
| **repomix** | Packs the whole repo into one AI-friendly file | None — regenerated per run | Node.js | One-shot context packing; no structural queries |

Rough guidance: if you want symbol-precise *editing* tools, Serena's LSP approach is
a better fit. If you want semantic *search* and are happy running a vector store,
claude-context covers that. If your repo is small enough to paste wholesale into a
large context window, repomix is the simplest thing that works. CRG's niche is the
persistent structural graph for **review**: impact analysis, risk scoring, and
test-coverage tracing with no external services.

## When should I not use it?

Consistent with the limitations section in the [README](../README.md#benchmarks):

- **Repos under a few hundred files.** An agent can often just read everything
  relevant directly; the graph's structural metadata adds overhead that a small repo
  doesn't repay. See [How big a codebase justifies it?](#how-big-a-codebase-justifies-it)
- **Trivial single-file changes.** The graph response carries impact-radius edges and
  source snippets, which can exceed the raw content of a one-file diff. This is
  measured and documented (the formal `token_efficiency` benchmark reports ratios
  below 1.0 for small commits — by design, see [REPRODUCING.md](REPRODUCING.md)).
- **One-off questions on a repo you won't revisit.** The build is fast (~10 seconds
  for a 500-file project) but the payoff comes from *reuse* across queries and
  sessions. For a single question, agentic search is fine.
- **Flow detection on JS/Go.** Entry-point detection is currently reliable mainly for
  Python framework patterns; JavaScript and Go flow detection needs work (33% recall,
  documented in the README limitations).

## Does it phone home?

No. There is zero telemetry. The graph is a SQLite file in `.code-review-graph/`
inside your repo, and the core build / review / search / MCP workflows run entirely
locally. The streamable-HTTP MCP transport binds to localhost by default.

The only network activity is opt-in:

- **Local embeddings** (`pip install "code-review-graph[embeddings]"`) download the
  sentence-transformers model from HuggingFace on first use. Your code does not leave
  the machine.
- **Cloud embeddings** (OpenAI-compatible, Google Gemini, MiniMax) send the text being
  embedded — currently function signatures — to the provider you explicitly configure
  via environment variables. CRG prints an egress warning unless you acknowledge it
  with `CRG_ACCEPT_CLOUD_EMBEDDINGS=1`; the warning is skipped automatically when the
  endpoint is localhost.

See [LEGAL.md](LEGAL.md) for the full privacy notes.

## How do I verify it is working?

1. **Check the graph exists and has content:**

   ```bash
   code-review-graph status
   ```

   You should see node/edge counts and graph statistics. Zero nodes means the build
   didn't run or found nothing to parse.

2. **See the savings on a real change** — make any edit, then:

   ```bash
   code-review-graph detect-changes --brief
   ```

   This prints the risk summary and the boxed **Token Savings** panel against the
   existing graph (read-only). Add `--verify` to cross-check the estimate against
   OpenAI's `cl100k_base` tokenizer (requires `pip install tiktoken`). If you suspect
   the graph is stale, `code-review-graph update --brief` re-parses changed files
   first and prints the same panel.

3. **Check the MCP wiring** — in Claude Code, run `/mcp` and confirm the
   `code-review-graph` server is connected with its tools listed. Then ask the
   assistant something structural ("what calls `parse_file`?") and watch it use
   `query_graph` instead of grepping.

If any of these fail, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## How big a codebase justifies it?

This comes up often (see #414). Honest guidance, tied to the documented small-repo
overhead:

- **Below a few hundred files:** marginal. The graph builds in seconds and works
  fine, but an agent can already hold most of the repo in context, and for trivial
  diffs the structural response can cost more tokens than it saves (the documented
  overhead regime — see [When should I not use it?](#when-should-i-not-use-it)).
- **A few hundred to a few thousand files:** this is where the benchmarks live. The
  six evaluation repos range from 60 to ~1,100 files and show 38x–528x reductions on
  whole-corpus agent questions, with the caveat noted above about what that baseline
  measures.
- **Multi-thousand-file repos and monorepos:** the strongest case. No agent can read
  the corpus per question (FastAPI alone is ~950k tokens of source), re-deriving
  structure by search every session is the dominant cost, and incremental updates
  keep the graph fresh in under 2 seconds.

A second axis matters as much as file count: **how often you ask multi-file
questions**. A 300-file repo you review daily benefits more than a 3,000-file repo
you touch once.

## How does it handle monorepos, git worktrees, and multiple repos?

**Monorepos.** One graph per repository root by default — commands auto-detect the
root by walking up to the nearest `.git`, and in git repos only tracked files are
indexed (`git ls-files`), so gitignored build artifacts are skipped automatically.
Use a `.code-review-graphignore` file to exclude tracked paths (e.g. `vendor/**`,
generated code), or pass `--repo <path>` to point a command at a specific directory.

**Git worktrees.** Each worktree is detected as its own root, so each gets its own
`.code-review-graph/` database matching its checkout. Don't try to share one database
across worktrees at different commits — the graph reflects one working tree. If you
want the database outside the working tree entirely (ephemeral workspaces, network
shares), use `--data-dir <path>` on `build`/`update`/etc., or set the `CRG_DATA_DIR`
environment variable.

**Multiple repos.** A lightweight registry (stored at
`~/.code-review-graph/registry.json`) lets MCP clients search across projects:

```bash
code-review-graph register ~/work/api --alias api   # add a repo (optional alias)
code-review-graph repos                             # list registered repos
code-review-graph unregister api                    # remove by path or alias
```

Once registered, the `list_repos_tool` and `cross_repo_search_tool` MCP tools work
across all of them. To keep several graphs fresh automatically, the bundled daemon watches
registered repos as child processes:

```bash
crg-daemon add ~/work/api --alias api
crg-daemon start
crg-daemon status
```

(Also available as `code-review-graph daemon start|stop|status`.) See
[COMMANDS.md](COMMANDS.md) for the full daemon reference.
