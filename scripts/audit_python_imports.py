#!/usr/bin/env python3
"""Audit the Python import edges in a built code-review-graph database.

Ground truth comes from CPython's own ``ast`` module, never from the graph:
every ``from ... import ...`` statement is re-resolved here with the import
semantics the interpreter uses, and the resulting file is compared against
the ``IMPORTS_FROM`` edge the parser actually wrote at that line.

Sections
--------
1. relative imports  -- statements with ``level > 0`` (``from .x import y``)
2. absolute imports  -- statements with ``level == 0`` that name a
   repository-local module (stdlib/third-party modules are reported
   separately because they legitimately have no file in the repository)
3. cross-module calls -- ``CALLS`` edges whose callee was bound by a
   relative import; resolved means the target names a file
4. dangling edges    -- share of all edges whose target matches no node
5. path-shaped targets that do not exist on disk, compared case-exactly so
   a case-insensitive filesystem (APFS, NTFS) cannot hide a wrong target

Usage
-----
    uv run --python 3.13 python scripts/audit_python_imports.py \
        --repo . --package code_review_graph \
        --calls-module code_review_graph/changes.py \
        --build-log build.log --json audit.json
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# Path helpers -- shared with tests/test_import_target_integrity.py
# ---------------------------------------------------------------------------

_BUILD_LOG_COUNTER = re.compile(r"Python import resolution: (\{[^}]*\})")
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


def looks_like_a_path(target: str) -> bool:
    """Return whether an edge target claims a file on this filesystem.

    A *resolved* target is always absolute -- the resolver writes the file it
    found. Everything else is a module specifier copied out of the source and
    makes no claim about this machine: ``os``, ``pkg.mod``, ``.relative``,
    ``./dep`` (an unresolved JS import), ``vars/common.yml``,
    ``package:flutter/material.dart``. Only absolute targets are checkable,
    so only absolute targets are checked.
    """
    if not target:
        return False
    base = target.split("::", 1)[0].replace("\\", "/")
    return base.startswith("/") or bool(_WINDOWS_ABSOLUTE.match(base))


def exists_case_exact(path: str) -> bool:
    """Return whether *path* exists with exactly this spelling.

    ``Path.is_file()`` answers "yes" for ``Registry.py`` on a case-insensitive
    filesystem when only ``registry.py`` exists, which is how a wrong import
    target stayed invisible on macOS while being plainly wrong on Linux. Every
    component is therefore compared against the real directory listing.
    """
    candidate = Path(path.split("::", 1)[0])
    if not candidate.is_absolute():
        return False
    if not candidate.is_file():
        return False
    current = candidate
    while current.parent != current:
        try:
            entries = os.listdir(current.parent)
        except OSError:
            return False
        if current.name not in entries:
            return False
        current = current.parent
    return True


# ---------------------------------------------------------------------------
# Ground truth from ast
# ---------------------------------------------------------------------------


def _python_files(root: Path) -> list[Path]:
    skip = {
        ".git", ".venv", "venv", "node_modules", "__pycache__",
        ".code-review-graph", ".mypy_cache", ".pytest_cache", "build", "dist",
    }
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for filename in filenames:
            if filename.endswith(".py"):
                out.append(Path(dirpath) / filename)
    return sorted(out)


def _module_file(base: Path, dotted: Optional[str]) -> Optional[Path]:
    """Resolve ``base`` + a dotted tail to ``.py`` or ``__init__.py``.

    Package before module. ``FileFinder`` checks whether the name is a
    directory holding ``__init__`` *before* it tries any file loader, so with
    both ``pkg/m/__init__.py`` and ``pkg/m.py`` on disk ``import pkg.m`` binds
    the package. Verified against the interpreter:

        $ python3 -c "import pkg.m; print(pkg.m.__file__)"
        .../pkg/m/__init__.py

    The reverse order (what this helper used to do, and what the parser used
    to do) makes the audit agree with the bug instead of catching it.
    """
    target = base if not dotted else base.joinpath(*dotted.split("."))
    candidates = (
        [target / "__init__.py", target.with_suffix(".py")]
        if dotted
        else [target / "__init__.py"]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _expected_module_file(
    source_file: Path, node: ast.ImportFrom, boundary: Path,
) -> Optional[Path]:
    """The file CPython would import from, or None when it is not in the repo."""
    base = source_file.resolve().parent
    for _ in range(max(node.level - 1, 0)):
        if base == boundary:
            return None
        base = base.parent
    if not base.is_relative_to(boundary):
        return None
    if node.level == 0:
        # Absolute: look for the module under the repository root only.
        return _module_file(boundary, node.module)
    return _module_file(base, node.module)


def _relative_names(tree: ast.AST) -> set[str]:
    """Local names bound by relative imports (alias included)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            for alias in node.names:
                if alias.name == "*":
                    continue
                names.add(alias.asname or alias.name)
    return names


