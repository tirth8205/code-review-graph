"""C / C++ language handlers."""

from __future__ import annotations

from ._base import BaseLanguageHandler


class _CBase(BaseLanguageHandler):
    """Shared handler logic for C and C++."""

    import_types = ["preproc_include"]
    call_types = ["call_expression"]

    def extract_import_targets(self, node, source: bytes) -> list[str]:
        imports = []
        for child in node.children:
            if child.type in ("system_lib_string", "string_literal"):
                val = child.text.decode("utf-8", errors="replace").strip("<>\"")
                imports.append(val)
        return imports


class CHandler(_CBase):
    language = "c"
    class_types = ["struct_specifier", "type_definition"]
    function_types = ["function_definition"]


class CppHandler(_CBase):
    language = "cpp"
    class_types = ["class_specifier", "struct_specifier"]
    function_types = ["function_definition"]

    def get_bases(self, node, source: bytes) -> list[str]:
        bases = []
        for child in node.children:
            if child.type == "base_class_clause":
                for sub in child.children:
                    if sub.type == "type_identifier":
                        bases.append(sub.text.decode("utf-8", errors="replace"))
        return bases
