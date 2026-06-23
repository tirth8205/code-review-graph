# Improve Review Metrics And Analysis Output — Deliverable Specification

**Worktree / branch:** `mw-improve-metrics-analyis-zazz`
**Feature:** Code Review Graph token-efficient Zazz review context
**Milestone:** N/A
**Deliverable:** Implement P1, P2, P3, P4, P5, and P7 from the fork storage-analysis recommendations
**Delivery topology:** single-deliverable branch
**Review artifact:** one PR for this specification
**Approved review shape:** one PR
**Decomposition rationale:** The selected improvements share one review-output contract: produce compact, deterministic, scoped, lower-noise review context for Zazz agents. Keeping them together lets reviewers assess the CLI/MCP payload shape end to end. P6 sibling-template divergence is intentionally excluded because it requires new parse-time/query capability and belongs in a later quality-focused deliverable.
**Integration branch:** `main`
**Merge policy:** PR review required; agents commit/push feature branches only
**Drafted:** 2026-06-23
**Shared run log:** `.zazz/execution/mw-improve-metrics-analysis-zazz-run-log.md`

---

## Introductory Discussion

`code-review-graph` is valuable because it turns a repository into a local, persistent structural graph that AI review tools can query instead of repeatedly reading broad file sets. The product documentation positions CRG as local-first MCP/CLI code intelligence: it indexes files, functions, classes, tests, imports, calls, inheritance, flows, communities, and risk signals into a SQLite-backed graph, then exposes targeted review context through commands and MCP tools. In practice, that means a reviewer can ask "what changed, what depends on it, what flows are affected, and where are test gaps?" before spending tokens on source files.

This matters even more in the Zazz methodology. Zazz work is expected to have explicit standards, feature context, architecture notes, deliverable specifications, acceptance criteria, and review boundaries. Those documents give graph analysis a richer interpretive frame: the graph can identify structure and blast radius, while Zazz standards/specifications tell the reviewer what the change is supposed to accomplish and which rules govern the touched paths. The desired outcome is not a graph that replaces human or standards-based review; it is a graph that routes agents to the right evidence faster and makes the Standards and Spec review axes cheaper to execute.

The current fork-improvement objective is therefore performance and efficiency at the review-output layer. Storage is already SQLite and queryable; the waste appears when absolute paths, large JSON payloads, unscoped summaries, noisy test gaps, and ambiguous savings panels are handed to multiple section agents. This specification improves the output contract so CRG can serve Zazz review more cleanly: portable paths, deterministic compact payloads, one-shot review context, section-scoped analysis, configurable test-gap suppression, and honest measurement of change-analysis savings.

A companion objective is to design a dedicated `code-review-graph` skill that the Zazz `pr-review` skill can invoke before and during review. That skill should help agents use graph evidence the way a principal engineer familiar with the whole codebase would: compare changed code against local architecture, sibling implementations, indexed standards, current specifications, and known drift before writing findings. The goal is to reduce generic LLM "best practice" reviews and instead surface standards violations, codebase-specific invariants, cross-family consolidation opportunities, generated-code/source-of-truth drift, and broader improvement candidates with concrete source evidence.

The skill should also encode a language-agnostic engineering bias toward removing redundant and repeated code, reducing lines of code, and consolidating duplicate behavior when doing so does not degrade structure, explicitness, readability, debuggability, or testability for either humans or agents. This principle must apply across CRG-supported ecosystems, including Python, JavaScript, TypeScript, Go, and database code such as Microsoft SQL Server T-SQL, PostgreSQL PL/pgSQL, and Oracle PL/SQL. The skill should avoid style-only compression, clever abstractions, and "fewer lines at any cost"; consolidation is valuable only when it makes the codebase easier to reason about and review.

The skill should explicitly look for agent slop: boilerplate comments, defensive code that cannot execute, redundant branches, generic abstractions not used by the codebase, cargo-culted error handling, renamed duplication, over-mocked tests, tests that assert implementation details without protecting behavior, and shallow tests that only exercise constructors, mocks, snapshots, or happy-path plumbing. The authoritative policy language should come from the repo's indexed standards when present, especially code-hygiene/code-structure and test-quality standards such as `.zazz/standards/code-structure.md`, `.zazz/standards/python-testing.md`, and `.zazz/standards/database-testing.md` in this baseline. CRG can help by surfacing graph and test-relationship signals, but the companion skill must make the quality judgment from source evidence and applicable standards.

This is the skill's "engineering taste" layer: standards provide the rules, CRG provides the evidence map, and the skill applies disciplined judgment about clarity, restraint, maintainability, and codebase fit. That taste must be explicit enough to prevent generic LLM preferences from overriding local standards, but flexible enough to recognize when the better review outcome is simplification, consolidation, or deleting low-value code/tests rather than adding more machinery.

---

## 0. Capability

This deliverable makes CRG's review-analysis output cheaper, more deterministic, and more directly useful for Zazz-style multi-agent review. It normalizes emitted paths to repository-relative form, adds compact budgeted review payloads, adds a one-shot `review-context` entrypoint, supports path-scoped section analysis, suppresses configured low-signal test gaps, and makes token-savings reporting scope-honest and machine-readable. It does not implement P6 sibling-template divergence.

The companion design objective is review finding quality. CRG should not merely hand agents fewer tokens; it should route Zazz `pr-review` agents toward the right standards, source lines, graph neighborhoods, and review topology. The current implementation slice remains focused on the review-output contract above, but implementors MUST preserve extension points for the review-quality capabilities in §2.a and MUST NOT make design choices that assume review context is limited to PR files only.

For overnight implementation, the scope boundary is explicit: implement only the P1/P2/P3/P4/P5/P7 review-output contract verified by AC1-AC10. The review-quality material in §2.a is binding as design guidance for output shape and naming, but it is not permission to implement standards-index packets, topology planning, read-first planning, standards drift detection, consolidation detection, low-value-test scoring, a new Zazz skill, or P6 sibling-divergence features in this PR.

---

## 1. Required Reading For The Implementor

Read these before opening an editor. Use section-pinned context; do not load every document in this repo.

### 1.a This Specification

Read this specification end to end first.

### 1.b Feature / Milestone Context

- `../proposals/code-review-graph-token-efficiency-and-turso-engine.md` — read sections 1, 2, 4, 5, 8, 10, 11, and Appendix source references for the token-efficiency phase context.
- `../../docs/FEATURES.md` — read `v2.3.6`, `v2.3.5`, `v2.3.4`, and `Privacy & Data`.
- `../../docs/USAGE.md` — read `Core Workflow`, `Context Savings`, `What Gets Indexed`, and `Ignore Patterns`.
- `../../docs/COMMANDS.md` — read `Core Tools`, `Flow Tools`, and `Community Tools`.
- `../../docs/architecture.md` — read `System Overview`, `Data Flow`, `Storage`, `Visualization`, and `Impact Analysis Algorithm`.
- Public feature page `https://code-review-graph.com/features` — use only as positioning context: local-first MCP/CLI code intelligence, 30-tool MCP surface, risk-scored review, token reduction, graph visualization, and optional embeddings.

