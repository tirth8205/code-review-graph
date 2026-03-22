"""Tree-sitter based multi-language code parser.

Extracts structural nodes (classes, functions, imports, types) and edges
(calls, inheritance, contains) from source files.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import tree_sitter_language_pack as tslp

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
    kind: str  # CALLS, IMPORTS_FROM, INHERITS, IMPLEMENTS, CONTAINS, TESTED_BY, DEPENDS_ON
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
    "kotlin": ["import_header"],
    "swift": ["import_declaration"],
    "php": ["namespace_use_declaration"],
    "scala": ["import_declaration"],
    "solidity": ["import_directive"],
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
    "kotlin": ["call_expression"],
    "swift": ["call_expression"],
    "php": ["function_call_expression", "member_call_expression"],
    "scala": ["call_expression", "instance_expression", "generic_function"],
    "solidity": ["call_expression"],
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
]


def _is_test_file(path: str) -> bool:
    return any(p.search(path) for p in _TEST_FILE_PATTERNS)


def _is_test_function(name: str, file_path: str) -> bool:
    """A function is a test if its name matches test patterns or it lives
    in a test file and has a test-runner name (describe, it, test, etc.).
    """
    if any(p.search(name) for p in _TEST_PATTERNS):
        return True
    # In test files, treat common JS/TS test-runner wrappers as tests
    if _is_test_file(file_path) and name in (
        "describe", "it", "test", "beforeEach", "afterEach",
        "beforeAll", "afterAll",
    ):
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

    def _get_parser(self, language: str):  # type: ignore[arg-type]
        if language not in self._parsers:
            try:
                self._parsers[language] = tslp.get_parser(language)  # type: ignore[arg-type]
            except Exception:
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
            if edge.kind == "CALLS" and "::" not in edge.target:
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

            # --- Classes ---
            if node_type in class_types:
                name = self._get_name(child, language, "class")
                if name:
                    node = NodeInfo(
                        kind="Class",
                        name=name,
                        file_path=file_path,
                        line_start=child.start_point[0] + 1,
                        line_end=child.end_point[0] + 1,
                        language=language,
                        parent_name=enclosing_class,
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
                            source=self._qualify(name, file_path, enclosing_class),
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
                    continue

            # --- Functions ---
            if node_type in func_types:
                name = self._get_name(child, language, "function")
                if name:
                    is_test = _is_test_function(name, file_path)
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

                    # Solidity: modifier invocations on functions → CALLS edges
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
                    continue

            # --- Imports ---
            if node_type in import_types:
                imports = self._extract_import(child, language, source)
                for imp_target in imports:
                    edges.append(EdgeInfo(
                        kind="IMPORTS_FROM",
                        source=file_path,
                        target=imp_target,
                        file_path=file_path,
                        line=child.start_point[0] + 1,
                    ))
                continue

            # --- Calls ---
            if node_type in call_types:
                call_name = self._get_call_name(child, language, source)
                if call_name and enclosing_func:
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
                        line=child.start_point[0] + 1,
                    ))

            # --- Solidity-specific constructs ---
            if language == "solidity":
                # Emit statements: emit EventName(...) → CALLS edge
                if node_type == "emit_statement" and enclosing_func:
                    for sub in child.children:
                        if sub.type == "expression":
                            for ident in sub.children:
                                if ident.type == "identifier":
                                    caller = self._qualify(
                                        enclosing_func, file_path, enclosing_class,
                                    )
                                    edges.append(EdgeInfo(
                                        kind="CALLS",
                                        source=caller,
                                        target=ident.text.decode("utf-8", errors="replace"),
                                        file_path=file_path,
                                        line=child.start_point[0] + 1,
                                    ))

                # State variable declarations → Function nodes (public ones
                # auto-generate getters, and all are critical for reviews)
                if node_type == "state_variable_declaration" and enclosing_class:
                    var_name = None
                    var_visibility = None
                    var_mutability = None
                    var_type = None
                    for sub in child.children:
                        if sub.type == "identifier":
                            var_name = sub.text.decode("utf-8", errors="replace")
                        elif sub.type == "visibility":
                            var_visibility = sub.text.decode("utf-8", errors="replace")
                        elif sub.type == "type_name":
                            var_type = sub.text.decode("utf-8", errors="replace")
                        elif sub.type in ("constant", "immutable"):
                            var_mutability = sub.type
                    if var_name:
                        qualified = self._qualify(var_name, file_path, enclosing_class)
                        nodes.append(NodeInfo(
                            kind="Function",
                            name=var_name,
                            file_path=file_path,
                            line_start=child.start_point[0] + 1,
                            line_end=child.end_point[0] + 1,
                            language=language,
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
                        continue

                # File-level and contract-level constant declarations
                if node_type == "constant_variable_declaration":
                    var_name = None
                    var_type = None
                    for sub in child.children:
                        if sub.type == "identifier":
                            var_name = sub.text.decode("utf-8", errors="replace")
                        elif sub.type == "type_name":
                            var_type = sub.text.decode("utf-8", errors="replace")
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
                            language=language,
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
                        continue

                # Using directives: using LibName for Type → DEPENDS_ON edge
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
                            self._qualify(enclosing_class, file_path, None)
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
                    continue

            # Recurse for other node types
            self._extract_from_tree(
                child, source, language, file_path, nodes, edges,
                enclosing_class=enclosing_class, enclosing_func=enclosing_func,
                import_map=import_map, defined_names=defined_names,
                _depth=_depth + 1,
            )

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

            target_type = target.type

            # Collect defined function/class names
            if target_type in func_types or target_type in class_types:
                name = self._get_name(target, language,
                                      "class" if target_type in class_types else "function")
                if name:
                    defined_names.add(name)

            # Collect import mappings: imported_name → module_path
            if node_type in import_types:
                self._collect_import_names(child, language, source, import_map)

        return import_map, defined_names

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
            resolved = self._resolve_module_to_file(
                import_map[call_name], file_path, language,
            )
            if resolved:
                return self._qualify(call_name, resolved, None)
        return call_name

    def _qualify(self, name: str, file_path: str, enclosing_class: Optional[str]) -> str:
        """Create a qualified name: file_path::ClassName.name or file_path::name."""
        if enclosing_class:
            return f"{file_path}::{enclosing_class}.{name}"
        return f"{file_path}::{name}"

    def _get_name(self, node, language: str, kind: str) -> Optional[str]:
        """Extract the name from a class/function definition node."""
        # Solidity: constructor and receive/fallback have no identifier child
        if language == "solidity":
            if node.type == "constructor_definition":
                return "constructor"
            if node.type == "fallback_receive_definition":
                for child in node.children:
                    if child.type in ("receive", "fallback"):
                        return child.text.decode("utf-8", errors="replace")
        # For C/C++: function names are inside function_declarator/pointer_declarator
        # Check these first to avoid matching the return type_identifier
        if language in ("c", "cpp") and kind == "function":
            for child in node.children:
                if child.type in ("function_declarator", "pointer_declarator"):
                    result = self._get_name(child, language, kind)
                    if result:
                        return result
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

    def _get_params(self, node, language: str, source: bytes) -> Optional[str]:
        """Extract parameter list as a string."""
        for child in node.children:
            if child.type in ("parameters", "formal_parameters", "parameter_list"):
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
        elif language == "ruby":
            # require 'module' or require_relative 'path'
            if "require" in text:
                match = re.search(r"""['"](.*?)['"]""", text)
                if match:
                    imports.append(match.group(1))
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

        # Solidity wraps call targets in an 'expression' node – unwrap it
        if language == "solidity" and first.type == "expression" and first.children:
            first = first.children[0]

        # Simple call: func_name(args)
        if first.type == "identifier":
            return first.text.decode("utf-8", errors="replace")

        # Method call: obj.method(args)
        member_types = (
            "attribute", "member_expression",
            "field_expression", "selector_expression",
        )
        if first.type in member_types:
            # Get the rightmost identifier (the method name)
            for child in reversed(first.children):
                if child.type in (
                    "identifier", "property_identifier", "field_identifier",
                    "field_name",
                ):
                    return child.text.decode("utf-8", errors="replace")
            return first.text.decode("utf-8", errors="replace")

        # Scoped call (e.g., Rust path::func())
        if first.type in ("scoped_identifier", "qualified_name"):
            return first.text.decode("utf-8", errors="replace")

        return None
