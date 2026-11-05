"""Graph-powered refactoring operations.

Provides rename previews, dead code detection, refactoring suggestions,
and safe application of refactoring edits to source files. All file writes
go through a preview-then-apply workflow with expiry enforcement and path
traversal prevention.
"""

from __future__ import annotations

import ast
import functools
import io
import logging
import re
import sys
import threading
import time
import tokenize
import uuid
import warnings
from collections import deque
from pathlib import Path, PurePosixPath
from typing import Any, Optional, Union

from .flows import _has_framework_decorator, _matches_entry_name
from .graph import GraphNode, GraphStore, _sanitize_name
from .parser import _STORYBOOK_FILE_RE, CodeParser, normalize_file_path
from .parser import is_test_file as _is_test_file
from .parser import repo_relative_path as _repo_relative

logger = logging.getLogger(__name__)

# Base class names that indicate a framework-managed class (ORM models,
# Pydantic schemas, settings).  Classes inheriting from these are invoked
# via metaclass/framework magic and should not be flagged as dead code.
_FRAMEWORK_BASE_CLASSES = frozenset({
    "Base", "DeclarativeBase", "Model", "BaseModel", "BaseSettings",
    "db.Model", "TableBase",
    # AWS CDK constructs -- instantiated by CDK app wiring, not explicit CALLS.
    "Stack", "NestedStack", "Construct", "Resource",
})

# Class name suffixes that indicate CDK/IaC constructs.
# These are instantiated by framework wiring, not direct CALLS edges.
# Used as fallback when INHERITS edges to external base classes are absent.
_CDK_CLASS_SUFFIXES = ("Stack", "Construct", "Pipeline", "Resources", "Layer")

