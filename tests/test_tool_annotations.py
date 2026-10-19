"""Every MCP tool declares whether it writes, so clients can tell read-only tools apart."""

import asyncio

from code_review_graph.main import mcp

WRITES_SOURCE = {"apply_refactor_tool"}
WRITES_GRAPH = {
    "build_or_update_graph_tool",
    "run_postprocess_tool",
    "embed_graph_tool",
    "generate_wiki_tool",
}


def _tools():
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def test_every_tool_declares_read_only_hint():
    tools = _tools()
    assert tools
    missing = sorted(name for name, t in tools.items() if not t.annotations or t.annotations.readOnlyHint is None)
    assert missing == []


def test_write_tools_are_not_read_only():
    tools = _tools()
    for name in WRITES_SOURCE | WRITES_GRAPH:
        assert tools[name].annotations.readOnlyHint is False, name
    for name in WRITES_SOURCE:
        assert tools[name].annotations.destructiveHint is True, name
    for name in WRITES_GRAPH:
        assert tools[name].annotations.destructiveHint is False, name


def test_query_tools_are_read_only():
    tools = _tools()
    for name, tool in tools.items():
        if name not in WRITES_SOURCE | WRITES_GRAPH:
            assert tool.annotations.readOnlyHint is True, name
