"""Dart language handler."""

from __future__ import annotations

from typing import Optional

from ._base import BaseLanguageHandler


class DartHandler(BaseLanguageHandler):
    language = "dart"
    class_types = ["class_definition", "mixin_declaration", "enum_declaration"]
    # function_signature covers both top-level functions and class methods
    # (class methods appear as method_signature > function_signature pairs;
    # the parser recurses into method_signature generically and then matches
    # function_signature inside it).
    function_types = ["function_signature"]
    # import_or_export wraps library_import > import_specification > configurable_uri
    import_types = ["import_or_export"]
    call_types: list[str] = []  # Dart uses call_expression from fallback

    def get_name(self, node, kind: str) -> str | None:
        # function_signature has a return-type node before the identifier;
        # search only for 'identifier' to avoid returning the return type name.
        if node.type == "function_signature":
            for child in node.children:
                if child.type == "identifier":
                    return child.text.decode("utf-8", errors="replace")
            return None
        return NotImplemented

    def extract_import_targets(self, node, source: bytes) -> list[str]:
        val = self._find_string_literal(node)
        if val:
            return [val]
        return []

    @staticmethod
    def _find_string_literal(node) -> Optional[str]:
        if node.type == "string_literal":
            return node.text.decode("utf-8", errors="replace").strip("'\"")
        for child in node.children:
            result = DartHandler._find_string_literal(child)
            if result is not None:
                return result
        return None

    def get_bases(self, node, source: bytes) -> list[str]:
        bases = []
        for child in node.children:
            if child.type == "superclass":
                for sub in child.children:
                    if sub.type == "type_identifier":
                        bases.append(sub.text.decode("utf-8", errors="replace"))
                    elif sub.type == "mixins":
                        for m in sub.children:
                            if m.type == "type_identifier":
                                bases.append(
                                    m.text.decode("utf-8", errors="replace"),
                                )
            elif child.type == "interfaces":
                for sub in child.children:
                    if sub.type == "type_identifier":
                        bases.append(sub.text.decode("utf-8", errors="replace"))
        return bases
