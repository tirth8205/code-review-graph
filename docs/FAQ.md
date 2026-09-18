# FAQ: how code-review-graph compares

Where another tool is better for a job, this page says so.

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

Both build a structural model of your code. They are built for different jobs.

**What LSP does better.** A language server sits on a compiler front end, so
its results are type-aware: go-to-definition through generics and overloads,
find-references that understands scoping, diagnostics, completions and safe
renames. For a complete reference list for one symbol in one language, use the
language server.

**What CRG does differently.**

- One graph for the whole repository. Language servers run one process per
  language and rebuild or revalidate per session. CRG parses with Tree-sitter,
  stores nodes and edges in one SQLite file (`.code-review-graph/graph.db`), and
  answers queries across more than 35 languages plus notebooks from one process,
  including cross-language edges.
- It persists. `update` re-parses only changed files and their dependents;
  hooks and `watch` keep it current between sessions.
- Review-oriented relationships: `tests_for`, execution flows, communities and
  risk-scored change analysis, which LSP does not model.

**Trade-off.** CRG's call resolution is AST-level and heuristic, not
compiler-backed. Dynamic dispatch, metaprogramming and duck typing produce
inferred edges, so every edge carries a confidence tier
(`EXTRACTED` or `INFERRED`). LSP is more precise per symbol; CRG is
broader, persistent and cheaper to query across the repository.

## Isn't this just RAG?

No. RAG splits code into text chunks, embeds them and retrieves by similarity.
That answers "find code that talks about X". It cannot answer "who calls X":
similarity between two functions says nothing about whether one calls the other.

CRG stores edges parsed from the AST: calls, imports, inheritance, test
coverage. "Who calls `login()`" is a graph lookup.

Embeddings are optional. They are one input to hybrid search (FTS5 BM25 plus
vectors) used to find a starting node; traversal then follows edges. The text
embedded per node is its name, parent, signature, a capped docstring summary,
directory and language, not the function body.

The benchmark that shows the difference is multi-hop retrieval: natural-language
query, anchor node, then one hop (`callers_of`, `tests_for`, ...). CRG scores
0.909 across 11 tasks on 6 repositories (see [REPRODUCING.md](REPRODUCING.md)).
Similarity retrieval has no second hop.

