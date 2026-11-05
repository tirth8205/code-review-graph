"""Recommendations must name tools exposed by the MCP server."""

import ast
from pathlib import Path

from code_review_graph.hints import _INTENT_TOOLS, _WORKFLOW, SessionState, generate_hints


def test_all_hints_name_registered_mcp_tools():
    tree = ast.parse((Path(__file__).parents[1] / "code_review_graph/main.py").read_text())
    registered = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "mcp"
            and decorator.func.attr == "tool"
            for decorator in node.decorator_list
        )
    }
    assert registered
    referenced = set(_WORKFLOW)
    for intent in _INTENT_TOOLS.values():
        referenced.update(intent)
    for tool_name in _WORKFLOW:
        hints = generate_hints(tool_name, {}, SessionState())
        referenced.update(step["tool"] for step in hints["next_steps"])
    assert referenced <= registered, sorted(referenced - registered)