### 1.c Prior Specifications In This Delivery Effort

N/A. This branch contains one deliverable specification.

### 1.d Standards

Per `.zazz/standards/index.yaml`, the following standards apply to this specification's likely scope:

| Standard | What it governs here |
| --- | --- |
| `.zazz/standards/docs-hygiene.md` | Markdown guidance, links, and durable execution docs. |
| `.zazz/standards/spec-hygiene.md` | Specification quality, testable ACs, proportional test plans, decisions, scope, and path portability. |
| `.zazz/standards/pr-process.md` | One logical PR and human review boundary. |

**Verification step before writing code:** run the standards lookup yourself against the §3 file list. If an applicable standard is missing from this table, stop and surface it to the Owner before proceeding.

### 1.e Existing Code References

- `code_review_graph/changes.py` — `analyze_changes`, risk scoring, affected flows, test-gap construction, and priority ordering.
- `code_review_graph/tools/review.py` — MCP-facing `get_review_context`, `get_affected_flows_func`, and `detect_changes_func`.
- `code_review_graph/cli.py` — `detect-changes`, `update --brief`, and `visualize` command patterns.
- `code_review_graph/context_savings.py` — token estimation, `context_savings`, and current CLI panel formatting.
- `code_review_graph/graph.py` — `GraphNode`, `GraphEdge`, `node_to_dict`, `edge_to_dict`, and absolute stored paths.
- `code_review_graph/incremental.py` — `.code-review-graphignore` loading and ignore glob matching patterns to mirror for scoped filters and suppression config.
- `code_review_graph/visualization.py` — graph export/HTML generation context for manual validation.
- `tests/test_changes.py`, `tests/test_cli.py`, `tests/test_tools.py`, `tests/test_context_savings.py`, and `tests/test_main.py` — test style and existing regression coverage around change analysis and context savings.

### 1.f Project Orientation

- `AGENTS.md` — worktree discipline, integration branch, Worktrunk usage, and human review boundary.
- `.zazz/agent-execution-discipline.md` — scope discipline, execution records, verification discipline, and halt conditions.
- `.zazz/code-review-graph.md` — Zazz interpretation rules for CRG as advisory graph context.
- `.zazz/execution/crg-approach-critical-review.md` — strict critique of this approach against the Owner-supplied PR #214 with/without-CRG review artifacts, including additional future improvement candidates.
- `.zazz/execution/mw-improve-metrics-analysis-zazz-spec-review-handoff.md` — completed spec-review handoff and rationale for the final overnight-ready shape.
- `.zazz/execution/mw-improve-metrics-analysis-zazz-run-log.md` — append-only execution log; read it before implementation and update it as work proceeds.

### 1.g Zazz `pr-review` Context

Read these only for review-shape and standards-selection context. Do not modify the `pr-review` skill in this deliverable.

- `.agents/skills/pr-review/SKILL.md` — Standards/Spec axis split, diff pinning, and graph-context gate.
- `.agents/skills/pr-review/code-review-graph.md` — current advisory CRG usage model for Zazz review.
- `.agents/skills/pr-review/shared-rules.md` — diff scope discipline and finding evidence requirements.
- `.agents/skills/pr-review/standards-axis.md` — standards-index selection, agentic slop, test value, and redundant-computation review guidance.
- `.agents/skills/pr-review/spec-axis.md` — spec-compliance axis and scope-drift handling.

---

## 2. Invariants

### INVARIANT 1 — Storage Remains SQLite-Derived And Local-First

This deliverable changes review-output projection and orchestration only. It MUST NOT replace the SQLite `GraphStore`, alter the schema/migration model, or require network access for core graph/review workflows.

### INVARIANT 2 — Stored Paths Stay Compatible; Emitted Review Paths Become Portable

The graph may continue storing absolute paths for compatibility with existing lookup behavior. Review-facing payloads MUST emit repository-relative paths wherever the consumer is expected to read, compare, cache, or paste the output.

### INVARIANT 3 — Compact Output Must Remain Review-Useful

Budgeted review output MUST include enough information for a section-scoped agent to decide what to read next: summary, risk score, changed-file count, top review priorities with repo-relative `file:line`, test gaps after suppression, affected-flow names/criticality, truncation metadata, and token/savings metadata. It MUST NOT collapse back to name-only priority lists.

### INVARIANT 4 — P6 Is Not In This Deliverable

Sibling-template divergence, nearest-sibling search, construct-level sibling diffs, and parse-time metadata for sibling divergence are out of scope. They MUST NOT be implemented or partially scaffolded in this PR.

### INVARIANT 5 — Token Efficiency Is Not Finding-Quality Proof

Passing this specification proves the review-output contract is smaller, more portable, more scoped, and more measurable. It does not prove graph-assisted review finds every critical defect. Finding-quality evaluation, standards-index context packets, sibling-invariant delta signals, and standards-drift detection are follow-up capabilities unless explicitly added through a signed-off specification revision.

## 2.a Review-Finding Quality Guidance For Zazz `pr-review`

This section is guidance for this deliverable's output shape, future CRG features, and companion Zazz skills. It is intentionally detailed because CRG is most valuable in Zazz when it helps a `pr-review` agent combine graph structure with the authoritative standards/specification context already required by the methodology.

The target packets below are future product or skill contracts unless an AC in §6 explicitly names them. The current implementation may leave internal seams that would make these packets easier later, but it MUST NOT add user-visible packet sections for `standards_context`, `standards_drift`, `review_topology`, `read_first`, `external_comparison_hints`, or `consolidation_candidates` in this PR unless the Owner revises this specification.

### Graph Scope And PR Scope

CRG's code graph should be understood as a graph of the indexed worktree, not merely the PR files. The PR or diff supplies seed nodes: changed files, changed functions, risk hotspots, and review priorities. The graph can then answer questions about unchanged callers, callees, sibling implementations, test relationships, communities, registries, and generated-code/source-of-truth relationships when those files are indexed.

This distinction matters for review:

- **Blocking PR findings stay diff-scoped.** A `pr-review` finding should normally target changed code or changed tests. Unchanged code is context and evidence unless the specification explicitly includes broader cleanup.
- **Graph evidence may be whole-worktree.** A changed report, stored procedure, route, or generated client can be compared against unchanged peers to identify family invariants, blast radius, and consolidation candidates.
- **Scoped packets need external hints.** `--scope` should reduce the main payload, but the compact output should be able to carry capped external comparison hints so a section agent does not lose critical sibling or standards context.

### Standards-Index Context Packets

