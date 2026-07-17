# Maintainer reconciliation — 2026-07-17

Status: local and remote CI validation complete; ready for maintainer review.

Base: `main` at `b72413c`  
Integration branch: `codex/reconcile-open-contributions-2026-07-17`  
Tracking issue: `crg-nqi`

## Outcome

This branch is a deliberately narrow reconciliation of independently useful,
evidence-backed fixes. It is not a release branch and it does not merge any
large contribution wholesale. Contributor commits were retained where the
patch was already the strongest implementation; conflict resolutions preserve
current `main` behavior and are called out below.

The audit snapshot covered:

- every local branch, worktree, stash, tracked change, and untracked path;
- all 104 open pull requests, using paginated live data rather than a
  single-page search result;
- all 84 open issues, excluding pull requests;
- all 29 repository discussions; and
- the repository knowledge graph, affected flows, tests, release notes, and
  current CI/review evidence.

No remote issue or source pull request is closed by this branch. Those actions
should happen only after this integration passes review and is merged.

## Preservation and safety record

The primary checkout remains on `main` at `b72413c`, equal to `origin/main`.
Its 31 untracked paths were not moved, cleaned, staged, or rewritten:

- 26 iCloud-suffixed `* 2.*` copies are byte-identical to tracked files; and
- five unique local artifacts remain private to the checkout: `.codex/`, a
  local transcript, `OC3_TECHNICAL_CONTRIBUTION.md`, its PDF, and
  `PRESENTATION_BRIEF.md`.

All three stashes were preserved:

- `stash@{0}` — CI lint/test fixes from merged PRs;
- `stash@{1}` — local `uv.lock` bump; and
- `stash@{2}` — scaling/token-efficiency mypy fixes.

All pre-existing worktrees and branches were preserved, including
`claude/hungry-morse`, `fix/incremental-flow-path-mismatch`,
`release/v2.3.7`, `release/v2.4.0`, `review/local-fixes`, the old workflow
branches, and their untracked worktree files. The reconciliation was performed
only in `.claude/worktrees/codex-reconciliation`.

Important local-branch conclusions:

- `review/local-fixes` is a preservation source, not a merge candidate. Its
  useful TESTED_BY work was extracted. Its incremental path work was disproven
  under the real node-replacement lifecycle; unsafe PID cleanup, over-broad
  ignore rules, raw C++ header sniffing, and scoped-call false positives also
  remain excluded.
- `release/v2.4.0` is the head of PR #559. Its token-budget, doctor, eval, and
  installer surfaces remain coupled and have correctness/supply-chain
  blockers. Four patch-equivalent commits were selected: the three-commit
  TESTED_BY series and the independent Action path-rendering fix.
- `release/v2.3.7` is PR #559 plus a version downgrade and must not be merged or
  pushed as a release candidate.
- `issue-194-specific-exception-logging` is patch-equivalent to work already on
  `main`; the multi-word search branches are superseded or need decomposition.

## Selected integration

