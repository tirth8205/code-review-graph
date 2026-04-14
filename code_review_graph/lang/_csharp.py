"""C# language handler."""

from __future__ import annotations

from ._base import BaseLanguageHandler


class CSharpHandler(BaseLanguageHandler):
    language = "csharp"
    class_types = [
        "class_declaration", "interface_declaration",
        "enum_declaration", "struct_declaration",
    ]
    function_types = ["method_declaration", "constructor_declaration"]
    import_types = ["using_directive"]
    call_types = ["invocation_expression", "object_creation_expression"]

    def extract_import_targets(self, node, source: bytes) -> list[str]:
        text = node.text.decode("utf-8", errors="replace").strip()
        parts = text.split()
        if len(parts) >= 2:
            return [parts[-1].rstrip(";")]
        return []

    def get_bases(self, node, source: bytes) -> list[str]:
        bases = []
        for child in node.children:
            if child.type in (
                "superclass", "super_interfaces", "extends_type",
                "implements_type", "type_identifier", "supertype",
            ):
                bases.append(child.text.decode("utf-8", errors="replace"))
        return bases
