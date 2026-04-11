"""Perl language handler."""

from __future__ import annotations

from ._base import BaseLanguageHandler


class PerlHandler(BaseLanguageHandler):
    language = "perl"
    class_types = ["package_statement", "class_statement", "role_statement"]
    function_types = ["subroutine_declaration_statement", "method_declaration_statement"]
    import_types = ["use_statement", "require_expression"]
    call_types = [
        "function_call_expression", "method_call_expression",
        "ambiguous_function_call_expression",
    ]

    def get_name(self, node, kind: str) -> str | None:
        for child in node.children:
            if child.type == "bareword":
                return child.text.decode("utf-8", errors="replace")
            if child.type == "package" and child.text != b"package":
                return child.text.decode("utf-8", errors="replace")
        return NotImplemented