# Patterns for mock/stub variables in test files that should not be flagged dead.
_MOCK_NAME_RE = re.compile(
    r"^(mock[A-Z_]|Mock[A-Z]|createMock[A-Z])|"  # mockDynamoClient, MockService, createMockX
    r"(Mock|Stub|Fake|Spy)$",                      # s3ClientMock, dbStub
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Thread-safe pending refactors storage
# ---------------------------------------------------------------------------

_refactor_lock = threading.Lock()
_pending_refactors: dict[str, dict] = {}
REFACTOR_EXPIRY_SECONDS = 600  # 10 minutes


def _cleanup_expired() -> int:
    """Remove expired refactors from the pending dict.  Returns count removed."""
    now = time.time()
    expired = [
        rid for rid, r in _pending_refactors.items()
        if now - r["created_at"] > REFACTOR_EXPIRY_SECONDS
    ]
    for rid in expired:
        del _pending_refactors[rid]
    return len(expired)


# ---------------------------------------------------------------------------
# 1. rename_preview
# ---------------------------------------------------------------------------

# Lines searched below ``line_start`` for a definition's name. Decorators,
# annotations and modifiers can start a definition above the line that
# names it.
_DEF_SEARCH_LINES = 20

_PY_SUFFIXES = (".py", ".pyi", ".pyw")
_PY_MODULE_ENDINGS = (".py", ".pyi", ".pyw", "/__init__.py", "/__init__.pyi")
_JS_FAMILY = frozenset({"javascript", "typescript", "tsx"})
_STAR_IMPORT_RE = re.compile(rb"\bimport\s+\*")
_DEF_KEYWORD_RE = re.compile(r"(?:async\s+)?def\s+|class\s+")
# Tree-sitter leaf types that spell a name in code (never a string or comment).
_JS_NAME_LEAVES = frozenset({
    "identifier", "type_identifier",
    "shorthand_property_identifier", "shorthand_property_identifier_pattern",
})
# A JS/TS identifier is a declaration when it fills one of these fields...
_JS_DECLARATION_FIELDS: dict[str, tuple[str, ...]] = {
    "variable_declarator": ("name",),
    "function_declaration": ("name",),
    "function_signature": ("name",),
    "generator_function_declaration": ("name",),
    "function_expression": ("name",),
    "function": ("name",),
    "generator_function": ("name",),
    "class_declaration": ("name",),
    "abstract_class_declaration": ("name",),
    "class": ("name",),
    "interface_declaration": ("name",),
    "type_alias_declaration": ("name",),
    "enum_declaration": ("name",),
    "type_parameter": ("name",),
    "required_parameter": ("pattern",),
    "optional_parameter": ("pattern",),
    "arrow_function": ("parameter",),
    "catch_clause": ("parameter",),
    "assignment_pattern": ("left",),
    "pair_pattern": ("value",),
    "for_in_statement": ("left",),
}
# ...or sits directly inside one of these.
_JS_DECLARATION_PARENTS = frozenset({
    "formal_parameters", "array_pattern", "rest_pattern",
    "import_clause", "namespace_import",
})
# Where a JS/TS declaration is visible: ``var`` and parameters to the
# function, ``let``/``const``/``function``/``class`` to the enclosing block.
_JS_FUNCTION_SCOPES = frozenset({
    "program", "function_declaration", "function_expression", "function",
    "arrow_function", "method_definition", "generator_function_declaration",
    "generator_function",
})
_JS_BLOCK_SCOPES = _JS_FUNCTION_SCOPES | frozenset({
    "statement_block", "for_statement", "for_in_statement", "catch_clause",
    "class_body", "switch_body",
})
_JS_NAMED_EXPRESSIONS = frozenset({
    "function_expression", "function", "generator_function", "class",
})


def _ident_pattern(name: str) -> re.Pattern[str]:
    """Match *name* as a whole identifier, never inside a longer one."""
    left = r"(?<![\w$])" if re.match(r"[\w$]", name[:1]) else ""
    right = r"(?![\w$])" if re.match(r"[\w$]", name[-1:]) else ""
    return re.compile(left + re.escape(name) + right)


def _decode_source(data: bytes) -> Optional[tuple[str, bool]]:
    """Decode *data* as UTF-8, returning ``(text, had_bom)``.

    ``None`` means the file is not UTF-8. Such a file is never rewritten:
    decoding it with replacement characters and writing it back would
    corrupt every byte that did not decode.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if text.startswith("\ufeff"):
        return text[1:], True
    return text, False


class _Source:
    """One file as a rename preview sees it, read once.

    Lines are split on ``\\n`` only, so a CRLF line keeps its ``\\r`` and
    line numbers agree with the parsers. Columns are 0-based characters.
    """

    def __init__(self, path: str, text: str) -> None:
        self.path = path
        self.text = text
        self.lines = text.split("\n")
        self.data = text.encode("utf-8")
        self._py_tree: Optional[ast.Module] = None
        self._py_parsed = False
        self._py_tokens: Optional[list[tuple[int, int, bool]]] = None
        self._py_tokens_for: Optional[str] = None
        self._py_calls: dict[int, list[tuple[int, int, bool]]] = {}
        self._py_calls_for: Optional[str] = None

    def line(self, lineno: int) -> str:
        if 1 <= lineno <= len(self.lines):
            return self.lines[lineno - 1]
        return ""

    def char_col(self, lineno: int, byte_col: int) -> int:
        """Character column of a UTF-8 byte column on *lineno*."""
        raw = self.line(lineno).encode("utf-8")[:byte_col]
        return len(raw.decode("utf-8", errors="replace"))

    def python_tree(self) -> Optional[ast.Module]:
        if not self._py_parsed:
            self._py_parsed = True
            # A lone CR is a line break to Python but not to ``split("\n")``,
            # so line numbers would disagree; leave such a file to the
            # lexical fallback.
            if "\r" not in self.text.replace("\r\n", ""):
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        self._py_tree = ast.parse(self.text, filename=self.path)
                except (SyntaxError, ValueError, RecursionError, MemoryError):
                    self._py_tree = None
        return self._py_tree

    def python_name_tokens(self, name: str) -> Optional[list[tuple[int, int, bool]]]:
        """``(line, col, is_attribute)`` for every NAME token spelling *name*.

        Comments and strings are separate tokens, so they never match.
        """
        if self._py_tokens_for != name:
            self._py_tokens_for = name
            found: list[tuple[int, int, bool]] = []
            prev = ""
            skip = {tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT,
                    tokenize.INDENT, tokenize.DEDENT}
            try:
                for tok in tokenize.generate_tokens(io.StringIO(self.text).readline):
                    if tok.type == tokenize.NAME and tok.string == name:
                        found.append((tok.start[0], tok.start[1], prev == "."))
                    if tok.type not in skip:
                        prev = tok.string
                if sys.version_info < (3, 12):
                    # An f-string is one STRING token before 3.12; the names
                    # in its expressions come from the AST instead.
                    found.extend(self._python_fstring_names(name))
                self._py_tokens = found
            except (tokenize.TokenError, SyntaxError):
                self._py_tokens = None
        return self._py_tokens

    def _python_name_at(self, node: ast.AST, name: str) -> Optional[tuple[int, int, bool]]:
        """``(line, col, is_attribute)`` where *node* spells *name*, if it does."""
        if isinstance(node, ast.Name) and node.id == name:
            return node.lineno, self.char_col(node.lineno, node.col_offset), False
        if (
            isinstance(node, ast.Attribute) and node.attr == name
            and node.end_lineno is not None and node.end_col_offset is not None
        ):
            # The attribute ends the expression, whatever spacing precedes it.
            start = node.end_col_offset - len(name.encode("utf-8"))
            return node.end_lineno, self.char_col(node.end_lineno, start), True
        return None

    def _python_fstring_names(self, name: str) -> list[tuple[int, int, bool]]:
        tree = self.python_tree()
        found: set[tuple[int, int, bool]] = set()
        for node in ast.walk(tree) if tree is not None else ():
            if isinstance(node, ast.JoinedStr):
                for inner in ast.walk(node):
                    site = self._python_name_at(inner, name)
                    if site is not None:
                        found.add(site)
        return sorted(found)

    def python_call_names(self, name: str) -> dict[int, list[tuple[int, int, bool]]]:
        """Calls of *name*, keyed by the line their call expression starts on.

        The parser dates a call edge by that line, so a call chained over
        several lines (``(obj  # comment`` then ``.name())``) sits on the
        receiver's line while the name is on a later one.
        """
        if self._py_calls_for != name:
            self._py_calls_for = name
            calls: dict[int, list[tuple[int, int, bool]]] = {}
            tree = self.python_tree()
            for node in ast.walk(tree) if tree is not None else ():
                if isinstance(node, ast.Call):
                    site = self._python_name_at(node.func, name)
                    if site is not None:
                        calls.setdefault(node.lineno, []).append(site)
            self._py_calls = calls
        return self._py_calls


class _Sources:
    """Files read, parsed and language-detected at most once per preview."""

    def __init__(self, repo_root: Optional[str]) -> None:
        self._root = repo_root
        self._files: dict[str, Optional[_Source]] = {}
        self._trees: dict[str, Any] = {}
        self._parser: Optional[CodeParser] = None

    @property
    def parser(self) -> CodeParser:
        if self._parser is None:
            self._parser = CodeParser(Path(self._root) if self._root else None)
        return self._parser

    def get(self, path: str) -> Optional[_Source]:
        if path not in self._files:
            source: Optional[_Source] = None
            try:
                decoded = _decode_source(Path(path).read_bytes())
            except OSError:
                decoded = None
            if decoded is not None:
                source = _Source(path, decoded[0])
            self._files[path] = source
        return self._files[path]

    def language(self, path: str) -> Optional[str]:
        try:
            return self.parser.detect_language(Path(path))
        except (OSError, ValueError):
            return None

    def tree(self, path: str) -> Any:
        """Tree-sitter tree for *path*, or ``None`` without a grammar."""
        if path not in self._trees:
            tree = None
            source = self.get(path)
            language = self.language(path)
            if source is not None and language:
                ts_parser = self.parser._get_parser(language)
                if ts_parser is not None:
                    try:
                        tree = ts_parser.parse(source.data)
                    except (ValueError, TypeError, RuntimeError):
                        tree = None
            self._trees[path] = tree
        return self._trees[path]

    def resolve_js_module(self, spec: str, from_file: str) -> Optional[str]:
        language = self.language(from_file) or "typescript"
        try:
            return self.parser._resolve_module_to_file(spec, from_file, language)
        except (OSError, ValueError):
            return None


# One exact place to rename: (line, 0-based column, site, replacement text).
# The replacement is ``None`` for the plain new name.
_Occurrence = tuple[int, int, str, Optional[str]]


def _import_targets_by_line(store: GraphStore, file_path: str) -> dict[int, set[str]]:
    """IMPORTS_FROM targets of *file_path*, keyed by the statement's line."""
    by_line: dict[int, set[str]] = {}
    for edge in store.get_edges_by_source(file_path):
        if edge.kind == "IMPORTS_FROM":
            by_line.setdefault(edge.line, set()).add(edge.target_qualified)
    return by_line


def _importer_files(store: GraphStore, module_file: str) -> list[str]:
    """Files with an IMPORTS_FROM edge into *module_file*."""
    return sorted({
        edge.file_path for edge in store.get_edges_by_target(module_file)
        if edge.kind == "IMPORTS_FROM"
    })


# --- Python: one name, resolved by scope ----------------------------------


class _PyScope:
    """A Python scope, as far as one name is concerned."""

    __slots__ = ("kind", "parent", "binds", "bound_yet", "declared", "target", "star")

    def __init__(self, kind: str, parent: Optional[_PyScope]) -> None:
        self.kind = kind
        self.parent = parent
        self.binds = False      # the name is bound somewhere in this scope
        self.bound_yet = False  # ...by a statement already run (class bodies)
        self.declared = ""      # "global" or "nonlocal" when declared that way
        self.target = False     # one of those bindings is the renamed symbol
        self.star = False       # a star import from an exporting module

    def bind(self, target: bool = False) -> None:
        self.binds = self.bound_yet = True
        if target:
            self.target = True

    def outside_class(self) -> _PyScope:
        """Where a class body looks up a name it has not bound yet."""
        scope = self.parent or self
        while scope.kind == "class" and scope.parent is not None:
            scope = scope.parent
        return scope


def _py_module_matches(target: str, module_path: str, exact: bool) -> bool:
    """Whether *target* is the file of the module spelled *module_path*."""
    for ending in _PY_MODULE_ENDINGS:
        candidate = module_path + ending
        if target == candidate or (not exact and target.endswith("/" + candidate)):
            return True
    return False


def _py_module_base(dotted: str, targets: set[str]) -> Optional[str]:
    """The directory that holds the top package of *dotted*, from the
    files the graph resolved an import of it to (``/repo`` for
    ``pkg.util`` resolved to ``/repo/pkg/util.py``)."""
    rel = "/" + dotted.replace(".", "/")
    for target in sorted(targets):
        for ending in ("/__init__.py", "/__init__.pyi", ".py", ".pyi", ".pyw"):
            if target.endswith(ending):
                module = target[: -len(ending)]
                if module.endswith(rel):
                    return module[: -len(rel)]
                break
    return None


def _dotted(expr: ast.expr) -> Optional[str]:
    """``a.b.c`` for a Name/Attribute chain, else ``None``."""
    parts: list[str] = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if not isinstance(expr, ast.Name):
        return None
    parts.append(expr.id)
    return ".".join(reversed(parts))


class _PyRenameScan:
    """Every place one Python module spells the symbol being renamed.

    Python resolves a name per scope, so this does too: an occurrence is
    renamed when it resolves to a scope whose binding of the name is the
    symbol itself -- its definition, or an import of it from a module that
    exports it. A parameter, a local variable or a class attribute that
    happens to share the name resolves elsewhere and is left alone.
    Comments and strings are not names at all, except the string entries of
    ``__all__``, which are renamed where the module's own binding is.
    """

    def __init__(
        self,
        source: _Source,
        name: str,
        *,
        target_def: Optional[ast.AST],
        import_targets: dict[int, set[str]],
        exporters: set[str],
    ) -> None:
        self.src = source
        self.name = name
        self.name_bytes = len(name.encode("utf-8"))
        self.word = _ident_pattern(name)
        self.target_def = target_def
        self.import_targets = import_targets
        self.exporters = exporters
        self.root = _PyScope("module", None)
        # Occurrences whose fate depends on the scope they resolve to.
        self.uses: list[tuple[_PyScope, int, int, str]] = []
        # Occurrences renamed whatever the scope: names imported from an
        # exporting module.
        self.fixed: list[_Occurrence] = []
        self.attributes: list[ast.Attribute] = []
        self.module_prefixes: set[str] = set()
        # Names bound to a module, by the module's path without its ending
        # (``pkg`` -> ``/repo/pkg`` after ``import pkg``), so that
        # ``pkg.util.name`` is followed to ``/repo/pkg/util.py``.
        self.module_paths: dict[str, str] = {}
        self.all_entries: list[ast.Constant] = []
        self.called: set[int] = set()  # ids of Name nodes that are called

    # -- driver ------------------------------------------------------------

    def run(self, tree: ast.Module) -> tuple[list[_Occurrence], bool]:
        """Return the occurrences to rename and whether the module binds it.

        The second value says the module's own global name is renamed, so
        the module now exports the new name and its importers must follow.
        """
        for stmt in tree.body:
            self._collect_all(stmt)
            self._visit(stmt, self.root)
        root = self.root
        if root.star and not root.binds:
            root.bind(target=True)
        found = list(self.fixed)
        for scope, line, col, site in self.uses:
            if self._owner(scope).target:
                found.append((line, col, site, None))
        if root.target:
            for const in self.all_entries:
                col = self._string_entry_col(const)
                if col is not None:
                    found.append((const.lineno, col, "__all__", None))
        for attr in self.attributes:
            if self._names_exporter(_dotted(attr.value)) and attr.end_lineno is not None:
                line = attr.end_lineno
                end = attr.end_col_offset or 0
                found.append(
                    (line, self.src.char_col(line, end - self.name_bytes), "attribute", None)
                )
        return found, root.target

    def _names_exporter(self, dotted: Optional[str]) -> bool:
        """Whether ``dotted`` is a module that exports the symbol."""
        if dotted is None:
            return False
        if dotted in self.module_prefixes:
            return True
        root, _, rest = dotted.partition(".")
        path = self.module_paths.get(root)
        if path is None:
            return False
        if rest:
            path = f"{path}/{rest.replace('.', '/')}"
        return any(_py_module_matches(e, path, True) for e in self.exporters)

    def _owner(self, scope: _PyScope) -> _PyScope:
        """The scope whose binding an occurrence in *scope* refers to."""
        current = scope
        while True:
            if current.declared == "global":
                return self.root
            if current.parent is None or (current.binds and current.declared != "nonlocal"):
                return current
            current = current.parent
            # A nested scope never sees a class body's names.
            while current.kind == "class" and current.parent is not None:
                current = current.parent

    # -- positions ---------------------------------------------------------

    def _use(self, scope: _PyScope, line: int, byte_col: int, site: str = "reference") -> None:
        self.uses.append((scope, line, self.src.char_col(line, byte_col), site))

    def _use_after(self, scope: _PyScope, line: int, byte_col: int, prefix: str = "") -> None:
        """Record the first whole-word occurrence after a byte column."""
        text = self.src.line(line)
        start = self.src.char_col(line, byte_col)
        pattern = re.compile(prefix + self.word.pattern) if prefix else self.word
        match = pattern.search(text, start)
        if match:
            col = match.end() - len(self.name)
            self.uses.append((scope, line, col, "reference"))

    def _def_name_col(self, node: Union[ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef]
                      ) -> Optional[int]:
        text = self.src.line(node.lineno)
        match = _DEF_KEYWORD_RE.match(text, self.src.char_col(node.lineno, node.col_offset))
        if match and text.startswith(self.name, match.end()):
            return match.end()
        return None

    def _string_entry_col(self, const: ast.Constant) -> Optional[int]:
        """Column of the name inside a one-line ``"name"`` literal."""
        if const.end_lineno != const.lineno or const.end_col_offset is None:
            return None
        raw = self.src.line(const.lineno).encode("utf-8")[const.col_offset:const.end_col_offset]
        match = re.fullmatch(r"([rRuU]?)('''|\"\"\"|'|\")(.*)\2", raw.decode("utf-8", "replace"))
        if not match or match.group(3) != self.name:
            return None
        start = self.src.char_col(const.lineno, const.col_offset)
        return start + len(match.group(1)) + len(match.group(2))

    # -- __all__ -----------------------------------------------------------

    def _collect_all(self, stmt: ast.stmt) -> None:
        """Remember ``__all__`` string entries spelling the name."""
        values: list[ast.AST] = []
        if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
                if stmt.value is not None:
                    values.append(stmt.value)
        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            func = stmt.value.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "__all__"
            ):
                values.extend(stmt.value.args)
        elif isinstance(stmt, (ast.If, ast.Try)):
            for child in ast.iter_child_nodes(stmt):
                if isinstance(child, ast.stmt):
                    self._collect_all(child)
                elif isinstance(child, ast.ExceptHandler):
                    for inner in child.body:
                        self._collect_all(inner)
        for value in values:
            for node in ast.walk(value):
                if isinstance(node, ast.Constant) and node.value == self.name:
                    self.all_entries.append(node)

    # -- visitor -----------------------------------------------------------

    def _visit(self, node: ast.AST, scope: _PyScope) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._visit_function(node, scope)
        elif isinstance(node, ast.ClassDef):
            self._visit_class(node, scope)
        elif isinstance(node, ast.Lambda):
            self._visit_arguments(node.args, scope)
            inner = _PyScope("function", scope)
            self._bind_arguments(node.args, inner)
            self._visit(node.body, inner)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            self._visit_comprehension(node, scope)
        elif isinstance(node, ast.Name):
            if node.id == self.name:
                site = "call" if id(node) in self.called else "reference"
                if not isinstance(node.ctx, ast.Load):
                    scope.bind()
                elif scope.kind == "class" and not scope.bound_yet:
                    # A class body reads names dynamically: before the class
                    # binds the name, ``x = f(name)`` reads the outer one.
                    scope = scope.outside_class()
                self._use(scope, node.lineno, node.col_offset, site)
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            if isinstance(node, ast.Assign):
                self._bind_import_module(node)
            # Evaluation order: the value runs before the target is bound.
            if isinstance(node, ast.AnnAssign):
                self._visit(node.annotation, scope)
            if node.value is not None:
                self._visit(node.value, scope)
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                self._visit(target, scope)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            self._visit(node.iter, scope)
            self._visit(node.target, scope)
            for stmt in (*node.body, *node.orelse):
                self._visit(stmt, scope)
        elif isinstance(node, ast.NamedExpr):
            self._visit(node.value, scope)
            owner = scope
            while owner.kind == "comprehension" and owner.parent is not None:
                owner = owner.parent
            self._visit(node.target, owner)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            if self.name in node.names:
                keyword = "global" if isinstance(node, ast.Global) else "nonlocal"
                scope.declared = keyword
                self._use_after(scope, node.lineno, node.col_offset + len(keyword))
        elif isinstance(node, ast.Import):
            self._visit_import(node, scope)
        elif isinstance(node, ast.ImportFrom):
            self._visit_import_from(node, scope)
        elif isinstance(node, ast.ExceptHandler):
            if node.type is not None:
                self._visit(node.type, scope)
            if node.name == self.name:
                scope.bind()
                anchor = node.type if node.type is not None else node
                line = anchor.end_lineno or anchor.lineno
                self._use_after(scope, line, anchor.end_col_offset or 0, prefix=r"\bas\s+")
            for stmt in node.body:
                self._visit(stmt, scope)
        elif isinstance(node, ast.Attribute):
            self._visit(node.value, scope)
            if node.attr == self.name:
                self.attributes.append(node)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                self.called.add(id(node.func))
            for child in ast.iter_child_nodes(node):
                self._visit(child, scope)
        else:
            if getattr(node, "name", None) == self.name or getattr(node, "rest", None) == self.name:
                # A match-statement capture: a binding with no reported
                # position. It only matters if it rebinds the renamed name.
                if isinstance(node, (ast.MatchAs, ast.MatchStar, ast.MatchMapping)):
                    scope.bind()
            for child in ast.iter_child_nodes(node):
                self._visit(child, scope)

    def _bind_import_module(self, node: ast.Assign) -> None:
        """``mod = importlib.import_module("pkg.util")`` binds a module."""
        call = node.value
        if not (
            len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
            and isinstance(call, ast.Call) and len(call.args) == 1 and not call.keywords
            and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)
            and _dotted(call.func) in ("importlib.import_module", "import_module")
        ):
            return
        module_path = call.args[0].value.replace(".", "/")
        if not module_path.startswith("/") and any(
            _py_module_matches(e, module_path, False) for e in self.exporters
        ):
            self.module_prefixes.add(node.targets[0].id)

    def _visit_function(
        self, node: Union[ast.FunctionDef, ast.AsyncFunctionDef], scope: _PyScope,
    ) -> None:
        # Decorators, defaults and annotations run in the enclosing scope.
        for decorator in node.decorator_list:
            self._visit(decorator, scope)
        self._visit_arguments(node.args, scope)
        if node.returns is not None:
            self._visit(node.returns, scope)
        self._bind_definition(node, scope)
        inner = _PyScope("function", scope)
        self._bind_arguments(node.args, inner)
        for stmt in node.body:
            self._visit(stmt, inner)

    def _visit_class(self, node: ast.ClassDef, scope: _PyScope) -> None:
        for decorator in node.decorator_list:
            self._visit(decorator, scope)
        for base in node.bases:
            self._visit(base, scope)
        for keyword in node.keywords:
            self._visit(keyword.value, scope)
        self._bind_definition(node, scope)
        inner = _PyScope("class", scope)
        for stmt in node.body:
            self._visit(stmt, inner)

    def _bind_definition(
        self,
        node: Union[ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef],
        scope: _PyScope,
    ) -> None:
        if node.name != self.name:
            return
        is_target = node is self.target_def
        scope.bind(target=is_target)
        col = self._def_name_col(node)
        if col is not None:
            site = "definition" if is_target else "reference"
            self.uses.append((scope, node.lineno, col, site))

    def _visit_arguments(self, args: ast.arguments, scope: _PyScope) -> None:
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg):
            if arg is not None and arg.annotation is not None:
                self._visit(arg.annotation, scope)
        for default in (*args.defaults, *args.kw_defaults):
            if default is not None:
                self._visit(default, scope)

    def _bind_arguments(self, args: ast.arguments, inner: _PyScope) -> None:
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg):
            if arg is not None and arg.arg == self.name:
                inner.bind()
                self._use(inner, arg.lineno, arg.col_offset)

    def _visit_comprehension(
        self,
        node: Union[ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp],
        scope: _PyScope,
    ) -> None:
        generators = node.generators
        # The first iterable is evaluated in the enclosing scope.
        self._visit(generators[0].iter, scope)
        inner = _PyScope("comprehension", scope)
        for index, generator in enumerate(generators):
            if index:
                self._visit(generator.iter, inner)
            self._visit(generator.target, inner)
            for condition in generator.ifs:
                self._visit(condition, inner)
        if isinstance(node, ast.DictComp):
            self._visit(node.key, inner)
            self._visit(node.value, inner)
        else:
            self._visit(node.elt, inner)

    # -- imports -----------------------------------------------------------

    def _alias_name_col(self, alias: ast.alias) -> int:
        return self.src.char_col(alias.lineno, alias.col_offset)

    def _alias_asname_col(self, alias: ast.alias) -> tuple[int, int]:
        line = alias.end_lineno or alias.lineno
        end = alias.end_col_offset or 0
        return line, self.src.char_col(line, end - self.name_bytes)

    def _visit_import(self, node: ast.Import, scope: _PyScope) -> None:
        targets = self.import_targets.get(node.lineno, set())
        exporting = targets & self.exporters
        for alias in node.names:
            module_path = alias.name.replace(".", "/")
            if any(_py_module_matches(t, module_path, False) for t in exporting) or (
                exporting and len(node.names) == 1
            ):
                self.module_prefixes.add(alias.asname or alias.name)
            bound = alias.asname or alias.name.split(".", 1)[0]
            base = _py_module_base(alias.name, targets)
            if base is not None:
                module = alias.name if alias.asname else bound
                self.module_paths[bound] = f"{base}/{module.replace('.', '/')}"
            if bound == self.name:
                scope.bind()
                if alias.asname is not None:
                    line, col = self._alias_asname_col(alias)
                    self.uses.append((scope, line, col, "import"))

    def _visit_import_from(self, node: ast.ImportFrom, scope: _PyScope) -> None:
        targets = self.import_targets.get(node.lineno, set())
        module_path = (node.module or "").replace(".", "/")
        exact = node.level > 0
        if exact:
            base = PurePosixPath(self.src.path).parent
            for _ in range(node.level - 1):
                base = base.parent
            module_path = str(base / module_path) if module_path else str(base)

        def submodule_of(target: str, imported: str) -> bool:
            prefix = f"{module_path}/{imported}" if module_path else imported
            return _py_module_matches(target, prefix, exact)

        module_targets = {t for t in targets if _py_module_matches(t, module_path, exact)}
        if not module_targets:
            # A resolution these rules do not spell: every target that is
            # not one of the submodules the statement names.
            module_targets = {
                t for t in targets
                if not any(submodule_of(t, alias.name) for alias in node.names)
            }
        from_exporter = bool(module_targets & self.exporters)
        absolute: Optional[str] = module_path if exact else None
        if not exact and node.module:
            for dotted in [node.module, *(f"{node.module}.{a.name}" for a in node.names)]:
                top = _py_module_base(dotted, targets)
                if top is not None:
                    absolute = f"{top}/{module_path}"
                    break

        for alias in node.names:
            if alias.name == "*":
                if from_exporter and scope is self.root:
                    scope.star = True
                continue
            bound = alias.asname or alias.name
            if absolute is not None:
                # ``from pkg import sub`` then ``sub.util.name``.
                self.module_paths[bound] = f"{absolute}/{alias.name}"
            if any(submodule_of(t, alias.name) for t in targets & self.exporters):
                # ``from pkg import util``: a module whose attribute is renamed.
                self.module_prefixes.add(bound)
            elif alias.name == self.name and from_exporter:
                # The imported name itself, whatever it is bound as locally.
                self.fixed.append(
                    (alias.lineno, self._alias_name_col(alias), "import", None)
                )
                if bound == self.name:
                    scope.bind(target=True)
                    if alias.asname is not None:  # ``import name as name``
                        line, col = self._alias_asname_col(alias)
                        self.uses.append((scope, line, col, "import"))
                continue
            if bound == self.name:
                # Something else bound under the name. Its local alias can
                # follow a consistent rename; the imported name cannot.
                scope.bind()
                if alias.asname is not None:
                    line, col = self._alias_asname_col(alias)
                    self.uses.append((scope, line, col, "import"))


