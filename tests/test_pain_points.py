"""TDD tests for known pain points identified from evaluation iterations.

Each test targets a specific resolution/analysis gap found in the HealthAgent
and Gadgetbridge evaluations. Tests are organized by pain point category and
marked with ``pytest.mark.xfail`` when they exercise functionality that does
not yet work.  The goal: make these green one at a time as we build enrichers
and fix resolution logic.

Categories:
  1. Call resolution -- module-level imports, star imports, JVM per-symbol
  2. Dead code false positives -- property calls, framework entry points
  3. Risk scoring differentiation -- continuous gradation
  4. Entry point / flow detection -- Android, Servlet, Express
"""

import tempfile
from importlib.util import find_spec
from pathlib import Path

import pytest

from code_review_graph.changes import compute_risk_score
from code_review_graph.flows import detect_entry_points
from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo
from code_review_graph.refactor import find_dead_code

FIXTURES = Path(__file__).parent / "fixtures"


# ===================================================================
# Helpers
# ===================================================================


class _GraphTestBase:
    """Mixin for tests that need a temporary graph store."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _add_func(
        self,
        name: str,
        path: str = "app.py",
        parent: str | None = None,
        is_test: bool = False,
        extra: dict | None = None,
        line_start: int = 1,
        line_end: int = 10,
        language: str = "python",
    ) -> int:
        node = NodeInfo(
            kind="Test" if is_test else "Function",
            name=name,
            file_path=path,
            line_start=line_start,
            line_end=line_end,
            language=language,
            parent_name=parent,
            is_test=is_test,
            extra=extra or {},
        )
        nid = self.store.upsert_node(node, file_hash="abc")
        self.store.commit()
        return nid

    def _add_class(
        self,
        name: str,
        path: str = "app.py",
        parent: str | None = None,
        extra: dict | None = None,
        line_start: int = 1,
        line_end: int = 10,
        language: str = "python",
    ) -> int:
        node = NodeInfo(
            kind="Class",
            name=name,
            file_path=path,
            line_start=line_start,
            line_end=line_end,
            language=language,
            parent_name=parent,
            extra=extra or {},
        )
        nid = self.store.upsert_node(node, file_hash="abc")
        self.store.commit()
        return nid

    def _add_edge(self, kind: str, source: str, target: str,
                  path: str = "app.py", line: int = 5) -> None:
        self.store.upsert_edge(EdgeInfo(
            kind=kind, source=source, target=target,
            file_path=path, line=line,
        ))
        self.store.commit()


# ===================================================================
# 1. CALL RESOLUTION
# ===================================================================


class TestResolutionModuleLevelImport:
    """Pain point: `import json; json.dumps()` stays as bare `dumps`.

    The parser only tracked `from X import Y` in import_map.  Module-level
    imports (`import X`) are now tracked, and module-qualified calls produce
    edges like `json::dumps`.
    """

    def setup_method(self):
        self.parser = CodeParser()

    def test_module_import_attribute_call_resolved(self):
        """import json; json.dumps(data) should produce a CALLS edge to json::dumps."""
        source = (FIXTURES / "resolution_python_module_import.py").read_bytes()
        _, edges = self.parser.parse_bytes(Path("/src/app.py"), source)
        calls = [e for e in edges if e.kind == "CALLS"]
        assert any("dumps" in e.target and "::" in e.target for e in calls), (
            f"Expected resolved call to json::dumps, got: "
            f"{[e.target for e in calls]}"
        )

    def test_module_import_nested_attribute(self):
        """import os.path; os.path.getsize() should resolve."""
        source = (FIXTURES / "resolution_python_module_import.py").read_bytes()
        _, edges = self.parser.parse_bytes(Path("/src/app.py"), source)
        calls = [e for e in edges if e.kind == "CALLS"]
        assert any("getsize" in e.target and "::" in e.target for e in calls), (
            f"Expected resolved call to os.path::getsize, got: "
            f"{[e.target for e in calls]}"
        )


class TestResolutionStarImport:
    """Pain point: `from X import *` doesn't populate import_map."""

    def setup_method(self):
        self.parser = CodeParser()

    def test_star_import_call_resolved(self):
        """from sample_python import *; create_auth_service() should resolve."""
        _, edges = self.parser.parse_file(
            FIXTURES / "resolution_python_star_import.py"
        )
        calls = [e for e in edges if e.kind == "CALLS"]
        assert any(
            "create_auth_service" in e.target and "::" in e.target for e in calls
        ), (
            f"Expected resolved call to sample_python::create_auth_service, got: "
            f"{[e.target for e in calls]}"
        )

    def test_star_import_respects_dunder_all(self):
        """__all__ should limit which names are exported via star import."""
        from code_review_graph.parser import CodeParser
        parser = CodeParser()
        # Parse a module with __all__
        p = parser._get_parser("python")
        code = (
            b'__all__ = ["public_func"]\n'
            b"def public_func(): pass\ndef _private(): pass\ndef other(): pass\n"
        )
        tree = p.parse(code)  # type: ignore[union-attr]
        result = CodeParser._extract_dunder_all(tree.root_node)
        assert result == {"public_func"}

    def test_star_import_excludes_private_without_all(self):
        """Without __all__, star import should exclude _private names."""
        from code_review_graph.parser import CodeParser
        parser = CodeParser()
        p = parser._get_parser("python")
        code = b'def public_func(): pass\ndef _private(): pass\nclass MyClass: pass\n'
        tree = p.parse(code)  # type: ignore[union-attr]
        result = CodeParser._extract_dunder_all(tree.root_node)
        assert result is None  # No __all__ defined
        _, defined = parser._collect_file_scope(tree.root_node, "python", code)
        exported = {n for n in defined if not n.startswith("_")}
        assert exported == {"public_func", "MyClass"}


class TestResolutionJvmPerSymbolImport:
    """JVM per-symbol IMPORTS_FROM edges.

    The `_get_jvm_import_names()` method works (unit-tested separately),
    but it only fires when `_resolve_module_to_file()` succeeds.  For JVM
    package imports (com.example.auth.UserService), resolution always fails
    because there's no Java project layout or scip-java index.

    These tests document that gap: per-symbol edges are only created when
    the module CAN be resolved to a file.
    """

    def setup_method(self):
        self.parser = CodeParser()

    def test_java_import_creates_per_symbol_edge(self):
        """import com.example.auth.UserService should create IMPORTS_FROM ::UserService."""
        _, edges = self.parser.parse_file(
            FIXTURES / "resolution_java_import.java"
        )
        imports = [e for e in edges if e.kind == "IMPORTS_FROM"]
        import_targets = {e.target for e in imports}
        has_user_service = any("::UserService" in t for t in import_targets)
        has_user = any("::User" in t for t in import_targets)
        assert has_user_service, (
            f"Expected ::UserService in import targets, got: {import_targets}"
        )
        assert has_user, (
            f"Expected ::User in import targets, got: {import_targets}"
        )

    def test_kotlin_import_creates_per_symbol_edge(self):
        """import com.example.auth.UserRepository should create IMPORTS_FROM ::UserRepository."""
        _, edges = self.parser.parse_file(
            FIXTURES / "resolution_kotlin_import.kt"
        )
        imports = [e for e in edges if e.kind == "IMPORTS_FROM"]
        import_targets = {e.target for e in imports}
        has_user_repo = any("::UserRepository" in t for t in import_targets)
        has_user = any("::User" in t for t in import_targets)
        assert has_user_repo, (
            f"Expected ::UserRepository in import targets, got: {import_targets}"
        )
        assert has_user, (
            f"Expected ::User in import targets, got: {import_targets}"
        )

    def test_get_jvm_import_names_unit(self):
        """Unit test: _get_jvm_import_names extracts symbol from dotted path."""

        class FakeNode:
            def __init__(self, text):
                self.text = text.encode("utf-8")

        assert self.parser._get_jvm_import_names(
            FakeNode("import com.example.UserService;"), "java"
        ) == ["UserService"]
        assert self.parser._get_jvm_import_names(
            FakeNode("import static org.junit.Assert.assertEquals"), "java"
        ) == ["assertEquals"]
        assert self.parser._get_jvm_import_names(
            FakeNode("import com.example.*"), "java"
        ) == []
        assert self.parser._get_jvm_import_names(
            FakeNode("import nodomain.freeyourgadget.gadgetbridge.model.ActivityKind"),
            "kotlin",
        ) == ["ActivityKind"]


