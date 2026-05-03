"""Tests for the Tree-sitter parser module."""

import tempfile
from pathlib import Path

from code_review_graph.parser import CodeParser

FIXTURES = Path(__file__).parent / "fixtures"


class TestCodeParser:
    def setup_method(self):
        self.parser = CodeParser()

    def test_detect_language_python(self):
        assert self.parser.detect_language(Path("foo.py")) == "python"

    def test_detect_language_typescript(self):
        assert self.parser.detect_language(Path("foo.ts")) == "typescript"

    def test_detect_language_unknown(self):
        assert self.parser.detect_language(Path("foo.txt")) is None

    # --- Shebang detection for extension-less Unix scripts (#237) ---

    def _write_shebang_file(self, tmp_path: Path, name: str, content: str) -> Path:
        """Helper: write an extension-less file with ``content`` and return its path."""
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        return p

    def test_detect_shebang_bin_bash(self, tmp_path):
        p = self._write_shebang_file(
            tmp_path, "deploy", "#!/bin/bash\nfoo() { echo hi; }\n",
        )
        assert self.parser.detect_language(p) == "bash"

    def test_detect_shebang_bin_sh_routed_to_bash(self, tmp_path):
        """/bin/sh scripts are parsed through the bash grammar."""
        p = self._write_shebang_file(
            tmp_path, "install-hook", "#!/bin/sh\necho hello\n",
        )
        assert self.parser.detect_language(p) == "bash"

    def test_detect_shebang_env_bash(self, tmp_path):
        p = self._write_shebang_file(
            tmp_path, "runner", "#!/usr/bin/env bash\nfoo() { echo hi; }\n",
        )
        assert self.parser.detect_language(p) == "bash"

    def test_detect_shebang_env_python3(self, tmp_path):
        p = self._write_shebang_file(
            tmp_path, "myapp",
            "#!/usr/bin/env python3\ndef main():\n    pass\n",
        )
        assert self.parser.detect_language(p) == "python"

    def test_detect_shebang_direct_python(self, tmp_path):
        p = self._write_shebang_file(
            tmp_path, "tool", "#!/usr/bin/python3\nprint('hi')\n",
        )
        assert self.parser.detect_language(p) == "python"

    def test_detect_shebang_node(self, tmp_path):
        p = self._write_shebang_file(
            tmp_path, "cli", "#!/usr/bin/env node\nconsole.log(1);\n",
        )
        assert self.parser.detect_language(p) == "javascript"

    def test_detect_shebang_env_dash_s_flag(self, tmp_path):
        """``#!/usr/bin/env -S node --flag`` (Linux -S) resolves to the interpreter."""
        p = self._write_shebang_file(
            tmp_path, "esm-tool",
            "#!/usr/bin/env -S node --experimental-vm-modules\n"
            "console.log('esm');\n",
        )
        assert self.parser.detect_language(p) == "javascript"

    def test_detect_shebang_ruby(self, tmp_path):
        p = self._write_shebang_file(
            tmp_path, "rake-task", "#!/usr/bin/env ruby\nputs 1\n",
        )
        assert self.parser.detect_language(p) == "ruby"

    def test_detect_shebang_perl(self, tmp_path):
        p = self._write_shebang_file(
            tmp_path, "cgi-script", "#!/usr/bin/env perl\nprint 1;\n",
        )
        assert self.parser.detect_language(p) == "perl"

    def test_detect_shebang_with_trailing_flags(self, tmp_path):
        """``#!/bin/bash -e`` still maps to bash (flags ignored)."""
        p = self._write_shebang_file(
            tmp_path, "strict", "#!/bin/bash -e\nfoo() { echo hi; }\n",
        )
        assert self.parser.detect_language(p) == "bash"

    def test_detect_shebang_missing_returns_none(self, tmp_path):
        """Extension-less text files without a shebang return None, not bash."""
        p = self._write_shebang_file(
            tmp_path, "README", "# just a readme, no shebang\nsome content\n",
        )
        assert self.parser.detect_language(p) is None

    def test_detect_shebang_empty_file_returns_none(self, tmp_path):
        p = tmp_path / "EMPTY"
        p.write_bytes(b"")
        assert self.parser.detect_language(p) is None

    def test_detect_shebang_binary_content_returns_none(self, tmp_path):
        """A garbage-byte first line that happens not to start with ``#!``
        must not raise and must return None."""
        p = tmp_path / "binary-blob"
        p.write_bytes(b"\x00\x01\x02\x03 garbage bytes not a shebang\n")
        assert self.parser.detect_language(p) is None

    def test_detect_shebang_unknown_interpreter_returns_none(self, tmp_path):
        """A valid shebang to an interpreter we don't route is treated as
        'unknown language' — same as an unmapped extension."""
        p = self._write_shebang_file(
            tmp_path, "ocaml-script", "#!/usr/bin/env ocaml\nlet x = 1\n",
        )
        assert self.parser.detect_language(p) is None

    def test_detect_shebang_does_not_override_extension(self, tmp_path):
        """A file with a known extension must still use extension-based
        detection, even if its first line is a misleading shebang."""
        p = tmp_path / "script.py"
        p.write_text("#!/bin/bash\nprint('hi')\n", encoding="utf-8")
        # .py wins over the bash shebang — non-intuitive-looking content
        # in a .py file must not fool the detector.
        assert self.parser.detect_language(p) == "python"

    def test_parse_shebang_script_produces_function_nodes(self, tmp_path):
        """End-to-end regression: an extension-less bash script is not only
        detected but also fully parsed into structural nodes via parse_file.
        """
        script = (
            "#!/usr/bin/env bash\n"
            "greet() {\n"
            '    echo "hi $1"\n'
            "}\n"
            "main() {\n"
            "    greet world\n"
            "}\n"
            "main\n"
        )
        p = self._write_shebang_file(tmp_path, "deploy", script)

        nodes, edges = self.parser.parse_file(p)

        # We at least got the File node plus both functions.
        assert len(nodes) >= 3
        funcs = [n for n in nodes if n.kind == "Function"]
        func_names = {f.name for f in funcs}
        assert "greet" in func_names
        assert "main" in func_names
        for n in nodes:
            assert n.language == "bash"

    def test_parse_python_file(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_python.py")

        # Should have File node
        file_nodes = [n for n in nodes if n.kind == "File"]
        assert len(file_nodes) == 1

        # Should find classes
        classes = [n for n in nodes if n.kind == "Class"]
        class_names = {c.name for c in classes}
        assert "BaseService" in class_names
        assert "AuthService" in class_names

        # Should find functions
        funcs = [n for n in nodes if n.kind == "Function"]
        func_names = {f.name for f in funcs}
        assert "__init__" in func_names
        assert "authenticate" in func_names
        assert "create_auth_service" in func_names
        assert "process_request" in func_names

    def test_parse_python_edges(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_python.py")

        edge_kinds = {e.kind for e in edges}
        assert "CONTAINS" in edge_kinds
        assert "IMPORTS_FROM" in edge_kinds
        assert "CALLS" in edge_kinds

        # Should detect inheritance
        inherits = [e for e in edges if e.kind == "INHERITS"]
        assert len(inherits) >= 1
        assert any("AuthService" in e.source and "BaseService" in e.target for e in inherits)

    def test_parse_python_imports(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_python.py")
        imports = [e for e in edges if e.kind == "IMPORTS_FROM"]
        import_targets = {e.target for e in imports}
        assert "os" in import_targets
        assert "pathlib" in import_targets

    def test_parse_python_calls(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_python.py")
        calls = [e for e in edges if e.kind == "CALLS"]
        call_targets = {e.target for e in calls}
        # _resolve_call_targets qualifies same-file definitions
        assert any("_validate_token" in t for t in call_targets)
        assert any("authenticate" in t for t in call_targets)

    def test_parse_typescript_file(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_typescript.ts")

        classes = [n for n in nodes if n.kind == "Class"]
        class_names = {c.name for c in classes}
        assert "UserRepository" in class_names
        assert "UserService" in class_names

        funcs = [n for n in nodes if n.kind == "Function"]
        func_names = {f.name for f in funcs}
        assert "findById" in func_names or "handleGetUser" in func_names

    def test_parse_test_file(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "test_sample.py")

        # Test functions should be detected
        tests = [n for n in nodes if n.kind == "Test"]
        test_names = {t.name for t in tests}
        assert "test_authenticate_valid" in test_names
        assert "test_process_request_ok" in test_names

    def test_calls_edge_same_file_resolution(self):
        """Call targets defined in the same file should be qualified."""
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_python.py")
        calls = [e for e in edges if e.kind == "CALLS"]
        file_path = str(FIXTURES / "sample_python.py")

        # create_auth_service() calls AuthService() — a class defined in the same file
        auth_service_calls = [
            e for e in calls if e.target == f"{file_path}::AuthService"
        ]
        assert len(auth_service_calls) >= 1

    def test_calls_edge_cross_file_resolution(self):
        """Call targets imported from another file should resolve to that file's qualified name."""
        _, edges = self.parser.parse_file(FIXTURES / "caller_example.py")
        calls = [e for e in edges if e.kind == "CALLS"]

        sample_path = str((FIXTURES / "sample_python.py").resolve())
        # setup_and_run() calls create_auth_service(), imported from sample_python
        resolved_calls = [
            e for e in calls if e.target == f"{sample_path}::create_auth_service"
        ]
        assert len(resolved_calls) == 1

    def test_same_file_calls_resolved(self):
        """Same-file call targets should be resolved to qualified names."""
        _, edges = self.parser.parse_file(FIXTURES / "sample_python.py")
        calls = [e for e in edges if e.kind == "CALLS"]
        # _validate_token is defined in the same file, so it should be qualified
        resolved_calls = [e for e in calls if "_validate_token" in e.target and "::" in e.target]
        assert len(resolved_calls) >= 1

    def test_calls_edge_decorated_function_resolution(self):
        """Decorated functions should be in defined_names and resolvable as call targets."""
        _, edges = self.parser.parse_file(FIXTURES / "sample_python.py")
        calls = [e for e in edges if e.kind == "CALLS"]
        file_path = str(FIXTURES / "sample_python.py")

        # guarded_process() calls process_request() — both in the same file,
        # but guarded_process is wrapped in a decorated_definition node
        resolved = [e for e in calls if e.target == f"{file_path}::process_request"
                    and "guarded_process" in e.source]
        assert len(resolved) == 1

    def test_multiple_calls_to_same_function(self):
        """Multiple calls to the same function on different lines should each produce an edge."""
        _, edges = self.parser.parse_file(FIXTURES / "multi_call_example.py")
        calls = [e for e in edges if e.kind == "CALLS" and "_internal_request" in e.target]
        assert len(calls) == 2
        lines = {e.line for e in calls}
        assert len(lines) == 2  # distinct line numbers

    def test_module_scope_calls_attributed_to_file(self):
        """Module-scope calls (script glue, top-level code) emit CALLS edges
        attributed to the File node, so callees aren't flagged as dead by
        find_dead_code.

        Regression test: prior to this fix, _extract_calls dropped the edge
        entirely when enclosing_func was None, leaving notebooks, CLI scripts,
        and top-level entry points with zero outgoing CALLS edges.
        """
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(
                "def helper():\n"
                "    return 42\n"
                "\n"
                "# Module-scope call — no enclosing function\n"
                "result = helper()\n"
            )
            tmp = Path(f.name)

        try:
            _, edges = self.parser.parse_file(tmp)
            calls = [e for e in edges if e.kind == "CALLS"]
            module_scope_calls = [e for e in calls if e.source == str(tmp)]
            assert any(
                "helper" in e.target for e in module_scope_calls
            ), f"Expected module-scope CALLS edge to helper(); got: {[(e.source, e.target) for e in calls]}"
        finally:
            tmp.unlink()

    def test_module_scope_calls_in_notebook(self):
        """Notebook code cells are entirely module-scope — every call inside
        them should produce a CALLS edge attributed to the .ipynb File node."""
        import json

        notebook = {
            "cells": [
                {
                    "cell_type": "code",
                    "source": [
                        "from helper_module import do_work\n",
                        "do_work()\n",
                    ],
                },
            ],
            "metadata": {"language_info": {"name": "python"}},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ipynb", delete=False) as f:
            json.dump(notebook, f)
            tmp = Path(f.name)

        try:
            _, edges = self.parser.parse_file(tmp)
            calls = [e for e in edges if e.kind == "CALLS"]
            assert any(
                "do_work" in e.target and e.source == str(tmp) for e in calls
            ), f"Expected notebook CALLS edge to do_work(); got: {[(e.source, e.target) for e in calls]}"
        finally:
            tmp.unlink()

    def test_parse_nonexistent_file(self):
        nodes, edges = self.parser.parse_file(Path("/nonexistent/file.py"))
        assert nodes == []
        assert edges == []

    def test_parse_unsupported_extension(self):
        nodes, edges = self.parser.parse_file(Path("readme.txt"))
        assert nodes == []
        assert edges == []

    def test_tested_by_edges_generated(self):
        """Test files should produce TESTED_BY edges when tests call production code."""
        nodes, edges = self.parser.parse_file(FIXTURES / "test_sample.py")
        tested_by = [e for e in edges if e.kind == "TESTED_BY"]
        assert len(tested_by) >= 1

    def test_recursion_depth_guard(self):
        """Parser should not crash on deeply nested code."""
        # Generate Python code with many nested functions (> _MAX_AST_DEPTH)
        depth = 200
        lines = []
        for i in range(depth):
            indent = "    " * i
            lines.append(f"{indent}def func_{i}():")
        lines.append("    " * depth + "pass")
        source = "\n".join(lines).encode("utf-8")

        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(source)
            f.flush()
            path = Path(f.name)

        try:
            # Should NOT raise RecursionError
            nodes, edges = self.parser.parse_bytes(path, source)
            # We should get some functions but not all 200 due to depth cap
            funcs = [n for n in nodes if n.kind == "Function"]
            assert len(funcs) > 0
            assert len(funcs) < depth  # capped by _MAX_AST_DEPTH
        finally:
            path.unlink(missing_ok=True)

    def test_module_file_cache_bounded(self):
        """Module file cache should not grow unboundedly."""
        parser = CodeParser()
        # Fill the cache up to the limit
        for i in range(parser._MODULE_CACHE_MAX + 100):
            parser._module_file_cache[f"key_{i}"] = f"/path/to/mod_{i}.py"
        # Trigger a resolve which should clear the cache
        parser._resolve_module_to_file("os", "/test/file.py", "python")
        assert len(parser._module_file_cache) <= parser._MODULE_CACHE_MAX

    # --- Vue SFC tests ---

    def test_detect_language_vue(self):
        assert self.parser.detect_language(Path("App.vue")) == "vue"

    def test_parse_vue_file(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_vue.vue")

        # Should have File node with language=vue
        file_nodes = [n for n in nodes if n.kind == "File"]
        assert len(file_nodes) == 1
        assert file_nodes[0].language == "vue"

        # Should find functions from <script setup>
        funcs = [n for n in nodes if n.kind == "Function"]
        func_names = {f.name for f in funcs}
        assert "increment" in func_names
        assert "onSelectUser" in func_names
        assert "fetchUsers" in func_names

    def test_parse_vue_imports(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_vue.vue")
        imports = [e for e in edges if e.kind == "IMPORTS_FROM"]
        import_targets = {e.target for e in imports}
        assert "vue" in import_targets
        assert "./UserList.vue" in import_targets

    def test_parse_vue_calls(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_vue.vue")
        calls = [e for e in edges if e.kind == "CALLS"]
        call_targets = {e.target for e in calls}
        assert "log" in call_targets or "console.log" in call_targets or any(
            "log" in t for t in call_targets
        )

    def test_parse_vue_contains_edges(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_vue.vue")
        contains = [e for e in edges if e.kind == "CONTAINS"]
        assert len(contains) >= 1

    def test_parse_vue_line_numbers_offset(self):
        """Line numbers should be offset to reflect position in the .vue file."""
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_vue.vue")
        funcs = [n for n in nodes if n.kind == "Function" and n.name == "increment"]
        assert len(funcs) == 1
        # increment() is on line 22 of the .vue file (inside <script setup> starting at line 9)
        assert funcs[0].line_start > 9

    def test_parse_vue_nodes_have_vue_language(self):
        """All extracted nodes from Vue SFC should have language='vue'."""
        nodes, _ = self.parser.parse_file(FIXTURES / "sample_vue.vue")
        for node in nodes:
            assert node.language == "vue"

    def test_parse_vue_empty_script(self):
        """Vue file with no script block should still produce a File node."""
        source = b"<template><div>Hello</div></template>\n"
        path = Path("empty_script.vue")
        nodes, edges = self.parser.parse_bytes(path, source)
        assert len(nodes) == 1
        assert nodes[0].kind == "File"

    def test_parse_vue_js_default(self):
        """Vue file without lang attr should parse script as JavaScript."""
        source = (
            b"<script>\n"
            b"export default {\n"
            b"  methods: {\n"
            b"    greet() { return 'hi' }\n"
            b"  }\n"
            b"}\n"
            b"</script>\n"
        )
        path = Path("js_default.vue")
        nodes, edges = self.parser.parse_bytes(path, source)
        funcs = [n for n in nodes if n.kind == "Function"]
        func_names = {f.name for f in funcs}
        assert "greet" in func_names

    # --- Dart tests ---

    def test_detect_language_dart(self):
        assert self.parser.detect_language(Path("main.dart")) == "dart"

    def test_parse_dart_file(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample.dart")

        file_nodes = [n for n in nodes if n.kind == "File"]
        assert len(file_nodes) == 1
        assert file_nodes[0].language == "dart"

        classes = [n for n in nodes if n.kind == "Class"]
        class_names = {c.name for c in classes}
        assert "Animal" in class_names
        assert "Dog" in class_names
        assert "SwimmingMixin" in class_names
        assert "PetType" in class_names

        funcs = [n for n in nodes if n.kind == "Function"]
        func_names = {f.name for f in funcs}
        assert "speak" in func_names
        assert "fetch" in func_names
        assert "_run" in func_names
        assert "create" in func_names
        assert "createDog" in func_names
        assert "swim" in func_names

    def test_parse_dart_imports(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample.dart")
        imports = [e for e in edges if e.kind == "IMPORTS_FROM"]
        import_targets = {e.target for e in imports}
        assert "dart:async" in import_targets
        assert "package:flutter/material.dart" in import_targets

    def test_parse_dart_inheritance(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample.dart")
        inherits = [e for e in edges if e.kind == "INHERITS"]
        assert any("Dog" in e.source and "Animal" in e.target for e in inherits)
        assert any("Dog" in e.source and "SwimmingMixin" in e.target for e in inherits)

    def test_parse_dart_contains_edges(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample.dart")
        contains = [e for e in edges if e.kind == "CONTAINS"]
        # File should contain top-level classes and functions
        file_path = str(FIXTURES / "sample.dart")
        file_contains = [e for e in contains if e.source == file_path]
        assert len(file_contains) >= 1
        # Dog class should contain its methods
        dog_contains = [e for e in contains if "Dog" in e.source]
        dog_targets = {e.target for e in dog_contains}
        assert any("speak" in t for t in dog_targets)
        assert any("fetch" in t for t in dog_targets)

    def test_parse_dart_method_parent(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample.dart")
        funcs = [n for n in nodes if n.kind == "Function"]
        # Both Animal and Dog define speak(); check Dog's specifically
        dog_speak = next(
            (f for f in funcs if f.name == "speak" and f.parent_name == "Dog"), None,
        )
        assert dog_speak is not None

    def test_parse_dart_top_level_function_no_parent(self):
        nodes, edges = self.parser.parse_file(FIXTURES / "sample.dart")
        funcs = [n for n in nodes if n.kind == "Function"]
        create_dog = next((f for f in funcs if f.name == "createDog"), None)
        assert create_dog is not None
        assert create_dog.parent_name is None

    def test_parse_dart_call_edges(self):
        """Dart CALLS extraction (#87 bug 1).

        tree-sitter-dart doesn't wrap calls in a single ``call_expression``
        node so the parser has a Dart-specific walker that detects
        ``identifier + selector > argument_part`` patterns. Verify we
        capture builtin calls (``print``), constructor calls (``Dog(...)``),
        and internal method calls (``_run()``).
        """
        nodes, edges = self.parser.parse_file(FIXTURES / "sample.dart")
        calls = [e for e in edges if e.kind == "CALLS"]
        assert calls, "expected at least one CALLS edge for Dart"
        targets = [e.target for e in calls]
        # Builtin print is called at least twice in sample.dart
        assert sum(1 for t in targets if t == "print") >= 2
        # _run() is called inside Dog.fetch(); the call target should
        # either be the bare name "_run" or a qualified form ending in
        # "::Dog._run" once the call resolver has run.
        assert any(t == "_run" or t.endswith("::Dog._run") for t in targets), (
            f"expected _run() call, got targets: {targets}"
        )
        # Dog(name) constructor call from createDog() — target may be
        # bare "Dog" or qualified "...::Dog".
        assert any(t == "Dog" or t.endswith("::Dog") for t in targets), (
            f"expected Dog() constructor call, got targets: {targets}"
        )

    # --- tsconfig alias resolution ---

    def test_tsconfig_alias_resolution(self):
        """Alias imports should resolve to absolute file paths."""
        nodes, edges = self.parser.parse_file(FIXTURES / "alias_importer.ts")
        imports = [e for e in edges if e.kind == "IMPORTS_FROM"]
        resolved_imports = [e for e in imports if e.target.endswith("utils.ts")]
        assert len(resolved_imports) >= 1, (
            f"Expected resolved alias import, got targets: {[e.target for e in imports]}"
        )

    def test_tsconfig_missing_gracefully_handled(self):
        """Files without a tsconfig should still parse without errors."""
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = os.path.join(tmp_dir, "no_tsconfig_file.ts")
            with open(tmp_path, "w") as f:
                f.write('import { foo } from "@/bar";\nexport const x = 1;\n')
            nodes, edges = self.parser.parse_file(Path(tmp_path))
            imports = [e for e in edges if e.kind == "IMPORTS_FROM"]
            assert any("@/bar" in e.target for e in imports)

    # --- Vitest/Jest test detection ---

    def test_vitest_test_detection(self):
        """Vitest describe/it/test calls should produce Test nodes."""
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_vitest.test.ts")
        tests = [n for n in nodes if n.kind == "Test"]
        test_names = {t.name for t in tests}
        assert any(n.startswith("describe") or n.startswith("describe:") for n in test_names), (
            f"Expected describe Test node, got: {test_names}"
        )
        assert any(n.startswith("it:") or n.startswith("test:") for n in test_names), (
            f"Expected it/test Test node, got: {test_names}"
        )

    def test_vitest_contains_edges(self):
        """describe Test nodes should CONTAIN it/test Test nodes."""
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_vitest.test.ts")
        describe_nodes = [
            n for n in nodes
            if n.kind == "Test"
            and (n.name.startswith("describe") or n.name.startswith("describe:"))
        ]
        assert len(describe_nodes) >= 1
        it_tests = [
            n for n in nodes
            if n.kind == "Test" and (n.name.startswith("it:") or n.name.startswith("test:"))
        ]
        assert len(it_tests) >= 2

        file_path = str(FIXTURES / "sample_vitest.test.ts")
        describe_qualified = {f"{file_path}::{n.name}" for n in describe_nodes}
        contains_sources = {e.source for e in edges if e.kind == "CONTAINS"}
        assert describe_qualified & contains_sources

    def test_vitest_calls_edges(self):
        """Calls inside test blocks should produce CALLS edges."""
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_vitest.test.ts")
        calls = [e for e in edges if e.kind == "CALLS"]
        assert len(calls) >= 1
        test_names = {n.name for n in nodes if n.kind == "Test"}
        file_path = str(FIXTURES / "sample_vitest.test.ts")
        test_qualified = {f"{file_path}::{name}" for name in test_names}
        call_sources = {e.source for e in calls}
        assert call_sources & test_qualified

    def test_vitest_tested_by_edges(self):
        """TESTED_BY edges should be generated from test calls to production code."""
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_vitest.test.ts")
        tested_by = [e for e in edges if e.kind == "TESTED_BY"]
        assert len(tested_by) >= 1, (
            f"Expected TESTED_BY edges, got none. "
            f"All edges: {[(e.kind, e.source, e.target) for e in edges]}"
        )

    # --- Python callback REFERENCES (#363) ---
    # Functions passed as bare-identifier arguments (executor.submit(fn),
    # filter(fn, xs), map(fn, xs), df.apply(fn), ...) should produce
    # REFERENCES edges so dead-code detection does not flag them as unused.
    # Pre-fix: only the JS/TS `arguments` node type triggered the
    # _ref_from_arguments dispatcher; Python's `argument_list` was ignored.

    def test_python_callback_references_emitted(self):
        """A function passed as a bare identifier to another call should
        produce a REFERENCES edge from the calling function to it."""
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_callback_refs.py")
        refs = [e for e in edges if e.kind == "REFERENCES"]
        ref_target_names = {e.target.rsplit("::", 1)[-1] for e in refs}
        for callback in ("executor_callback", "filter_callback", "map_callback"):
            assert callback in ref_target_names, (
                f"Expected REFERENCES edge to {callback}, got targets: "
                f"{ref_target_names}"
            )

    def test_python_callback_references_not_treated_as_dead(self):
        """End-to-end: with REFERENCES edges in place, find_dead_code
        should not flag callback functions as dead."""
        from code_review_graph.graph import GraphStore
        from code_review_graph.refactor import find_dead_code

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "graph.db"
            store = GraphStore(db_path)
            try:
                nodes, edges = self.parser.parse_file(
                    FIXTURES / "sample_callback_refs.py"
                )
                store.store_file_nodes_edges(
                    str(FIXTURES / "sample_callback_refs.py"),
                    nodes, edges, "",
                )
                dead = find_dead_code(store)
                dead_names = {d["name"] for d in dead}
                for callback in (
                    "executor_callback", "filter_callback", "map_callback",
                ):
                    assert callback not in dead_names, (
                        f"{callback} was flagged as dead but is used as a "
                        f"callback. Dead names: {dead_names}"
                    )
            finally:
                store.close()

    def test_non_test_file_describe_not_special(self):
        """describe() in a non-test file should NOT create Test nodes."""
        import tempfile
        code = (
            b'function describe(name, fn) { fn(); }\n'
            b'describe("test", () => { console.log("hello"); });\n'
        )
        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False, prefix="regular_") as f:
            f.write(code)
            tmp_path = Path(f.name)
        try:
            nodes, edges = self.parser.parse_file(tmp_path)
            tests = [n for n in nodes if n.kind == "Test"]
            assert len(tests) == 0, (
                f"Non-test file should not have Test nodes, got: {[t.name for t in tests]}"
            )
        finally:
            tmp_path.unlink(missing_ok=True)

    # --- JSX component CALLS tests ---

    def test_tsx_jsx_component_invocation_creates_call_edge(self):
        source = (
            b"import MarkdownMsg from './MarkdownMsg';\n\n"
            b"export function BookWorkspace() {\n"
            b"  return <section><MarkdownMsg text={value} /></section>;\n"
            b"}\n"
        )
        path = FIXTURES / "BookWorkspace.tsx"

        _, edges = self.parser.parse_bytes(path, source)

        calls = [e for e in edges if e.kind == "CALLS"]
        expected_target = f"{str((FIXTURES / 'MarkdownMsg.tsx').resolve())}::MarkdownMsg"
        jsx_calls = [
            e for e in calls
            if e.source == f"{path}::BookWorkspace" and e.target == expected_target
        ]
        assert len(jsx_calls) == 1

    def test_tsx_intrinsic_dom_elements_do_not_create_call_edges(self):
        source = (
            b"export function BookWorkspace() {\n"
            b"  return <section><div /><span /></section>;\n"
            b"}\n"
        )
        path = FIXTURES / "BookWorkspace.tsx"

        _, edges = self.parser.parse_bytes(path, source)

        calls = [e for e in edges if e.kind == "CALLS"]
        assert calls == []

    def test_tsx_member_component_invocation_creates_unqualified_call_edge(self):
        source = (
            b"export function BookWorkspace() {\n"
            b"  return <UI.MarkdownMsg text={value} />;\n"
            b"}\n"
        )
        path = FIXTURES / "BookWorkspace.tsx"

        _, edges = self.parser.parse_bytes(path, source)

        calls = [e for e in edges if e.kind == "CALLS"]
        jsx_calls = [
            e for e in calls
            if e.source == f"{path}::BookWorkspace" and e.target == "MarkdownMsg"
        ]
        assert len(jsx_calls) == 1

    def test_tsx_namespace_import_component_invocation_resolves_to_module_file(self):
        source = (
            b"import * as UI from './MarkdownMsg';\n\n"
            b"export function BookWorkspace() {\n"
            b"  return <UI.MarkdownMsg text={value} />;\n"
            b"}\n"
        )
        path = FIXTURES / "BookWorkspace.tsx"

        _, edges = self.parser.parse_bytes(path, source)

        calls = [e for e in edges if e.kind == "CALLS"]
        expected_target = f"{str((FIXTURES / 'MarkdownMsg.tsx').resolve())}::MarkdownMsg"
        jsx_calls = [
            e for e in calls
            if e.source == f"{path}::BookWorkspace" and e.target == expected_target
        ]
        assert len(jsx_calls) == 1

    def test_tsx_nested_member_component_invocation_resolves_namespace_root(self):
        source = (
            b"import * as UI from './MarkdownMsg';\n\n"
            b"export function BookWorkspace() {\n"
            b"  return <UI.Messages.MarkdownMsg text={value} />;\n"
            b"}\n"
        )
        path = FIXTURES / "BookWorkspace.tsx"

        _, edges = self.parser.parse_bytes(path, source)

        calls = [e for e in edges if e.kind == "CALLS"]
        expected_target = f"{str((FIXTURES / 'MarkdownMsg.tsx').resolve())}::MarkdownMsg"
        jsx_calls = [
            e for e in calls
            if e.source == f"{path}::BookWorkspace" and e.target == expected_target
        ]
        assert len(jsx_calls) == 1

    def test_tsx_barrel_reexport_resolves_component_to_origin_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "components").mkdir()
            (root / "components" / "MarkdownMsg.tsx").write_text(
                "export function MarkdownMsg() { return <div />; }\n",
                encoding="utf-8",
            )
            (root / "components" / "index.ts").write_text(
                "export { MarkdownMsg } from './MarkdownMsg';\n",
                encoding="utf-8",
            )
            consumer = root / "BookWorkspace.tsx"
            source = (
                b"import { MarkdownMsg } from './components';\n\n"
                b"export function BookWorkspace() {\n"
                b"  return <MarkdownMsg text={value} />;\n"
                b"}\n"
            )

            _, edges = self.parser.parse_bytes(consumer, source)

            calls = [e for e in edges if e.kind == "CALLS"]
            expected_target = (
                f"{str((root / 'components' / 'MarkdownMsg.tsx').resolve())}"
                "::MarkdownMsg"
            )
            jsx_calls = [
                e for e in calls
                if e.source == f"{consumer}::BookWorkspace" and e.target == expected_target
            ]
            assert len(jsx_calls) == 1

    def test_tsx_barrel_aliased_reexport_resolves_component_to_origin_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "components").mkdir()
            (root / "components" / "MarkdownMsg.tsx").write_text(
                "export function MarkdownMsg() { return <div />; }\n",
                encoding="utf-8",
            )
            (root / "components" / "index.ts").write_text(
                "export { MarkdownMsg as Msg } from './MarkdownMsg';\n",
                encoding="utf-8",
            )
            consumer = root / "BookWorkspace.tsx"
            source = (
                b"import { Msg } from './components';\n\n"
                b"export function BookWorkspace() {\n"
                b"  return <Msg text={value} />;\n"
                b"}\n"
            )

            _, edges = self.parser.parse_bytes(consumer, source)

            calls = [e for e in edges if e.kind == "CALLS"]
            expected_target = (
                f"{str((root / 'components' / 'MarkdownMsg.tsx').resolve())}"
                "::MarkdownMsg"
            )
            jsx_calls = [
                e for e in calls
                if e.source == f"{consumer}::BookWorkspace" and e.target == expected_target
            ]
            assert len(jsx_calls) == 1

    def test_tsx_barrel_star_reexport_resolves_component_to_origin_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "components").mkdir()
            (root / "components" / "MarkdownMsg.tsx").write_text(
                "export function MarkdownMsg() { return <div />; }\n",
                encoding="utf-8",
            )
            (root / "components" / "index.ts").write_text(
                "export * from './MarkdownMsg';\n",
                encoding="utf-8",
            )
            consumer = root / "BookWorkspace.tsx"
            source = (
                b"import { MarkdownMsg } from './components';\n\n"
                b"export function BookWorkspace() {\n"
                b"  return <MarkdownMsg text={value} />;\n"
                b"}\n"
            )

            _, edges = self.parser.parse_bytes(consumer, source)

            calls = [e for e in edges if e.kind == "CALLS"]
            expected_target = (
                f"{str((root / 'components' / 'MarkdownMsg.tsx').resolve())}"
                "::MarkdownMsg"
            )
            jsx_calls = [
                e for e in calls
                if e.source == f"{consumer}::BookWorkspace" and e.target == expected_target
            ]
            assert len(jsx_calls) == 1

    def test_grimoire_style_jsx_fixture_tracks_all_component_call_sites(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            components = root / "components"
            components.mkdir()
            (components / "MarkdownMsg.jsx").write_text(
                "export function MarkdownMsg({ text }) { return <div>{text}</div>; }\n",
                encoding="utf-8",
            )
            (components / "index.js").write_text(
                "export { MarkdownMsg } from './MarkdownMsg';\n",
                encoding="utf-8",
            )
            consumer = root / "BookWorkspace.jsx"
            consumer.write_text(
                "import { MarkdownMsg } from './components';\n\n"
                "export function BookDashboard() {\n"
                "  return (\n"
                "    <>\n"
                "      <MarkdownMsg text='a' />\n"
                "      <MarkdownMsg text='b' />\n"
                "      <MarkdownMsg text='c' />\n"
                "    </>\n"
                "  );\n"
                "}\n\n"
                "export function AIPanel() {\n"
                "  return (\n"
                "    <>\n"
                "      <MarkdownMsg text='d' />\n"
                "      <MarkdownMsg text='e' />\n"
                "    </>\n"
                "  );\n"
                "}\n",
                encoding="utf-8",
            )

            _, edges = self.parser.parse_file(consumer)

            expected_target = (
                f"{str((components / 'MarkdownMsg.jsx').resolve())}::MarkdownMsg"
            )
            jsx_calls = [
                e for e in edges
                if e.kind == "CALLS" and e.target == expected_target
            ]
            by_source = {}
            for edge in jsx_calls:
                by_source[edge.source] = by_source.get(edge.source, 0) + 1
            assert by_source == {
                f"{consumer}::BookDashboard": 3,
                f"{consumer}::AIPanel": 2,
            }

    def test_nested_barrel_chain_resolves_component_to_origin_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            messages = root / "components" / "messages"
            messages.mkdir(parents=True)
            (messages / "MarkdownMsg.jsx").write_text(
                "export function MarkdownMsg({ text }) { return <div>{text}</div>; }\n",
                encoding="utf-8",
            )
            (messages / "index.js").write_text(
                "export { MarkdownMsg } from './MarkdownMsg';\n",
                encoding="utf-8",
            )
            (root / "components" / "index.js").write_text(
                "export { MarkdownMsg as Msg } from './messages';\n",
                encoding="utf-8",
            )
            consumer = root / "BookWorkspace.jsx"
            consumer.write_text(
                "import { Msg } from './components';\n\n"
                "export function BookDashboard() {\n"
                "  return <Msg text='a' />;\n"
                "}\n",
                encoding="utf-8",
            )

            _, edges = self.parser.parse_file(consumer)

            expected_target = (
                f"{str((messages / 'MarkdownMsg.jsx').resolve())}::MarkdownMsg"
            )
            jsx_calls = [
                e for e in edges
                if e.kind == "CALLS"
                and e.source == f"{consumer}::BookDashboard"
                and e.target == expected_target
            ]
            assert len(jsx_calls) == 1

    def test_junit_annotation_marks_test(self):
        """Java @Test annotation should mark functions as tests."""
        nodes, _ = self.parser.parse_bytes(
            Path("/src/MyTest.java"),
            b"class MyTest {\n"
            b"  @Test\n"
            b"  void verifyBehavior() { }\n"
            b"  void helperMethod() { }\n"
            b"}\n",
        )
        test_nodes = [n for n in nodes if n.is_test]
        test_names = {n.name for n in test_nodes}
        assert "verifyBehavior" in test_names
        assert "helperMethod" not in test_names

    def test_kotlin_test_annotation_marks_test(self):
        """Kotlin @Test annotation should mark functions as tests."""
        nodes, _ = self.parser.parse_bytes(
            Path("/src/SampleTest.kt"),
            b"class SampleTest {\n"
            b"  @Test fun checkResult() { }\n"
            b"  fun setup() { }\n"
            b"}\n",
        )
        test_nodes = [n for n in nodes if n.is_test]
        test_names = {n.name for n in test_nodes}
        assert "checkResult" in test_names
        assert "setup" not in test_names

    def test_detects_test_functions(self):
        """Functions with test-like names should be marked is_test=True."""
        nodes, _ = self.parser.parse_bytes(
            Path("/src/test_example.py"),
            b"def test_something(): pass\n"
            b"def helper(): pass\n",
        )
        test_nodes = [n for n in nodes if n.is_test]
        test_names = {n.name for n in test_nodes}
        assert "test_something" in test_names
        assert "helper" not in test_names


class TestValueReferences:
    """Tests for REFERENCES edge extraction from function-as-value patterns."""

    def setup_method(self):
        self.parser = CodeParser()

    def test_ts_object_literal_function_values(self):
        """Object literal values that are function identifiers emit REFERENCES edges."""
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_map_dispatch.ts")
        refs = [e for e in edges if e.kind == "REFERENCES"]
        ref_targets_bare = {e.target.split("::")[-1] for e in refs}
        # handleCreate, handleUpdate, handleDelete are values in the handlers object
        assert "handleCreate" in ref_targets_bare
        assert "handleUpdate" in ref_targets_bare
        assert "handleDelete" in ref_targets_bare

    def test_ts_shorthand_property_references(self):
        """Shorthand properties like { validateInput } emit REFERENCES edges."""
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_map_dispatch.ts")
        refs = [e for e in edges if e.kind == "REFERENCES"]
        ref_targets_bare = {e.target.split("::")[-1] for e in refs}
        assert "validateInput" in ref_targets_bare
        assert "processData" in ref_targets_bare

    def test_ts_array_function_elements(self):
        """Array elements that are function identifiers emit REFERENCES edges."""
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_map_dispatch.ts")
        refs = [e for e in edges if e.kind == "REFERENCES"]
        ref_targets_bare = {e.target.split("::")[-1] for e in refs}
        # pipeline = [validateInput, processData, formatOutput]
        assert "formatOutput" in ref_targets_bare

    def test_ts_callback_argument_reference(self):
        """Function identifiers passed as arguments emit REFERENCES edges."""
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_map_dispatch.ts")
        refs = [e for e in edges if e.kind == "REFERENCES"]
        ref_targets_bare = {e.target.split("::")[-1] for e in refs}
        # register(handleCreate) in dispatch function
        assert "handleCreate" in ref_targets_bare

    def test_ts_property_assignment_reference(self):
        """Property assignment RHS identifiers emit REFERENCES edges."""
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_map_dispatch.ts")
        refs = [e for e in edges if e.kind == "REFERENCES"]
        ref_targets_bare = {e.target.split("::")[-1] for e in refs}
        # dynamicHandlers['format'] = formatOutput
        assert "formatOutput" in ref_targets_bare

    def test_python_dict_function_values(self):
        """Python dict values that are function identifiers emit REFERENCES edges."""
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_map_dispatch.py")
        refs = [e for e in edges if e.kind == "REFERENCES"]
        ref_targets_bare = {e.target.split("::")[-1] for e in refs}
        assert "handle_create" in ref_targets_bare
        assert "handle_update" in ref_targets_bare
        assert "handle_delete" in ref_targets_bare

    def test_python_list_function_elements(self):
        """Python list elements that are function identifiers emit REFERENCES edges."""
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_map_dispatch.py")
        refs = [e for e in edges if e.kind == "REFERENCES"]
        ref_targets_bare = {e.target.split("::")[-1] for e in refs}
        # pipeline = [validate_input, process_data, format_output]
        assert "validate_input" in ref_targets_bare
        assert "process_data" in ref_targets_bare
        assert "format_output" in ref_targets_bare

    def test_references_have_correct_source(self):
        """REFERENCES edges should have the enclosing function as source."""
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_map_dispatch.ts")
        refs = [e for e in edges if e.kind == "REFERENCES"]
        # The register(handleCreate) call is inside 'dispatch'
        dispatch_refs = [
            e for e in refs
            if "dispatch" in e.source and "handleCreate" in e.target
        ]
        assert len(dispatch_refs) >= 1

    def test_no_references_for_unknown_identifiers(self):
        """Identifiers not in defined_names or import_map should NOT emit REFERENCES."""
        nodes, edges = self.parser.parse_bytes(
            Path("/test/example.ts"),
            b"function outer() {\n"
            b"  const map = { key: unknownFunc };\n"
            b"}\n",
        )
        refs = [e for e in edges if e.kind == "REFERENCES"]
        ref_targets = {e.target for e in refs}
        assert "unknownFunc" not in ref_targets

    def test_no_references_for_constants(self):
        """All-uppercase identifiers should NOT emit REFERENCES (likely constants)."""
        nodes, edges = self.parser.parse_bytes(
            Path("/test/example.ts"),
            b"const MAX_SIZE = 100;\n"
            b"function outer() {\n"
            b"  const arr = [MAX_SIZE];\n"
            b"}\n",
        )
        refs = [e for e in edges if e.kind == "REFERENCES"]
        ref_targets = {e.target for e in refs}
        assert "MAX_SIZE" not in ref_targets

    def test_resolve_references_targets(self):
        """REFERENCES edges should have resolved (qualified) targets for local funcs."""
        nodes, edges = self.parser.parse_file(FIXTURES / "sample_map_dispatch.ts")
        refs = [e for e in edges if e.kind == "REFERENCES"]
        file_path = str(FIXTURES / "sample_map_dispatch.ts")
        # At least some targets should be fully qualified
        qualified_refs = [e for e in refs if "::" in e.target]
        assert len(qualified_refs) > 0


class TestModuleScopeCalls:
    """Module-scope calls (no enclosing function) must attribute to the File node.

    Previously these edges were silently dropped, causing ``find_dead_code`` to
    flag CLI entrypoints, notebook-helper functions, and top-level JSX renders
    as dead. The fix emits a CALLS edge with ``source = file_path`` (the File
    node's qualified name).
    """

    def setup_method(self):
        self.parser = CodeParser()

    def test_python_top_level_call_attributes_to_file(self):
        source = (
            b"def worker():\n"
            b"    return 1\n"
            b"\n"
            b"worker()\n"
        )
        path = FIXTURES / "module_scope_py.py"
        _, edges = self.parser.parse_bytes(path, source)

        calls = [e for e in edges if e.kind == "CALLS"]
        top_level = [
            e for e in calls
            if e.source == str(path) and e.target.endswith("worker")
        ]
        assert len(top_level) == 1
        # Edge originates at the call site (line 4), not the def (line 1).
        assert top_level[0].line == 4

    def test_python_if_main_block_call_attributes_to_file(self):
        source = (
            b"def run_job():\n"
            b"    return 1\n"
            b"\n"
            b"if __name__ == '__main__':\n"
            b"    run_job()\n"
        )
        path = FIXTURES / "module_scope_cli.py"
        _, edges = self.parser.parse_bytes(path, source)

        calls = [e for e in edges if e.kind == "CALLS"]
        top_level = [
            e for e in calls
            if e.source == str(path) and e.target.endswith("run_job")
        ]
        assert len(top_level) == 1
        # Edge originates inside the `if __name__` block (line 5).
        assert top_level[0].line == 5

    def test_tsx_top_level_jsx_render_attributes_to_file(self):
        # Bare top-level JSX expression statement exercises the
        # _extract_jsx_child path specifically (not a value-reference
        # fallback from the `const element = ...` assignment).
        source = (
            b"import App from './App';\n"
            b"\n"
            b"<App />;\n"
        )
        path = FIXTURES / "module_scope_entry.tsx"
        _, edges = self.parser.parse_bytes(path, source)

        calls = [e for e in edges if e.kind == "CALLS"]
        top_level = [
            e for e in calls
            if e.source == str(path) and e.target.endswith("App")
        ]
        assert len(top_level) == 1
        # Edge originates at the JSX site (line 3), not the import (line 1).
        assert top_level[0].line == 3

    def test_r_top_level_call_attributes_to_file(self):
        # R scripts are overwhelmingly module-scope by convention; this is
        # the highest-leverage language for the fix after Python.
        source = (
            b"worker <- function() {\n"
            b"  1\n"
            b"}\n"
            b"\n"
            b"worker()\n"
        )
        path = FIXTURES / "module_scope_sample.R"
        _, edges = self.parser.parse_bytes(path, source)

        top_level = [
            e for e in edges
            if e.kind == "CALLS"
            and e.source == str(path)
            and e.target.endswith("worker")
        ]
        assert len(top_level) == 1

    def test_elixir_top_level_dotted_call_attributes_to_file(self):
        # `.exs` scripts and mix tasks commonly have module-scope `IO.puts`,
        # which is what the parser comment explicitly calls out.
        source = b'IO.puts("hello")\n'
        path = FIXTURES / "module_scope_script.exs"
        _, edges = self.parser.parse_bytes(path, source)

        top_level = [
            e for e in edges
            if e.kind == "CALLS"
            and e.source == str(path)
            and e.target.endswith("puts")
        ]
        assert len(top_level) == 1
