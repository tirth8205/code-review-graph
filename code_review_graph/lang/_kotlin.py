"""Kotlin language handler."""

from __future__ import annotations

from ._base import BaseLanguageHandler


class KotlinHandler(BaseLanguageHandler):
    language = "kotlin"
    class_types = ["class_declaration", "object_declaration"]
    function_types = ["function_declaration"]
    import_types = ["import_header"]
    call_types = ["call_expression"]

    def get_bases(self, node, source: bytes) -> list[str]:
        bases = []
        for child in node.children:
            if child.type in (
                "superclass", "super_interfaces", "extends_type",
                "implements_type", "type_identifier", "supertype",
                "delegation_specifier",
            ):
                bases.append(child.text.decode("utf-8", errors="replace"))
        return bases