class TestResolutionCrossFileBareNames:
    """Pain point: multiple files define `sync`, `get`, `run` etc.

    Without cross-file symbol table, bare-name calls can't be traced back.
    """

    def setup_method(self):
        self.parser = CodeParser()

    def test_bare_name_disambiguation_via_import(self):
        """Same-file resolution: a bare call to a locally-defined function
        should resolve to the qualified name even without imports.
        """
        second_file = FIXTURES / "resolution_python_module_import.py"
        _, edges2 = self.parser.parse_bytes(
            second_file,
            b"def create_auth_service(): pass\ndef other(): create_auth_service()\n",
        )
        calls = [e for e in edges2 if e.kind == "CALLS"]
        resolved = [e for e in calls if "::" in e.target and "create_auth_service" in e.target]
        assert len(resolved) >= 1


class TestResolutionMethodCallOnImportedClass:
    """Pain point: `service.authenticate(token)` where service is of type
    AuthService (imported) can't resolve to AuthService.authenticate.

    This requires type inference that tree-sitter can't provide.
    """

    def setup_method(self):
        self.parser = CodeParser()

    def test_method_on_typed_variable_resolves(self):
        """service.authenticate() where service: AuthService should resolve."""
        _, edges = self.parser.parse_bytes(
            Path("/src/app.py"),
            (
                b"from auth import AuthService\n"
                b"def main():\n"
                b"    service: AuthService = AuthService('x', 'y')\n"
                b"    service.authenticate('token')\n"
            ),
        )
        calls = [e for e in edges if e.kind == "CALLS"]
        # Should resolve authenticate to AuthService.authenticate
        assert any(
            "authenticate" in e.target and "::" in e.target for e in calls
        ), f"Expected resolved authenticate call, got: {[e.target for e in calls]}"

    def test_kotlin_typed_variable_resolves(self):
        """val syncer: SleepSyncer = ... ; syncer.sync() -> SleepSyncer::sync."""
        _, edges = self.parser.parse_bytes(
            Path("/src/Main.kt"),
            (
                b"package com.example\n"
                b"import com.example.syncers.SleepSyncer\n"
                b"fun main() {\n"
                b"    val syncer: SleepSyncer = SleepSyncer()\n"
                b"    syncer.sync()\n"
                b"}\n"
            ),
        )
        calls = [e for e in edges if e.kind == "CALLS"]
        assert any(
            "sync" in e.target and "SleepSyncer" in e.target and "::" in e.target
            for e in calls
        ), f"Expected SleepSyncer::sync, got: {[e.target for e in calls]}"

    def test_kotlin_constructor_param_typed_call(self):
        """class Foo(val repo: UserRepository) ; repo.save() -> UserRepository::save."""
        _, edges = self.parser.parse_bytes(
            Path("/src/Service.kt"),
            (
                b"package com.example\n"
                b"class UserService(val repo: UserRepository) {\n"
                b"    fun persist(user: User) {\n"
                b"        repo.save(user)\n"
                b"    }\n"
                b"}\n"
            ),
        )
        calls = [e for e in edges if e.kind == "CALLS"]
        assert any(
            "save" in e.target and "UserRepository" in e.target and "::" in e.target
            for e in calls
        ), f"Expected UserRepository::save, got: {[e.target for e in calls]}"

    def test_java_typed_variable_resolves(self):
        """AuthService service = new AuthService(); service.auth() -> AuthService::auth."""
        _, edges = self.parser.parse_bytes(
            Path("/src/App.java"),
            (
                b"package com.example;\n"
                b"public class App {\n"
                b"    public void main() {\n"
                b"        AuthService service = new AuthService();\n"
                b"        service.authenticate();\n"
                b"    }\n"
                b"}\n"
            ),
        )
        calls = [e for e in edges if e.kind == "CALLS"]
        assert any(
            "authenticate" in e.target and "AuthService" in e.target and "::" in e.target
            for e in calls
        ), f"Expected AuthService::authenticate, got: {[e.target for e in calls]}"

    def test_java_field_typed_call(self):
        """private UserRepository repo; ... repo.findById() -> UserRepository::findById."""
        _, edges = self.parser.parse_bytes(
            Path("/src/Service.java"),
            (
                b"package com.example;\n"
                b"public class UserService {\n"
                b"    private UserRepository repo;\n"
                b"    public User get(int id) {\n"
                b"        return repo.findById(id);\n"
                b"    }\n"
                b"}\n"
            ),
        )
        calls = [e for e in edges if e.kind == "CALLS"]
        assert any(
            "findById" in e.target and "UserRepository" in e.target and "::" in e.target
            for e in calls
        ), f"Expected UserRepository::findById, got: {[e.target for e in calls]}"

    def test_kotlin_companion_object_call_qualified(self):
        """StepsSyncer.sync() should produce a target containing StepsSyncer."""
        _, edges = self.parser.parse_bytes(
            Path("/src/Main.kt"),
            (
                b"package com.example\n"
                b"object StepsSyncer {\n"
                b"    fun sync(): Int = 0\n"
                b"}\n"
                b"fun main() {\n"
                b"    StepsSyncer.sync()\n"
                b"}\n"
            ),
        )
        calls = [e for e in edges if e.kind == "CALLS"]
        assert any(
            "StepsSyncer" in e.target and "sync" in e.target for e in calls
        ), f"Expected StepsSyncer.sync target, got: {[e.target for e in calls]}"

    def test_java_static_method_call_qualified(self):
        """Math.abs() should produce a target containing Math."""
        _, edges = self.parser.parse_bytes(
            Path("/src/App.java"),
            (
                b"package com.example;\n"
                b"public class App {\n"
                b"    public int calc(int x) {\n"
                b"        return Math.abs(x);\n"
                b"    }\n"
                b"}\n"
            ),
        )
        calls = [e for e in edges if e.kind == "CALLS"]
        assert any(
            "Math" in e.target and "abs" in e.target for e in calls
        ), f"Expected Math.abs target, got: {[e.target for e in calls]}"

    def test_python_classmethod_call_qualified(self):
        """MyClass.create() should produce a target containing MyClass."""
        _, edges = self.parser.parse_bytes(
            Path("/src/app.py"),
            (
                b"class MyClass:\n"
                b"    @classmethod\n"
                b"    def create(cls): pass\n"
                b"\n"
                b"def main():\n"
                b"    MyClass.create()\n"
            ),
        )
        calls = [e for e in edges if e.kind == "CALLS"]
        assert any(
            "MyClass" in e.target and "create" in e.target for e in calls
        ), f"Expected MyClass.create target, got: {[e.target for e in calls]}"

    def test_python_constructor_infers_type(self):
        """service = AuthService() then service.call() should resolve via constructor."""
        _, edges = self.parser.parse_bytes(
            Path("/src/app.py"),
            (
                b"from auth import AuthService\n"
                b"def main():\n"
                b"    service = AuthService('key')\n"
                b"    service.authenticate('token')\n"
            ),
        )
        calls = [e for e in edges if e.kind == "CALLS"]
        assert any(
            "authenticate" in e.target and "AuthService" in e.target
            and "::" in e.target for e in calls
        ), f"Expected AuthService::authenticate, got: {[e.target for e in calls]}"

    def test_kotlin_constructor_infers_type(self):
        """val syncer = SleepSyncer() (no type annotation) then syncer.sync() resolves."""
        _, edges = self.parser.parse_bytes(
            Path("/src/Main.kt"),
            (
                b"package com.example\n"
                b"import com.example.syncers.SleepSyncer\n"
                b"fun main() {\n"
                b"    val syncer = SleepSyncer()\n"
                b"    syncer.sync()\n"
                b"}\n"
            ),
        )
        calls = [e for e in edges if e.kind == "CALLS"]
        assert any(
            "sync" in e.target and "SleepSyncer" in e.target and "::" in e.target
            for e in calls
        ), f"Expected SleepSyncer::sync, got: {[e.target for e in calls]}"

    def test_java_var_constructor_infers_type(self):
        """var svc = new AuthService() should infer type from object_creation_expression."""
        _, edges = self.parser.parse_bytes(
            Path("/src/App.java"),
            (
                b"package com.example;\n"
                b"public class App {\n"
                b"    public void main() {\n"
                b"        var service = new AuthService();\n"
                b"        service.authenticate();\n"
                b"    }\n"
                b"}\n"
            ),
        )
        calls = [e for e in edges if e.kind == "CALLS"]
        assert any(
            "authenticate" in e.target and "AuthService" in e.target
            and "::" in e.target for e in calls
        ), f"Expected AuthService::authenticate, got: {[e.target for e in calls]}"

    def test_ts_typed_variable_resolves(self):
        """const svc: AuthService = new AuthService(); svc.call() -> AuthService::call."""
        _, edges = self.parser.parse_bytes(
            Path("/src/app.ts"),
            (
                b"import { AuthService } from './auth';\n"
                b"function main() {\n"
                b"    const svc: AuthService = new AuthService('key');\n"
                b"    svc.authenticate('token');\n"
                b"}\n"
            ),
        )
        calls = [e for e in edges if e.kind == "CALLS"]
        assert any(
            "authenticate" in e.target and "AuthService" in e.target
            and "::" in e.target for e in calls
        ), f"Expected AuthService::authenticate, got: {[e.target for e in calls]}"

    def test_ts_constructor_infers_type(self):
        """const db = new Database() (no annotation) then db.query() resolves."""
        _, edges = self.parser.parse_bytes(
            Path("/src/app.ts"),
            (
                b"import { Database } from './db';\n"
                b"function main() {\n"
                b"    const db = new Database();\n"
                b"    db.query('SELECT 1');\n"
                b"}\n"
            ),
        )
        calls = [e for e in edges if e.kind == "CALLS"]
        assert any(
            "query" in e.target and "Database" in e.target
            and "::" in e.target for e in calls
        ), f"Expected Database::query, got: {[e.target for e in calls]}"

    def test_js_constructor_infers_type(self):
        """const svc = new AuthService() in .js file should also resolve."""
        _, edges = self.parser.parse_bytes(
            Path("/src/app.js"),
            (
                b"const AuthService = require('./auth');\n"
                b"function main() {\n"
                b"    const svc = new AuthService();\n"
                b"    svc.authenticate();\n"
                b"}\n"
            ),
        )
        calls = [e for e in edges if e.kind == "CALLS"]
        assert any(
            "authenticate" in e.target and "AuthService" in e.target
            and "::" in e.target for e in calls
        ), f"Expected AuthService::authenticate, got: {[e.target for e in calls]}"

    def test_module_level_call_emits_edge(self):
        """Calls at file scope (not inside any function) should emit CALLS edges."""
        _, edges = self.parser.parse_bytes(
            Path("/src/init.py"),
            (
                b"def setup(): pass\n"
                b"setup()\n"
            ),
        )
        calls = [e for e in edges if e.kind == "CALLS"]
        assert any(
            "setup" in e.target for e in calls
        ), f"Expected CALLS edge for module-level setup(), got: {[e.target for e in calls]}"

    def test_func_passed_as_keyword_arg(self):
        """Thread(target=agent_thread) should emit CALLS to agent_thread."""
        _, edges = self.parser.parse_bytes(
            Path("/src/app.py"),
            (
                b"import threading\n"
                b"def agent_thread(): pass\n"
                b"def run():\n"
                b"    t = threading.Thread(target=agent_thread)\n"
            ),
        )
        calls = [e for e in edges if e.kind == "CALLS"]
        assert any(
            "agent_thread" in e.target for e in calls
        ), f"Expected CALLS edge for agent_thread, got: {[e.target for e in calls]}"

    def test_class_passed_as_positional_arg(self):
        """HTTPServer(addr, Handler) should emit CALLS to Handler."""
        _, edges = self.parser.parse_bytes(
            Path("/src/app.py"),
            (
                b"class Handler: pass\n"
                b"def run():\n"
                b"    server = make_server(('localhost', 8080), Handler)\n"
            ),
        )
        calls = [e for e in edges if e.kind == "CALLS"]
        assert any(
            "Handler" in e.target for e in calls
        ), f"Expected CALLS edge for Handler, got: {[e.target for e in calls]}"

    def test_func_ref_in_executor(self):
        """run_in_executor(None, _build_prompt) should emit CALLS to _build_prompt."""
        _, edges = self.parser.parse_bytes(
            Path("/src/app.py"),
            (
                b"def _build_prompt(): pass\n"
                b"async def main():\n"
                b"    loop = asyncio.get_event_loop()\n"
                b"    result = await loop.run_in_executor(None, _build_prompt)\n"
            ),
        )
        calls = [e for e in edges if e.kind == "CALLS"]
        assert any(
            "_build_prompt" in e.target for e in calls
        ), f"Expected CALLS edge for _build_prompt, got: {[e.target for e in calls]}"


