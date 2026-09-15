# Multi-repo organisation references — design

Date: 2026-09-15 (revised after adversarial review). Status: for maintainer review.
Branch target: `staging`.

## 1. Goal

Let a repository reference symbols that live in sibling repositories of the same
organisation, so that `import x from "../../ui/src/button"` or
`from org_lib import thing` in repo A produces an edge that queries can follow into
repo B's graph, and so that cross-repo tools can be scoped to a named group.

Constraints chosen by the maintainer:

- **Group lives on registry entries.** No second config file.
- **Tag at build, resolve at query.** Cross-repo edges are ordinary edges whose
  `extra` JSON names the target repository. No schema migration.
- **Cross only into same-group siblings.** Resolvers clamp to the repo root unless
  the target is inside a registered sibling of the same group.
- PR #880 (`npm_alias_resolver.py`) is merged first. It resolves `npm:`-prefixed
  dependency aliases to workspace packages inside one repository and cannot leave the
  root; sibling package lookup is new code that runs after it returns `None`.

Non-goals: a shared server-side graph (#931), a shared base graph across worktrees
(discussion #464), per-call `data_dir` (#950, declined in #951), rendering several
repos in one visualization.

Sequencing: after PR #988 merges (it rewrites `tools/registry_tools.py` with
`_select_repos` and the `repos` filter from #915, moves the schema to v10, adds the
token-budget contract in `tools/_common._bounded` and `tests/test_token_budget.py`,
and changes the pre-commit hook). Then review and merge #880.

## 2. Current state

- `~/.code-review-graph/registry.json` is `{"repos": [{path, alias?, data_dir?}]}`
  with no version key. `Registry` has `register(path, alias=None, data_dir=None)`
  (re-registering a path updates alias and data_dir), `unregister`, `list_repos`,
  `find_by_alias`, `find_by_path`, `set_data_dir`, `get_data_dir_for_repo`.
  `ConnectionPool` and `resolve_repo` exist but nothing outside tests calls them.
  `Registry.register` does not enforce alias uniqueness; `daemon.add_repo_to_config`
  does. `Registry.__init__` creates its parent directory.
- Two tools cross repos, both search-only: `list_repos_tool` (no parameters) and
  `cross_repo_search_tool(query, kind, limit, max_results, repos)` on #988. Every
  other tool takes one `repo_root`; `main._resolve_repo_root` applies the
  `serve --repo` default, then `tools/_common._resolve_root` → `_validate_repo_root`.
  Aliases are not accepted anywhere.
- Node identity is `<absolute POSIX path>::<symbol>`; File nodes are the bare path.
  Builds canonicalise the root with `_canonical_repo_root` (`expanduser().resolve()`)
  and store `str(repo_root / rel_path)`, which matches what `Registry.register`
  stores. Each `graph.db` is isolated.
- Resolvers that leave the root today and emit `IMPORTS_FROM` edges whose target is an
  out-of-root absolute path with no node anywhere: the JS/TS branch of
  `_do_resolve_module` (`(caller_dir / module).resolve()`), the Python, Java and
  Kotlin branches of the same function (`while True: current = current.parent`),
  `TsconfigResolver._load_tsconfig_for_file` (walks to the filesystem root) and
  `TsconfigResolver._match_and_probe`. `_resolve_python_module_in_repo` is already
  bounded by `_python_repo_boundary` and only serves star-import export maps; Rust
  and PHP resolvers are bounded too.
- `edges.extra` is `TEXT DEFAULT '{}'`; `EdgeInfo.extra` is serialised in
  `upsert_edge`, which also derives `confidence` and `confidence_tier` from it, and
  read back into `GraphEdge.extra` by `_row_to_edge`. `idx_edges_target` indexes
  `target_qualified`. `json_extract` is already used by `get_all_files`.
- Tools tolerate edge targets with no local node only when the target is bare (no
  `::`) or carries `ambiguous_targets` / `unresolved_targets`; a qualified target with
  no node is silently skipped by `callees_of`, and impact radius drops ghost endpoints
  through its `JOIN nodes`.
- `uncertainty._staleness` already compares `git_head_sha` metadata with the live
  HEAD for the empty-result `confidence` sentence. No tool emits a `caveats` key.
- Daemon: `WatchRepo` has `path` and `alias`; `_serialize_toml` writes only those;
  `Daemon.start` and reload call `registry.register` with no error handling.
- `CodeParser` is constructed in five places in `incremental.py`, including inside
  `ProcessPoolExecutor` workers from a `(rel_path, repo_root_str)` tuple.

## 3. Registry

**Entry fields.** Existing `path`, `alias`, `data_dir` plus:

- `group: str | None` — organisation name, free text, case-sensitive, no
  normalisation.
- `packages: list[str]` — package names this repository publishes. Auto-detected at
  register time from `package.json` (`name`, plus each `workspaces` member's name,
  globs expanded; `pnpm-workspace.yaml` honoured), `pyproject.toml`
  (`[project].name`, plus the import name: the top-level package directory under the
  root or `src/`), `setup.cfg` (`metadata.name`), `go.mod` (`module`), `Cargo.toml`
  (`[package].name`), `composer.json` (`name`). Detection reads manifests as
  text/TOML/JSON/YAML only; never executes `setup.py`. `--package NAME` adds to the
  union.

The file gains `"version": 2`. Version-1 files (no `version`) load unchanged with the
new fields defaulting to `None` / `[]`; the next save writes version 2.

**API.**

```
Registry.register(path, alias=None, data_dir=None, group=None, packages=None,
                  replace_alias=False)
    # idempotent for a registered path: updates alias/data_dir/group/packages when
    # given; returns a COPY of the entry plus "updated": True/False;
    # raises ValueError when alias belongs to a different path unless replace_alias
Registry.list_repos(group=None)             # returns copies
Registry.find_repo_containing(abs_path)     # longest registered path prefix
Registry.find_by_package(name, group=None)
Registry.siblings_of(path)                  # same group, excluding the entry whose
                                            # path equals or contains `path`; copies
```

Loading a registry that already holds duplicate aliases logs a warning naming both
entries and keeps the first. `set_data_dir`-created entries get the same defaults.

**CLI.** `register <path> [--alias A] [--group G] [--package P ...]`,
`repos [--group G]`.

**Daemon.** `WatchRepo` gains `group`; `_load_config` / `_serialize_toml` round-trip
it; auto-register passes `group` and `replace_alias=True` inside `try/except
ValueError` with a warning, so a stale alias cannot stop `daemon start`.

## 4. Build-time tagging

**Where the sibling maps come from.** `full_build` and `incremental_update` compute
them once per run, keyed on `_canonical_repo_root(root)`, guarded by
`default_registry_path().is_file()` (as `get_data_dir` already does), and only when
the entry has a group: `sibling_roots: dict[str, str]` (resolved root → alias) and
`sibling_packages: dict[str, tuple[str, str]]` (package name → (alias, root)). Both
are plain dicts and travel to every `CodeParser` construction site in
`incremental.py`, including the worker args tuple and `_PARSE_WORKER_STATE` for the
process pool. `--no-siblings` (and `CRG_NO_SIBLINGS=1`) disables the lookup. The
pre-commit hook, PostToolUse hook, `watch` and the daemon all reach this code through
`incremental_update`, so nothing else needs to pass a group.

**Boundary rule**, applied where a resolved path can leave the root: the JS/TS,
Python, Java and Kotlin branches of `_do_resolve_module`, and both
`TsconfigResolver._load_tsconfig_for_file` and `_match_and_probe`
(`TsconfigResolver.__init__` gains `repo_root` and `sibling_roots`):

1. Resolve as today.
2. Under the repo root: unchanged.
3. Under a sibling root: keep. The resolved path is the target for file-level
   `IMPORTS_FROM` edges; `_resolve_module_to_file` returns the path and the sibling
   alias so every emission site can tag.
4. Otherwise: fall back to the raw module string as the target, the way
   `exclude_files` handling already does, and count it under `clamped_out_of_root`.
   This is a behaviour change for ungrouped users: an out-of-root import that today
   yields a dangling absolute-path target becomes a bare-specifier target, which the
   empty-result `confidence` logic already understands.

**Bare package specifiers** (`@org/ui`, `org_lib`, `github.com/org/lib`): after the
tsconfig and `npm:` alias resolvers return `None` in the JS branch (and the
equivalent point in the Python and Go branches), look the specifier up in
`sibling_packages` (longest-prefix match for Go modules) and resolve to the sibling
package's entry point (`main` / `exports` from `package.json`; the import package
directory for Python; the module root for Go). Same tagging.

