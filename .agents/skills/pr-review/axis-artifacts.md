# Axis Artifact Workflow

Use this workflow for every multi-axis PR review. Each axis produces a standalone packet before the final review is
consolidated. The packet files make the review auditable, reduce accidental loss during aggregation, and let a follow-up
session rerun or compare one axis without redoing the whole review.

## Artifact Directory

Before dispatch, create a fresh axis artifact directory. Use a deterministic path when possible:

```text
/tmp/pr-review-axis/<repo-name>-<base-short-sha>-<head-short-sha>/
```

If the user asks for durable intermediate evidence in the repository, use:

```text
docs/execution/.pr-review-axis/<artifact-stem>/
```

Do not overwrite an existing axis directory. If the path exists, add a short suffix such as `-rerun-2`.

## Required Packet Files

Use these exact file names for active axes:

```text
01-standards-code-quality.md
02-functionality-spec.md
03-security-data-ops.md
04-test-quality.md
```

If an axis is skipped, write its packet file anyway with:

- skipped reason;
- context searched;
- residual risk.

## Packet Contents

Each packet is an axis-local review record, not the final report. It MUST include:

- axis name;
- target branch or PR;
- pinned merge-base;
- standards/spec/CRG context used by that axis;
- commands or checks the axis relied on;
- findings in the `findings-reporting.md` Markdown subsection shape;
- open questions or residual risk;
- explicit `PASS`, `FAIL`, or `SKIPPED` status for that axis.

Axis packets may be organized by severity or by the axis reviewer's natural flow. The final artifact, however, is
always file-first per `findings-reporting.md`.

## Sub-Agent Instructions

When sub-agents can write to the shared workspace or `/tmp`, instruct each active axis to write its own packet file and
return only:

- packet path;
- finding counts by severity;
- one-line axis verdict;
- any blockers that prevented writing the packet.

When sub-agents cannot write files, they return their complete packet text. The orchestrator then writes that text
verbatim to the expected packet file before consolidation.

## Aggregation Rules

The orchestrator MUST read every axis packet file before producing the final findings artifact.

During consolidation:

- preserve every actionable finding unless the orchestrator can prove it is duplicate and records the reason;
- keep axis identity on every finding;
- group final findings by file path and line number;
- include all `[boulder]`, `[rock]`, and `[big-pebble]` findings in the must-fix summary;
- keep optional `[pebble]` and `[sand]` findings in the detailed file section, especially when they are in a file that
  already has must-fix work;
- extract cross-file organization, deduplication, standards-codification, or line-count-reduction themes into
  `Systemic Improvement Opportunities` instead of repeating the same optional finding across many files;
- record the axis packet directory and packet file names in the final artifact's verification/evidence section.

If the final artifact has materially fewer findings than the axis packets, add a short consolidation note explaining
which findings were dropped, merged, downgraded, or ruled out and why.
