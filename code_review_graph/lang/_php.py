"""PHP language handler."""

from __future__ import annotations

from ._base import BaseLanguageHandler


class PhpHandler(BaseLanguageHandler):
    language = "php"
    class_types = ["class_declaration", "interface_declaration"]
    function_types = ["function_definition", "method_declaration"]
    import_types = ["namespace_use_declaration"]
    call_types = ["function_call_expression", "member_call_expression"]
