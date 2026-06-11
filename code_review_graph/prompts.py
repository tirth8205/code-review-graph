"""MCP prompt templates for Code Review Graph.

Provides 5 pre-built prompt workflows, all enforcing token-efficient
detail_level="minimal" first patterns with get_minimal_context entry point.

1. review_changes   - pre-commit review using detect_changes + affected_flows
2. architecture_map - architecture docs using communities, flows, Mermaid
3. debug_issue      - guided debugging using search, flow tracing
4. onboard_developer - new dev orientation using stats, architecture, flows
5. pre_merge_check  - PR readiness with risk scoring, test gaps, dead code
"""

from __future__ import annotations

from fastmcp.prompts.prompt import Message

_TOKEN_EFFICIENCY_PREAMBLE = (  # nosec B105 — prompt template, not a password
    """\
## Rules for Token-Efficient Graph Usage
1. ALWAYS call `get_minimal_context` first with a task description.
2. Use `detail_level="minimal"` on all tool calls unless the minimal output \
is insufficient.
3. Only escalate to `detail_level="standard"` or `"verbose"` for the specific \
entities that need deeper inspection.
4. Never request more than 3 tool calls per turn unless absolutely necessary.
5. Prefer targeted queries (query_graph with a specific symbol) over broad \
scans (list_communities with full members).
6. When reviewing changes: detect_changes(detail_level="minimal") → only \
expand on high-risk items.
"""
)


def _user(content: str) -> list[Message]:
    """Wrap content as a single-message user prompt.

    fastmcp >=3.2 rejects raw dicts in prompt return values; each message
    must be a ``Message`` instance (or a plain ``str``). We standardise on
    ``Message`` so role is explicit and future multi-turn prompts compose
    naturally.
    """
    return [Message(role="user", content=content)]


def review_changes_prompt(base: str = "HEAD~1") -> list[Message]:
    """Pre-commit review workflow.

    Args:
        base: Git ref to diff against. Default: HEAD~1.
    """
    return _user(
        f"{_TOKEN_EFFICIENCY_PREAMBLE}\n"
        f"## Review Workflow\n"
        f'1. Call `get_minimal_context(task="review changes against '
        f'{base}")` to get risk overview.\n'
        f'2. If risk is "low": call '
        f'`detect_changes(detail_level="minimal")` → report summary '
        f"+ any test gaps.\n"
        f'3. If risk is "medium" or "high":\n'
        f'   a. Call `detect_changes(detail_level="standard")` for '
        f"full change list.\n"
        f"   b. For each high-risk function, call "
        f'`query_graph(pattern="callers_of", target=<func>, '
        f'detail_level="minimal")`.\n'
        f'   c. Call `get_affected_flows(detail_level="minimal")` '
        f"only if >3 changed functions.\n"
        f"4. Summarize: risk level, what changed, test gaps, "
        f"specific improvements needed.\n\n"
        f"Do NOT call get_review_context unless you need source code "
        f"snippets for a specific function."
    )


def architecture_map_prompt() -> list[Message]:
    """Architecture documentation workflow."""
    return _user(
        f"{_TOKEN_EFFICIENCY_PREAMBLE}\n"
        "## Architecture Mapping Workflow\n"
        '1. Call `get_minimal_context(task="map architecture")`.\n'
        '2. Call `get_architecture_overview(detail_level="minimal")` '
        "for community coupling summary.\n"
        '3. Call `list_flows(detail_level="minimal")` for critical '
        "flow names + criticality scores.\n"
        "4. Only call `get_community(name=<X>, "
        'detail_level="standard")` for the 1-2 communities the user '
        "is most interested in.\n"
        "5. Produce a concise Mermaid diagram showing communities as "
        "boxes and key flows as arrows."
    )


def debug_issue_prompt(description: str = "") -> list[Message]:
    """Guided debugging workflow.

    Args:
        description: Description of the issue to debug.
    """
    desc_part = description or "<description>"
    return _user(
        f"{_TOKEN_EFFICIENCY_PREAMBLE}\n"
        "## Debug Workflow\n"
        f'1. Call `get_minimal_context(task="debug: '
        f'{desc_part}")`.\n'
        "2. Call `semantic_search_nodes(query=<keywords from "
        'description>, detail_level="minimal", limit=5)`.\n'
        "3. For the top 1-2 results, call "
        '`query_graph(pattern="callers_of", target=<name>, '
        'detail_level="minimal")`.\n'
        "4. If the issue involves execution flow: call "
        "`get_flow(name=<relevant flow>)` for the single most "
        "relevant flow.\n"
        "5. Only call `get_review_context` or `get_impact_radius` "
        "if you need to trace the blast radius of a specific change."
    )


def onboard_developer_prompt() -> list[Message]:
    """New developer orientation workflow."""
    return _user(
        f"{_TOKEN_EFFICIENCY_PREAMBLE}\n"
        "## Onboarding Workflow\n"
        '1. Call `get_minimal_context(task="onboard developer")`.\n'
        "2. Call `list_graph_stats()` for technology overview.\n"
        '3. Call `get_architecture_overview(detail_level="minimal")` '
        "for the 30-second mental model.\n"
        '4. Call `list_communities(detail_level="minimal")` — '
        "present as a table of module names + sizes.\n"
        '5. Call `list_flows(detail_level="minimal")` — highlight '
        "the top 3 critical flows.\n"
        "6. Only drill into a specific community or flow if the "
        "developer asks."
    )


def pre_merge_check_prompt(base: str = "HEAD~1") -> list[Message]:
    """PR readiness check workflow.

    Args:
        base: Git ref to diff against. Default: HEAD~1.
    """
    return _user(
        f"{_TOKEN_EFFICIENCY_PREAMBLE}\n"
        "## Pre-Merge Check Workflow\n"
        '1. Call `get_minimal_context(task="pre-merge check")`.\n'
        '2. Call `detect_changes(detail_level="minimal")` for risk '
        "score and test gaps.\n"
        "3. If risk > 0.4: call "
        '`get_affected_flows(detail_level="minimal")`.\n'
        "4. If test_gap_count > 0: call "
        '`query_graph(pattern="tests_for", '
        'target=<each untested function>, detail_level="minimal")` '
        "for up to 3 functions.\n"
        '5. Call `refactor(mode="dead_code", '
        'detail_level="minimal")` to check for newly dead code.\n'
        "6. Only call `find_large_functions` or `get_impact_radius` "
        "if risk > 0.7.\n"
        "7. Output: GO/NO-GO recommendation with 1-sentence "
        "justification + list of required follow-ups."
    )