def _call_root_and_symbol(func: ast.expr) -> Optional[tuple[str, str]]:
    """(root name, called symbol) for ``a()``/``a.b()``/``a.b.c()``."""
    if isinstance(func, ast.Name):
        return func.id, func.id
    if isinstance(func, ast.Attribute):
        current: ast.expr = func
        while isinstance(current, ast.Attribute):
            current = current.value
        if isinstance(current, ast.Name):
            return current.id, func.attr
    return None


def _relative_call_sites(tree: ast.AST) -> list[tuple[int, str]]:
    """``(line, symbol)`` for every call made through a relative import.

    Ground truth taken from the source, so the denominator does not move
    when the graph gets better at resolving these calls.
    """
    names = _relative_names(tree)
    sites: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        parsed = _call_root_and_symbol(node.func)
        if parsed is None:
            continue
        root, symbol = parsed
        if root in names:
            sites.append((node.lineno, symbol))
    return sites


# ---------------------------------------------------------------------------
# Call-attribution ground truth
#
# ``audit_dangling`` only sees a target that matches NO node. It cannot see a
# target that matches the WRONG node, which is the failure that actually
# reaches a reviewer: ``some_dict.get(...)`` recorded as a call into
# ``ConnectionPool.get`` looks perfectly resolved and is simply false. This
# section re-reads each attributed call with ``ast`` and asks whether the
# source at that line gives any reason for the node it was attributed to.
#
# The test is deliberately more permissive than any resolver: a variable's
# class counts if the annotation or construction appears ANYWHERE in the file,
# in any scope. A call that fails it fails on the source, not on a scope rule.
# ---------------------------------------------------------------------------


class _CallSite:
    """One call expression, classified by what it is called on."""

    __slots__ = ("name", "kind", "detail")

    def __init__(self, name: str, kind: str, detail: str = "") -> None:
        self.name = name
        self.kind = kind          # plain | self | name | expr
        self.detail = detail      # receiver name, or enclosing class


class _FileFacts:
    """Everything one source file says about its own names."""

    __slots__ = (
        "calls", "classes", "class_members", "local_returns",
        "module_bindings", "module_returns", "symbol_sources", "toplevel",
        "var_classes",
    )

    def __init__(self) -> None:
        self.calls: dict[int, list[_CallSite]] = defaultdict(list)
        self.classes: set[str] = set()
        self.class_members: dict[str, set[str]] = defaultdict(set)
        # Return annotations, split the way a reader can see them: any
        # non-class ``def`` for this file's own calls, module-level ``def``s
        # only for a call that reaches this file through an import.
        self.local_returns: dict[str, ast.expr] = {}
        self.module_bindings: dict[str, set[str]] = defaultdict(set)
        self.module_returns: dict[str, ast.expr] = {}
        self.symbol_sources: dict[str, set[str]] = defaultdict(set)
        self.toplevel: set[str] = set()
        self.var_classes: dict[str, set[str]] = defaultdict(set)


def _annotation_class(annotation: Optional[ast.expr]) -> Optional[str]:
    """Class named by an annotation, unwrapping Optional/Annotated/etc."""
    if annotation is None:
        return None
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return annotation.attr
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            parsed = ast.parse(annotation.value, mode="eval").body
        except (SyntaxError, ValueError):
            return None
        return _annotation_class(parsed)
    if isinstance(annotation, ast.Subscript):
        inner = annotation.slice
        if isinstance(inner, ast.Tuple) and inner.elts:
            inner = inner.elts[0]
        return _annotation_class(inner)
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        # PEP 604: ``EmbeddingProvider | None`` is the modern spelling of
        # ``Optional[EmbeddingProvider]``, which this reader already unwraps.
        return (
            _annotation_class(annotation.left)
            or _annotation_class(annotation.right)
        )
    return None


def _assigned_name(target: ast.expr) -> Optional[str]:
    """``x`` and ``self.x`` both bind the name a receiver would be spelled by."""
    if isinstance(target, ast.Name):
        return target.id
    if (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id in ("self", "cls")
    ):
        return target.attr
    return None