Zazz review should use the standards index as the authority boundary. CRG and companion skills should prefer `docs/standards/index.yaml` or the repo's configured standards index over globbing `docs/standards/*`.

Target packet:

```json
{
  "standards_context": [
    {
      "standard": "docs/standards/database-sproc-errors.md",
      "matched_paths": ["backend/database/sql_migrations/stored-procedure/..."],
      "matched_activities": ["modifying stored procedure error handling"],
      "sections": ["Return-code initialization", "Error propagation"],
      "why": "Changed SQL procedure participates in the indexed sproc error-handling rule."
    }
  ],
  "excluded_non_indexed_standards": [
    {
      "path": "docs/standards/service-layer-guide.md",
      "why": "Present in docs tree but not authoritative in standards index."
    }
  ]
}
```

Required behavior for future implementation:

- Match changed paths and detected activities to indexed standards.
- Return the smallest useful standards sections, not entire standards files, when section anchors are available.
- Explicitly name non-indexed standards that look tempting but are not authoritative.
- Provide separate packets for the Standards axis and Spec axis when their evidence needs differ.
- Treat missing or malformed standards-index data as a review setup warning, not as permission to glob all standards.

### Standards-Vs-Codebase Drift Detection

CRG can improve finding quality by distinguishing a PR defect from a broader standards/codebase drift. This is especially important when a written standard conflicts with the majority implementation pattern.

Target drift output:

```json
{
  "standards_drift": [
    {
      "standard": "docs/standards/api-responses.md",
      "rule": "GET empty collection returns 204",
      "codebase_pattern": "Most sibling endpoints return 200 with an empty data array.",
      "evidence_count": {"matching_standard": 1, "matching_codebase_pattern": 12},
      "affected_changed_files": ["api/reports/monthly-return-addresses.ts"],
      "review_guidance": "Do not block this PR solely for the drift unless the deliverable explicitly changes the response contract; file or reference a standards cleanup item."
    }
  ]
}
```

Rules:

- Drift observations are normally team/process notes, not blockers.
- Drift must not silence concrete changed-code defects. If changed code uniquely violates a safety invariant, the finding remains actionable even when siblings have related drift.
- Drift should cite both the governing standard and codebase evidence.
- Drift should help severity calibration: "local PR defect", "preexisting family issue", "standard needs update", or "spec requires intentional divergence".

### Review-Topology Planning

The PR #214 comparison showed that token savings can be dominated by review topology: number of sub-agents, duplicated cache reads, and breadth of each axis. CRG should help plan topology rather than leaving it to ad hoc orchestration.

Target planner output:

```json
{
  "review_topology": {
    "recommended_subagents": 2,
    "axes": [
      {
        "name": "standards-axis",
        "scope_globs": ["backend/database/**", "backend/api/**"],
        "read_first_packet": "standards-axis"
      },
      {
        "name": "spec-axis",
        "scope_globs": ["backend/api/**", "docs/specifications/**"],
        "read_first_packet": "spec-axis"
      }
    ],
    "why_not_split_further": [
      "Changed files are in one graph community.",
      "Applicable standards overlap heavily across files."
    ],
    "budget_notes": {
      "shared_packet_tokens": 900,
      "per_axis_packet_tokens": 700
    }
  }
}
```

Planner inputs should include changed-file count, graph communities, risk hotspots, applicable standards, deliverable/spec paths, generated-code markers, and known review axes. The planner should optimize for review quality first, then token efficiency: use fewer agents when context overlaps heavily, split when standards/spec responsibilities are materially different, and explain the tradeoff.

### Read-First Plans

Compact review output should move from "interesting graph facts" toward "read this evidence first." A read-first plan combines graph output, exact source lines, applicable standards, tests, and external comparison hints.

Target packet:

```json
{
  "read_first": [
    {
      "path": "backend/database/sql_migrations/stored-procedure/R__dbo.qb2_GetAllReturnAddresses.sql",
      "lines": "20-35",
      "why": "Changed procedure participates in return-code safety invariant.",
      "axis": ["standards-axis", "spec-axis"]
    },
    {
      "path": "docs/standards/database-sproc-errors.md",
      "section": "Return-code initialization",
      "why": "Authoritative indexed standard for changed procedure."
    }
  ],
  "external_comparison_hints": [
    {
      "path": "backend/database/sql_migrations/stored-procedure/R__dbo.qb2_GetAllCustomerAddresses.sql",
      "why": "Sibling GetAll procedure initializes @ReturnCode before conditional error lookup."
    }
  ]
}
```

Rules:

- Include enough line-level evidence for the agent to verify before writing a finding.
- Prefer source lines and indexed standards over summaries.
- Keep shared read-first items separate from axis-specific items so multiple sub-agents do not duplicate broad reading.
- Never ask an agent to make a finding from graph facts alone; graph facts route reading.

### Generated-Code And Cross-Report Consolidation Guidance

The graph can help detect consolidation opportunities outside the immediate PR file when it indexes the full worktree and the relevant generated or source-of-truth files are included. Tree-sitter structure plus graph relationships can identify repeated functions/classes, report families, route/client pairs, registries, OpenAPI operation families, generated API clients, stored-procedure/data-wrapper pairs, and report/plugin templates. The consolidation lens is language-agnostic: Python, JavaScript, TypeScript, Go, T-SQL, PL/pgSQL, PL/SQL, and other supported languages should be reviewed for repeated behavior using language-appropriate evidence rather than one ecosystem's idioms.

Useful signals:

- changed file belongs to a graph community with many near-identical report implementations
- several reports call the same API or wrapper with duplicated adapter code
- generated clients and hand-written wrappers drift from the same OpenAPI/schema source
- a new API shape could remove repeated per-report conditionals, mapping logic, or response normalization
- tests duplicate the same setup/assertions across a report family
- stored procedures repeat error handling, return-code initialization, transaction handling, temp-table shaping, cursor loops, or result-set mapping that could be safely centralized or standardized
- language-specific boilerplate repeats across services, handlers, DTOs, repositories, migrations, or report definitions without a readability or isolation benefit

Review handling:

- If the deliverable is a single report fix, cross-report consolidation is usually a nonblocking improvement note.
- If the PR introduces a new duplicated pattern where a shared API/helper would be simpler, the finding may target the changed code.
- If the specification's objective includes reducing report-family complexity, CRG should broaden analysis to the whole report family and emit consolidation candidates with evidence.
- Generated output should not be the main consolidation target when the generator/template/source schema is available. Prefer findings against the source of truth.
- If generated files are ignored or excluded, CRG should say so and avoid pretending it analyzed them. Future synthetic edges can link source schemas to generated artifacts without indexing every generated line.
- Reducing lines of code is a positive signal only when the proposed consolidation preserves clear boundaries, obvious data flow, meaningful names, readable tests, and direct failure diagnosis.
- The reviewer should reject consolidation advice that turns simple local code into an opaque generic framework, hides important domain differences, or makes agent review harder.

