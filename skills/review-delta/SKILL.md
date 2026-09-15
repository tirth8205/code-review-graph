---
name: review-delta
description: Review only changes since last commit using impact analysis. Token-efficient delta review with automatic blast-radius detection.
argument-hint: "[file or function name]"
---

# Review Delta

Review only the changed code and its blast radius.

## Steps

1. Call `get_minimal_context_tool(task="review changes")`. If it returns `status: not_ready`, call `build_or_update_graph_tool()` and continue.
2. Call `detect_changes_tool(detail_level="minimal")` for risk-scored changed functions, test gaps and affected flows. Changes come from `git diff` against `HEAD~1`; if the argument names a file, pass it in `changed_files`.
3. Call `get_review_context_tool(detail_level="minimal")` when you need source snippets for the changed areas and `review_guidance` (untested functions, wide blast radius, inheritance changes).
4. For each untested high-risk function, confirm with `query_graph_tool(pattern="tests_for", target="<function>")`.
5. Use `detail_level="standard"` only for a high-risk item the minimal output leaves unclear. Do not load whole files unless a snippet is not enough.

## Report

- Summary: one line
- Risk: low, medium or high, from the blast radius
- Issues: bugs, missing tests, style
- Blast radius: impacted files and functions
- Recommendations
