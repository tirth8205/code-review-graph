"""Lua language handler."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ..parser import EdgeInfo, NodeInfo, _is_test_function
from ._base import BaseLanguageHandler

if TYPE_CHECKING:
    from ..parser import CodeParser


class LuaHandler(BaseLanguageHandler):
    language = "lua"
    class_types: list[str] = []  # Lua has no class keyword; table-based OOP
    function_types = ["function_declaration"]
    import_types: list[str] = []  # require() handled via extract_constructs
    call_types = ["function_call"]

    def get_name(self, node, kind: str) -> str | None:
        # function_declaration names may be dot_index_expression or
        # method_index_expression (e.g. function Animal.new() / Animal:speak()).
        # Return only the method name; the table name is used as parent_name
        # in extract_constructs.
        if node.type == "function_declaration":
            for child in node.children:
                if child.type in ("dot_index_expression", "method_index_expression"):
                    for sub in reversed(child.children):
                        if sub.type == "identifier":
                            return sub.text.decode("utf-8", errors="replace")
                    return None
        return NotImplemented

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
        """Handle Lua-specific AST constructs.

        Handles:
        - variable_declaration with require() -> IMPORTS_FROM edge
        - variable_declaration with function_definition -> named Function node
        - function_declaration with dot/method name -> Function with table parent
        - top-level require() call -> IMPORTS_FROM edge
        """
        if node_type == "variable_declaration":
            return self._handle_variable_declaration(
                child, source, parser, file_path, nodes, edges,
                enclosing_class, enclosing_func,
                import_map, defined_names, depth,
            )

        if node_type == "function_declaration":
            return self._handle_table_function(
                child, source, parser, file_path, nodes, edges,
                enclosing_class, enclosing_func,
                import_map, defined_names, depth,
            )

        # Top-level require() not wrapped in variable_declaration
        if node_type == "function_call" and not enclosing_func:
            req_target = self._get_require_target(child)
            if req_target is not None:
                resolved = parser._resolve_module_to_file(
                    req_target, file_path, self.language,
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

    # ------------------------------------------------------------------
    # Lua-specific helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_require_target(call_node) -> Optional[str]:
        """Extract the module path from a Lua require() call.

        Returns the string argument or None if this is not a require() call.
        """
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
                        for sub in arg.children:
                            if sub.type == "string_content":
                                return sub.text.decode(
                                    "utf-8", errors="replace",
                                )
                        raw = arg.text.decode("utf-8", errors="replace")
                        return raw.strip("'\"")
        return None

    def _handle_variable_declaration(
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
        depth: int,
    ) -> bool:
        """Handle Lua variable declarations that contain require() or
        anonymous function definitions.

        ``local json = require("json")``  -> IMPORTS_FROM edge
        ``local fn = function(x) ... end`` -> Function node named "fn"
        """
        language = self.language

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
                req_target = self._get_require_target(expr)
                if req_target is not None:
                    resolved = parser._resolve_module_to_file(
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
                qualified = parser._qualify(var_name, file_path, enclosing_class)
                params = parser._get_params(expr, language, source)

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
                    expr, source, language, file_path, nodes, edges,
                    enclosing_class=enclosing_class,
                    enclosing_func=var_name,
                    import_map=import_map,
                    defined_names=defined_names,
                    _depth=depth + 1,
                )
                return True

        return False

    def _handle_table_function(
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
        depth: int,
    ) -> bool:
        """Handle Lua function declarations with table-qualified names.

        ``function Animal.new(name)``  -> Function "new", parent "Animal"
        ``function Animal:speak()``    -> Function "speak", parent "Animal"

        Plain ``function foo()`` is NOT handled here (returns False).
        """
        language = self.language
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
        qualified = parser._qualify(method_name, file_path, table_name)
        params = parser._get_params(child, language, source)

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
        container = parser._qualify(table_name, file_path, None)
        edges.append(EdgeInfo(
            kind="CONTAINS",
            source=container,
            target=qualified,
            file_path=file_path,
            line=child.start_point[0] + 1,
        ))
        # Recurse into function body for calls
        parser._extract_from_tree(
            child, source, language, file_path, nodes, edges,
            enclosing_class=table_name,
            enclosing_func=method_name,
            import_map=import_map,
            defined_names=defined_names,
            _depth=depth + 1,
        )
        return True


class LuauHandler(LuaHandler):
    """Roblox Luau (.luau) handler -- reuses the Lua handler."""

    language = "luau"
    class_types = ["type_definition"]