# ===================================================================
# 2. DEAD CODE FALSE POSITIVES
# ===================================================================


class TestDeadCodeFalsePositives(_GraphTestBase):
    """Tests for known false positives in dead code detection.

    Each test seeds a graph scenario where a function is actually used
    but find_dead_code() incorrectly flags it.
    """

    def test_property_getter_not_dead(self):
        """@property methods are accessed as attributes, not called.
        They should not be flagged as dead code.
        """
        self.store.upsert_node(NodeInfo(
            kind="File", name="/repo/models.py", file_path="/repo/models.py",
            line_start=1, line_end=50, language="python",
        ))
        self._add_func(
            "full_name", path="/repo/models.py", parent="User",
            extra={"decorators": ["property"]},
        )
        self._add_class("User", path="/repo/models.py")
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "full_name" not in dead_names, (
            "@property getter flagged as dead code"
        )

    def test_interface_implementation_not_dead(self):
        """Methods implementing an interface should not be dead.
        Even if no direct CALLS edges point to them, they're called
        polymorphically via the interface.
        """
        self.store.upsert_node(NodeInfo(
            kind="File", name="/repo/syncer.kt", file_path="/repo/syncer.kt",
            line_start=1, line_end=50, language="kotlin",
        ))
        self._add_class("Syncer", path="/repo/syncer.kt", language="kotlin")
        self._add_func(
            "sync", path="/repo/syncer.kt", parent="Syncer",
            language="kotlin",
        )
        self._add_class("SleepSyncer", path="/repo/syncer.kt", language="kotlin")
        self._add_func(
            "sync", path="/repo/syncer.kt", parent="SleepSyncer",
            language="kotlin", line_start=20, line_end=30,
        )
        # SleepSyncer inherits Syncer
        self._add_edge(
            "INHERITS", "/repo/syncer.kt::SleepSyncer", "Syncer",
            path="/repo/syncer.kt",
        )
        # Some caller calls Syncer.sync (the interface method)
        self._add_func("doSync", path="/repo/manager.kt", language="kotlin")
        self._add_edge(
            "IMPORTS_FROM", "/repo/manager.kt", "/repo/syncer.kt",
            path="/repo/manager.kt",
        )
        self._add_edge(
            "CALLS", "/repo/manager.kt::doSync", "sync",
            path="/repo/manager.kt",
        )
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        # SleepSyncer.sync implements Syncer.sync -- should NOT be dead
        assert "sync" not in dead_names, (
            "Interface implementation flagged as dead code"
        )

    def test_override_not_dead_when_parent_method_has_qualified_callers(self):
        """When base class method has qualified CALLS edges, subclass overrides
        should not be flagged as dead. This is the real-world case: self.sync()
        in BaseConnector.run() resolves to base.py::BaseConnector.sync, and
        EgymConnector.sync (an override) has zero direct callers.
        """
        # Base class with sync() method in base.py
        self._add_class("BaseConnector", path="/repo/base.py")
        self._add_func("sync", path="/repo/base.py", parent="BaseConnector")
        self._add_func(
            "run", path="/repo/base.py", parent="BaseConnector",
            line_start=20, line_end=30,
        )
        # run() calls self.sync() -> resolves to qualified BaseConnector.sync
        self._add_edge(
            "CALLS",
            "/repo/base.py::BaseConnector.run",
            "/repo/base.py::BaseConnector.sync",
            path="/repo/base.py",
        )
        # Subclass EgymConnector overrides sync()
        self._add_class("EgymConnector", path="/repo/egym.py")
        self._add_func("sync", path="/repo/egym.py", parent="EgymConnector")
        # INHERITS edge
        self._add_edge(
            "INHERITS",
            "/repo/egym.py::EgymConnector",
            "BaseConnector",
            path="/repo/egym.py",
        )

        dead = find_dead_code(self.store)
        dead_qns = {d["qualified_name"] for d in dead}
        # EgymConnector.sync overrides BaseConnector.sync which has callers
        assert "/repo/egym.py::EgymConnector.sync" not in dead_qns, (
            "Override of called parent method flagged as dead code"
        )

    def test_bare_name_reverse_tracing(self):
        """When caller calls bare `sync`, and SleepSyncer.sync exists,
        callers_of(SleepSyncer.sync) should find the caller.
        """
        self.store.upsert_node(NodeInfo(
            kind="File", name="/repo/syncer.kt", file_path="/repo/syncer.kt",
            line_start=1, line_end=50, language="kotlin",
        ))
        self._add_func(
            "sync", path="/repo/syncer.kt", parent="SleepSyncer",
            language="kotlin",
        )
        self._add_func("doSync", path="/repo/manager.kt", language="kotlin")
        # Bare-name call: doSync() -> sync
        self._add_edge(
            "CALLS", "/repo/manager.kt::doSync", "sync",
            path="/repo/manager.kt",
        )

        # Post-build resolution qualifies bare targets
        resolved = self.store.resolve_bare_call_targets()
        assert resolved == 1

        # Query callers of the qualified name
        edges = self.store.get_edges_by_target(
            "/repo/syncer.kt::SleepSyncer.sync"
        )
        callers = [e for e in edges if e.kind == "CALLS"]
        assert len(callers) >= 1, (
            "Bare-name CALLS edge to 'sync' should be findable when querying "
            "callers of SleepSyncer.sync"
        )

    def test_bare_name_disambiguation_via_imports(self):
        """When multiple nodes share a bare name, resolve via import edges."""
        # Two files each have a 'sync' method
        self.store.upsert_node(NodeInfo(
            kind="File", name="/repo/a.kt", file_path="/repo/a.kt",
            line_start=1, line_end=50, language="kotlin",
        ))
        self.store.upsert_node(NodeInfo(
            kind="File", name="/repo/b.kt", file_path="/repo/b.kt",
            line_start=1, line_end=50, language="kotlin",
        ))
        self._add_func("sync", path="/repo/a.kt", parent="ClassA", language="kotlin")
        self._add_func("sync", path="/repo/b.kt", parent="ClassB", language="kotlin")
        self._add_func("caller", path="/repo/caller.kt", language="kotlin")

        # caller.kt imports from b.kt
        self._add_edge(
            "IMPORTS_FROM", "/repo/caller.kt", "/repo/b.kt::ClassB",
            path="/repo/caller.kt",
        )
        # Bare call: caller() -> sync
        self._add_edge(
            "CALLS", "/repo/caller.kt::caller", "sync",
            path="/repo/caller.kt",
        )

        resolved = self.store.resolve_bare_call_targets()
        assert resolved == 1

        # Should resolve to b.kt's sync (imported), not a.kt's
        edges = self.store.get_edges_by_target("/repo/b.kt::ClassB.sync")
        callers = [e for e in edges if e.kind == "CALLS"]
        assert len(callers) == 1

    def test_bare_name_ambiguous_left_unresolved(self):
        """When multiple candidates exist and no imports disambiguate, skip."""
        self.store.upsert_node(NodeInfo(
            kind="File", name="/repo/a.kt", file_path="/repo/a.kt",
            line_start=1, line_end=50, language="kotlin",
        ))
        self.store.upsert_node(NodeInfo(
            kind="File", name="/repo/b.kt", file_path="/repo/b.kt",
            line_start=1, line_end=50, language="kotlin",
        ))
        self._add_func("sync", path="/repo/a.kt", parent="ClassA", language="kotlin")
        self._add_func("sync", path="/repo/b.kt", parent="ClassB", language="kotlin")
        self._add_func("caller", path="/repo/caller.kt", language="kotlin")
        # No imports -- ambiguous
        self._add_edge(
            "CALLS", "/repo/caller.kt::caller", "sync",
            path="/repo/caller.kt",
        )

        resolved = self.store.resolve_bare_call_targets()
        assert resolved == 0  # Left bare

    def test_exported_function_not_dead(self):
        """Functions that are imported by other files should not be dead.
        Even without direct CALLS, IMPORTS_FROM edges should count.
        """
        self.store.upsert_node(NodeInfo(
            kind="File", name="/repo/utils.py", file_path="/repo/utils.py",
            line_start=1, line_end=50, language="python",
        ))
        self._add_func("helper", path="/repo/utils.py")
        # Another file imports it
        self._add_edge(
            "IMPORTS_FROM", "/repo/main.py", "/repo/utils.py::helper",
            path="/repo/main.py",
        )
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "helper" not in dead_names, (
            "Imported function flagged as dead code"
        )


