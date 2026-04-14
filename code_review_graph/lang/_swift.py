"""Swift language handler."""

from __future__ import annotations

from ._base import BaseLanguageHandler


class SwiftHandler(BaseLanguageHandler):
    language = "swift"
    class_types = ["class_declaration", "struct_declaration", "protocol_declaration"]
    function_types = ["function_declaration"]
    import_types = ["import_declaration"]
    call_types = ["call_expression"]