### Agent Slop And Low-Value Test Guidance

CRG can support slop detection, but it should not claim certainty from graph shape alone. The graph should produce evidence packets that help a Zazz `pr-review` agent inspect suspicious code and tests quickly. The companion skill should load the applicable indexed standards first and use their language for findings; CRG provides targeting evidence, standards provide the rule and severity frame, and engineering taste supplies the final judgment about whether the code is actually clearer, smaller, safer, and easier to maintain.

Authoritative standard sources in this baseline include:

- `.zazz/standards/code-structure.md` for anti-slop structure, module cohesion, duplicated logic, and redundant runtime work
- `.zazz/standards/python-testing.md` for behavior-focused Python test quality and test consolidation
- `.zazz/standards/database-testing.md` for tSQLt behavior commentary, naming, and meaningful database test expectations
- stack-specific standards matched through `.zazz/standards/index.yaml` for frontend, HTTP, service, data-layer, report, or database review

Useful graph-assisted signals:

- new functions/classes with no callers, no tests, or no durable role in a graph community
- changed tests that only touch mocks/stubs and do not connect to changed production nodes
- tests that cover only generated accessors, constructors, snapshots, or trivial wrappers
- repeated test bodies with renamed identifiers and no distinct behavior coverage
- changed production branches that have no corresponding test edge or read-first test suggestion
- wrappers/helpers introduced for a single call site without reducing complexity, duplication, or policy drift
- comments that restate code, generic TODOs, or broad defensive cases unsupported by callers or standards
- exception/error-handling paths copied from unrelated siblings without matching the local contract

Review handling:

- Treat these as evidence prompts, not automatic findings.
- A slop finding should cite changed code, the expected behavior or standard, and why the extra code/test does not improve maintainability or confidence.
- Useless-test findings should explain the behavior not protected, not merely say "this test is shallow."
- If a test is intentionally narrow because higher-level coverage exists elsewhere, CRG should help locate that higher-level coverage before the reviewer writes a finding.
- Prefer deleting, simplifying, or moving low-value tests over keeping tests that create false confidence or brittle maintenance cost.
- Apply this across languages and SQL dialects; stored procedure tests, migration tests, API contract tests, and generated-client tests can all be noisy or low-value when they do not protect observable behavior.

Target consolidation packet:

```json
{
  "consolidation_candidates": [
    {
      "family": "return-address reports",
      "changed_files": ["reports/return-address-summary.ts"],
      "related_files": [
        "reports/customer-address-summary.ts",
        "reports/vendor-address-summary.ts"
      ],
      "pattern": "Repeated response normalization after equivalent API calls.",
      "suggestion": "Consider a shared report data adapter or API response helper if this deliverable owns family-wide cleanup.",
      "review_severity": "nonblocking_unless_in_scope"
    }
  ]
}
```

---

## 3. Scope

### Approved Review Shape

This specification is approved for one PR. If implementation surfaces a need to split, stack, combine, or treat the work as a large exception, stop and revise the specification with Owner sign-off before continuing.

**Rationale.** P1, P2, P3, P4, P5, and P7 converge on one user-visible review-context contract. Splitting them would make reviewers chase partial payload shapes across PRs. Stacking is not warranted because no lower-layer public API needs separate landing before the upper layer; the implementation can remain one coherent CLI/MCP output slice. P6 is excluded because it would materially change parser/query capability and review risk.

**Review unit owned by this specification.**

- `mw-improve-metrics-analysis-zazz` — one implementation PR that updates CRG review-output generation and its tests.

### Strict Scope Constraint

Every product-code modification in this specification lives under `code_review_graph/`, `tests/`, `docs/`, and `.zazz/`. If implementation surfaces a need to modify packaging, workflows, VS Code extension files, parser internals for P6-like metadata, or external repos, stop and surface to the Owner.

The only intended `.agents/` interaction is required reading in §1.g. Do not edit `.agents/skills/pr-review/` in this deliverable; the companion `code-review-graph` skill is a follow-on specification candidate, not current product work.

### In Scope

| Path | New / Modified | Reason |
| --- | --- | --- |
| `code_review_graph/review_projection.py` | New | Central projection helpers for repo-relative path normalization, deterministic sorting, compact budgeted review payloads, and path-scope filtering. |
| `code_review_graph/test_gap_config.py` | New | Load and apply test-gap suppression config from `.code-review-graphignore`-style patterns or `[tool.code-review-graph.test_gap_suppressions]` in `pyproject.toml`. |
| `code_review_graph/changes.py` | Modified | Use deterministic priority ordering, repo-relative review projections, path scoping, and test-gap suppression. |
| `code_review_graph/tools/review.py` | Modified | Add `for_review`, `max_tokens`, and `path_globs`/`scope` parameters to MCP-facing review functions. |
| `code_review_graph/main.py` | Modified | Expose new MCP arguments for `detect_changes_tool`; add a `review_context_tool` only if the existing MCP naming pattern supports it cleanly. |
| `code_review_graph/cli.py` | Modified | Add `detect-changes --for-review --max-tokens N --scope GLOB`, add `review-context --base <ref>`, and update brief savings wording. |
| `code_review_graph/context_savings.py` | Modified | Add scope-honest panel title/note and machine-readable savings record. |
| `code_review_graph/incremental.py` | Modified if needed | Reuse or expose ignore-pattern matching for scope/test-gap suppression without duplicating fragile glob logic. |
| `docs/COMMANDS.md` | Modified | Document `review-context`, `--for-review`, `--max-tokens`, `--scope`, and savings-record semantics. |
| `docs/USAGE.md` | Modified | Document compact review workflow, scoped section-agent workflow, and savings scope caveat. |
| `docs/FEATURES.md` | Modified | Add changelog-style feature bullets for the new review-output capability. |
| `tests/test_changes.py` | Modified | Add behavior tests for deterministic output, repo-relative paths, scoping, and suppression. |
| `tests/test_cli.py` | Modified | Add CLI tests for `--for-review`, `--max-tokens`, `--scope`, `review-context`, and savings panel wording. |
| `tests/test_tools.py` | Modified | Add MCP/tool tests for compact payload and scoped filtering behavior. |
| `tests/test_context_savings.py` | Modified | Add savings-record and scope-honest panel tests. |
| `tests/fixtures/` | Modified if needed | Add small fixture payloads/config files only when they make tests clearer than inline setup. |
| `.zazz/execution/mw-improve-metrics-analysis-zazz-run-log.md` | New during implementation | Append-only run log for OQ resolutions, phase evidence, deviations, and optional manual qb-mono validation. |

### Out Of Scope