def _py_find_definition(
    tree: ast.Module, name: str, line: int,
) -> tuple[Optional[ast.AST], str]:
    """The ``def``/``class`` named *name* at *line*, and where it lives.

    The place is ``"module"``, ``"function"`` (nested in a function) or
    ``"class"`` (a method or nested class, reached as an attribute).
    """
    stack: list[tuple[ast.AST, str]] = [(tree, "module")]
    while stack:
        current, place = stack.pop()
        for child in ast.iter_child_nodes(current):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                first = min([d.lineno for d in child.decorator_list] + [child.lineno])
                if child.name == name and first <= line <= child.lineno:
                    return child, place
                inner = "class" if isinstance(child, ast.ClassDef) else "function"
                stack.append((child, inner))
            elif isinstance(child, (ast.stmt, ast.ExceptHandler, ast.match_case)):
                stack.append((child, place))
    return None, ""


def _python_rename_sites(
    store: GraphStore, node: GraphNode, sources: _Sources, call_files: set[str],
) -> Optional[dict[str, list[_Occurrence]]]:
    """Exact sites for a Python function or class reached by its name.

    Covers the defining module and, for a module-level symbol, every module
    that imports it -- directly, through a package that re-exports it, or
    through ``import module`` and attribute access -- and every module in
    *call_files* (those with a call edge to the bare name), where a call
    such as ``pkg.util.name()`` after ``import pkg``, or through a module
    bound by ``importlib``, reaches it without an import that names it.
    Returns ``None`` when the symbol is a method (reached as an attribute,
    so only the graph's call sites can find it) or the source cannot be
    parsed; the caller then falls back to the graph.
    """
    name = node.name
    home = node.file_path
    if not home.endswith(_PY_SUFFIXES):
        return None
    source = sources.get(home)
    tree = source.python_tree() if source is not None else None
    if source is None or tree is None:
        return None
    target_def, place = _py_find_definition(tree, name, node.line_start)
    if target_def is None or place == "class":
        return None

    results: dict[str, list[_Occurrence]] = {}
    exporters = {home}
    marker = name.encode("utf-8")

    def scan(path: str) -> Optional[bool]:
        src = sources.get(path)
        parsed = src.python_tree() if src is not None else None
        if src is None or parsed is None:
            return None
        scanner = _PyRenameScan(
            src, name,
            target_def=target_def if path == home else None,
            import_targets=_import_targets_by_line(store, path) if path != home else {},
            exporters=exporters,
        )
        try:
            found, exports = scanner.run(parsed)
        except RecursionError:
            return None
        results[path] = found
        return exports

    if not scan(home) or place != "module":
        # A nested function is invisible outside its module.
        return results if home in results else None

    pending: deque[str] = deque()
    queued: set[str] = set()
    for path in [
        *_importer_files(store, home),
        *(edge.file_path for edge in store.get_edges_by_target(node.qualified_name)
          if edge.kind == "IMPORTS_FROM"),
        *sorted(call_files),
    ]:
        if path not in queued:
            queued.add(path)
            pending.append(path)
    while pending:
        path = pending.popleft()
        queued.discard(path)
        if path == home or not path.endswith(_PY_SUFFIXES):
            continue
        src = sources.get(path)
        if src is None:
            continue
        if marker not in src.data and not _STAR_IMPORT_RE.search(src.data):
            # Cannot name the symbol, so cannot import or re-export it.
            results.setdefault(path, [])
            continue
        exports = scan(path)
        if exports and path not in exporters:
            # This module now re-exports the symbol: its importers follow.
            exporters.add(path)
            for importer in _importer_files(store, path):
                if importer not in queued:
                    queued.add(importer)
                    pending.append(importer)
    return results


# --- JavaScript / TypeScript ----------------------------------------------


def _ts_text(node: Any) -> str:
    return node.text.decode("utf-8", errors="replace") if node is not None else ""


def _ts_same(a: Any, b: Any) -> bool:
    return a is not None and b is not None and (
        a.start_byte, a.end_byte) == (b.start_byte, b.end_byte)


def _ts_string_value(node: Any) -> str:
    text = _ts_text(node)
    return text[1:-1] if len(text) >= 2 and text[0] in "'\"`" else text


def _js_module_call_spec(node: Any) -> Any:
    """The specifier string of ``import('./m')`` or ``require('./m')``."""
    if node is None or node.type != "call_expression":
        return None
    func = node.child_by_field_name("function")
    if func is None or not (
        func.type == "import" or (func.type == "identifier" and _ts_text(func) == "require")
    ):
        return None
    args = node.child_by_field_name("arguments")
    first = args.named_children[0] if args is not None and args.named_children else None
    return first if first is not None and first.type == "string" else None