**Tag shape and emission sites.** A helper `_sibling_for_path(path) -> alias | None`
(longest matching sibling root) decides tagging at every site that emits an edge
whose target was resolved through a file path: `_extract_imports` (file-level
`IMPORTS_FROM`, target `<sibling root>/<rel>`), `_resolve_imported_symbol` and
`_resolve_exported_symbol` (symbol-level CALLS / REFERENCES / INHERITS targets
`<sibling root>/<rel>::<symbol>`; these read the sibling's source file to follow
re-exports, which is allowed only under registered sibling roots), and the post-parse
`_resolve_call_targets` pass. `edge.extra` gains
`{"cross_repo": true, "target_repo": "<alias>", "target_root": "<POSIX root>"}`.
`confidence` / `confidence_tier` are unchanged.

**Counters.** The worker return tuple gains a per-file `stats` dict
(`clamped_out_of_root`, `cross_repo_edges`), merged by `full_build` /
`incremental_update` into the build summary.

No placeholder nodes are inserted for cross-repo targets. Because qualified targets
without a local node are dropped today, `callees_of`, `references_to`, `imports_of`
and impact radius gain an explicit `extra.cross_repo` branch (§5).

## 5. Query-time resolution

**Sibling graphs.** A new `GraphStore.open_read_only(db_path)` classmethod opens with
`sqlite3.connect(f"file:{path}?mode=ro", uri=True, ...)`, skips `_init_schema` and
`run_migrations`, and sets no WAL pragma. A `SiblingGraphs` helper (replacing the
unused `ConnectionPool`) locates each sibling's database with
`get_db_path(sibling_root, read_only=True)` (registry `data_dir` → `CRG_DATA_DIR` →
default), keeps an LRU sized to the group, and validates each store: schema version
metadata equals `LATEST_VERSION`, and the File-marker prefix matches the sibling root
(the check `_assert_graph_matches_root` performs). A missing, legacy-path, foreign-root
or wrong-schema sibling produces a caveat, never an exception. At most
`CRG_MAX_SIBLINGS` (default 8) siblings are consulted per call; the rest are reported
in `siblings_skipped`.