- P6 sibling-template divergence, nearest-sibling search, and construct-level sibling diffing.
- Turso/libSQL/storage-engine work.
- Parser rewrites or new parse-time metadata for sibling comparison.
- Product-level token accounting across external agent transcripts.
- Product implementation of standards-index context packets, finding-quality benchmark harnesses, standards-vs-codebase drift detection, prior-finding reconciliation, review-topology planning, read-first planning, and consolidation-candidate detection unless the Owner revises this specification. This document still defines their target behavior as design guidance in §2.a.
- Installing CRG in external consumer repos as a permanent configuration change.
- Committing or editing files in any external validation repo.

---

## 4. Decisions

### D-1 — Keep Storage Absolute; Normalize At Review Projection Boundaries

**Decision.** Add review-output path normalization without changing stored `nodes.file_path` or qualified-name storage.

**Why.** Existing lookup, diff mapping, visualization, and Windows regression coverage assume absolute stored paths. Projection-level normalization delivers the token/cache win with lower migration risk.

### D-2 — Use One Shared Projection Helper Instead Of Per-Command Formatting

**Decision.** Implement compact review shape in a reusable module called from CLI and MCP paths.

**Why.** CLI `detect-changes`, MCP `detect_changes_func`, and the new `review-context` must agree on path normalization, sorting, scoping, and token budgeting. Scattering transformations across command handlers would produce drift.

### D-3 — Budget By Stable Estimated Tokens, With Explicit Truncation Metadata

**Decision.** Use the existing `estimate_tokens` 4-chars/token approximation for `--max-tokens`, and include truncation counts/flags for each budgeted list.

**Why.** Exact model tokenization is optional and model-specific; the product already treats estimates as directional. Truncation metadata keeps compact output honest when lists are shortened.

### D-4 — Scope Filtering Applies To Review Projection And Analysis Inputs Where Safe

**Decision.** `--scope`/`path_globs` first restricts changed files and projected result rows by repo-relative path. Upstream analysis may still use full changed-file context when required for correctness, but emitted rows must be scope-filtered.

**Why.** Downstream filtering gives immediate token savings and minimizes risk. Implementers may also filter analysis inputs when tests prove no loss of intended behavior for section agents.

### D-5 — Test-Gap Suppression Is Configured, Observable, And Non-Destructive

**Decision.** Suppressed gaps are removed from `test_gaps` but counted in metadata and optionally named in debug/standard output.

**Why.** The user-facing goal is lower noise, not hiding that suppression occurred. Metadata lets reviewers detect overly broad allowlists.

### D-6 — `review-context` Composes Existing Build/Update And Analysis Pieces

**Decision.** The new command is orchestration glue: stale-check/build-or-update, then compact review projection. It does not invent a second analysis engine.

**Why.** Existing `build_or_update_graph`, `get_changed_files`, `analyze_changes`, and postprocess controls already handle graph lifecycle. The missing user value is one command and one MCP-style call.

### D-7 — Savings Reporting Names Its Measurement Scope

**Decision.** Rename the human panel to "Change-analysis token savings" and add a machine-readable `savings_record` with scope fields.

**Why.** The existing panel can be misread as whole-review-session savings. This deliverable measures the detect/change-analysis step unless a later deliverable adds whole-session accounting.

### D-8 — Open Questions Are Resolved For Unattended Implementation

**Decision.** The implementation defaults in §10 are approved for this deliverable and do not require another Owner round-trip before code begins.

**Why.** Overnight implementation needs a runnable contract. These defaults preserve the P1/P2/P3/P4/P5/P7 scope while avoiding speculative product choices.

---

## 5. Agent Implementation Rules

### Team Integration

Commit and push only to the feature branch. Do not merge directly to `main`; all integration happens through human PR review.

### Command Working Directory

Run commands from the repository root:

```bash
uv run pytest tests/test_changes.py tests/test_cli.py tests/test_tools.py tests/test_context_savings.py tests/test_main.py -q
uv run ruff check .
uv run mypy code_review_graph
```

Adjust only when local evidence shows the repo uses a different current command.

### Commit And Push

Default to one coherent green commit after the specification's DoD and verifier pass. Waypoint commits are allowed only at coherent green recovery points. Do not commit red tests, half-applied refactors, or local-only evidence artifacts as product commits.

### Scope Verification

For this single-specification branch:

```bash
git diff main --stat
```

The diff should list only files in §3 unless the Owner approved a specification revision.

### Autonomy Boundaries

Hard constraints:

- Scope in §3.
- Approved review shape in §3.
- Invariants in §2.
- Public CLI/MCP contracts named in §6.
- Standards in §1.d.
- Acceptance criteria in §6.
- Halt conditions below.

Adaptive guidance:

- exact helper names and module split
- exact suppression config table shape, if documented and tested
- test function names, if the intent and coverage remain equivalent
- whether `review-context` is CLI-only or also exposed as MCP, as long as CLI and existing MCP `detect_changes_tool` satisfy the ACs

The agent may adapt internals when verified local evidence supports it, provided hard constraints still hold. Meaningful deviations go in the run log. Contract-changing deviations require Owner sign-off and specification revision.

### Run Log

Maintain `.zazz/execution/mw-improve-metrics-analysis-zazz-run-log.md`. Append entries after standards verification, phase completions, deviations, manual evidence, QA findings, rework, and load-bearing issues. The run log has been bootstrapped with the §10 default resolutions; implementation should cite those entries rather than re-opening the questions.

### Optional External Validation

The Owner may provide a local qb-mono worktree for manual validation. If used, the implementing agent may remove that external repo's local `.code-review-graph/` derived cache before rebuilding CRG there. This is allowed only because CRG graph data is a gitignored rebuildable cache. Record the validation repo identity, cache removal, commands, and generated evidence paths in the run log. Do not commit or otherwise modify external repo source files.

### Halt Conditions

The agent must stop and surface to the Owner if any of these occur:

1. A §10 resolved default becomes impossible to implement without changing public CLI/MCP contract, scope, or an invariant.
2. Same automated test fails 3 iterations in a row.
3. `uv run ruff check .` or `uv run mypy code_review_graph` fails for a reason not addressable by the obvious fix in 2 iterations.
4. `git diff main --stat` shows a product-code file outside §3.
5. Implementation appears to require parser, storage-engine, migration, or P6 sibling-divergence changes.
6. A standard not prescribed in §1.d matches the file list via standards-index lookup.
7. Required reference data or optional external validation repo is unavailable; proceed without optional validation only after logging that it was skipped.
8. A needed deviation changes scope, public CLI/MCP contract, ACs, approved review topology, or an invariant.

---

## 6. Acceptance Criteria

