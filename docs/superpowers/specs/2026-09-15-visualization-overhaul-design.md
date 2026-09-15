# Visualization overhaul — design

Date: 2026-09-15 (revised after adversarial review). Status: for maintainer review.
Branch target: `staging`.

## 1. Goal

Make `code-review-graph visualize` useful on real repositories (10k–100k+ nodes)
without giving up the property that made it adoptable: one offline HTML file,
no server, no account. Four phases, each its own PR series into `staging`:

1. **Maintenance** — fix confirmed bugs, make the size caps real, document what exists.
2. **Sigma.js renderer** — WebGL rendering with worker layout; raises the full-mode
   ceiling from 3k to ~30k nodes; D3 canvas fallback when WebGL is unavailable.
3. **UX pack** — focus+context, path highlight, hierarchical collapse, heat overlays,
   deep links, minimap. Renderer-independent.
4. **Served lazy mode** — `visualize --serve` becomes a read-only local API so the page
   loads only what is viewed; the same renderer file backs the VS Code webview.

Non-goals: a hosted service, multi-repo rendering (see the multi-repo spec),
GPU-only renderers as the default, editing the graph from the UI, a light theme.

Sequencing: starts after PR #988 merges to `staging`. #988 touches `cli.py`,
`tools/`, the schema (v10) and already moves the Action cache key to `schema10`;
this work leaves both alone. The CI trigger change for `staging` PRs landed in #989.

## 2. Current state

- `code_review_graph/visualization.py` (2355 lines) dumps the whole graph with
  `export_graph_data(store)` and splices it into one of two inline templates:
  `_HTML_TEMPLATE` (full D3 v7 SVG force layout, one ~700-line inline script of
  top-level `var`s whose state functions write straight into D3 selections) and
  `_AGGREGATED_HTML_TEMPLATE` (one super-node per community or file, drill-down).
- `generate_html(store, output_path, mode="auto", max_full_nodes=3000,
  max_full_edges=9000)`; `_resolve_auto_mode` picks full only under both caps
  (`DEFAULT_MAX_FULL_NODES`, `DEFAULT_MAX_FULL_EDGES`).
- `--mode full` and community drill-down have no cap at all and render everything as
  SVG DOM. #609 stalled at 2792 nodes / 17488 edges before the edge cap existed.
