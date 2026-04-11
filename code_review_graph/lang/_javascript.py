"""JavaScript / TypeScript / TSX language handler."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ..parser import EdgeInfo, NodeInfo, _is_test_function
from ._base import BaseLanguageHandler

if TYPE_CHECKING:
    from ..parser import CodeParser


class _JsTsBase(BaseLanguageHandler):
    """Shared handler logic for JS, TS, and TSX."""

    class_types = ["class_declaration", "class"]
    function_types = ["function_declaration", "method_definition", "arrow_function"]
    import_types = ["import_statement"]
    # No builtin_names -- JS/TS builtins are not filtered

    _JS_FUNC_VALUE_TYPES = frozenset(
        {"arrow_function", "function_expression", "function"},
    )

    def get_bases(self, node, source: bytes) -> list[str]:
        bases = []
        for child in node.children:
            if child.type in ("extends_clause", "implements_clause"):
                for sub in child.children:
                    if sub.type in ("identifier", "type_identifier", "nested_identifier"):
                        bases.append(sub.text.decode("utf-8", errors="replace"))
        return bases

    def extract_import_targets(self, node, source: bytes) -> list[str]:
        imports = []
        for child in node.children:
            if child.type == "string":
                val = child.text.decode("utf-8", errors="replace").strip("'\"")
                imports.append(val)
        return imports

    def extract_constructs(
        self,
        child,
        node_type: str,
        parser: CodeParser,
        source: bytes,
        file_path: str,
        nodes: list[NodeInfo],
        edges: list[EdgeInfo],
        enclosing_class: str | None,
        enclosing_func: str | None,
        import_map: dict[str, str] | None,
        defined_names: set[str] | None,
        depth: int,
    ) -> bool:
        # --- Variable-assigned functions (const foo = () => {}) ---
        if node_type in ("lexical_declaration", "variable_declaration"):
            if self._extract_var_functions(
                child, source, parser, file_path, nodes, edges,
                enclosing_class, enclosing_func,
                import_map, defined_names, depth,
            ):
                return True

        # --- Class field arrow functions (handler = () => {}) ---
        if node_type == "public_field_definition":
            if self._extract_field_function(
                child, source, parser, file_path, nodes, edges,
                enclosing_class, enclosing_func,
                import_map, defined_names, depth,
            ):
                return True

        # --- Re-exports: export { X } from './mod', export * from './mod' ---
        if node_type == "export_statement":
            self._extract_reexport_edges(child, parser, file_path, edges)
            # Don't return True -- export_statement may also contain definitions
            return False

        return False

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    def _extract_var_functions(
        self,
        child,
        source: bytes,
        parser: CodeParser,
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
        language = self.language
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
            qualified = parser._qualify(var_name, file_path, enclosing_class)
            params = parser._get_params(func_node, language, source)
            ret_type = parser._get_return_type(func_node, language, source)

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
                parser._qualify(enclosing_class, file_path, None)
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
            parser._extract_from_tree(
                func_node, source, language, file_path, nodes, edges,
                enclosing_class=enclosing_class,
                enclosing_func=var_name,
                import_map=import_map,
                defined_names=defined_names,
                _depth=_depth + 1,
            )
            handled = True

        if not handled:
            # Not a function assignment -- let generic recursion handle it
            return False
        return True

    def _extract_field_function(
        self,
        child,
        source: bytes,
        parser: CodeParser,
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
        language = self.language
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
        qualified = parser._qualify(prop_name, file_path, enclosing_class)
        params = parser._get_params(func_node, language, source)

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
            parser._qualify(enclosing_class, file_path, None)
            if enclosing_class else file_path
        )
        edges.append(EdgeInfo(
            kind="CONTAINS",
            source=container,
            target=qualified,
            file_path=file_path,
            line=child.start_point[0] + 1,
        ))

        parser._extract_from_tree(
            func_node, source, language, file_path, nodes, edges,
            enclosing_class=enclosing_class,
            enclosing_func=prop_name,
            import_map=import_map,
            defined_names=defined_names,
            _depth=_depth + 1,
        )
        return True

    def _extract_reexport_edges(
        self,
        node,
        parser: CodeParser,
        file_path: str,
        edges: list[EdgeInfo],
    ) -> None:
        """Emit IMPORTS_FROM edges for JS/TS re-exports with ``from`` clause."""
        language = self.language
        # Must have a 'from' string
        module = None
        for child in node.children:
            if child.type == "string":
                module = child.text.decode("utf-8", errors="replace").strip("'\"")
        if not module:
            return
        resolved = parser._resolve_module_to_file(module, file_path, language)
        target = resolved if resolved else module
        # File-level IMPORTS_FROM
        edges.append(EdgeInfo(
            kind="IMPORTS_FROM",
            source=file_path,
            target=target,
            file_path=file_path,
            line=node.start_point[0] + 1,
        ))
        # Per-symbol edges for named re-exports
        if resolved:
            for child in node.children:
                if child.type == "export_clause":
                    for spec in child.children:
                        if spec.type == "export_specifier":
                            names = [
                                s.text.decode("utf-8", errors="replace")
                                for s in spec.children
                                if s.type == "identifier"
                            ]
                            if names:
                                edges.append(EdgeInfo(
                                    kind="IMPORTS_FROM",
                                    source=file_path,
                                    target=f"{resolved}::{names[0]}",
                                    file_path=file_path,
                                    line=node.start_point[0] + 1,
                                ))


class JavaScriptHandler(_JsTsBase):
    language = "javascript"
    call_types = [
        "call_expression", "new_expression",
    ]


class TypeScriptHandler(_JsTsBase):
    language = "typescript"
    call_types = ["call_expression", "new_expression"]


class TsxHandler(_JsTsBase):
    language = "tsx"
    call_types = [
        "call_expression", "new_expression",
    ]