- **AC1 — Review payload paths are repo-relative and deterministic.** `detect-changes --for-review` and MCP compact review output emit repo-relative `file`/`file_path` values for changed functions, priorities, test gaps, and affected-flow surfaces. Equal-risk priorities sort by stable secondary keys: repo-relative path, line, qualified name. Verified by `tests/test_changes.py::test_for_review_projection_uses_repo_relative_paths_and_stable_tiebreaks` and `tests/test_tools.py::test_detect_changes_for_review_uses_portable_paths`.
- **AC2 — Compact budgeted review mode exists.** `code-review-graph detect-changes --for-review --max-tokens N` returns a ready-to-inline payload containing summary, risk score, changed-file count, top priorities with repo-relative `file:line`, de-noised test gaps, affected-flow name/criticality summaries, and truncation metadata. It stays under the requested budget when `N` is realistically above the minimum envelope size, and reports when the budget is too small for the minimum envelope. Verified by `tests/test_cli.py::test_detect_changes_for_review_obeys_token_budget_with_truncation_metadata`.
- **AC3 — One-shot review-context command exists.** `code-review-graph review-context --base <ref> --max-tokens N` performs stale-safe graph setup using existing build/update helpers, then emits the same compact review payload as `detect-changes --for-review`. It must avoid duplicate changed-file detection work where practical. Verified by `tests/test_cli.py::test_review_context_runs_update_then_compact_projection_once`.
- **AC4 — Section-scoped analysis works.** CLI `--scope <glob>` and MCP `path_globs`/`scope` restrict emitted changed functions, review priorities, test gaps, and affected flows to matching repo-relative paths. Nonmatching changed files do not appear in compact payloads. Verified by `tests/test_changes.py::test_for_review_scope_filters_projected_rows_by_path_glob` and `tests/test_tools.py::test_detect_changes_scope_returns_only_matching_section`.
- **AC5 — Test-gap suppression removes configured noise without hiding suppression.** Configured suppressions by path glob, node kind/name pattern, or decorator/modifier when available prevent matching rows from appearing in `test_gaps`; payload metadata reports `suppressed_test_gap_count`. Verified by `tests/test_changes.py::test_test_gap_suppressions_remove_boilerplate_and_report_count`.
- **AC6 — Savings output is scope-honest and machine-readable.** The brief panel title or note states that the number measures change-analysis output, not whole review-session savings. JSON/compact payloads include a `savings_record` with base ref, changed-file count, estimate flag, baseline tokens, returned tokens, saved tokens, saved percent, and measurement scope. Verified by `tests/test_context_savings.py::test_savings_record_names_change_analysis_scope` and `tests/test_cli.py::test_brief_panel_names_change_analysis_scope`.
- **AC7 — P6 remains absent.** No new nearest-sibling, sibling divergence, construct-level majority comparison, or sibling-divergence parser metadata is introduced. Verified by code inspection and `rg -n "sibling_diverg|nearest_sibling|construct.*diverg" code_review_graph tests` returning no product implementation except explicit out-of-scope documentation.
- **AC8 — Documentation teaches the new workflow.** `docs/COMMANDS.md`, `docs/USAGE.md`, and `docs/FEATURES.md` document the compact, scoped, one-shot review flow and savings-scope caveat. Verified by `rg -n "review-context|--for-review|--max-tokens|--scope|Change-analysis token savings" docs`.
- **AC9 — Type, lint, and scoped tests are clean.** Verified by `uv run pytest tests/test_changes.py tests/test_cli.py tests/test_tools.py tests/test_context_savings.py tests/test_main.py -q`, `uv run ruff check .`, and `uv run mypy code_review_graph`.
- **AC10 — Scope is clean.** Verified by `git diff main --stat` listing only files allowed by §3.

---

## 7. Test Plan

Test value rule: every automated test below proves an AC, invariant, public contract, realistic edge case, regression, or named risk. Prefer table-driven cases when several realistic cases share one behavior boundary.

Reference data sources:

- Existing in-memory `GraphStore` helpers in `tests/test_changes.py` — primary source for deterministic analysis tests.
- Existing CLI monkeypatch patterns in `tests/test_cli.py` — source for command-surface tests without expensive repo builds.
- Existing context-savings helpers in `tests/test_context_savings.py` — source for savings metadata tests.
- Small synthetic repo fixtures in `tmp_path` — used only for end-to-end command behavior that needs git diff and graph data.

Automated tests:

- `test_for_review_projection_uses_repo_relative_paths_and_stable_tiebreaks` — verifies AC1 by creating two equal-risk nodes with absolute stored paths and asserting compact output uses repo-relative paths and deterministic secondary ordering.
- `test_detect_changes_for_review_uses_portable_paths` — verifies AC1 through the MCP/tool path by patching `_get_store` and asserting no absolute checkout prefix appears in the compact result.
- `test_detect_changes_for_review_obeys_token_budget_with_truncation_metadata` — verifies AC2 by constructing an analysis with more rows than fit the budget and asserting list truncation plus `truncated`/`omitted_count` metadata.
- `test_review_context_runs_update_then_compact_projection_once` — verifies AC3 by patching build/update and change detection helpers, then asserting the command composes them once and prints compact JSON.
- `test_for_review_scope_filters_projected_rows_by_path_glob` — verifies AC4 with a two-directory fixture, one matching glob and one nonmatching glob.
- `test_detect_changes_scope_returns_only_matching_section` — verifies AC4 through the tool path and protects section-agent usage.
- `test_test_gap_suppressions_remove_boilerplate_and_report_count` — verifies AC5 using a configured suppression for a realistic boilerplate pattern and a nonsuppressed function in the same changed file.
- `test_savings_record_names_change_analysis_scope` — verifies AC6 by asserting exact metadata keys and `measurement_scope == "change_analysis"`.
- `test_brief_panel_names_change_analysis_scope` — verifies AC6 by asserting the brief panel title/note prevents whole-session misreading.
- `test_detect_changes_maps_committed_change_to_functions` — existing coverage intentionally reused for path remapping regression; keep it green while adding repo-relative output projection.
- `test_analyze_changes_review_priorities_ordered` — existing coverage intentionally reused but should be tightened or supplemented for equal-risk tie ordering.

Manual verification:

- Optional: Run CRG against the Owner-supplied qb-mono validation worktree with the compact/scoped workflow and capture payload sizes in the run log. If an existing `.code-review-graph/` derived cache is present in that external repo, it may be removed before rebuilding. This is optional manual evidence, not a required automated test.
- Optional: Generate the CRG visualization using `.zazz/execution/crg-graph-visualization-guidance.md` after implementing the new output paths to understand graph structure and validate user-facing guidance.

Existing coverage intentionally reused:

- `tests/test_cli.py::test_json_output_includes_compact_savings_metadata` — already proves JSON output carries compact `context_savings`; extend rather than duplicate if the new `savings_record` lives beside it.
- `tests/test_context_savings.py::test_estimate_tokens_uses_conservative_character_approximation` — already proves the budget estimator primitive.

---

## 8. TDD Entry Point + Prescriptive Execution Sequence