- File nodes auto-collapse only above 2000 nodes (#132). A separate `isLarge = N > 300`
  switch tunes force parameters only.
- D3 is vendored at `code_review_graph/assets/d3.v7.min.js`, copied next to the HTML
  by `_write_d3_asset`, SRI-pinned, with a `document.write` CDN fallback (#475).
- `visualize --serve` (the `--serve` block of the visualize handler in `cli.py`)
  serves `html_path.parent`, which is the data directory, with
  `SimpleHTTPRequestHandler` on fixed port 8765 bound to `localhost`. That exposes
  every file in the data directory (`graph.db`, earlier exports, the wiki) and
  whatever else lives in a shared `--data-dir` / `CRG_DATA_DIR`.
- Exporters in `exports.py`: `export_json`, `export_graphml`, `export_neo4j_cypher`,
  `export_obsidian_vault`, `export_svg`. GraphML uses the namespace
  `http://graphml.graphstruct.org/graphml` (wrong). `_cypher_props` checks
  `isinstance(v, (int, float))` before its `bool` branch, so a boolean would be
  emitted as Python `True`/`False`; latent today because no boolean property is
  passed. None of the four non-JSON exporters has a test.
- Confirmed UI bugs in `_HTML_TEMPLATE`: `updateLinks()` ignores community hiding, so
  edges to hidden communities dangle; the clear branch of `applyCommunityFilter()`
  never resets label display; `showDetailPanel()` hides `#legend` and only the
  close-button and svg-click handlers restore it, not Escape; the node `focus`
  handler calls `moveTooltip(ev)` which reads `pageX` from a `FocusEvent`.
- `_aggregate_file` rescans all nodes per file; `_aggregate_community` duplicates full
  node dicts into `community_details`; `_obsidian_slug` collision check is quadratic.
- Tests cover the Python data path (export, aggregation, auto-mode, defaults, the D3
  asset pin) plus string-presence and `node --check` syntax assertions on the HTML.
  Nothing executes the page in a browser. Several tests pin the exact script-tag list
  (`script_sources == [_D3_FILENAME]`), the `__D3_SCRIPTS__` sentinel, SVG-specific
  strings in `_assert_responsive_graph_script`, the D3 symbol names, and the node-dict
  shape of `community_details`.
- The VS Code extension has a second D3 SVG renderer (`src/views/graphWebview.ts`,
  `src/webview/graph.ts`, `d3` npm dependency) reading `graph.db` with a first-N node
  slice (default 500). Its webview CSP has no `worker-src`.
- Demand: zero open visualization issues. Every reported breakage is fixed on main.

## 3. Phase 1 — Maintenance (small)

**Bugs.** Fix the four UI bugs. GraphML namespace becomes
`http://graphml.graphdrawing.org/xmlns` with the standard `schemaLocation`. In
`_cypher_props`, move the `bool` check ahead of the `(int, float)` check; the test
injects a boolean property to observe it. Precompute a file → community map in
`_aggregate_file`, build `community_details` in one pass, use a set for slug
collisions.

**Caps become real.** `--mode full` honours `max_full_nodes` / `max_full_edges`;
exceeding them requires an explicit `--max-nodes N --max-edges M` and prints the
counts. Community drill-down data is truncated in Python at generation time to the
top `max_full_nodes` members by degree, with `member_total` recorded so the page shows
"N of M rendered".

**Filters.** `--include GLOB` / `--exclude GLOB`, repeatable. Matching uses `fnmatch`
on repo-relative POSIX paths (absolute node paths are made relative to the repo root
first). Include is applied first, then exclude. Edges with a filtered endpoint are
dropped. Displayed counts are recomputed as "rendered N of M". No interaction with
`forget` or `.code-review-graphignore`, which are build-time mechanisms. The existing
">50k nodes, consider filtering" warning names these flags.

**Payload.** A new `_slim_for_page(data)` step inside `generate_html` drops keys the
templates never read. `export_graph_data` and `--format json` are unchanged. Dropped:
edge `id`, `file_path`, `line`, `confidence`, `confidence_tier`, `ambiguous_targets`,
`unresolved_targets`, `ambiguous_target_count`, `unresolved_target_count`,
`ambiguous_targets_truncated`, `unresolved_targets_truncated`; node `is_test`,
`parent_name`, and `id` when there are no flows. `community_details` members become
node indices into the single node table. The existing test
`test_community_detail_data_complete` is rewritten for index references.

**`--serve` hardening.** When `--serve` is given, `graph.html` and the vendored JS are
generated into a temporary directory and that directory alone is served; the path is
printed. Bind `127.0.0.1` (decided here so Phase 4 agrees). Add `--port` (default
8765) and `--open` (`webbrowser.open`). The handler rejects requests whose `Host` is
not loopback using `is_loopback_host` / `split_host_port` from
`http_origin_guard.py` (the class `LoopbackOriginGuard` is ASGI middleware and does
not apply to `http.server`).

**Docs.** On top of #988's rewritten docs: document `--mode auto|full|community|file`,
the caps and override flags, `--serve` flags, `--include/--exclude`, and the
vendored-D3 offline behaviour in `docs/COMMANDS.md`, `docs/USAGE.md`,
`docs/FEATURES.md`. Formats are already listed in #988's `COMMANDS.md`. Fix the stale
"starts collapsed" wording in `docs/USAGE.md`, `docs/FEATURES.md`, `docs/ROADMAP.md`.

**Tests.** Exporter tests for graphml (parse with `xml.etree`), cypher (boolean
property), obsidian, svg; `--mode` dispatch and cap-override tests; a `--serve`
handler test proving only the temp directory is served and a foreign `Host` is
rejected.

## 4. Phase 2 — Sigma.js renderer (medium)

**Extract the JS first.** The inline script moves to
`code_review_graph/assets/viz-core.js` (shipped in the wheel like the D3 asset);
`__GRAPH_DATA__` stays inline. This is a rewrite of the page script, not a refactor,
and it is done here so the Phase 3 PRs do not all edit one Python raw string.

**Boundary.** The model owns graph data, collapse state, filters, search index,
selection, and keyboard handling; it never touches DOM, canvas, or WebGL. A renderer
implements `mount(container)`, `update(nodes, edges)`, `focus(id)`, `fit()`,
`setColorBy(key)`, `destroy()` and owns positions, camera, hit-testing and drawing.
Two renderers: `SigmaRenderer` (WebGL) and `D3CanvasRenderer` (canvas with
`simulation.find` hit-testing; replaces the SVG path in full mode). The aggregated
template stays on D3 SVG until Phase 3g folds it into the same file.

**Assets.** Vendor `graphology.umd.min.js`, `sigma.min.js` (3.x) and
`graphology-library.min.js` (the one bundle that carries `circlepack`, `FA2Layout` and
`shortestPath`) under `code_review_graph/assets/` with SHA-384 pins, loaded in that
order (graphology is a global dependency of the other two). `_write_d3_asset`
generalises to `_write_viz_assets()`. CDN fallback tags carry the same integrity
hashes (cdnjs for graphology and sigma, jsdelivr for graphology-library). D3 stays for
the canvas fallback and the aggregated template, so the on-disk cost next to the HTML
is about 710 KB total. The `__D3_SCRIPTS__`-before-`__GRAPH_DATA__` guard becomes a
single `__VIZ_SCRIPTS__` placeholder. `--inline-assets` embeds everything for a true
single file; inline mode escapes `</script` inside vendored sources.

**Layout.** ForceAtlas2 from graphology-library. Its worker is created from a Blob
URL; whether `file://` pages may spawn it is verified per browser (Chrome, Firefox,
Safari) during Phase 2 and recorded in the PR. When `new Worker` throws, or under a
CSP without `worker-src blob:`, the page falls back to synchronous
`forceAtlas2.assign(graph, {iterations})` on the main thread. Seed positions with
`circlepack` grouped by `community_id`. Stop on settle or after a bounded iteration
count; a "re-layout" button restarts. The `isLarge` force tuning does not survive the
move.

**Selection and caps.** `--renderer auto|sigma|d3` (env `CRG_VIZ_RENDERER`). The
choice is serialised into the page as `window.__CRG_RENDERER` because Python cannot
know at generation time whether the browser has WebGL. Caps are decided in Python:
`auto` and `sigma` generate at the Sigma caps (30000 nodes / 90000 edges); `d3`
generates at the D3 canvas caps (8000 / 24000). If `auto` lands in a browser without
WebGL, `D3CanvasRenderer` truncates client-side to the top 8000 nodes by degree and
shows a "N of M rendered" banner. Aggregation still applies above the generation
caps.

**Interaction.** Sigma `nodeReducer` / `edgeReducer` implement hover, search, flow and
community dimming. Labels shown by degree threshold and zoom level.

**Security.** SRI on every script tag; no `eval`; `</` escaping unchanged. In
`--inline-assets` mode the page carries a CSP `<meta>`:
`script-src 'sha384-…'` with one hash per inline block (the SRI pins double as
hashes for byte-identical inlined libraries), `worker-src blob:`,
`style-src 'unsafe-inline'`. The sibling-file mode keeps the `document.write` CDN
fallback and therefore no CSP.

**Tests.** Asset hash tests; `node --check` for `viz-core.js`; existing tests updated:
`script_sources` compared as a set of vendored filenames, a `__VIZ_SCRIPTS__`
sentinel case, the SVG-specific responsive assertions moved to the D3 renderer, symbol
names checked only in the D3 path. Browser tests: Playwright behind a new extra
`browser-test` that is not part of `all` and is not added to `OPTIONAL_GROUPS` in
`tests/test_documentation.py`; a `browser` marker registered in
`[tool.pytest.ini_options]`; tests call `pytest.importorskip("playwright")` so the
main matrix skips them. A CI job `viz-browser` runs
`pip install -e ".[dev,browser-test]" && playwright install --with-deps chromium &&
pytest -m browser` with no coverage gate. The WebGL branch launches Chromium with
`--use-angle=swiftshader --enable-unsafe-swiftshader`; the fallback branch generates
with `--renderer d3`. `--renderer auto` defaults to Sigma only once this job is green.

## 5. Phase 3 — UX pack (medium, one small PR per item)

a. **Focus+context.** Select a node → local graph with a depth slider (1–3);
   double-click expands neighbours.
b. **Path highlight.** Two-symbol picker; `shortestPath` over CALLS / IMPORTS_FROM /
   INHERITS in the page; highlights the path and lists the hops.
c. **Hierarchical collapse.** Directory and community super-nodes as first-class
   objects with a breadcrumb; replaces the "auto-collapse File nodes above 2000
   nodes" heuristic (#132).
d. **Heat overlays.** "Color by: kind | community | churn | risk | criticality".
   `visualize --heat churn,risk,criticality` is opt-in. Churn uses
   `compute_file_churn` (`git log`); risk uses `compute_risk_score`, computed only for
   rendered nodes with batched queries; node criticality is the maximum
   `compute_criticality` over the flows containing the node. Values ride in a `heat`
   payload added by `_slim_for_page`.
e. **Deep links.** URL hash `#node=<qn>&depth=2&mode=full&color=risk`, each value
   `encodeURIComponent`-encoded; MCP tool output builds links the same way.
f. **Minimap** canvas, `+`/`-` zoom, help overlay in both templates.
g. **Aggregated-mode parity.** The aggregated view moves into `viz-core.js` with edge
   toggles, kind shapes, keyboard navigation and a "rendered N of M" counter.

## 6. Phase 4 — Served lazy mode (large)

**Server.** `visualize --serve` becomes a small Starlette app run by uvicorn on
`127.0.0.1:<port>` (default 8765), a second server separate from `serve --http`.
`starlette` and `uvicorn` become declared dependencies (they are installed today
through `fastmcp`). The app reuses `build_http_middleware(host, port)` so
`LoopbackOriginGuard` applies unchanged. The served page needs the server; it is not
the offline file, which stays the default output.

**Endpoints**, all read-only, bounded, parameterised, names through `_sanitize_name`:

- `GET /api/summary` — counts, communities, top-degree nodes
- `GET /api/neighbors?qn=&depth=&kinds=`
- `GET /api/paths?from=&to=&max=`
- `GET /api/search?q=&limit=`
- `GET /api/node?qn=`

The page opens on the aggregated view and fetches neighbourhoods on expand; the path
tool uses `/api/paths`.

**One renderer for VS Code.** `scripts/sync_viz_core.py` copies
`code_review_graph/assets/viz-core.js` into
`code-review-graph-vscode/src/webview/vendor/` and a CI check fails when the copy is
stale. The webview CSP gains `worker-src blob:`. The extension keeps reading
`graph.db` directly (no `/api`), drops its `d3` dependency, and its `maxNodes` slice
becomes top-N by degree with batched edge fetches.

Follow-up, not in this phase: an opt-in `--renderer cosmos` GPU overview.

## 7. Testing and CI

- Phase 1: exporter, dispatch, cap, filter and `--serve` handler tests.
- Phase 2: the optional Playwright job described above.
- Phase 4: endpoint tests against a built fixture graph, including bound enforcement
  and origin rejection.
- Schema untouched; no migration; Action cache key stays at `schema10` from #988.

## 8. Rollout

Phase 1 → 2 → 3 (items in parallel) → 4. Each phase lands behind a flag where it
changes default behaviour, and `CHANGELOG.md` `[Unreleased]` grows per PR. Docs for
each phase ship in the same PR.

## 9. Decisions recorded

- One offline HTML stays the default deliverable. Server mode is additive.
- WebGL via Sigma with automatic D3 canvas fallback; never WebGL-only.
- Layout is computed in the browser, not persisted in `graph.db`.
- The page script lives in a shipped asset file from Phase 2 on.
- Dark theme stays; `prefers-color-scheme` is not planned.
- Playwright is acceptable as an optional CI dependency.
