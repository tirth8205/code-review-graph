---
name: code-review-graph-refactor
description: >-
  Use when refactoring code. Plans and executes safe refactoring via refactor_tool
  (rename preview, dead code detection) before touching any files.
version: 1.0.0
---

<!-- code-review-graph-refactor v1.0.0 | lifecycle: active -->

# Refactor Safely

Plan and execute safe refactoring using dependency analysis.

## Workflow

1. Use `refactor_tool` with `mode="suggest"` for community-driven refactoring suggestions.
2. Use `refactor_tool` with `mode="dead_code"` to find unreferenced code.
3. For renames, use `refactor_tool` with `mode="rename"` to preview all affected locations.
4. Use `apply_refactor_tool` with the `refactor_id` to apply the rename edits.
5. After changes, run `detect_changes` to verify the refactoring impact.

## Safety Checks

- Always preview before applying (`mode="rename"` gives you a full edit list).
- Check `get_impact_radius` before any major refactor.
- Use `get_affected_flows` to ensure no critical execution paths are broken.
- Run `find_large_functions` to identify decomposition targets.

## Token Efficiency Rules

- ALWAYS start with `get_minimal_context(task="refactor <target>")` before any other graph tool.
- Use `detail_level="minimal"` on all calls.
- Target: complete any refactor plan in ≤5 tool calls and ≤800 total output tokens.
