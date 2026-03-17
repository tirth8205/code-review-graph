"""Tests for the Tree-sitter parser module."""

from pathlib import Path

from code_review_graph.parser import CodeParser, NodeInfo, EdgeInfo

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
        assert "_validate_token" in call_targets
        assert "authenticate" in call_targets

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

    def test_unresolved_calls_stay_bare(self):
        """Method calls and unknown calls should remain as bare names."""
        _, edges = self.parser.parse_file(FIXTURES / "sample_python.py")
        calls = [e for e in edges if e.kind == "CALLS"]
        # self._validate_token() is a method call — can't resolve the target file
        bare_calls = [e for e in calls if e.target == "_validate_token"]
        assert len(bare_calls) >= 1

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
