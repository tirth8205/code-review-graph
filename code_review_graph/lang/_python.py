"""Python language handler."""

from __future__ import annotations

from pathlib import Path

from ._base import BaseLanguageHandler


class PythonHandler(BaseLanguageHandler):
    language = "python"
    class_types = ["class_definition"]
    function_types = ["function_definition"]
    import_types = ["import_statement", "import_from_statement"]
    call_types = ["call"]
    builtin_names = frozenset({
        "len", "str", "int", "float", "bool", "list", "dict", "set", "tuple",
        "print", "range", "enumerate", "zip", "map", "filter", "sorted",
        "reversed", "isinstance", "issubclass", "type", "id", "hash",
        "hasattr", "getattr", "setattr", "delattr", "callable",
        "repr", "abs", "min", "max", "sum", "round", "pow", "divmod",
        "iter", "next", "open", "super", "property", "staticmethod",
        "classmethod", "vars", "dir", "help", "input", "format",
        "bytes", "bytearray", "memoryview", "frozenset", "complex",
        "chr", "ord", "hex", "oct", "bin", "any", "all",
    })

    def get_bases(self, node, source: bytes) -> list[str]:
        bases = []
        for child in node.children:
            if child.type == "argument_list":
                for arg in child.children:
                    if arg.type in ("identifier", "attribute"):
                        bases.append(arg.text.decode("utf-8", errors="replace"))
        return bases

    def extract_import_targets(self, node, source: bytes) -> list[str]:
        imports = []
        if node.type == "import_from_statement":
            for child in node.children:
                if child.type == "dotted_name":
                    imports.append(child.text.decode("utf-8", errors="replace"))
                    break
        else:
            for child in node.children:
                if child.type == "dotted_name":
                    imports.append(child.text.decode("utf-8", errors="replace"))
        return imports

    def collect_import_names(
        self, node, file_path: str, import_map: dict[str, str],
    ) -> bool:
        if node.type == "import_from_statement":
            # from X.Y import A, B -> {A: X.Y, B: X.Y}
            module = None
            seen_import_keyword = False
            for child in node.children:
                if child.type == "dotted_name" and not seen_import_keyword:
                    module = child.text.decode("utf-8", errors="replace")
                elif child.type == "import":
                    seen_import_keyword = True
                elif seen_import_keyword and module:
                    if child.type in ("identifier", "dotted_name"):
                        name = child.text.decode("utf-8", errors="replace")
                        import_map[name] = module
                    elif child.type == "aliased_import":
                        # from X import A as B -> {B: X}
                        names = [
                            sub.text.decode("utf-8", errors="replace")
                            for sub in child.children
                            if sub.type in ("identifier", "dotted_name")
                        ]
                        if names:
                            import_map[names[-1]] = module
        elif node.type == "import_statement":
            # import json -> {json: json}
            # import os.path -> {os: os.path}
            # import X as Y -> {Y: X}
            for child in node.children:
                if child.type in ("dotted_name", "identifier"):
                    mod = child.text.decode("utf-8", errors="replace")
                    top_level = mod.split(".")[0]
                    import_map[top_level] = mod
                elif child.type == "aliased_import":
                    names = [
                        sub.text.decode("utf-8", errors="replace")
                        for sub in child.children
                        if sub.type in ("identifier", "dotted_name")
                    ]
                    if len(names) >= 2:
                        import_map[names[-1]] = names[0]
        else:
            return False
        return True

    def resolve_module(self, module: str, caller_file: str) -> str | None:
        caller_dir = Path(caller_file).parent
        rel_path = module.replace(".", "/")
        candidates = [rel_path + ".py", rel_path + "/__init__.py"]
        current = caller_dir
        while True:
            for candidate in candidates:
                target = current / candidate
                if target.is_file():
                    return str(target.resolve())
            if current == current.parent:
                break
            current = current.parent
        return None
