"""Solidity language handler."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..parser import EdgeInfo, NodeInfo
from ._base import BaseLanguageHandler

if TYPE_CHECKING:
    from ..parser import CodeParser


class SolidityHandler(BaseLanguageHandler):
    language = "solidity"
    class_types = [
        "contract_declaration", "interface_declaration", "library_declaration",
        "struct_declaration", "enum_declaration", "error_declaration",
        "user_defined_type_definition",
    ]
    # Events and modifiers use kind="Function" because the graph schema has no
    # dedicated kind for them.  State variables are also modeled as Function
    # nodes (public ones auto-generate getters).
    function_types = [
        "function_definition", "constructor_definition", "modifier_definition",
        "event_definition", "fallback_receive_definition",
    ]
    import_types = ["import_directive"]
    call_types = ["call_expression"]

    def get_name(self, node, kind: str) -> str | None:
        if node.type == "constructor_definition":
            return "constructor"
        if node.type == "fallback_receive_definition":
            for child in node.children:
                if child.type in ("receive", "fallback"):
                    return child.text.decode("utf-8", errors="replace")
        return NotImplemented

    def extract_import_targets(self, node, source: bytes) -> list[str]:
        imports = []
        for child in node.children:
            if child.type == "string":
                val = child.text.decode("utf-8", errors="replace").strip('"')
                if val:
                    imports.append(val)
        return imports

    def get_bases(self, node, source: bytes) -> list[str]:
        bases = []
        for child in node.children:
            if child.type == "inheritance_specifier":
                for sub in child.children:
                    if sub.type == "user_defined_type":
                        for ident in sub.children:
                            if ident.type == "identifier":
                                bases.append(
                                    ident.text.decode("utf-8", errors="replace"),
                                )
        return bases

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
        # Emit statements: emit EventName(...) -> CALLS edge
        if node_type == "emit_statement" and enclosing_func:
            for sub in child.children:
                if sub.type == "expression":
                    for ident in sub.children:
                        if ident.type == "identifier":
                            caller = parser._qualify(
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
                qualified = parser._qualify(
                    var_name, file_path, enclosing_class,
                )
                nodes.append(NodeInfo(
                    kind="Function",
                    name=var_name,
                    file_path=file_path,
                    line_start=child.start_point[0] + 1,
                    line_end=child.end_point[0] + 1,
                    language=self.language,
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
                    source=parser._qualify(
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
                qualified = parser._qualify(
                    var_name, file_path, enclosing_class,
                )
                nodes.append(NodeInfo(
                    kind="Function",
                    name=var_name,
                    file_path=file_path,
                    line_start=child.start_point[0] + 1,
                    line_end=child.end_point[0] + 1,
                    language=self.language,
                    parent_name=enclosing_class,
                    return_type=var_type,
                    extra={"solidity_kind": "constant"},
                ))
                container = (
                    parser._qualify(enclosing_class, file_path, None)
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
                    parser._qualify(
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
