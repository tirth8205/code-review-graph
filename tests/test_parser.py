"""Tests for the Tree-sitter parser module."""

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