def _binding_positions(
    target: ast.expr,
) -> list[tuple[str, tuple[int, ...]]]:
    """Names one target binds, each with its position inside the value.

    ``x = ...`` is the empty path; ``a, b = ...`` numbers its elements;
    ``a, (b, c) = ...`` nests. A starred element ends the numbering, because
    nothing after it has a position anyone can name.
    """
    found: list[tuple[str, tuple[int, ...]]] = []

    def visit(node: ast.expr, path: tuple[int, ...]) -> None:
        if isinstance(node, (ast.Tuple, ast.List)):
            for index, element in enumerate(node.elts):
                if isinstance(element, ast.Starred):
                    return
                visit(element, path + (index,))
            return
        name = _assigned_name(node)
        if name is not None:
            found.append((name, path))

    visit(target, ())
    return found


def _dotted_path(node: ast.expr) -> Optional[str]:
    """``a.b.c`` as text, or None for anything that is not a pure name path."""
    parts: list[str] = []
    current: ast.expr = node
    for _ in range(8):
        if isinstance(current, ast.Name):
            parts.append(current.id)
            return ".".join(reversed(parts))
        if not isinstance(current, ast.Attribute):
            return None
        parts.append(current.attr)
        current = current.value
    return None


def _constructor_class(func: ast.expr) -> Optional[str]:
    """Class a call expression constructs, bare or dotted, by the same rule.

    ``GraphStore(...)`` and ``pkg.graph.GraphStore(...)`` both name the class
    in their last segment. The capitalisation rule keeps ``os.path.join(...)``
    from claiming a class called ``join``; it mirrors the parser's rule so
    the two can disagree about facts, never about which shapes count.
    """
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute) and _dotted_path(func.value):
        return func.attr if func.attr[:1].isupper() else None
    return None


def _annotation_at_path(
    annotation: Optional[ast.expr], path: tuple[int, ...],
) -> Optional[ast.expr]:
    """Walk a ``tuple[...]`` annotation down to one position."""
    current = annotation
    for index in path:
        if isinstance(current, ast.Constant) and isinstance(current.value, str):
            try:
                current = ast.parse(current.value, mode="eval").body
            except (SyntaxError, ValueError):
                return None
        if not isinstance(current, ast.Subscript):
            return None
        head = current.value
        head_name = (
            head.id if isinstance(head, ast.Name)
            else head.attr if isinstance(head, ast.Attribute)
            else None
        )
        if head_name not in ("Tuple", "tuple"):
            return None
        inner = current.slice
        if not isinstance(inner, ast.Tuple) or not inner.elts:
            return None
        elements = inner.elts
        if (
            len(elements) == 2
            and isinstance(elements[1], ast.Constant)
            and elements[1].value is Ellipsis
        ):
            current = elements[0]
            continue
        if index >= len(elements):
            return None
        current = elements[index]
    return current


