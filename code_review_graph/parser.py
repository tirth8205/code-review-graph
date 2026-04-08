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
    ".dart": "dart",
    ".r": "r",  # .lower() in detect_language handles .R → .r
    ".mjs": "javascript",
    ".astro": "typescript",
    ".pl": "perl",
    ".pm": "perl",
    ".t": "perl",
    ".xs": "c",  # Perl XS: parsed as C to capture functions/structs/includes
    ".lua": "lua",
    ".ipynb": "notebook",
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
    # Lua: require() is a function_call, handled via _extract_lua_constructs
    "lua": [],
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
        self._tsconfig_resolver = TsconfigResolver()

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

        # Jupyter notebooks: extract code cells and parse as Python
        if language == "notebook":
            return self._parse_notebook(path, source)

        # Databricks .py notebook exports
        if language == "python" and source.startswith(
            b"# Databricks notebook source\n",
        ):
            return self._parse_databricks_py_notebook(path, source)

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

            # --- Lua-specific constructs ---
            if language == "lua" and self._extract_lua_constructs(
                child, node_type, source, language, file_path,
                nodes, edges, enclosing_class, enclosing_func,
                import_map, defined_names, _depth,
            ):
                continue

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
        # Lua: function_declaration names may be dot_index_expression or
        # method_index_expression (e.g. function Animal.new() / Animal:speak()).
        # Return only the method name; the table name is used as parent_name
        # in _extract_lua_constructs.
        if language == "lua" and node.type == "function_declaration":
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

        # Lua: dot_index_expression (obj.method) and method_index_expression
        # (obj:method) — extract the rightmost identifier as the call name.
        if language == "lua" and first.type in (
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
