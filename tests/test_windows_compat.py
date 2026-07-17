"""Cross-platform guards for Windows-only test-suite constraints."""

from __future__ import annotations

import ast
from pathlib import Path


def _qualified_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _is_delete_false_named_tempfile(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if (_qualified_name(node.func) or "").split(".")[-1] != "NamedTemporaryFile":
        return False
    return any(
        keyword.arg == "delete"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is False
        for keyword in node.keywords
    )


def _references_temp_name(node: ast.AST, target: str) -> bool:
    return any(
        isinstance(child, ast.Attribute)
        and child.attr == "name"
        and _qualified_name(child.value) == target
        for child in ast.walk(node)
    )


def test_delete_false_named_tempfiles_close_before_graphstore_reopens_them():
    """Windows forbids reopening a NamedTemporaryFile while its handle is open."""
    failures: list[str] = []
    tests_dir = Path(__file__).parent

    for path in sorted(tests_dir.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        functions = (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        for function in functions:
            nodes = list(ast.walk(function))
            assignments = (
                node
                for node in nodes
                if isinstance(node, (ast.Assign, ast.AnnAssign))
                and _is_delete_false_named_tempfile(node.value)
            )
            for assignment in assignments:
                raw_targets = (
                    assignment.targets
                    if isinstance(assignment, ast.Assign)
                    else [assignment.target]
                )
                targets = [
                    name
                    for target in raw_targets
                    if (name := _qualified_name(target)) is not None
                ]
                for target in targets:
                    close_lines = [
                        node.lineno
                        for node in nodes
                        if isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "close"
                        and _qualified_name(node.func.value) == target
                    ]
                    reopen_lines = [
                        node.lineno
                        for node in nodes
                        if isinstance(node, ast.Call)
                        and (_qualified_name(node.func) or "").split(".")[-1] == "GraphStore"
                        and _references_temp_name(node, target)
                    ]
                    if not close_lines or (
                        reopen_lines and min(close_lines) >= min(reopen_lines)
                    ):
                        failures.append(f"{path.name}:{assignment.lineno} ({target})")

    assert not failures, "Close temporary handles before GraphStore reopens them:\n" + "\n".join(
        failures
    )