def _file_facts(
    source_file: Path, boundary: Path, cache: dict[str, _FileFacts],
) -> Optional[_FileFacts]:
    key = _posix(source_file)
    if key in cache:
        return cache[key]
    try:
        tree = ast.parse(source_file.read_bytes(), filename=str(source_file))
    except (OSError, SyntaxError, ValueError):
        return None
    facts = _FileFacts()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                target = alias.name if alias.asname else alias.name.split(".", 1)[0]
                resolved = _module_file(boundary, target)
                if resolved is not None:
                    facts.module_bindings[local].add(_posix(resolved))
        elif isinstance(node, ast.ImportFrom):
            package = _expected_module_file(source_file, node, boundary)
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                submodule = (
                    _module_file(package.parent, alias.name)
                    if package is not None and package.name == "__init__.py"
                    else None
                )
                if submodule is not None:
                    facts.module_bindings[local].add(_posix(submodule))
                elif package is not None:
                    facts.symbol_sources[local].add(_posix(package))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = node.args
            for argument in (
                list(arguments.posonlyargs)
                + list(arguments.args)
                + list(arguments.kwonlyargs)
            ):
                class_name = _annotation_class(argument.annotation)
                if class_name:
                    facts.var_classes[argument.arg].add(class_name)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            facts.toplevel.add(node.name)
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.returns is not None
        ):
            facts.module_returns[node.name] = node.returns

    def collect_returns(node: ast.AST) -> None:
        """Every ``def`` outside a class body, with its return annotation."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                continue
            if (
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.returns is not None
            ):
                facts.local_returns[child.name] = child.returns
            collect_returns(child)

    collect_returns(tree)
    # Imports and annotations are in hand now, so a binding can be read
    # against the file's complete account of its own names.
    cache[key] = facts
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AnnAssign, ast.Assign, ast.With, ast.AsyncWith)):
            continue
        bindings: list[tuple[ast.expr, Optional[ast.expr], Optional[ast.expr]]]
        if isinstance(node, ast.AnnAssign):
            bindings = [(node.target, node.value, node.annotation)]
        elif isinstance(node, ast.Assign):
            bindings = [(target, node.value, None) for target in node.targets]
        else:
            # ``with GraphStore(p) as store`` binds exactly like an
            # assignment; only the grammar differs.
            bindings = [
                (item.optional_vars, item.context_expr, None)
                for item in node.items
                if item.optional_vars is not None
            ]
        for target, value, annotation in bindings:
            for name, path in _binding_positions(target):
                for class_name in (
                    _annotation_class(_annotation_at_path(annotation, path)),
                    _value_class(value, path, facts, boundary, cache),
                ):
                    if class_name:
                        facts.var_classes[name].add(class_name)

    def visit(node: ast.AST, class_stack: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                facts.classes.add(child.name)
                for member in child.body:
                    if isinstance(
                        member, (ast.FunctionDef, ast.AsyncFunctionDef),
                    ):
                        facts.class_members[child.name].add(member.name)
                visit(child, class_stack + [child.name])
                continue
            if isinstance(child, ast.Call):
                facts.calls[child.lineno].append(
                    _classify_call(child, class_stack),
                )
            visit(child, class_stack)

    visit(tree, [])
    cache[key] = facts
    return facts


def _callee_return_annotation(
    func: ast.expr,
    facts: _FileFacts,
    boundary: Path,
    cache: dict[str, _FileFacts],
) -> Optional[ast.expr]:
    """Return annotation the source writes for the callee of one call.

    A ``def`` in this file, the module a ``from X import f`` names, or the
    module an ``import m`` binding names for ``m.f(...)``. Annotations only:
    no function body is read here or anywhere else.
    """
    if isinstance(func, ast.Name):
        local = facts.local_returns.get(func.id)
        if local is not None:
            return local
        for origin in _expand_reexports(
            facts.symbol_sources.get(func.id, set()), func.id, boundary, cache,
        ):
            other = _file_facts(Path(origin), boundary, cache)
            if other is not None and func.id in other.module_returns:
                return other.module_returns[func.id]
        return None
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        modules = facts.module_bindings.get(func.value.id, set())
        if len(modules) != 1:
            return None
        other = _file_facts(Path(next(iter(modules))), boundary, cache)
        return None if other is None else other.module_returns.get(func.attr)
    return None


def _value_class(
    value: Optional[ast.expr],
    path: tuple[int, ...],
    facts: _FileFacts,
    boundary: Path,
    cache: dict[str, _FileFacts],
) -> Optional[str]:
    """Class the source says a bound value is, at one position inside it."""
    if value is None:
        return None
    if isinstance(value, (ast.Tuple, ast.List)) and path:
        if path[0] >= len(value.elts):
            return None
        return _value_class(value.elts[path[0]], path[1:], facts, boundary, cache)
    if not isinstance(value, ast.Call):
        return None
    annotation = _callee_return_annotation(value.func, facts, boundary, cache)
    if annotation is not None:
        return _annotation_class(_annotation_at_path(annotation, path))
    if path:
        return None
    return _constructor_class(value.func)


def _classify_call(call: ast.Call, class_stack: list[str]) -> _CallSite:
    func = call.func
    if isinstance(func, ast.Name):
        return _CallSite(func.id, "plain")
    if isinstance(func, ast.Attribute):
        receiver = func.value
        if isinstance(receiver, ast.Call):
            # ``CodeParser().parse_file(...)``: the receiver has no name, but
            # the expression states its own class.
            constructed = _constructor_class(receiver.func)
            return _CallSite(
                func.attr, "expr",
                f"class:{constructed}" if constructed else "",
            )
        if isinstance(receiver, ast.Name):
            if receiver.id in ("self", "cls"):
                return _CallSite(
                    func.attr, "self", class_stack[-1] if class_stack else "",
                )
            return _CallSite(func.attr, "name", receiver.id)
        # ``self.field.m()`` is how a field receiver is spelled.
        if (
            isinstance(receiver, ast.Attribute)
            and isinstance(receiver.value, ast.Name)
            and receiver.value.id in ("self", "cls")
        ):
            return _CallSite(func.attr, "name", receiver.attr)
        if isinstance(receiver, ast.Attribute):
            dotted = _dotted_path(receiver)
            if dotted and dotted.split(".", 1)[0] not in ("self", "cls"):
                # ``pkg.graph.GraphStore(...)`` is a dotted module path, not a
                # member call; the name it calls is a top-level name there.
                return _CallSite(func.attr, "expr", f"module:{dotted}")
        return _CallSite(func.attr, "expr")
    return _CallSite("", "expr")


def _expand_reexports(
    sources: set[str],
    symbol: str,
    boundary: Path,
    cache: dict[str, _FileFacts],
) -> set[str]:
    """Follow re-exports of *symbol* to the file that really defines it.

    ``from .tools import query_graph`` names the package, but the function
    lives in ``tools/query.py`` and the resolver is right to say so; likewise
    ``from .graph import NodeInfo``, which ``graph.py`` imports from
    ``parser.py``. Each hop follows the SAME name, so the set stays tight.
    """
    seen = set(sources)
    frontier = list(sources)
    for _ in range(3):
        following: list[str] = []
        for module in frontier:
            facts = _file_facts(Path(module), boundary, cache)
            if facts is None:
                continue
            for origin in (
                facts.symbol_sources.get(symbol, set())
                | facts.module_bindings.get(symbol, set())
            ):
                if origin not in seen:
                    seen.add(origin)
                    following.append(origin)
        if not following:
            break
        frontier = following
    return seen


def _attribution_is_justified(
    site: _CallSite,
    caller_file: str,
    target_file: str,
    target_symbol: str,
    facts: _FileFacts,
    target_facts: Optional[_FileFacts],
    boundary: Path,
    cache: dict[str, _FileFacts],
) -> bool:
    """Does the source at this call site give a reason for this node?"""
    leaf = target_symbol.rsplit(".", 1)[-1]
    parent = target_symbol.rsplit(".", 1)[0] if "." in target_symbol else None
    if site.name != leaf:
        return False
    if site.kind == "plain":
        if target_file == caller_file:
            return True
        return target_file in _expand_reexports(
            facts.symbol_sources.get(leaf, set()), leaf, boundary, cache,
        )
    if site.kind == "self":
        return (
            target_file == caller_file
            and parent is not None
            and parent in facts.classes
        )
    if site.kind == "name":
        receiver = site.detail
        if parent is None and target_file in _expand_reexports(
            facts.module_bindings.get(receiver, set()), leaf, boundary, cache,
        ):
            # ``m.f()`` through a module binding: ``f`` must be a top-level
            # name of that module.
            return target_facts is None or leaf in target_facts.toplevel
        if parent is not None and parent in facts.var_classes.get(receiver, set()):
            return True
        # ``Klass.method()``: the receiver names the class itself.
        return parent is not None and receiver == parent
    if site.kind == "expr":
        # A nameless receiver still says what it is in two shapes.
        if site.detail.startswith("class:"):
            return parent is not None and parent == site.detail[len("class:"):]
        if site.detail.startswith("module:"):
            dotted = site.detail[len("module:"):]
            root = dotted.split(".", 1)[0]
            if root not in facts.module_bindings:
                return False
            resolved = _module_file(boundary, dotted)
            if resolved is None or _posix(resolved) != target_file:
                return False
            return parent is None and (
                target_facts is None or leaf in target_facts.toplevel
            )
    return False


def audit_call_attribution(
    conn: sqlite3.Connection, repo_root: Path,
) -> dict[str, Any]:
    """Precision of Python CALLS edges that DO name a node.

    ``audit_dangling`` scores a target matching no node; this scores a target
    matching the wrong one. An edge counts as judged when the call at that
    line calls something with the target's own leaf name; otherwise the
    parser and ``ast`` disagree about the line and the edge is set aside.
    """
    cache: dict[str, _FileFacts] = {}
    judged = 0
    justified = 0
    unjudged = 0
    offenders: list[dict[str, Any]] = []
    by_target: dict[str, int] = defaultdict(int)

    for row in conn.execute(
        "SELECT e.file_path, e.line, e.target_qualified FROM edges e "
        "WHERE e.kind = 'CALLS' AND e.file_path LIKE '%.py' "
        "AND instr(e.target_qualified, '::') > 0 "
        "AND EXISTS ("
        "  SELECT 1 FROM nodes n WHERE n.qualified_name = e.target_qualified"
        ")"
    ):
        caller_file = row["file_path"]
        target_file, _, target_symbol = row["target_qualified"].partition("::")
        if not target_symbol:
            continue
        facts = _file_facts(Path(caller_file), repo_root, cache)
        if facts is None:
            continue
        sites = facts.calls.get(row["line"], [])
        leaf = target_symbol.rsplit(".", 1)[-1]
        matching = [site for site in sites if site.name == leaf]
        if not matching:
            unjudged += 1
            continue
        target_facts = (
            _file_facts(Path(target_file), repo_root, cache)
            if target_file.endswith(".py")
            else None
        )
        judged += 1
        if any(
            _attribution_is_justified(
                site, caller_file, target_file, target_symbol,
                facts, target_facts, repo_root, cache,
            )
            for site in matching
        ):
            justified += 1
        else:
            by_target[row["target_qualified"]] += 1
            if len(offenders) < 25:
                offenders.append({
                    "file": caller_file,
                    "line": row["line"],
                    "target": row["target_qualified"],
                    "receiver": matching[0].detail or matching[0].kind,
                })

    worst = sorted(by_target.items(), key=lambda item: -item[1])[:15]
    return {
        "attributed_calls_judged": judged,
        "attributed_calls_justified": justified,
        "attributed_calls_unjustified": judged - justified,
        "attributed_calls_precision_pct": _pct(justified, judged),
        "attributed_calls_unjudged": unjudged,
        "worst_targets": [
            {"target": target, "unjustified": count} for target, count in worst
        ],
        "examples": offenders,
    }


# ---------------------------------------------------------------------------
# Graph access
# ---------------------------------------------------------------------------


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _import_edges(conn: sqlite3.Connection) -> dict[tuple[str, int], list[str]]:
    edges: dict[tuple[str, int], list[str]] = defaultdict(list)
    for row in conn.execute(
        "SELECT file_path, line, target_qualified FROM edges "
        "WHERE kind = 'IMPORTS_FROM'"
    ):
        edges[(row["file_path"], row["line"])].append(row["target_qualified"])
    return edges


def _posix(path: Path) -> str:
    return path.resolve().as_posix()


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def audit_imports(
    conn: sqlite3.Connection, package_root: Path, boundary: Path,
) -> dict[str, Any]:
    edges = _import_edges(conn)
    stats = {
        "relative_total": 0,
        "relative_correct": 0,
        "relative_wrong": 0,
        "relative_missing": 0,
        "relative_unresolvable_on_disk": 0,
        "absolute_local_total": 0,
        "absolute_local_correct": 0,
        "absolute_external_total": 0,
    }
    wrong_examples: list[dict[str, Any]] = []

    for source_file in _python_files(package_root):
        try:
            tree = ast.parse(source_file.read_bytes(), filename=str(source_file))
        except (SyntaxError, ValueError):
            continue
        key_file = _posix(source_file)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            expected = _expected_module_file(source_file, node, boundary)
            targets = edges.get((key_file, node.lineno), [])
            if node.level > 0:
                stats["relative_total"] += 1
                if expected is None:
                    stats["relative_unresolvable_on_disk"] += 1
                    continue
                if _posix(expected) in targets:
                    stats["relative_correct"] += 1
                elif not targets:
                    stats["relative_missing"] += 1
                else:
                    stats["relative_wrong"] += 1
                    if len(wrong_examples) < 15:
                        wrong_examples.append({
                            "file": key_file,
                            "line": node.lineno,
                            "expected": _posix(expected),
                            "actual": targets,
                        })
            elif expected is None:
                stats["absolute_external_total"] += 1
            else:
                stats["absolute_local_total"] += 1
                if _posix(expected) in targets:
                    stats["absolute_local_correct"] += 1

    stats["relative_correct_pct"] = _pct(
        stats["relative_correct"], stats["relative_total"],
    )
    stats["wrong_examples"] = wrong_examples
    return stats


def audit_calls(
    conn: sqlite3.Connection, repo_root: Path, modules: Iterable[str],
) -> dict[str, Any]:
    total = 0
    resolved = 0
    per_module: dict[str, str] = {}
    for rel in modules:
        source_file = (repo_root / rel).resolve()
        if not source_file.is_file():
            continue
        try:
            tree = ast.parse(source_file.read_bytes(), filename=str(source_file))
        except (SyntaxError, ValueError):
            continue
        sites = _relative_call_sites(tree)
        if not sites:
            continue
        by_line: dict[int, list[str]] = defaultdict(list)
        for row in conn.execute(
            "SELECT line, target_qualified FROM edges "
            "WHERE kind = 'CALLS' AND file_path = ?",
            (_posix(source_file),),
        ):
            by_line[row["line"]].append(row["target_qualified"])

        module_total = len(sites)
        module_resolved = 0
        for line, symbol in sites:
            for target in by_line.get(line, ()):
                head, sep, bound = target.partition("::")
                if not sep or not looks_like_a_path(head):
                    continue
                if bound == symbol or bound.split(".")[-1] == symbol:
                    module_resolved += 1
                    break
        total += module_total
        resolved += module_resolved
        per_module[rel] = f"{module_resolved}/{module_total}"
    return {
        "calls_total": total,
        "calls_resolved": resolved,
        "calls_resolved_pct": _pct(resolved, total),
        "per_module": per_module,
    }


def audit_python_import_edges(conn: sqlite3.Connection) -> dict[str, Any]:
    """How many Python IMPORTS_FROM edges actually name a repository file.

    This is the number ``imports_resolved`` in the build log is a proxy for.
    The counter itself only reports what the *post-build* suffix index
    recovered, so it stays at zero once the parser resolves these at parse
    time -- once because nothing could be recovered, once because nothing is
    left to recover. This metric distinguishes the two.
    """
    total = 0
    resolved = 0
    for row in conn.execute(
        "SELECT e.target_qualified FROM edges e "
        "JOIN nodes f ON f.kind = 'File' AND f.file_path = e.file_path "
        "WHERE e.kind = 'IMPORTS_FROM' AND f.language = 'python'"
    ):
        total += 1
        if looks_like_a_path(row["target_qualified"]):
            resolved += 1
    return {
        "python_import_edges": total,
        "python_import_edges_resolved": resolved,
        "python_import_edges_resolved_pct": _pct(resolved, total),
    }


def audit_dangling(conn: sqlite3.Connection) -> dict[str, Any]:
    total = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    dangling = conn.execute(
        "SELECT COUNT(*) FROM edges e "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM nodes n WHERE n.qualified_name = e.target_qualified"
        ")"
    ).fetchone()[0]
    imports_total = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE kind = 'IMPORTS_FROM'"
    ).fetchone()[0]
    imports_dangling = conn.execute(
        "SELECT COUNT(*) FROM edges e WHERE e.kind = 'IMPORTS_FROM' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM nodes n WHERE n.qualified_name = e.target_qualified"
        ")"
    ).fetchone()[0]
    return {
        "edges_total": total,
        "edges_dangling": dangling,
        "edges_dangling_pct": _pct(dangling, total),
        "imports_total": imports_total,
        "imports_dangling": imports_dangling,
        "imports_dangling_pct": _pct(imports_dangling, imports_total),
    }


def missing_path_targets(
    conn: sqlite3.Connection, limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """IMPORTS_FROM targets shaped like a path that no such file answers to."""
    offenders: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT DISTINCT file_path, line, target_qualified FROM edges "
        "WHERE kind = 'IMPORTS_FROM'"
    ):
        target = row["target_qualified"]
        if not looks_like_a_path(target):
            continue
        if exists_case_exact(target):
            continue
        offenders.append({
            "file": row["file_path"],
            "line": row["line"],
            "target": target,
        })
        if limit is not None and len(offenders) >= limit:
            break
    return offenders


def build_log_counters(log_path: Path) -> dict[str, Any]:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    match = None
    for match in _BUILD_LOG_COUNTER.finditer(text):
        pass
    if match is None:
        return {}
    try:
        return ast.literal_eval(match.group(1))
    except (SyntaxError, ValueError):
        return {}


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

DEFAULT_CALLS_MODULES = [
    "code_review_graph/changes.py",
    "code_review_graph/flows.py",
    "code_review_graph/hints.py",
    "code_review_graph/refactor.py",
    "code_review_graph/search.py",
    "code_review_graph/communities.py",
    "code_review_graph/wiki.py",
]


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=".", help="repository root")
    ap.add_argument(
        "--db", default=None,
        help="graph.db (default: <repo>/.code-review-graph/graph.db)",
    )
    ap.add_argument(
        "--package", default=None,
        help="subdirectory to audit imports in (default: the whole repo)",
    )
    ap.add_argument(
        "--calls-module", action="append", default=None, dest="calls_modules",
        help="repo-relative .py file to audit cross-module CALLS in (repeatable)",
    )
    ap.add_argument("--build-log", default=None, help="build output to read counters from")
    ap.add_argument("--json", default=None, help="write the full report here")
    ap.add_argument("--label", default="", help="label printed with the report")
    args = ap.parse_args(argv)

    repo_root = Path(args.repo).resolve()
    db_path = (
        Path(args.db).resolve()
        if args.db
        else repo_root / ".code-review-graph" / "graph.db"
    )
    if not db_path.is_file():
        print(f"no graph database at {db_path}", file=sys.stderr)
        return 2
    package_root = (
        (repo_root / args.package).resolve() if args.package else repo_root
    )
    calls_modules = args.calls_modules or DEFAULT_CALLS_MODULES

    conn = _connect(db_path)
    try:
        report: dict[str, Any] = {
            "label": args.label,
            "repo": repo_root.as_posix(),
            "package": package_root.as_posix(),
            "imports": audit_imports(conn, package_root, repo_root),
            "calls": audit_calls(conn, repo_root, calls_modules),
            "edges": audit_dangling(conn),
            "python_edges": audit_python_import_edges(conn),
            "attribution": audit_call_attribution(conn, repo_root),
        }
        offenders = missing_path_targets(conn)
        report["missing_path_targets"] = {
            "count": len(offenders),
            "examples": offenders[:15],
        }
    finally:
        conn.close()

    if args.build_log:
        report["build_log"] = build_log_counters(Path(args.build_log).resolve())

    imports = report["imports"]
    calls = report["calls"]
    edges = report["edges"]
    print(f"== python import audit {args.label} ==")
    print(f"repo:    {report['repo']}")
    print(f"package: {report['package']}")
    print(
        f"relative imports correct: {imports['relative_correct']}"
        f"/{imports['relative_total']} ({imports['relative_correct_pct']}%)"
        f"  wrong={imports['relative_wrong']} missing={imports['relative_missing']}"
        f" not-on-disk={imports['relative_unresolvable_on_disk']}"
    )
    print(
        f"absolute repo-local imports correct: "
        f"{imports['absolute_local_correct']}/{imports['absolute_local_total']}"
        f"  (external modules seen: {imports['absolute_external_total']})"
    )
    python_edges = report["python_edges"]
    print(
        "python IMPORTS_FROM edges naming a repository file: "
        f"{python_edges['python_import_edges_resolved']}"
        f"/{python_edges['python_import_edges']}"
        f" ({python_edges['python_import_edges_resolved_pct']}%)"
    )
    print(
        f"cross-module CALLS resolved: {calls['calls_resolved']}"
        f"/{calls['calls_total']} ({calls['calls_resolved_pct']}%)"
    )
    attribution = report["attribution"]
    print(
        "python CALLS attributed to a node, justified by the source: "
        f"{attribution['attributed_calls_justified']}"
        f"/{attribution['attributed_calls_judged']}"
        f" ({attribution['attributed_calls_precision_pct']}%)"
        f"  unjudged={attribution['attributed_calls_unjudged']}"
    )
    for worst in attribution["worst_targets"][:5]:
        print(f"  UNJUSTIFIED x{worst['unjustified']} -> {worst['target']}")
    print(
        f"edges with no matching node: {edges['edges_dangling']}"
        f"/{edges['edges_total']} ({edges['edges_dangling_pct']}%)"
    )
    print(
        f"  of which IMPORTS_FROM: {edges['imports_dangling']}"
        f"/{edges['imports_total']} ({edges['imports_dangling_pct']}%)"
    )
    print(
        "path-shaped IMPORTS_FROM targets missing on disk: "
        f"{report['missing_path_targets']['count']}"
    )
    if report.get("build_log"):
        print(f"build log counters: {report['build_log']}")
    for example in imports["wrong_examples"][:5]:
        print(
            f"  WRONG {example['file']}:{example['line']} "
            f"expected {example['expected']} got {example['actual']}"
        )
    for example in report["missing_path_targets"]["examples"][:5]:
        print(f"  MISSING {example['file']}:{example['line']} -> {example['target']}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