The execution sequence is derived from §6 Acceptance Criteria and §7 Test Plan. Do not change the implementation contract by changing only this section; revise ACs/decisions first when the contract changes.

### TDD Entry Point

Add the first failing test:

```python
def test_for_review_projection_uses_repo_relative_paths_and_stable_tiebreaks(tmp_path):
    """Compact review output is portable and deterministic."""
    ...
```

### Prescriptive Execution Sequence

Follow this order unless verified local evidence shows a safer order. Log meaningful deviations.

**Phase 1: Projection foundation**

1.1. Add `code_review_graph/review_projection.py` with repo-relative path helpers, deterministic sort keys, compact row builders, and budget truncation.
1.2. Keep stored graph paths unchanged.
1.3. Add tests for repo-relative output and stable tie ordering.
1.4. Run: `uv run pytest tests/test_changes.py -q`. Expect projection tests to pass and existing change-analysis tests to stay green.

**Phase 2: Compact `--for-review` mode**

2.1. Wire projection helpers into `analyze_changes`, `detect_changes_func`, and CLI `detect-changes`.
2.2. Add `--for-review` and `--max-tokens` CLI options.
2.3. Add/extend MCP parameters with `for_review: bool = False` and `max_tokens: int | None = None`.
2.4. Run: `uv run pytest tests/test_changes.py tests/test_cli.py tests/test_tools.py tests/test_main.py -q`.

**Phase 3: One-shot `review-context`**

3.1. Add a CLI `review-context` subcommand that composes stale-safe graph setup and compact review projection.
3.2. Prefer existing `build_or_update_graph` and `analyze_changes` plumbing; do not create a parallel analysis path.
3.3. Add a command test that patches setup and analysis helpers and asserts each is called once.
3.4. Run: `uv run pytest tests/test_cli.py -q`.

**Phase 4: Section scope filtering**

4.1. Add `scope`/`path_globs` support, using repo-relative path matching and existing ignore/glob semantics where practical.
4.2. Scope emitted rows for changed functions, review priorities, test gaps, and affected flows.
4.3. Add CLI and MCP tests for matching and nonmatching directories.
4.4. Run: `uv run pytest tests/test_changes.py tests/test_tools.py tests/test_cli.py -q`.

**Phase 5: Test-gap suppression**

5.1. Add `code_review_graph/test_gap_config.py`.
5.2. Load suppressions from a documented config source; prefer `pyproject.toml` when structured matching is needed, and mirror `.code-review-graphignore`-style glob parsing for path rules.
5.3. Apply suppression before appending test gaps and record suppression metadata.
5.4. Run: `uv run pytest tests/test_changes.py -q`.

**Phase 6: Scope-honest savings**

6.1. Add `savings_record` helper and update panel title/note.
6.2. Keep existing `context_savings` metadata backward compatible.
6.3. Add tests for exact scope wording and record fields.
6.4. Run: `uv run pytest tests/test_context_savings.py tests/test_cli.py -q`.

**Phase 7: Documentation and final verification**

7.1. Update `docs/COMMANDS.md`, `docs/USAGE.md`, and `docs/FEATURES.md`.
7.2. Run the scoped test command, ruff, mypy, and scope verification.
7.3. Optional: run qb-mono manual validation and graph visualization guidance, then log evidence.

### Skeleton: `code_review_graph/review_projection.py`

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .context_savings import estimate_tokens


@dataclass(frozen=True)
class ProjectionBudget:
    max_tokens: int | None
    minimum_envelope_tokens: int


def to_repo_relative(path: str | None, repo_root: Path | None) -> str | None:
    """Return a portable repo-relative path when possible."""
    if path is None:
        return None
    if repo_root is None:
        return path
    candidate = Path(path)
    try:
        return candidate.resolve().relative_to(repo_root.resolve()).as_posix()
    except (OSError, ValueError):
        return path.replace("\\", "/")


def priority_sort_key(row: dict[str, Any]) -> tuple[float, str, int, str]:
    """Sort by descending risk, then stable path/line/name."""
    risk = float(row.get("risk_score", 0.0))
    file_path = str(row.get("file_path") or row.get("file") or "")
    line = int(row.get("line_start") or row.get("line") or 0)
    name = str(row.get("qualified_name") or row.get("name") or "")
    return (-risk, file_path, line, name)


def filter_by_path_globs(
    rows: Iterable[dict[str, Any]],
    globs: list[str] | None,
) -> list[dict[str, Any]]:
    """Return rows whose repo-relative file path matches any requested scope."""
    ...


def project_for_review(
    analysis: dict[str, Any],
    *,
    repo_root: Path | None,
    changed_files: list[str],
    base: str,
    max_tokens: int | None,
    path_globs: list[str] | None = None,
) -> dict[str, Any]:
    """Build the compact, deterministic, budgeted review payload."""
    ...


def truncate_to_budget(payload: dict[str, Any], max_tokens: int | None) -> dict[str, Any]:
    """Trim priority/gap/flow lists until the payload fits the token budget."""
    if max_tokens is None:
        return payload
    ...
    payload["budget"] = {
        "max_tokens": max_tokens,
        "estimated_tokens": estimate_tokens(payload),
        "truncated": True,
    }
    return payload
```

### Skeleton: `code_review_graph/test_gap_config.py`

```python
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class TestGapSuppression:
    path_globs: tuple[str, ...] = field(default_factory=tuple)
    kinds: tuple[str, ...] = field(default_factory=tuple)
    name_patterns: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""


def load_test_gap_suppressions(repo_root: Path | None) -> list[TestGapSuppression]:
    """Load optional low-signal test-gap suppression rules."""
    ...


def is_test_gap_suppressed(
    node: object,
    *,
    repo_relative_path: str,
    suppressions: list[TestGapSuppression],
) -> bool:
    """Return True when a changed node's missing TESTED_BY edge is configured noise."""
    ...