| Branch commit(s) | Source | Decision and evidence |
| --- | --- | --- |
| `34c5d00` | PR #564 | Use `#graph-svg` instead of a page-wide `svg` selector in both templates; carries focused regressions. |
| `d8e5453` | PR #565 | Remove machine-specific hook paths, add a PATH guard, and avoid applying Bash hooks to unrelated tools. |
| `cbf9355` | PR #573 | Resolve PHP `use`, grouped imports, aliases, functions, and constants to local files; preserves contributor attribution. |
| `ddc8544`, `918ef13`, `580205d` | exact patch-equivalents of PR #559 commits `278e400`, `03e319e`, `a11dc04` | Correct TESTED_BY direction at every selected consumer, update dead-code analysis, and add a parser-to-store-to-query regression. This incorporates the #527 work and supersedes overlapping PR #598. |
| `6ece151` | PR #559 commit | Render repository-relative paths in Action comments without taking the rest of the release branch. |
| `771307e` | issue #612 | Capture bare, member, chained, and null-conditional C# receiver calls with correct caller attribution; implemented red/green with focused tests. |
| `eeef686` | issue #613 | Keep packaged documentation fallback available through the real MCP wrapper; implemented red/green with an installed-layout regression. |
| `571f665` | content-equivalent/rebased port of PR #578 | Replace regex JSONC stripping with a string-aware scanner so URLs and comment-like string contents survive; import-neighborhood context differs from the source patch. |
| `df87b60`, `e5f563b` | PR #563 | Generate the uppercase `SKILL.md` filename required by the [Claude Code skills documentation](https://code.claude.com/docs/en/skills) and update regressions. This does not adopt PR #562's unnecessary lowercasing of the display name. |
| `90408c9` | PR #354 | Refuse and preserve valid top-level arrays/scalars, while treating empty/comment-only configs as fresh objects. Conflict resolution retained PR #578's stronger string-aware JSONC parser. Production fixes for #312/#350 were intentionally omitted because they are already on `main`; their regressions remain. |
| `0abd789` | PR #353 | Persist Kotlin/C# annotations using the established metadata shape and resolve C# namespace importers. This does not claim to solve the remaining impact-radius design in #310. |
| `b9ec19d` | PR #393 | Repair advertised Zig parsing and add structure/call/import/test fixtures. Conflict resolution retained the newer Nix implementation on `main`. |
| `fc549ae` | reconciliation review fix | Preserve an existing platform config byte-for-byte when its nested server collection has the wrong array/object type; red/green coverage exercises both schemas. |
| `d611a2d` | reconciliation review fix | Generate TESTED_BY for in-source Zig tests regardless of filename and carry effective parent names through nested C# namespaces; both gaps were reproduced before implementation. |
| `c7d7211` | reconciliation review fix | Replace recursive C# namespace discovery with an explicit stack; a 1,200-level AST regression failed before the change and now passes without truncating namespace metadata. |

The final diff size and repository-wide validation results are recorded below.

## Pull-request inventory and dispositions

Live pagination returned 104 open PRs: 100 on page 1 and four on page 2. All
target `main`; 102 are non-draft, while #582 and #618 are drafts. Each open PR
appears exactly once in the routing inventory below.

- Selected-area or directly overlapping work (24): #621, #618, #611, #601,
  #598, #586, #583, #582, #578, #573, #572, #568, #566, #565, #564, #562,
  #559, #538, #530, #527, #477, #354, #353, #92.
- Parser/language work (29): #614, #602, #591, #590, #589, #580, #577, #560,
  #539, #526, #522, #517, #516, #514, #462, #459, #393, #415, #339, #338,
  #337, #333, #332, #331, #330, #329, #328, #252, #95.
- Graph/search/performance/product work (25): #615, #606, #605, #604, #603,
  #600, #599, #581, #555, #552, #536, #509, #468, #460, #458, #457, #452,
  #394, #341, #340, #336, #335, #334, #327, #326.
- Platform/install/CI/dependency/docs work (26): #617, #597, #596, #595, #584,
  #563, #557, #556, #554, #548, #547, #546, #545, #544, #543, #542, #540,
  #531, #505, #495, #491, #453, #449, #373, #347, #129.

The routing groups are not blanket approvals. Material non-selection decisions:

- PR #559 is not safe to merge wholesale. Its advertised hard token cap only
  constrains snippets: a 44-file run with source disabled and a nominal 6,000
  token limit still returned roughly 1.67 million characters. The lean default
  hides tools that its own prompts and recovery text require. Eval can reuse
  stale results after ignored failures; doctor can report false health and
  mutate the database; installers execute floating network content. Separate
  Beads issues `crg-1nx` and `crg-4ys` track the redesign.
- PR #601's bare endpoint resolver is complementary to the TESTED_BY direction
  fix, but it activates global unique-name resolution without import evidence
  and materially changes graph communities. It needs precision and performance
  evaluation before adoption.
- PR #568 and the related local scoped resolver can manufacture global
  `Class.method` edges from uniqueness alone and add full-scan work. They remain
  excluded pending scoped identity semantics.
- PR #586 prevents row loss but still binds ambiguous overload calls to the
  first definition. Stable symbol identity is tracked in `crg-lw5`.
- PR #611 plausibly avoids an embedding import race but adds about seven seconds
  of eager startup latency. It needs concurrency coverage and an explicit
  latency decision.
- Draft PR #618 is stronger than #566 for Git paths because it uses NUL-delimited
  bytes and `os.fsdecode`; it remains separate until its draft/CI state and
  overlap with branch/tracked-output behavior are resolved.