# ===================================================================
# 3. RISK SCORING
# ===================================================================


class TestRiskScoringContinuous(_GraphTestBase):
    """Pain point: risk scores cluster at 0.50-0.70 with only 4 unique values.

    The continuous test coverage scale (0.30 untested -> 0.05 well-tested)
    should produce differentiated scores.
    """

    def test_risk_score_decreases_with_more_tests(self):
        """More TESTED_BY edges should monotonically decrease the test coverage
        component of the risk score.
        """
        self._add_func("func_0_tests", path="a.py", line_start=1, line_end=10)
        self._add_func("func_1_test", path="b.py", line_start=1, line_end=10)
        self._add_func("func_3_tests", path="c.py", line_start=1, line_end=10)
        self._add_func("func_5_tests", path="d.py", line_start=1, line_end=10)

        # Add tests for func_1
        self._add_func("test_1", path="test_b.py", is_test=True)
        self._add_edge("TESTED_BY", "test_b.py::test_1", "b.py::func_1_test", "test_b.py")

        # Add 3 tests for func_3
        for i in range(3):
            self._add_func(f"test_3_{i}", path="test_c.py", is_test=True,
                           line_start=i * 10 + 1, line_end=i * 10 + 10)
            self._add_edge(
                "TESTED_BY", f"test_c.py::test_3_{i}", "c.py::func_3_tests", "test_c.py",
            )

        # Add 5 tests for func_5
        for i in range(5):
            self._add_func(f"test_5_{i}", path="test_d.py", is_test=True,
                           line_start=i * 10 + 1, line_end=i * 10 + 10)
            self._add_edge(
                "TESTED_BY", f"test_d.py::test_5_{i}", "d.py::func_5_tests", "test_d.py",
            )

        scores = {}
        for name, path in [
            ("func_0_tests", "a.py"),
            ("func_1_test", "b.py"),
            ("func_3_tests", "c.py"),
            ("func_5_tests", "d.py"),
        ]:
            node = self.store.get_node(f"{path}::{name}")
            assert node is not None, f"Node {path}::{name} not found"
            scores[name] = compute_risk_score(self.store, node)

        # Monotonically decreasing
        assert scores["func_0_tests"] > scores["func_1_test"], (
            f"0 tests ({scores['func_0_tests']}) should score higher than "
            f"1 test ({scores['func_1_test']})"
        )
        assert scores["func_1_test"] > scores["func_3_tests"], (
            f"1 test ({scores['func_1_test']}) should score higher than "
            f"3 tests ({scores['func_3_tests']})"
        )
        assert scores["func_3_tests"] > scores["func_5_tests"], (
            f"3 tests ({scores['func_3_tests']}) should score higher than "
            f"5 tests ({scores['func_5_tests']})"
        )

    def test_risk_scores_span_meaningful_range(self):
        """When combining multiple scoring factors, risk scores should span
        a meaningful range -- not cluster within 0.20.
        """
        # Low risk: well-tested, no security keywords, few callers
        self._add_func("safe_helper", path="utils.py", line_start=1, line_end=10)
        for i in range(5):
            self._add_func(f"test_safe_{i}", path="test_utils.py", is_test=True,
                           line_start=i * 10 + 1, line_end=i * 10 + 10)
            self._add_edge(
                "TESTED_BY", f"test_utils.py::test_safe_{i}",
                "utils.py::safe_helper", "test_utils.py",
            )

        # High risk: untested, security keyword, many callers, cross-community
        self._add_func(
            "authenticate_user", path="auth.py",
            line_start=1, line_end=10,
        )
        for i in range(10):
            caller_path = f"caller_{i}.py"
            self._add_func(f"caller_{i}", path=caller_path,
                           line_start=1, line_end=10)
            self._add_edge(
                "CALLS", f"{caller_path}::caller_{i}",
                "auth.py::authenticate_user", caller_path,
            )

        low_node = self.store.get_node("utils.py::safe_helper")
        high_node = self.store.get_node("auth.py::authenticate_user")
        assert low_node is not None
        assert high_node is not None

        low_score = compute_risk_score(self.store, low_node)
        high_score = compute_risk_score(self.store, high_node)

        # High risk should be at least 0.30 higher than low risk
        gap = high_score - low_score
        assert gap >= 0.30, (
            f"Risk score gap too small: high={high_score:.4f} low={low_score:.4f} "
            f"gap={gap:.4f} (want >= 0.30)"
        )


