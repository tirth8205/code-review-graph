"""Go language handler."""

from __future__ import annotations

from ._base import BaseLanguageHandler


class GoHandler(BaseLanguageHandler):
    language = "go"
    class_types = ["type_declaration"]
    function_types = ["function_declaration", "method_declaration"]
    import_types = ["import_declaration"]
    call_types = ["call_expression"]
    builtin_names = frozenset({
        "len", "cap", "make", "new", "delete", "append", "copy",
        "close", "panic", "recover", "print", "println",
    })

    def get_name(self, node, kind: str) -> str | None:
        # Go type_declaration wraps type_spec which holds the identifier
        if node.type == "type_declaration":
            for child in node.children:
                if child.type == "type_spec":
                    for sub in child.children:
                        if sub.type in ("identifier", "name", "type_identifier"):
                            return sub.text.decode("utf-8", errors="replace")
            return None
        return NotImplemented  # fall back to default for function_declaration etc.

    def get_bases(self, node, source: bytes) -> list[str]:
        # Embedded structs / interface composition
        # Embedded fields are field_declaration nodes with only a type_identifier
        # (no field name), e.g. `type Child struct { Parent }`
        bases = []
        for child in node.children:
            if child.type == "type_spec":
                for sub in child.children:
                    if sub.type in ("struct_type", "interface_type"):
                        for field_node in sub.children:
                            if field_node.type == "field_declaration_list":
                                for f in field_node.children:
                                    if f.type == "field_declaration":
                                        children = [
                                            c for c in f.children
                                            if c.type not in ("comment",)
                                        ]
                                        if (
                                            len(children) == 1
                                            and children[0].type == "type_identifier"
                                        ):
                                            bases.append(
                                                children[0].text.decode(
                                                    "utf-8", errors="replace",
                                                )
                                            )
        return bases

    def extract_import_targets(self, node, source: bytes) -> list[str]:
        imports = []
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
        return imports