def _js_is_declaration(leaf: Any) -> bool:
    if leaf.type == "shorthand_property_identifier_pattern":
        return True
    parent = leaf.parent
    if parent is None:
        return False
    if parent.type in _JS_DECLARATION_PARENTS:
        return True
    return any(
        _ts_same(parent.child_by_field_name(field), leaf)
        for field in _JS_DECLARATION_FIELDS.get(parent.type, ())
    )


def _ts_ancestor(node: Any, kinds: frozenset[str]) -> Any:
    """The nearest strict ancestor of one of *kinds*, else the root."""
    current = node.parent
    last = node
    while current is not None:
        if current.type in kinds:
            return current
        last, current = current, current.parent
    return last


def _js_declaration_scope(leaf: Any) -> Any:
    """The syntax node whose extent a JS/TS declaration is visible in."""
    child, current = leaf, leaf.parent
    while current is not None:
        kind = current.type
        if kind == "formal_parameters" or (
            kind == "arrow_function"
            and _ts_same(current.child_by_field_name("parameter"), child)
        ):
            return current if kind == "arrow_function" else current.parent
        if kind in ("catch_clause", "for_in_statement"):
            return current
        if kind == "variable_declarator":
            declaration = current.parent
            if declaration is not None and declaration.type == "variable_declaration":
                return _ts_ancestor(declaration, _JS_FUNCTION_SCOPES)  # ``var`` hoists
            return _ts_ancestor(current, _JS_BLOCK_SCOPES)
        if kind in _JS_NAMED_EXPRESSIONS and _ts_same(current.child_by_field_name("name"), child):
            return current  # a named function/class expression sees only itself
        if kind in _JS_DECLARATION_FIELDS and _ts_same(current.child_by_field_name("name"), child):
            return _ts_ancestor(current, _JS_BLOCK_SCOPES)
        if kind in ("import_statement", "program"):
            return _ts_ancestor(current, frozenset({"program"}))
        child, current = current, current.parent
    return child


class _JsScan:
    """Import, export and reference sites of one name in one JS/TS module.

    Each declaration of the name is visible in a syntax range (a function,
    a block, the module). A plain occurrence belongs to the innermost range
    containing it, so a parameter or local that reuses the name shadows the
    symbol inside its own function and nowhere else.
    """

    def __init__(
        self,
        source: _Source,
        name: str,
        new_name: str,
        *,
        def_lines: Optional[range],
        import_targets: dict[int, set[str]],
        exporters: set[str],
        resolve: Any,
    ) -> None:
        self.src = source
        self.name = name
        self.new_name = new_name
        self.def_lines = def_lines
        self.import_targets = import_targets
        self.exporters = exporters
        self.resolve = resolve
        self.renames: list[_Occurrence] = []
        # Plain occurrences, decided once every declaration is known:
        # (occurrence, start byte, end byte, a local ``export { name }``).
        self.plain: list[tuple[_Occurrence, int, int, bool]] = []
        # (line, column) of plain occurrences bound by another declaration
        # (a default import, a ``require`` destructuring, a local): the
        # graph's call edges must not rename them either.
        self.kept: set[tuple[int, int]] = set()
        # Declaration ranges: (start byte, end byte, is the renamed symbol).
        self.scopes: list[tuple[int, int, bool]] = []
        # ``obj.name`` uses: (obj, obj's start byte, the name's leaf).
        self.members: list[tuple[str, int, Any]] = []
        # Names bound to an exporting module's namespace, and the byte
        # ranges they are bound in.
        self.prefixes: dict[str, list[tuple[int, int]]] = {}
        self.barrels: set[str] = set()
        self.reexports = False      # ``export { name } from`` an exporter
        self.local_export = False   # ``export { name }`` of the symbol
        self.found_definition = False
        # ``export function name``: the module exports the symbol under its
        # name (a default export is ``default``, whatever it is called).
        self.exported_definition = False
        self.commonjs = False  # ``module.exports`` or ``exports.x`` appears
        self.root: Any = None

    def occurrence(self, leaf: Any, site: str, new: Optional[str] = None) -> _Occurrence:
        line = leaf.start_point[0] + 1
        return line, self.src.char_col(line, leaf.start_point[1]), site, new

    def declare(self, node: Any, target: bool) -> None:
        self.scopes.append((node.start_byte, node.end_byte, target))

    def statement_targets(self, statement: Any) -> set[str]:
        line = statement.start_point[0] + 1
        targets = set(self.import_targets.get(line, set()))
        source = statement.child_by_field_name("source")
        if not targets and source is not None:
            resolved = self.resolve(_ts_string_value(source))
            if resolved:
                targets.add(resolved)
        return targets

    def run(self, root: Any) -> None:
        self.root = root
        stack = [root]
        while stack:
            current = stack.pop()
            kind = current.type
            if kind == "import_statement":
                self.visit_import(current)
                continue
            if kind == "export_statement" and current.child_by_field_name("source") is not None:
                self.visit_reexport(current)
                continue
            if kind == "variable_declarator":
                self.visit_module_value(current)
            elif kind == "call_expression":
                self.visit_import_then(current)
            if kind in ("member_expression", "nested_identifier"):
                obj = current.child_by_field_name("object")
                prop = current.child_by_field_name("property")
                if obj is None and current.named_child_count == 2:
                    obj, prop = current.named_children
                if prop is not None and obj is not None:
                    if obj.type == "identifier" and _ts_text(prop) == self.name:
                        self.members.append((_ts_text(obj), obj.start_byte, prop))
                    if obj.type == "identifier" and (
                        _ts_text(obj) == "exports"
                        or (_ts_text(obj) == "module" and _ts_text(prop) == "exports")
                    ):
                        self.commonjs = True
                    # ``x.name`` is a property of x, never the binding.
                    stack.append(obj)
                    continue
            if current.child_count == 0:
                if kind in _JS_NAME_LEAVES and _ts_text(current) == self.name:
                    self.visit_name(current)
                continue
            # Source order: the definition is the first declaration of the
            # name in its line window, before any local that reuses it.
            stack.extend(reversed(current.children))

        for obj_name, obj_start, prop in self.members:
            if any(s <= obj_start < e for s, e in self.prefixes.get(obj_name, ())):
                self.renames.append(self.occurrence(prop, "attribute"))
        for occurrence, start, end, exported in self.plain:
            resolved = self.resolves_to_symbol(start, end)
            if resolved:
                self.renames.append(occurrence)
                self.local_export = self.local_export or exported
            elif resolved is False:
                self.kept.add((occurrence[0], occurrence[1]))

    def resolves_to_symbol(self, start: int, end: int) -> Optional[bool]:
        """Whether the innermost declaration around a range is the symbol.

        ``None`` when no declaration of the name is in scope there.
        """
        best: Optional[tuple[int, bool]] = None
        for scope_start, scope_end, target in self.scopes:
            if scope_start <= start and end <= scope_end:
                size = scope_end - scope_start
                # On a tie the other declaration wins: renaming is the risk.
                if best is None or size < best[0] or (size == best[0] and not target):
                    best = (size, target)
        return None if best is None else best[1]

    @property
    def bound(self) -> bool:
        return any(target for _s, _e, target in self.scopes)

    @property
    def authoritative(self) -> bool:
        """The scan resolved every plain occurrence, so call sites add nothing."""
        return self.bound

    @property
    def exports_by_name(self) -> bool:
        """An ES module export of the symbol under its own name."""
        return self.reexports or self.local_export or self.exported_definition

    @property
    def exports(self) -> bool:
        """Importers of this module name the symbol as the rename changes it.

        A CommonJS home keeps its key (``module.exports = { name: new }``),
        so its importers keep the old name; any other home is taken to
        export the name, as an import of it would be an error otherwise.
        """
        return self.exports_by_name or (self.def_lines is not None and not self.commonjs)

    def visit_name(self, leaf: Any) -> None:
        if _js_is_declaration(leaf):
            line = leaf.start_point[0] + 1
            is_target = (
                self.def_lines is not None and line in self.def_lines
                and not self.found_definition
            )
            if is_target:
                self.found_definition = True
                export = _ts_ancestor(leaf, frozenset({"export_statement"}))
                self.exported_definition = export.type == "export_statement" and not any(
                    c.type == "default" for c in export.children
                )
                self.renames.append(self.occurrence(leaf, "definition"))
            self.declare(_js_declaration_scope(leaf), is_target)
            return
        replacement = None
        site = "reference"
        exported = False
        parent = leaf.parent
        if leaf.type == "shorthand_property_identifier":
            # ``{ name }`` is ``{ name: name }``: keep the key, rename the value.
            replacement = f"{self.name}: {self.new_name}"
        elif parent is not None and parent.type == "export_specifier":
            name = parent.child_by_field_name("name")
            alias = parent.child_by_field_name("alias")
            if not _ts_same(name, leaf) and _ts_text(name) != self.name:
                return  # ``export { other as name }`` exports something else
            site = "export"
            exported = alias is None or _ts_text(alias) == self.name
        self.plain.append(
            (self.occurrence(leaf, site, replacement), leaf.start_byte, leaf.end_byte, exported)
        )

    def namespace(self, ident: Any, targets: set[str], scope: Any) -> None:
        """Bind *ident* to a module namespace within *scope*.

        A module that is not (yet) known to export the symbol is a barrel
        candidate: if it turns out to re-export it, this file is scanned
        again and ``ident.name`` is renamed then.
        """
        if targets & self.exporters:
            self.prefixes.setdefault(_ts_text(ident), []).append(
                (scope.start_byte, scope.end_byte)
            )
        else:
            self.barrels.update(targets)

    def module_targets(self, spec: Any) -> set[str]:
        """Files a module specifier (a string node) resolves to."""
        if spec is None:
            return set()
        resolved = self.resolve(_ts_string_value(spec))
        if resolved:
            return {resolved}
        return set(self.import_targets.get(spec.start_point[0] + 1, set()))

    def visit_module_value(self, declarator: Any) -> None:
        """``const ns = await import('./m')`` or ``const ns = require('./m')``."""
        ident = declarator.child_by_field_name("name")
        value = declarator.child_by_field_name("value")
        if ident is None or ident.type != "identifier" or value is None:
            return
        if value.type == "await_expression" and value.named_child_count == 1:
            value = value.named_children[0]
        spec = _js_module_call_spec(value)
        if spec is not None:
            self.namespace(ident, self.module_targets(spec), _js_declaration_scope(ident))

    def visit_import_then(self, call: Any) -> None:
        """``import('./m').then((ns) => ns.name())``."""
        func = call.child_by_field_name("function")
        if func is None or func.type != "member_expression":
            return
        prop = func.child_by_field_name("property")
        spec = _js_module_call_spec(func.child_by_field_name("object"))
        args = call.child_by_field_name("arguments")
        if spec is None or _ts_text(prop) != "then" or args is None or not args.named_children:
            return
        callback = args.named_children[0]
        if callback.type not in ("arrow_function", "function_expression", "function"):
            return
        param = callback.child_by_field_name("parameter")
        if param is None:
            params = callback.child_by_field_name("parameters")
            first = (
                params.named_children[0]
                if params is not None and params.named_children else None
            )
            if first is not None and first.type in ("required_parameter", "optional_parameter"):
                first = first.child_by_field_name("pattern")
            param = first
        if param is not None and param.type == "identifier":
            self.namespace(param, self.module_targets(spec), callback)

    def visit_import(self, statement: Any) -> None:
        targets = self.statement_targets(statement)
        from_exporter = bool(targets & self.exporters)
        require = next(
            (c for c in statement.children if c.type == "import_require_clause"), None,
        )
        if require is not None:
            # TypeScript ``import ns = require('./m')``.
            ident = next((c for c in require.children if c.type == "identifier"), None)
            if ident is not None:
                spec = require.child_by_field_name("source")
                self.namespace(ident, targets or self.module_targets(spec), self.root)
                if _ts_text(ident) == self.name:
                    self.declare(self.root, False)
            return
        clause = next((c for c in statement.children if c.type == "import_clause"), None)
        if clause is None:
            return
        for part in clause.children:
            if part.type == "identifier":
                if _ts_text(part) == self.name:
                    self.declare(self.root, False)  # a default import under the name
            elif part.type == "namespace_import":
                ident = next((c for c in part.children if c.type == "identifier"), None)
                if ident is not None:
                    self.namespace(ident, targets, self.root)
                    if _ts_text(ident) == self.name:
                        self.declare(self.root, False)
            elif part.type == "named_imports":
                for spec in part.children:
                    if spec.type == "import_specifier":
                        self.visit_import_specifier(spec, targets, from_exporter)

    def visit_import_specifier(self, spec: Any, targets: set[str], from_exporter: bool) -> None:
        name = spec.child_by_field_name("name")
        alias = spec.child_by_field_name("alias")
        alias_is_name = alias is not None and _ts_text(alias) == self.name
        if _ts_text(name) != self.name:
            if alias_is_name:
                self.declare(self.root, False)
            return
        if not from_exporter:
            if alias is None or alias_is_name:
                self.declare(self.root, False)
            self.barrels.update(targets)
            return
        self.renames.append(self.occurrence(name, "import"))
        if alias is None or alias_is_name:
            self.declare(self.root, True)
            if alias_is_name:
                self.renames.append(self.occurrence(alias, "import"))

    def visit_reexport(self, statement: Any) -> None:
        targets = self.statement_targets(statement)
        clause = next((c for c in statement.children if c.type == "export_clause"), None)
        if not targets & self.exporters:
            # A barrel re-exporting another barrel: once that one is known
            # to export the symbol, this file is scanned again.
            if clause is None or any(
                _ts_text(spec.child_by_field_name("name")) == self.name
                for spec in clause.children if spec.type == "export_specifier"
            ):
                self.barrels.update(targets)
            return
        if clause is None:
            if any(c.type == "*" for c in statement.children) and not any(
                c.type == "namespace_export" for c in statement.children
            ):
                self.reexports = True  # ``export * from`` passes the name through
            return
        for spec in clause.children:
            if spec.type != "export_specifier":
                continue
            name = spec.child_by_field_name("name")
            alias = spec.child_by_field_name("alias")
            if _ts_text(name) != self.name:
                continue
            self.renames.append(self.occurrence(name, "export"))
            if alias is None:
                self.reexports = True
            elif _ts_text(alias) == self.name:
                self.reexports = True
                self.renames.append(self.occurrence(alias, "export"))