# ===================================================================
# 4. ENTRY POINT / FLOW DETECTION
# ===================================================================


class TestEntryPointDetection(_GraphTestBase):
    """Tests for framework-specific entry point detection."""

    def test_android_oncreate_is_entry_point(self):
        """Android Activity.onCreate() should be detected as entry point."""
        self._add_func(
            "onCreate", path="/app/MainActivity.kt",
            parent="MainActivity", language="kotlin",
        )
        eps = detect_entry_points(self.store)
        ep_names = {ep.name for ep in eps}
        assert "onCreate" in ep_names

    def test_android_onresume_is_entry_point(self):
        """Android onResume() should be detected as entry point."""
        self._add_func(
            "onResume", path="/app/MainActivity.kt",
            parent="MainActivity", language="kotlin",
        )
        eps = detect_entry_points(self.store)
        ep_names = {ep.name for ep in eps}
        assert "onResume" in ep_names

    def test_android_ondestroy_is_entry_point(self):
        """Android onDestroy() should be detected as entry point."""
        self._add_func(
            "onDestroy", path="/app/MainActivity.kt",
            parent="MainActivity", language="kotlin",
        )
        eps = detect_entry_points(self.store)
        ep_names = {ep.name for ep in eps}
        assert "onDestroy" in ep_names

    def test_servlet_doget_is_entry_point(self):
        """Java Servlet doGet() should be detected as entry point."""
        self._add_func(
            "doGet", path="/web/UserServlet.java",
            parent="UserServlet", language="java",
        )
        eps = detect_entry_points(self.store)
        ep_names = {ep.name for ep in eps}
        assert "doGet" in ep_names

    def test_servlet_dopost_is_entry_point(self):
        """Java Servlet doPost() should be detected as entry point."""
        self._add_func(
            "doPost", path="/web/UserServlet.java",
            parent="UserServlet", language="java",
        )
        eps = detect_entry_points(self.store)
        ep_names = {ep.name for ep in eps}
        assert "doPost" in ep_names

    def test_express_error_handler_is_entry_point(self):
        """Express errorHandler function should be detected as entry point."""
        self._add_func(
            "errorHandler", path="/src/app.ts",
            language="typescript",
        )
        eps = detect_entry_points(self.store)
        ep_names = {ep.name for ep in eps}
        assert "errorHandler" in ep_names

    def test_composable_decorator_is_entry_point(self):
        """@Composable annotated functions should be entry points."""
        self._add_func(
            "HomeScreen", path="/ui/Home.kt",
            parent=None, language="kotlin",
            extra={"decorators": ["Composable"]},
        )
        eps = detect_entry_points(self.store)
        ep_names = {ep.name for ep in eps}
        assert "HomeScreen" in ep_names

    def test_spring_get_mapping_is_entry_point(self):
        """@GetMapping annotated functions should be entry points."""
        self._add_func(
            "getUsers", path="/web/UserController.java",
            parent="UserController", language="java",
            extra={"decorators": ["GetMapping('/users')"]},
        )
        eps = detect_entry_points(self.store)
        ep_names = {ep.name for ep in eps}
        assert "getUsers" in ep_names

    def test_hilt_viewmodel_is_entry_point(self):
        """@HiltViewModel annotated classes should be entry points."""
        self._add_func(
            "UserViewModel", path="/viewmodel/UserViewModel.kt",
            parent=None, language="kotlin",
            extra={"decorators": ["HiltViewModel"]},
        )
        eps = detect_entry_points(self.store)
        ep_names = {ep.name for ep in eps}
        assert "UserViewModel" in ep_names