**`query_graph_tool`** gains `cross_repo: bool = False`.

- Forward patterns (`callees_of`, `imports_of`, `references_to`): an edge whose
  `extra.cross_repo` is set is emitted even though the target has no local node. With
  `cross_repo=False` it is emitted with `repo: "<alias>"` and `followed: false`; with
  `cross_repo=True` the target node (kind, file, line) is fetched from the sibling
  store, or `target_missing_in_sibling: true` is reported when the sibling has no
  such node (a rename in the sibling that this repo has not re-parsed yet).
- Reverse patterns (`callers_of`, `importers_of`, `inheritors_of`, `tests_for`): each
  consulted sibling is queried for edges into this repo:
  `WHERE (target_qualified = ? OR target_qualified LIKE ? || '::%') AND
  json_extract(extra, '$.target_repo') = ?`, using `idx_edges_target`.

Every cross-repo result passes through the same `add_result` / `response_limit`
bounding as local results.

**`get_impact_radius_tool`** gains `cross_repo: bool = False`. A new store method
`get_cross_repo_edges_from(qualified_names)` returns tagged edges whose source is in
`seeds ∪ impacted` (`json_extract(extra, '$.cross_repo') = 1`). For each target repo
the sibling's impact query is seeded with those targets and `max_depth - 1`. Results
merge under `cross_repo_summary: {repos: [...], nodes_by_repo: {...},
siblings_skipped: n}`, every list `_bounded`.

**`cross_repo_search_tool`** gains `group: str | None`, composed with #915's `repos`
list inside `_select_repos` as AND (both filters must admit an entry).
**`list_repos_tool`** gains a standalone `group` filter over
`Registry.list_repos(group=)`.

**`repo_root` accepts a registry alias** on every tool. `_resolve_root` treats the
value as a path when it contains a path separator or exists on disk; otherwise, and
only when the registry file exists, it tries `Registry.find_by_alias`; the result then
passes `_validate_repo_root` as today.