def _js_reexport_files(
    store: GraphStore, sources: _Sources, name: str, home: str,
) -> list[str]:
    """JS/TS files that spell ``export { name } from``.

    A re-export carries no graph edge, so a barrel that no file imports
    the name from by name (a package's public ``index.ts``) is found only
    by reading the files.
    """
    marker = name.encode("utf-8")
    pattern = re.compile(
        rb"\bexport\s+(?:type\s+)?\{[^}]*?(?<![\w$])" + re.escape(marker)
        + rb"(?![\w$])[^}]*\}\s*from\b"
    )
    found = []
    for path in store.get_all_files():
        if path == home or sources.language(path) not in _JS_FAMILY:
            continue
        try:
            data = Path(path).read_bytes()
        except OSError:
            continue
        if marker in data and pattern.search(data):
            found.append(path)
    return sorted(found)


def _js_rename_sites(
    store: GraphStore,
    node: GraphNode,
    new_name: str,
    sources: _Sources,
    graph_files: set[str],
) -> Optional[dict[str, _JsScan]]:
    """Exact sites for a top-level JS/TS symbol.

    Follows named imports (``import { name }``, ``import { name as x }``),
    namespace objects (``import * as ns``, ``import ns = require()``,
    ``const ns = require()``, ``await import()`` and ``import().then``,
    then ``ns.name``), local export lists and re-exporting barrels
    (``export { name } from``, ``export * from``). Re-exports carry no
    import edge, so barrels are found from the consumers that import
    through them and, for an exported symbol, by reading the files that
    spell ``export { name } from``. *graph_files* are the files with a call
    edge to the name; they are scanned too, so a namespace object there is
    followed.
    """
    home = node.file_path
    if sources.tree(home) is None or sources.get(home) is None:
        return None
    window = range(node.line_start, min(node.line_end, node.line_start + _DEF_SEARCH_LINES) + 1)
    # Modules whose export of the name the rename changes; the home module
    # joins once its scan shows it is not CommonJS.
    exporters: set[str] = set()
    results: dict[str, _JsScan] = {}
    pending: deque[str] = deque([home])
    queued = {home}

    def enqueue(paths: Any) -> None:
        for path in paths:
            if path not in queued:
                queued.add(path)
                pending.append(path)

    enqueue(_importer_files(store, home))
    enqueue(sorted(graph_files))
    while pending:
        path = pending.popleft()
        queued.discard(path)
        if sources.language(path) not in _JS_FAMILY:
            continue
        src = sources.get(path)
        tree = sources.tree(path)
        if src is None or tree is None:
            continue
        scan = _JsScan(
            src, node.name, new_name,
            def_lines=window if path == home else None,
            import_targets=_import_targets_by_line(store, path),
            exporters=exporters,
            resolve=functools.partial(sources.resolve_js_module, from_file=path),
        )
        scan.run(tree.root_node)
        results[path] = scan
        if path == home:
            if not scan.found_definition:
                return None
            if scan.exports_by_name:
                enqueue(_js_reexport_files(store, sources, node.name, home))
        if scan.exports and path not in exporters:
            exporters.add(path)
            enqueue(_importer_files(store, path))
            # Consumers already scanned against the old exporter set.
            enqueue(p for p, other in results.items() if path in other.barrels)
        enqueue(sorted(b for b in scan.barrels if b not in exporters and b not in results))
    return results


# --- Lexical fallback: the graph's line, the name as a code token --------


def _ts_is_name_leaf(kind: str) -> bool:
    return "identifier" in kind or kind in ("name", "constant")


def _ts_call_row(leaf: Any) -> Optional[int]:
    """The row where the call that *leaf* names as its callee starts.

    ``None`` when *leaf* is not a callee: an argument, or a name in a
    statement that is not a call.
    """
    current = leaf.parent
    while current is not None:
        kind = current.type
        if "argument" in kind:
            return None
        if "call" in kind or "invocation" in kind:
            return int(current.start_point[0])
        if kind.endswith(("statement", "declaration", "declarator")) or kind == "program":
            return None
        current = current.parent
    return None


def _ts_in_comment_or_string(tree: Any, row: int, start: int, end: int) -> bool:
    """Whether bytes ``start:end`` of *row* lie in a comment or a string.

    Code embedded in a string (``"$(name)"`` in a shell script, ``"#{name}"``)
    is code: the walk stops at the substitution or interpolation.
    """
    current = tree.root_node.descendant_for_point_range((row, start), (row, end))
    while current is not None:
        kind = current.type
        if "substitution" in kind or "interpolation" in kind:
            return False
        if "comment" in kind or "string" in kind:
            return True
        current = current.parent
    return False


def _lexical_sites(
    sources: _Sources,
    source: _Source,
    lines: dict[int, str],
    name: str,
    new_name: str,
    mode: str = "any",
) -> list[_Occurrence]:
    """The name as a code token on each of *lines* (line -> site).

    Python files are tokenized and other files read through their
    Tree-sitter grammar, so a comment, a string or a longer identifier
    never matches. A call the parser dates by the line its expression
    starts on (a chain split over several lines) is found where its name
    is. *mode* ``"attribute"`` keeps only ``x.name`` (Python) and
    ``"bare"`` only the plain name. In a file that is not Python, a line
    where no token spells the name falls back to the first whole-word
    match outside comments and strings, so a grammar that hides embedded
    code (a Vue template, a notebook) still gets its edit.
    """
    found: list[_Occurrence] = []
    tokened: set[int] = set()
    path = source.path
    if path.endswith(_PY_SUFFIXES):
        tokens = source.python_name_tokens(name)
        if tokens is not None:
            # Every name is a token, so a line without one has none to rename.
            tokened.update(lines)
            hits = [(line, col, attr, lines[line]) for line, col, attr in tokens if line in lines]
            calls = source.python_call_names(name)
            for start in lines:
                hits.extend(
                    (line, col, attr, lines[start]) for line, col, attr in calls.get(start, ())
                )
            for line, col, is_attribute, site in hits:
                if mode == "any" or (mode == "attribute") == is_attribute:
                    found.append((line, col, site, None))
    else:
        tree = sources.tree(path)
        if tree is not None and lines:
            wanted = name.encode("utf-8")
            stack = [tree.root_node]
            while stack:
                current = stack.pop()
                if current.child_count:
                    stack.extend(current.children)
                    continue
                kind = current.type
                if current.text != wanted or not _ts_is_name_leaf(kind):
                    continue
                line = current.start_point[0] + 1
                site_line: Optional[int] = line
                if line not in lines:
                    row = _ts_call_row(current)
                    site_line = row + 1 if row is not None and row + 1 in lines else None
                if site_line is None:
                    continue
                tokened.add(site_line)
                if mode == "bare" and kind == "property_identifier":
                    continue  # ``x.name`` or a ``name:`` key, not the binding
                replacement = (
                    f"{name}: {new_name}" if kind == "shorthand_property_identifier" else None
                )
                col = source.char_col(line, current.start_point[1])
                found.append((line, col, lines[site_line], replacement))
    word = _ident_pattern(name)
    fallback_tree = None
    for line, site in lines.items():
        if line in tokened:
            continue
        text = source.line(line)
        for match in word.finditer(text):
            if fallback_tree is None:
                fallback_tree = sources.tree(path) or False
            if fallback_tree:
                start = len(text[:match.start()].encode("utf-8"))
                end = start + len(name.encode("utf-8"))
                if _ts_in_comment_or_string(fallback_tree, line - 1, start, end):
                    continue
            found.append((line, match.start(), site, None))
            break
    return found


def _locate_definition(
    sources: _Sources, source: _Source, node: GraphNode, new_name: str,
) -> Optional[_Occurrence]:
    """The definition's name as a code token, searching past decorators."""
    last = max(node.line_start, min(node.line_end or node.line_start,
                                    node.line_start + _DEF_SEARCH_LINES))
    lines = {line: "definition" for line in range(node.line_start, last + 1)}
    hits = _lexical_sites(sources, source, lines, node.name, new_name)
    return min(hits, key=lambda hit: (hit[0], hit[1])) if hits else None


# --- Target resolution and the preview -------------------------------------


