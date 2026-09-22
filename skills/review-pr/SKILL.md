---
name: review-pr
description: Review a PR or branch diff using the knowledge graph for full structural context. Outputs a structured review with blast-radius analysis.
argument-hint: "[PR number or branch name]"
---

# Review PR

Review all commits of a pull request or branch against its base branch.

## Steps

1. Identify the changes. With a branch name run `git diff --name-only main...<branch>`; otherwise diff the current branch against `main` (or `master`). Use that ref as `base` below.
2. Call `get_minimal_context_tool(task="review PR", base="main")`. If it returns `status: not_ready`, call `build_or_update_graph_tool()` and continue.
3. Call `detect_changes_tool(base="main", detail_level="minimal")` for risk-scored changed functions, test gaps and affected flows across the whole branch.
4. For high-risk functions call `query_graph_tool(pattern="callers_of", target="<function>")` and `query_graph_tool(pattern="tests_for", target="<function>")`. Flag public API changes whose callers were not updated.
5. Call `get_review_context_tool(base="main")` for source snippets, or `get_impact_radius_tool(base="main")` for the full dependent list, only when step 3 leaves a high-risk item unclear.
6. Read the changed source of the highest-impact files first. Do not load whole files unless the change needs it.

## Output

```
## PR Review: <title>

### Summary
<1-3 sentences>

### Risk
- Overall: Low / Medium / High
- Blast radius: <n> files, <m> functions
- Test coverage: <covered> of <changed> changed functions

### Files
#### <path>
- Changes: <what>
- Impact: <who depends on it>
- Issues: <bugs, style, concerns>

### Missing Tests
- <function> in <file>

### Recommendations
1. <action>
```

## Tips

- Use `semantic_search_nodes_tool` to find related code the PR may have missed.
