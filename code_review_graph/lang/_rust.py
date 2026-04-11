"""Rust language handler."""

from __future__ import annotations

from ._base import BaseLanguageHandler


class RustHandler(BaseLanguageHandler):
    language = "rust"
    class_types = ["struct_item", "enum_item", "impl_item"]
    function_types = ["function_item"]
    import_types = ["use_declaration"]
    call_types = ["call_expression", "macro_invocation"]
    builtin_names = frozenset({
        "println", "eprintln", "format", "vec", "panic", "todo",
        "unimplemented", "unreachable", "assert", "assert_eq", "assert_ne",
        "dbg", "cfg",
    })

    def extract_import_targets(self, node, source: bytes) -> list[str]:
        text = node.text.decode("utf-8", errors="replace").strip()
        return [text.replace("use ", "").rstrip(";").strip()]