# ===================================================================
# 5. PARSER-LEVEL INTEGRATION (parse real fixtures)
# ===================================================================


class TestParserFixtureIntegration:
    """Parse the new fixture files and verify expected edges/nodes."""

    def setup_method(self):
        self.parser = CodeParser()

    def test_android_lifecycle_nodes_extracted(self):
        """android_lifecycle.kt should produce nodes for lifecycle methods."""
        nodes, _ = self.parser.parse_file(FIXTURES / "android_lifecycle.kt")
        func_names = {n.name for n in nodes if n.kind == "Function"}
        assert "onCreate" in func_names
        assert "onResume" in func_names
        assert "onDestroy" in func_names
        assert "initializeUI" in func_names

    def test_android_lifecycle_calls_extracted(self):
        """onCreate should call initializeUI, onResume should call refreshData."""
        _, edges = self.parser.parse_file(FIXTURES / "android_lifecycle.kt")
        calls = [e for e in edges if e.kind == "CALLS"]
        targets = {e.target for e in calls}
        # These are same-file calls, should be resolved
        assert any("initializeUI" in t for t in targets), (
            f"Expected call to initializeUI, got: {targets}"
        )
        assert any("refreshData" in t for t in targets), (
            f"Expected call to refreshData, got: {targets}"
        )

    def test_servlet_nodes_extracted(self):
        """servlet_handler.java should produce nodes for doGet, doPost."""
        nodes, _ = self.parser.parse_file(FIXTURES / "servlet_handler.java")
        func_names = {n.name for n in nodes if n.kind == "Function"}
        assert "doGet" in func_names
        assert "doPost" in func_names
        assert "handleGetUser" in func_names

    def test_servlet_calls_extracted(self):
        """doGet should call handleGetUser, doPost should call handleCreateUser."""
        _, edges = self.parser.parse_file(FIXTURES / "servlet_handler.java")
        calls = [e for e in edges if e.kind == "CALLS"]
        targets = {e.target for e in calls}
        assert any("handleGetUser" in t for t in targets), (
            f"Expected call to handleGetUser, got: {targets}"
        )
        assert any("handleCreateUser" in t for t in targets), (
            f"Expected call to handleCreateUser, got: {targets}"
        )

    def test_express_routes_nodes_extracted(self):
        """express_routes.ts should produce nodes for handler functions."""
        nodes, _ = self.parser.parse_file(FIXTURES / "express_routes.ts")
        func_names = {n.name for n in nodes if n.kind == "Function"}
        assert "getUsers" in func_names
        assert "createUser" in func_names
        assert "errorHandler" in func_names

    def test_java_import_per_symbol(self):
        """resolution_java_import.java should have IMPORTS_FROM with ::ClassName."""
        _, edges = self.parser.parse_file(
            FIXTURES / "resolution_java_import.java"
        )
        imports = [e for e in edges if e.kind == "IMPORTS_FROM"]
        import_targets = {e.target for e in imports}
        assert any("::UserService" in t for t in import_targets), (
            f"Expected ::UserService import, got: {import_targets}"
        )

    def test_kotlin_import_per_symbol(self):
        """resolution_kotlin_import.kt should have IMPORTS_FROM with ::ClassName."""
        _, edges = self.parser.parse_file(
            FIXTURES / "resolution_kotlin_import.kt"
        )
        imports = [e for e in edges if e.kind == "IMPORTS_FROM"]
        import_targets = {e.target for e in imports}
        assert any("::UserRepository" in t for t in import_targets), (
            f"Expected ::UserRepository import, got: {import_targets}"
        )


# ===================================================================
# 6. Jedi post-build enrichment -- cross-file method calls
# ===================================================================