```

---

## 9. Definition Of Done

- [ ] All §1 required reading consumed; standards-index verification performed.
- [ ] §10 resolved defaults confirmed implementable and already logged; any impossible default halted and surfaced.
- [ ] Scoped tests green: `uv run pytest tests/test_changes.py tests/test_cli.py tests/test_tools.py tests/test_context_savings.py tests/test_main.py -q`.
- [ ] Lint clean: `uv run ruff check .`.
- [ ] Type check clean or known repo baseline documented: `uv run mypy code_review_graph`.
- [ ] Documentation checks complete by inspection and `rg` commands in AC8.
- [ ] P6 absence verified by the AC7 `rg` command.
- [ ] Optional external qb-mono validation completed or explicitly logged as skipped.
- [ ] Optional graph visualization guidance run completed or explicitly logged as skipped.
- [ ] Scope verification lists exactly the files in §3 for this specification slice.
- [ ] PR shape matches the approved review shape in §3.
- [ ] All AC1-AC10 verified, with evidence cited.
- [ ] Run log for this specification is up to date.
- [ ] Verifier sub-agent dispatched and returned all-pass.
- [ ] PR draft body links this specification and lists each AC's verification.

---

## 10. Resolved Open Questions

These questions are resolved for unattended implementation. Do not ask the Owner again unless one default becomes impossible without changing scope, public contract, ACs, or an invariant. Record any such impossibility as a halt condition in the run log.

- **OQ-1 — Default token budget.** Resolved: `--for-review` has no default truncation when `--max-tokens` is omitted. `review-context` documentation and examples recommend `--max-tokens 2000`, but the command does not silently impose it.
- **OQ-2 — Suppression config source.** Resolved: support structured rules in `pyproject.toml` under `[tool.code-review-graph.test_gap_suppressions]` for this deliverable. Suppression rules may contain path globs, node kinds, name patterns, and reasons. Reuse `.code-review-graphignore`-style glob matching semantics for path matching, but do not overload `.code-review-graphignore` with suppression semantics and do not add a new `.code-review-graph/test-gap-suppressions.toml` file in this PR.
- **OQ-3 — MCP shape.** Resolved: update existing `detect_changes_tool` / MCP-facing `detect_changes_func` with compact review parameters first. `review-context` is required as a CLI command. Add a separate `review_context_tool` only if the existing MCP registration pattern makes it a thin wrapper over the same projection path; absence of a new MCP tool is acceptable when `detect_changes_tool(for_review=True, ...)` satisfies AC1, AC2, AC4, and AC6.
- **OQ-4 — Optional qb-mono validation.** Resolved: automated tests, lint, mypy, scope verification, and documentation checks are required. External qb-mono validation and visualization are optional manual evidence. Run them only after automated verification is green and the local worktree is available without setup churn; otherwise log them as skipped.

---

## 11. Run Log Protocol

This specification uses the shared run log:

`.zazz/execution/mw-improve-metrics-analysis-zazz-run-log.md`

The run log is an append-only execution record under `.zazz/execution/`. It may be committed or ignored according to repo practice for this branch; do not rely on `/tmp/` for evidence that must survive reboot.

Required sections:

- Standards Verification
- OQ Resolutions
- Phase Completions
- Deviations
- Manual Evidence Locations
- QA Findings & Rework
- Issues & Recoveries
- Verifier Sub-Agent Report

Session start protocol:

1. Read this specification end to end.
2. Read the entire run log.
3. Confirm the next phase based on the most recent Phase Completion entry.
4. Confirm the §10 defaults are still implementable without changing scope.
5. Begin implementation.

---

## 12. Appendix — Agent Implementation Prompt

Paste this prompt into a fresh overnight implementation session. This is an implementation prompt, not a spec-review prompt.

```text
You are starting fresh in the active worktree for branch mw-improve-metrics-analyis-zazz.
Your task is unattended overnight implementation of Improve Review Metrics And Analysis Output.

Specification: .zazz/specifications/mw-improve-metrics-analysis-zazz.md
Shared run log: .zazz/execution/mw-improve-metrics-analysis-zazz-run-log.md
Visualization guidance: .zazz/execution/crg-graph-visualization-guidance.md

Read the specification end to end before doing anything else. Then read the shared run log in full.

Work only inside this worktree for development. You may read or query outside this worktree only where the specification or run log explicitly allows optional validation/reference reading. The external validation worktree named below may be used to run CRG and clean/rebuild derived CRG state, but product implementation edits belong only in this worktree.

NON-NEGOTIABLE RULES
1. Follow the specification's Agent Implementation Rules.
2. Treat §10 as resolved. Do not reopen OQs unless a resolved default is impossible without changing scope, public contract, ACs, or an invariant.
3. Verify standards via .zazz/standards/index.yaml before writing code.
4. Tests and verification are not optional. Every AC must have evidence.
5. Do not implement P6 sibling-template divergence.
6. Do not merge to main; integration happens through human PR review.
7. Do not implement the future review-quality features in §2.a unless an AC explicitly requires them.
8. Do not edit .agents/skills/pr-review/ or create a new Zazz skill in this deliverable.

ORDER OF WORK
1. Read the specification, run log, required docs, standards, and code references.
2. Verify the §10 resolved defaults are still implementable.
3. Review ACs (§6) and Test Plan (§7); start with the TDD entry point in §8.
4. Confirm the implementation still matches the approved review shape in §3.
5. Execute the specification's phases.
6. Run verification and complete the DoD (§9).
7. Dispatch a verifier sub-agent.
8. Prepare PR-ready output.

CURRENT IMPLEMENTATION SLICE
- Implement P1/P2/P3/P4/P5/P7 only: repo-relative deterministic paths, compact budgeted --for-review output, CLI review-context, scoped packets, observable test-gap suppression, and scope-honest savings metadata.
- Keep P6 sibling-template divergence absent.
- Treat standards-index packets, read-first plans, topology planning, standards drift, consolidation detection, low-value-test scoring, and a dedicated Zazz code-review-graph skill as future deliverables.

REQUIRED FINAL VERIFICATION
- uv run pytest tests/test_changes.py tests/test_cli.py tests/test_tools.py tests/test_context_savings.py tests/test_main.py -q
- uv run ruff check .
- uv run mypy code_review_graph
- rg -n "sibling_diverg|nearest_sibling|construct.*diverg" code_review_graph tests
- rg -n "review-context|--for-review|--max-tokens|--scope|Change-analysis token savings" docs
- git diff main --stat
- git diff --check

OPTIONAL EXTERNAL VALIDATION
The approved external validation worktree is:

  /Users/michael/Victory/Dev/qb-mono-wt/ss-BE-ReturnAddressWireUp

This worktree is already merged and may be used to execute CRG against a larger repo. You may clean and rerun CRG there, including removing and rebuilding local derived CRG state such as .code-review-graph/. Record validation commands and evidence in the run log. Do not make product implementation edits in that repo, and do not commit or push from that repo.

VERIFIER SUB-AGENT
After your own DoD checklist is green, dispatch a fresh sub-agent:

  "You are verifying Improve Review Metrics And Analysis Output in branch mw-improve-metrics-analyis-zazz. Read the specification at .zazz/specifications/mw-improve-metrics-analysis-zazz.md and the shared run log at .zazz/execution/mw-improve-metrics-analysis-zazz-run-log.md. Follow the Implementation Rules. For each AC, independently verify it by running the cited test or command where practical. Cross-check deviations and QA findings logged in the run log against the code. Verify the specification slice matches its scope using git diff main --stat. Verify P6 sibling-template divergence was not implemented. Do not modify code or the run log. Return PASS/FAIL per AC with evidence."

Only declare done after the verifier reports all-pass.
```

---

*End of specification. Implementation proceeds from this specification and the run log; no separate plan is created.*
