"""Post-build Jedi enrichment for Python call resolution.

After tree-sitter parsing, many method calls on lowercase-receiver variables
are dropped (e.g. ``svc.authenticate()`` where ``svc = factory()``).  Jedi
can resolve these by tracing return types across files.

This module runs as a post-build step: it re-walks Python ASTs to find
dropped calls, uses ``jedi.Script.goto()`` to resolve them, and adds the
resulting CALLS edges to the graph database.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from .parser import CodeParser, EdgeInfo
from .parser import _is_test_file as _parser_is_test_file

logger = logging.getLogger(__name__)

_SELF_NAMES = frozenset({"self", "cls", "super"})


def enrich_jedi_calls(store, repo_root: Path) -> dict:
    """Resolve untracked Python method calls via Jedi.

    Walks Python files, finds ``receiver.method()`` calls that tree-sitter
    dropped (lowercase receiver, not self/cls), resolves them with Jedi,
    and inserts new CALLS edges.

    Returns stats dict with ``resolved`` count.
    """
    try:
        import jedi
    except ImportError:
        logger.info("Jedi not installed, skipping Python enrichment")
        return {"skipped": True, "reason": "jedi not installed"}

    repo_root = Path(repo_root).resolve()

    # Get Python files from the graph — skip early if none
    all_files = store.get_all_files()
    py_files = [f for f in all_files if f.endswith(".py")]

    if not py_files:
        return {"resolved": 0, "files": 0}

    # Scope the Jedi project to Python-only directories to avoid scanning
    # non-Python files (e.g. node_modules, TS sources).  This matters for
    # polyglot monorepos where jedi.Project(path=repo_root) would scan
    # thousands of irrelevant files during initialization.
    py_dirs = sorted({str(Path(f).parent) for f in py_files})
    common_py_root = Path(os.path.commonpath(py_dirs)) if py_dirs else repo_root
    if not str(common_py_root).startswith(str(repo_root)):
        common_py_root = repo_root
    project = jedi.Project(
        path=str(common_py_root),
        added_sys_path=[str(repo_root)],
        smart_sys_path=False,
    )

    # Pre-parse all Python files to find which ones have pending method calls.
    # This avoids expensive Jedi Script creation for files with nothing to resolve.
    parser = CodeParser()
    ts_parser = parser._get_parser("python")
    if not ts_parser:
        return {"resolved": 0, "files": 0}

    # Build set of method names that actually exist in project code.
    # No point asking Jedi to resolve `logger.getLogger()` if no project
    # file defines a function called `getLogger`.
    project_func_names = {
        r["name"]
        for r in store._conn.execute(
            "SELECT DISTINCT name FROM nodes WHERE kind IN ('Function', 'Test')"
        ).fetchall()
    }

    files_with_pending: list[tuple[str, bytes, list]] = []
    total_skipped = 0
    for file_path in py_files:
        try:
            source = Path(file_path).read_bytes()
        except (OSError, PermissionError):
            continue
        tree = ts_parser.parse(source)
        is_test = _parser_is_test_file(file_path)
        pending = _find_untracked_method_calls(tree.root_node, is_test)
        if pending:
            # Only keep calls whose method name exists in project code
            filtered = [p for p in pending if p[2] in project_func_names]
            total_skipped += len(pending) - len(filtered)
            if filtered:
                files_with_pending.append((file_path, source, filtered))

    if not files_with_pending:
        return {"resolved": 0, "files": 0}

    logger.debug(
        "Jedi: %d/%d Python files have pending calls (%d calls skipped — no project target)",
        len(files_with_pending), len(py_files), total_skipped,
    )

    resolved_count = 0
    files_enriched = 0
    errors = 0

    for file_path, source, pending in files_with_pending:
        source_text = source.decode("utf-8", errors="replace")

        # Get existing CALLS edges for this file to skip duplicates
        existing = set()
        for edge in _get_file_call_edges(store, file_path):
            existing.add((edge.source_qualified, edge.line))

        # Get function nodes from DB for enclosing-function lookup
        func_nodes = [
            n for n in store.get_nodes_by_file(file_path)
            if n.kind in ("Function", "Test")
        ]

        # Create Jedi script once per file
        try:
            script = jedi.Script(source_text, path=file_path, project=project)
        except Exception as e:
            logger.debug("Jedi failed to load %s: %s", file_path, e)
            errors += 1
            continue

        file_resolved = 0
        for jedi_line, col, _method_name, _enclosing_name in pending:
            # Find enclosing function qualified name
            enclosing = _find_enclosing(func_nodes, jedi_line)
            if not enclosing:
                enclosing = file_path  # module-level

            # Skip if we already have a CALLS edge from this source at this line
            if (enclosing, jedi_line) in existing:
                continue

            # Ask Jedi to resolve
            try:
                names = script.goto(jedi_line, col)
            except Exception:  # nosec B112 - Jedi may fail on malformed code
                continue

            if not names:
                continue

            name = names[0]
            if not name.module_path:
                continue

            module_path = Path(name.module_path).resolve()

            # Only emit edges for project-internal definitions
            try:
                module_path.relative_to(repo_root)
            except ValueError:
                continue

            # Build qualified target: file_path::Class.method or file_path::func
            target_file = str(module_path)
            parent = name.parent()
            if parent and parent.type == "class":
                target = f"{target_file}::{parent.name}.{name.name}"
            else:
                target = f"{target_file}::{name.name}"

            store.upsert_edge(EdgeInfo(
                kind="CALLS",
                source=enclosing,
                target=target,
                file_path=file_path,
                line=jedi_line,
            ))
            existing.add((enclosing, jedi_line))
            file_resolved += 1

        if file_resolved:
            files_enriched += 1
            resolved_count += file_resolved

    if resolved_count:
        store.commit()
        logger.info(
            "Jedi enrichment: resolved %d calls in %d files",
            resolved_count, files_enriched,
        )

    return {
        "resolved": resolved_count,
        "files": files_enriched,
        "errors": errors,
    }


def _get_file_call_edges(store, file_path: str):
    """Get all CALLS edges originating from a file."""
    conn = store._conn
    rows = conn.execute(
        "SELECT * FROM edges WHERE file_path = ? AND kind = 'CALLS'",
        (file_path,),
    ).fetchall()
    from .graph import GraphEdge
    return [
        GraphEdge(
            id=r["id"], kind=r["kind"],
            source_qualified=r["source_qualified"],
            target_qualified=r["target_qualified"],
            file_path=r["file_path"], line=r["line"],
            extra={},
        )
        for r in rows
    ]


def _find_enclosing(func_nodes, line: int) -> Optional[str]:
    """Find the qualified name of the function enclosing a given line."""
    best = None
    best_span = float("inf")
    for node in func_nodes:
        if node.line_start <= line <= node.line_end:
            span = node.line_end - node.line_start
            if span < best_span:
                best = node.qualified_name
                best_span = span
    return best


def _find_untracked_method_calls(root, is_test_file: bool = False):
    """Walk Python AST to find method calls the parser would have dropped.

    Returns list of (jedi_line, col, method_name, enclosing_func_name) tuples.
    Jedi_line is 1-indexed, col is 0-indexed.
    """
    results: list[tuple[int, int, str, Optional[str]]] = []
    _walk_calls(root, results, is_test_file, enclosing_func=None)
    return results


def _walk_calls(node, results, is_test_file, enclosing_func):
    """Recursively walk AST collecting dropped method calls."""
    # Track enclosing function scope
    if node.type == "function_definition":
        name = None
        for child in node.children:
            if child.type == "identifier":
                name = child.text.decode("utf-8", errors="replace")
                break
        for child in node.children:
            _walk_calls(child, results, is_test_file, name or enclosing_func)
        return

    if node.type == "decorated_definition":
        for child in node.children:
            _walk_calls(child, results, is_test_file, enclosing_func)
        return

    # Check for call expressions with attribute access
    if node.type == "call":
        first = node.children[0] if node.children else None
        if first and first.type == "attribute":
            _check_dropped_call(first, results, is_test_file, enclosing_func)

    for child in node.children:
        _walk_calls(child, results, is_test_file, enclosing_func)


def _check_dropped_call(attr_node, results, is_test_file, enclosing_func):
    """Check if an attribute-based call was dropped by the parser."""
    children = attr_node.children
    if len(children) < 2:
        return

    receiver = children[0]
    # Only handle simple identifier receivers
    if receiver.type != "identifier":
        return

    receiver_text = receiver.text.decode("utf-8", errors="replace")

    # The parser keeps: self/cls/super calls and uppercase-receiver calls
    # The parser keeps: calls handled by typed-var enrichment (but those are
    # separate edges -- we check for duplicates via existing-edge set)
    if receiver_text in _SELF_NAMES:
        return
    if receiver_text[:1].isupper():
        return
    if is_test_file:
        return  # test files already track all calls

    # Find the method name identifier
    method_node = children[-1]
    if method_node.type != "identifier":
        return

    row, col = method_node.start_point  # 0-indexed
    method_name = method_node.text.decode("utf-8", errors="replace")
    results.append((row + 1, col, method_name, enclosing_func))