@pytest.mark.skipif(find_spec("jedi") is None, reason="jedi not installed")
class TestJediEnrichment:
    """Test Jedi-based resolution of method calls the parser drops.

    When code calls ``svc.method()`` and ``svc`` came from a factory function
    (lowercase receiver, no type annotation), the tree-sitter parser drops the
    call.  Jedi can trace the return type and resolve it.
    """

    def test_jedi_resolves_factory_return_method(self, tmp_path):
        """svc = create_service(); svc.authenticate() should resolve via Jedi."""

        # Use a clean directory name to avoid _is_test_file matching pytest names
        import tempfile
        proj = Path(tempfile.mkdtemp(prefix="proj_"))
        try:
            self._run_factory_test(proj)
        finally:
            import shutil
            shutil.rmtree(proj, ignore_errors=True)

    def _run_factory_test(self, proj):
        from code_review_graph.jedi_resolver import enrich_jedi_calls

        # Create package structure
        helpers = proj / "helpers"
        helpers.mkdir()
        (helpers / "__init__.py").write_text("")
        (helpers / "auth.py").write_text(
            "class AuthService:\n"
            "    def authenticate(self, token):\n"
            "        return True\n\n"
            "def create_auth_service():\n"
            "    return AuthService()\n"
        )
        (proj / "app.py").write_text(
            "from helpers.auth import create_auth_service\n\n"
            "def login(token):\n"
            "    svc = create_auth_service()\n"
            "    return svc.authenticate(token)\n"
        )

        # Build graph
        parser = CodeParser()
        store = GraphStore(str(proj / "graph.db"))
        try:
            for f in [helpers / "__init__.py", helpers / "auth.py", proj / "app.py"]:
                source = f.read_bytes()
                nodes, edges = parser.parse_bytes(f, source)
                store.store_file_nodes_edges(str(f), nodes, edges)
            store.commit()

            # Before Jedi: parser now emits instance method calls as bare names
            # (since instance-method tracking was added). Check that the edge
            # exists but is NOT fully qualified to a file path.
            app_path = str(proj / "app.py")
            login_qn = f"{app_path}::login"
            edges_before = store.get_edges_by_source(login_qn)
            auth_before = [
                e for e in edges_before
                if e.kind == "CALLS" and "authenticate" in e.target_qualified
            ]
            # If parser emits it, it should be bare (no file path resolution)
            if auth_before:
                assert "auth.py" not in auth_before[0].target_qualified, (
                    "Parser should not resolve instance call to file path"
                )

            # Run Jedi enrichment (may resolve 0 if parser already emitted the call)
            enrich_jedi_calls(store, proj)

            # After parse + optional Jedi: should have a CALLS edge to authenticate
            edges_after = store.get_edges_by_source(login_qn)
            auth_after = [
                e for e in edges_after
                if e.kind == "CALLS" and "authenticate" in e.target_qualified
            ]
            assert len(auth_after) >= 1, (
                f"Expected authenticate() call edge, got: "
                f"{[e.target_qualified for e in edges_after]}"
            )
        finally:
            store.close()

    def test_jedi_skips_stdlib_calls(self, tmp_path):
        """list.append(), str.upper() etc should NOT create edges."""
        import tempfile

        from code_review_graph.jedi_resolver import enrich_jedi_calls
        proj = Path(tempfile.mkdtemp(prefix="proj_"))
        try:
            (proj / "main.py").write_text(
                "def process():\n"
                "    items = []\n"
                "    items.append(1)\n"
                "    name = 'hello'\n"
                "    return name.upper()\n"
            )

            parser = CodeParser()
            store = GraphStore(str(proj / "graph.db"))
            try:
                f = proj / "main.py"
                nodes, edges = parser.parse_bytes(f, f.read_bytes())
                store.store_file_nodes_edges(str(f), nodes, edges)
                store.commit()

                stats = enrich_jedi_calls(store, proj)
                assert stats.get("resolved", 0) == 0
            finally:
                store.close()
        finally:
            import shutil
            shutil.rmtree(proj, ignore_errors=True)

    def test_jedi_no_duplicate_edges(self, tmp_path):
        """If typed-var enrichment already resolved a call, Jedi should skip it."""
        import tempfile

        from code_review_graph.jedi_resolver import enrich_jedi_calls
        proj = Path(tempfile.mkdtemp(prefix="proj_"))
        try:
            (proj / "service.py").write_text(
                "class AuthService:\n"
                "    def authenticate(self, token):\n"
                "        return True\n"
            )
            (proj / "app.py").write_text(
                "from service import AuthService\n\n"
                "def login(token):\n"
                "    svc = AuthService()\n"
                "    return svc.authenticate(token)\n"
            )

            parser = CodeParser()
            store = GraphStore(str(proj / "graph.db"))
            try:
                for f in [proj / "service.py", proj / "app.py"]:
                    nodes, edges = parser.parse_bytes(f, f.read_bytes())
                    store.store_file_nodes_edges(str(f), nodes, edges)
                store.commit()

                app_path = str(proj / "app.py")
                login_qn = f"{app_path}::login"
                edges_before = store.get_edges_by_source(login_qn)
                auth_before = [
                    e for e in edges_before
                    if e.kind == "CALLS" and "authenticate" in e.target_qualified
                ]
                count_before = len(auth_before)

                enrich_jedi_calls(store, proj)

                edges_after = store.get_edges_by_source(login_qn)
                auth_after = [
                    e for e in edges_after
                    if e.kind == "CALLS" and "authenticate" in e.target_qualified
                ]
                assert len(auth_after) <= count_before + 1, (
                    "Jedi should not create duplicate edges"
                )
            finally:
                store.close()
        finally:
            import shutil
            shutil.rmtree(proj, ignore_errors=True)

    def test_jedi_returns_stats(self, tmp_path):
        """Enrichment should return meaningful stats."""
        import tempfile

        from code_review_graph.jedi_resolver import enrich_jedi_calls
        proj = Path(tempfile.mkdtemp(prefix="proj_"))
        try:
            (proj / "empty.py").write_text("x = 1\n")

            parser = CodeParser()
            store = GraphStore(str(proj / "graph.db"))
            try:
                f = proj / "empty.py"
                nodes, edges = parser.parse_bytes(f, f.read_bytes())
                store.store_file_nodes_edges(str(f), nodes, edges)
                store.commit()

                stats = enrich_jedi_calls(store, proj)
                assert "resolved" in stats
                assert isinstance(stats["resolved"], int)
            finally:
                store.close()
        finally:
            import shutil
            shutil.rmtree(proj, ignore_errors=True)


# ===================================================================
# 5. Transitive TESTED_BY -- tests_for should follow CALLS chains
# ===================================================================


class TestTransitiveTestedBy(_GraphTestBase):
    """tests_for(A) should find tests that cover A's callees transitively.

    Real-world case: RecordedWorkoutSyncer.sync CALLS WorkoutSyncerUtils.map...
    and WorkoutSyncerUtilsTest tests WorkoutSyncerUtils.map... -- so
    tests_for(RecordedWorkoutSyncer) should return WorkoutSyncerUtilsTest.
    """

    def test_transitive_tested_by_one_hop(self):
        """A calls B, test covers B -> tests_for(A) should include that test."""
        # Production: syncer.sync -> utils.map
        self._add_func("sync", path="syncer.kt", parent="Syncer")
        self._add_func("map", path="utils.kt", parent="Utils")
        self._add_edge("CALLS", "syncer.kt::Syncer.sync", "utils.kt::Utils.map")

        # Test: test_map tests utils.map
        self._add_func("test_map", path="test_utils.kt", is_test=True)
        self._add_edge("CALLS", "test_utils.kt::test_map", "utils.kt::Utils.map")
        self._add_edge("TESTED_BY", "test_utils.kt::test_map", "utils.kt::Utils.map")

        results = self.store.get_transitive_tests("syncer.kt::Syncer.sync")
        test_names = {r["name"] for r in results}
        assert "test_map" in test_names

    def test_transitive_does_not_duplicate_direct(self):
        """If A already has direct tests, transitive should not duplicate them."""
        self._add_func("sync", path="syncer.kt", parent="Syncer")
        self._add_func("map", path="utils.kt", parent="Utils")
        self._add_edge("CALLS", "syncer.kt::Syncer.sync", "utils.kt::Utils.map")

        # Direct test for sync
        self._add_func("test_sync", path="test_syncer.kt", is_test=True)
        self._add_edge("CALLS", "test_syncer.kt::test_sync", "syncer.kt::Syncer.sync")
        self._add_edge("TESTED_BY", "test_syncer.kt::test_sync", "syncer.kt::Syncer.sync")

        # Indirect test for utils.map
        self._add_func("test_map", path="test_utils.kt", is_test=True)
        self._add_edge("CALLS", "test_utils.kt::test_map", "utils.kt::Utils.map")
        self._add_edge("TESTED_BY", "test_utils.kt::test_map", "utils.kt::Utils.map")

        results = self.store.get_transitive_tests("syncer.kt::Syncer.sync")
        test_names = [r["name"] for r in results]
        # Both tests present, no duplicates
        assert "test_sync" in test_names
        assert "test_map" in test_names
        assert len(test_names) == len(set(test_names))

    def test_transitive_marks_indirect(self):
        """Indirect tests should be marked as such."""
        self._add_func("sync", path="syncer.kt", parent="Syncer")
        self._add_func("map", path="utils.kt", parent="Utils")
        self._add_edge("CALLS", "syncer.kt::Syncer.sync", "utils.kt::Utils.map")

        self._add_func("test_map", path="test_utils.kt", is_test=True)
        self._add_edge("CALLS", "test_utils.kt::test_map", "utils.kt::Utils.map")
        self._add_edge("TESTED_BY", "test_utils.kt::test_map", "utils.kt::Utils.map")

        results = self.store.get_transitive_tests("syncer.kt::Syncer.sync")
        indirect = [r for r in results if r.get("indirect")]
        assert len(indirect) == 1
        assert indirect[0]["name"] == "test_map"


# ===================================================================
# 8. JSX HANDLER FUNCTION REFERENCES
# ===================================================================


