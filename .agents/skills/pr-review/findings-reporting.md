# Findings Reporting Contract

Use this contract for every final PR review artifact produced by the `pr-review` skill. Axis reviewers work from their
own briefs and write axis packet files per `axis-artifacts.md`, then the final artifact is consolidated from those
packet files into a readable, file-first report that is consistent across runs.

## Severity Tags

- `[boulder]` — critical cross-cutting issue, merge blocker, security/data-loss/regression risk, or issue that affects
  multiple user-visible flows. Must be fixed before approval.
- `[rock]` — significant defect or standards violation: acceptance criterion not met, clear regression, unsafe
  migration, broken API contract, missing auth/authz, or serious missing evidence. Must be fixed before approval.
- `[big-pebble]` — important non-trivial cleanup, reviewability issue, or standards drift that does not prove a runtime
  defect but is still expected to be addressed in the current PR. Must be fixed before approval.
- `[pebble]` — recommended improvement, cleanup, edge case, or maintainability suggestion that is useful but optional.
- `[sand]` — nitpick, wording, formatting, or taste-level suggestion.

Approval rule: any open `[boulder]`, `[rock]`, or `[big-pebble]` means the PR is not approvable. `[pebble]` and `[sand]`
never block approval.

## Required Final Artifact Structure

Start with a title and a short verdict paragraph before the first heading:

```markdown
# PR Review - <target>

**Verdict: Not approvable - N blocking findings.**

One or two sentences naming the blocking themes and the most important verification result. Keep details in the sections
below.
```

Then use this exact section order:

```markdown
## Must-Fix Findings By File And Line

[Blocking action queue, grouped by file path and sorted by line number. Say "No must-fix findings." if empty.]

## Detailed Findings By File

[All findings, including optional findings, grouped by file path and sorted by line number.]

## Systemic Improvement Opportunities

[Only include when one improvement spans multiple files, reduces repeated code, clarifies code organization, or should
be codified in standards. Do not repeat normal one-file findings here.]

## Cross-Axis Overlap

[Only include when multiple axes flag the same file:line.]

## Consolidation Notes

[Only include when final findings materially differ from axis packet findings.]

## Axis Coverage

[Per-axis PASS/FAIL or skipped status, plus any important residual risk.]

## Verification

[Commands/checks run and results, or checks not run.]

## Summary

[Per-axis finding counts, verdict, and residual risk.]
```

Do not replace the file-first sections with separate long axis sections. Preserve axis information on each finding and
in the summary counts instead.

## Readability Rules

The final artifact must be pleasant to read in rendered Markdown.

- Do not emit raw HTML anchors such as `<a id="..."></a>` in final artifacts. Use short Markdown headings and link to
  their generated anchors from the must-fix summary.
- Do not wrap detailed findings in fenced `text` code blocks. Code fences make long paths and prose horizontally scroll
  and hide the structure of the finding.
- Do not repeat the full repository path on every line once inside a file section. Put the path in the `### <file>`
  heading, then use `#### F-<axis>-<n> [severity] line <n> - <title>` for individual findings.
- Keep headings short enough that their generated Markdown anchors are usable. Prefer `line 75 - Insert sproc result
  columns are out of order` over restating the whole file path.
- Use normal paragraphs and bullets for `Evidence`, `Why`, and `Proposed fix`; soft-wrap long prose.
- Put SQL, Python, shell commands, or JSON examples in fenced code blocks only when showing actual code or command
  snippets, not for the whole finding.

Do not produce the final artifact directly from sub-agent chat responses. Read the axis packet files first, then
consolidate. If a sub-agent could not write a packet and returned packet text instead, write that text to the expected
packet path before creating the final artifact.

If you restructure an existing findings artifact, save a sibling copy first, such as
`<artifact>.pre-file-grouping.md`. Do not overwrite the only copy of prior review evidence while changing structure.

## Must-Fix Summary

The must-fix summary is an action queue, not a second copy of the findings.

Rules:

- Include every `[boulder]`, `[rock]`, and `[big-pebble]`.
- Exclude `[pebble]` and `[sand]` unless the user explicitly says a specific pebble is expected to be addressed; in
  that case retag it as `[big-pebble]`.
- Group by file path, sorted lexicographically.
- Within each file, sort by ascending line number, then severity: boulder, rock, big-pebble.
- Keep each entry short: severity, axis, line, title, proposed-fix summary, and a link to the detailed finding.
- Do not repeat the full `Why` body in this summary.
- If multiple axes flag the same `file:line`, either list one entry with multiple axis/detail links or adjacent entries
  under the same file and line. Do not hide the axis identity.
- When a file has must-fix findings and also optional `[pebble]` or `[sand]` findings, add a short "Related optional"
  note under that file with links/counts so the author can clean up nearby work efficiently. Do not relabel optional
  items as blocking unless the reviewer expects them to be fixed for approval.

Template:

```markdown
## Must-Fix Findings By File And Line

- `backend/src/foo.py`
  - **[rock] line 42 — <title>**  
    Axis: Standards / Code Quality; Proposed fix: <short fix summary>; Details: [F-SCQ-001](#f-scq-001-rock-line-42-title)
  - **[big-pebble] line 87 — <title>**  
    Axis: Test Quality; Proposed fix: <short fix summary>; Details: [F-TQ-002](#f-tq-002-big-pebble-line-87-title)
  - Related optional: 2 pebbles in this file; see [F-SCQ-003](#f-scq-003-pebble-line-103-title) and
    [F-TQ-004](#f-tq-004-pebble-line-118-title).
```

## Detailed Findings By File

Group every finding, including optional findings, by source file. Use one `### <file path>` heading per file. Sort file
headings lexicographically. Sort findings within a file by line number, then severity.

Keep optional `[pebble]` and `[sand]` findings in the same file section as blocking findings. This lets the author
address all issues in one file while resolving conflicts, without changing the approval meaning of optional items.

Every finding must be a copy-paste-able Markdown subsection in this exact shape. Do not fence the whole finding:

```markdown
#### F-SCQ-001 [rock] line 42 - <one-line problem statement>

- **Axis:** Standards / Code Quality
- **Severity:** rock
- **Location:** `backend/src/foo.py:42`
- **Evidence:** <changed code, related source/test/standard lines, or verification command that proves the issue>
- **Why it matters:** <impact + governing standard/spec/API contract when applicable>
- **Proposed fix:** <concrete suggested change>
```

Rules:

- The heading starts with a stable finding ID, then the severity tag, then the line number and title.
- Use finding IDs with axis prefixes:
  - `F-SCQ-###` for Standards / Code Quality
  - `F-FS-###` for Functionality / Spec
  - `F-SDO-###` for Security / Data / Ops
  - `F-TQ-###` for Test Quality
- The `Axis:` value is exactly one of:
  - `Standards / Code Quality`
  - `Functionality / Spec`
  - `Security / Data / Ops`
  - `Test Quality`
- The `Severity:` value is the tag without brackets: `boulder`, `rock`, `big-pebble`, `pebble`, or `sand`.
- Use the most specific changed line that demonstrates the issue. If the finding covers a whole file, use line `1` and
  explain the scope in `Why`.
- Keep one issue per block. Do not combine unrelated findings under one severity tag.
- Do not use "same as above"; each block must stand alone when copied into a PR comment.
- When a finding depends on a standard, spec, or API contract, name it in `Why`.

## Systemic Improvement Opportunities

Include this section when the review uncovers a cross-file improvement that is useful but should not be repeated as many
small findings. This section is for organization, deduplication, standards-codification, or line-count reduction themes.

Good entries include:

- splitting several oversized test modules by operation or route group;
- extracting repeated route/OpenAPI response declarations into an existing local helper, if the repo has that pattern;
- consolidating repeated mock setup or payload builders across sibling tests;
- extracting a named helper for repeated filtering, row mapping, or error translation logic;
- codifying a convention in standards when sibling files disagree and the PR exposes the gap.

Template:

```markdown
## Systemic Improvement Opportunities

- **Split service-layer tests by operation.**  
  Files: `backend/tests/svc/test_service_charges.py`, `backend/tests/svc/test_quality_bank_pipeline_specs.py`  
  Why: both files are in the code-structure 400-600 line "should split" band and already have natural operation
  boundaries.  
  Suggested next step: split into get/create/update/delete or document the cohesion exception in the PR.
```

Do not use this section to smuggle in blocking findings. If an opportunity is required for approval, it must also appear
as a `[boulder]`, `[rock]`, or `[big-pebble]` under the affected file.

## Cross-Axis Overlap

If multiple axes flag the same `file:line`, include a short overlap section after detailed findings:

```markdown
## Cross-Axis Overlap

- `backend/src/foo.py:42` — Standards / Code Quality and Security / Data / Ops both flagged this location; prioritize it
  as a systemic issue.
```

Omit the section when there is no overlap.

## Consolidation Notes

Include this section only when the final artifact has materially fewer actionable findings than the axis packets, or when
an axis packet finding is merged, downgraded, ruled out, or omitted.

For each changed packet finding, record:

- packet file and finding title or file:line;
- action: kept, merged, downgraded, ruled out, or omitted;
- reason.

Keep this concise. The goal is auditability, not repeating every finding.

## Axis Coverage

Report whether each axis passed, failed, or was skipped. Keep this concise; detailed findings live in the file-first
section.

Template:

```markdown
## Axis Coverage

- **Standards / Code Quality**: FAIL — 2 findings (0 boulders, 1 rock, 1 big-pebble, 0 pebbles, 0 sand)
- **Functionality / Spec**: PASS — no findings
- **Security / Data / Ops**: FAIL — 1 finding (0 boulders, 1 rock, 0 big-pebbles, 0 pebbles, 0 sand)
- **Test Quality**: SKIPPED — no practical local test surface; residual risk: <risk>
```

## Verification

List exact commands and results. If checks were not run, say why. Also list the axis artifact directory and the packet
files read during consolidation.

## Summary

End with:

- per-axis finding counts;
- final verdict: `Approvable` or `Not approvable — N blocking findings remain`;
- residual risk, including standards/spec gaps and tests not run.
