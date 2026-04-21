"""Tree-sitter based multi-language code parser.

Extracts structural nodes (classes, functions, imports, types) and edges
(calls, inheritance, contains) from source files.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, Optional

import tree_sitter_language_pack as tslp

from .tsconfig_resolver import TsconfigResolver


class CellInfo(NamedTuple):
    """Represents a single cell in a notebook with its language."""
    cell_index: int
    language: str
    source: str


_SQL_TABLE_RE = re.compile(
    r"(?:FROM|JOIN|INTO|CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW)|INSERT\s+OVERWRITE)"
    r"\s+((?:`[^`]+`|\w+)(?:\.(?:`[^`]+`|\w+))*)",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models for extracted entities
# ---------------------------------------------------------------------------


@dataclass
class NodeInfo:
    kind: str  # File, Class, Function, Type, Test
    name: str
    file_path: str
    line_start: int
    line_end: int
    language: str = ""
    parent_name: Optional[str] = None  # enclosing class/module
    params: Optional[str] = None
    return_type: Optional[str] = None
    modifiers: Optional[str] = None
    is_test: bool = False
    extra: dict = field(default_factory=dict)


@dataclass
class EdgeInfo:
    # CALLS, IMPORTS_FROM, INHERITS, IMPLEMENTS, CONTAINS,
    # TESTED_BY, DEPENDS_ON, REFERENCES
    kind: str
    source: str  # qualified name or path
    target: str  # qualified name or path
    file_path: str
    line: int = 0
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Language extension mapping
# ---------------------------------------------------------------------------

EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".cs": "csharp",
    ".rb": "ruby",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".c": "c",
    ".h": "c",
    ".hpp": "cpp",
    ".kt": "kotlin",
    ".swift": "swift",
    ".php": "php",
    ".scala": "scala",
    ".sol": "solidity",
    ".vue": "vue",
    ".dart": "dart",
    ".r": "r",  # .lower() in detect_language handles .R → .r
    ".mjs": "javascript",
    ".astro": "typescript",
    ".pl": "perl",
    ".pm": "perl",
    ".t": "perl",
    ".xs": "c",  # Perl XS: parsed as C to capture functions/structs/includes
    ".lua": "lua",
    ".luau": "luau",
    ".m": "objc",  # Objective-C (.h still maps to C; .mm defers to C++ for simplicity)
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".ksh": "bash",  # Korn shell — close enough to bash for tree-sitter-bash (#235)
    ".ex": "elixir",
    ".exs": "elixir",
    ".ipynb": "notebook",
    ".zig": "zig",
    ".ps1": "powershell",
    ".psm1": "powershell",
    ".psd1": "powershell",
    ".svelte": "svelte",
    ".jl": "julia",
    # ReScript: .res is implementation, .resi is interface. Both share one
    # language label; the parser flags interface files via extra metadata.
    # No tree-sitter grammar is bundled in tree_sitter_language_pack, so
    # extraction is regex-based (see _parse_rescript).
    ".res": "rescript",
    ".resi": "rescript",
    ".gd": "gdscript",
}

# Tree-sitter node type mappings per language
# Maps (language) -> dict of semantic role -> list of TS node types
_CLASS_TYPES: dict[str, list[str]] = {
    "python": ["class_definition"],
    "javascript": ["class_declaration", "class"],
    "typescript": ["class_declaration", "class"],
    "tsx": ["class_declaration", "class"],
    "go": ["type_declaration"],
    "rust": ["struct_item", "enum_item", "impl_item"],
    "java": ["class_declaration", "interface_declaration", "enum_declaration"],
    "c": ["struct_specifier", "type_definition"],
    "cpp": ["class_specifier", "struct_specifier"],
    "csharp": [
        "class_declaration", "interface_declaration",
        "enum_declaration", "struct_declaration",
    ],
    "ruby": ["class", "module"],
    "r": [],  # Classes detected via call pattern-matching, not AST node types
    "perl": ["package_statement", "class_statement", "role_statement"],
    "kotlin": ["class_declaration", "object_declaration"],
    "swift": ["class_declaration", "struct_declaration", "protocol_declaration"],
    "php": ["class_declaration", "interface_declaration"],
    "scala": [
        "class_definition", "trait_definition", "object_definition", "enum_definition",
    ],
    "solidity": [
        "contract_declaration", "interface_declaration", "library_declaration",
        "struct_declaration", "enum_declaration", "error_declaration",
        "user_defined_type_definition",
    ],
    "dart": ["class_definition", "mixin_declaration", "enum_declaration"],
    "lua": [],  # Lua has no class keyword; table-based OOP handled via constructs handler
    "luau": ["type_definition"],  # Luau type aliases; table-based OOP via constructs handler
    "objc": [
        "class_interface", "class_implementation",
        "category_interface", "protocol_declaration",
    ],
    "bash": [],  # Shell has no classes
    # Elixir: `defmodule Name do ... end` is a ``call`` node whose first
    # identifier is literally "defmodule". Dispatched via
    # _extract_elixir_constructs to avoid matching every ``call`` here.
    "elixir": [],
    "zig": ["container_declaration"],
    "powershell": ["class_statement"],
    "julia": ["struct_definition", "abstract_definition"],
}

_FUNCTION_TYPES: dict[str, list[str]] = {
    "python": ["function_definition"],
    "javascript": ["function_declaration", "method_definition", "arrow_function"],
    "typescript": ["function_declaration", "method_definition", "arrow_function"],
    "tsx": ["function_declaration", "method_definition", "arrow_function"],
    "go": ["function_declaration", "method_declaration"],
    "rust": ["function_item"],
    "java": ["method_declaration", "constructor_declaration"],
    "c": ["function_definition"],
    "cpp": ["function_definition"],
    "csharp": ["method_declaration", "constructor_declaration"],
    "ruby": ["method", "singleton_method"],
    "r": ["function_definition"],
    "perl": ["subroutine_declaration_statement", "method_declaration_statement"],
    "kotlin": ["function_declaration"],
    "swift": ["function_declaration"],
    "php": ["function_definition", "method_declaration"],
    "scala": ["function_definition", "function_declaration"],
    # Solidity: events and modifiers use kind="Function" because the graph
    # schema has no dedicated kind for them.  State variables are also modeled
    # as Function nodes (public ones auto-generate getters) and distinguished
    # via extra["solidity_kind"].
    "solidity": [
        "function_definition", "constructor_definition", "modifier_definition",
        "event_definition", "fallback_receive_definition",
    ],
    # Dart: function_signature covers both top-level functions and class methods
    # (class methods appear as method_signature > function_signature pairs;
    # the parser recurses into method_signature generically and then matches
    # function_signature inside it).
    "dart": ["function_signature"],
    "lua": ["function_declaration"],
    "luau": ["function_declaration"],
    # Objective-C: method_definition lives inside implementation_definition
    # inside class_implementation. C-style function_definition is also present
    # for main() and helper functions.
    "objc": ["method_definition", "function_definition"],
    # Bash: only function_definition; everything else is a command.
    "bash": ["function_definition"],
    # Elixir: def/defp/defmacro are all ``call`` nodes whose first
    # identifier matches. Dispatched via _extract_elixir_constructs.
    "elixir": [],
    "zig": ["fn_proto", "fn_decl"],
    "powershell": ["function_statement"],
    "julia": [
        "function_definition",
        "short_function_definition",
    ],
}

_IMPORT_TYPES: dict[str, list[str]] = {
    "python": ["import_statement", "import_from_statement"],
    "javascript": ["import_statement"],
    "typescript": ["import_statement"],
    "tsx": ["import_statement"],
    "go": ["import_declaration"],
    "rust": ["use_declaration"],
    "java": ["import_declaration"],
    "c": ["preproc_include"],
    "cpp": ["preproc_include"],
    "csharp": ["using_directive"],
    "ruby": ["call"],  # require/require_relative
    "r": ["call"],  # library(), require(), source() — filtered downstream
    "perl": ["use_statement", "require_expression"],
    "kotlin": ["import_header"],
    "swift": ["import_declaration"],
    "php": ["namespace_use_declaration"],
    "scala": ["import_declaration"],
    "solidity": ["import_directive"],
    # Dart: import_or_export wraps library_import > import_specification > configurable_uri
    "dart": ["import_or_export"],
    # Lua/Luau: require() is a function_call, handled via _extract_lua_constructs
    "lua": [],
    "luau": [],
    # Objective-C: #import "..." and #include "..." both arrive as preproc_include
    # (tree-sitter-objc doesn't distinguish via a separate preproc_import node).
    "objc": ["preproc_include"],
    # Bash: source / . <file> is a command — handled in _extract_bash_source below.
    "bash": [],
    # Elixir: alias/import/require/use are all ``call`` nodes —
    # handled in _extract_elixir_constructs.
    "elixir": [],
    # Zig: @import("...") is a builtin_call_expr — handled
    # generically via call types below.
    "zig": [],
    "powershell": [],
    # Julia: import/using are import_statement nodes.
    "julia": ["import_statement", "using_statement"],
}

_CALL_TYPES: dict[str, list[str]] = {
    "python": ["call"],
    "javascript": ["call_expression", "new_expression"],
    "typescript": ["call_expression", "new_expression"],
    "tsx": ["call_expression", "new_expression"],
    "go": ["call_expression"],
    "rust": ["call_expression", "macro_invocation"],
    "java": ["method_invocation", "object_creation_expression"],
    "c": ["call_expression"],
    "cpp": ["call_expression"],
    "csharp": ["invocation_expression", "object_creation_expression"],
    "ruby": ["call", "method_call"],
    "r": ["call"],
    "perl": [
        "function_call_expression", "method_call_expression",
        "ambiguous_function_call_expression",
    ],
    "kotlin": ["call_expression"],
    "swift": ["call_expression"],
    "php": ["function_call_expression", "member_call_expression"],
    "scala": ["call_expression", "instance_expression", "generic_function"],
    "solidity": ["call_expression"],
    "lua": ["function_call"],
    "luau": ["function_call"],
    # Objective-C: [receiver message:args] produces message_expression;
    # C-style foo(x) produces call_expression.
    "objc": ["message_expression", "call_expression"],
    # Bash: every command invocation is a "command" node.
    "bash": ["command"],
    # Elixir: everything is a ``call`` node — dispatched via
    # _extract_elixir_constructs which filters out def/defmodule/alias/etc.
    # before treating what's left as a real call.
    "elixir": [],
    "zig": ["call_expression", "builtin_call_expr"],
    "powershell": ["command_expression"],
    "julia": ["call_expression"],
}

# Patterns that indicate a test function
_TEST_PATTERNS = [
    re.compile(r"^test_"),
    re.compile(r"^Test"),
    re.compile(r"_test$"),
    re.compile(r"\.test\."),
    re.compile(r"\.spec\."),
    re.compile(r"_spec$"),
]

_TEST_FILE_PATTERNS = [
    re.compile(r"test_.*\.py$"),
    re.compile(r".*_test\.py$"),
    re.compile(r".*\.test\.[jt]sx?$"),
    re.compile(r".*\.spec\.[jt]sx?$"),
    re.compile(r".*_test\.go$"),
    re.compile(r"tests?/"),
    re.compile(r".*_test\.dart$"),
    re.compile(r"test[_-].*\.[rR]$"),
    re.compile(r"tests/testthat/"),
    re.compile(r".*Test\.kt$"),
    re.compile(r".*Test\.java$"),
    re.compile(r".*_test\.resi?$"),
    re.compile(r".*\.test\.resi?$"),
]

_TEST_RUNNER_NAMES = frozenset({
    "describe", "it", "test", "beforeEach", "afterEach",
    "beforeAll", "afterAll",
})

# Annotations/decorators that mark test methods (JUnit, TestNG, etc.)
_TEST_ANNOTATIONS = frozenset({
    "Test", "ParameterizedTest", "RepeatedTest", "TestFactory",
    "org.junit.Test", "org.junit.jupiter.api.Test",
})


# ---------------------------------------------------------------------------
# ReScript regex patterns and helpers (no tree-sitter grammar bundled)
# ---------------------------------------------------------------------------

_RESCRIPT_IDENT = r"[A-Za-z_][A-Za-z0-9_']*"

# `module Name =`, `module type Name =`, `module Name: {`, `module Name: (Sig) => {`
_RESCRIPT_MODULE_RE = re.compile(
    r"^\s*module\s+(?:type\s+)?([A-Z][A-Za-z0-9_']*)\s*[:=]",
    re.MULTILINE,
)

# Optional leading decorator block on the same line, e.g. `@deriving(foo)`.
_RESCRIPT_DECORATOR_PREFIX = r"(?:@[A-Za-z_][A-Za-z0-9_']*(?:\([^)]*\))?\s+)*"

# `let [rec] name` / `and name` — captures binding name. Multi-line decorators
# on prior lines don't interfere (they end with a newline and the anchor
# restarts on the next line); same-line decorators are tolerated.
_RESCRIPT_LET_RE = re.compile(
    rf"^\s*{_RESCRIPT_DECORATOR_PREFIX}"
    rf"(?:let\s+(?:rec\s+)?|and\s+)({_RESCRIPT_IDENT})\b",
    re.MULTILINE,
)

# `external name: sig = "..."`
_RESCRIPT_EXTERNAL_RE = re.compile(
    rf"^\s*{_RESCRIPT_DECORATOR_PREFIX}external\s+({_RESCRIPT_IDENT})\s*:",
    re.MULTILINE,
)

# `type name` / `type rec name` / `type name<'a>`
_RESCRIPT_TYPE_RE = re.compile(
    rf"^\s*{_RESCRIPT_DECORATOR_PREFIX}type\s+(?:rec\s+)?({_RESCRIPT_IDENT})\b",
    re.MULTILINE,
)

# `open Foo` / `include Foo.Bar`
_RESCRIPT_OPEN_RE = re.compile(
    r"^\s*(open|include)\s+([A-Z][A-Za-z0-9_'.]*)",
    re.MULTILINE,
)

# `module X = Foo.Bar` with no `{` body — a module alias/re-export. Distinct
# from `module X = { ... }` (handled by _RESCRIPT_MODULE_RE + brace scan).
_RESCRIPT_MODULE_ALIAS_RE = re.compile(
    r"^\s*module\s+([A-Z][A-Za-z0-9_']*)\s*=\s*"
    r"([A-Z][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*)\s*$",
    re.MULTILINE,
)

# JSX opening tag: `<Foo`, `<Foo.Bar`, `<Foo.Bar.Baz`. First segment must be
# Capitalized (lowercase tags are HTML elements, not ReScript components).
# The leading `<` must NOT be part of `=>`, `<=`, `<-`, or a generic-type
# parameter (we approximate by requiring the char before `<` to be space,
# newline, `{`, `(`, `,`, `>`, `}`, or BOF).
_RESCRIPT_JSX_RE = re.compile(
    r"(?:^|(?<=[\s{(,>}]))"
    r"<([A-Z][A-Za-z0-9_']*(?:\.[A-Z][A-Za-z0-9_']*)*)\b",
    re.MULTILINE,
)

# `@module("path")` — source module for an external binding
_RESCRIPT_MODULE_ATTR_RE = re.compile(
    r'@module\(\s*"([^"]+)"\s*\)',
)

# `Ident(`, `Mod.fn(` — anything that looks like a call site. Preceded by a
# non-identifier char to avoid matching suffixes of identifiers.
_RESCRIPT_CALL_RE = re.compile(
    rf"(?<![A-Za-z0-9_']){_RESCRIPT_IDENT}(?:\.{_RESCRIPT_IDENT})*\s*\(",
)

# Recompiled to grab the captured identifier sequence. We need a different
# regex with a capture group for matching:
_RESCRIPT_CALL_RE = re.compile(
    r"(?<![A-Za-z0-9_'])"
    r"([A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*)"
    r"\s*\(",
)

# Reserved words + syntactic noise that should never be treated as names
# or as call targets.
_RESCRIPT_KEYWORDS = frozenset({
    "let", "rec", "and", "type", "module", "open", "include", "external",
    "if", "else", "switch", "when", "match", "fun", "true", "false",
    "for", "while", "mutable", "try", "catch", "throw", "assert",
    "lazy", "do", "in", "of", "as", "exception", "private",
    "constraint", "with", "downto", "to", "unpack", "async", "await",
})


def _strip_rescript_noise(text: str) -> str:
    """Replace ReScript comments and string/backtick content with spaces.

    Newlines are preserved so absolute offsets still map back to accurate
    line numbers. ReScript block comments may nest, so we track depth.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        # Line comment
        if c == "/" and nxt == "/":
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue
        # Nestable block comment
        if c == "/" and nxt == "*":
            depth = 1
            out.append("  ")
            i += 2
            while i < n and depth > 0:
                if i + 1 < n and text[i] == "/" and text[i + 1] == "*":
                    depth += 1
                    out.append("  ")
                    i += 2
                elif i + 1 < n and text[i] == "*" and text[i + 1] == "/":
                    depth -= 1
                    out.append("  ")
                    i += 2
                else:
                    out.append("\n" if text[i] == "\n" else " ")
                    i += 1
            continue
        # Double-quoted string — blank content, keep quotes + newlines.
        if c == '"':
            out.append('"')
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    out.append("  ")
                    i += 2
                    continue
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append('"')
                i += 1
            continue
        # Backtick template string — blank content, preserve newlines.
        if c == "`":
            out.append("`")
            i += 1
            while i < n and text[i] != "`":
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append("`")
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _rescript_brace_depth_array(cleaned: str) -> list[int]:
    """Compute brace depth at every offset in `cleaned` (comment/string-stripped).

    Returned array has length len(cleaned); `depth[i]` is the depth
    immediately before the character at position i.
    """
    depth = [0] * (len(cleaned) + 1)
    d = 0
    for i, c in enumerate(cleaned):
        depth[i] = d
        if c == "{":
            d += 1
        elif c == "}":
            d = max(0, d - 1)
    depth[len(cleaned)] = d
    return depth


