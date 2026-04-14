"""Ruby language handler."""

from __future__ import annotations

import re

from ._base import BaseLanguageHandler


class RubyHandler(BaseLanguageHandler):
    language = "ruby"
    class_types = ["class", "module"]
    function_types = ["method", "singleton_method"]
    import_types = ["call"]  # require / require_relative
    call_types = ["call", "method_call"]

    def extract_import_targets(self, node, source: bytes) -> list[str]:
        text = node.text.decode("utf-8", errors="replace").strip()
        if "require" in text:
            match = re.search(r"""['"](.*?)['"]""", text)
            if match:
                return [match.group(1)]
        return []