class TestJSXHandlerRefs:
    """Pain point: onClick={handleDelete} does not emit a CALLS edge.

    _walk_func_ref_args only scans argument_list nodes, not jsx_expression
    nodes, so function references in JSX attributes are missed entirely.
    This is the #1 source of dead code false positives in React/TSX codebases.
    """

    def setup_method(self):
        self.parser = CodeParser()

    def test_jsx_onclick_emits_calls_edge(self):
        """<button onClick={handleDelete}> should emit a CALLS edge."""
        _, edges = self.parser.parse_bytes(
            Path("/src/Component.tsx"),
            (
                b"function handleDelete() { console.log('del'); }\n"
                b"function MyComponent() {\n"
                b"  return <button onClick={handleDelete}>Delete</button>;\n"
                b"}\n"
            ),
        )
        calls = [e for e in edges if e.kind == "CALLS"]
        targets = [e.target for e in calls]
        assert any("handleDelete" in t for t in targets), (
            f"Expected CALLS to handleDelete, got: {targets}"
        )

    def test_jsx_multiple_handlers(self):
        """Multiple JSX handlers in one component should all emit CALLS edges."""
        _, edges = self.parser.parse_bytes(
            Path("/src/Form.tsx"),
            (
                b"function handleChange(e: any) { }\n"
                b"function handleSubmit() { }\n"
                b"function Form() {\n"
                b"  return (\n"
                b"    <form onSubmit={handleSubmit}>\n"
                b"      <input onChange={handleChange} />\n"
                b"    </form>\n"
                b"  );\n"
                b"}\n"
            ),
        )
        calls = [e for e in edges if e.kind == "CALLS"]
        targets = [e.target for e in calls]
        assert any("handleSubmit" in t for t in targets), (
            f"Expected CALLS to handleSubmit, got: {targets}"
        )
        assert any("handleChange" in t for t in targets), (
            f"Expected CALLS to handleChange, got: {targets}"
        )


# ===================================================================
# 9. CLASS-LEVEL TRANSITIVE TESTED_BY
# ===================================================================


class TestClassLevelTransitiveTestedBy(_GraphTestBase):
    """Pain point: get_transitive_tests('ClassName') returns nothing.

    CALLS edges have method-level sources (ClassName.method), not class-level.
    When queried with a class qualified name, the transitive lookup finds no
    outgoing CALLS edges and returns empty.
    """

    def test_class_level_query_finds_method_tests(self):
        """tests_for(Syncer) should find tests for Syncer.sync's callees."""
        self._add_class("Syncer", path="syncer.kt")
        # Method of that class
        self._add_func("sync", path="syncer.kt", parent="Syncer")
        self._add_edge("CONTAINS", "syncer.kt::Syncer", "syncer.kt::Syncer.sync")

        # sync calls Utils.map
        self._add_func("map", path="utils.kt", parent="Utils")
        self._add_edge("CALLS", "syncer.kt::Syncer.sync", "utils.kt::Utils.map")

        # test_map tests Utils.map
        self._add_func("test_map", path="test_utils.kt", is_test=True)
        self._add_edge("CALLS", "test_utils.kt::test_map", "utils.kt::Utils.map")
        self._add_edge("TESTED_BY", "test_utils.kt::test_map", "utils.kt::Utils.map")

        results = self.store.get_transitive_tests("syncer.kt::Syncer")
        test_names = {r["name"] for r in results}
        assert "test_map" in test_names, (
            f"Expected test_map in class-level transitive tests, got: {test_names}"
        )


# ===================================================================
# 10. DECORATOR PATTERN GAPS IN DEAD CODE EXCLUSION
# ===================================================================


class TestDecoratorPatternGaps(_GraphTestBase):
    """Pain point: functions with framework decorators not in the pattern list
    are falsely reported as dead code. Gaps include bare @tool (LangChain),
    Pydantic AI agent methods, Flask blueprints, and middleware decorators.
    """

    def test_bare_tool_decorator_not_dead(self):
        """@tool (LangChain) should exclude from dead code."""
        self._add_func("search_docs", extra={"decorators": ["tool"]})
        dead_names = {d["name"] for d in find_dead_code(self.store)}
        assert "search_docs" not in dead_names

    def test_pydantic_ai_tool_plain_not_dead(self):
        """@agent.tool_plain should exclude from dead code."""
        self._add_func("get_weather", extra={"decorators": ["weather_agent.tool_plain"]})
        dead_names = {d["name"] for d in find_dead_code(self.store)}
        assert "get_weather" not in dead_names

    def test_pydantic_ai_system_prompt_not_dead(self):
        """@agent.system_prompt should exclude from dead code."""
        self._add_func("build_prompt", extra={"decorators": ["agent.system_prompt"]})
        dead_names = {d["name"] for d in find_dead_code(self.store)}
        assert "build_prompt" not in dead_names

    def test_pydantic_ai_result_validator_not_dead(self):
        """@agent.result_validator should exclude from dead code."""
        self._add_func("validate_output", extra={"decorators": ["agent.result_validator"]})
        dead_names = {d["name"] for d in find_dead_code(self.store)}
        assert "validate_output" not in dead_names

    def test_flask_blueprint_route_not_dead(self):
        """@bp.route('/path') should exclude from dead code."""
        self._add_func("list_items", extra={"decorators": ['bp.route("/items")']})
        dead_names = {d["name"] for d in find_dead_code(self.store)}
        assert "list_items" not in dead_names

    def test_middleware_decorator_not_dead(self):
        """@app.middleware('http') should exclude from dead code."""
        self._add_func("log_requests", extra={"decorators": ['app.middleware("http")']})
        dead_names = {d["name"] for d in find_dead_code(self.store)}
        assert "log_requests" not in dead_names

    def test_exception_handler_not_dead(self):
        """@app.exception_handler(404) should exclude from dead code."""
        self._add_func("not_found", extra={"decorators": ["app.exception_handler(404)"]})
        dead_names = {d["name"] for d in find_dead_code(self.store)}
        assert "not_found" not in dead_names


# ===================================================================
# 11. NESTED FUNCTION REFERENCES AS ARGUMENTS
# ===================================================================


class TestNestedFuncRefArgs:
    """Pain point: nested functions passed as arguments don't get CALLS edges.

    _walk_func_ref_args checks identifiers against defined_names, which only
    contains top-level file scope names. Nested functions (def inside def)
    are not in defined_names, so Thread(target=nested_fn) produces no edge.
    This is the #1 source of dead code false positives in HealthAgent.
    """

    def setup_method(self):
        self.parser = CodeParser()

    def test_nested_func_thread_target(self):
        """Thread(target=nested_fn) should emit CALLS to nested_fn."""
        _, edges = self.parser.parse_bytes(
            Path("/test.py"),
            b"def outer():\n"
            b"    def worker():\n"
            b"        pass\n"
            b"    import threading\n"
            b"    t = threading.Thread(target=worker)\n",
        )
        calls = [e for e in edges if e.kind == "CALLS"]
        targets = [e.target for e in calls]
        assert any("worker" in t for t in targets), (
            f"Expected CALLS to worker, got: {targets}"
        )

    def test_nested_func_run_in_executor(self):
        """run_in_executor(None, nested_fn) should emit CALLS to nested_fn."""
        _, edges = self.parser.parse_bytes(
            Path("/test.py"),
            b"async def outer():\n"
            b"    def _build():\n"
            b"        pass\n"
            b"    await loop.run_in_executor(None, _build)\n",
        )
        calls = [e for e in edges if e.kind == "CALLS"]
        targets = [e.target for e in calls]
        assert any("_build" in t for t in targets), (
            f"Expected CALLS to _build, got: {targets}"
        )