**Honesty.** Cross-repo is off by default. Responses gain a `caveats: list[str]` key
(bounded, added to the `BUDGETS` table with worst-case `cross_repo=True` entries)
carrying sibling problems: stale (`git_head_sha` differs from the sibling's HEAD,
reusing `_staleness` / `_live_git_head`), missing, wrong schema, foreign root,
skipped. `empty_query_confidence` gains a `cross_repo_hint` argument so an empty
result mentions "cross-repo resolution is off" when tagged edges were seen but not
followed.

**Staleness across repos.** A's cross-repo edges name B's paths and symbols. A rename
in B leaves A's edge pointing at nothing until A's importing file is re-parsed;
incremental updates in A cannot see B's changes. This is documented, surfaced as
`target_missing_in_sibling`, and not solved here.

## 6. Security

- Sibling roots and packages come only from the registry file under `CRG_HOME`,
  which the user controls. Every sibling root passes `_validate_repo_root`.
- Resolved targets must lie under the repo root or a registered sibling root after
  `Path.resolve()`; symlink escapes fall back to the bare specifier.
- Sibling stores are opened read-only; all SQL is parameterised; returned names go
  through `_sanitize_name`. Manifest detection never executes code.

## 7. Testing

New fixture `tests/fixtures/org/` with two mini repos, `app` and `lib`: a JS relative
import `../../lib/src/button`, a tsconfig `paths` alias into `lib`, a bare `@org/lib`
specifier, and a Python `from org_lib import helper`.

Tests pass an explicit `Registry(path=tmp_path / "registry.json")` or set `CRG_HOME`:

- registry: v1 file loads, v2 round-trips, duplicate alias error and `replace_alias`,
  `group` listing, `find_repo_containing` (self excluded), package detection per
  manifest, idempotent re-register returns a copy.
- parser: out-of-root target becomes a bare specifier with no group; tagged with the
  sibling alias when grouped; the tagged target equals the node id produced by
  building `lib`; parallel builds (process pool) tag identically to serial ones.
- end-to-end: build both repos, `query_graph_tool(cross_repo=True)` for `callees_of`,
  `imports_of` and `callers_of` across the boundary, `get_impact_radius_tool(
  cross_repo=True)` summary, `cross_repo_search_tool(group=...)`, alias accepted as
  `repo_root`, `cross_repo=False` still lists the tagged edge as unfollowed.
- caveats: stale sibling, missing sibling, wrong schema version, `siblings_skipped`.
- budgets: `tests/test_token_budget.py` worst-case entries for `cross_repo=True`.
- daemon: `group` round-trips through `watch.toml`; a duplicate alias warns instead of
  failing `daemon start`.

## 8. Documentation

README multi-repo section, `docs/FAQ.md` registry and monorepo guidance,
`docs/COMMANDS.md` for the new parameters, `docs/schema.md` for the `extra` keys
(`cross_repo`, `target_repo`, `target_root`) and the `caveats` response key,
`CHANGELOG.md` `[Unreleased]`, including the bare-specifier behaviour change.

## 9. Rollout (each a PR into `staging`)

0. Merge #988; review and merge #880.
1. Registry v2 + CLI + daemon `group` (no behaviour change for existing users).
2. Parser boundary rule + tagging + counters, with the two-repo fixture.
3. Query-time resolution: `open_read_only`, `SiblingGraphs`, `query_graph`,
   `impact_radius`, `cross_repo_search(group=)`, alias as `repo_root`, `caveats`,
   budgets.
4. Docs and FAQ.

## 10. Decisions recorded

- Group is a free-text, case-sensitive string on the registry entry.
- `target_root` is stored POSIX-normalised like every stored path.
- `group` and `repos` filters compose as AND; `_select_repos` keeps its signature and
  receives the group-filtered entry list.
- Ungrouped out-of-root imports become bare-specifier targets (behaviour change,
  documented in the changelog).
- Sibling databases are never written and never migrated by a consumer.

## 11. Follow-ups outside this spec

- Strict multi-root `serve --http` with no cwd fallback (#311, #607).
- Shared graph backends (#931) and the worktree base-graph design (discussion #464)
  need a maintainer policy answer on the threads.