def _rename_target(store: GraphStore, old_name: str) -> Optional[GraphNode]:
    """The node *old_name* names exactly, or ``None``.

    A qualified name (absolute, or ``path/to/file.py::Symbol`` relative to
    the repository) picks one node. Otherwise the name must equal a symbol
    (``helper``, ``Widget.render``) or, failing that, a node's bare name;
    of several such nodes the first by file and line is taken, and the
    preview's ``qualified_name`` says which. There is no fuzzy fallback:
    the edits rename the node's own name, so asking for ``helper`` must
    never rename ``helper_count``.
    """
    node = store.get_node(old_name)
    if node is None and "::" in old_name:
        root = store.get_repo_root()
        if root:
            node = store.get_node(normalize_file_path(Path(root) / old_name))
    if node is not None or "::" in old_name:
        return node if node is not None and node.kind != "File" else None
    candidates = [
        n for n in store.search_nodes_by_qualified_tail(old_name, limit=500)
        if n.kind != "File"
    ] or [
        n for n in store.search_nodes(old_name, limit=500)
        if n.name == old_name and n.kind != "File"
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        logger.info(
            "rename_preview: %r names %d symbols; pass a qualified name to pick another",
            old_name, len(candidates),
        )
    return min(candidates, key=lambda n: (n.file_path, n.line_start))


def rename_preview(
    store: GraphStore,
    old_name: str,
    new_name: str,
) -> Optional[dict[str, Any]]:
    """Build a rename edit list for *old_name* -> *new_name*.

    *old_name* is a symbol name, a dotted symbol (``Widget.render``) or a
    qualified name (``pkg/util.py::helper``). Every edit names one exact
    place: a line and a 1-based ``column`` where the old name starts as a
    code token, plus the ``site`` it is (``definition``, ``call``,
    ``reference``, ``import``, ``export``, ``attribute`` or ``__all__``).
    Import statements are covered: ``from module import name``, its
    multi-line and aliased forms, relative imports, package re-exports with
    their ``__all__`` entries, ``import module`` with ``module.name``, and
    the JS/TS ``import { name }`` family. An alias keeps its own name, and
    a comment, a string or an unrelated name that contains the old one is
    never an edit. An edit carries no ``column`` only when its file could
    not be read; applying it then takes the first whole-word match.

    Returns:
        The preview dict, stored for ``apply_refactor``; ``None`` if no
        symbol has that name or its definition is no longer where the graph
        says (the graph is stale).
    """
    node = _rename_target(store, old_name)
    if node is None:
        logger.warning("rename_preview: node %r not found", old_name)
        return None

    edits = _rename_edits(store, node, new_name)
    if edits is None:
        logger.warning(
            "rename_preview: %s is not at %s:%d any more; the graph is stale",
            node.name, node.file_path, node.line_start,
        )
        return None

    stats = {"high": 0, "medium": 0, "low": 0}
    for e in edits:
        stats[e["confidence"]] += 1

    refactor_id = uuid.uuid4().hex[:8]
    preview: dict[str, Any] = {
        "refactor_id": refactor_id,
        "type": "rename",
        "old_name": _sanitize_name(old_name),
        "new_name": _sanitize_name(new_name),
        "qualified_name": _sanitize_name(node.qualified_name),
        "edits": edits,
        "stats": stats,
        "created_at": time.time(),
    }

    with _refactor_lock:
        _cleanup_expired()
        _pending_refactors[refactor_id] = preview

    logger.info(
        "rename_preview: created refactor %s (%s -> %s, %d edits)",
        refactor_id, old_name, new_name, len(edits),
    )
    return preview


def _rename_edits(
    store: GraphStore, node: GraphNode, new_name: str,
) -> Optional[list[dict[str, Any]]]:
    """Every edit renaming *node*, sorted; ``None`` if its definition moved."""
    name = node.name
    sources = _Sources(store.get_repo_root())

    # Lines the graph knows about: the definition, and call, reference and
    # symbol-level import edges that target this exact node.
    graph_lines: dict[str, dict[int, str]] = {}

    def note(path: str, line: int, site: str) -> None:
        graph_lines.setdefault(path, {}).setdefault(line, site)

    note(node.file_path, node.line_start, "definition")
    resolved_files: set[str] = set()
    for edge in store.get_edges_by_target(node.qualified_name):
        site = {"CALLS": "call", "REFERENCES": "reference", "IMPORTS_FROM": "import"}.get(
            edge.kind
        )
        if site:
            note(edge.file_path, edge.line, site)
            resolved_files.add(edge.file_path)

    language = node.language or ""
    exact: dict[str, list[_Occurrence]] = {}
    authoritative: set[str] = set()
    # Places a scan resolved to another binding of the name.
    kept: dict[str, set[tuple[int, int]]] = {}
    member = bool(node.parent_name)
    js_module_symbol = False
    # A bare-name CALLS edge is a guess about which same-named symbol a
    # call means, but it is the only evidence for a call no import spells.
    bare_edges = store.search_edges_by_target_name(
        name, kind="CALLS", language=language or None,
    )
    bare_files = {edge.file_path for edge in bare_edges}
    python_sites = (
        _python_rename_sites(store, node, sources, bare_files)
        if language == "python" else None
    )

    if python_sites is not None:
        # The scan read the modules with a call edge to the name as well as
        # those that import it, and resolved each use by scope.
        exact.update(python_sites)
        authoritative.update(python_sites)
    else:
        for edge in bare_edges:
            note(edge.file_path, edge.line, "call")
        if language in _JS_FAMILY and not member:
            scans = _js_rename_sites(store, node, new_name, sources, resolved_files | bare_files)
            if scans is not None:
                js_module_symbol = True
                for path, scan in scans.items():
                    exact[path] = scan.renames
                    kept[path] = scan.kept
                    if scan.authoritative:
                        authoritative.add(path)

    # A Python method is only ever reached as ``something.name``; a JS/TS
    # module-level symbol never is, except through the namespace objects
    # the scan already followed.
    mode = "any"
    if language == "python" and member:
        mode = "attribute"
    elif js_module_symbol:
        mode = "bare"

    occurrences: dict[str, list[_Occurrence]] = {
        path: list(found) for path, found in exact.items()
    }
    unreadable: dict[str, dict[int, str]] = {}
    for path, sites in graph_lines.items():
        if path in authoritative:
            continue
        source = sources.get(path)
        if source is None:
            unreadable[path] = sites
            continue
        file_occurrences = occurrences.setdefault(path, [])
        if path == node.file_path and not any(
            occ[2] == "definition" for occ in file_occurrences
        ):
            definition = _locate_definition(sources, source, node, new_name)
            if definition is None:
                return None
            file_occurrences.append(definition)
            sites = {ln: kind for ln, kind in sites.items() if ln != node.line_start}
        skip = kept.get(path, set())
        file_occurrences.extend(
            occ for occ in _lexical_sites(sources, source, sites, name, new_name, mode)
            if (occ[0], occ[1]) not in skip
        )

    if node.file_path not in unreadable and not any(
        occ[2] == "definition" for occ in occurrences.get(node.file_path, [])
    ):
        # Renaming the uses without the definition would break every one.
        return None

    edits: list[dict[str, Any]] = []
    for path, file_occurrences in occurrences.items():
        placed: set[tuple[int, int]] = set()
        for line, col, site, replacement in sorted(file_occurrences, key=lambda o: (o[0], o[1])):
            if (line, col) in placed:
                continue
            placed.add((line, col))
            edits.append({
                "file": path,
                "line": line,
                "column": col + 1,
                "old": name,
                "new": replacement or new_name,
                "site": site,
                "confidence": "high",
            })
    # Files that could not be read keep the graph's line; applying such an
    # edit takes the first whole-word match on that line.
    for path, sites in unreadable.items():
        for ln, kind in sorted(sites.items()):
            edits.append({
                "file": path, "line": ln, "old": name, "new": new_name,
                "site": kind, "confidence": "high",
            })
    edits.sort(key=lambda e: (e["file"], e["line"], e.get("column", 0)))
    return edits


# ---------------------------------------------------------------------------
# 2. find_dead_code
# ---------------------------------------------------------------------------


def _is_entry_point(node: Any) -> bool:
    """Check if a node looks like an entry point by name or decorator.

    Unlike ``flows.detect_entry_points()`` which treats ALL uncalled functions
    as entry points, this checks only for conventional name patterns and
    framework decorators -- the indicators that a function is *intentionally*
    an entry point rather than simply unreferenced dead code.
    """
    if _has_framework_decorator(node):
        return True
    if _matches_entry_name(node):
        return True
    return False


_MIN_PKG_SEGMENT_LEN = 4  # ignore short dirs like "src", "lib", "app"


@functools.lru_cache(maxsize=4096)
def _path_segments(file_path: str) -> tuple[str, ...]:
    """Return directory segments long enough to serve as package-name anchors."""
    parts = file_path.replace("\\", "/").split("/")
    return tuple(
        p for p in parts[:-1]  # skip the filename itself
        if len(p) >= _MIN_PKG_SEGMENT_LEN and p not in ("home", "src", "lib", "app")
    )


# Matches identifiers inside type annotations (e.g. "GoalCreate" in
# "body: GoalCreate", "Optional[UserResponse]", "list[Item]").
_TYPE_IDENT_RE = re.compile(r"[A-Z][A-Za-z0-9_]*")


def _collect_type_referenced_names(store: GraphStore) -> set[str]:
    """Collect class names that appear in function params or return types."""
    funcs = store.get_nodes_by_kind(kinds=["Function", "Test"])
    names: set[str] = set()
    for f in funcs:
        for text in (f.params, f.return_type):
            if text:
                names.update(_TYPE_IDENT_RE.findall(text))
    return names


def find_dead_code(
    store: GraphStore,
    kind: Optional[str] = None,
    file_pattern: Optional[str] = None,
    root: Optional[Union[str, Path]] = None,
) -> list[dict[str, Any]]:
    """Find functions/classes with no callers, no test refs, no importers, and no references.

    Entry points (functions matching framework decorators or conventional name
    patterns like ``main``, ``test_*``, ``handle_*``) are excluded.

    .. note::

        **Caveats — dynamic dispatch patterns.**  Static analysis cannot track
        all runtime-determined call patterns.  Functions registered via fully
        dynamic keys (``map[computedKey()] = fn``), ``Reflect.apply``, or
        runtime ``require()`` may still appear as dead code.  Treat results as
        hints, especially for TypeScript projects that use map-based dispatch,
        plugin registries, or dynamic requires.

    Args:
        store: The GraphStore instance.
        kind: Optional filter (e.g. ``"Function"`` or ``"Class"``).
        file_pattern: Optional file-path substring filter.
        root: Optional repo root path for computing ``relative_path``.

    Returns:
        List of dead-code dicts with name, qualified_name, kind, file_path,
        relative_path, line, and language fields.
    """
    # Query candidate nodes.
    candidates = store.get_nodes_by_kind(
        kinds=[kind] if kind else ["Function", "Class"],
        file_pattern=file_pattern,
    )

    # Stored file paths are absolute. Test-ness is read from the path
    # relative to this root so ``tests/`` means the repository's own tests
    # and not a directory above the checkout (#1023).
    test_root = root or store.get_repo_root()

    def _package_segments(file_path: str) -> tuple[str, ...]:
        """Directory segments of *file_path* that could name a package.

        Only the part inside the repository counts. The directories above a
        checkout belong to whoever cloned it, so matching them against an
        import specifier makes the answer depend on where the repository
        happens to live: a job directory named "test" or "common" would
        accept callers that a checkout elsewhere rejects (#1023). When the
        root is unknown there is nothing better than the whole path, which is
        what this did before roots were recorded.
        """
        return _path_segments(_repo_relative(file_path, test_root) or file_path)

    def _in_test_file(file_path: str) -> bool:
        """Whether *file_path* is test code, for dead-code purposes.

        Dead-code detection opts into the ambiguous ``test-utils/`` and
        ``test_utils/`` directories, which the shared default leaves out
        because shared libraries are published from them. Here the trade is
        the other way round: a wrong "this is dead" report sends a reviewer
        to delete working code, while over-suppressing only costs a hint.
        """
        return _is_test_file(file_path, test_root, include_helper_dirs=True)

    # Build set of class names referenced in function type annotations.
    type_ref_names = _collect_type_referenced_names(store)

    # Build class hierarchy: class_qualified_name -> [bare_base_names]
    class_bases: dict[str, list[str]] = {}
    conn = store._conn
    for row in conn.execute(
        "SELECT source_qualified, target_qualified FROM edges WHERE kind = 'INHERITS'"
    ).fetchall():
        base = row[1].rsplit("::", 1)[-1] if "::" in row[1] else row[1]
        class_bases.setdefault(row[0], []).append(base)

    # Build import graph: file_path -> set of file_paths it imports from.
    # Used to filter bare-name caller matches to plausible callers.
    importer_files: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT file_path, target_qualified FROM edges WHERE kind = 'IMPORTS_FROM'"
    ).fetchall():
        importer_files.setdefault(row[0], set()).add(row[1])

    # Build set of globally unique names (only one non-test node with that name).
    # For unique names, any bare-name CALLS edge is reliable — no ambiguity.
    name_counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT name, COUNT(*) FROM nodes "
        "WHERE kind IN ('Function', 'Class') AND is_test = 0 "
        "GROUP BY name"
    ).fetchall():
        name_counts[row[0]] = row[1]

    def _is_plausible_caller(
        edge_file: str, node_file: str, node_name: str = "",
    ) -> bool:
        """A bare-name edge is plausible if it comes from the same file,
        from a file that has an IMPORTS_FROM edge whose target matches
        the node's file path, or the name is globally unique (no ambiguity)."""
        if edge_file == node_file:
            return True
        # Unique names (only one definition) have no ambiguity -- accept all callers.
        if node_name and name_counts.get(node_name, 0) == 1:
            return True
        for imp_target in importer_files.get(edge_file, ()):
            # Strip "::name" suffix — workspace-resolved imports may include it
            imp_path = imp_target.split("::")[0] if "::" in imp_target else imp_target
            # __init__.py represents its parent package directory
            if imp_path.endswith("/__init__.py"):
                imp_dir = imp_path[:-12]  # strip "/__init__.py"
                if node_file.startswith(imp_dir + "/"):
                    return True
            if imp_path.startswith(node_file) or node_file.startswith(imp_path + "/"):
                return True
            # 2-hop: edge_file imports X, X re-exports from node_file (barrel files)
            for imp2 in importer_files.get(imp_target, ()):
                imp2_path = imp2.split("::")[0] if "::" in imp2 else imp2
                if imp2_path.endswith("/__init__.py"):
                    imp2_dir = imp2_path[:-12]
                    if node_file.startswith(imp2_dir + "/"):
                        return True
                if imp2_path.startswith(node_file) or node_file.startswith(imp2_path + "/"):
                    return True
            # Package-alias heuristic: monorepo imports like "@scope/pkg-name"
            # contain the directory name of the target package.  Check if the
            # import target string contains a significant directory segment from
            # the node's file path (e.g. "lambda-common" in both the import
            # "@cova-utils/lambda-common" and the path "libraries/lambda-common/...").
            if not imp_target.startswith("/"):
                # imp_target is a package specifier, not a file path
                for seg in _package_segments(node_file):
                    if seg in imp_target:
                        return True
        return False

    dead: list[dict[str, Any]] = []

    for node in candidates:

        # Skip test nodes and anything defined in test files.
        if node.is_test or _in_test_file(node.file_path):
            continue

        # Skip ambient type declarations (.d.ts) — they describe external APIs.
        if node.file_path.endswith(".d.ts"):
            continue

        # Skip dunder methods -- invoked by runtime, never have explicit callers.
        if node.name.startswith("__") and node.name.endswith("__"):
            continue

        # Skip JS/TS/Java constructors -- invoked via `new ClassName()`, which
        # creates a CALLS edge to the class, not to `constructor`.
        if node.name == "constructor" and node.parent_name:
            continue

        # Skip mock/stub variables in test files -- these are test helpers
        # referenced via variable assignment, not function calls.
        if node.is_test or _in_test_file(node.file_path):
            if _MOCK_NAME_RE.search(node.name):
                continue

        if node.extra.get("verilog_kind"):
            continue

        # Skip entry points (by name pattern or decorator, not just "uncalled").
        if _is_entry_point(node):
            continue

        # Check for callers (CALLS), test refs (TESTED_BY), importers (IMPORTS_FROM),
        # and value references (REFERENCES -- function-as-value in maps, arrays, etc.).

        # Skip classes referenced in type annotations (Pydantic schemas, etc.).
        if node.kind == "Class" and node.name in type_ref_names:
            continue

        # Skip Angular/NestJS decorated classes -- they are framework-managed
        # and instantiated by the DI container, not direct CALLS edges.
        if node.kind == "Class" and _has_framework_decorator(node):
            continue

        # Skip classes (and their methods) inheriting from known framework bases.
        _is_framework_class = False
        _check_qn = node.qualified_name if node.kind == "Class" else (
            node.qualified_name.rsplit(".", 1)[0] if node.parent_name else None
        )
        if _check_qn:
            outgoing = store.get_edges_by_source(_check_qn)
            base_names = {
                e.target_qualified.rsplit("::", 1)[-1]
                for e in outgoing if e.kind == "INHERITS"
            }
            if base_names & _FRAMEWORK_BASE_CLASSES:
                _is_framework_class = True
        if node.kind == "Class":
            if _is_framework_class:
                continue
            # Fallback: CDK class name suffixes (no INHERITS edge for external bases)
            if any(node.name.endswith(s) for s in _CDK_CLASS_SUFFIXES):
                continue
        if node.kind == "Function" and _is_framework_class:
            continue
        # Also skip methods whose parent class name matches CDK suffixes
        # (fallback for external base classes without INHERITS edges).
        if (
            node.kind == "Function"
            and node.parent_name
            and any(node.parent_name.endswith(s) for s in _CDK_CLASS_SUFFIXES)
        ):
            continue

        # Skip decorated functions/classes that are invoked implicitly rather
        # than via explicit CALLS edges.
        decorators = node.extra.get("decorators", ())
        if isinstance(decorators, (list, tuple)) and decorators:
            if node.kind in ("Function", "Test"):
                # @property -- invoked via attribute access
                # @abstractmethod -- polymorphic dispatch, never called directly
                # @classmethod/@staticmethod -- called via Class.method()
                if any(
                    d in ("property", "abstractmethod", "classmethod", "staticmethod")
                    or d.endswith(".abstractmethod")
                    # Angular @HostListener -- method called by framework event system
                    or d.startswith("HostListener")
                    for d in decorators
                ):
                    continue
            if node.kind == "Class":
                # @dataclass classes are instantiated as types, not via CALLS
                if any("dataclass" in d for d in decorators):
                    continue

        # Skip methods that override an @abstractmethod in a base class --
        # they are called polymorphically via the base class reference.
        if node.kind == "Function" and node.parent_name:
            parent_qn = node.qualified_name.rsplit(".", 1)[0]
            parent_edges = store.get_edges_by_source(parent_qn)
            base_class_names = [
                e.target_qualified for e in parent_edges if e.kind == "INHERITS"
            ]
            for base_name in base_class_names:
                # Try fully-qualified base first, then bare name match
                base_method_qn = f"{base_name}.{node.name}"
                base_nodes = store.get_node(base_method_qn)
                if base_nodes is None:
                    # Base class may be bare name -- search in same file
                    base_method_qn2 = (
                        node.file_path + "::" + base_name + "." + node.name
                    )
                    base_nodes = store.get_node(base_method_qn2)
                if base_nodes is not None:
                    base_decos = base_nodes.extra.get("decorators", ())
                    if isinstance(base_decos, (list, tuple)) and any(
                        "abstractmethod" in d for d in base_decos
                    ):
                        break
            else:
                base_name = None  # no abstract override found
            if base_name is not None:
                continue

        incoming = [
            e for e in store.get_edges_by_target(node.qualified_name)
            if not _STORYBOOK_FILE_RE.search(e.file_path)
        ]
        # Also check class-qualified edges (e.g. "ClassName::method") which
        # lack the file-path prefix used in node.qualified_name.
        if not any(e.kind == "CALLS" for e in incoming) and node.parent_name:
            class_qn = f"{node.parent_name}::{node.name}"
            incoming += [
                e for e in store.get_edges_by_target(class_qn)
                if not _STORYBOOK_FILE_RE.search(e.file_path)
            ]
        # Also check bare-name and partially-qualified edges.
        # CALLS targets may be bare ("funcName"), class-qualified
        # ("Class::method"), or workspace-qualified ("pkg/dir::funcName").
        if not any(e.kind == "CALLS" for e in incoming):
            bare = store.search_edges_by_target_name(
                node.name,
                kind="CALLS",
                language=node.language or None,
            )
            # Also search for partially-qualified targets ending with ::name
            suffix_rows = conn.execute(
                "SELECT * FROM edges WHERE kind = 'CALLS'"
                " AND target_qualified LIKE ?",
                (f"%::{node.name}",),
            ).fetchall()
            suffix_edges = [store._row_to_edge(r) for r in suffix_rows]
            all_bare = bare + suffix_edges
            all_bare = [
                e for e in all_bare
                if _is_plausible_caller(e.file_path, node.file_path, node.name)
            ]
            incoming = incoming + all_bare
        # TESTED_BY edges are stored as source=production, target=test by the
        # parser, so a tested production node is the *source* of its TESTED_BY
        # edge -- look outgoing, not incoming. See: #515
        outgoing_tb = [
            e for e in store.get_edges_by_source(node.qualified_name)
            if e.kind == "TESTED_BY"
        ]
        if not outgoing_tb and node.parent_name:
            # Class-qualified source (e.g. "ClassName::method") which lacks the
            # file-path prefix used in node.qualified_name.
            class_qn = f"{node.parent_name}::{node.name}"
            outgoing_tb += [
                e for e in store.get_edges_by_source(class_qn)
                if e.kind == "TESTED_BY"
            ]
        if not outgoing_tb:
            # Bare-name source fallback: unresolved TESTED_BY edges may store the
            # production function by its plain name (e.g. "authenticate").
            bare_rows = conn.execute(
                "SELECT * FROM edges WHERE kind = 'TESTED_BY' AND source_qualified = ?",
                (node.name,),
            ).fetchall()
            outgoing_tb += [
                e for e in (store._row_to_edge(r) for r in bare_rows)
                if _is_plausible_caller(e.file_path, node.file_path, node.name)
            ]
        # Check INHERITS -- classes with subclasses are not dead.
        if node.kind == "Class" and not any(e.kind == "INHERITS" for e in incoming):
            bare_inh = store.search_edges_by_target_name(
                node.name,
                kind="INHERITS",
                language=node.language or None,
            )
            incoming = incoming + bare_inh
        # Stories demonstrate a component but do not prove application use.
        # Check paths as well as flags so pre-existing graphs behave correctly.
        incoming = [e for e in incoming if not _STORYBOOK_FILE_RE.search(e.file_path)]
        outgoing_tb = [e for e in outgoing_tb if not _STORYBOOK_FILE_RE.search(e.file_path)]
        has_callers = any(e.kind == "CALLS" for e in incoming)
        has_test_refs = bool(outgoing_tb)
        has_importers = any(e.kind == "IMPORTS_FROM" for e in incoming)
        has_references = any(e.kind == "REFERENCES" for e in incoming)
        has_subclasses = any(e.kind == "INHERITS" for e in incoming)

        # For classes with no direct references, check if any member has callers.
        no_refs = not (
            has_callers or has_test_refs or has_importers
            or has_references or has_subclasses
        )
        if node.kind == "Class" and no_refs:
            member_prefix = node.qualified_name + "."
            # Also check bare class-name pattern (unresolved CALLS targets)
            bare_prefix = node.name + "."
            member_calls = conn.execute(
                "SELECT file_path FROM edges WHERE kind = 'CALLS'"
                " AND (target_qualified LIKE ? OR target_qualified LIKE ?)",
                (f"%{member_prefix}%", f"%{bare_prefix}%"),
            )
            if any(not _STORYBOOK_FILE_RE.search(row[0]) for row in member_calls):
                has_callers = True

        if not (
            has_callers or has_test_refs or has_importers
            or has_references or has_subclasses
        ):
            # Check if this is a method override where the base class method
            # has callers (polymorphic dispatch: callers of Base.method()
            # implicitly call SubClass.method() at runtime).
            if node.kind == "Function" and node.parent_name and not has_callers:
                method_suffix = "." + node.name
                if node.qualified_name.endswith(method_suffix):
                    class_qn = node.qualified_name[: -len(method_suffix)]
                    for base_name in class_bases.get(class_qn, []):
                        rows = conn.execute(
                            "SELECT n.qualified_name FROM nodes n "
                            "WHERE n.parent_name = ? AND n.name = ? "
                            "AND n.kind IN ('Function', 'Test')",
                            (base_name, node.name),
                        ).fetchall()
                        for (base_method_qn,) in rows:
                            if conn.execute(
                                "SELECT 1 FROM edges "
                                "WHERE target_qualified = ? AND kind = 'CALLS' "
                                "LIMIT 1",
                                (base_method_qn,),
                            ).fetchone():
                                has_callers = True
                                break
                        if has_callers:
                            break

            if not has_callers:
                if root:
                    try:
                        rel = str(Path(node.file_path).relative_to(root))
                    except ValueError:
                        rel = node.file_path
                else:
                    rel = node.file_path
                dead.append({
                    "name": _sanitize_name(node.name),
                    "qualified_name": _sanitize_name(node.qualified_name),
                    "kind": node.kind,
                    "file": node.file_path,
                    "file_path": node.file_path,
                    "relative_path": rel,
                    "line": node.line_start,
                    "language": node.language,
                })

    logger.info("find_dead_code: found %d dead symbols", len(dead))
    return dead


