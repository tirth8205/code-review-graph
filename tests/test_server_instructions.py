"""The MCP server instructions only name tools and patterns that exist.

With deferred tool loading an agent sees the server instructions and the tool
names, not the tool descriptions, so the instructions are its routing table. A
renamed tool or query pattern would leave that table pointing at something the
server no longer has, with nothing else failing.
"""

from __future__ import annotations

import asyncio
import re

from code_review_graph import main
from code_review_graph.tools.query import _QUERY_PATTERNS

# Claude Code truncates server instructions past this many characters.
CLAUDE_CODE_INSTRUCTIONS_LIMIT = 2048


def test_server_is_built_with_the_routing_instructions() -> None:
    assert main.mcp.instructions == main.SERVER_INSTRUCTIONS


def test_instructions_fit_before_truncation() -> None:
    assert len(main.SERVER_INSTRUCTIONS) <= CLAUDE_CODE_INSTRUCTIONS_LIMIT


def test_every_tool_named_is_registered() -> None:
    named = set(re.findall(r"\b\w+_tool\b", main.SERVER_INSTRUCTIONS))
    assert named, "the instructions no longer name any tool"
    registered = {tool.name for tool in asyncio.run(main.mcp.list_tools())}
    assert named <= registered, f"named but not registered: {sorted(named - registered)}"


def test_every_query_pattern_named_exists() -> None:
    named = set(re.findall(r"pattern=(\w+)", main.SERVER_INSTRUCTIONS))
    named |= set(re.findall(r"pattern=\w+ / (\w+)", main.SERVER_INSTRUCTIONS))
    assert named, "the instructions no longer name any query pattern"
    assert named <= set(_QUERY_PATTERNS), (
        f"named but not a query pattern: {sorted(named - set(_QUERY_PATTERNS))}"
    )


def test_plain_text_search_is_left_to_grep() -> None:
    # The graph indexes structure, not text. Without this line the routing table
    # reads as "use the graph for everything", and an agent looking for a log
    # message or an env var name gets an empty graph answer instead of a grep hit.
    assert "Grep" in main.SERVER_INSTRUCTIONS
    assert "does not index text" in main.SERVER_INSTRUCTIONS