def _scan_rescript_modules(cleaned: str, offset_to_line) -> list[dict]:
    """Find `module Name = { ... }` blocks and their offset/line ranges.

    Returns dicts with name, start/end offsets, start/end lines, and parent
    module name (or None for top-level).
    """
    modules: list[dict] = []
    n = len(cleaned)
    # Module aliases (`module X = Foo.Bar`) also match _RESCRIPT_MODULE_RE but
    # have no brace body — skip them here to avoid the greedy `{`-scanner
    # swallowing the next unrelated block (e.g. a `let` body).
    alias_starts = {
        m.start() for m in _RESCRIPT_MODULE_ALIAS_RE.finditer(cleaned)
    }
    for match in _RESCRIPT_MODULE_RE.finditer(cleaned):
        if match.start() in alias_starts:
            continue
        name = match.group(1)
        header_start = match.start()
        # Find the first `{` after the header's `:` or `=`. To avoid grabbing
        # a `{` from an unrelated following statement, require that the chars
        # between `match.end()` and `brace_open` contain no definition-starting
        # keywords (`let`, `type`, `module`, `external`).
        brace_open = cleaned.find("{", match.end())
        if brace_open == -1:
            continue
        between = cleaned[match.end():brace_open]
        if re.search(
            r"(?:^|\s)(?:let|type|module|external|and)\s",
            between,
        ):
            continue
        # Walk braces to find the matching close.
        depth = 1
        j = brace_open + 1
        while j < n and depth > 0:
            c = cleaned[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            j += 1
        brace_close = j - 1 if depth == 0 else n - 1
        modules.append({
            "name": name,
            "start_off": header_start,
            "end_off": brace_close,
            "body_start_off": brace_open + 1,
            "start_line": offset_to_line(header_start),
            "end_line": offset_to_line(brace_close),
            "parent": None,
        })

    # Parent = innermost strictly-containing module.
    for i, m in enumerate(modules):
        parent_name = None
        parent_start = -1
        for j, other in enumerate(modules):
            if i == j:
                continue
            if (
                other["start_off"] < m["start_off"]
                and other["end_off"] > m["end_off"]
                and other["start_off"] > parent_start
            ):
                parent_name = other["name"]
                parent_start = other["start_off"]
        m["parent"] = parent_name
    return modules


def _is_test_file(path: str) -> bool:
    return any(p.search(path) for p in _TEST_FILE_PATTERNS)


def _is_test_function(
    name: str, file_path: str, decorators: tuple[str, ...] = (),
) -> bool:
    """A function is a test if its name matches test patterns, it lives
    in a test file and has a test-runner name, or it has a @Test annotation.
    """
    if any(p.search(name) for p in _TEST_PATTERNS):
        return True
    if _is_test_file(file_path) and name in _TEST_RUNNER_NAMES:
        return True
    if decorators and any(d in _TEST_ANNOTATIONS for d in decorators):
        return True
    return False


def file_hash(path: Path) -> str:
    """SHA-256 hash of file contents."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class CodeParser:
    """Parses source files using Tree-sitter and extracts structural information."""

    _MODULE_CACHE_MAX = 15_000  # Evict cache to cap memory on huge monorepos

    def __init__(self) -> None:
        self._parsers: dict[str, object] = {}
        self._module_file_cache: dict[str, Optional[str]] = {}
        self._export_symbol_cache: dict[str, Optional[str]] = {}
        self._tsconfig_resolver = TsconfigResolver()
        # Per-parse cache of Dart pubspec root lookups; see #87
        self._dart_pubspec_cache: dict[tuple[str, str], Optional[Path]] = {}

    def _get_parser(self, language: str):  # type: ignore[arg-type]
        if language not in self._parsers:
            try:
                self._parsers[language] = tslp.get_parser(language)  # type: ignore[arg-type]
            except (LookupError, ValueError, ImportError) as exc:
                # language not packaged, or grammar load failed
                logger.debug("tree-sitter parser unavailable for %s: %s", language, exc)
                return None
        return self._parsers[language]

    def detect_language(self, path: Path) -> Optional[str]:
        return EXTENSION_TO_LANGUAGE.get(path.suffix.lower())

    def parse_file(self, path: Path) -> tuple[list[NodeInfo], list[EdgeInfo]]:
        """Parse a single file and return extracted nodes and edges."""
        try:
            source = path.read_bytes()
        except (OSError, PermissionError):
            return [], []
        return self.parse_bytes(path, source)

    def parse_bytes(self, path: Path, source: bytes) -> tuple[list[NodeInfo], list[EdgeInfo]]:
        """Parse pre-read bytes and return extracted nodes and edges.

        This avoids re-reading the file from disk, eliminating TOCTOU gaps
        when the caller has already read the bytes (e.g. for hashing).
        """
        language = self.detect_language(path)
        if not language:
            return [], []

        # Vue SFCs: parse with vue parser, then delegate script blocks to JS/TS
        if language == "vue":
            return self._parse_vue(path, source)

        # Svelte SFCs: same approach as Vue — extract <script> blocks
        if language == "svelte":
            return self._parse_svelte(path, source)

        # Jupyter notebooks: extract code cells and parse as Python
        if language == "notebook":
            return self._parse_notebook(path, source)

        # Databricks .py notebook exports
        if language == "python" and source.startswith(
            b"# Databricks notebook source\n",
        ):
            return self._parse_databricks_py_notebook(path, source)

        # ReScript: regex-based parser (no tree-sitter grammar bundled).
        if language == "rescript":
            return self._parse_rescript(path, source)

        parser = self._get_parser(language)
        if not parser:
            return [], []

        tree = parser.parse(source)
        nodes: list[NodeInfo] = []
        edges: list[EdgeInfo] = []
        file_path_str = str(path)

        # File node
        test_file = _is_test_file(file_path_str)
        nodes.append(NodeInfo(
            kind="File",
            name=file_path_str,
            file_path=file_path_str,
            line_start=1,
            line_end=source.count(b"\n") + 1,
            language=language,
            is_test=test_file,
        ))

        # Pre-scan for import mappings and defined names
        import_map, defined_names = self._collect_file_scope(
            tree.root_node, language, source,
        )

        # Walk the tree
        self._extract_from_tree(
            tree.root_node, source, language, file_path_str, nodes, edges,
            import_map=import_map, defined_names=defined_names,
        )

        # Resolve bare call targets to qualified names using same-file definitions
        edges = self._resolve_call_targets(nodes, edges, file_path_str)

        # Generate TESTED_BY edges: when a test function calls a production
        # function, create an edge from the production function back to the test.
        if test_file:
            test_qnames = set()
            for n in nodes:
                if n.is_test:
                    qn = self._qualify(n.name, n.file_path, n.parent_name)
                    test_qnames.add(qn)
            for edge in list(edges):
                if edge.kind == "CALLS" and edge.source in test_qnames:
                    edges.append(EdgeInfo(
                        kind="TESTED_BY",
                        source=edge.target,
                        target=edge.source,
                        file_path=edge.file_path,
                        line=edge.line,
                    ))

        return nodes, edges

    def _parse_vue(
        self, path: Path, source: bytes,
    ) -> tuple[list[NodeInfo], list[EdgeInfo]]:
        """Parse a Vue SFC by extracting <script> blocks and delegating to JS/TS."""
        vue_parser = self._get_parser("vue")
        if not vue_parser:
            return [], []

        tree = vue_parser.parse(source)
        file_path_str = str(path)
        test_file = _is_test_file(file_path_str)

        all_nodes: list[NodeInfo] = [NodeInfo(
            kind="File",
            name=file_path_str,
            file_path=file_path_str,
            line_start=1,
            line_end=source.count(b"\n") + 1,
            language="vue",
            is_test=test_file,
        )]
        all_edges: list[EdgeInfo] = []

        # Find script_element blocks in the Vue AST
        for child in tree.root_node.children:
            if child.type != "script_element":
                continue

            # Detect language from lang="ts" attribute
            script_lang = "javascript"
            start_tag = None
            raw_text_node = None
            for sub in child.children:
                if sub.type == "start_tag":
                    start_tag = sub
                elif sub.type == "raw_text":
                    raw_text_node = sub

            if start_tag:
                for attr in start_tag.children:
                    if attr.type == "attribute":
                        attr_name = None
                        attr_value = None
                        for a in attr.children:
                            if a.type == "attribute_name":
                                attr_name = a.text.decode("utf-8", errors="replace")
                            elif a.type == "quoted_attribute_value":
                                for v in a.children:
                                    if v.type == "attribute_value":
                                        attr_value = v.text.decode(
                                            "utf-8", errors="replace",
                                        )
                        if attr_name == "lang" and attr_value in ("ts", "typescript"):
                            script_lang = "typescript"

            if not raw_text_node:
                continue

            script_source = raw_text_node.text
            line_offset = raw_text_node.start_point[0]  # 0-based line of raw_text start

            # Parse the script block with the appropriate JS/TS parser
            script_parser = self._get_parser(script_lang)
            if not script_parser:
                continue

            script_tree = script_parser.parse(script_source)

            # Collect imports and defined names from the script block
            import_map, defined_names = self._collect_file_scope(
                script_tree.root_node, script_lang, script_source,
            )

            nodes: list[NodeInfo] = []
            edges: list[EdgeInfo] = []
            self._extract_from_tree(
                script_tree.root_node, script_source, script_lang,
                file_path_str, nodes, edges,
                import_map=import_map, defined_names=defined_names,
            )

            # Adjust line numbers to account for position within the .vue file
            for node in nodes:
                node.line_start += line_offset
                node.line_end += line_offset
                node.language = "vue"
            for edge in edges:
                edge.line += line_offset

            all_nodes.extend(nodes)
            all_edges.extend(edges)

        # Generate TESTED_BY edges
        if test_file:
            test_qnames = set()
            for n in all_nodes:
                if n.is_test:
                    qn = self._qualify(n.name, n.file_path, n.parent_name)
                    test_qnames.add(qn)
            for edge in list(all_edges):
                if edge.kind == "CALLS" and edge.source in test_qnames:
                    all_edges.append(EdgeInfo(
                        kind="TESTED_BY",
                        source=edge.target,
                        target=edge.source,
                        file_path=edge.file_path,
                        line=edge.line,
                    ))

        return all_nodes, all_edges

    def _parse_svelte(
        self, path: Path, source: bytes,
    ) -> tuple[list[NodeInfo], list[EdgeInfo]]:
        """Parse a Svelte SFC by extracting <script> blocks.

        Uses the same approach as Vue: parse the outer HTML structure,
        locate ``<script>`` blocks, detect ``lang="ts"`` for TypeScript,
        and delegate each block to the appropriate JS/TS parser.
        """
        # Svelte uses HTML-like structure; reuse the vue grammar which
        # also handles generic HTML with <script> elements.
        svelte_parser = self._get_parser("svelte")
        # Fall back to the vue grammar if a dedicated svelte grammar
        # is not available in the installed tree-sitter language pack.
        if not svelte_parser:
            svelte_parser = self._get_parser("vue")
        if not svelte_parser:
            return [], []

        tree = svelte_parser.parse(source)
        file_path_str = str(path)
        test_file = _is_test_file(file_path_str)

        all_nodes: list[NodeInfo] = [NodeInfo(
            kind="File",
            name=file_path_str,
            file_path=file_path_str,
            line_start=1,
            line_end=source.count(b"\n") + 1,
            language="svelte",
            is_test=test_file,
        )]
        all_edges: list[EdgeInfo] = []

        # Walk root children looking for script_element blocks
        for child in tree.root_node.children:
            if child.type != "script_element":
                continue

            script_lang = "javascript"
            start_tag = None
            raw_text_node = None
            for sub in child.children:
                if sub.type == "start_tag":
                    start_tag = sub
                elif sub.type == "raw_text":
                    raw_text_node = sub

            if start_tag:
                for attr in start_tag.children:
                    if attr.type == "attribute":
                        attr_name = None
                        attr_value = None
                        for a in attr.children:
                            if a.type == "attribute_name":
                                attr_name = a.text.decode(
                                    "utf-8", errors="replace",
                                )
                            elif a.type == "quoted_attribute_value":
                                for v in a.children:
                                    if v.type == "attribute_value":
                                        attr_value = v.text.decode(
                                            "utf-8",
                                            errors="replace",
                                        )
                        if (
                            attr_name == "lang"
                            and attr_value
                            in ("ts", "typescript")
                        ):
                            script_lang = "typescript"

            if not raw_text_node:
                continue

            script_source = raw_text_node.text
            line_offset = raw_text_node.start_point[0]

            script_parser = self._get_parser(script_lang)
            if not script_parser:
                continue

            script_tree = script_parser.parse(script_source)
            import_map, defined_names = self._collect_file_scope(
                script_tree.root_node, script_lang, script_source,
            )

            nodes: list[NodeInfo] = []
            edges: list[EdgeInfo] = []
            self._extract_from_tree(
                script_tree.root_node, script_source,
                script_lang, file_path_str, nodes, edges,
                import_map=import_map,
                defined_names=defined_names,
            )

            for node in nodes:
                node.line_start += line_offset
                node.line_end += line_offset
                node.language = "svelte"
            for edge in edges:
                edge.line += line_offset

            all_nodes.extend(nodes)
            all_edges.extend(edges)

        # Generate TESTED_BY edges
        if test_file:
            test_qnames = set()
            for n in all_nodes:
                if n.is_test:
                    qn = self._qualify(
                        n.name, n.file_path, n.parent_name,
                    )
                    test_qnames.add(qn)
            for edge in list(all_edges):
                if (
                    edge.kind == "CALLS"
                    and edge.source in test_qnames
                ):
                    all_edges.append(EdgeInfo(
                        kind="TESTED_BY",
                        source=edge.target,
                        target=edge.source,
                        file_path=edge.file_path,
                        line=edge.line,
                    ))

        return all_nodes, all_edges

    def _parse_notebook(
        self, path: Path, source: bytes,
    ) -> tuple[list[NodeInfo], list[EdgeInfo]]:
        """Parse a Jupyter notebook by extracting code cells."""
        try:
            nb = json.loads(source)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return [], []

        # Determine kernel language
        kernel_lang = (
            nb.get("metadata", {}).get("kernelspec", {}).get("language")
            or nb.get("metadata", {}).get("language_info", {}).get("name")
            or "python"
        ).lower()

        # Only parse supported languages
        supported = {"python", "r"}
        if kernel_lang not in supported:
            return [], []

        # Build CellInfo list from code cells
        cells: list[CellInfo] = []
        magic_lang_map = {
            "%python": "python",
            "%sql": "sql",
            "%r": "r",
        }
        skip_magics = {"%scala", "%md", "%sh"}

        for cell_idx, cell in enumerate(nb.get("cells", [])):
            if cell.get("cell_type") != "code":
                continue
            lines = cell.get("source", [])
            if isinstance(lines, str):
                lines = lines.splitlines(keepends=True)
            if not lines:
                continue

            # Check first line for language-switching magic
            first_line = lines[0].strip()
            cell_lang = kernel_lang
            cell_lines = lines

            for magic, lang in magic_lang_map.items():
                if first_line == magic or first_line.startswith(magic + " "):
                    cell_lang = lang
                    cell_lines = lines[1:]  # strip magic line
                    break
            else:
                # Check for skip magics
                for skip in skip_magics:
                    if first_line == skip or first_line.startswith(skip + " "):
                        cell_lines = []
                        break

            # Filter %pip, ! lines from Python/R content (not SQL)
            if cell_lang in ("python", "r"):
                filtered = [
                    ln for ln in cell_lines
                    if not ln.lstrip().startswith(("%", "!"))
                ]
            else:
                filtered = cell_lines
            if not filtered:
                continue

            cell_source = "".join(filtered)
            cells.append(CellInfo(cell_index=cell_idx, language=cell_lang, source=cell_source))

        if not cells:
            file_path_str = str(path)
            return [NodeInfo(
                kind="File",
                name=file_path_str,
                file_path=file_path_str,
                line_start=1,
                line_end=1,
                language=kernel_lang,
                is_test=_is_test_file(file_path_str),
            )], []

        return self._parse_notebook_cells(path, cells, kernel_lang)

    def _parse_notebook_cells(
        self,
        path: Path,
        cells: list[CellInfo],
        default_language: str,
    ) -> tuple[list[NodeInfo], list[EdgeInfo]]:
        """Parse notebook cells grouped by language.

        Args:
            path: Notebook file path.
            cells: List of CellInfo with index, language, and source.
            default_language: Default language for the File node.
        """
        file_path_str = str(path)
        test_file = _is_test_file(file_path_str)

        # Group cells by language
        lang_cells: dict[str, list[CellInfo]] = {}
        for cell in cells:
            lang_cells.setdefault(cell.language, []).append(cell)

        all_nodes: list[NodeInfo] = []
        all_edges: list[EdgeInfo] = []

        # Track offsets per language for cell_index tagging.
        # Each language group is parsed independently by Tree-sitter,
        # so line numbers restart at 1 for each group.
        all_cell_offsets: list[tuple[int, int, int]] = []
        max_line = 1

        for lang, lang_group in lang_cells.items():
            if lang == "sql":
                # SQL: regex-based table extraction
                for cell in lang_group:
                    for match in _SQL_TABLE_RE.finditer(cell.source):
                        table_name = match.group(1).replace("`", "")
                        all_edges.append(EdgeInfo(
                            kind="IMPORTS_FROM",
                            source=file_path_str,
                            target=table_name,
                            file_path=file_path_str,
                            line=1,
                        ))
                continue

            if lang not in ("python", "r"):
                continue

            ts_parser = self._get_parser(lang)
            if not ts_parser:
                continue

            # Concatenate cells of this language.
            # Line numbers start at 1 for each language group because
            # Tree-sitter parses each concatenation independently.
            code_chunks: list[str] = []
            cell_offsets: list[tuple[int, int, int]] = []
            current_line = 1

            for cell in lang_group:
                cell_line_count = cell.source.count("\n") + (
                    1 if not cell.source.endswith("\n") else 0
                )
                cell_offsets.append((
                    cell.cell_index, current_line, current_line + cell_line_count - 1,
                ))
                code_chunks.append(cell.source)
                current_line += cell_line_count + 1

            concatenated = "\n".join(code_chunks)
            concat_bytes = concatenated.encode("utf-8")

            tree = ts_parser.parse(concat_bytes)

            import_map, defined_names = self._collect_file_scope(
                tree.root_node, lang, concat_bytes,
            )
            self._extract_from_tree(
                tree.root_node, concat_bytes, lang,
                file_path_str, all_nodes, all_edges,
                import_map=import_map, defined_names=defined_names,
            )

            all_cell_offsets.extend(cell_offsets)
            max_line = max(max_line, current_line)

        # Create File node
        file_node = NodeInfo(
            kind="File",
            name=file_path_str,
            file_path=file_path_str,
            line_start=1,
            line_end=max_line,
            language=default_language,
            is_test=test_file,
        )
        all_nodes.insert(0, file_node)

        # Resolve call targets
        all_edges = self._resolve_call_targets(
            all_nodes, all_edges, file_path_str,
        )

        # Tag nodes with cell_index
        for node in all_nodes:
            if node.kind == "File":
                continue
            for cell_idx, start, end in all_cell_offsets:
                if start <= node.line_start <= end:
                    node.extra["cell_index"] = cell_idx
                    break

        # Generate TESTED_BY edges
        if test_file:
            test_qnames = set()
            for n in all_nodes:
                if n.is_test:
                    qn = self._qualify(n.name, n.file_path, n.parent_name)
                    test_qnames.add(qn)
            for edge in list(all_edges):
                if edge.kind == "CALLS" and edge.source in test_qnames:
                    all_edges.append(EdgeInfo(
                        kind="TESTED_BY",
                        source=edge.target,
                        target=edge.source,
                        file_path=edge.file_path,
                        line=edge.line,
                    ))

        return all_nodes, all_edges

    def _parse_databricks_py_notebook(
        self, path: Path, source: bytes,
    ) -> tuple[list[NodeInfo], list[EdgeInfo]]:
        """Parse a Databricks .py notebook export."""
        text = source.decode("utf-8", errors="replace")

        # Strip the header line
        lines = text.split("\n")
        if lines and lines[0].strip() == "# Databricks notebook source":
            lines = lines[1:]

        # Split on COMMAND delimiters
        cell_chunks: list[list[str]] = [[]]
        for line in lines:
            if re.match(r"^# COMMAND\s*-+\s*$", line):
                cell_chunks.append([])
            else:
                cell_chunks[-1].append(line)

        # Classify each cell
        cells: list[CellInfo] = []
        magic_lang_map = {
            "# MAGIC %sql": "sql",
            "# MAGIC %r": "r",
        }
        skip_prefixes = ("# MAGIC %md", "# MAGIC %sh")

        for cell_idx, chunk in enumerate(cell_chunks):
            non_empty = [ln for ln in chunk if ln.strip()]
            if not non_empty:
                continue

            first_line = non_empty[0]

            # Check if all non-empty lines are MAGIC lines
            all_magic = all(ln.startswith("# MAGIC ") for ln in non_empty)

            # Detect language from the first MAGIC line (e.g. "# MAGIC %sql")
            cell_lang = None
            if all_magic:
                for prefix, lang in magic_lang_map.items():
                    if first_line.startswith(prefix):
                        cell_lang = lang
                        break

            if cell_lang:
                # Strip "# MAGIC " prefix (8 chars) then skip the %lang directive line
                stripped = [
                    ln[8:] if ln.startswith("# MAGIC ") else ln
                    for ln in chunk
                ]
                # Remove the first non-empty line if it's just the %lang directive
                stripped_non_empty = [ln for ln in stripped if ln.strip()]
                if stripped_non_empty and stripped_non_empty[0].strip().startswith("%"):
                    # Drop the directive line from the source
                    first_directive = stripped_non_empty[0]
                    stripped = [ln for ln in stripped if ln != first_directive]
                cell_source = "\n".join(stripped)
                cells.append(CellInfo(
                    cell_index=cell_idx, language=cell_lang, source=cell_source,
                ))
                continue

            # Check for skip prefixes (md, sh)
            if all_magic and first_line.startswith(skip_prefixes):
                continue

            # Default: Python cell (mixed or no MAGIC)
            py_lines = [ln for ln in chunk if not ln.startswith("# MAGIC ")]
            cell_source = "\n".join(py_lines)
            cells.append(CellInfo(
                cell_index=cell_idx, language="python", source=cell_source,
            ))

        if not cells:
            file_path_str = str(path)
            file_node = NodeInfo(
                kind="File",
                name=file_path_str,
                file_path=file_path_str,
                line_start=1,
                line_end=1,
                language="python",
                is_test=_is_test_file(file_path_str),
            )
            file_node.extra["notebook_format"] = "databricks_py"
            return [file_node], []

        nodes, edges = self._parse_notebook_cells(path, cells, "python")

        # Tag File node with notebook_format
        for node in nodes:
            if node.kind == "File":
                node.extra["notebook_format"] = "databricks_py"
                break

        return nodes, edges

    # ------------------------------------------------------------------
    # ReScript: regex-based structural parser (no tree-sitter grammar
    # is bundled for ReScript, so we extract best-effort structure via
    # comment-stripping + line-anchored regex + brace-counted module scan).
    # ------------------------------------------------------------------

    def _parse_rescript(
        self, path: Path, source: bytes,
    ) -> tuple[list[NodeInfo], list[EdgeInfo]]:
        """Parse a ReScript `.res` or `.resi` file.

        Extracts modules, let bindings, types, external bindings, open/include
        imports, and function calls. Interface files (`.resi`) are flagged via
        ``File`` node ``extra["rescript_interface"]=True`` and skip call
        extraction since signatures have no call sites.
        """
        text = source.decode("utf-8", errors="replace")
        file_path_str = str(path)
        test_file = _is_test_file(file_path_str)
        is_interface = path.suffix.lower() == ".resi"

        # Strip comments and string/backtick literal content so downstream
        # regex matches are not fooled by code-looking text inside strings.
        # Newlines are preserved so offset→line mapping stays accurate.
        cleaned = _strip_rescript_noise(text)

        # Build offset → line index (1-based).
        line_starts = [0]
        for i, ch in enumerate(cleaned):
            if ch == "\n":
                line_starts.append(i + 1)

        def offset_to_line(off: int) -> int:
            lo, hi = 0, len(line_starts) - 1
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if line_starts[mid] <= off:
                    lo = mid
                else:
                    hi = mid - 1
            return lo + 1

        nodes: list[NodeInfo] = []
        edges: list[EdgeInfo] = []

        file_extra: dict = {}
        if is_interface:
            file_extra["rescript_interface"] = True
        nodes.append(NodeInfo(
            kind="File",
            name=file_path_str,
            file_path=file_path_str,
            line_start=1,
            line_end=text.count("\n") + 1,
            language="rescript",
            is_test=test_file,
            extra=file_extra,
        ))

        # Modules with brace-matched offset ranges.
        modules = _scan_rescript_modules(cleaned, offset_to_line)
        depth_arr = _rescript_brace_depth_array(cleaned)

        def is_top_level(off: int, parent_mod: Optional[str]) -> bool:
            """True if offset is at file scope (depth 0) or directly inside
            `parent_mod`'s body (depth = module body depth)."""
            d = depth_arr[off] if off < len(depth_arr) else 0
            if parent_mod is None:
                return d == 0
            for m in modules:
                if m["name"] == parent_mod and m["start_off"] <= off <= m["end_off"]:
                    expected = depth_arr[m["body_start_off"]]
                    return d == expected
            return False
        for m in modules:
            nodes.append(NodeInfo(
                kind="Class",
                name=m["name"],
                file_path=file_path_str,
                line_start=m["start_line"],
                line_end=m["end_line"],
                language="rescript",
                parent_name=m["parent"],
                extra={"rescript_kind": "module"},
            ))

        def enclosing_module(off: int) -> Optional[str]:
            innermost_name = None
            innermost_start = -1
            for m in modules:
                if (
                    m["start_off"] <= off <= m["end_off"]
                    and m["start_off"] > innermost_start
                ):
                    innermost_name = m["name"]
                    innermost_start = m["start_off"]
            return innermost_name

        # First: let/and bindings — collect offsets so we can later compute
        # end offsets for call attribution.
        let_entries: list[dict] = []
        for match in _RESCRIPT_LET_RE.finditer(cleaned):
            name = match.group(1)
            if name in _RESCRIPT_KEYWORDS:
                continue
            off = match.start(1)
            parent = enclosing_module(off)
            if not is_top_level(off, parent):
                continue  # nested local `let` — not a structural node
            line_start = offset_to_line(off)
            is_test_fn = _is_test_function(name, file_path_str)
            let_entries.append({
                "name": name,
                "start_off": off,
                "line_start": line_start,
                "parent": parent,
                "is_test": is_test_fn,
            })

        # Sort by start_off, compute end_off as next same-or-outer-scope let start
        # or the closing brace of the enclosing module, or end of file.
        let_entries.sort(key=lambda e: e["start_off"])
        for i, entry in enumerate(let_entries):
            nxt = len(cleaned)
            for later in let_entries[i + 1:]:
                nxt = later["start_off"]
                break
            # Clamp by enclosing module end if any
            if entry["parent"]:
                for m in modules:
                    if (
                        m["name"] == entry["parent"]
                        and m["start_off"] <= entry["start_off"] <= m["end_off"]
                    ):
                        nxt = min(nxt, m["end_off"])
                        break
            entry["end_off"] = max(nxt, entry["start_off"] + 1)
            entry["line_end"] = offset_to_line(entry["end_off"] - 1)

        for entry in let_entries:
            nodes.append(NodeInfo(
                kind="Test" if entry["is_test"] else "Function",
                name=entry["name"],
                file_path=file_path_str,
                line_start=entry["line_start"],
                line_end=entry["line_end"],
                language="rescript",
                parent_name=entry["parent"],
                is_test=entry["is_test"],
            ))

        # External bindings (also create IMPORTS_FROM edges for @module attrs).
        for match in _RESCRIPT_EXTERNAL_RE.finditer(cleaned):
            name = match.group(1)
            if name in _RESCRIPT_KEYWORDS:
                continue
            off = match.start(1)
            parent = enclosing_module(off)
            if not is_top_level(off, parent):
                continue
            line_start = offset_to_line(off)
            nodes.append(NodeInfo(
                kind="Function",
                name=name,
                file_path=file_path_str,
                line_start=line_start,
                line_end=line_start,
                language="rescript",
                parent_name=parent,
                extra={"rescript_external": True},
            ))
            # Look back up to 200 chars for a nearby @module("...") attr.
            # Read from the ORIGINAL text (not `cleaned`) so string literal
            # content like "fs" is preserved. Offsets are length-equivalent
            # because `_strip_rescript_noise` replaces with spaces/newlines.
            look_start = max(0, off - 200)
            snippet = text[look_start:off]
            for attr in _RESCRIPT_MODULE_ATTR_RE.finditer(snippet):
                edges.append(EdgeInfo(
                    kind="IMPORTS_FROM",
                    source=file_path_str,
                    target=attr.group(1),
                    file_path=file_path_str,
                    line=line_start,
                    extra={"rescript_import_kind": "external_module"},
                ))

        # Type definitions.
        for match in _RESCRIPT_TYPE_RE.finditer(cleaned):
            name = match.group(1)
            if name in _RESCRIPT_KEYWORDS:
                continue
            off = match.start(1)
            parent = enclosing_module(off)
            if not is_top_level(off, parent):
                continue
            line_start = offset_to_line(off)
            nodes.append(NodeInfo(
                kind="Type",
                name=name,
                file_path=file_path_str,
                line_start=line_start,
                line_end=line_start,
                language="rescript",
                parent_name=parent,
            ))

        # open / include statements.
        for match in _RESCRIPT_OPEN_RE.finditer(cleaned):
            kind = match.group(1)
            target = match.group(2)
            off = match.start()
            line = offset_to_line(off)
            edges.append(EdgeInfo(
                kind="IMPORTS_FROM",
                source=file_path_str,
                target=target,
                file_path=file_path_str,
                line=line,
                extra={"rescript_import_kind": kind},
            ))

        # Module aliases: `module X = Foo.Bar` (no brace body). These
        # re-export another module and are the second most common way ReScript
        # files reference each other (after JSX).
        for match in _RESCRIPT_MODULE_ALIAS_RE.finditer(cleaned):
            alias_name = match.group(1)
            target = match.group(2)
            off = match.start()
            # Skip if the alias was actually the header of a `module X = { ... }`
            # block already captured by `modules`. That scanner requires `{` to
            # follow, so a trailing-dot form like `module X = Foo.Bar` at EOL
            # never gets mistaken for a block.
            if any(m["start_off"] == off for m in modules):
                continue
            line = offset_to_line(off)
            edges.append(EdgeInfo(
                kind="IMPORTS_FROM",
                source=file_path_str,
                target=target,
                file_path=file_path_str,
                line=line,
                extra={
                    "rescript_import_kind": "module_alias",
                    "alias_name": alias_name,
                },
            ))

        # JSX component usage: `<Foo />`, `<Foo.Bar />`. The root module is
        # what matters for cross-file dependency tracking (importers_of);
        # the specific component is the CALLS target for finer queries.
        if not is_interface:
            for match in _RESCRIPT_JSX_RE.finditer(cleaned):
                target = match.group(1)
                off = match.start(1)
                root = target.split(".", 1)[0]
                line = offset_to_line(off)
                edges.append(EdgeInfo(
                    kind="IMPORTS_FROM",
                    source=file_path_str,
                    target=root,
                    file_path=file_path_str,
                    line=line,
                    extra={"rescript_import_kind": "jsx"},
                ))
                # Attribute a CALLS edge to the enclosing let, so
                # callers_of(<Foo.Bar />) can find the caller.
                caller = None
                caller_parent = None
                for entry in let_entries:
                    if entry["start_off"] <= off < entry["end_off"]:
                        caller = entry["name"]
                        caller_parent = entry["parent"]
                    elif entry["start_off"] > off:
                        break
                if caller is not None:
                    edges.append(EdgeInfo(
                        kind="CALLS",
                        source=self._qualify(
                            caller, file_path_str, caller_parent,
                        ),
                        target=target,
                        file_path=file_path_str,
                        line=line,
                        extra={"rescript_call_kind": "jsx"},
                    ))

        # Calls — interface files have no call sites, skip.
        if not is_interface and let_entries:
            for match in _RESCRIPT_CALL_RE.finditer(cleaned):
                target = match.group(1)
                off = match.start(1)
                top = target.split(".", 1)[0]
                if top in _RESCRIPT_KEYWORDS or target in _RESCRIPT_KEYWORDS:
                    continue
                # Find enclosing let by offset range.
                caller = None
                caller_parent = None
                for entry in let_entries:
                    if entry["start_off"] <= off < entry["end_off"]:
                        caller = entry["name"]
                        caller_parent = entry["parent"]
                    elif entry["start_off"] > off:
                        break
                if caller is None:
                    continue
                # Skip the definition site itself: `let name = ...` where
                # name(x) is actually the definition header, not a call.
                if caller == target and off == next(
                    (e["start_off"] for e in let_entries if e["name"] == caller),
                    -1,
                ):
                    continue
                line = offset_to_line(off)
                source_qn = self._qualify(caller, file_path_str, caller_parent)
                edges.append(EdgeInfo(
                    kind="CALLS",
                    source=source_qn,
                    target=target,
                    file_path=file_path_str,
                    line=line,
                ))

        # CONTAINS edges: each module node contains its members.
        for n in nodes:
            if n.kind in ("Function", "Type", "Test") and n.parent_name:
                edges.append(EdgeInfo(
                    kind="CONTAINS",
                    source=self._qualify(n.parent_name, file_path_str, None),
                    target=self._qualify(n.name, file_path_str, n.parent_name),
                    file_path=file_path_str,
                    line=n.line_start,
                ))

        # Tag modules whose member functions are all externals as JS bindings.
        # (e.g. `module TextEncoder = { type encoder; @new external ... }`)
        member_funcs: dict[str, list[NodeInfo]] = {}
        for n in nodes:
            if n.kind == "Function" and n.parent_name:
                member_funcs.setdefault(n.parent_name, []).append(n)
        for mod_node in nodes:
            if mod_node.kind != "Class":
                continue
            members = member_funcs.get(mod_node.name, [])
            if members and all(
                m.extra.get("rescript_external") for m in members
            ):
                mod_node.extra["rescript_kind"] = "js_binding"

        # Dedupe IMPORTS_FROM edges by (source, target). The same `open X`
        # can appear multiple times legitimately (e.g. reopened within
        # different scopes), and include+open of the same module produces
        # two edges; collapse them.
        seen_imports: set[tuple[str, str]] = set()
        deduped_edges: list[EdgeInfo] = []
        for e in edges:
            if e.kind == "IMPORTS_FROM":
                key = (e.source, e.target)
                if key in seen_imports:
                    continue
                seen_imports.add(key)
            deduped_edges.append(e)
        edges = deduped_edges

        edges = self._resolve_call_targets(nodes, edges, file_path_str)

        if test_file:
            test_qnames = set()
            for n in nodes:
                if n.is_test:
                    qn = self._qualify(n.name, n.file_path, n.parent_name)
                    test_qnames.add(qn)
            for edge in list(edges):
                if edge.kind == "CALLS" and edge.source in test_qnames:
                    edges.append(EdgeInfo(
                        kind="TESTED_BY",
                        source=edge.target,
                        target=edge.source,
                        file_path=edge.file_path,
                        line=edge.line,
                    ))

        return nodes, edges

    def _resolve_call_targets(
        self,
        nodes: list[NodeInfo],
        edges: list[EdgeInfo],
        file_path: str,
    ) -> list[EdgeInfo]:
        """Resolve bare call targets to qualified names using same-file definitions.

        After parsing, CALLS edges store bare function names (e.g. ``FirebaseAuth``)
        as targets. This method builds a symbol table from the parsed nodes and
        qualifies any bare target that matches a local definition, so that
        ``callers_of`` / ``callees_of`` queries produce correct results.

        External calls (names not defined in this file) remain bare.
        """
        # Build symbol table: bare_name -> qualified_name
        symbols: dict[str, str] = {}
        for node in nodes:
            if node.kind in ("Function", "Class", "Type", "Test"):
                bare = node.name
                qualified = self._qualify(bare, file_path, node.parent_name)
                if bare not in symbols:
                    symbols[bare] = qualified

        resolved: list[EdgeInfo] = []
        for edge in edges:
            if edge.kind in ("CALLS", "REFERENCES") and "::" not in edge.target:
                if edge.target in symbols:
                    edge = EdgeInfo(
                        kind=edge.kind,
                        source=edge.source,
                        target=symbols[edge.target],
                        file_path=edge.file_path,
                        line=edge.line,
                        extra=edge.extra,
                    )
            resolved.append(edge)
        return resolved

    _MAX_AST_DEPTH = 180  # Guard against pathologically nested source files
    _MAX_TEST_DESCRIPTION_LEN = 200  # Cap test description length in node names

    def _get_test_description(self, call_node, source: bytes) -> Optional[str]:
        """Extract the first string argument from a test runner call node."""
        for child in call_node.children:
            if child.type == "arguments":
                for arg in child.children:
                    if arg.type in ("string", "template_string"):
                        raw = arg.text.decode("utf-8", errors="replace")
                        stripped = raw.strip("'\"`")
                        normalized = re.sub(r"\s+", " ", stripped).strip()
                        if len(normalized) > self._MAX_TEST_DESCRIPTION_LEN:
                            normalized = normalized[: self._MAX_TEST_DESCRIPTION_LEN]
                        return normalized
        return None

    def _extract_from_tree(
        self,
        root,
        source: bytes,
        language: str,
        file_path: str,
        nodes: list[NodeInfo],
        edges: list[EdgeInfo],
        enclosing_class: Optional[str] = None,
        enclosing_func: Optional[str] = None,
        import_map: Optional[dict[str, str]] = None,
        defined_names: Optional[set[str]] = None,
        _depth: int = 0,
    ) -> None:
        """Recursively walk the AST and extract nodes/edges."""
        if _depth > self._MAX_AST_DEPTH:
            return
        class_types = set(_CLASS_TYPES.get(language, []))
        func_types = set(_FUNCTION_TYPES.get(language, []))
        import_types = set(_IMPORT_TYPES.get(language, []))
        call_types = set(_CALL_TYPES.get(language, []))

        for child in root.children:
            node_type = child.type

            # --- R-specific constructs ---
            if language == "r" and self._extract_r_constructs(
                child, node_type, source, language, file_path,
                nodes, edges, enclosing_class, enclosing_func,
                import_map, defined_names,
            ):
                continue

            # --- Lua/Luau-specific constructs ---
            if language in ("lua", "luau") and self._extract_lua_constructs(
                child, node_type, source, language, file_path,
                nodes, edges, enclosing_class, enclosing_func,
                import_map, defined_names, _depth,
            ):
                continue

            # --- Bash-specific constructs ---
            # ``source ./foo.sh`` and ``. ./foo.sh`` are commands in
            # tree-sitter-bash; re-interpret them as IMPORTS_FROM edges so
            # cross-script wiring works the same as in other languages.
            if language == "bash" and node_type == "command":
                if self._extract_bash_source_command(
                    child, file_path, edges,
                ):
                    continue

            # --- Elixir-specific constructs ---
            # Every top-level construct in Elixir is a ``call`` node:
            # defmodule, def/defp/defmacro, alias/import/require/use, and
            # ordinary function invocations all share the same node type.
            # Dispatch via _extract_elixir_constructs so we can tell them
            # apart by the first-identifier text and still recurse into
            # bodies with the correct enclosing scope. See: #112
            if language == "elixir" and node_type == "call":
                if self._extract_elixir_constructs(
                    child, source, language, file_path, nodes, edges,
                    enclosing_class, enclosing_func,
                    import_map, defined_names, _depth,
                ):
                    continue

            # --- Dart call detection (see #87) ---
            # tree-sitter-dart does not wrap calls in a single
            # ``call_expression`` node; instead the pattern is
            # ``identifier + selector > argument_part`` as siblings inside
            # the parent.  Scan child's children here and emit CALLS edges
            # for any we find; nested calls are handled by the main recursion.
            if language == "dart":
                self._extract_dart_calls_from_children(
                    child, source, file_path, edges,
                    enclosing_class, enclosing_func,
                )

            # --- JS/TS variable-assigned functions (const foo = () => {}) ---
            if (
                language in ("javascript", "typescript", "tsx")
                and node_type in ("lexical_declaration", "variable_declaration")
                and self._extract_js_var_functions(
                    child, source, language, file_path, nodes, edges,
                    enclosing_class, enclosing_func,
                    import_map, defined_names, _depth,
                )
            ):
                continue

            # --- Classes ---
            if node_type in class_types and self._extract_classes(
                child, source, language, file_path, nodes, edges,
                enclosing_class, import_map, defined_names,
                _depth,
            ):
                continue

            # --- JS/TS class field arrow functions (handler = () => {}) ---
            if (
                language in ("javascript", "typescript", "tsx")
                and node_type == "public_field_definition"
                and self._extract_js_field_function(
                    child, source, language, file_path, nodes, edges,
                    enclosing_class, enclosing_func,
                    import_map, defined_names, _depth,
                )
            ):
                continue

            # --- Functions ---
            if node_type in func_types and self._extract_functions(
                child, source, language, file_path, nodes, edges,
                enclosing_class, import_map, defined_names,
                _depth,
            ):
                continue

            # --- Imports ---
            if node_type in import_types:
                self._extract_imports(
                    child, language, source, file_path, edges,
                )
                continue

            # --- Calls ---
            if node_type in call_types:
                if self._extract_calls(
                    child, source, language, file_path, nodes, edges,
                    enclosing_class, enclosing_func,
                    import_map, defined_names, _depth,
                ):
                    continue

            # --- JSX component invocations ---
            if (
                language in ("javascript", "typescript", "tsx")
                and node_type in ("jsx_opening_element", "jsx_self_closing_element")
            ):
                self._extract_jsx_component_call(
                    child, language, file_path, edges,
                    enclosing_class, enclosing_func,
                    import_map, defined_names,
                )

            # --- Value references (function-as-value in maps, arrays, args) ---
            self._extract_value_references(
                child, node_type, source, language, file_path, edges,
                enclosing_class, enclosing_func,
                import_map, defined_names,
            )

            # --- Solidity-specific constructs ---
            if language == "solidity" and self._extract_solidity_constructs(
                child, node_type, source, file_path, nodes, edges,
                enclosing_class, enclosing_func,
            ):
                continue

            # Recurse for other node types
            self._extract_from_tree(
                child, source, language, file_path, nodes, edges,
                enclosing_class=enclosing_class,
                enclosing_func=enclosing_func,
                import_map=import_map, defined_names=defined_names,
                _depth=_depth + 1,
            )

    def _elixir_call_identifier(self, node) -> Optional[str]:
        """Return the leading identifier of an Elixir ``call`` node.

        For ``def add(a, b)`` returns ``"def"``; for ``defmodule Calc``
        returns ``"defmodule"``; for ``IO.puts(msg)`` returns the dotted
        path's final identifier (``"puts"``); for ``alias Calculator``
        returns ``"alias"``.
        """
        if not node.children:
            return None
        first = node.children[0]
        if first.type == "identifier":
            return first.text.decode("utf-8", errors="replace")
        # Dotted calls: dot > left: alias "IO", right: identifier "puts"
        if first.type == "dot":
            for child in reversed(first.children):
                if child.type == "identifier":
                    return child.text.decode("utf-8", errors="replace")
        return None

    def _elixir_module_name(self, arguments) -> Optional[str]:
        """Extract a module name from a ``defmodule`` / ``alias`` / etc.
        arguments node. Supports ``Calc`` (single alias) and ``Foo.Bar``
        (dotted alias inside a `dot` node).
        """
        for child in arguments.children:
            if child.type == "alias":
                return child.text.decode("utf-8", errors="replace")
            if child.type == "dot":
                return child.text.decode("utf-8", errors="replace")
        return None

    def _elixir_function_name_and_params(
        self, arguments, source: bytes,
    ) -> tuple[Optional[str], Optional[str]]:
        """Extract the function name and parameter list from a ``def``/
        ``defp``/``defmacro`` arguments node.

        The ``arguments`` of a ``def`` call wraps another ``call`` whose
        first child is the function's identifier and whose children
        (past the parens) are the parameters.
        """
        for child in arguments.children:
            if child.type == "call":
                name: Optional[str] = None
                for sub in child.children:
                    if sub.type == "identifier" and name is None:
                        name = sub.text.decode("utf-8", errors="replace")
                # Parameter text is everything between the parens of
                # the inner call; source slice is simplest.
                params_text = child.text.decode("utf-8", errors="replace")
                # Strip the function name off the front.
                if name and params_text.startswith(name):
                    params_text = params_text[len(name):]
                return name, params_text
            if child.type == "identifier":
                # Zero-arity def like `def reset, do: ...` has no inner
                # call; just the identifier.
                return child.text.decode("utf-8", errors="replace"), None
        return None, None

    def _extract_elixir_constructs(
        self,
        node,
        source: bytes,
        language: str,
        file_path: str,
        nodes: list[NodeInfo],
        edges: list[EdgeInfo],
        enclosing_class: Optional[str],
        enclosing_func: Optional[str],
        import_map: Optional[dict[str, str]],
        defined_names: Optional[set[str]],
        _depth: int,
    ) -> bool:
        """Handle every Elixir ``call`` node by dispatching on the leading
        identifier. See: #112

        Returns True if the node was fully handled (and the main loop
        should skip generic recursion); False to let the default dispatch
        continue (never used here — Elixir has no other node types).
        """
        ident = self._elixir_call_identifier(node)
        if ident is None:
            return False

        # ---- defmodule Name do ... end ----------------------------------
        if ident == "defmodule":
            arguments = None
            do_block = None
            for sub in node.children:
                if sub.type == "arguments":
                    arguments = sub
                elif sub.type == "do_block":
                    do_block = sub
            if arguments is None:
                return False
            mod_name = self._elixir_module_name(arguments)
            if mod_name is None:
                return False
            qualified = self._qualify(mod_name, file_path, None)
            nodes.append(NodeInfo(
                kind="Class",
                name=mod_name,
                file_path=file_path,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                language=language,
                parent_name=None,
            ))
            # CONTAINS file -> module
            edges.append(EdgeInfo(
                kind="CONTAINS",
                source=file_path,
                target=qualified,
                file_path=file_path,
                line=node.start_point[0] + 1,
            ))
            if do_block is not None:
                self._extract_from_tree(
                    do_block, source, language, file_path, nodes, edges,
                    enclosing_class=mod_name,
                    enclosing_func=None,
                    import_map=import_map, defined_names=defined_names,
                    _depth=_depth + 1,
                )
            return True

        # ---- def / defp / defmacro / defmacrop -------------------------
        if ident in ("def", "defp", "defmacro", "defmacrop"):
            arguments = None
            do_block = None
            for sub in node.children:
                if sub.type == "arguments":
                    arguments = sub
                elif sub.type == "do_block":
                    do_block = sub
            if arguments is None:
                return False
            fn_name, params = self._elixir_function_name_and_params(
                arguments, source,
            )
            if fn_name is None:
                return False
            is_test = _is_test_function(fn_name, file_path)
            kind = "Test" if is_test else "Function"
            qualified = self._qualify(fn_name, file_path, enclosing_class)
            nodes.append(NodeInfo(
                kind=kind,
                name=fn_name,
                file_path=file_path,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                language=language,
                parent_name=enclosing_class,
                params=params,
                is_test=is_test,
            ))
            container = (
                self._qualify(enclosing_class, file_path, None)
                if enclosing_class else file_path
            )
            edges.append(EdgeInfo(
                kind="CONTAINS",
                source=container,
                target=qualified,
                file_path=file_path,
                line=node.start_point[0] + 1,
            ))
            if do_block is not None:
                self._extract_from_tree(
                    do_block, source, language, file_path, nodes, edges,
                    enclosing_class=enclosing_class,
                    enclosing_func=fn_name,
                    import_map=import_map, defined_names=defined_names,
                    _depth=_depth + 1,
                )
            return True

        # ---- alias / import / require / use ----------------------------
        if ident in ("alias", "import", "require", "use"):
            for sub in node.children:
                if sub.type == "arguments":
                    mod = self._elixir_module_name(sub)
                    if mod is not None:
                        edges.append(EdgeInfo(
                            kind="IMPORTS_FROM",
                            source=file_path,
                            target=mod,
                            file_path=file_path,
                            line=node.start_point[0] + 1,
                        ))
                    break
            return True

        # ---- Everything else = a regular function/method call ----------
        # Emit a CALLS edge when we're inside a function (same rule as
        # the generic _extract_calls path).
        if enclosing_func:
            # For dotted calls like `IO.puts(msg)`, prefer the dotted
            # identifier; for bare calls use the first identifier.
            call_name = ident
            caller = self._qualify(
                enclosing_func, file_path, enclosing_class,
            )
            target = self._resolve_call_target(
                call_name, file_path, language,
                import_map or {}, defined_names or set(),
            )
            edges.append(EdgeInfo(
                kind="CALLS",
                source=caller,
                target=target,
                file_path=file_path,
                line=node.start_point[0] + 1,
            ))
        # Recurse into arguments + do_block so nested calls are caught.
        for sub in node.children:
            if sub.type in ("arguments", "do_block"):
                self._extract_from_tree(
                    sub, source, language, file_path, nodes, edges,
                    enclosing_class=enclosing_class,
                    enclosing_func=enclosing_func,
                    import_map=import_map, defined_names=defined_names,
                    _depth=_depth + 1,
                )
        return True

    def _extract_bash_source_command(
        self,
        node,
        file_path: str,
        edges: list[EdgeInfo],
    ) -> bool:
        """Detect ``source foo.sh`` / ``. foo.sh`` and emit an IMPORTS_FROM
        edge. Returns True if handled (so the main loop skips recursing
        into this command). See: #197
        """
        command_name: Optional[str] = None
        args: list[str] = []
        for sub in node.children:
            if sub.type == "command_name":
                command_name = sub.text.decode("utf-8", errors="replace").strip()
            elif sub.type in ("word", "string", "raw_string") and command_name:
                txt = sub.text.decode("utf-8", errors="replace").strip()
                # Strip surrounding quotes if present
                if len(txt) >= 2 and txt[0] in ("'", '"') and txt[-1] == txt[0]:
                    txt = txt[1:-1]
                if txt:
                    args.append(txt)
        if command_name in ("source", ".") and args:
            target = args[0]
            # Try to resolve relative paths to real files
            resolved = self._resolve_module_to_file(target, file_path, "bash")
            edges.append(EdgeInfo(
                kind="IMPORTS_FROM",
                source=file_path,
                target=resolved if resolved else target,
                file_path=file_path,
                line=node.start_point[0] + 1,
            ))
            return True
        return False

    def _extract_dart_calls_from_children(
        self,
        parent,
        source: bytes,
        file_path: str,
        edges: list[EdgeInfo],
        enclosing_class: Optional[str],
        enclosing_func: Optional[str],
    ) -> None:
        """Detect Dart call sites from a parent node's children (#87 bug 1).

        tree-sitter-dart does not emit a single ``call_expression`` node for
        Dart calls.  Instead it produces ``identifier`` / method-selector
        siblings followed by a ``selector`` whose child is ``argument_part``:

            identifier "print"
            selector
              argument_part

        And for method calls like ``obj.foo()`` the middle selector is a
        ``unconditional_assignable_selector`` holding the method name:

            identifier "obj"
            selector
              unconditional_assignable_selector "."
                identifier "foo"
            selector
              argument_part

        This walker scans the immediate children of ``parent`` for either
        shape and emits a ``CALLS`` edge.  Nested calls are picked up as
        ``_extract_from_tree`` recurses into child nodes.
        """
        call_name: Optional[str] = None
        for sub in parent.children:
            if sub.type == "identifier":
                call_name = sub.text.decode("utf-8", errors="replace")
                continue
            if sub.type == "selector":
                # Case A: selector > unconditional_assignable_selector > identifier
                # (updates call_name to the method name)
                method_name: Optional[str] = None
                has_arguments = False
                for ssub in sub.children:
                    if ssub.type == "unconditional_assignable_selector":
                        for ident in ssub.children:
                            if ident.type == "identifier":
                                method_name = ident.text.decode(
                                    "utf-8", errors="replace"
                                )
                                break
                    elif ssub.type == "argument_part":
                        has_arguments = True
                if method_name is not None:
                    call_name = method_name
                if has_arguments and call_name:
                    src_qn = (
                        self._qualify(enclosing_func, file_path, enclosing_class)
                        if enclosing_func else file_path
                    )
                    edges.append(EdgeInfo(
                        kind="CALLS",
                        source=src_qn,
                        target=call_name,
                        file_path=file_path,
                        line=parent.start_point[0] + 1,
                    ))
                    # After emitting for this call, clear call_name so we
                    # don't re-emit on any trailing chained selector.
                    call_name = None
                continue
            # Non-identifier, non-selector children don't change the
            # pending call name (``return``, ``await``, ``yield``, etc.)
            # but anything unexpected should reset it to avoid spurious
            # edges across unrelated siblings.
            if sub.type not in ("return", "await", "yield", "this", "const", "new"):
                call_name = None

    def _extract_r_constructs(
        self,
        child,
        node_type: str,
        source: bytes,
        language: str,
        file_path: str,
        nodes: list[NodeInfo],
        edges: list[EdgeInfo],
        enclosing_class: Optional[str],
        enclosing_func: Optional[str],
        import_map: Optional[dict[str, str]],
        defined_names: Optional[set[str]],
    ) -> bool:
        """Handle R-specific AST nodes (assignments and class-defining calls).

        Returns True if the child was fully handled and should be skipped
        by the main loop.
        """
        # R: function definitions via assignment
        if node_type == "binary_operator":
            handled = self._handle_r_binary_operator(
                child, source, language, file_path, nodes, edges,
                enclosing_class, enclosing_func,
                import_map, defined_names,
            )
            if handled:
                return True

        # R: setClass/setRefClass/setGeneric calls and imports
        if node_type == "call":
            handled = self._handle_r_call(
                child, source, language, file_path, nodes, edges,
                enclosing_class, enclosing_func,
                import_map, defined_names,
            )
            if handled:
                return True

        return False

    # ------------------------------------------------------------------
    # Lua-specific helpers
    # ------------------------------------------------------------------

    def _extract_lua_constructs(
        self,
        child,
        node_type: str,
        source: bytes,
        language: str,
        file_path: str,
        nodes: list[NodeInfo],
        edges: list[EdgeInfo],
        enclosing_class: Optional[str],
        enclosing_func: Optional[str],
        import_map: Optional[dict[str, str]],
        defined_names: Optional[set[str]],
        _depth: int,
    ) -> bool:
        """Handle Lua-specific AST constructs.

        Returns True if the child was fully handled and should be skipped
        by the main loop.

        Handles:
        - variable_declaration with require() -> IMPORTS_FROM edge
        - variable_declaration with function_definition -> named Function node
        - function_declaration with dot/method name -> Function with table parent
        - top-level require() call -> IMPORTS_FROM edge
        """
        # --- variable_declaration: require() or anonymous function ---
        if node_type == "variable_declaration":
            return self._handle_lua_variable_declaration(
                child, source, language, file_path, nodes, edges,
                enclosing_class, enclosing_func,
                import_map, defined_names, _depth,
            )

        # --- function_declaration with dot/method table name ---
        if node_type == "function_declaration":
            return self._handle_lua_table_function(
                child, source, language, file_path, nodes, edges,
                enclosing_class, enclosing_func,
                import_map, defined_names, _depth,
            )

        # --- Top-level require() not wrapped in variable_declaration ---
        if node_type == "function_call" and not enclosing_func:
            req_target = self._lua_get_require_target(child)
            if req_target is not None:
                resolved = self._resolve_module_to_file(
                    req_target, file_path, language,
                )
                edges.append(EdgeInfo(
                    kind="IMPORTS_FROM",
                    source=file_path,
                    target=resolved if resolved else req_target,
                    file_path=file_path,
                    line=child.start_point[0] + 1,
                ))
                return True

        return False

    def _handle_lua_variable_declaration(
        self,
        child,
        source: bytes,
        language: str,
        file_path: str,
        nodes: list[NodeInfo],
        edges: list[EdgeInfo],
        enclosing_class: Optional[str],
        enclosing_func: Optional[str],
        import_map: Optional[dict[str, str]],
        defined_names: Optional[set[str]],
        _depth: int,
    ) -> bool:
        """Handle Lua variable declarations that contain require() or
        anonymous function definitions.

        ``local json = require("json")``  -> IMPORTS_FROM edge
        ``local fn = function(x) ... end`` -> Function node named "fn"
        """
        # Walk into: variable_declaration > assignment_statement
        assign = None
        for sub in child.children:
            if sub.type == "assignment_statement":
                assign = sub
                break
        if not assign:
            return False

        # Get variable name from variable_list
        var_name = None
        for sub in assign.children:
            if sub.type == "variable_list":
                for ident in sub.children:
                    if ident.type == "identifier":
                        var_name = ident.text.decode("utf-8", errors="replace")
                        break
                break

        # Get value from expression_list
        expr_list = None
        for sub in assign.children:
            if sub.type == "expression_list":
                expr_list = sub
                break

        if not var_name or not expr_list:
            return False

        # Check for require() call
        for expr in expr_list.children:
            if expr.type == "function_call":
                req_target = self._lua_get_require_target(expr)
                if req_target is not None:
                    resolved = self._resolve_module_to_file(
                        req_target, file_path, language,
                    )
                    edges.append(EdgeInfo(
                        kind="IMPORTS_FROM",
                        source=file_path,
                        target=resolved if resolved else req_target,
                        file_path=file_path,
                        line=child.start_point[0] + 1,
                    ))
                    return True

        # Check for anonymous function: local foo = function(...) end
        for expr in expr_list.children:
            if expr.type == "function_definition":
                is_test = _is_test_function(var_name, file_path)
                kind = "Test" if is_test else "Function"
                qualified = self._qualify(var_name, file_path, enclosing_class)
                params = self._get_params(expr, language, source)

                nodes.append(NodeInfo(
                    kind=kind,
                    name=var_name,
                    file_path=file_path,
                    line_start=child.start_point[0] + 1,
                    line_end=child.end_point[0] + 1,
                    language=language,
                    parent_name=enclosing_class,
                    params=params,
                    is_test=is_test,
                ))
                container = (
                    self._qualify(enclosing_class, file_path, None)
                    if enclosing_class else file_path
                )
                edges.append(EdgeInfo(
                    kind="CONTAINS",
                    source=container,
                    target=qualified,
                    file_path=file_path,
                    line=child.start_point[0] + 1,
                ))
                # Recurse into the function body for calls
                self._extract_from_tree(
                    expr, source, language, file_path, nodes, edges,
                    enclosing_class=enclosing_class,
                    enclosing_func=var_name,
                    import_map=import_map,
                    defined_names=defined_names,
                    _depth=_depth + 1,
                )
                return True

        return False

    def _handle_lua_table_function(
        self,
        child,
        source: bytes,
        language: str,
        file_path: str,
        nodes: list[NodeInfo],
        edges: list[EdgeInfo],
        enclosing_class: Optional[str],
        enclosing_func: Optional[str],
        import_map: Optional[dict[str, str]],
        defined_names: Optional[set[str]],
        _depth: int,
    ) -> bool:
        """Handle Lua function declarations with table-qualified names.

        ``function Animal.new(name)``  -> Function "new", parent "Animal"
        ``function Animal:speak()``    -> Function "speak", parent "Animal"

        Plain ``function foo()`` is NOT handled here (returns False).
        """
        table_name = None
        method_name = None

        for sub in child.children:
            if sub.type in ("dot_index_expression", "method_index_expression"):
                identifiers = [
                    c for c in sub.children if c.type == "identifier"
                ]
                if len(identifiers) >= 2:
                    table_name = identifiers[0].text.decode(
                        "utf-8", errors="replace",
                    )
                    method_name = identifiers[-1].text.decode(
                        "utf-8", errors="replace",
                    )
                break

        if not table_name or not method_name:
            return False

        is_test = _is_test_function(method_name, file_path)
        kind = "Test" if is_test else "Function"
        qualified = self._qualify(method_name, file_path, table_name)
        params = self._get_params(child, language, source)

        nodes.append(NodeInfo(
            kind=kind,
            name=method_name,
            file_path=file_path,
            line_start=child.start_point[0] + 1,
            line_end=child.end_point[0] + 1,
            language=language,
            parent_name=table_name,
            params=params,
            is_test=is_test,
        ))
        # CONTAINS: table -> method
        container = self._qualify(table_name, file_path, None)
        edges.append(EdgeInfo(
            kind="CONTAINS",
            source=container,
            target=qualified,
            file_path=file_path,
            line=child.start_point[0] + 1,
        ))
        # Recurse into function body for calls
        self._extract_from_tree(
            child, source, language, file_path, nodes, edges,
            enclosing_class=table_name,
            enclosing_func=method_name,
            import_map=import_map,
            defined_names=defined_names,
            _depth=_depth + 1,
        )
        return True

    @staticmethod
    def _lua_get_require_target(call_node) -> Optional[str]:
        """Extract the module path from a Lua require() call.

        Returns the string argument or None if this is not a require() call.
        """
        # Structure: function_call > identifier("require") > arguments > string
        first_child = call_node.children[0] if call_node.children else None
        if (
            not first_child
            or first_child.type != "identifier"
            or first_child.text != b"require"
        ):
            return None
        for child in call_node.children:
            if child.type == "arguments":
                for arg in child.children:
                    if arg.type == "string":
                        # String node has string_content child
                        for sub in arg.children:
                            if sub.type == "string_content":
                                return sub.text.decode(
                                    "utf-8", errors="replace",
                                )
                        # Fallback: strip quotes from full text
                        raw = arg.text.decode("utf-8", errors="replace")
                        return raw.strip("'\"")
        return None

    # ------------------------------------------------------------------
    # JS/TS: variable-assigned functions  (const foo = () => {})
    # ------------------------------------------------------------------

    _JS_FUNC_VALUE_TYPES = frozenset(
        {"arrow_function", "function_expression", "function"},
    )

    def _extract_js_var_functions(
        self,
        child,
        source: bytes,
        language: str,
        file_path: str,
        nodes: list[NodeInfo],
        edges: list[EdgeInfo],
        enclosing_class: Optional[str],
        enclosing_func: Optional[str],
        import_map: Optional[dict[str, str]],
        defined_names: Optional[set[str]],
        _depth: int,
    ) -> bool:
        """Handle JS/TS variable declarations that assign functions.

        Patterns handled:
          const foo = () => {}
          let bar = function() {}
          export const baz = (x: number): string => x.toString()

        Returns True if at least one function was extracted from the
        declaration, so the caller can skip generic recursion.
        """
        handled = False
        for declarator in child.children:
            if declarator.type != "variable_declarator":
                continue

            # Find identifier and function value
            var_name = None
            func_node = None
            for sub in declarator.children:
                if sub.type == "identifier" and var_name is None:
                    var_name = sub.text.decode("utf-8", errors="replace")
                elif sub.type in self._JS_FUNC_VALUE_TYPES:
                    func_node = sub

            if not var_name or not func_node:
                continue

            is_test = _is_test_function(var_name, file_path)
            kind = "Test" if is_test else "Function"
            qualified = self._qualify(var_name, file_path, enclosing_class)
            params = self._get_params(func_node, language, source)
            ret_type = self._get_return_type(func_node, language, source)

            nodes.append(NodeInfo(
                kind=kind,
                name=var_name,
                file_path=file_path,
                line_start=child.start_point[0] + 1,
                line_end=child.end_point[0] + 1,
                language=language,
                parent_name=enclosing_class,
                params=params,
                return_type=ret_type,
                is_test=is_test,
            ))
            container = (
                self._qualify(enclosing_class, file_path, None)
                if enclosing_class else file_path
            )
            edges.append(EdgeInfo(
                kind="CONTAINS",
                source=container,
                target=qualified,
                file_path=file_path,
                line=child.start_point[0] + 1,
            ))

            # Recurse into the function body for calls
            self._extract_from_tree(
                func_node, source, language, file_path, nodes, edges,
                enclosing_class=enclosing_class,
                enclosing_func=var_name,
                import_map=import_map,
                defined_names=defined_names,
                _depth=_depth + 1,
            )
            handled = True

        if not handled:
            # Not a function assignment — let generic recursion handle it
            return False
        return True

    def _extract_js_field_function(
        self,
        child,
        source: bytes,
        language: str,
        file_path: str,
        nodes: list[NodeInfo],
        edges: list[EdgeInfo],
        enclosing_class: Optional[str],
        enclosing_func: Optional[str],
        import_map: Optional[dict[str, str]],
        defined_names: Optional[set[str]],
        _depth: int,
    ) -> bool:
        """Handle class field arrow functions: handler = (e) => { ... }"""
        prop_name = None
        func_node = None
        for sub in child.children:
            if sub.type == "property_identifier" and prop_name is None:
                prop_name = sub.text.decode("utf-8", errors="replace")
            elif sub.type in self._JS_FUNC_VALUE_TYPES:
                func_node = sub

        if not prop_name or not func_node:
            return False

        is_test = _is_test_function(prop_name, file_path)
        kind = "Test" if is_test else "Function"
        qualified = self._qualify(prop_name, file_path, enclosing_class)
        params = self._get_params(func_node, language, source)

        nodes.append(NodeInfo(
            kind=kind,
            name=prop_name,
            file_path=file_path,
            line_start=child.start_point[0] + 1,
            line_end=child.end_point[0] + 1,
            language=language,
            parent_name=enclosing_class,
            params=params,
            is_test=is_test,
        ))
        container = (
            self._qualify(enclosing_class, file_path, None)
            if enclosing_class else file_path
        )
        edges.append(EdgeInfo(
            kind="CONTAINS",
            source=container,
            target=qualified,
            file_path=file_path,
            line=child.start_point[0] + 1,
        ))

        self._extract_from_tree(
            func_node, source, language, file_path, nodes, edges,
            enclosing_class=enclosing_class,
            enclosing_func=prop_name,
            import_map=import_map,
            defined_names=defined_names,
            _depth=_depth + 1,
        )
        return True

    def _extract_classes(
        self,
        child,
        source: bytes,
        language: str,
        file_path: str,
        nodes: list[NodeInfo],
        edges: list[EdgeInfo],
        enclosing_class: Optional[str],
        import_map: Optional[dict[str, str]],
        defined_names: Optional[set[str]],
        _depth: int,
    ) -> bool:
        """Extract a class definition node and its inheritance edges.

        Returns True if the child was handled (class with a name found).
        """
        name = self._get_name(child, language, "class")
        if not name:
            return False

        # Swift: detect the actual type keyword (class/struct/enum/actor/extension)
        # and store it in extra["swift_kind"] for richer downstream analysis.
        # Tree-sitter maps struct/enum/actor/extension all to class_declaration;
        # protocol uses its own protocol_declaration node type.
        extra: dict = {}
        if language == "swift":
            if child.type == "class_declaration":
                _swift_keywords = {"class", "struct", "enum", "actor", "extension"}
                for kw_child in child.children:
                    kw_text = kw_child.text.decode("utf-8", errors="replace")
                    if kw_text in _swift_keywords:
                        extra["swift_kind"] = kw_text
                        break
            elif child.type == "protocol_declaration":
                extra["swift_kind"] = "protocol"

        node = NodeInfo(
            kind="Class",
            name=name,
            file_path=file_path,
            line_start=child.start_point[0] + 1,
            line_end=child.end_point[0] + 1,
            language=language,
            parent_name=enclosing_class,
            extra=extra,
        )
        nodes.append(node)

        # CONTAINS edge
        edges.append(EdgeInfo(
            kind="CONTAINS",
            source=file_path,
            target=self._qualify(name, file_path, enclosing_class),
            file_path=file_path,
            line=child.start_point[0] + 1,
        ))

        # Inheritance edges
        bases = self._get_bases(child, language, source)
        for base in bases:
            edges.append(EdgeInfo(
                kind="INHERITS",
                source=self._qualify(
                    name, file_path, enclosing_class,
                ),
                target=base,
                file_path=file_path,
                line=child.start_point[0] + 1,
            ))

        # Recurse into class body
        self._extract_from_tree(
            child, source, language, file_path, nodes, edges,
            enclosing_class=name, enclosing_func=None,
            import_map=import_map, defined_names=defined_names,
            _depth=_depth + 1,
        )
        return True

    def _extract_functions(
        self,
        child,
        source: bytes,
        language: str,
        file_path: str,
        nodes: list[NodeInfo],
        edges: list[EdgeInfo],
        enclosing_class: Optional[str],
        import_map: Optional[dict[str, str]],
        defined_names: Optional[set[str]],
        _depth: int,
    ) -> bool:
        """Extract a function/method definition node.

        Returns True if the child was handled (function with a name found).
        """
        name = self._get_name(child, language, "function")
        if not name:
            return False

        # Go methods: attach to their receiver type as the enclosing class,
        # so `func (s *T) Foo()` becomes a member of T rather than a
        # top-level function. See: #190
        if language == "go" and child.type == "method_declaration":
            receiver_type = self._get_go_receiver_type(child)
            if receiver_type:
                enclosing_class = receiver_type

        # Extract annotations/decorators for test detection
        decorators: tuple[str, ...] = ()
        deco_list: list[str] = []
        for sub in child.children:
            # Java/Kotlin/C#: annotations inside a modifiers child
            if sub.type == "modifiers":
                for mod in sub.children:
                    if mod.type in ("annotation", "marker_annotation"):
                        text = mod.text.decode("utf-8", errors="replace")
                        deco_list.append(text.lstrip("@").strip())
        # Python: check parent decorated_definition for decorator siblings
        if child.parent and child.parent.type == "decorated_definition":
            for sib in child.parent.children:
                if sib.type == "decorator":
                    text = sib.text.decode("utf-8", errors="replace")
                    deco_list.append(text.lstrip("@").strip())
        if deco_list:
            decorators = tuple(deco_list)

        is_test = _is_test_function(name, file_path, decorators)
        kind = "Test" if is_test else "Function"
        qualified = self._qualify(name, file_path, enclosing_class)
        params = self._get_params(child, language, source)
        ret_type = self._get_return_type(child, language, source)

        node = NodeInfo(
            kind=kind,
            name=name,
            file_path=file_path,
            line_start=child.start_point[0] + 1,
            line_end=child.end_point[0] + 1,
            language=language,
            parent_name=enclosing_class,
            params=params,
            return_type=ret_type,
            is_test=is_test,
        )
        nodes.append(node)

        # CONTAINS edge
        container = (
            self._qualify(enclosing_class, file_path, None)
            if enclosing_class
            else file_path
        )
        edges.append(EdgeInfo(
            kind="CONTAINS",
            source=container,
            target=qualified,
            file_path=file_path,
            line=child.start_point[0] + 1,
        ))

        # Solidity: modifier invocations on functions -> CALLS edges
        if language == "solidity":
            for sub in child.children:
                if sub.type == "modifier_invocation":
                    for ident in sub.children:
                        if ident.type == "identifier":
                            edges.append(EdgeInfo(
                                kind="CALLS",
                                source=qualified,
                                target=ident.text.decode(
                                    "utf-8", errors="replace",
                                ),
                                file_path=file_path,
                                line=sub.start_point[0] + 1,
                            ))
                            break

        # Recurse to find calls inside the function
        self._extract_from_tree(
            child, source, language, file_path, nodes, edges,
            enclosing_class=enclosing_class, enclosing_func=name,
            import_map=import_map, defined_names=defined_names,
            _depth=_depth + 1,
        )
        return True

    def _extract_imports(
        self,
        child,
        language: str,
        source: bytes,
        file_path: str,
        edges: list[EdgeInfo],
    ) -> None:
        """Extract import edges from an import statement node."""
        imports = self._extract_import(child, language, source)
        for imp_target in imports:
            resolved = self._resolve_module_to_file(
                imp_target, file_path, language,
            )
            edges.append(EdgeInfo(
                kind="IMPORTS_FROM",
                source=file_path,
                target=resolved if resolved else imp_target,
                file_path=file_path,
                line=child.start_point[0] + 1,
            ))

    def _extract_calls(
        self,
        child,
        source: bytes,
        language: str,
        file_path: str,
        nodes: list[NodeInfo],
        edges: list[EdgeInfo],
        enclosing_class: Optional[str],
        enclosing_func: Optional[str],
        import_map: Optional[dict[str, str]],
        defined_names: Optional[set[str]],
        _depth: int,
    ) -> bool:
        """Extract call expressions, including test runner special cases.

        Returns True if the child was fully handled (test runner call that
        should skip default recursion). Returns False if the caller should
        continue to Solidity handling and default recursion.
        """
        call_name = self._get_call_name(child, language, source)

        # For member expressions like describe.only / it.skip / test.each,
        # resolve the base call name so those are treated as test runner
        # calls.
        effective_call_name = call_name
        if (
            call_name
            and language in ("javascript", "typescript", "tsx")
            and _is_test_file(file_path)
            and call_name not in _TEST_RUNNER_NAMES
        ):
            effective_call_name = (
                self._get_base_call_name(child, source) or call_name
            )

        # Special handling: test runner calls in test files -> Test nodes
        if (
            effective_call_name
            and language in ("javascript", "typescript", "tsx")
            and _is_test_file(file_path)
            and effective_call_name in _TEST_RUNNER_NAMES
        ):
            test_desc = self._get_test_description(child, source)
            line_no = child.start_point[0] + 1
            synthetic_base = (
                f"{effective_call_name}:{test_desc}"
                if test_desc else effective_call_name
            )
            synthetic_name = f"{synthetic_base}@L{line_no}"
            qualified = self._qualify(
                synthetic_name, file_path, enclosing_class,
            )

            nodes.append(NodeInfo(
                kind="Test",
                name=synthetic_name,
                file_path=file_path,
                line_start=child.start_point[0] + 1,
                line_end=child.end_point[0] + 1,
                language=language,
                parent_name=enclosing_class,
                is_test=True,
            ))

            # CONTAINS edge: parent -> this test
            container = (
                self._qualify(
                    enclosing_func, file_path, enclosing_class,
                )
                if enclosing_func
                else file_path
            )
            edges.append(EdgeInfo(
                kind="CONTAINS",
                source=container,
                target=qualified,
                file_path=file_path,
                line=child.start_point[0] + 1,
            ))

            # Recurse into the call's children (the arrow function body)
            self._extract_from_tree(
                child, source, language, file_path, nodes, edges,
                enclosing_class=enclosing_class,
                enclosing_func=synthetic_name,
                import_map=import_map, defined_names=defined_names,
                _depth=_depth + 1,
            )
            return True

        if call_name and enclosing_func:
            caller = self._qualify(
                enclosing_func, file_path, enclosing_class,
            )
            target = self._resolve_call_target(
                call_name, file_path, language,
                import_map or {}, defined_names or set(),
            )
            edges.append(EdgeInfo(
                kind="CALLS",
                source=caller,
                target=target,
                file_path=file_path,
                line=child.start_point[0] + 1,
            ))

        return False

    def _extract_jsx_component_call(
        self,
        child,
        language: str,
        file_path: str,
        edges: list[EdgeInfo],
        enclosing_class: Optional[str],
        enclosing_func: Optional[str],
        import_map: Optional[dict[str, str]],
        defined_names: Optional[set[str]],
    ) -> None:
        """Emit a synthetic CALLS edge for JSX component usage.

        React-style component invocations use JSX rather than ``call_expression``.
        Treat uppercase component tags such as ``<MarkdownMsg />`` as call-like
        edges so caller/impact queries can cross the JSX boundary. Intrinsic DOM
        tags (``<div>``) are ignored.
        """
        if not enclosing_func:
            return

        target = self._resolve_jsx_component_target(
            child, language, file_path, import_map or {}, defined_names or set(),
        )
        if not target:
            return

        caller = self._qualify(enclosing_func, file_path, enclosing_class)
        edges.append(EdgeInfo(
            kind="CALLS",
            source=caller,
            target=target,
            file_path=file_path,
            line=child.start_point[0] + 1,
        ))

    def _resolve_jsx_component_target(
        self,
        node,
        language: str,
        file_path: str,
        import_map: dict[str, str],
        defined_names: set[str],
    ) -> Optional[str]:
        """Resolve a JSX component element to a call target."""
        component_ref = self._get_jsx_component_reference(node)
        if component_ref is None:
            return None

        base_name, component_name = component_ref
        if base_name is None:
            return self._resolve_call_target(
                component_name, file_path, language, import_map, defined_names,
            )

        if base_name in import_map:
            resolved = self._resolve_imported_symbol(
                component_name, import_map[base_name], file_path, language,
            )
            if resolved:
                return resolved

        return component_name

    # ------------------------------------------------------------------
    # Value-reference extraction (function-as-value patterns)
    # ------------------------------------------------------------------

    # AST node types that represent object literal key-value pairs.
    _PAIR_TYPES = frozenset({"pair"})

    # AST node types for array/list containers.
    _ARRAY_TYPES = frozenset({"array", "list"})

    # Names that are almost certainly not function references (constants,
    # common primitives).  All-uppercase identifiers and very short names
    # are excluded by a length/casing heuristic in the method itself.
    _VALUE_REF_SKIP_NAMES = frozenset({
        "true", "false", "null", "undefined", "None", "True", "False",
        "self", "this", "cls", "super",
    })

    def _extract_value_references(
        self,
        child,
        node_type: str,
        source: bytes,
        language: str,
        file_path: str,
        edges: list[EdgeInfo],
        enclosing_class: Optional[str],
        enclosing_func: Optional[str],
        import_map: Optional[dict[str, str]],
        defined_names: Optional[set[str]],
    ) -> None:
        """Emit ``REFERENCES`` edges for function-as-value patterns.

        Detects identifiers in value positions that likely refer to
        functions — object literal values, map property assignments,
        array elements, and callback arguments.  This reduces false
        positives in dead-code detection for dispatch-map patterns
        like ``Record<string, Handler>``.

        Only emits edges when the identifier matches a locally defined
        name or an imported symbol, avoiding noise from arbitrary
        variable references.
        """
        imap = import_map or {}
        dnames = defined_names or set()

        # Use enclosing function as source, or the file path for module-scope code.
        if enclosing_func:
            caller = self._qualify(enclosing_func, file_path, enclosing_class)
        else:
            caller = file_path

        # --- JS/TS/Python: object literal pair values  { key: fnRef } ---
        if node_type in self._PAIR_TYPES:
            self._ref_from_pair(child, source, language, file_path, caller, edges, imap, dnames)
            return

        # --- JS/TS: shorthand property identifiers  { fnRef } ---
        if (
            node_type == "shorthand_property_identifier"
            and language in ("javascript", "typescript", "tsx")
        ):
            name = child.text.decode("utf-8", errors="replace")
            self._emit_reference_if_known(
                name, language, file_path, caller, edges, imap, dnames,
                line=child.start_point[0] + 1,
            )
            return

        # --- JS/TS/Python: assignment with member/subscript LHS ---
        if node_type in ("assignment_expression", "augmented_assignment", "assignment"):
            self._ref_from_assignment(
                child, source, language, file_path, caller, edges, imap, dnames,
            )
            return

        # --- JS/TS/Python: array / list elements ---
        if node_type in self._ARRAY_TYPES:
            self._ref_from_array(child, source, language, file_path, caller, edges, imap, dnames)
            return

        # --- Callback arguments (identifier args inside call_expression) ---
        if node_type == "arguments":
            self._ref_from_arguments(
                child, source, language, file_path, caller, edges, imap, dnames,
            )

    def _emit_reference_if_known(
        self,
        name: str,
        language: str,
        file_path: str,
        caller: str,
        edges: list[EdgeInfo],
        import_map: dict[str, str],
        defined_names: set[str],
        line: int = 0,
    ) -> None:
        """Emit a ``REFERENCES`` edge if *name* is a known function/import."""
        if not name or name in self._VALUE_REF_SKIP_NAMES:
            return
        # Skip all-uppercase names (likely constants) and single-char names.
        if name.isupper() or len(name) <= 1:
            return
        # Must be a known local definition or import to be worth tracking.
        if name not in defined_names and name not in import_map:
            return

        target = self._resolve_call_target(
            name, file_path, language, import_map, defined_names,
        )
        edges.append(EdgeInfo(
            kind="REFERENCES",
            source=caller,
            target=target,
            file_path=file_path,
            line=line,
        ))

    def _ref_from_pair(
        self,
        pair_node,
        source: bytes,
        language: str,
        file_path: str,
        caller: str,
        edges: list[EdgeInfo],
        import_map: dict[str, str],
        defined_names: set[str],
    ) -> None:
        """Extract a REFERENCES edge from an object/dict literal pair value."""
        # pair children: key, ":", value
        children = pair_node.children
        # Find the value — it's the last meaningful child.
        value_node = None
        for ch in reversed(children):
            if ch.type not in (":", ",", "comment"):
                value_node = ch
                break
        if value_node is None:
            return
        if value_node.type == "identifier":
            name = value_node.text.decode("utf-8", errors="replace")
            self._emit_reference_if_known(
                name, language, file_path, caller, edges,
                import_map, defined_names,
                line=value_node.start_point[0] + 1,
            )

    def _ref_from_assignment(
        self,
        assign_node,
        source: bytes,
        language: str,
        file_path: str,
        caller: str,
        edges: list[EdgeInfo],
        import_map: dict[str, str],
        defined_names: set[str],
    ) -> None:
        """Extract REFERENCES from ``obj.key = fnRef`` or ``obj['key'] = fnRef``."""
        children = assign_node.children
        if len(children) < 3:
            return
        lhs = children[0]
        # LHS must be a member_expression or subscript_expression (map assignment).
        if lhs.type not in (
            "member_expression", "subscript_expression",
            "attribute", "subscript",
        ):
            return
        # RHS is the last non-punctuation child.
        rhs = None
        for ch in reversed(children):
            if ch.type not in ("=", ":", ",", "comment", "type_annotation"):
                rhs = ch
                break
        if rhs is None or rhs.type != "identifier":
            return
        name = rhs.text.decode("utf-8", errors="replace")
        self._emit_reference_if_known(
            name, language, file_path, caller, edges,
            import_map, defined_names,
            line=rhs.start_point[0] + 1,
        )

    def _ref_from_array(
        self,
        array_node,
        source: bytes,
        language: str,
        file_path: str,
        caller: str,
        edges: list[EdgeInfo],
        import_map: dict[str, str],
        defined_names: set[str],
    ) -> None:
        """Extract REFERENCES from array/list elements that are identifiers."""
        for ch in array_node.children:
            if ch.type == "identifier":
                name = ch.text.decode("utf-8", errors="replace")
                self._emit_reference_if_known(
                    name, language, file_path, caller, edges,
                    import_map, defined_names,
                    line=ch.start_point[0] + 1,
                )

    def _ref_from_arguments(
        self,
        args_node,
        source: bytes,
        language: str,
        file_path: str,
        caller: str,
        edges: list[EdgeInfo],
        import_map: dict[str, str],
        defined_names: set[str],
    ) -> None:
        """Extract REFERENCES from identifier arguments (callbacks)."""
        for ch in args_node.children:
            if ch.type == "identifier":
                name = ch.text.decode("utf-8", errors="replace")
                self._emit_reference_if_known(
                    name, language, file_path, caller, edges,
                    import_map, defined_names,
                    line=ch.start_point[0] + 1,
                )

    def _extract_solidity_constructs(
        self,
        child,
        node_type: str,
        source: bytes,
        file_path: str,
        nodes: list[NodeInfo],
        edges: list[EdgeInfo],
        enclosing_class: Optional[str],
        enclosing_func: Optional[str],
    ) -> bool:
        """Handle Solidity-specific AST constructs (emit, state vars, etc.).

        Returns True if the child was fully handled and should skip
        default recursion.
        """
        # Emit statements: emit EventName(...) -> CALLS edge
        if node_type == "emit_statement" and enclosing_func:
            for sub in child.children:
                if sub.type == "expression":
                    for ident in sub.children:
                        if ident.type == "identifier":
                            caller = self._qualify(
                                enclosing_func, file_path,
                                enclosing_class,
                            )
                            edges.append(EdgeInfo(
                                kind="CALLS",
                                source=caller,
                                target=ident.text.decode(
                                    "utf-8", errors="replace",
                                ),
                                file_path=file_path,
                                line=child.start_point[0] + 1,
                            ))
            # emit_statement falls through to default recursion
            return False

        # State variable declarations -> Function nodes (public ones
        # auto-generate getters, and all are critical for reviews)
        if node_type == "state_variable_declaration" and enclosing_class:
            var_name = None
            var_visibility = None
            var_mutability = None
            var_type = None
            for sub in child.children:
                if sub.type == "identifier":
                    var_name = sub.text.decode(
                        "utf-8", errors="replace",
                    )
                elif sub.type == "visibility":
                    var_visibility = sub.text.decode(
                        "utf-8", errors="replace",
                    )
                elif sub.type == "type_name":
                    var_type = sub.text.decode(
                        "utf-8", errors="replace",
                    )
                elif sub.type in ("constant", "immutable"):
                    var_mutability = sub.type
            if var_name:
                qualified = self._qualify(
                    var_name, file_path, enclosing_class,
                )
                nodes.append(NodeInfo(
                    kind="Function",
                    name=var_name,
                    file_path=file_path,
                    line_start=child.start_point[0] + 1,
                    line_end=child.end_point[0] + 1,
                    language="solidity",
                    parent_name=enclosing_class,
                    return_type=var_type,
                    modifiers=var_visibility,
                    extra={
                        "solidity_kind": "state_variable",
                        "mutability": var_mutability,
                    },
                ))
                edges.append(EdgeInfo(
                    kind="CONTAINS",
                    source=self._qualify(
                        enclosing_class, file_path, None,
                    ),
                    target=qualified,
                    file_path=file_path,
                    line=child.start_point[0] + 1,
                ))
                return True
            return False

        # File-level and contract-level constant declarations
        if node_type == "constant_variable_declaration":
            var_name = None
            var_type = None
            for sub in child.children:
                if sub.type == "identifier":
                    var_name = sub.text.decode(
                        "utf-8", errors="replace",
                    )
                elif sub.type == "type_name":
                    var_type = sub.text.decode(
                        "utf-8", errors="replace",
                    )
            if var_name:
                qualified = self._qualify(
                    var_name, file_path, enclosing_class,
                )
                nodes.append(NodeInfo(
                    kind="Function",
                    name=var_name,
                    file_path=file_path,
                    line_start=child.start_point[0] + 1,
                    line_end=child.end_point[0] + 1,
                    language="solidity",
                    parent_name=enclosing_class,
                    return_type=var_type,
                    extra={"solidity_kind": "constant"},
                ))
                container = (
                    self._qualify(enclosing_class, file_path, None)
                    if enclosing_class
                    else file_path
                )
                edges.append(EdgeInfo(
                    kind="CONTAINS",
                    source=container,
                    target=qualified,
                    file_path=file_path,
                    line=child.start_point[0] + 1,
                ))
                return True
            return False

        # Using directives: using LibName for Type -> DEPENDS_ON edge
        if node_type == "using_directive":
            lib_name = None
            for sub in child.children:
                if sub.type == "type_alias":
                    for ident in sub.children:
                        if ident.type == "identifier":
                            lib_name = ident.text.decode(
                                "utf-8", errors="replace",
                            )
            if lib_name:
                source_name = (
                    self._qualify(
                        enclosing_class, file_path, None,
                    )
                    if enclosing_class
                    else file_path
                )
                edges.append(EdgeInfo(
                    kind="DEPENDS_ON",
                    source=source_name,
                    target=lib_name,
                    file_path=file_path,
                    line=child.start_point[0] + 1,
                ))
            return True

        return False

    def _collect_file_scope(
        self, root, language: str, source: bytes,
    ) -> tuple[dict[str, str], set[str]]:
        """Pre-scan top-level AST to collect import mappings and defined names.

        Returns:
            (import_map, defined_names) where import_map maps imported names
            to their source module/path, and defined_names is the set of
            function/class names defined at file scope.
        """
        import_map: dict[str, str] = {}
        defined_names: set[str] = set()

        class_types = set(_CLASS_TYPES.get(language, []))
        func_types = set(_FUNCTION_TYPES.get(language, []))
        import_types = set(_IMPORT_TYPES.get(language, []))

        # Node types that wrap a class/function with decorators/annotations
        decorator_wrappers = {"decorated_definition", "decorator"}

        for child in root.children:
            node_type = child.type

            # Unwrap decorator wrappers to reach the inner definition
            target = child
            if node_type in decorator_wrappers:
                for inner in child.children:
                    if inner.type in func_types or inner.type in class_types:
                        target = inner
                        break
            elif (
                language in ("javascript", "typescript", "tsx")
                and node_type == "export_statement"
            ):
                for inner in child.children:
                    if inner.type in func_types or inner.type in class_types:
                        target = inner
                        break

            target_type = target.type

            # R: function names live on the left side of binary_operator
            if language == "r" and target_type == "binary_operator":
                r_children = target.children
                if (
                    len(r_children) >= 3
                    and r_children[0].type == "identifier"
                    and r_children[2].type == "function_definition"
                ):
                    name = r_children[0].text.decode("utf-8", errors="replace")
                    defined_names.add(name)
                    continue

            # Collect defined function/class names
            if target_type in func_types or target_type in class_types:
                name = self._get_name(target, language,
                                      "class" if target_type in class_types else "function")
                if name:
                    defined_names.add(name)
                    continue

            if (
                language in ("javascript", "typescript", "tsx")
                and node_type == "export_statement"
            ):
                self._collect_js_exported_local_names(child, defined_names)

            # Collect import mappings: imported_name → module_path
            if node_type in import_types:
                self._collect_import_names(child, language, source, import_map)

        return import_map, defined_names

    def _collect_js_exported_local_names(
        self, node, defined_names: set[str],
    ) -> None:
        """Collect locally exported JS/TS names from export statements."""
        for child in node.children:
            if child.type in ("lexical_declaration", "variable_declaration"):
                for sub in child.children:
                    if sub.type == "variable_declarator":
                        for part in sub.children:
                            if part.type == "identifier":
                                defined_names.add(
                                    part.text.decode("utf-8", errors="replace"),
                                )
                                break

    def _collect_import_names(
        self, node, language: str, source: bytes, import_map: dict[str, str],
    ) -> None:
        """Extract imported names and their source modules into import_map."""
        if language == "python":
            if node.type == "import_from_statement":
                # from X.Y import A, B → {A: X.Y, B: X.Y}
                module = None
                seen_import_keyword = False
                for child in node.children:
                    if child.type == "dotted_name" and not seen_import_keyword:
                        module = child.text.decode("utf-8", errors="replace")
                    elif child.type == "import":
                        seen_import_keyword = True
                    elif seen_import_keyword and module:
                        if child.type in ("identifier", "dotted_name"):
                            name = child.text.decode("utf-8", errors="replace")
                            import_map[name] = module
                        elif child.type == "aliased_import":
                            # from X import A as B → {B: X}
                            names = [
                                sub.text.decode("utf-8", errors="replace")
                                for sub in child.children
                                if sub.type in ("identifier", "dotted_name")
                            ]
                            # Last name is the alias (local name)
                            if names:
                                import_map[names[-1]] = module

        elif language in ("javascript", "typescript", "tsx"):
            # import { A, B } from './path' → {A: ./path, B: ./path}
            module = None
            for child in node.children:
                if child.type == "string":
                    module = child.text.decode("utf-8", errors="replace").strip("'\"")
            if module:
                for child in node.children:
                    if child.type == "import_clause":
                        self._collect_js_import_names(child, module, import_map)

    def _collect_js_import_names(
        self, clause_node, module: str, import_map: dict[str, str],
    ) -> None:
        """Walk JS/TS import_clause to extract named and default imports."""
        for child in clause_node.children:
            if child.type == "identifier":
                # Default import
                import_map[child.text.decode("utf-8", errors="replace")] = module
            elif child.type == "namespace_import":
                for sub in child.children:
                    if sub.type == "identifier":
                        import_map[sub.text.decode("utf-8", errors="replace")] = module
                        break
            elif child.type == "named_imports":
                for spec in child.children:
                    if spec.type == "import_specifier":
                        # Could be: name or name as alias
                        names = [
                            s.text.decode("utf-8", errors="replace")
                            for s in spec.children
                            if s.type in ("identifier", "property_identifier")
                        ]
                        # Last identifier is the local name
                        if names:
                            import_map[names[-1]] = module

    def _resolve_module_to_file(
        self, module: str, file_path: str, language: str,
    ) -> Optional[str]:
        """Resolve a module/import path to an absolute file path.

        Uses self._module_file_cache to avoid repeated filesystem lookups.
        """
        caller_dir = str(Path(file_path).parent)
        cache_key = f"{language}:{caller_dir}:{module}"
        if cache_key in self._module_file_cache:
            return self._module_file_cache[cache_key]

        resolved = self._do_resolve_module(module, file_path, language)
        if len(self._module_file_cache) >= self._MODULE_CACHE_MAX:
            self._module_file_cache.clear()
        self._module_file_cache[cache_key] = resolved
        return resolved

    def _do_resolve_module(
        self, module: str, file_path: str, language: str,
    ) -> Optional[str]:
        """Language-aware module-to-file resolution."""
        caller_dir = Path(file_path).parent

        if language == "bash":
            # ``source ./lib.sh`` or ``source lib.sh`` — resolve relative
            # to the caller's directory. See: #197
            try:
                target = (caller_dir / module).resolve()
                if target.is_file():
                    return str(target)
            except (OSError, ValueError):
                pass
            return None

        if language == "python":
            rel_path = module.replace(".", "/")
            candidates = [rel_path + ".py", rel_path + "/__init__.py"]
            # Walk up from caller's directory to find the module file
            current = caller_dir
            while True:
                for candidate in candidates:
                    target = current / candidate
                    if target.is_file():
                        return str(target.resolve())
                if current == current.parent:
                    break
                current = current.parent

        elif language in ("javascript", "typescript", "tsx", "vue"):
            if module.startswith("."):
                # Relative import — resolve from caller's directory
                base = caller_dir / module
                extensions = [".ts", ".tsx", ".js", ".jsx", ".vue"]
                # Try exact path first (might already have extension)
                if base.is_file():
                    return str(base.resolve())
                # Try with extensions
                for ext in extensions:
                    target = base.with_suffix(ext)
                    if target.is_file():
                        return str(target.resolve())
                # Try index file in directory
                if base.is_dir():
                    for ext in extensions:
                        target = base / f"index{ext}"
                        if target.is_file():
                            return str(target.resolve())
            else:
                # Non-relative import — try tsconfig path alias resolution
                resolved = self._tsconfig_resolver.resolve_alias(module, file_path)
                if resolved:
                    return resolved

        elif language == "dart":
            if module.startswith("."):
                # Dart relative imports include the .dart extension
                base = caller_dir / module
                if base.is_file():
                    return str(base.resolve())
                # Fallback: try appending .dart
                target = base.with_suffix(".dart")
                if target.is_file():
                    return str(target.resolve())
            elif module.startswith("package:"):
                # ``package:<name>/<sub_path>`` — resolve to the current repo's
                # ``lib/<sub_path>`` iff a ``pubspec.yaml`` declaring that
                # package name is found in an ancestor directory. See: #87
                try:
                    uri_body = module[len("package:"):]
                    pkg_name, _, sub_path = uri_body.partition("/")
                    if not sub_path:
                        return None
                    pubspec_root = self._find_dart_pubspec_root(
                        caller_dir, pkg_name
                    )
                    if pubspec_root is not None:
                        target = pubspec_root / "lib" / sub_path
                        if target.is_file():
                            return str(target.resolve())
                except (OSError, ValueError):
                    return None
            # ``dart:core`` / ``dart:async`` etc. are SDK libraries we do
            # not track; fall through to return None.

        return None

    def _find_dart_pubspec_root(
        self, start: Path, pkg_name: str,
    ) -> Optional[Path]:
        """Walk up from ``start`` to find a ``pubspec.yaml`` whose ``name:``
        matches ``pkg_name``. Returns the directory containing that pubspec,
        or None if no match is found. Result is cached per (start, pkg_name)
        pair so repeated lookups within one parse pass are cheap.
        """
        cache_key = (str(start), pkg_name)
        cached = self._dart_pubspec_cache.get(cache_key)
        if cached is not None or cache_key in self._dart_pubspec_cache:
            return cached
        current = start
        # Avoid infinite loops on weird symlinks.
        for _ in range(20):
            pubspec = current / "pubspec.yaml"
            if pubspec.is_file():
                try:
                    text = pubspec.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = ""
                m = re.search(r"^name:\s*([\w-]+)", text, re.MULTILINE)
                if m and m.group(1) == pkg_name:
                    self._dart_pubspec_cache[cache_key] = current
                    return current
            if current.parent == current:
                break
            current = current.parent
        self._dart_pubspec_cache[cache_key] = None
        return None

    def _resolve_call_target(
        self,
        call_name: str,
        file_path: str,
        language: str,
        import_map: dict[str, str],
        defined_names: set[str],
    ) -> str:
        """Resolve a bare call name to a qualified target, with fallback."""
        if call_name in defined_names:
            return self._qualify(call_name, file_path, None)
        if call_name in import_map:
            resolved = self._resolve_imported_symbol(
                call_name, import_map[call_name], file_path, language,
            )
            if resolved:
                return resolved
        return call_name

    def _resolve_imported_symbol(
        self,
        symbol_name: str,
        module: str,
        file_path: str,
        language: str,
    ) -> Optional[str]:
        """Resolve an imported symbol to its defining qualified name when possible."""
        resolved = self._resolve_module_to_file(module, file_path, language)
        if not resolved:
            return None

        export_target = self._resolve_exported_symbol(resolved, symbol_name)
        if export_target:
            return export_target
        return self._qualify(symbol_name, resolved, None)

    def _resolve_exported_symbol(
        self,
        module_file: str,
        symbol_name: str,
        seen: Optional[set[tuple[str, str]]] = None,
    ) -> Optional[str]:
        """Resolve a JS/TS symbol through common re-export/barrel patterns."""
        cache_key = f"{module_file}::{symbol_name}"
        if cache_key in self._export_symbol_cache:
            return self._export_symbol_cache[cache_key]

        key = (module_file, symbol_name)
        if seen is None:
            seen = set()
        if key in seen:
            return None
        seen.add(key)

        path = Path(module_file)
        language = self.detect_language(path)
        if language not in ("javascript", "typescript", "tsx", "vue"):
            return None

        try:
            source = path.read_bytes()
        except (OSError, PermissionError):
            return None

        parser = self._get_parser(language)
        if not parser:
            return None

        tree = parser.parse(source)

        # Direct local definition/export in the module file.
        import_map, defined_names = self._collect_file_scope(
            tree.root_node, language, source,
        )
        if symbol_name in defined_names:
            result = self._qualify(symbol_name, module_file, None)
            self._export_symbol_cache[cache_key] = result
            return result

        for child in tree.root_node.children:
            if child.type != "export_statement":
                continue

            export_clause = None
            target_module = None
            has_star_export = False

            for sub in child.children:
                if sub.type == "export_clause":
                    export_clause = sub
                elif sub.type == "string":
                    target_module = sub.text.decode("utf-8", errors="replace").strip("'\"")
                elif sub.type == "*":
                    has_star_export = True

            # Re-exported names: export { Foo as Bar } from './x'
            if export_clause is not None:
                for spec in export_clause.children:
                    if spec.type != "export_specifier":
                        continue
                    names = [
                        part.text.decode("utf-8", errors="replace")
                        for part in spec.children
                        if part.type in ("identifier", "property_identifier")
                    ]
                    if not names:
                        continue
                    exported_name = names[-1]
                    original_name = names[0]
                    if exported_name != symbol_name:
                        continue
                    if target_module:
                        resolved_module = self._resolve_module_to_file(
                            target_module, module_file, language,
                        )
                        if resolved_module:
                            result = self._resolve_exported_symbol(
                                resolved_module, original_name, seen,
                            ) or self._qualify(original_name, resolved_module, None)
                            self._export_symbol_cache[cache_key] = result
                            return result
                    result = self._qualify(original_name, module_file, None)
                    self._export_symbol_cache[cache_key] = result
                    return result

            # Star re-export: export * from './x'
            if has_star_export and target_module:
                resolved_module = self._resolve_module_to_file(
                    target_module, module_file, language,
                )
                if resolved_module:
                    result = self._resolve_exported_symbol(
                        resolved_module, symbol_name, seen,
                    )
                    if result:
                        self._export_symbol_cache[cache_key] = result
                        return result

        self._export_symbol_cache[cache_key] = None
        return None

    def _qualify(self, name: str, file_path: str, enclosing_class: Optional[str]) -> str:
        """Create a qualified name: file_path::ClassName.name or file_path::name."""
        if enclosing_class:
            return f"{file_path}::{enclosing_class}.{name}"
        return f"{file_path}::{name}"

    def _get_name(self, node, language: str, kind: str) -> Optional[str]:
        """Extract the name from a class/function definition node."""
        # Dart: function_signature has a return-type node before the identifier;
        # search only for 'identifier' to avoid returning the return type name.
        if language == "dart" and node.type == "function_signature":
            for child in node.children:
                if child.type == "identifier":
                    return child.text.decode("utf-8", errors="replace")
            return None
        # Solidity: constructor and receive/fallback have no identifier child
        if language == "solidity":
            if node.type == "constructor_definition":
                return "constructor"
            if node.type == "fallback_receive_definition":
                for child in node.children:
                    if child.type in ("receive", "fallback"):
                        return child.text.decode("utf-8", errors="replace")
        # Lua/Luau: function_declaration names may be dot_index_expression or
        # method_index_expression (e.g. function Animal.new() / Animal:speak()).
        # Return only the method name; the table name is used as parent_name
        # in _extract_lua_constructs.
        if language in ("lua", "luau") and node.type == "function_declaration":
            for child in node.children:
                if child.type in ("dot_index_expression", "method_index_expression"):
                    # Last identifier child is the method name
                    for sub in reversed(child.children):
                        if sub.type == "identifier":
                            return sub.text.decode("utf-8", errors="replace")
                    return None
        # Perl: bareword for subroutine names, package for package names
        if language == "perl":
            for child in node.children:
                if child.type == "bareword":
                    return child.text.decode("utf-8", errors="replace")
                if child.type == "package" and child.text != b"package":
                    return child.text.decode("utf-8", errors="replace")
        # For C/C++/Objective-C: function names are inside
        # function_declarator / pointer_declarator. Check these first to
        # avoid matching the return type_identifier as the function name.
        if language in ("c", "cpp", "objc") and kind == "function":
            for child in node.children:
                if child.type in ("function_declarator", "pointer_declarator"):
                    result = self._get_name(child, language, kind)
                    if result:
                        return result

        # Objective-C method_definition: the method name is the first
        # ``identifier`` child (first part of the selector). Multi-part
        # selectors like ``- (void)add:(int)a to:(int)b`` keep ``add`` as
        # the canonical method name; later parts are keyword arguments.
        if language == "objc" and node.type == "method_definition":
            for child in node.children:
                if child.type == "identifier":
                    return child.text.decode("utf-8", errors="replace")

        # Bash function_definition: ``foo() { ... }`` — tree-sitter-bash
        # stores the function name as a ``word`` child, which the generic
        # loop below doesn't recognize.
        if language == "bash" and node.type == "function_definition":
            for child in node.children:
                if child.type == "word":
                    return child.text.decode("utf-8", errors="replace")
        # Go methods: tree-sitter-go uses field_identifier for the name
        # (e.g. func (s *T) MethodName(...) { }). Must run before the generic
        # loop, which would match the result type's type_identifier (e.g. int64).
        if language == "go" and node.type == "method_declaration":
            for child in node.children:
                if child.type == "field_identifier":
                    return child.text.decode("utf-8", errors="replace")
        # Swift extensions: name is inside user_type > type_identifier
        # (e.g. `extension MyClass: Protocol { ... }`)
        if language == "swift" and node.type == "class_declaration":
            for child in node.children:
                if child.type == "user_type":
                    for sub in child.children:
                        if sub.type == "type_identifier":
                            return sub.text.decode("utf-8", errors="replace")
        # Most languages use a 'name' child
        for child in node.children:
            if child.type in (
                "identifier", "name", "type_identifier", "property_identifier",
                "simple_identifier", "constant",
            ):
                return child.text.decode("utf-8", errors="replace")
        # For Go type declarations, look for type_spec
        if language == "go" and node.type == "type_declaration":
            for child in node.children:
                if child.type == "type_spec":
                    return self._get_name(child, language, kind)
        return None

    def _get_go_receiver_type(self, node) -> Optional[str]:
        """Extract the receiver type from a Go method_declaration.

        For ``func (s *T) Foo() {...}`` returns ``"T"``. For ``func (T) Foo()``
        also returns ``"T"``. Returns None if no receiver is present.

        The receiver is always the first ``parameter_list`` child of a
        Go ``method_declaration`` and contains a single ``parameter_declaration``
        whose type is either a ``type_identifier`` or a ``pointer_type``
        wrapping one. See: #190
        """
        for child in node.children:
            if child.type != "parameter_list":
                continue
            for param in child.children:
                if param.type != "parameter_declaration":
                    continue
                for sub in param.children:
                    if sub.type == "type_identifier":
                        return sub.text.decode("utf-8", errors="replace")
                    if sub.type == "pointer_type":
                        for ptr_child in sub.children:
                            if ptr_child.type == "type_identifier":
                                return ptr_child.text.decode(
                                    "utf-8", errors="replace"
                                )
            # First parameter_list is always the receiver; stop searching.
            return None
        return None

    def _get_params(self, node, language: str, source: bytes) -> Optional[str]:
        """Extract parameter list as a string."""
        for child in node.children:
            param_types = (
                "parameters", "formal_parameters",
                "parameter_list", "formal_parameter_list",
            )
            if child.type in param_types:
                return child.text.decode("utf-8", errors="replace")
        # Solidity: parameters are direct children between ( and )
        if language == "solidity":
            params = [
                c.text.decode("utf-8", errors="replace")
                for c in node.children
                if c.type == "parameter"
            ]
            if params:
                return f"({', '.join(params)})"
        return None

    def _get_return_type(self, node, language: str, source: bytes) -> Optional[str]:
        """Extract return type annotation if present."""
        for child in node.children:
            if child.type in ("type", "return_type", "type_annotation", "return_type_definition"):
                return child.text.decode("utf-8", errors="replace")
        # Python: look for -> annotation
        if language == "python":
            for i, child in enumerate(node.children):
                if child.type == "->" and i + 1 < len(node.children):
                    return node.children[i + 1].text.decode("utf-8", errors="replace")
        return None

    def _get_bases(self, node, language: str, source: bytes) -> list[str]:
        """Extract base classes / implemented interfaces."""
        bases = []
        if language == "python":
            for child in node.children:
                if child.type == "argument_list":
                    for arg in child.children:
                        if arg.type in ("identifier", "attribute"):
                            bases.append(arg.text.decode("utf-8", errors="replace"))
        elif language in ("java", "csharp", "kotlin"):
            # Look for superclass/interfaces in extends/implements clauses
            for child in node.children:
                if child.type in (
                    "superclass", "super_interfaces", "extends_type",
                    "implements_type", "type_identifier", "supertype",
                    "delegation_specifier",
                ):
                    text = child.text.decode("utf-8", errors="replace")
                    bases.append(text)
        elif language == "scala":
            for child in node.children:
                if child.type == "extends_clause":
                    for sub in child.children:
                        if sub.type == "type_identifier":
                            bases.append(sub.text.decode("utf-8", errors="replace"))
                        elif sub.type == "generic_type":
                            for ident in sub.children:
                                if ident.type == "type_identifier":
                                    bases.append(
                                        ident.text.decode("utf-8", errors="replace")
                                    )
                                    break
        elif language == "cpp":
            # C++: base_class_clause contains type_identifiers
            for child in node.children:
                if child.type == "base_class_clause":
                    for sub in child.children:
                        if sub.type == "type_identifier":
                            bases.append(sub.text.decode("utf-8", errors="replace"))
        elif language in ("typescript", "javascript", "tsx"):
            # extends clause
            for child in node.children:
                if child.type in ("extends_clause", "implements_clause"):
                    for sub in child.children:
                        if sub.type in ("identifier", "type_identifier", "nested_identifier"):
                            bases.append(sub.text.decode("utf-8", errors="replace"))
        elif language == "solidity":
            # contract Foo is Bar, Baz { ... }
            for child in node.children:
                if child.type == "inheritance_specifier":
                    for sub in child.children:
                        if sub.type == "user_defined_type":
                            for ident in sub.children:
                                if ident.type == "identifier":
                                    bases.append(ident.text.decode("utf-8", errors="replace"))
        elif language == "go":
            # Embedded structs / interface composition
            for child in node.children:
                if child.type == "type_spec":
                    for sub in child.children:
                        if sub.type in ("struct_type", "interface_type"):
                            for field_node in sub.children:
                                if field_node.type == "field_declaration_list":
                                    for f in field_node.children:
                                        if f.type == "type_identifier":
                                            bases.append(f.text.decode("utf-8", errors="replace"))
        elif language == "dart":
            # class Foo extends Bar with Mixin implements Iface { ... }
            # AST: superclass contains type_identifier (base) and mixins (with clause);
            #      interfaces is a sibling of superclass.
            for child in node.children:
                if child.type == "superclass":
                    for sub in child.children:
                        if sub.type == "type_identifier":
                            bases.append(sub.text.decode("utf-8", errors="replace"))
                        elif sub.type == "mixins":
                            for m in sub.children:
                                if m.type == "type_identifier":
                                    bases.append(m.text.decode("utf-8", errors="replace"))
                elif child.type == "interfaces":
                    for sub in child.children:
                        if sub.type == "type_identifier":
                            bases.append(sub.text.decode("utf-8", errors="replace"))
        elif language == "swift":
            # Swift: class Foo: Bar, Baz { ... } / extension Foo: Protocol { ... }
            # AST: inheritance_specifier > user_type > type_identifier
            for child in node.children:
                if child.type == "inheritance_specifier":
                    for sub in child.children:
                        if sub.type == "user_type":
                            for ident in sub.children:
                                if ident.type == "type_identifier":
                                    bases.append(
                                        ident.text.decode("utf-8", errors="replace")
                                    )
                                    break
        return bases

    def _extract_import(self, node, language: str, source: bytes) -> list[str]:
        """Extract import targets as module/path strings."""
        imports = []
        text = node.text.decode("utf-8", errors="replace").strip()

        if language == "python":
            # import x.y.z  or  from x.y import z
            if node.type == "import_from_statement":
                for child in node.children:
                    if child.type == "dotted_name":
                        imports.append(child.text.decode("utf-8", errors="replace"))
                        break
            else:
                for child in node.children:
                    if child.type == "dotted_name":
                        imports.append(child.text.decode("utf-8", errors="replace"))
        elif language in ("javascript", "typescript", "tsx"):
            # import ... from 'module'
            for child in node.children:
                if child.type == "string":
                    val = child.text.decode("utf-8", errors="replace").strip("'\"")
                    imports.append(val)
        elif language == "go":
            for child in node.children:
                if child.type == "import_spec_list":
                    for spec in child.children:
                        if spec.type == "import_spec":
                            for s in spec.children:
                                if s.type == "interpreted_string_literal":
                                    val = s.text.decode("utf-8", errors="replace")
                                    imports.append(val.strip('"'))
                elif child.type == "import_spec":
                    for s in child.children:
                        if s.type == "interpreted_string_literal":
                            val = s.text.decode("utf-8", errors="replace")
                            imports.append(val.strip('"'))
        elif language == "rust":
            # use crate::module::item
            imports.append(text.replace("use ", "").rstrip(";").strip())
        elif language in ("c", "cpp"):
            # #include <header> or #include "header"
            for child in node.children:
                if child.type in ("system_lib_string", "string_literal"):
                    val = child.text.decode("utf-8", errors="replace").strip("<>\"")
                    imports.append(val)
        elif language in ("java", "csharp"):
            # import/using package.Class
            parts = text.split()
            if len(parts) >= 2:
                imports.append(parts[-1].rstrip(";"))
        elif language == "solidity":
            # import "path/to/file.sol" or import {Symbol} from "path"
            for child in node.children:
                if child.type == "string":
                    val = child.text.decode("utf-8", errors="replace").strip('"')
                    if val:
                        imports.append(val)
        elif language == "scala":
            parts = []
            selectors = []
            is_wildcard = False
            for child in node.children:
                if child.type == "identifier":
                    parts.append(child.text.decode("utf-8", errors="replace"))
                elif child.type == "namespace_selectors":
                    for sub in child.children:
                        if sub.type == "identifier":
                            selectors.append(sub.text.decode("utf-8", errors="replace"))
                elif child.type == "namespace_wildcard":
                    is_wildcard = True
            base = ".".join(parts)
            if selectors:
                for name in selectors:
                    imports.append(f"{base}.{name}")
            elif is_wildcard:
                imports.append(f"{base}.*")
            elif base:
                imports.append(base)
        elif language == "r":
            # library(pkg), require(pkg), source("file.R")
            func_name = self._r_call_func_name(node)
            if func_name in ("library", "require", "source"):
                for _name, value in self._r_iter_args(node):
                    if value.type == "identifier":
                        imports.append(value.text.decode("utf-8", errors="replace"))
                    elif value.type == "string":
                        val = self._r_first_string_arg(node)
                        if val:
                            imports.append(val)
                    break  # Only first argument matters
        elif language == "ruby":
            # require 'module' or require_relative 'path'
            if "require" in text:
                match = re.search(r"""['"](.*?)['"]""", text)
                if match:
                    imports.append(match.group(1))
        elif language == "dart":
            # import 'dart:async' or import 'package:flutter/material.dart'
            # Node structure: import_or_export > library_import > import_specification
            #                 > configurable_uri > uri > string_literal
            def _find_string_literal(n) -> Optional[str]:
                if n.type == "string_literal":
                    return n.text.decode("utf-8", errors="replace").strip("'\"")
                for c in n.children:
                    result = _find_string_literal(c)
                    if result is not None:
                        return result
                return None
            val = _find_string_literal(node)
            if val:
                imports.append(val)
        else:
            # Fallback: just record the text
            imports.append(text)

        return imports

    def _get_call_name(self, node, language: str, source: bytes) -> Optional[str]:
        """Extract the function/method name being called."""
        if not node.children:
            return None

        first = node.children[0]

        # Scala: instance_expression (new Foo(...)) – extract the type name
        if node.type == "instance_expression":
            for child in node.children:
                if child.type in ("type_identifier", "identifier"):
                    return child.text.decode("utf-8", errors="replace")
            return None

        # Objective-C: [receiver method:arg] — the method name is the
        # SECOND identifier-like child (the first is the receiver). For
        # multi-part selectors like `[obj add:a to:b]` we keep the first
        # part (`add`) as the call name; later parts are keyword arguments.
        if language == "objc" and node.type == "message_expression":
            receiver_skipped = False
            for child in node.children:
                if child.type in ("[", "]"):
                    continue
                if not receiver_skipped:
                    # First non-bracket child is the receiver (identifier,
                    # message_expression for chained calls, etc.)
                    receiver_skipped = True
                    continue
                if child.type == "identifier":
                    return child.text.decode("utf-8", errors="replace")
            return None

        # Bash: `command` node's first child is the command name.
        if language == "bash" and node.type == "command":
            for child in node.children:
                if child.type == "command_name":
                    # command_name wraps a word — get its text
                    txt = child.text.decode("utf-8", errors="replace").strip()
                    return txt or None
            return None

        # Solidity wraps call targets in an 'expression' node – unwrap it
        if language == "solidity" and first.type == "expression" and first.children:
            first = first.children[0]

        # Perl method_call_expression: $obj->method() — find the 'method' child
        if language == "perl" and node.type == "method_call_expression":
            for child in node.children:
                if child.type == "method":
                    return child.text.decode("utf-8", errors="replace")
            return None  # method child not found

        # Simple call: func_name(args)
        # Kotlin uses "simple_identifier" instead of "identifier".
        if first.type in ("identifier", "simple_identifier"):
            return first.text.decode("utf-8", errors="replace")

        # Perl: function_call_expression / ambiguous_function_call_expression
        if first.type == "function":
            return first.text.decode("utf-8", errors="replace")

        # Lua/Luau: dot_index_expression (obj.method) and method_index_expression
        # (obj:method) — extract the rightmost identifier as the call name.
        if language in ("lua", "luau") and first.type in (
            "dot_index_expression", "method_index_expression",
        ):
            for child in reversed(first.children):
                if child.type == "identifier":
                    return child.text.decode("utf-8", errors="replace")
            return None

        # Method call: obj.method(args)
        # Kotlin uses "navigation_expression" for member access (obj.method).
        member_types = (
            "attribute", "member_expression",
            "field_expression", "selector_expression",
            "navigation_expression",
        )
        if first.type in member_types:
            # Get the rightmost identifier (the method name)
            # Kotlin navigation_expression uses navigation_suffix > simple_identifier.
            for child in reversed(first.children):
                if child.type in (
                    "identifier", "property_identifier", "field_identifier",
                    "field_name", "simple_identifier",
                ):
                    return child.text.decode("utf-8", errors="replace")
                if child.type == "navigation_suffix":
                    for sub in child.children:
                        if sub.type == "simple_identifier":
                            return sub.text.decode("utf-8", errors="replace")
            return first.text.decode("utf-8", errors="replace")

        # Scoped call (e.g., Rust path::func())
        if first.type in ("scoped_identifier", "qualified_name"):
            return first.text.decode("utf-8", errors="replace")

        # R namespace-qualified call: dplyr::filter()
        if first.type == "namespace_operator":
            return first.text.decode("utf-8", errors="replace")

        return None

    def _get_jsx_component_reference(self, node) -> Optional[tuple[Optional[str], str]]:
        """Extract ``(base_name, component_name)`` for a JSX element.

        ``base_name`` is set for member-style elements such as
        ``<UI.MarkdownMsg />`` and ``None`` for plain component tags such as
        ``<MarkdownMsg />``.
        """
        for child in node.children:
            if child.type == "identifier":
                name = child.text.decode("utf-8", errors="replace")
                if self._looks_like_component_name(name):
                    return (None, name)
                return None
            if child.type == "member_expression":
                base_name = self._get_member_expression_root_name(child)
                component_name = None
                for sub in reversed(child.children):
                    if sub.type in ("identifier", "property_identifier"):
                        component_name = sub.text.decode("utf-8", errors="replace")
                        break
                if component_name and self._looks_like_component_name(component_name):
                    return (base_name, component_name)
                for sub in reversed(child.children):
                    if sub.type in ("identifier", "property_identifier"):
                        name = sub.text.decode("utf-8", errors="replace")
                        if self._looks_like_component_name(name):
                            return (None, name)
                        return None
                text = child.text.decode("utf-8", errors="replace")
                tail = text.split(".")[-1]
                if self._looks_like_component_name(tail):
                    return (None, tail)
                return None
        return None

    def _get_member_expression_root_name(self, node) -> Optional[str]:
        """Return the leftmost identifier for a nested member expression."""
        for child in node.children:
            if child.type == "identifier":
                return child.text.decode("utf-8", errors="replace")
            if child.type == "member_expression":
                return self._get_member_expression_root_name(child)
        return None

    @staticmethod
    def _looks_like_component_name(name: str) -> bool:
        """Return True for JSX names that look like user components."""
        return bool(name) and name[0].isupper()

    # Modifier suffixes used in JS/TS test runners
    _TEST_MODIFIER_SUFFIXES = frozenset({
        "only", "skip", "each", "todo", "concurrent", "failing",
    })

    def _get_base_call_name(self, node, source: bytes) -> Optional[str]:
        """Return the base object name for member-expression calls like describe.only()."""
        if not node.children:
            return None
        first = node.children[0]
        if first.type != "member_expression":
            return None
        rightmost: Optional[str] = None
        for child in reversed(first.children):
            if child.type in ("identifier", "property_identifier"):
                rightmost = child.text.decode("utf-8", errors="replace")
                break
        if rightmost not in self._TEST_MODIFIER_SUFFIXES:
            return None
        for child in first.children:
            if child.type == "identifier":
                return child.text.decode("utf-8", errors="replace")
            if child.type == "member_expression":
                for inner in child.children:
                    if inner.type == "identifier":
                        return inner.text.decode("utf-8", errors="replace")
        return None

    # ------------------------------------------------------------------
    # R-specific helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _r_call_func_name(call_node) -> Optional[str]:
        """Extract the function name from an R call node."""
        for child in call_node.children:
            if child.type in ("identifier", "namespace_operator"):
                return child.text.decode("utf-8", errors="replace")
        return None

    @staticmethod
    def _r_first_string_arg(call_node) -> Optional[str]:
        """Extract the first string argument value from an R call node."""
        for child in call_node.children:
            if child.type == "arguments":
                for arg in child.children:
                    if arg.type == "argument":
                        for sub in arg.children:
                            if sub.type == "string":
                                for sc in sub.children:
                                    if sc.type == "string_content":
                                        return sc.text.decode("utf-8", errors="replace")
                break
        return None

    @staticmethod
    def _r_iter_args(call_node):
        """Yield (name_str, value_node) pairs from an R call's arguments."""
        for child in call_node.children:
            if child.type != "arguments":
                continue
            for arg in child.children:
                if arg.type != "argument":
                    continue
                has_eq = any(sub.type == "=" for sub in arg.children)
                if has_eq:
                    name = None
                    value = None
                    for sub in arg.children:
                        if sub.type == "identifier" and name is None:
                            name = sub.text.decode("utf-8", errors="replace")
                        elif sub.type not in ("=", ","):
                            value = sub
                    yield (name, value)
                else:
                    for sub in arg.children:
                        if sub.type not in (",",):
                            yield (None, sub)
                            break
            break

    @classmethod
    def _r_find_named_arg(cls, call_node, arg_name: str):
        """Find a named argument's value node in an R call."""
        for name, value in cls._r_iter_args(call_node):
            if name == arg_name:
                return value
        return None

    # ------------------------------------------------------------------
    # R-specific handlers
    # ------------------------------------------------------------------

    def _handle_r_binary_operator(
        self, node, source: bytes, language: str, file_path: str,
        nodes: list[NodeInfo], edges: list[EdgeInfo],
        enclosing_class: Optional[str], enclosing_func: Optional[str],
        import_map: Optional[dict[str, str]],
        defined_names: Optional[set[str]],
    ) -> bool:
        """Handle R binary_operator nodes: name <- function(...) { ... }."""
        children = node.children
        if len(children) < 3:
            return False

        left, op, right = children[0], children[1], children[2]
        if op.type not in ("<-", "="):
            return False

        if right.type == "function_definition" and left.type == "identifier":
            name = left.text.decode("utf-8", errors="replace")
            is_test = _is_test_function(name, file_path)
            kind = "Test" if is_test else "Function"
            qualified = self._qualify(name, file_path, enclosing_class)
            params = self._get_params(right, language, source)

            nodes.append(NodeInfo(
                kind=kind,
                name=name,
                file_path=file_path,
                line_start=right.start_point[0] + 1,
                line_end=right.end_point[0] + 1,
                language=language,
                parent_name=enclosing_class,
                params=params,
                is_test=is_test,
            ))

            container = (
                self._qualify(enclosing_class, file_path, None)
                if enclosing_class else file_path
            )
            edges.append(EdgeInfo(
                kind="CONTAINS",
                source=container,
                target=qualified,
                file_path=file_path,
                line=right.start_point[0] + 1,
            ))

            self._extract_from_tree(
                right, source, language, file_path, nodes, edges,
                enclosing_class=enclosing_class, enclosing_func=name,
                import_map=import_map, defined_names=defined_names,
            )
            return True

        if right.type == "call" and left.type == "identifier":
            call_func = self._r_call_func_name(right)
            if call_func in ("setRefClass", "setClass", "setGeneric"):
                assign_name = left.text.decode("utf-8", errors="replace")
                return self._handle_r_class_call(
                    right, source, language, file_path, nodes, edges,
                    enclosing_class, enclosing_func,
                    import_map, defined_names,
                    assign_name=assign_name,
                )

        return False

    def _handle_r_call(
        self, node, source: bytes, language: str, file_path: str,
        nodes: list[NodeInfo], edges: list[EdgeInfo],
        enclosing_class: Optional[str], enclosing_func: Optional[str],
        import_map: Optional[dict[str, str]],
        defined_names: Optional[set[str]],
    ) -> bool:
        """Handle R call nodes for imports and class definitions."""
        func_name = self._r_call_func_name(node)
        if not func_name:
            return False

        if func_name in ("library", "require", "source"):
            imports = self._extract_import(node, language, source)
            for imp_target in imports:
                edges.append(EdgeInfo(
                    kind="IMPORTS_FROM",
                    source=file_path,
                    target=imp_target,
                    file_path=file_path,
                    line=node.start_point[0] + 1,
                ))
            return True

        if func_name in ("setRefClass", "setClass", "setGeneric"):
            return self._handle_r_class_call(
                node, source, language, file_path, nodes, edges,
                enclosing_class, enclosing_func,
                import_map, defined_names,
            )

        if enclosing_func:
            call_name = self._get_call_name(node, language, source)
            if call_name:
                caller = self._qualify(enclosing_func, file_path, enclosing_class)
                target = self._resolve_call_target(
                    call_name, file_path, language,
                    import_map or {}, defined_names or set(),
                )
                edges.append(EdgeInfo(
                    kind="CALLS",
                    source=caller,
                    target=target,
                    file_path=file_path,
                    line=node.start_point[0] + 1,
                ))

        self._extract_from_tree(
            node, source, language, file_path, nodes, edges,
            enclosing_class=enclosing_class, enclosing_func=enclosing_func,
            import_map=import_map, defined_names=defined_names,
        )
        return True

    def _handle_r_class_call(
        self, node, source: bytes, language: str, file_path: str,
        nodes: list[NodeInfo], edges: list[EdgeInfo],
        enclosing_class: Optional[str], enclosing_func: Optional[str],
        import_map: Optional[dict[str, str]],
        defined_names: Optional[set[str]],
        assign_name: Optional[str] = None,
    ) -> bool:
        """Handle setClass/setRefClass/setGeneric calls -> Class nodes."""
        class_name = self._r_first_string_arg(node) or assign_name
        if not class_name:
            return False

        qualified = self._qualify(class_name, file_path, enclosing_class)
        nodes.append(NodeInfo(
            kind="Class",
            name=class_name,
            file_path=file_path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            language=language,
            parent_name=enclosing_class,
        ))
        edges.append(EdgeInfo(
            kind="CONTAINS",
            source=file_path,
            target=qualified,
            file_path=file_path,
            line=node.start_point[0] + 1,
        ))

        methods_list = self._r_find_named_arg(node, "methods")
        if methods_list is not None:
            self._extract_r_methods(
                methods_list, source, language, file_path,
                nodes, edges, class_name,
                import_map, defined_names,
            )

        return True

    def _extract_r_methods(
        self, list_call, source: bytes, language: str, file_path: str,
        nodes: list[NodeInfo], edges: list[EdgeInfo],
        class_name: str,
        import_map: Optional[dict[str, str]],
        defined_names: Optional[set[str]],
    ) -> None:
        """Extract methods from a setRefClass methods = list(...) call."""
        for method_name, func_def in self._r_iter_args(list_call):
            if not method_name or func_def is None:
                continue
            if func_def.type != "function_definition":
                continue

            qualified = self._qualify(method_name, file_path, class_name)
            params = self._get_params(func_def, language, source)
            nodes.append(NodeInfo(
                kind="Function",
                name=method_name,
                file_path=file_path,
                line_start=func_def.start_point[0] + 1,
                line_end=func_def.end_point[0] + 1,
                language=language,
                parent_name=class_name,
                params=params,
            ))
            edges.append(EdgeInfo(
                kind="CONTAINS",
                source=self._qualify(class_name, file_path, None),
                target=qualified,
                file_path=file_path,
                line=func_def.start_point[0] + 1,
            ))
            self._extract_from_tree(
                func_def, source, language, file_path, nodes, edges,
                enclosing_class=class_name,
                enclosing_func=method_name,
                import_map=import_map,
                defined_names=defined_names,
            )