- PR #621 is the focused Windows Codex-hook candidate, but target-native command
  execution was not covered by this branch's Linux CI. Its contributor-authored
  patch was removed from this integration and moved to dedicated
  [draft PR #626](https://github.com/tirth8205/code-review-graph/pull/626) for
  Windows testing.
- PRs #595, #597, and #596 form a promising Windows daemon sequence, but they
  require genuine Windows execution and should not be hidden inside this
  cross-platform reconciliation.
- PR #615 contains a credible small inherited-file-descriptor fix but no
  regression. Reproduce the zombie-process failure and add one first.
- PR #477 contains useful second-template visualization work but emits a literal
  escaped quote in generated JavaScript. PR #564 is the safe subset; remaining
  behavior needs browser/`node --check` coverage.
- PRs #457 and #552 have the same head and an under-specified three-second cache
  key. PRs #458 and #460 are stale/unmergeable token alternatives; #604 adds a
  broad provenance surface; #536 adds a large optional DSL. These need isolated
  product/API review.
- PRs #326–#341 are a cumulative stale stack whose tip includes large obsolete
  deletions. Broad parser/framework PRs, platform integrations, dependencies,
  translations, and product features remain independent review units rather
  than being bundled here.
- PRs #556/#557 address fork-PR comments but need a clean port, explicit
  `actions: read` and `issues: write` permissions, actionlint, and fork security
  verification; #557's raw head also contains unrelated parser/package-lock
  changes.
- PR #491's uninstall design can delete user-owned Cursor scripts, misses Gemini
  MCP state, parses JSONC unsafely, and duplicates platform inventories.
- PR #459 may spawn a parser-probe subprocess per file. PR #394 is optional
  defense in depth because the supported FastMCP version already threadpools
  synchronous handlers.

CI evidence is sparse: only PR #559 had both successful CI and PR Review runs at
the audit snapshot. Many fork workflows show `action_required`, which is neither
a pass nor a failure. Contributor-reported results were treated as supporting
evidence, never as a substitute for validation of this combined branch.

## Open-issue inventory

All 84 open issues were read and classified exactly once:

- Confirmed/actionable (21): #623, #622, #620, #619, #616, #613, #612, #610,
  #609, #585, #579, #576, #500, #475, #473, #461, #343, #310, #291, #173,
  #63.
- Local/release partial or fixed (18): #574, #569, #567, #561, #558, #553,
  #551, #550, #549, #537, #534, #523, #515, #497, #463, #450, #419, #295.
- Already solved on `main` or release-pending (10): #524, #471, #243, #218,
  #212, #190, #132, #91, #87, #83.
- Support/retest (5): #474, #314, #262, #209, #189.
- Feature backlog (25): #607, #593, #592, #588, #587, #521, #518, #504,
  #482, #478, #436, #434, #430, #429, #369, #348, #346, #320, #311, #305,
  #269, #265, #232, #210, #199.
- Insufficient evidence/discussion (5): #535, #532, #506, #492, #426.

Selected patches address or materially advance #523 (visualization), #549 and
#558 (portable hooks), #574 (PHP imports), #515 (TESTED_BY via the #559 subset),
#553 (JSONC), #612, #613, and #295. PR #353 advances only the namespace
importer portion of #310; its impact-radius/detect-changes BFS remains open.
Issues #561 and #567 remain unaddressed because PRs #562 and #568 are absent.
Issue #622's collision/overload problem is intentionally deferred because the
open patch is not a complete identity model. Issues #619, #616, and #610 need
separate API, platform-discovery, and startup-latency decisions respectively.

Issue #569 remains open. The audited local/PR #572 variants normalize paths
after incremental reparsing has already replaced node IDs; existing flow and
community memberships therefore reference deleted nodes and modified files can
still be skipped. A lifecycle-aware fix needs a regression that changes graph
topology, not only a path-format fixture.

## Discussion inventory

All 29 discussions were enumerated and read. The threads with direct engineering
implications are #501, #464, #137, #376, #355, #414, #410, #318, #467, #479,
and #405:

- #467 is real evidence for a lean tool surface, but does not validate PR #559's
  current cap implementation.
- #318 and #410 support trustworthy status/doctor UX, while strengthening the
  requirement that diagnostics be non-mutating and fail honestly.
- #501 supports portable PowerShell/Codex hooks and informed the dedicated
  validation path for PR #621; #405 shows the hook contract still needs clearer
  documentation.
- #464 and #137 reinforce worktree/monorepo-safe path and registry behavior.
- #376 and #355 reinforce explicit inclusion/exclusion semantics; they do not
  justify hiding source paths with broad ignore patterns.
- #414 informs scalability claims, and #479 supports fixing both visualization
  templates rather than only the first page shape.

The remaining support, setup, product, or announcement discussions were #525,
#411, #105, #375, #109, #254, #206, #111, #131, #89, #186, #178, #134, #113,
#101, #96, #85, and #84. They provide documentation/backlog context but no
additional change was safe to couple into this branch.

## Follow-up tracking and merge sequence

The audit created focused Beads issues rather than hiding unresolved work in a
large branch:

- `crg-1nx` — redesign v2.4 token budgeting and the lean tool surface;
- `crg-4ys` — split/harden doctor, eval, and installers;
- `crg-ys0` — remove unsafe blockers from `review/local-fixes`;
- `crg-lw5` — design stable symbol identity for collisions/overloads;
- `crg-o1d` — repair #569 across incremental node-ID replacement;
- `crg-dtv` — close SQLite connections exposed by coverage warnings;
- `crg-8u4` — exclude maintainer-only `.beads` hooks from the sdist; and
- existing platform/performance issues remain the owners for Windows daemon,
  HOME isolation, and daemon-stop behavior.

Recommended review order:

1. path/edge semantics and their end-to-end tests;
2. parser changes by language (PHP, C#, Kotlin, Zig);
3. skills/config/hook compatibility and Windows CI;
4. visualization, Action rendering, and packaged docs;
5. release-note accuracy and potential source-PR/issue closure only after merge.

## Validation record

Completed on the assembled branch:

- final Python 3.13 suite excluding the known native WatchDaemon failure:
  `1,447 passed`, `13 deselected`, `2 xpassed`;
- isolated CI-equivalent coverage run: `1,446 passed`, `1 skipped`,
  `13 deselected`, `2 xpassed`; coverage `72.95%` against a `65%` threshold;
- combined skills/multilingual regression run: `501 passed`, plus the final
  C# namespace regression class: `6 passed`;
- ruff: clean; mypy: no issues in 62 source files; Bandit: no issues;
- Python and VS Code schema versions both `9`;
- wheel and sdist built successfully, and both contain the packaged
  `LLM-OPTIMIZED-REFERENCE.md` required by the docs fallback;
- full knowledge-graph rebuild: 181 parsed files, 3,415 nodes, 24,940 edges,
  200 flows, 16 communities, and no build errors;
- graph review: 25 changed files, risk score `0.65`, 26 affected flows; the
  parser breadth is the main blast radius and received two independent review
  passes plus focused language regressions; and
- draft PR #624: lint, mypy, Bandit, schema sync, PR Review, GitGuardian, and
  the Python 3.10, 3.11, 3.12, and 3.13 test jobs all passed; and
- `git diff --check`: clean.

The independent reviews first found five P1/P2 gaps: the two #569 lifecycle
defects were removed, while the nested-config, Zig TESTED_BY, and nested-C#
cases were fixed red/green. A second pass found the deep C# recursion failure;
that too was fixed red/green. No other P1/P2 finding remained.

The macOS Python 3.13 baseline has a native watchdog/FSEvents `SIGBUS` in
`TestWatchDaemon` on both `main` and the integration branch. It is tracked in
`crg-229` and is excluded from broad comparison runs; it must not be represented
as a regression introduced here.

The isolated coverage run emits 29 `ResourceWarning`s for unclosed SQLite
connections; `crg-dtv` tracks turning those warnings into deterministic closes.
Package inspection also found pre-existing maintainer-only `.beads` hooks in the
sdist; `crg-8u4` tracks the manifest policy fix. Neither is hidden as a passing
claim.

The Windows hook patch from PR #621 is intentionally absent from this
integration. It remains in
[draft PR #626](https://github.com/tirth8205/code-review-graph/pull/626) until
target-native CI and maintainer review verify command execution, stdin draining,
failure behavior, and upgrades from existing Unix-only hook entries.