# ---------------------------------------------------------------------------
# 3. suggest_refactorings
# ---------------------------------------------------------------------------


def suggest_refactorings(store: GraphStore) -> list[dict[str, Any]]:
    """Produce community-driven refactoring suggestions.

    Currently two categories:
    - **move**: Functions in Community A only called by Community B.
    - **remove**: Dead code (no callers, tests, or importers and not entry points).

    Returns:
        List of suggestion dicts with type, description, symbols, rationale.
    """
    suggestions: list[dict[str, Any]] = []

    # --- Dead code suggestions ---
    dead = find_dead_code(store)
    for d in dead:
        suggestions.append({
            "type": "remove",
            "description": f"Remove unused {d['kind'].lower()} '{d['name']}'",
            "symbols": [d["qualified_name"]],
            "rationale": "No callers, no test references, no importers, not an entry point.",
        })

    # --- Cross-community move suggestions ---
    # Only attempt if communities table exists and has data.
    community_rows = store.get_communities_list()

    if community_rows:
        # Build node -> community_id mapping.
        node_community: dict[str, int] = {}
        for crow in community_rows:
            cid = crow["id"]
            member_qns = store.get_community_member_qns(cid)
            for qn in member_qns:
                node_community[qn] = cid

        community_names: dict[int, str] = {
            r["id"]: r["name"] for r in community_rows
        }

        # Check functions called only by members of a different community.
        all_funcs = [
            node for node in store.get_nodes_by_kind(["Function"])
            if not node.extra.get("verilog_kind")
        ]

        for fnode in all_funcs:
            f_community = node_community.get(fnode.qualified_name)
            if f_community is None:
                continue

            incoming_calls = [
                e for e in store.get_edges_by_target(fnode.qualified_name)
                if e.kind == "CALLS"
            ]
            if not incoming_calls:
                continue

            caller_communities = set()
            for edge in incoming_calls:
                c_community = node_community.get(edge.source_qualified)
                if c_community is not None:
                    caller_communities.add(c_community)

            # If ALL callers are from a single *different* community, suggest move.
            if len(caller_communities) == 1:
                target_community = next(iter(caller_communities))
                if target_community != f_community:
                    src_name = community_names.get(f_community, f"community-{f_community}")
                    tgt_name = community_names.get(
                        target_community, f"community-{target_community}"
                    )
                    suggestions.append({
                        "type": "move",
                        "description": (
                            f"Move '{_sanitize_name(fnode.name)}' from "
                            f"'{src_name}' to '{tgt_name}'"
                        ),
                        "symbols": [_sanitize_name(fnode.qualified_name)],
                        "rationale": (
                            f"Function is in community '{src_name}' but only "
                            f"called by members of community '{tgt_name}'."
                        ),
                    })

    logger.info("suggest_refactorings: produced %d suggestions", len(suggestions))
    return suggestions