**Where RAG-style search is better:** conceptual questions over prose, comments
and docs ("where is rate limiting discussed?"). CRG's own keyword ranking is a
known weakness (MRR 0.35; see the limitations in the
[README](../README.md#benchmarks)).

## Why not just grep?

Claude Code deliberately ships without a code index. Agentic search (glob,
grep, targeted reads) is as fresh as the working tree, has no staleness failure
modes and needs no setup. For one-hop questions ("where is `parse_file`
defined?") it works well and CRG will not beat it by much.

The gap is multi-hop structural questions, where each hop costs another round
of grep, read and reasoning:

- Impact radius: "what could break if I change this file?" needs callers,
  dependents and their tests. One `get_impact_radius_tool` call returns all
  three.
- Callers of callers: `traverse_graph_tool`, or repeated
  `query_graph_tool(pattern="callers_of")`, instead of grepping each
  intermediate name. Grep matches text, so overloaded or re-exported names give
  false hits the agent has to read to rule out.
- Tests for: `query_graph_tool(pattern="tests_for")` maps code to tests through
  parsed edges and naming conventions; `detect_changes_tool` adds transitive
  coverage. Grep only finds tests that mention the name literally.
- Affected flows: "which execution paths does this change touch?" has no grep
  equivalent.

The graph also persists. Agentic search re-derives the same structure every
session.

One caveat on the numbers: the whole-corpus token-reduction figures (about 65x
median, 36x to 376x range) compare a graph response with reading the whole
corpus, not with a skilled agentic-grep session (see
[REPRODUCING.md](REPRODUCING.md) for what each benchmark measures). For
single-hop lookups in a small repository, grep is cheap and good.

## How does it compare to Serena, codegraph, claude-context, and repomix?

These solve adjacent problems. The table is based on each project's public
documentation; check upstream for current behaviour.

| Tool | Approach | Persistence | External deps | Review focus |
|---|---|---|---|---|
| **code-review-graph** | Tree-sitter AST to structural graph (calls, imports, inheritance, tests) over MCP and CLI | SQLite in `.code-review-graph/`, incremental updates | None for the core; embeddings optional | Yes: blast radius, risk-scored change analysis, test-gap detection |
| **Serena** | LSP-backed symbol retrieval and editing tools over MCP | Language-server state plus per-project memories | A language server per language | General coding-agent toolkit, not review-specific |
| **codegraph** | AST/call-graph indexing over MCP (several projects share this name) | Varies by implementation | Varies by implementation | Retrieval-focused |
| **claude-context** | Chunk and embed semantic code search over MCP | Vector index in a vector database | Embedding provider plus vector DB | Search-focused, not review-specific |
| **repomix** | Packs the whole repository into one file for an LLM | None; regenerated per run | Node.js | One-shot context packing; no structural queries |

If you want symbol-precise editing tools, Serena's LSP approach fits better.
If you want semantic search and can run a vector store, claude-context covers
that. If the repository fits in one context window, repomix is the simplest
option. CRG's niche is a persistent structural graph for review: impact
analysis, risk scoring and test-coverage tracing with no external services.

## When should I not use it?

- Repositories under a few hundred files. An agent can read what it needs
  directly; the graph's structural metadata is overhead a small repository does
  not repay. See [How big a codebase justifies it?](#how-big-a-codebase-justifies-it).
- Trivial single-file changes. The review response carries impact-radius edges
  and source snippets, which can exceed a one-file diff. The formal
  `token_efficiency` benchmark reports ratios below 1.0 for small commits (see
  [REPRODUCING.md](REPRODUCING.md)).
- One-off questions on a repository you will not revisit. The payoff comes from
  reuse across queries and sessions.
- Flow detection on JavaScript and Go. Entry-point detection is reliable mainly
  for Python and PHP/Laravel patterns; overall flow recall is 33% (see the
  README limitations).

## Does it phone home?

No. There is no telemetry. The graph is a SQLite file in your repository, and
build, review, search and the MCP server run locally. `serve --http` binds to
`127.0.0.1` by default.

The only network activity is opt-in:

- Local embeddings (`pip install "code-review-graph[embeddings]"`) download the
  `all-MiniLM-L6-v2` model from Hugging Face on first use. Your code stays on
  the machine.
- Cloud embeddings (OpenAI-compatible, Google Gemini, MiniMax, Voyage AI) send
  the embedded text (node names, signatures, docstring summaries and file paths)
  to the provider you configure through environment variables. CRG prints an
  egress warning on stderr until you set `CRG_ACCEPT_CLOUD_EMBEDDINGS=1`; the
  warning is skipped when the endpoint is localhost.

See [LEGAL.md](LEGAL.md).

## How do I verify it is working?

1. Check the graph:

   ```bash
   code-review-graph status
   ```

   It prints `Nodes`, `Edges` and `Files`. Zero nodes means the build did not
   run or found nothing to parse.

2. See the savings on a real change. Edit something, then:

   ```bash
   code-review-graph detect-changes --brief
   ```

   This prints the risk summary and the Token Savings panel against the
   existing graph without re-parsing. Add `--verify` to compare the estimate
   with the `cl100k_base` tokenizer (`pip install tiktoken`). If the graph may
   be stale, `code-review-graph update --brief` re-parses changed files first
   and prints the same panel.

3. Check the MCP wiring. In Claude Code run `/mcp` and confirm
   `code-review-graph` is connected. Ask something structural ("what calls
   `parse_file`?") and watch the assistant call `query_graph_tool` instead of
   grepping.

If any step fails, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## How big a codebase justifies it?

- Below a few hundred files: marginal. The graph works, but an agent can hold
  most of the repository in context, and for trivial diffs the structural
  response can cost more tokens than it saves (see
  [When should I not use it?](#when-should-i-not-use-it)).
- A few hundred files and up: the six evaluation repositories (express,
  fastapi, flask, gin, httpx and code-review-graph) show 36x to 376x reductions
  on whole-corpus questions, with the caveat above about the baseline.
- Multi-thousand-file repositories and monorepos: the strongest case. No agent
  can read the corpus per question (FastAPI alone is about 950k tokens of
  source), and incremental updates keep the graph current.

File count is only one axis. A 300-file repository you review daily benefits
more than a 3,000-file repository you touch once.

## How does it handle monorepos, git worktrees, and multiple repos?

**Monorepos.** One graph per repository root. Commands find the root by walking
up to the nearest `.git`, and in git repositories only tracked files
(`git ls-files`) are indexed, so gitignored build output is skipped. Use
`.code-review-graphignore` to exclude tracked paths (`vendor/**`, generated
code), or pass `--repo <path>` to point a command at a specific directory.

**Git worktrees.** Each worktree is its own root and gets its own
`.code-review-graph/` matching its checkout. Do not share one database across
worktrees at different commits. The pre-commit hook skips linked worktrees
unless `CRG_HOOK_WORKTREES=1` is set, so a commit there does not build a second
graph by accident. To keep the database outside the working tree (ephemeral
workspaces, network shares), pass `--data-dir <path>` to `build`, `update`,
`status` and the other graph commands, or set `CRG_DATA_DIR`.

**Multiple repos.** A registry at `~/.code-review-graph/registry.json` (or
under `$CRG_HOME`) lets MCP clients search across projects:

```bash
code-review-graph register ~/work/api --alias api   # add a repo (optional alias)
code-review-graph repos                             # list registered repos
code-review-graph unregister api                    # remove by path or alias
```

Once registered, `list_repos_tool` and `cross_repo_search_tool` work across
all of them. To keep several graphs current, the daemon watches registered
repositories as child processes:

```bash
crg-daemon add ~/work/api --alias api
crg-daemon start
crg-daemon status
```

Also available as `code-review-graph daemon start|stop|status`. See
[COMMANDS.md](COMMANDS.md) for the daemon reference.