# ---------------------------------------------------------------------------
# 4. apply_refactor
# ---------------------------------------------------------------------------


def _apply_file_edits(
    text: str, file_edits: list[dict[str, Any]],
) -> tuple[str, int, int]:
    """Apply one file's rename edits; return ``(new_text, applied, skipped)``.

    An edit with a ``column`` replaces the old name at exactly that place and
    is skipped if the file no longer has it there. An edit without one (a
    preview of a file that could not be read) takes the first whole-word
    match on its line that no other edit claimed. Nothing is ever replaced
    outside the edit's own line, and never inside a longer identifier.
    Every edit is located against the original text and the replacements
    are made right to left, so edits on one line cannot shift each other.
    """
    lines = text.split("\n")
    claimed: dict[tuple[int, int], tuple[str, str]] = {}
    applied = skipped = 0
    ordered = sorted(file_edits, key=lambda e: e.get("column") is None)
    for edit in ordered:
        old_text = str(edit.get("old", ""))
        new_text = str(edit.get("new", ""))
        index = int(edit.get("line") or 0) - 1
        if not old_text or not 0 <= index < len(lines):
            skipped += 1
            continue
        line = lines[index]
        word = _ident_pattern(old_text)
        column = edit.get("column")
        start: Optional[int] = None
        if column is not None:
            candidate = int(column) - 1
            match = word.match(line, candidate) if candidate >= 0 else None
            # ``match`` anchors the start; the lookbehind still sees the
            # character before it, so a longer identifier cannot match.
            if match is not None and match.start() == candidate:
                start = candidate
        else:
            start = next(
                (m.start() for m in word.finditer(line) if (index, m.start()) not in claimed),
                None,
            )
        if start is None:
            skipped += 1
            continue
        if (index, start) in claimed:
            continue  # the same place twice: one replacement
        claimed[(index, start)] = (old_text, new_text)
        applied += 1

    by_line: dict[int, list[tuple[int, str, str]]] = {}
    for (index, start), (old_text, new_text) in claimed.items():
        by_line.setdefault(index, []).append((start, old_text, new_text))
    for index, replacements in by_line.items():
        line = lines[index]
        for start, old_text, new_text in sorted(replacements, reverse=True):
            line = line[:start] + new_text + line[start + len(old_text):]
        lines[index] = line
    return "\n".join(lines), applied, skipped


def apply_refactor(
    refactor_id: str,
    repo_root: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply a previously previewed refactoring to source files.

    Validates the refactor_id, checks expiry, ensures all edit paths are
    within the repo root, then performs exact string replacements on the
    target files.

    Args:
        refactor_id: ID from a prior ``rename_preview`` call.
        repo_root: Validated repository root path.
        dry_run: If True, compute the would-be changes and return a
            unified-diff representation per affected file, but do NOT
            write anything to disk. The ``refactor_id`` is preserved so
            the same preview can be committed afterwards via a second
            call without ``dry_run``. See: #176

    Returns:
        Status dict with applied count and modified files. When
        ``dry_run=True`` the dict additionally contains:

        - ``dry_run``: ``True``
        - ``would_modify``: list of file paths that would be changed
        - ``diffs``: map of file path → unified diff string showing the
          proposed change
    """
    repo_root = repo_root.resolve()

    with _refactor_lock:
        _cleanup_expired()
        preview = _pending_refactors.get(refactor_id)

    if preview is None:
        logger.warning("apply_refactor: unknown or expired refactor_id %s", refactor_id)
        return {"status": "error", "error": f"Refactor '{refactor_id}' not found or expired."}

    # Check expiry explicitly.
    age = time.time() - preview["created_at"]
    if age > REFACTOR_EXPIRY_SECONDS:
        with _refactor_lock:
            _pending_refactors.pop(refactor_id, None)
        logger.warning("apply_refactor: refactor %s expired (%.0fs old)", refactor_id, age)
        return {"status": "error", "error": f"Refactor '{refactor_id}' has expired."}

    edits = preview.get("edits", [])
    if not edits:
        if dry_run:
            return {
                "status": "ok", "dry_run": True, "applied": 0,
                "files_modified": [], "edits_applied": 0,
                "would_modify": [], "diffs": {},
            }
        return {"status": "ok", "applied": 0, "files_modified": [], "edits_applied": 0}

    # --- Path traversal validation ---
    for edit in edits:
        edit_path = Path(edit["file"]).resolve()
        try:
            edit_path.relative_to(repo_root)
        except ValueError:
            logger.error(
                "apply_refactor: path traversal blocked for %s (repo_root=%s)",
                edit_path, repo_root,
            )
            return {
                "status": "error",
                "error": f"Edit path '{edit['file']}' is outside repo root.",
            }

    # --- Compute new content for every edit (shared by dry-run and write paths) ---
    # Group edits by file so multiple edits to the same file apply
    # sequentially against the updated content rather than stomping each
    # other. Dry-run and write modes then share this computation.
    from collections import defaultdict
    edits_by_file: dict[str, list[dict]] = defaultdict(list)
    for edit in edits:
        edits_by_file[edit["file"]].append(edit)

    # file -> (old_content, new_content, edit_count, had_bom)
    planned: dict[str, tuple[str, str, int, bool]] = {}
    edits_skipped = 0
    for file_str, file_edits in edits_by_file.items():
        file_path = Path(file_str)
        if not file_path.is_file():
            logger.warning("apply_refactor: file not found: %s", file_path)
            edits_skipped += len(file_edits)
            continue
        try:
            decoded = _decode_source(file_path.read_bytes())
        except OSError as exc:
            logger.warning("apply_refactor: could not read %s: %s", file_path, exc)
            edits_skipped += len(file_edits)
            continue
        if decoded is None:
            # Rewriting a file that is not UTF-8 would replace every byte
            # that failed to decode, so it is left untouched.
            logger.warning("apply_refactor: %s is not UTF-8; not rewritten", file_path)
            edits_skipped += len(file_edits)
            continue
        original, had_bom = decoded
        content, applied, skipped = _apply_file_edits(original, file_edits)
        edits_skipped += skipped
        if skipped:
            logger.warning(
                "apply_refactor: %d edit(s) in %s no longer match the file",
                skipped, file_path,
            )
        if applied > 0:
            planned[file_str] = (original, content, applied, had_bom)

    # --- Dry-run path: return diffs, no writes ---
    if dry_run:
        import difflib
        diffs: dict[str, str] = {}
        for file_str, (original, new_content, _count, _bom) in planned.items():
            diff_lines = list(difflib.unified_diff(
                original.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{file_str}",
                tofile=f"b/{file_str}",
                n=3,
            ))
            diffs[file_str] = "".join(diff_lines)
        total_edits = sum(count for _o, _n, count, _b in planned.values())
        result = {
            "status": "ok",
            "dry_run": True,
            "applied": 0,
            "edits_applied": total_edits,
            "would_modify": sorted(planned.keys()),
            "files_modified": [],
            "diffs": diffs,
        }
        if edits_skipped:
            result["edits_skipped"] = edits_skipped
        logger.info(
            "apply_refactor: dry-run %s — %d edits would be applied to %d files",
            refactor_id, total_edits, len(planned),
        )
        # Do NOT pop the pending refactor — let the user commit via a
        # second call with dry_run=False.
        return result

    # --- Real-write path: write the pre-computed new content ---
    files_modified: set[str] = set()
    edits_applied = 0
    for file_str, (_original, new_content, count, had_bom) in planned.items():
        file_path = Path(file_str)
        try:
            # Bytes, not text mode: text mode would turn CRLF into LF.
            prefix = "\ufeff" if had_bom else ""
            file_path.write_bytes((prefix + new_content).encode("utf-8"))
            edits_applied += count
            files_modified.add(str(file_path))
            logger.info("apply_refactor: applied %d edit(s) to %s", count, file_path)
        except OSError as exc:
            logger.error("apply_refactor: could not write %s: %s", file_path, exc)

    # Remove from pending after successful application.
    with _refactor_lock:
        _pending_refactors.pop(refactor_id, None)

    result = {
        "status": "ok",
        "applied": edits_applied,
        "files_modified": sorted(files_modified),
        "edits_applied": edits_applied,
    }
    if edits_skipped:
        result["edits_skipped"] = edits_skipped
    logger.info("apply_refactor: completed %s — %d edits applied", refactor_id, edits_applied)
    return result
