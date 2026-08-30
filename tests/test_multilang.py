"""Tests for Go, Rust, Java, C, C++, C#, Ruby, PHP, Kotlin, Swift, Solidity, and Vue parsing."""

from pathlib import Path

import pytest

from code_review_graph.parser import CodeParser

FIXTURES = Path(__file__).parent / "fixtures"


class TestGoParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample_go.go")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("main.go")) == "go"

    def test_finds_structs_and_interfaces(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "User" in names
        assert "InMemoryRepo" in names
        assert "UserRepository" in names

    def test_finds_functions(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "NewInMemoryRepo" in names
        assert "CreateUser" in names

    def test_finds_imports(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = {e.target for e in imports}
        assert "errors" in targets
        assert "fmt" in targets

    def test_finds_calls(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        assert len(calls) >= 1

    def test_finds_contains(self):
        contains = [e for e in self.edges if e.kind == "CONTAINS"]
        assert len(contains) >= 3

    def test_methods_attached_to_receiver(self):
        """Go methods should be attached to their receiver type (#190).

        `func (r *InMemoryRepo) FindByID(...)` should produce a Function node
        with parent_name='InMemoryRepo' and a CONTAINS edge from the type to
        the method, so `inheritors_of`/`query_graph` can find methods via the
        struct they belong to.
        """
        funcs = [n for n in self.nodes if n.kind == "Function"]
        find_by_id = next(f for f in funcs if f.name == "FindByID")
        save = next(
            f for f in funcs
            if f.name == "Save" and f.parent_name == "InMemoryRepo"
        )
        assert find_by_id.parent_name == "InMemoryRepo"
        assert save.parent_name == "InMemoryRepo"
        # Free functions should still have no parent.
        assert next(f for f in funcs if f.name == "NewInMemoryRepo").parent_name is None
        assert next(f for f in funcs if f.name == "CreateUser").parent_name is None

        contains = [(e.source, e.target) for e in self.edges if e.kind == "CONTAINS"]
        find_by_id_contains = [
            (s, t) for (s, t) in contains
            if t.endswith("::InMemoryRepo.FindByID")
        ]
        save_contains = [
            (s, t) for (s, t) in contains
            if t.endswith("::InMemoryRepo.Save")
        ]
        assert find_by_id_contains, (
            f"no CONTAINS edge for InMemoryRepo.FindByID in {contains}"
        )
        assert save_contains, (
            f"no CONTAINS edge for InMemoryRepo.Save in {contains}"
        )
        # Source of each CONTAINS should be the InMemoryRepo type,
        # not the file path.
        assert find_by_id_contains[0][0].endswith("::InMemoryRepo")
        assert save_contains[0][0].endswith("::InMemoryRepo")

    def test_receiver_call_resolves_to_receiver_method(self):
        calls = [
            edge for edge in self.edges
            if edge.kind == "CALLS"
            and edge.source.endswith("::InMemoryRepo.SaveAndReturn")
        ]
        assert len(calls) == 1
        assert calls[0].target.endswith("::InMemoryRepo.Save")
        assert calls[0].extra["receiver"] == "r"

    @pytest.mark.parametrize(
        ("method_name", "call_count"),
        [
            ("CallsShadowedReceiver", 1),
            ("CallsBlockShadowedReceiver", 1),
            ("CallsVarShadowedReceiver", 1),
            ("CallsRangeShadowedReceiver", 1),
            ("CallsForClauseShadowedReceiver", 3),
            ("CallsTypeSwitchShadowedReceiver", 1),
            ("CallsExpressionCaseShadowedReceiver", 1),
            ("CallsSelectCaseShadowedReceiver", 1),
            ("CallsNamedResultShadowedReceiver", 1),
        ],
    )
    def test_shadowed_receiver_call_stays_unresolved(
        self, method_name, call_count,
    ):
        calls = [
            edge for edge in self.edges
            if edge.kind == "CALLS"
            and edge.source.endswith(f"::ShadowA.{method_name}")
            and edge.extra.get("receiver") == "a"
        ]
        assert len(calls) == call_count
        assert all(edge.target == "Save" for edge in calls)
        assert all("go_method_receiver" not in edge.extra for edge in calls)

    def test_receiver_call_resolves_after_shadowing_scope(self):
        calls = [
            edge for edge in self.edges
            if edge.kind == "CALLS"
            and edge.source.endswith("::ShadowA.CallsAfterShadowScope")
            and edge.extra.get("receiver") == "a"
        ]
        assert [edge.target.endswith("::ShadowA.Save") for edge in calls] == [
            False, True,
        ]

    def test_initializer_uses_receiver_before_shadowing(self):
        calls = [
            edge for edge in self.edges
            if edge.kind == "CALLS"
            and edge.source.endswith("::ShadowA.CallsInitializerScope")
            and edge.extra.get("receiver") == "a"
        ]
        assert [edge.target.endswith("::ShadowA.Save") for edge in calls] == [
            True, False,
        ]

    def test_same_scope_redeclaration_keeps_receiver_resolution(self):
        calls = [
            edge for edge in self.edges
            if edge.kind == "CALLS"
            and edge.source.endswith("::ShadowA.CallsSameScopeRedeclaration")
            and edge.extra.get("receiver") == "a"
        ]
        assert len(calls) == 1
        assert calls[0].target.endswith("::ShadowA.Save")
        assert calls[0].extra["go_method_receiver"] is True

    @pytest.mark.parametrize(
        ("method_name", "receiver_resolution"),
        [
            ("CallsTypeSwitchInitShadowedReceiver", [True, False, True]),
            ("CallsSelectReceiveShadowedReceiver", [True, False, True]),
            ("CallsConstShadowedReceiver", [False, True]),
            ("CallsTypeShadowedReceiver", [False, True]),
        ],
    )
    def test_receiver_bindings_follow_lexical_lifetime(
        self, method_name, receiver_resolution,
    ):
        calls = [
            edge for edge in self.edges
            if edge.kind == "CALLS"
            and edge.source.endswith(f"::ShadowA.{method_name}")
            and edge.extra.get("receiver") == "a"
        ]
        assert [
            edge.target.endswith("::ShadowA.Save") for edge in calls
        ] == receiver_resolution

    def test_deep_receiver_scope_walk_does_not_overflow(self):
        source = (
            "package auth\n"
            "type ShadowA struct{}\n"
            "func (a *ShadowA) Save() {}\n"
            "func (a *ShadowA) Deep() {\n"
            + "(" * 1200
            + "a.Save()"
            + ")" * 1200
            + "\n}\n"
        ).encode()
        root = self.parser._get_parser("go").parse(source).root_node
        stack = [root]
        method = None
        call = None
        while stack:
            current = stack.pop()
            if (
                current.type == "method_declaration"
                and current.child_by_field_name("name").text == b"Deep"
            ):
                method = current
            if current.type == "call_expression":
                call = current
            stack.extend(current.children)
        assert method is not None
        assert call is not None
        bindings = self.parser._go_receiver_binding_index(method, "a")
        assert not self.parser._go_receiver_is_shadowed(call, bindings)

    def test_receiver_binding_prepass_runs_once_per_method(self, monkeypatch):
        builds = []
        original = CodeParser._go_receiver_binding_index

        def counted(parser, method, receiver_name):
            name = method.child_by_field_name("name").text.decode()
            if name == "ManyCalls":
                builds.append(name)
            return original(parser, method, receiver_name)

        monkeypatch.setattr(CodeParser, "_go_receiver_binding_index", counted)
        source = (
            "package auth\n"
            "type ShadowA struct{}\n"
            "func (a *ShadowA) Save() {}\n"
            "func (a *ShadowA) ManyCalls() {\n"
            + "\n".join("a.Save()" for _ in range(200))
            + "\n}\n"
        ).encode()
        _, edges = CodeParser().parse_bytes(Path("many_calls.go"), source)
        calls = [
            edge for edge in edges
            if edge.kind == "CALLS" and edge.source.endswith("::ShadowA.ManyCalls")
        ]
        assert len(calls) == 200
        assert builds == ["ManyCalls"]

    def test_generic_methods_attached_to_receiver(self):
        nodes, edges = self.parser.parse_bytes(
            Path("generic.go"),
            b"""package sample
type Box[T any] struct{}
func (b Box[T]) Value() {}
func (b *Box[T]) Pointer() {}
""",
        )

        parents = {
            node.name: node.parent_name
            for node in nodes
            if node.kind == "Function"
        }
        assert parents == {"Value": "Box", "Pointer": "Box"}
        assert {
            edge.target.rsplit("::", 1)[-1]
            for edge in edges
            if edge.kind == "CONTAINS" and edge.source.endswith("::Box")
        } == {"Box.Value", "Box.Pointer"}


class TestRustParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample_rust.rs")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("lib.rs")) == "rust"

    def test_finds_structs_and_traits(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "User" in names
        assert "InMemoryRepo" in names

    def test_finds_functions(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "new" in names
        assert "create_user" in names
        assert "find_by_id" in names
        assert "save" in names

    def test_finds_imports(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        assert len(imports) >= 1

    def test_finds_calls(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        assert len(calls) >= 3

    def test_detects_test_attribute(self):
        tests = [n for n in self.nodes if n.kind == "Test"]
        names = {t.name for t in tests}
        assert "new_repo_is_empty" in names
        assert "create_user_saves_to_repo" in names
        assert all(t.is_test for t in tests)

    def test_detects_tokio_test_attribute(self):
        tests = {n.name for n in self.nodes if n.kind == "Test"}
        assert "async_test_is_detected" in tests

    def test_non_test_functions_not_misclassified(self):
        funcs = {n.name for n in self.nodes if n.kind == "Function"}
        assert "create_user" in funcs
        assert "new" in funcs
        # `create_user` carries no `#[test]` — must stay Function.
        for n in self.nodes:
            if n.name == "create_user":
                assert not n.is_test


class TestJavaParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "SampleJava.java")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("Main.java")) == "java"

    def test_finds_classes_and_interfaces(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "UserRepository" in names
        assert "User" in names
        assert "InMemoryRepo" in names
        assert "UserService" in names

    def test_finds_methods(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "findById" in names
        assert "save" in names
        assert "getUser" in names

    def test_method_names_not_return_types(self):
        """Method names must be the actual name, not the return type.

        tree-sitter-java puts type_identifier (return type) before
        identifier (method name).  Without the Java-specific branch in
        _get_name the generic loop picks up the return type instead.
        """
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        # getName()/getEmail() return String — must not be indexed as "String"
        assert "getName" in names
        assert "getEmail" in names
        assert "getId" in names
        # createUser() returns User — must not be indexed as "User" (the class)
        assert "createUser" in names

    def test_finds_imports(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        assert len(imports) >= 2

    def test_finds_inheritance(self):
        inherits = [e for e in self.edges if e.kind == "INHERITS"]
        # InMemoryRepo implements UserRepository + CachedRepo extends InMemoryRepo
        assert len(inherits) >= 2
        targets = {e.target for e in inherits}
        assert "UserRepository" in targets
        assert "InMemoryRepo" in targets

    def test_inheritance_target_is_bare_name(self):
        """INHERITS edge target must be the type name, not 'implements Foo'.

        tree-sitter-java wraps extends/implements in superclass and
        super_interfaces nodes whose .text includes the keyword.
        Without the Java-specific branch in _get_bases the full text
        (e.g. 'implements UserRepository') is stored as the edge target.
        """
        inherits = [e for e in self.edges if e.kind == "INHERITS"]
        # Must have both extends and implements edges to test both paths
        assert len(inherits) >= 2, (
            "Expected at least 2 INHERITS edges (extends + implements)"
        )
        for e in inherits:
            assert not e.target.startswith("implements "), (
                f"INHERITS target should be bare type name, got: {e.target!r}"
            )
            assert not e.target.startswith("extends "), (
                f"INHERITS target should be bare type name, got: {e.target!r}"
            )

    def test_finds_calls(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        assert len(calls) >= 3


class TestJavaMethodNames:
    """Regression tests for #804: Java method/constructor names come from the
    grammar ``name`` field (same approach as C# after #794), not the first
    ``identifier`` child walk.
    """

    def _parse(self, source: str, tmp_path):
        p = tmp_path / "x.java"
        p.write_text(source, encoding="utf-8")
        return CodeParser().parse_file(p)

    def test_methods_and_constructors_use_name_field(self, tmp_path):
        nodes, _ = self._parse(
            "public class Suite {\n"
            "    public Suite() { }\n"
            "    public String getLabel() { return \"\"; }\n"
            "    public User createUser() { return null; }\n"
            "    public void plainVoid() { }\n"
            "    public List<String> genericReturn() { return null; }\n"
            "}\n",
            tmp_path,
        )
        funcs = [n for n in nodes if n.kind == "Function"]
        assert {f.name for f in funcs} == {
            "Suite",
            "getLabel",
            "createUser",
            "plainVoid",
            "genericReturn",
        }
        assert len(funcs) == 5
        assert "String" not in {f.name for f in funcs}
        assert "User" not in {f.name for f in funcs}
        assert "List" not in {f.name for f in funcs}


class TestJavaImportResolution:
    """Test that Java imports are resolved to absolute file paths."""

    def test_resolves_project_import(self, tmp_path):
        """Import of a project class resolves to its .java file."""
        # Create a mini Java project with two packages
        auth = tmp_path / "src/main/java/com/example/auth"
        auth.mkdir(parents=True)
        (auth / "User.java").write_text(
            "package com.example.auth;\npublic class User {}\n"
        )
        svc = tmp_path / "src/main/java/com/example/service"
        svc.mkdir(parents=True)
        (svc / "App.java").write_text(
            "package com.example.service;\n"
            "import com.example.auth.User;\n"
            "public class App {}\n"
        )

        parser = CodeParser()
        _, edges = parser.parse_file(svc / "App.java")
        imports = [e for e in edges if e.kind == "IMPORTS_FROM"]
        assert len(imports) == 1
        assert imports[0].target == (auth / "User.java").resolve().as_posix()

    def test_jdk_import_stays_unresolved(self):
        """JDK imports have no local file and remain as raw strings."""
        parser = CodeParser()
        _, edges = parser.parse_file(FIXTURES / "SampleJava.java")
        imports = [e for e in edges if e.kind == "IMPORTS_FROM"]
        # All imports in SampleJava.java are java.util.* (JDK)
        for e in imports:
            assert not e.target.endswith(".java"), (
                f"JDK import should not resolve to a file: {e.target!r}"
            )

    def test_static_import_resolves_to_class(self, tmp_path):
        """Static import of a member resolves to the enclosing class file."""
        pkg = tmp_path / "src/main/java/com/example/util"
        pkg.mkdir(parents=True)
        (pkg / "Helper.java").write_text(
            "package com.example.util;\n"
            "public class Helper { public static int MAX = 1; }\n"
        )
        app_dir = tmp_path / "src/main/java/com/example/app"
        app_dir.mkdir(parents=True)
        (app_dir / "App.java").write_text(
            "package com.example.app;\n"
            "import static com.example.util.Helper.MAX;\n"
            "public class App {}\n"
        )

        parser = CodeParser()
        _, edges = parser.parse_file(app_dir / "App.java")
        imports = [e for e in edges if e.kind == "IMPORTS_FROM"]
        assert len(imports) == 1
        assert imports[0].target == (pkg / "Helper.java").resolve().as_posix()

    def test_wildcard_import_stays_unresolved(self, tmp_path):
        """Wildcard imports cannot resolve to a single file."""
        app_dir = tmp_path / "src/main/java/com/example"
        app_dir.mkdir(parents=True)
        (app_dir / "App.java").write_text(
            "package com.example;\n"
            "import java.util.*;\n"
            "public class App {}\n"
        )

        parser = CodeParser()
        _, edges = parser.parse_file(app_dir / "App.java")
        imports = [e for e in edges if e.kind == "IMPORTS_FROM"]
        assert len(imports) == 1
        assert imports[0].target == "java.util.*"


class TestCParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample.c")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("main.c")) == "c"

    def test_finds_structs(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "User" in names

    def test_finds_functions(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "print_user" in names
        assert "main" in names
        assert "create_user" in names

    def test_finds_imports(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = {e.target for e in imports}
        assert "stdio.h" in targets


class TestCppParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample.cpp")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("main.cpp")) == "cpp"

    def test_finds_classes(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "Animal" in names
        assert "Dog" in names

    def test_finds_functions(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "greet" in names or "main" in names

    def test_finds_inheritance(self):
        inherits = [e for e in self.edges if e.kind == "INHERITS"]
        assert len(inherits) >= 1


class TestHhParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample.hh")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("types.hh")) == "cpp"

    def test_finds_classes(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "Shape" in names
        assert "Circle" in names

    def test_finds_functions(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "perimeter" in names

    def test_finds_inheritance(self):
        inherits = [e for e in self.edges if e.kind == "INHERITS"]
        assert len(inherits) >= 1


def _has_csharp_parser():
    try:
        import tree_sitter_language_pack as tslp
        tslp.get_parser("csharp")
        return True
    except (LookupError, ImportError):
        return False


@pytest.mark.skipif(not _has_csharp_parser(), reason="csharp tree-sitter grammar not installed")
class TestCSharpParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "Sample.cs")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("Program.cs")) == "csharp"

    def test_finds_classes_and_interfaces(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "User" in names
        assert "InMemoryRepo" in names

    def test_finds_methods(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "FindById" in names or "Save" in names

    def test_finds_inheritance(self):
        inherits = [e for e in self.edges if e.kind == "INHERITS"]
        targets = {e.target for e in inherits}
        assert "IRepository" in targets
        assert "InMemoryRepo" in targets
        assert "System.IDisposable" in targets
        assert "List<User>" in targets
        assert all(not e.target.startswith(":") for e in inherits)
        assert all("," not in e.target for e in inherits)

    def test_inheritance_hard_cases(self):
        inherits = [e for e in self.edges if e.kind == "INHERITS"]
        by_source = {}
        for edge in inherits:
            by_source.setdefault(edge.source.rsplit("::", 1)[-1], set()).add(
                edge.target
            )

        assert by_source.get("AuditedUser") == {"User", "IRepository"}
        assert by_source.get("TaggedUser") == {"User"}
        assert "IRepository" in by_source.get("Token", set())
        assert "System.Collections.Generic.List<User>" in {
            edge.target for edge in inherits
        }
        assert "ConstrainedHolder" not in by_source
        assert by_source.get("SeededRepo") == {"InMemoryRepo"}
        assert all(not edge.target.startswith("(") for edge in inherits)
        assert "Status" not in by_source
        assert "byte" not in {edge.target for edge in inherits}

    def test_nested_types_keep_complete_identity_and_containment(self, tmp_path):
        path = tmp_path / "Nested.cs"
        path.write_text(
            "public class Details {\n"
            "    public class QueryHandler { public void Handle() {} }\n"
            "}\n"
            "public class Edit {\n"
            "    public class QueryHandler { public void Handle() {} }\n"
            "}\n"
            "public class A {\n"
            "    public class B {\n"
            "        public class C { public void M() {} }\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )

        nodes, edges = self.parser.parse_file(path)
        prefix = path.as_posix()
        qualified = {
            self.parser._node_qualified(node)
            for node in nodes
            if node.kind != "File"
        }
        assert {
            f"{prefix}::Details.QueryHandler.Handle",
            f"{prefix}::Edit.QueryHandler.Handle",
            f"{prefix}::A.B.C",
            f"{prefix}::A.B.C.M",
        } <= qualified

        contains = {
            (edge.source, edge.target)
            for edge in edges
            if edge.kind == "CONTAINS"
        }
        assert {
            (f"{prefix}::Details", f"{prefix}::Details.QueryHandler"),
            (
                f"{prefix}::Details.QueryHandler",
                f"{prefix}::Details.QueryHandler.Handle",
            ),
            (f"{prefix}::Edit", f"{prefix}::Edit.QueryHandler"),
            (
                f"{prefix}::Edit.QueryHandler",
                f"{prefix}::Edit.QueryHandler.Handle",
            ),
            (f"{prefix}::A", f"{prefix}::A.B"),
            (f"{prefix}::A.B", f"{prefix}::A.B.C"),
            (f"{prefix}::A.B.C", f"{prefix}::A.B.C.M"),
        } <= contains
        assert all(source == prefix or source in qualified for source, _ in contains)

        from code_review_graph.graph import GraphStore
        from code_review_graph.tools.query import query_graph

        graph_dir = tmp_path / ".code-review-graph"
        graph_dir.mkdir()
        store = GraphStore(graph_dir / "graph.db")
        try:
            store.store_file_nodes_edges(str(path), nodes, edges)
        finally:
            store.close()

        # Addressed by qualified name; short-name lookup of a dotted nested
        # path is a separate query-resolution change.
        result = query_graph(
            "children_of",
            f"{prefix}::Details.QueryHandler",
            repo_root=str(tmp_path),
        )
        assert result["status"] == "ok"
        assert {child["name"] for child in result["results"]} == {"Handle"}

    def test_incremental_upgrade_rebuilds_legacy_nested_identities(self, tmp_path):
        from unittest.mock import patch

        from code_review_graph.graph import GraphStore
        from code_review_graph.incremental import (
            CSHARP_IDENTITY_VERSION,
            incremental_update,
        )
        from code_review_graph.parser import NodeInfo

        path = tmp_path / "Nested.cs"
        path.write_text(
            "public class Details {\n"
            "    public class QueryHandler { public void Handle() {} }\n"
            "}\n"
            "public class Edit {\n"
            "    public class QueryHandler { public void Handle() {} }\n"
            "}\n",
            encoding="utf-8",
        )
        store = GraphStore(tmp_path / "graph.db")
        legacy = f"{path.as_posix()}::QueryHandler.Handle"
        try:
            store.upsert_node(NodeInfo(
                kind="Function",
                name="Handle",
                file_path=str(path),
                line_start=2,
                line_end=2,
                language="csharp",
                parent_name="QueryHandler",
            ))
            store.commit()

            with patch(
                "code_review_graph.incremental.get_all_tracked_files",
                return_value=["Nested.cs"],
            ):
                result = incremental_update(tmp_path, store, changed_files=[])

            assert result["identity_rebuild"] is True
            assert store.get_metadata("csharp_identity_version") == (
                CSHARP_IDENTITY_VERSION
            )
            assert store.get_node(legacy) is None
            assert store.get_node(
                f"{path.as_posix()}::Details.QueryHandler.Handle"
            ) is not None
            assert store.get_node(
                f"{path.as_posix()}::Edit.QueryHandler.Handle"
            ) is not None
        finally:
            store.close()

    @pytest.mark.parametrize(
        ("statement", "expected_targets"),
        [
            ("Ping();", {"Ping"}),
            ("service.Send();", {"Send"}),
            ("service.GetClient().Fetch();", {"GetClient", "Fetch"}),
            ("service?.Notify();", {"Notify"}),
        ],
        ids=("bare", "member", "chained", "null-conditional"),
    )
    def test_finds_calls_and_attributes_them_to_enclosing_method(
        self, tmp_path, statement, expected_targets,
    ):
        source_file = tmp_path / "Calls.cs"
        source_file.write_text(
            "class Caller\n"
            "{\n"
            "    void Run()\n"
            "    {\n"
            f"        {statement}\n"
            "    }\n"
            "}\n"
        )

        _, edges = self.parser.parse_file(source_file)
        calls = [edge for edge in edges if edge.kind == "CALLS"]
        call_targets = {
            edge.target.split("::")[-1].split(".")[-1]: edge
            for edge in calls
        }

        assert expected_targets <= call_targets.keys()
        assert all(
            call_targets[target].source.endswith("::Caller.Run")
            for target in expected_targets
        )


@pytest.mark.skipif(
    not _has_csharp_parser(), reason="csharp tree-sitter grammar not installed",
)
class TestCSharpMethodNames:
    """Regression tests for #791: a non-generic C# return type is itself an
    ``identifier``, so ``async Task Foo()`` was named ``Task`` and every such
    method in a class merged onto one ``qualified_name``.
    """

    def _parse(self, source: str, tmp_path):
        p = tmp_path / "x.cs"
        p.write_text(source, encoding="utf-8")
        return CodeParser().parse_file(p)

    def test_non_generic_return_type_is_not_the_name(self, tmp_path):
        nodes, _ = self._parse(
            "public class Suite {\n"
            "    public async Task Should_do_thing() { }\n"
            "    public async Task Should_do_other_thing() { }\n"
            "    public async Task<int> Returns_generic() { return 1; }\n"
            "    public void PlainVoid() { }\n"
            "    public Suite() { }\n"
            "}\n",
            tmp_path,
        )
        funcs = [n for n in nodes if n.kind == "Function"]
        assert {f.name for f in funcs} == {
            "Should_do_thing",
            "Should_do_other_thing",
            "Returns_generic",
            "PlainVoid",
            "Suite",
        }
        assert len(funcs) == 5


@pytest.mark.skipif(
    not _has_csharp_parser(), reason="csharp tree-sitter grammar not installed",
)
class TestCSharpAttributes:
    """Regression tests for #295 (C# half): C# attributes use
    ``attribute_list`` nodes, not ``modifiers > annotation``, so they need
    a dedicated capture path. Persisted in ``modifiers`` + ``extra['decorators']``.
    """

    def _parse(self, source: str, tmp_path):
        p = tmp_path / "x.cs"
        p.write_text(source, encoding="utf-8")
        return CodeParser().parse_file(p)

    def test_method_attributes_captured(self, tmp_path):
        nodes, _ = self._parse(
            "namespace Api;\npublic class Ctrl {\n"
            "    [HttpGet(\"/x\")]\n    [Authorize]\n"
            "    public void Get() {}\n}\n",
            tmp_path,
        )
        get = next(n for n in nodes if n.kind == "Function" and n.name == "Get")
        assert get.extra.get("decorators") == ["HttpGet", "Authorize"]
        assert get.modifiers == "HttpGet,Authorize"

    def test_class_attribute_captured(self, tmp_path):
        nodes, _ = self._parse(
            "namespace Api;\n[ApiController]\npublic class Ctrl {\n"
            "    public void Get() {}\n}\n",
            tmp_path,
        )
        ctrl = next(n for n in nodes if n.kind == "Class" and n.name == "Ctrl")
        assert ctrl.extra.get("decorators") == ["ApiController"]
        assert ctrl.modifiers == "ApiController"

    def test_unattributed_method_has_none_modifiers(self, tmp_path):
        nodes, _ = self._parse(
            "namespace Api;\npublic class C {\n    public void Plain() {}\n}\n",
            tmp_path,
        )
        plain = next(n for n in nodes if n.kind == "Function" and n.name == "Plain")
        assert plain.modifiers is None
        assert "decorators" not in plain.extra


@pytest.mark.skipif(
    not _has_csharp_parser(), reason="csharp tree-sitter grammar not installed",
)
class TestCSharpNamespaceResolution:
    """Regression tests for #310: C# ``using X.Y;`` directives carry a
    namespace string as their ``IMPORTS_FROM.target`` (not a file path), so
    ``importers_of`` returned [] for every .cs file. The fix tags File
    nodes with their declared namespaces and adds a namespace fallback.
    """

    def _write(self, path: Path, source: str) -> None:
        path.write_text(source, encoding="utf-8")

    def test_file_scoped_namespace_tagged(self, tmp_path):
        f = tmp_path / "Core.cs"
        self._write(f, "namespace ACME.Core;\npublic class TaskBoard {}\n")
        nodes, _ = CodeParser().parse_file(f)
        file_node = next(n for n in nodes if n.kind == "File")
        assert file_node.extra.get("csharp_namespaces") == ["ACME.Core"]

    def test_block_namespace_tagged(self, tmp_path):
        f = tmp_path / "Core.cs"
        self._write(f, "namespace ACME.Core {\n    public class T {}\n}\n")
        nodes, _ = CodeParser().parse_file(f)
        file_node = next(n for n in nodes if n.kind == "File")
        assert file_node.extra.get("csharp_namespaces") == ["ACME.Core"]

    def test_non_csharp_file_has_no_namespace_tag(self, tmp_path):
        f = tmp_path / "mod.py"
        self._write(f, "def foo():\n    pass\n")
        nodes, _ = CodeParser().parse_file(f)
        file_node = next(n for n in nodes if n.kind == "File")
        assert "csharp_namespaces" not in file_node.extra

    def test_importers_of_resolves_namespace_to_file(self, tmp_path):
        from code_review_graph.graph import GraphStore
        from code_review_graph.tools.query import query_graph

        (tmp_path / ".git").mkdir()
        (tmp_path / ".code-review-graph").mkdir()
        core = tmp_path / "Core.cs"
        self._write(core, "namespace ACME.Core;\npublic class TaskBoard {}\n")
        app = tmp_path / "App.cs"
        self._write(app, "using ACME.Core;\nnamespace ACME.App;\npublic class App {}\n")
        unrelated = tmp_path / "Unrelated.cs"
        self._write(
            unrelated,
            "using System.Linq;\nnamespace ACME.Other;\npublic class Other {}\n",
        )

        store = GraphStore(tmp_path / ".code-review-graph" / "graph.db")
        parser = CodeParser()
        for path in (core, app, unrelated):
            nodes, edges = parser.parse_file(path)
            for n in nodes:
                store.upsert_node(n)
            for e in edges:
                store.upsert_edge(e)
        store.commit()
        store.close()

        result = query_graph("importers_of", str(core), repo_root=str(tmp_path))
        assert result.get("status") == "ok"
        importers = {r["file"] for r in result.get("results", [])}
        assert app.as_posix() in importers
        assert unrelated.as_posix() not in importers

    def test_importers_of_resolves_nested_block_namespace(self, tmp_path):
        from code_review_graph.graph import GraphStore
        from code_review_graph.tools.query import query_graph

        (tmp_path / ".git").mkdir()
        (tmp_path / ".code-review-graph").mkdir()
        core = tmp_path / "Core.cs"
        self._write(
            core,
            "namespace Acme {\n"
            "    namespace Core {\n"
            "        public class TaskBoard {}\n"
            "    }\n"
            "}\n",
        )
        app = tmp_path / "App.cs"
        self._write(
            app,
            "using Acme.Core;\n"
            "namespace Acme.App;\n"
            "public class App {}\n",
        )

        store = GraphStore(tmp_path / ".code-review-graph" / "graph.db")
        parser = CodeParser()
        for path in (core, app):
            nodes, edges = parser.parse_file(path)
            for node in nodes:
                store.upsert_node(node)
            for edge in edges:
                store.upsert_edge(edge)
        store.commit()
        store.close()

        result = query_graph("importers_of", str(core), repo_root=str(tmp_path))
        assert result.get("status") == "ok"
        importers = {r["file"] for r in result.get("results", [])}
        assert app.as_posix() in importers

    def test_deep_ast_preserves_nested_namespace_metadata(self, tmp_path):
        """Namespace discovery must not recurse through the whole C# AST."""
        source_file = tmp_path / "Deep.cs"
        deep_expression = "(" * 1200 + "1" + ")" * 1200
        self._write(
            source_file,
            "namespace Acme {\n"
            "    namespace Core {\n"
            "        public class Calculator {\n"
            "            public int Value() {\n"
            f"                return {deep_expression};\n"
            "            }\n"
            "        }\n"
            "    }\n"
            "}\n",
        )

        try:
            nodes, _ = CodeParser().parse_file(source_file)
        except RecursionError:
            pytest.fail("C# namespace discovery overflowed on a deep expression AST")

        file_node = next(node for node in nodes if node.kind == "File")
        assert file_node.extra.get("csharp_namespaces") == [
            "Acme",
            "Acme.Core",
        ]


@pytest.mark.skipif(
    not _has_csharp_parser(), reason="csharp tree-sitter grammar not installed",
)
class TestCSharpReceiverCallResolution:
    """Regression tests for #612: C# receiver calls (``Service.StaticCall()``,
    ``obj.InstanceCall()``, ``obj?.ConditionalCall()``) were extracted but every
    call target stayed a bare unresolved name, so ``callers_of`` marked callers
    unresolved and ``get_impact_radius`` reported zero impacted nodes/files for
    the callee's file. These tests run the full build pipeline (``full_build``
    plus ``run_post_processing``) on a multi-file fixture and assert on
    built-graph query results, not parse-time output.
    """

    SERVICE = (
        "namespace Acme.Services;\n"
        "\n"
        "public class Service\n"
        "{\n"
        "    public static void StaticCall() { }\n"
        "    public void InstanceCall() { }\n"
        "    public void ConditionalCall() { }\n"
        "}\n"
    )
    CONSUMER = (
        "using Acme.Services;\n"
        "\n"
        "namespace Acme.App;\n"
        "\n"
        "public class Consumer\n"
        "{\n"
        "    public void Run()\n"
        "    {\n"
        "        Service.StaticCall();\n"
        "        var obj = new Service();\n"
        "        obj.InstanceCall();\n"
        "        Service typed = obj;\n"
        "        typed.InstanceCall();\n"
        "        obj?.ConditionalCall();\n"
        "    }\n"
        "}\n"
    )
    # Same-file resolution: two classes in one file, one calling the other.
    SINGLE = (
        "namespace Acme.Single;\n"
        "\n"
        "public class Widget\n"
        "{\n"
        "    public static void Spin() { }\n"
        "}\n"
        "\n"
        "public class Runner\n"
        "{\n"
        "    public void Go()\n"
        "    {\n"
        "        Widget.Spin();\n"
        "    }\n"
        "}\n"
    )
    # Decoy classes with identical class/method names in an unrelated
    # namespace: resolution must use receiver + namespace evidence, not
    # graph-wide name uniqueness.
    DECOY = (
        "namespace Other.Zone;\n"
        "\n"
        "public class Widget\n"
        "{\n"
        "    public static void Spin() { }\n"
        "}\n"
        "\n"
        "public class Service\n"
        "{\n"
        "    public static void StaticCall() { }\n"
        "    public void InstanceCall() { }\n"
        "    public void ConditionalCall() { }\n"
        "}\n"
    )
    TESTS = (
        "using Acme.Services;\n"
        "\n"
        "namespace Acme.Tests;\n"
        "\n"
        "public class ServiceTests\n"
        "{\n"
        "    public void TestStaticDispatch()\n"
        "    {\n"
        "        Service.StaticCall();\n"
        "    }\n"
        "}\n"
    )
    NESTED = (
        "public class Details\n"
        "{\n"
        "    public class QueryHandler { public void Handle() { } }\n"
        "}\n"
        "public class Edit\n"
        "{\n"
        "    public class QueryHandler { public void Handle() { } }\n"
        "}\n"
    )
    NESTED_CONSUMERS = (
        "public class DetailsConsumer\n"
        "{\n"
        "    Details.QueryHandler handler;\n"
        "    public void CallDetails() { handler.Handle(); }\n"
        "}\n"
        "public class EditConsumer\n"
        "{\n"
        "    Edit.QueryHandler handler;\n"
        "    public void CallEdit() { handler.Handle(); }\n"
        "}\n"
        "public class AmbiguousConsumer\n"
        "{\n"
        "    QueryHandler handler;\n"
        "    public void CallAmbiguous() { handler.Handle(); }\n"
        "}\n"
    )
    PREFIX_COLLISION = (
        "public class A\n"
        "{\n"
        "    public class B\n"
        "    {\n"
        "        public class C { public void M() { } }\n"
        "    }\n"
        "}\n"
        "public class ExactPathConsumer\n"
        "{\n"
        "    B.C value;\n"
        "    public void Call() { value.M(); }\n"
        "}\n"
        "public class LexicalOuter\n"
        "{\n"
        "    public class D\n"
        "    {\n"
        "        public class E { public void M() { } }\n"
        "    }\n"
        "    public class SuffixPathConsumer\n"
        "    {\n"
        "        D.E value;\n"
        "        public void Call() { value.M(); }\n"
        "    }\n"
        "}\n"
        "public class E { public void M() { } }\n"
    )
    WRONG_NAMESPACE_EXACT_PATH = (
        "namespace Other;\n"
        "public class Vault\n"
        "{\n"
        "    public class Archive\n"
        "    {\n"
        "        public class StoreHandler { public void Store() { } }\n"
        "    }\n"
        "}\n"
    )
    WRONG_NAMESPACE_EXACT_CONSUMER = (
        "public class ExactPathNamespaceConsumer\n"
        "{\n"
        "    Vault.Archive.StoreHandler handler;\n"
        "    public void Call() { handler.Store(); }\n"
        "}\n"
    )
    GLOBAL_USING = "global using Depot;\n"
    GLOBAL_USING_TARGET = (
        "namespace Depot;\n"
        "public class Crate\n"
        "{\n"
        "    public class PackHandler { public void Pack() { } }\n"
        "}\n"
    )
    GLOBAL_USING_CONSUMER = (
        "public class GlobalUsingConsumer\n"
        "{\n"
        "    Crate.PackHandler handler;\n"
        "    public void Call() { handler.Pack(); }\n"
        "}\n"
    )
    MULTI_NAMESPACE = (
        "namespace App\n"
        "{\n"
        "    public class SomethingElse { }\n"
        "}\n"
        "namespace Other\n"
        "{\n"
        "    public class Journal\n"
        "    {\n"
        "        public class PostHandler { public void Post() { } }\n"
        "    }\n"
        "}\n"
    )
    MULTI_NAMESPACE_CONSUMER = (
        "public class MultiNamespaceConsumer\n"
        "{\n"
        "    App.Journal.PostHandler handler;\n"
        "    public void Call() { handler.Post(); }\n"
        "}\n"
    )
    WRONG_NAMESPACE = (
        "namespace Other;\n"
        "public class Ledger\n"
        "{\n"
        "    public class AuditHandler { public void Run() { } }\n"
        "}\n"
    )
    WRONG_NAMESPACE_CONSUMER = (
        "public class WrongNamespaceConsumer\n"
        "{\n"
        "    App.Ledger.AuditHandler handler;\n"
        "    public void Call() { handler.Run(); }\n"
        "}\n"
    )
    LONE_NESTED = (
        "public class Wrapper\n"
        "{\n"
        "    public class LoneHandler { public void Handle() { } }\n"
        "}\n"
    )
    LONE_BARE_CONSUMER = (
        "using External;\n"
        "public class LoneBareConsumer\n"
        "{\n"
        "    LoneHandler handler;\n"
        "    public void Call() { handler.Handle(); }\n"
        "}\n"
    )
    EXACT_PREFIX_TARGET = (
        "public class B\n"
        "{\n"
        "    public class C { public void M() { } }\n"
        "}\n"
    )
    NAMESPACED = (
        "namespace App\n"
        "{\n"
        "    public class Report\n"
        "    {\n"
        "        public class ExportHandler { public void Run() { } }\n"
        "    }\n"
        "}\n"
    )
    NAMESPACE_SUFFIX_DECOY = (
        "public class X\n"
        "{\n"
        "    public class App\n"
        "    {\n"
        "        public class Report\n"
        "        {\n"
        "            public class ExportHandler { public void Run() { } }\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    NAMESPACED_CONSUMER = (
        "public class NamespacedConsumer\n"
        "{\n"
        "    App.Report.ExportHandler handler;\n"
        "    public void Call() { handler.Run(); }\n"
        "}\n"
    )
    LEXICAL_SHADOW = (
        "public class D\n"
        "{\n"
        "    public class E { public void M() { } }\n"
        "}\n"
        "public class Shadower\n"
        "{\n"
        "    public class D\n"
        "    {\n"
        "        public class E { public void M() { } }\n"
        "    }\n"
        "    public class Consumer\n"
        "    {\n"
        "        D.E value;\n"
        "        public void Call() { value.M(); }\n"
        "    }\n"
        "}\n"
    )

    def _build(self, tmp_path):
        from unittest.mock import patch

        from code_review_graph.graph import GraphStore
        from code_review_graph.incremental import full_build
        from code_review_graph.postprocessing import run_post_processing

        (tmp_path / ".git").mkdir()
        (tmp_path / ".code-review-graph").mkdir()
        files = {
            "Service.cs": self.SERVICE,
            "Consumer.cs": self.CONSUMER,
            "Single.cs": self.SINGLE,
            "Decoy.cs": self.DECOY,
            "ServiceTests.cs": self.TESTS,
            "Nested.cs": self.NESTED,
            "NestedConsumers.cs": self.NESTED_CONSUMERS,
            "PrefixCollision.cs": self.PREFIX_COLLISION,
            "ExactPrefixTarget.cs": self.EXACT_PREFIX_TARGET,
            "LexicalShadow.cs": self.LEXICAL_SHADOW,
            "LoneNested.cs": self.LONE_NESTED,
            "WrongNamespace.cs": self.WRONG_NAMESPACE,
            "MultiNamespace.cs": self.MULTI_NAMESPACE,
            "WrongNamespaceExactPath.cs": self.WRONG_NAMESPACE_EXACT_PATH,
            "WrongNamespaceExactConsumer.cs": self.WRONG_NAMESPACE_EXACT_CONSUMER,
            "GlobalUsings.cs": self.GLOBAL_USING,
            "GlobalUsingTarget.cs": self.GLOBAL_USING_TARGET,
            "GlobalUsingConsumer.cs": self.GLOBAL_USING_CONSUMER,
            "MultiNamespaceConsumer.cs": self.MULTI_NAMESPACE_CONSUMER,
            "WrongNamespaceConsumer.cs": self.WRONG_NAMESPACE_CONSUMER,
            "LoneBareConsumer.cs": self.LONE_BARE_CONSUMER,
            "Namespaced.cs": self.NAMESPACED,
            "NamespaceSuffixDecoy.cs": self.NAMESPACE_SUFFIX_DECOY,
            "NamespacedConsumer.cs": self.NAMESPACED_CONSUMER,
        }
        for name, content in files.items():
            (tmp_path / name).write_text(content, encoding="utf-8")
        store = GraphStore(tmp_path / ".code-review-graph" / "graph.db")
        with patch(
            "code_review_graph.incremental.get_all_tracked_files",
            return_value=sorted(files),
        ):
            full_build(tmp_path, store)
        run_post_processing(store)
        store.close()

    def _call_targets_of(self, tmp_path, caller_suffix):
        from code_review_graph.graph import GraphStore

        store = GraphStore(tmp_path / ".code-review-graph" / "graph.db")
        try:
            rows = store._conn.execute(
                "SELECT target_qualified FROM edges "
                "WHERE kind = 'CALLS' AND source_qualified LIKE ?",
                (f"%::{caller_suffix}",),
            ).fetchall()
            return {row["target_qualified"] for row in rows}
        finally:
            store.close()

    def test_full_build_resolves_receiver_calls_to_canonical_methods(
        self, tmp_path,
    ):
        self._build(tmp_path)
        service = str(tmp_path / "Service.cs")
        targets = self._call_targets_of(tmp_path, "Consumer.Run")
        assert f"{service}::Service.StaticCall" in targets
        assert f"{service}::Service.InstanceCall" in targets
        assert f"{service}::Service.ConditionalCall" in targets
        decoy = str(tmp_path / "Decoy.cs")
        assert not any(t.startswith(f"{decoy}::") for t in targets)

    def test_full_build_resolves_same_file_receiver_call(self, tmp_path):
        self._build(tmp_path)
        single = str(tmp_path / "Single.cs")
        targets = self._call_targets_of(tmp_path, "Runner.Go")
        assert f"{single}::Widget.Spin" in targets

    def test_nested_receiver_calls_resolve_to_distinct_methods(self, tmp_path):
        from code_review_graph.tools.query import query_graph

        self._build(tmp_path)
        nested = str(tmp_path / "Nested.cs")
        expected = {
            "DetailsConsumer.CallDetails": (
                "CallDetails", f"{nested}::Details.QueryHandler.Handle"
            ),
            "EditConsumer.CallEdit": (
                "CallEdit", f"{nested}::Edit.QueryHandler.Handle"
            ),
        }
        for caller, (caller_name, target) in expected.items():
            assert self._call_targets_of(tmp_path, caller) == {target}
            result = query_graph("callers_of", target, repo_root=str(tmp_path))
            assert result["status"] == "ok"
            resolved = {
                node["name"]
                for node in result["results"]
                if node.get("target_resolution") != "unresolved"
            }
            assert resolved == {caller_name}

    def test_bare_nested_receiver_stays_unresolved_when_ambiguous(self, tmp_path):
        self._build(tmp_path)
        assert self._call_targets_of(
            tmp_path, "AmbiguousConsumer.CallAmbiguous",
        ) == {"Handle"}

    def test_exact_nested_receiver_beats_suffix_match(self, tmp_path):
        self._build(tmp_path)
        target = tmp_path / "ExactPrefixTarget.cs"
        assert self._call_targets_of(
            tmp_path, "ExactPathConsumer.Call",
        ) == {f"{target.as_posix()}::B.C.M"}

    def test_enclosing_scope_resolves_receiver_over_shorter_global_type(
        self, tmp_path,
    ):
        """``D.E`` inside ``LexicalOuter`` binds to ``LexicalOuter.D.E`` through
        the lexical phase, not to the shorter top-level ``E``."""
        self._build(tmp_path)
        target = tmp_path / "PrefixCollision.cs"
        assert self._call_targets_of(
            tmp_path, "LexicalOuter.SuffixPathConsumer.Call",
        ) == {f"{target.as_posix()}::LexicalOuter.D.E.M"}

    def test_shortened_receiver_requires_the_dropped_part_to_be_a_namespace(
        self, tmp_path,
    ):
        """``App.Ledger.AuditHandler`` is stored as ``Ledger.AuditHandler``
        only when ``App`` is the defining file's namespace. The one indexed
        candidate here sits in namespace ``Other``, so dropping ``App`` to reach
        it would bind a type the source never asked for; C# resolves the leading
        identifier first and never restarts at a later component.
        """
        self._build(tmp_path)
        targets = self._call_targets_of(tmp_path, "WrongNamespaceConsumer.Call")
        assert targets == {"Run"}, targets

    def test_dropped_namespace_must_be_the_files_only_namespace(self, tmp_path):
        """The namespace evidence is per file, and one C# file may declare
        several namespaces. ``Definitions.cs`` declaring both ``App`` and
        ``Other`` proves only that ``App`` appears somewhere in the file, not
        that it encloses ``Journal.PostHandler`` -- which actually sits in
        ``Other``. Ambiguous evidence must not resolve the call.
        """
        self._build(tmp_path)
        targets = self._call_targets_of(tmp_path, "MultiNamespaceConsumer.Call")
        assert targets == {"Post"}, targets

    @pytest.mark.xfail(
        reason=(
            "Known limitation: namespaces are absent from parent_name, so an "
            "exact containing-type match cannot prove the candidate is the type "
            "the receiver names. Deciding this needs per-node namespaces; "
            "file-level evidence cannot. main mislinks this too, on the bare "
            "name, so the branch narrows rather than introduces it."
        ),
    )
    def test_exact_path_match_should_check_the_candidate_namespace(
        self, tmp_path,
    ):
        """``Vault.Archive.StoreHandler`` declared inside ``namespace Other`` is
        keyed exactly as the receiver spells it, but the caller means a
        different type and cannot see ``Other``."""
        self._build(tmp_path)
        targets = self._call_targets_of(
            tmp_path, "ExactPathNamespaceConsumer.Call",
        )
        assert targets == {"Store"}, targets

    def test_global_using_target_still_resolves(self, tmp_path):
        """A ``global using`` applies project-wide, from a different file than
        the call site. Any future namespace-visibility check must not treat the
        caller's own file as the only source of visibility evidence, or valid
        calls like this one regress to unresolved.
        """
        self._build(tmp_path)
        target = tmp_path / "GlobalUsingTarget.cs"
        assert self._call_targets_of(tmp_path, "GlobalUsingConsumer.Call") == {
            f"{target.as_posix()}::Crate.PackHandler.Pack"
        }

    def test_bare_receiver_never_selects_a_nested_type(self, tmp_path):
        """A bare ``QueryHandler`` cannot name ``Details.QueryHandler`` in C#;
        it has to be qualified. Even as the only indexed candidate it must stay
        unresolved -- the real type may be in an unindexed dependency.
        """
        self._build(tmp_path)
        targets = self._call_targets_of(tmp_path, "LoneBareConsumer.Call")
        assert targets == {"Handle"}, targets

    def test_enclosing_type_shadows_global_type_of_the_same_path(self, tmp_path):
        """C# lookup starts at the innermost enclosing type, so inside
        ``Shadower.Consumer`` the receiver ``D.E`` is ``Shadower.D.E`` even
        though a top-level ``D.E`` also exists."""
        self._build(tmp_path)
        target = tmp_path / "LexicalShadow.cs"
        assert self._call_targets_of(
            tmp_path, "Shadower.Consumer.Call",
        ) == {f"{target.as_posix()}::Shadower.D.E.M"}

    def test_namespaced_receiver_beats_conflicting_containing_type_suffix(
        self, tmp_path,
    ):
        """C# namespaces are not part of ``parent_name``, so the receiver
        ``App.Report.ExportHandler`` only matches exactly once shortened to
        ``Report.ExportHandler``. An unrelated nested type whose containing
        path merely ends in ``App.Report.ExportHandler`` must not win first via
        the suffix map -- every exact candidate is tried before any heuristic.
        """
        self._build(tmp_path)
        target = tmp_path / "Namespaced.cs"
        assert self._call_targets_of(
            tmp_path, "NamespacedConsumer.Call",
        ) == {f"{target.as_posix()}::Report.ExportHandler.Run"}

    def test_callers_of_returns_resolved_caller_after_full_build(self, tmp_path):
        from code_review_graph.tools.query import query_graph

        self._build(tmp_path)
        service = str(tmp_path / "Service.cs")
        for method in ("StaticCall", "InstanceCall", "ConditionalCall"):
            result = query_graph(
                "callers_of",
                f"{service}::Service.{method}",
                repo_root=str(tmp_path),
            )
            assert result.get("status") == "ok"
            run_callers = [
                r for r in result.get("results", [])
                if r.get("name") == "Run"
            ]
            assert run_callers, f"callers_of({method}) missed Consumer.Run"
            assert all(
                r.get("target_resolution") != "unresolved"
                for r in run_callers
            ), f"callers_of({method}) still marks Consumer.Run unresolved"

    def test_impact_radius_of_service_file_reaches_consumer(self, tmp_path):
        from code_review_graph.tools.query import get_impact_radius

        self._build(tmp_path)
        result = get_impact_radius(
            changed_files=["Service.cs"], repo_root=str(tmp_path),
        )
        assert result.get("status") == "ok"
        impacted_names = {
            n["name"] for n in result.get("impacted_nodes", [])
        }
        assert "Run" in impacted_names
        assert str(tmp_path / "Consumer.cs") in set(
            result.get("impacted_files", []),
        )

    def test_impact_radius_of_decoy_file_does_not_reach_consumer(
        self, tmp_path,
    ):
        from code_review_graph.tools.query import get_impact_radius

        self._build(tmp_path)
        result = get_impact_radius(
            changed_files=["Decoy.cs"], repo_root=str(tmp_path),
        )
        assert result.get("status") == "ok"
        impacted_names = {
            n["name"] for n in result.get("impacted_nodes", [])
        }
        assert "Run" not in impacted_names

    def test_tests_for_finds_test_through_resolved_receiver_call(
        self, tmp_path,
    ):
        from code_review_graph.tools.query import query_graph

        self._build(tmp_path)
        service = str(tmp_path / "Service.cs")
        result = query_graph(
            "tests_for",
            f"{service}::Service.StaticCall",
            repo_root=str(tmp_path),
        )
        assert result.get("status") == "ok"
        test_names = {r.get("name") for r in result.get("results", [])}
        assert "TestStaticDispatch" in test_names


@pytest.mark.skipif(
    not _has_csharp_parser(), reason="csharp tree-sitter grammar not installed",
)
class TestCSharpNamespaceImpactAndCoverage:
    """End-to-end regression tests for #310 / #792.

    C# ``using X.Y;`` directives produce IMPORTS_FROM edges targeting the
    raw namespace string, never a file path. PR #353 added a namespace
    fallback for ``importers_of`` only, leaving:

    - ``get_impact_radius`` returning 0 impacted files/nodes for changed
      .cs files (the impact traversal had no namespace expansion), and
    - ``tests_for`` / the ``detect_changes`` test-gap detector reporting
      covered C# code as untested (``_resolve_bare_endpoints`` only accepts
      file-path import evidence C# never emits).
    """

    def _build(self, tmp_path):
        from code_review_graph.graph import GraphStore

        (tmp_path / ".git").mkdir()
        (tmp_path / ".code-review-graph").mkdir()
        core = tmp_path / "Core.cs"
        core.write_text(
            "namespace ACME.Core;\n"
            "public class TaskBoard {\n"
            "    public int CountTasks() { return 0; }\n"
            "}\n",
            encoding="utf-8",
        )
        app = tmp_path / "App.cs"
        app.write_text(
            "using ACME.Core;\n"
            "namespace ACME.App;\n"
            "public class App {\n"
            "    public void Run() {\n"
            "        var b = new TaskBoard();\n"
            "        b.CountTasks();\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )
        # NUnit-style test whose name does NOT match any naming convention
        # (no Test prefix on the method), so coverage must come from the
        # resolved TESTED_BY edge rather than the name-based fallback.
        tests = tmp_path / "TaskBoardTests.cs"
        tests.write_text(
            "using NUnit.Framework;\n"
            "using ACME.Core;\n"
            "namespace ACME.Core.Tests;\n"
            "[TestFixture]\n"
            "public class TaskBoardTests {\n"
            "    [Test]\n"
            "    public void CountTasks_ReturnsZero() {\n"
            "        var board = new TaskBoard();\n"
            "        var n = board.CountTasks();\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )
        unrelated = tmp_path / "Unrelated.cs"
        unrelated.write_text(
            "using System.Linq;\n"
            "namespace ACME.Other;\n"
            "public class Other {}\n",
            encoding="utf-8",
        )

        store = GraphStore(tmp_path / ".code-review-graph" / "graph.db")
        parser = CodeParser()
        for path in (core, app, tests, unrelated):
            nodes, edges = parser.parse_file(path)
            for n in nodes:
                store.upsert_node(n)
            for e in edges:
                store.upsert_edge(e)
        store.commit()
        # Same bare-endpoint resolution the build/postprocess pipeline runs.
        store.resolve_bare_call_targets()
        store.resolve_bare_tested_by_sources()
        return store, core, app, tests, unrelated

    def test_impact_radius_sql_reaches_csharp_importers(self, tmp_path):
        store, core, app, tests, unrelated = self._build(tmp_path)
        try:
            impact = store.get_impact_radius_sql([str(core)])
            assert str(app) in impact["impacted_files"]
            assert str(tests) in impact["impacted_files"]
            assert str(unrelated) not in impact["impacted_files"]
            assert impact["total_impacted"] > 0
            # The namespace string itself must never surface as a node.
            impacted_qns = {n.qualified_name for n in impact["impacted_nodes"]}
            assert "ACME.Core" not in impacted_qns
        finally:
            store.close()

    def test_impact_radius_networkx_reaches_csharp_importers(self, tmp_path):
        store, core, app, tests, unrelated = self._build(tmp_path)
        try:
            impact = store._get_impact_radius_networkx([str(core)])
            assert str(app) in impact["impacted_files"]
            assert str(tests) in impact["impacted_files"]
            assert str(unrelated) not in impact["impacted_files"]
            impacted_qns = {n.qualified_name for n in impact["impacted_nodes"]}
            assert "ACME.Core" not in impacted_qns
        finally:
            store.close()

    def test_importer_appears_in_both_importers_of_and_impact(self, tmp_path):
        """Owner acceptance for #310: the same importer must appear in both
        ``importers_of`` and the public ``get_impact_radius`` results."""
        from code_review_graph.tools.query import query_graph

        store, core, app, _tests, _unrelated = self._build(tmp_path)
        try:
            result = query_graph(
                "importers_of", str(core), repo_root=str(tmp_path),
            )
            assert result.get("status") == "ok"
            importers = {r["file"] for r in result.get("results", [])}
            assert str(app) in importers

            impact = store.get_impact_radius([str(core)])
            assert str(app) in impact["impacted_files"]
        finally:
            store.close()

    def test_bare_tested_by_source_resolves_via_namespace_evidence(self, tmp_path):
        store, core, _app, tests, _unrelated = self._build(tmp_path)
        try:
            method_qn = f"{core}::TaskBoard.CountTasks"
            test_qn = f"{tests}::TaskBoardTests.CountTasks_ReturnsZero"
            tested_by = [
                e for e in store.get_edges_by_source(method_qn)
                if e.kind == "TESTED_BY"
            ]
            assert [e.target_qualified for e in tested_by] == [test_qn]
        finally:
            store.close()

    def test_tests_for_finds_csharp_test_via_edge(self, tmp_path):
        from code_review_graph.tools.query import query_graph

        store, core, _app, tests, _unrelated = self._build(tmp_path)
        store.close()
        result = query_graph(
            "tests_for",
            f"{core}::TaskBoard.CountTasks",
            repo_root=str(tmp_path),
        )
        assert result.get("status") == "ok"
        found = {
            r["qualified_name"]: r for r in result.get("results", [])
        }
        test_qn = f"{tests}::TaskBoardTests.CountTasks_ReturnsZero"
        assert test_qn in found
        # Must come from the resolved TESTED_BY edge, not name matching.
        assert found[test_qn].get("inferred_by") != "naming_convention"

    def test_detect_changes_does_not_report_covered_method_untested(self, tmp_path):
        from code_review_graph.changes import analyze_changes

        store, core, _app, _tests, _unrelated = self._build(tmp_path)
        try:
            result = analyze_changes(store, [str(core)])
            gap_names = {g["name"] for g in result["test_gaps"]}
            assert "CountTasks" not in gap_names
        finally:
            store.close()


class TestRubyParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample.rb")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("app.rb")) == "ruby"

    def test_finds_classes(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "User" in names or "UserRepository" in names

    def test_finds_methods(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "initialize" in names or "find_by_id" in names or "save" in names

    def test_finds_calls(self):
        """Ruby method calls must produce CALLS edges.

        Ruby's grammar uses the same ``call`` node type for both
        ``require`` and ordinary method invocation, so the dispatcher must
        not treat every ``call`` as an import. Paren calls (``save(user)``),
        command calls (``puts ...``) and member calls (``User.new`` /
        ``@users.size``) are all captured. Bare implicit-self calls with no
        parens (e.g. a lone ``helper``) parse as ``identifier`` rather than
        ``call`` and are intentionally not covered here.
        """
        calls = [e for e in self.edges if e.kind == "CALLS"]
        assert len(calls) >= 1

        targets = {e.target for e in calls}
        target_names = {t.split("::")[-1].split(".")[-1] for t in targets}

        # Paren, command and member calls are all captured.
        assert "save" in target_names
        assert "puts" in target_names
        assert "new" in target_names
        assert "size" in target_names

        # A same-class call resolves to the defining method node, not a bare
        # name, so callers_of/callees_of work within a file.
        assert any(t.endswith("sample.rb::UserRepository.save") for t in targets)

        # Calls are attributed to their enclosing method.
        create_user_targets = {
            e.target for e in calls
            if e.source.endswith("UserRepository.create_user")
        }
        assert any(t.endswith("UserRepository.save") for t in create_user_targets)
        assert any(t.endswith("new") for t in create_user_targets)


class TestPHPParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample.php")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("index.php")) == "php"

    def test_finds_classes(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "User" in names or "InMemoryRepo" in names

    def test_finds_functions(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert len(names) > 0

    def test_finds_calls(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        targets = {e.target for e in calls}
        target_names = {t.split("::")[-1].split(".")[-1] for t in targets}

        run_queries_targets = {
            e.target for e in calls if e.source.endswith("::ExtendedRepo.runQueries")
        }

        # Plain function calls
        assert "sqlQuery" in target_names
        assert "xl" in target_names
        assert "text" in target_names

        # Member and nullsafe method calls
        assert "execute" in target_names
        assert "search" in target_names

        # Scoped/static calls
        assert any(
            target.endswith("sample.php::QueryUtils.fetchRecords")
            for target in targets
        )
        assert any(
            target.endswith("sample.php::EncounterService.create")
            for target in targets
        )
        assert any(t.endswith("__construct") for t in run_queries_targets)
        assert any(t.endswith("factory") for t in run_queries_targets)

        # Global namespaced calls should normalize to a stable name
        assert "dirname" in target_names

    def test_finds_extended_php_types_bases_and_object_creation(self):
        source = b"""<?php
trait Auditable {}
enum Status: string { case Active = 'active'; }
interface Contract {}

class Service extends \\Framework\\Base implements Contract, \\Other\\Marker {
    public function run(): void {
        $worker = new \\App\\Worker();
        $worker->save();
        Service::factory();
    }
}
"""

        nodes, edges = self.parser.parse_bytes(Path("extended.php"), source)

        class_names = {node.name for node in nodes if node.kind == "Class"}
        assert {"Auditable", "Status", "Contract", "Service"} <= class_names

        inherited = {edge.target for edge in edges if edge.kind == "INHERITS"}
        assert "\\Framework\\Base" in inherited
        assert "Contract" in inherited
        assert "\\Other\\Marker" in inherited

        calls = {edge.target for edge in edges if edge.kind == "CALLS"}
        assert "App\\Worker" in calls
        # Existing PHP call formatting must stay unchanged.
        assert "save" in calls
        assert "Service::factory" in calls


class TestPHPTestAnnotations:
    """Regression tests for #693: PHP test methods were only recognised by
    the ``test_`` name prefix. Neither PHPUnit's legacy ``/** @test */``
    docblock tag nor the PHP 8 ``#[Test]`` attribute was detected, so those
    methods were misclassified as production ``Function`` nodes. Mirrors the
    C# attribute fix from #295: PHP attributes need their own capture path
    (``attribute_list > attribute_group > attribute``, one level deeper than
    C#'s), and the docblock tag needs a separate preceding-sibling check.
    """

    def _parse(self, tmp_path):
        p = tmp_path / "tests" / "ExampleTest.php"
        p.parent.mkdir()
        p.write_text(
            "<?php\n"
            "namespace Tests;\n\n"
            "use PHPUnit\\Framework\\TestCase;\n"
            "use PHPUnit\\Framework\\Attributes\\Test;\n\n"
            "use PHPUnit\\Framework\\Attributes\\Test as UnitTest;\n"
            "use PHPUnit\\Framework\\Attributes\\DataProvider;\n\n"
            "use App\\Attributes\\Test as OtherTest;\n\n"
            "class ExampleTest extends TestCase\n"
            "{\n"
            "    public function test_prefixed_method_should_be_detected(): void\n"
            "    {\n"
            "    }\n\n"
            "    public function testItAddsTwoNumbers(): void\n"
            "    {\n"
            "    }\n\n"
            "    /** @test */\n"
            "    public function docblock_annotated_method(): void\n"
            "    {\n"
            "    }\n\n"
            "    #[Test]\n"
            "    public function php8_attribute_annotated_method(): void\n"
            "    {\n"
            "    }\n\n"
            "    #[\\PHPUnit\\Framework\\Attributes\\Test]\n"
            "    public function qualified_attribute_method(): void\n"
            "    {\n"
            "    }\n\n"
            "    #[UnitTest]\n"
            "    public function aliased_attribute_method(): void\n"
            "    {\n"
            "    }\n\n"
            "    #[DataProvider('rows'), Test]\n"
            "    public function grouped_attribute_method(): void\n"
            "    {\n"
            "    }\n\n"
            "    #[\\App\\Attributes\\Test]\n"
            "    public function unrelated_qualified_attribute(): void\n"
            "    {\n"
            "    }\n\n"
            "    #[OtherTest]\n"
            "    public function unrelated_aliased_attribute(): void\n"
            "    {\n"
            "    }\n\n"
            "    /** @test-case is documentation, not a PHPUnit tag. */\n"
            "    public function documented_helper(): void\n"
            "    {\n"
            "    }\n\n"
            "    public function helperNotATest(): void\n"
            "    {\n"
            "    }\n"
            "}\n\n"
            "function testDatabaseAvailable(): void\n"
            "{\n"
            "}\n",
            encoding="utf-8",
        )
        return CodeParser().parse_file(p)

    def test_name_prefix_still_detected(self, tmp_path):
        nodes, _ = self._parse(tmp_path)
        m = next(n for n in nodes if n.name == "test_prefixed_method_should_be_detected")
        assert m.kind == "Test"
        assert m.is_test is True

    def test_phpunit_camel_case_name_prefix_detected(self, tmp_path):
        nodes, _ = self._parse(tmp_path)
        m = next(n for n in nodes if n.name == "testItAddsTwoNumbers")
        assert m.kind == "Test"
        assert m.is_test is True

    def test_phpunit_name_prefix_does_not_mark_top_level_function(self, tmp_path):
        nodes, _ = self._parse(tmp_path)
        m = next(n for n in nodes if n.name == "testDatabaseAvailable")
        assert m.kind == "Function"
        assert m.is_test is False

    def test_docblock_annotation_detected(self, tmp_path):
        nodes, _ = self._parse(tmp_path)
        m = next(n for n in nodes if n.name == "docblock_annotated_method")
        assert m.kind == "Test"
        assert m.is_test is True

    def test_php8_attribute_detected(self, tmp_path):
        nodes, _ = self._parse(tmp_path)
        m = next(n for n in nodes if n.name == "php8_attribute_annotated_method")
        assert m.kind == "Test"
        assert m.is_test is True
        assert m.extra.get("decorators") == ["Test"]

    def test_qualified_php8_attribute_detected(self, tmp_path):
        nodes, _ = self._parse(tmp_path)
        m = next(n for n in nodes if n.name == "qualified_attribute_method")
        assert m.kind == "Test"
        assert m.is_test is True

    def test_aliased_php8_attribute_detected(self, tmp_path):
        nodes, _ = self._parse(tmp_path)
        m = next(n for n in nodes if n.name == "aliased_attribute_method")
        assert m.kind == "Test"
        assert m.is_test is True

    def test_grouped_php8_attribute_detected(self, tmp_path):
        nodes, _ = self._parse(tmp_path)
        m = next(n for n in nodes if n.name == "grouped_attribute_method")
        assert m.kind == "Test"
        assert m.is_test is True

    def test_unrelated_qualified_attribute_is_not_detected(self, tmp_path):
        nodes, _ = self._parse(tmp_path)
        m = next(
            n for n in nodes if n.name == "unrelated_qualified_attribute"
        )
        assert m.kind == "Function"
        assert m.is_test is False

    def test_unrelated_aliased_attribute_is_not_detected(self, tmp_path):
        nodes, _ = self._parse(tmp_path)
        m = next(
            n for n in nodes if n.name == "unrelated_aliased_attribute"
        )
        assert m.kind == "Function"
        assert m.is_test is False

    def test_similar_docblock_tag_is_not_detected(self, tmp_path):
        nodes, _ = self._parse(tmp_path)
        m = next(n for n in nodes if n.name == "documented_helper")
        assert m.kind == "Function"
        assert m.is_test is False

    def test_plain_method_not_detected(self, tmp_path):
        nodes, _ = self._parse(tmp_path)
        m = next(n for n in nodes if n.name == "helperNotATest")
        assert m.kind == "Function"
        assert m.is_test is False


class TestPHPImportResolution:
    """PHP ``use`` imports resolve to absolute file paths (PSR-4 layout)."""

    def test_resolves_project_import(self, tmp_path):
        """``use`` of a project class resolves to its .php file."""
        entity = tmp_path / "src/App/Domain/Entity"
        entity.mkdir(parents=True)
        (entity / "Job.php").write_text(
            "<?php\nnamespace App\\Domain\\Entity;\nclass Job {}\n"
        )
        svc = tmp_path / "src/App/Service"
        svc.mkdir(parents=True)
        (svc / "MatchService.php").write_text(
            "<?php\nnamespace App\\Service;\n"
            "use App\\Domain\\Entity\\Job;\n"
            "class MatchService {}\n"
        )

        parser = CodeParser(tmp_path)
        _, edges = parser.parse_file(svc / "MatchService.php")
        imports = [e for e in edges if e.kind == "IMPORTS_FROM"]
        assert len(imports) == 1
        assert imports[0].target == (entity / "Job.php").resolve().as_posix()

    def test_vendor_import_stays_unresolved(self, tmp_path):
        """A class with no local file stays as the bare FQN, not a raw
        ``use ...;`` statement and not a fake path."""
        svc = tmp_path / "src/App/Service"
        svc.mkdir(parents=True)
        (svc / "Logger.php").write_text(
            "<?php\nnamespace App\\Service;\n"
            "use Psr\\Log\\LoggerInterface;\n"
            "class Logger {}\n"
        )
        parser = CodeParser()
        _, edges = parser.parse_file(svc / "Logger.php")
        imports = [e for e in edges if e.kind == "IMPORTS_FROM"]
        assert len(imports) == 1
        assert imports[0].target == "Psr\\Log\\LoggerInterface"
        assert not imports[0].target.endswith(".php")

    def test_aliased_import_records_fqn_not_alias(self, tmp_path):
        """``use A\\B\\C as D`` records the FQN A\\B\\C, ignoring the alias."""
        contact = tmp_path / "src/App/Domain/Embedded"
        contact.mkdir(parents=True)
        (contact / "Contact.php").write_text(
            "<?php\nnamespace App\\Domain\\Embedded;\nclass Contact {}\n"
        )
        job = tmp_path / "src/App/Domain/Entity"
        job.mkdir(parents=True)
        (job / "Job.php").write_text(
            "<?php\nnamespace App\\Domain\\Entity;\n"
            "use App\\Domain\\Embedded\\Contact as ContactEmbedded;\n"
            "class Job {}\n"
        )
        parser = CodeParser(tmp_path)
        _, edges = parser.parse_file(job / "Job.php")
        imports = [e for e in edges if e.kind == "IMPORTS_FROM"]
        assert len(imports) == 1
        assert imports[0].target == (contact / "Contact.php").resolve().as_posix()

    def test_grouped_use_expands_to_multiple_imports(self, tmp_path):
        """``use App\\Domain\\{Entity\\Job, Model\\Status}`` -> two imports,
        each prefixed with the group namespace and resolved independently."""
        base = tmp_path / "src/App/Domain"
        (base / "Entity").mkdir(parents=True)
        (base / "Model").mkdir(parents=True)
        (base / "Entity/Job.php").write_text(
            "<?php\nnamespace App\\Domain\\Entity;\nclass Job {}\n"
        )
        (base / "Model/Status.php").write_text(
            "<?php\nnamespace App\\Domain\\Model;\nclass Status {}\n"
        )
        consumer = tmp_path / "src/App/Service"
        consumer.mkdir(parents=True)
        (consumer / "C.php").write_text(
            "<?php\nnamespace App\\Service;\n"
            "use App\\Domain\\{Entity\\Job, Model\\Status};\n"
            "class C {}\n"
        )
        parser = CodeParser(tmp_path)
        _, edges = parser.parse_file(consumer / "C.php")
        targets = {e.target for e in edges if e.kind == "IMPORTS_FROM"}
        assert (base / "Entity/Job.php").resolve().as_posix() in targets
        assert (base / "Model/Status.php").resolve().as_posix() in targets
        assert len(targets) == 2


class TestKotlinParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample.kt")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("Main.kt")) == "kotlin"

    def test_finds_classes(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "User" in names or "InMemoryRepo" in names

    def test_finds_functions(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "createUser" in names or "findById" in names or "save" in names

    def test_finds_calls(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        targets = {c.target for c in calls}
        # Simple call: println(...)
        assert "println" in targets
        # Method call: repo.save(user)
        assert any("save" in t for t in targets)


class TestKotlinAnnotations:
    """Regression tests for #295: Kotlin nodes must persist annotation
    metadata in both ``modifiers`` (comma-joined string) and
    ``extra['decorators']`` (list) so consumers can filter queries like
    "show me all @Composable functions" or "find @HiltViewModel classes".
    """

    def _parse(self, source: str, tmp_path):
        p = tmp_path / "x.kt"
        p.write_text(source, encoding="utf-8")
        return CodeParser().parse_file(p)

    def test_hilt_viewmodel_annotation_on_class(self, tmp_path):
        nodes, _ = self._parse(
            "package com.example\n@HiltViewModel\nclass MyVM {\n    fun noop() {}\n}\n",
            tmp_path,
        )
        vm = next(n for n in nodes if n.kind == "Class" and n.name == "MyVM")
        assert vm.modifiers == "HiltViewModel"
        assert vm.extra.get("decorators") == ["HiltViewModel"]

    def test_composable_annotation_on_function(self, tmp_path):
        nodes, _ = self._parse(
            "package com.example\n@Composable\nfun Greeting(n: String) {\n"
            "    println(n)\n}\n",
            tmp_path,
        )
        fn = next(n for n in nodes if n.kind == "Function" and n.name == "Greeting")
        assert fn.modifiers == "Composable"
        assert fn.extra.get("decorators") == ["Composable"]

    def test_unannotated_function_has_none_modifiers(self, tmp_path):
        """Guard: adding annotation support must not leak an empty string
        or empty list onto unannotated nodes."""
        nodes, _ = self._parse(
            "package com.example\nfun bare() { println(1) }\n", tmp_path,
        )
        fn = next(n for n in nodes if n.kind == "Function" and n.name == "bare")
        assert fn.modifiers is None
        assert "decorators" not in fn.extra

    def test_test_annotation_still_triggers_test_kind(self, tmp_path):
        """Guard: annotation persistence must not break the pre-existing
        @Test -> Test-kind promotion."""
        nodes, _ = self._parse(
            "package com.example\nclass T {\n    @Test\n    fun testX() { println(1) }\n}\n",
            tmp_path,
        )
        t = next(n for n in nodes if n.kind == "Test" and n.name == "testX")
        assert t.extra.get("decorators") == ["Test"]


class TestSwiftParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample.swift")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("App.swift")) == "swift"

    def test_finds_classes(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "User" in names
        assert "InMemoryRepo" in names

    def test_finds_functions(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "createUser" in names or "findById" in names or "save" in names

    def test_finds_enum(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "Direction" in names

    def test_finds_actor(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "DataStore" in names

    def test_finds_extension(self):
        """Extensions should be detected and linked to the extended type."""
        classes = [n for n in self.nodes if n.kind == "Class"]
        # Extension of InMemoryRepo should produce a Class node named InMemoryRepo
        # with swift_kind == "extension"
        ext_nodes = [c for c in classes if c.extra.get("swift_kind") == "extension"]
        assert len(ext_nodes) >= 1
        assert ext_nodes[0].name == "InMemoryRepo"

    def test_finds_protocol(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "UserRepository" in names

    def test_swift_kind_extra(self):
        """Each Swift type should have the correct swift_kind in extra."""
        classes = {n.name: n for n in self.nodes if n.kind == "Class"}
        assert classes["User"].extra.get("swift_kind") == "struct"
        assert classes["Direction"].extra.get("swift_kind") == "enum"
        assert classes["DataStore"].extra.get("swift_kind") == "actor"
        assert classes["UserRepository"].extra.get("swift_kind") == "protocol"
        # InMemoryRepo appears twice (class + extension); check at least one is "class"
        repo_nodes = [n for n in self.nodes if n.kind == "Class" and n.name == "InMemoryRepo"]
        kinds = {n.extra.get("swift_kind") for n in repo_nodes}
        assert "class" in kinds
        assert "extension" in kinds

    def test_inheritance_edges(self):
        """Swift inheritance / conformance should produce INHERITS edges."""
        inherits = [e for e in self.edges if e.kind == "INHERITS"]
        targets = {e.target for e in inherits}
        # InMemoryRepo: UserRepository
        assert "UserRepository" in targets
        # Direction: String
        assert "String" in targets
        # extension InMemoryRepo: CustomStringConvertible
        assert "CustomStringConvertible" in targets

    def test_finds_initializers(self):
        """`init` / `convenience init` are Function nodes on their own type."""
        inits = [
            n for n in self.nodes
            if n.kind == "Function" and n.name == "init" and n.parent_name == "InMemoryRepo"
        ]
        assert len(inits) == 2

    def test_finds_deinitializer(self):
        funcs = {(n.name, n.parent_name) for n in self.nodes if n.kind == "Function"}
        assert ("deinit", "InMemoryRepo") in funcs

    def test_finds_subscript(self):
        """`subscript` is named after its keyword, not its return type."""
        funcs = {(n.name, n.parent_name) for n in self.nodes if n.kind == "Function"}
        assert ("subscript", "InMemoryRepo") in funcs
        assert not any(n.name == "User" for n in self.nodes if n.kind == "Function")

    def test_initializer_body_calls_attributed_to_declaration(self):
        """Calls inside init/deinit/subscript belong to that declaration, not the file."""
        calls = {
            (e.source.rsplit("::", 1)[-1], e.target.rsplit("::", 1)[-1])
            for e in self.edges if e.kind == "CALLS"
        }
        # init(seed:) calls save(user); convenience init() delegates to self.init
        assert ("InMemoryRepo.init", "InMemoryRepo.save") in calls
        assert ("InMemoryRepo.init", "InMemoryRepo.init") in calls
        assert ("InMemoryRepo.deinit", "removeAll") in calls
        assert ("InMemoryRepo.subscript", "InMemoryRepo.findById") in calls
        # Previously these landed on the File node, making blast radius file-wide.
        assert not any(src.endswith("sample.swift") for src, _ in calls)


class TestScalaParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample.scala")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("Main.scala")) == "scala"

    def test_finds_classes_traits_objects(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "Repository" in names
        assert "User" in names
        assert "InMemoryRepo" in names
        assert "UserService" in names
        assert "Color" in names

    def test_finds_functions(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "findById" in names
        assert "save" in names
        assert "createUser" in names
        assert "getUser" in names
        assert "apply" in names

    def test_finds_imports(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = {e.target for e in imports}
        assert "scala.util.Try" in targets
        assert "scala.collection.mutable" in targets
        assert "scala.collection.mutable.HashMap" in targets
        assert "scala.collection.mutable.ListBuffer" in targets
        assert "scala.concurrent.*" in targets
        assert len(imports) >= 3

    def test_finds_inheritance(self):
        inherits = [e for e in self.edges if e.kind == "INHERITS"]
        targets = {e.target for e in inherits}
        assert "Repository" in targets
        assert "Serializable" in targets

    def test_finds_calls(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        assert len(calls) >= 3


class TestSolidityParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample.sol")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("Vault.sol")) == "solidity"

    def test_finds_contracts_interfaces_libraries(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "StakingVault" in names
        assert "BoostedPool" in names
        assert "IStakingPool" in names
        assert "RewardMath" in names

    def test_finds_structs(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "StakerPosition" in names

    def test_finds_enums(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "PoolStatus" in names

    def test_finds_custom_errors(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "InsufficientStake" in names
        assert "PoolNotActive" in names

    def test_finds_functions(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "stake" in names
        assert "unstake" in names
        assert "stakedBalance" in names
        assert "pendingBonus" in names

    def test_finds_constructors(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        constructors = [f for f in funcs if f.name == "constructor"]
        assert len(constructors) == 2  # StakingVault + BoostedPool

    def test_finds_modifiers(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "nonZero" in names
        assert "whenPoolActive" in names

    def test_finds_events(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "Staked" in names
        assert "Unstaked" in names
        assert "BonusClaimed" in names

    def test_finds_file_level_events(self):
        funcs = [
            n for n in self.nodes
            if n.kind == "Function" and n.parent_name is None
        ]
        names = {f.name for f in funcs}
        # file-level events declared outside any contract
        assert "Staked" in names or "Unstaked" in names

    def test_finds_user_defined_value_types(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "Price" in names
        assert "PositionId" in names

    def test_finds_file_level_constants(self):
        constants = [
            n for n in self.nodes
            if n.extra.get("solidity_kind") == "constant"
        ]
        names = {c.name for c in constants}
        assert "MAX_SUPPLY" in names
        assert "ZERO_ADDRESS" in names

    def test_finds_free_functions(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        free = [f for f in funcs if f.name == "protocolFee"]
        assert len(free) == 1
        assert free[0].parent_name is None

    def test_finds_using_directive(self):
        depends = [e for e in self.edges if e.kind == "DEPENDS_ON"]
        targets = {e.target for e in depends}
        assert "RewardMath" in targets

    def test_finds_selective_imports(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = {e.target for e in imports}
        assert "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol" in targets

    def test_finds_state_variables(self):
        state_vars = [
            n for n in self.nodes
            if n.extra.get("solidity_kind") == "state_variable"
        ]
        names = {v.name for v in state_vars}
        assert "stakes" in names
        assert "totalStaked" in names
        assert "guardian" in names
        assert "status" in names
        assert "MIN_STAKE" in names
        assert "launchTime" in names
        assert "bonusRate" in names
        assert "assetPrice" in names

    def test_state_variable_types(self):
        state_vars = {
            n.name: n for n in self.nodes
            if n.extra.get("solidity_kind") == "state_variable"
        }
        assert state_vars["totalStaked"].return_type == "uint256"
        assert state_vars["guardian"].return_type == "address"
        assert state_vars["stakes"].modifiers == "public"

    def test_finds_receive_and_fallback(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "receive" in names
        assert "fallback" in names

    def test_finds_imports(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = {e.target for e in imports}
        assert "@openzeppelin/contracts/token/ERC20/ERC20.sol" in targets
        assert "@openzeppelin/contracts/access/Ownable.sol" in targets

    def test_finds_inheritance(self):
        inherits = [e for e in self.edges if e.kind == "INHERITS"]
        pairs = {(e.source.split("::")[-1], e.target) for e in inherits}
        assert ("StakingVault", "ERC20") in pairs
        assert ("StakingVault", "Ownable") in pairs
        assert ("StakingVault", "IStakingPool") in pairs
        assert ("BoostedPool", "StakingVault") in pairs

    def test_finds_function_calls(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        targets = {e.target.split("::")[-1] if "::" in e.target else e.target for e in calls}
        assert "require" in targets
        assert "_mint" in targets
        assert "_burn" in targets
        assert "pendingBonus" in targets or "BoostedPool.pendingBonus" in targets

    def test_finds_emit_edges(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        # Targets may be qualified (e.g. "file::BoostedPool.BonusClaimed")
        target_basenames = {e.target.split("::")[-1].split(".")[-1] for e in calls}
        assert "Staked" in target_basenames
        assert "Unstaked" in target_basenames
        assert "BonusClaimed" in target_basenames

    def test_finds_modifier_invocations(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        # Extract (source_basename, target_basename) to handle qualified names
        target_basenames = {e.target.split("::")[-1].split(".")[-1] for e in calls}
        assert "nonZero" in target_basenames
        assert "whenPoolActive" in target_basenames

    def test_finds_constructor_modifier_invocations(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        target_basenames = {e.target.split("::")[-1].split(".")[-1] for e in calls}
        assert "ERC20" in target_basenames
        assert "Ownable" in target_basenames
        assert "StakingVault" in target_basenames

    def test_finds_contains(self):
        contains = [e for e in self.edges if e.kind == "CONTAINS"]
        targets = {e.target.split("::")[-1] for e in contains}
        assert "StakingVault" in targets
        assert "StakingVault.stake" in targets
        assert "StakingVault.stakes" in targets
        assert "StakingVault.Staked" not in targets  # Staked is file-level
        assert "BoostedPool.claimBonus" in targets

    def test_extracts_params(self):
        funcs = {
            n.name: n for n in self.nodes
            if n.kind == "Function" and n.parent_name == "RewardMath"
        }
        assert funcs["mulPrecise"].params == "(uint256 a, uint256 b)"

    def test_extracts_return_type(self):
        funcs = {
            n.name: n for n in self.nodes
            if n.kind == "Function" and n.parent_name == "RewardMath"
        }
        assert "uint256" in funcs["mulPrecise"].return_type


class TestVueParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample_vue.vue")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("App.vue")) == "vue"

    def test_finds_functions(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "increment" in names
        assert "onSelectUser" in names
        assert "fetchUsers" in names

    def test_finds_imports(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = {e.target for e in imports}
        assert "vue" in targets
        assert "./UserList.vue" in targets

    def test_finds_contains(self):
        contains = [e for e in self.edges if e.kind == "CONTAINS"]
        assert len(contains) >= 3

    def test_nodes_have_vue_language(self):
        for node in self.nodes:
            assert node.language == "vue"

    def test_finds_calls(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        assert len(calls) >= 1


class TestRParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample.R")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("script.r")) == "r"
        assert self.parser.detect_language(Path("script.R")) == "r"

    def test_finds_functions(self):
        funcs = [n for n in self.nodes if n.kind == "Function" and n.parent_name is None]
        names = {f.name for f in funcs}
        assert "add" in names
        assert "multiply" in names
        assert "process_data" in names

    def test_finds_s4_classes(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "MyClass" in names

    def test_finds_class_methods(self):
        methods = [
            n for n in self.nodes
            if n.kind == "Function" and n.parent_name == "MyClass"
        ]
        names = {m.name for m in methods}
        assert "greet" in names
        assert "get_age" in names

    def test_finds_imports(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = {e.target for e in imports}
        assert "dplyr" in targets
        assert "ggplot2" in targets
        assert "utils.R" in targets

    def test_finds_calls(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        targets = {e.target for e in calls}
        assert "dplyr::filter" in targets
        assert "dplyr::summarize" in targets

    def test_finds_params(self):
        funcs = {n.name: n for n in self.nodes if n.kind == "Function"}
        assert funcs["add"].params is not None
        assert "x" in funcs["add"].params
        assert "y" in funcs["add"].params

    def test_finds_contains(self):
        contains = [e for e in self.edges if e.kind == "CONTAINS"]
        targets = {e.target.split("::")[-1] for e in contains}
        assert "add" in targets
        assert "multiply" in targets
        assert "MyClass" in targets
        assert "MyClass.greet" in targets

    def test_detects_test_functions(self):
        parser = CodeParser()
        nodes, _edges = parser.parse_file(FIXTURES / "test_sample.R")
        file_node = [n for n in nodes if n.kind == "File"][0]
        assert file_node.is_test is True
        test_funcs = [n for n in nodes if n.is_test and n.kind == "Test"]
        names = {f.name for f in test_funcs}
        assert "test_add" in names


class TestPerlParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample.pl")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("script.pl")) == "perl"
        assert self.parser.detect_language(Path("Module.pm")) == "perl"
        assert self.parser.detect_language(Path("test.t")) == "perl"

    def test_finds_packages(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "Animal" in names
        assert "Dog" in names

    def test_finds_subroutines(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "new" in names
        assert "speak" in names
        assert "fetch" in names
        assert "bark" in names

    def test_finds_imports(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        assert len(imports) >= 1

    def test_finds_calls(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        targets = {e.target for e in calls}
        assert any(t == "speak" or t.endswith("::speak") for t in targets)  # $self->speak() — method_call_expression
        assert "bless" in targets  # ambiguous_function_call_expression

    def test_finds_contains(self):
        contains = [e for e in self.edges if e.kind == "CONTAINS"]
        assert len(contains) >= 3


class TestXSParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample.xs")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("MyModule.xs")) == "c"

    def test_finds_structs(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "Point" in names

    def test_finds_functions(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "_add" in names
        assert "compute_distance" in names

    def test_finds_includes(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = {e.target for e in imports}
        assert "XSUB.h" in targets
        assert "string.h" in targets

    def test_finds_calls(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        targets = {e.target for e in calls}
        assert any(t == "_add" or t.endswith("::_add") for t in targets)

    def test_finds_contains(self):
        contains = [e for e in self.edges if e.kind == "CONTAINS"]
        assert len(contains) >= 3


class TestLuaParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample.lua")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("init.lua")) == "lua"
        assert self.parser.detect_language(Path("config.lua")) == "lua"

    def test_finds_top_level_functions(self):
        funcs = [
            n for n in self.nodes
            if n.kind == "Function" and n.parent_name is None
        ]
        names = {f.name for f in funcs}
        assert "greet" in names
        assert "helper" in names
        assert "process_animals" in names

    def test_finds_variable_assigned_functions(self):
        funcs = [
            n for n in self.nodes
            if n.kind == "Function" and n.parent_name is None
        ]
        names = {f.name for f in funcs}
        assert "transform" in names
        assert "validate" in names

    def test_finds_dot_syntax_methods(self):
        funcs = [
            n for n in self.nodes
            if n.kind == "Function" and n.parent_name == "Animal"
        ]
        names = {f.name for f in funcs}
        assert "new" in names

    def test_finds_colon_syntax_methods(self):
        funcs = [
            n for n in self.nodes
            if n.kind == "Function" and n.parent_name == "Animal"
        ]
        names = {f.name for f in funcs}
        assert "speak" in names
        assert "rename" in names

    def test_finds_inherited_table_methods(self):
        dog_funcs = [
            n for n in self.nodes
            if n.kind in ("Function", "Test") and n.parent_name == "Dog"
        ]
        names = {f.name for f in dog_funcs}
        assert "new" in names
        assert "fetch" in names

    def test_finds_imports(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = {e.target for e in imports}
        assert "cjson" in targets
        assert "lib.utils" in targets
        assert "logging" in targets
        assert len(imports) == 3

    def test_finds_calls(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        targets = {e.target for e in calls}
        assert "print" in targets
        assert "setmetatable" in targets
        assert "assert" in targets

    def test_finds_contains(self):
        contains = [e for e in self.edges if e.kind == "CONTAINS"]
        targets = {e.target.split("::")[-1] for e in contains}
        assert "greet" in targets
        assert "helper" in targets
        assert "Animal.new" in targets
        assert "Animal.speak" in targets
        assert "Dog.fetch" in targets

    def test_method_parent_names(self):
        funcs = {
            (n.name, n.parent_name) for n in self.nodes
            if n.kind == "Function" and n.parent_name is not None
        }
        assert ("new", "Animal") in funcs
        assert ("speak", "Animal") in funcs
        assert ("rename", "Animal") in funcs
        assert ("new", "Dog") in funcs
        assert ("fetch", "Dog") in funcs

    def test_detects_test_functions(self):
        tests = [n for n in self.nodes if n.kind == "Test"]
        names = {t.name for t in tests}
        assert "test_greet" in names
        assert "test_animal_speak" in names
        assert "test_dog_fetch" in names
        assert len(tests) == 3

    def test_extracts_params(self):
        funcs = {n.name: n for n in self.nodes if n.kind == "Function"}
        assert funcs["greet"].params is not None
        assert "name" in funcs["greet"].params
        # Animal.new has (name, sound)
        animal_new = [
            n for n in self.nodes
            if n.name == "new" and n.parent_name == "Animal"
        ][0]
        assert animal_new.params is not None
        assert "name" in animal_new.params
        assert "sound" in animal_new.params

    def test_nodes_have_lua_language(self):
        for node in self.nodes:
            assert node.language == "lua"

    def test_calls_inside_methods(self):
        """Verify that calls inside methods have correct source qualified names."""
        calls = [e for e in self.edges if e.kind == "CALLS"]
        sources = {e.source.split("::")[-1] for e in calls}
        assert "Dog.fetch" in sources  # Dog:fetch calls self:speak and print
        assert "Animal.speak" in sources  # Animal:speak calls log:info


class TestLuauParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample.luau")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("init.luau")) == "luau"
        assert self.parser.detect_language(Path("module.luau")) == "luau"

    def test_finds_type_aliases(self):
        types = [n for n in self.nodes if n.kind == "Class"]
        names = {t.name for t in types}
        assert "Vector3" in names
        assert "Callback" in names

    def test_finds_top_level_functions(self):
        funcs = [
            n for n in self.nodes
            if n.kind == "Function" and n.parent_name is None
        ]
        names = {f.name for f in funcs}
        assert "greet" in names
        assert "add" in names
        assert "process_animals" in names

    def test_finds_variable_assigned_functions(self):
        funcs = [
            n for n in self.nodes
            if n.kind == "Function" and n.parent_name is None
        ]
        names = {f.name for f in funcs}
        assert "transform" in names

    def test_finds_dot_syntax_methods(self):
        funcs = [
            n for n in self.nodes
            if n.kind == "Function" and n.parent_name == "Animal"
        ]
        names = {f.name for f in funcs}
        assert "new" in names

    def test_finds_colon_syntax_methods(self):
        funcs = [
            n for n in self.nodes
            if n.kind == "Function" and n.parent_name == "Animal"
        ]
        names = {f.name for f in funcs}
        assert "speak" in names
        assert "rename" in names

    def test_finds_inherited_table_methods(self):
        dog_funcs = [
            n for n in self.nodes
            if n.kind in ("Function", "Test") and n.parent_name == "Dog"
        ]
        names = {f.name for f in dog_funcs}
        assert "new" in names
        assert "fetch" in names

    def test_finds_imports(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = {e.target for e in imports}
        assert "lib.utils" in targets
        assert "logging" in targets
        assert len(imports) >= 2

    def test_finds_calls(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        targets = {e.target for e in calls}
        assert "print" in targets
        assert "setmetatable" in targets
        assert "assert" in targets

    def test_finds_contains(self):
        contains = [e for e in self.edges if e.kind == "CONTAINS"]
        targets = {e.target.split("::")[-1] for e in contains}
        assert "greet" in targets
        assert "add" in targets
        assert "Animal.new" in targets
        assert "Animal.speak" in targets
        assert "Dog.fetch" in targets

    def test_method_parent_names(self):
        funcs = {
            (n.name, n.parent_name) for n in self.nodes
            if n.kind == "Function" and n.parent_name is not None
        }
        assert ("new", "Animal") in funcs
        assert ("speak", "Animal") in funcs
        assert ("rename", "Animal") in funcs
        assert ("new", "Dog") in funcs
        assert ("fetch", "Dog") in funcs

    def test_detects_test_functions(self):
        tests = [n for n in self.nodes if n.kind == "Test"]
        names = {t.name for t in tests}
        assert "test_greet" in names
        assert "test_animal_speak" in names
        assert "test_dog_fetch" in names
        assert len(tests) == 3

    def test_nodes_have_luau_language(self):
        for node in self.nodes:
            assert node.language == "luau"

    def test_calls_inside_methods(self):
        """Verify that calls inside methods have correct source qualified names."""
        calls = [e for e in self.edges if e.kind == "CALLS"]
        sources = {e.source.split("::")[-1] for e in calls}
        assert "Dog.fetch" in sources
        assert "Animal.speak" in sources


class TestObjectiveCParsing:
    """Objective-C parser — closes #88."""

    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample.m")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("foo.m")) == "objc"

    def test_nodes_have_objc_language(self):
        for n in self.nodes:
            assert n.language == "objc"

    def test_finds_class(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        # Both @interface and @implementation produce Class nodes; that's
        # fine because they upsert to the same qualified name in the store.
        names = {c.name for c in classes}
        assert "Calculator" in names

    def test_finds_instance_and_class_methods(self):
        funcs = {
            (n.name, n.parent_name) for n in self.nodes if n.kind == "Function"
        }
        assert ("add", "Calculator") in funcs
        assert ("reset", "Calculator") in funcs
        assert ("logResult", "Calculator") in funcs
        assert ("sharedCalculator", "Calculator") in funcs

    def test_finds_c_main(self):
        """Top-level C-style main() must be extracted via the
        function_declarator pattern that C/C++ already use (#88)."""
        funcs = [n for n in self.nodes if n.kind == "Function"]
        main_fn = next((f for f in funcs if f.name == "main"), None)
        assert main_fn is not None
        assert main_fn.parent_name is None  # top-level, not attached to a class

    def test_finds_imports(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = {e.target for e in imports}
        # Angle-bracket system headers and quoted user headers both arrive
        # as preproc_include in tree-sitter-objc.
        assert any("Foundation" in t for t in targets)
        assert any("Logger" in t for t in targets)

    def test_extracts_message_expression_calls(self):
        """Objective-C uses [receiver method:args] for method calls; these
        must produce CALLS edges (#88)."""
        calls = [e for e in self.edges if e.kind == "CALLS"]
        targets = [e.target for e in calls]
        # Internal [self logResult:sum] should resolve to Calculator.logResult
        assert any(t.endswith("::Calculator.logResult") for t in targets)
        # [Calculator sharedCalculator] from main should also resolve
        assert any(t.endswith("::Calculator.sharedCalculator") for t in targets)
        # External NSLog(...) call_expression should be captured too
        assert "NSLog" in targets


class TestBashParsing:
    """Bash/Shell parser — closes #197."""

    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample.sh")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("build.sh")) == "bash"
        assert self.parser.detect_language(Path("build.bash")) == "bash"
        assert self.parser.detect_language(Path("run.zsh")) == "bash"
        # Regression for #235 — Korn shell (.ksh) should parse as bash.
        assert self.parser.detect_language(Path("legacy.ksh")) == "bash"

    def test_ksh_extension_parses_as_bash(self, tmp_path):
        """Regression for #235: a real .ksh file is parsed through the bash
        grammar end-to-end and produces the same structural nodes/edges
        as an equivalent .sh file."""
        fixture_source = (FIXTURES / "sample.sh").read_text(encoding="utf-8")
        ksh_copy = tmp_path / "legacy.ksh"
        ksh_copy.write_text(fixture_source, encoding="utf-8")

        ksh_nodes, ksh_edges = self.parser.parse_file(ksh_copy)

        # Language tagging: every node must be "bash".
        assert ksh_nodes, "parser produced zero nodes for .ksh file"
        for n in ksh_nodes:
            assert n.language == "bash"

        # Same function set as the .sh fixture.
        ksh_funcs = {n.name for n in ksh_nodes if n.kind == "Function"}
        sh_funcs = {n.name for n in self.nodes if n.kind == "Function"}
        assert ksh_funcs == sh_funcs, (
            f".ksh and .sh produced different function sets: "
            f"sh-only={sh_funcs - ksh_funcs}, ksh-only={ksh_funcs - sh_funcs}"
        )

        # Same structural-edge totals by kind.
        def by_kind(edges):
            counts: dict[str, int] = {}
            for e in edges:
                counts[e.kind] = counts.get(e.kind, 0) + 1
            return counts
        assert by_kind(ksh_edges) == by_kind(self.edges)

    def test_nodes_have_bash_language(self):
        for n in self.nodes:
            assert n.language == "bash"

    def test_finds_functions(self):
        funcs = {n.name for n in self.nodes if n.kind == "Function"}
        assert "log_info" in funcs
        assert "log_error" in funcs
        assert "ensure_dir" in funcs
        assert "cleanup" in funcs
        assert "main" in funcs

    def test_functions_have_no_parent(self):
        """Bash has no classes so every function should be top-level."""
        for n in self.nodes:
            if n.kind == "Function":
                assert n.parent_name is None

    def test_source_creates_import_edge(self):
        """`source ./lib.sh` / `. ./config.sh` should produce IMPORTS_FROM
        edges (#197)."""
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        assert len(imports) >= 2
        targets = [e.target for e in imports]
        # sample_lib.sh exists on disk so should be resolved to an absolute path
        assert any(t.endswith("sample_lib.sh") for t in targets)
        # sample_config.sh doesn't exist; unresolved path is kept as-is
        assert any("sample_config.sh" in t for t in targets)

    def test_command_invocations_create_call_edges(self):
        """Each `command` node inside a function body should become a
        CALLS edge keyed on its command_name (#197)."""
        calls = [e for e in self.edges if e.kind == "CALLS"]
        targets = {e.target for e in calls}
        # Built-ins and external commands kept as bare names
        assert "echo" in targets
        assert "mkdir" in targets
        # Internal function calls should resolve to qualified names
        assert any(t.endswith("::log_info") for t in targets)
        assert any(t.endswith("::ensure_dir") for t in targets)
        assert any(t.endswith("::cleanup") for t in targets)

    def test_main_calls_resolve_to_internal_functions(self):
        """main() should have CALLS edges to log_info, ensure_dir, and cleanup."""
        calls = [
            e for e in self.edges
            if e.kind == "CALLS" and e.source.endswith("::main")
        ]
        call_targets = {e.target for e in calls}
        assert any(t.endswith("::log_info") for t in call_targets)
        assert any(t.endswith("::ensure_dir") for t in call_targets)
        assert any(t.endswith("::cleanup") for t in call_targets)


class TestElixirParsing:
    """Elixir parser — closes #112."""

    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample.ex")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("lib.ex")) == "elixir"
        assert self.parser.detect_language(Path("script.exs")) == "elixir"

    def test_nodes_have_elixir_language(self):
        for n in self.nodes:
            assert n.language == "elixir"

    def test_modules_become_classes(self):
        classes = {n.name for n in self.nodes if n.kind == "Class"}
        assert "Calculator" in classes
        assert "MathHelpers" in classes

    def test_def_defp_produce_functions_with_parent_module(self):
        funcs = {
            (n.name, n.parent_name) for n in self.nodes if n.kind == "Function"
        }
        # public defs
        assert ("add", "Calculator") in funcs
        assert ("subtract", "Calculator") in funcs
        assert ("compute", "Calculator") in funcs
        assert ("double", "MathHelpers") in funcs
        assert ("triple", "MathHelpers") in funcs
        # private defp
        assert ("log", "Calculator") in funcs

    def test_alias_import_require_produce_imports(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = [e.target for e in imports]
        # alias Calculator, import Calculator, require Logger
        assert targets.count("Calculator") >= 2
        assert "Logger" in targets

    def test_internal_calls_resolve_to_qualified_names(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        targets = {e.target for e in calls}
        # Calculator.compute calls add() and log() — both inside Calculator
        assert any(t.endswith("::Calculator.add") for t in targets)
        assert any(t.endswith("::Calculator.log") for t in targets)
        # MathHelpers.double calls Calculator.compute
        assert any(t.endswith("::Calculator.compute") for t in targets)
        # MathHelpers.triple calls double() — within the same module
        assert any(t.endswith("::MathHelpers.double") for t in targets)

    def test_contains_edges_wire_module_to_functions(self):
        contains = [e for e in self.edges if e.kind == "CONTAINS"]
        # Each function should be CONTAINS-linked to its parent module
        function_targets = {
            e.target for e in contains
            if "::" in e.source and "Calculator" in e.source
        }
        assert any(t.endswith("::Calculator.add") for t in function_targets)
        assert any(t.endswith("::Calculator.compute") for t in function_targets)


class TestGDScriptParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample.gd")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("player.gd")) == "gdscript"
        assert self.parser.detect_language(Path("globals/manager.gd")) == "gdscript"

    def test_finds_class_name_statement(self):
        """File-level ``class_name X`` declaration becomes a Class node."""
        classes = {n.name for n in self.nodes if n.kind == "Class"}
        assert "SampleManager" in classes

    def test_finds_inner_class(self):
        classes = {n.name for n in self.nodes if n.kind == "Class"}
        assert "Item" in classes

    def test_finds_top_level_functions(self):
        funcs = [
            n for n in self.nodes
            if n.kind == "Function" and n.parent_name is None
        ]
        names = {f.name for f in funcs}
        for expected in ("_ready", "_load_items", "get_item", "helper"):
            assert expected in names, f"missing top-level function {expected}"

    def test_finds_inner_class_methods(self):
        """Methods defined inside ``class Inner:`` should attach to the inner class."""
        inner_funcs = [
            n for n in self.nodes
            if n.kind == "Function" and n.parent_name == "Item"
        ]
        names = {f.name for f in inner_funcs}
        assert "promote" in names

    def test_finds_extends_as_import(self):
        """``extends Node`` is the GDScript analogue of an import — parent class."""
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = {e.target for e in imports}
        assert "Node" in targets, f"expected Node in imports, got {targets}"

    def test_finds_direct_calls(self):
        """Bare calls (``range(...)``, ``_load_items()``) produce CALLS edges."""
        calls = [e for e in self.edges if e.kind == "CALLS"]
        targets = {e.target for e in calls}
        assert "range" in targets

    def test_finds_attribute_calls(self):
        """``obj.method(...)`` calls live inside ``attribute`` nodes as ``attribute_call``."""
        calls = [e for e in self.edges if e.kind == "CALLS"]
        targets = {e.target for e in calls}
        # timer.start(), items.append(item), item_added.emit(item)
        assert "start" in targets
        assert "append" in targets
        assert "emit" in targets

    def test_internal_calls_resolve_to_qualified_names(self):
        """A bare ``_load_items()`` call inside _ready should resolve to the
        same-file function's qualified name."""
        calls = [e for e in self.edges if e.kind == "CALLS"]
        targets = {e.target for e in calls}
        assert any(t.endswith("::_load_items") for t in targets), (
            f"expected ::_load_items in call targets, got {targets}"
        )

    def test_contains_edges_wire_classes_and_functions(self):
        contains = [(e.source, e.target) for e in self.edges if e.kind == "CONTAINS"]
        # File CONTAINS the top-level Class and Function nodes.
        file_contains = {t for s, t in contains if not s.endswith(".gd::Item")
                         and not s.endswith(".gd::SampleManager")}
        assert any(t.endswith("::SampleManager") for t in file_contains)
        assert any(t.endswith("::Item") for t in file_contains)
        assert any(t.endswith("::_ready") for t in file_contains)
        # Inner class CONTAINS its method.
        item_contains = {t for s, t in contains if s.endswith("::Item")}
        assert any(t.endswith("::Item.promote") for t in item_contains)

class TestJuliaParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample.jl")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("foo.jl")) == "julia"

    def test_finds_module(self):
        classes = {n.name for n in self.nodes if n.kind == "Class"}
        assert "SampleModule" in classes

    def test_finds_structs(self):
        classes = {n.name for n in self.nodes if n.kind == "Class"}
        assert "Dog" in classes
        assert "MutablePoint" in classes

    def test_finds_abstract_types(self):
        classes = {n.name for n in self.nodes if n.kind == "Class"}
        assert "AbstractAnimal" in classes

    def test_struct_inheritance(self):
        inherits = [e for e in self.edges if e.kind == "INHERITS"]
        # Dog's qualified source is file::SampleModule.Dog; we only care
        # about the trailing struct name and the target.
        pairs = {
            (e.source.split("::")[-1].split(".")[-1], e.target)
            for e in inherits
        }
        assert ("Dog", "AbstractAnimal") in pairs

    def test_finds_long_form_functions(self):
        funcs = {n.name for n in self.nodes if n.kind == "Function"}
        assert "greet" in funcs
        assert "outer" in funcs
        assert "inner" in funcs
        assert "process" in funcs
        assert "show" in funcs

    def test_finds_short_form_functions(self):
        funcs = {n.name for n in self.nodes if n.kind == "Function"}
        assert "add" in funcs
        assert "square" in funcs

    def test_finds_macros(self):
        funcs = {n.name for n in self.nodes if n.kind == "Function"}
        assert "sayhello" in funcs

    def test_finds_imports(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = {e.target for e in imports}
        assert "LinearAlgebra" in targets
        assert "JSON" in targets

    def test_finds_selective_imports(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = {e.target for e in imports}
        assert "Statistics.mean" in targets or "Statistics" in targets
        assert "Statistics.std" in targets or "Statistics" in targets

    def test_finds_base_imports(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = {e.target for e in imports}
        assert "Base.show" in targets or "Base" in targets
        assert "Base.print" in targets or "Base" in targets

    def test_finds_include(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = {e.target for e in imports}
        assert any("utils.jl" in t for t in targets)

    def test_finds_calls(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        assert len(calls) >= 1

    def test_finds_contains(self):
        contains = [e for e in self.edges if e.kind == "CONTAINS"]
        assert len(contains) >= 3

    def test_finds_exports(self):
        refs = [
            e for e in self.edges
            if e.kind == "REFERENCES"
            and e.extra
            and e.extra.get("julia_export")
        ]
        # Targets may be resolved to qualified names (file::SampleModule.greet)
        # if the exported symbol is defined locally; otherwise they stay bare.
        trailing = {e.target.split(".")[-1] for e in refs}
        assert "greet" in trailing
        assert "Dog" in trailing
        assert "process" in trailing

    def test_finds_testsets(self):
        tests = [n for n in self.nodes if n.kind == "Test"]
        assert any("Arithmetic" in t.name for t in tests)

    def test_nested_function_parent(self):
        contains = [e for e in self.edges if e.kind == "CONTAINS"]
        # The CONTAINS edge for inner should originate from outer, and
        # its qualified target should carry `outer.inner` in the name.
        assert any(
            e.source.endswith("outer")
            and e.target.endswith("outer.inner")
            for e in contains
        )

    def test_qualified_function_name(self):
        funcs = {n.name for n in self.nodes if n.kind == "Function"}
        # function Base.show(...) -> name is "show", not "Base.show"
        assert "show" in funcs
        assert "Base.show" not in funcs

    def test_nodes_have_julia_language(self):
        nameable = [n for n in self.nodes if n.kind in ("Class", "Function", "Test")]
        assert all(n.language == "julia" for n in nameable)
        assert len(nameable) >= 5

    def test_finds_enum_type(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        by_name = {c.name: c for c in classes}
        assert "Color" in by_name
        assert by_name["Color"].extra.get("julia_kind") == "enum"

    def test_finds_enum_variants(self):
        variants = {
            n.name for n in self.nodes
            if n.kind == "Function"
            and (n.extra or {}).get("julia_kind") == "enum_variant"
        }
        assert {"RED", "BLUE", "GREEN"} <= variants

    def test_enum_variants_contained_by_type(self):
        contains = [e for e in self.edges if e.kind == "CONTAINS"]
        # Color -> RED, BLUE, GREEN
        variants_under_color = {
            e.target.split(".")[-1]
            for e in contains
            if e.source.endswith("Color")
        }
        assert {"RED", "BLUE", "GREEN"} <= variants_under_color

    def test_finds_public_symbols(self):
        refs = [
            e for e in self.edges
            if e.kind == "REFERENCES"
            and e.extra
            and e.extra.get("julia_public")
        ]
        trailing = {e.target.split(".")[-1] for e in refs}
        assert "square" in trailing
        assert "add" in trailing

    def test_qualified_function_references_base(self):
        refs = [e for e in self.edges if e.kind == "REFERENCES"]
        # function Base.show(...) should emit a REFERENCES edge to Base
        assert any(
            "show" in e.source and e.target == "Base"
            for e in refs
        )

class TestRescriptParser:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample.res")

    def test_detects_language_for_res_and_resi(self):
        assert self.parser.detect_language(Path("lib.res")) == "rescript"
        assert self.parser.detect_language(Path("lib.resi")) == "rescript"

    def test_file_node(self):
        files = [n for n in self.nodes if n.kind == "File"]
        assert len(files) == 1
        assert files[0].language == "rescript"
        assert files[0].extra.get("rescript_interface") is not True

    def test_finds_top_level_modules(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert {"User", "App", "Validator"}.issubset(names)

    def test_nested_module_has_parent(self):
        validator = next(
            n for n in self.nodes if n.kind == "Class" and n.name == "Validator"
        )
        assert validator.parent_name == "User"

    def test_finds_top_level_lets(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "main" in names
        assert "defaultTimeout" in names
        assert "fact" in names
        assert "helper" in names

    def test_let_inside_let_body_is_not_top_level(self):
        # `let u = ...` inside App.start should NOT appear as a Function node.
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "u" not in names
        assert "valid" not in names
        assert "n" not in names

    def test_external_binding_extracted(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        by_name = {f.name: f for f in funcs}
        assert "readFile" in by_name
        assert by_name["readFile"].extra.get("rescript_external") is True

    def test_module_attr_creates_import_edge(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = {e.target for e in imports}
        assert "fs" in targets

    def test_open_and_include_create_import_edges(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = {e.target for e in imports}
        assert "Belt" in targets
        assert "Js.Promise" in targets

    def test_types_extracted(self):
        types = [n for n in self.nodes if n.kind == "Type"]
        names = {t.name for t in types}
        assert {"status", "result", "t", "config"}.intersection(names)

    def test_member_let_has_parent_module(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        by_name = {f.name: f for f in funcs}
        assert by_name["greet"].parent_name == "User"
        assert by_name["isAdult"].parent_name == "Validator"
        assert by_name["start"].parent_name == "App"

    def test_calls_attributed_to_enclosing_let(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        sources = {e.source for e in calls}
        targets = {e.target for e in calls}
        assert any(s.endswith("::App.start") for s in sources)
        assert "User.make" in targets or any(
            t.endswith("::User.make") for t in targets
        )

    def test_contains_edges_wire_module_to_members(self):
        contains = [e for e in self.edges if e.kind == "CONTAINS"]
        targets = {e.target for e in contains}
        assert any(t.endswith("::User.greet") for t in targets)
        assert any(t.endswith("::Validator.isAdult") for t in targets)

    def test_nodes_have_rescript_language(self):
        non_file = [n for n in self.nodes if n.kind != "File"]
        assert all(n.language == "rescript" for n in non_file)


class TestRescriptInterfaceParser:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample.resi")

    def test_file_flagged_as_interface(self):
        file_node = next(n for n in self.nodes if n.kind == "File")
        assert file_node.extra.get("rescript_interface") is True

    def test_modules_extracted_from_interface(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "User" in names
        assert "App" in names
        assert "Validator" in names

    def test_signatures_extracted_without_bodies(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        # Top-level and module-member signatures should both appear.
        assert "defaultTimeout" in names
        assert "fact" in names
        assert "make" in names
        assert "greet" in names
        assert "isAdult" in names
        assert "start" in names

    def test_external_signature_extracted(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        by_name = {f.name: f for f in funcs}
        assert "readFile" in by_name
        assert by_name["readFile"].extra.get("rescript_external") is True

    def test_no_calls_extracted_from_interface(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        assert calls == []


class TestRescriptEdgeCases:
    """Bug-fix tests: IMPORTS_FROM dedup, JS binding tag, JSX, module alias."""

    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample.res")

    def test_duplicate_open_produces_single_import_edge(self):
        # sample.res has `open Belt` twice — should emit only one edge.
        belt_edges = [
            e for e in self.edges
            if e.kind == "IMPORTS_FROM" and e.target == "Belt"
        ]
        assert len(belt_edges) == 1

    def test_module_alias_emits_import_edge(self):
        # `module IntMap = Belt.Map.Int` → IMPORTS_FROM Belt.Map.Int
        aliases = [
            e for e in self.edges
            if e.extra.get("rescript_import_kind") == "module_alias"
        ]
        assert any(e.target == "Belt.Map.Int" for e in aliases)
        assert any(e.extra.get("alias_name") == "IntMap" for e in aliases)

    def test_module_alias_is_not_treated_as_block_module(self):
        # IntMap is an alias — should NOT appear as a Class node.
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "IntMap" not in names

    def test_js_binding_module_is_tagged(self):
        text_encoder = next(
            n for n in self.nodes if n.kind == "Class" and n.name == "TextEncoder"
        )
        assert text_encoder.extra.get("rescript_kind") == "js_binding"

    def test_regular_module_keeps_module_tag(self):
        user = next(
            n for n in self.nodes if n.kind == "Class" and n.name == "User"
        )
        assert user.extra.get("rescript_kind") == "module"

    def test_jsx_emits_import_and_call_edges(self):
        jsx_imports = [
            e for e in self.edges
            if e.extra.get("rescript_import_kind") == "jsx"
        ]
        jsx_targets = {e.target for e in jsx_imports}
        assert "Layout" in jsx_targets
        assert "User" in jsx_targets
        assert "AnalyticsFilterUi" in jsx_targets

        jsx_calls = [
            e for e in self.edges
            if e.kind == "CALLS"
            and e.extra.get("rescript_call_kind") == "jsx"
        ]
        call_targets = {e.target for e in jsx_calls}
        assert "User.Badge" in call_targets
        assert "AnalyticsFilterUi.Filter" in call_targets

    def test_jsx_call_attributed_to_enclosing_let(self):
        jsx_calls = [
            e for e in self.edges
            if e.kind == "CALLS"
            and e.extra.get("rescript_call_kind") == "jsx"
        ]
        assert all(e.source.endswith("::render") for e in jsx_calls)


class TestRescriptCrossModuleResolver:
    """Integration test for the cross-module resolver post-pass."""

    def _build(self, tmp_path):
        from code_review_graph.graph import GraphStore
        from code_review_graph.incremental import full_build

        (tmp_path / ".git").mkdir()

        (tmp_path / "LogicUtils.res").write_text(
            "let safeParse = (s) => s\n"
            "let trim = (s) => s\n"
        )
        (tmp_path / "CurrencyFormatUtils.res").write_text(
            "let format = (n) => n\n"
        )
        (tmp_path / "Caller.res").write_text(
            "open CurrencyFormatUtils\n"
            "let run = () => {\n"
            "  let a = LogicUtils.safeParse(\"x\")\n"
            "  let b = LogicUtils.safeParse(\"y\")\n"
            "  let c = format(12.0)\n"
            "  let d = <Layout name=\"hi\" />\n"
            "  (a, b, c, d)\n"
            "}\n"
        )
        (tmp_path / "Layout.res").write_text(
            "let make = (~name) => name\n"
        )

        store = GraphStore(tmp_path / "graph.db")
        result = full_build(tmp_path, store)
        return store, result

    def test_qualified_call_resolves_to_canonical_node(self, tmp_path):
        store, _ = self._build(tmp_path)
        cur = store._conn.cursor()
        rows = cur.execute(
            "SELECT target_qualified FROM edges "
            "WHERE kind='CALLS' AND source_qualified LIKE '%Caller.res::run'"
        ).fetchall()
        targets = {r["target_qualified"] for r in rows}
        # Both LogicUtils.safeParse callsites should now point to the canonical
        # node path, not the bare `LogicUtils.safeParse` string.
        assert any(
            t.endswith("LogicUtils.res::safeParse") for t in targets
        ), f"no canonical resolution in {targets}"
        assert not any(t == "LogicUtils.safeParse" for t in targets)

    def test_callers_of_canonical_node_finds_both_sites(self, tmp_path):
        store, _ = self._build(tmp_path)
        # Two calls to safeParse from the same caller — both should survive
        # as separate edges pointing to the canonical node.
        cur = store._conn.cursor()
        count = cur.execute(
            "SELECT COUNT(*) as c FROM edges "
            "WHERE kind='CALLS' "
            "AND target_qualified LIKE '%LogicUtils.res::safeParse'"
        ).fetchone()["c"]
        assert count == 2

    def test_bare_call_resolves_via_open_directive(self, tmp_path):
        store, _ = self._build(tmp_path)
        cur = store._conn.cursor()
        rows = cur.execute(
            "SELECT target_qualified FROM edges WHERE kind='CALLS' "
            "AND target_qualified LIKE '%CurrencyFormatUtils.res::format'"
        ).fetchall()
        assert len(rows) == 1

    def test_imports_from_rewrites_to_file_path(self, tmp_path):
        store, _ = self._build(tmp_path)
        cur = store._conn.cursor()
        rows = cur.execute(
            "SELECT target_qualified FROM edges WHERE kind='IMPORTS_FROM' "
            "AND file_path LIKE '%Caller.res'"
        ).fetchall()
        targets = {r["target_qualified"] for r in rows}
        # `open CurrencyFormatUtils` and `<Layout />` should both resolve
        # to file paths.
        assert any(t.endswith("CurrencyFormatUtils.res") for t in targets)
        assert any(t.endswith("Layout.res") for t in targets)

    def test_resolver_stats_in_build_result(self, tmp_path):
        _, result = self._build(tmp_path)
        stats = result["rescript_resolution"]
        assert stats["files_indexed"] == 4
        assert stats["calls_resolved"] >= 3
        assert stats["imports_resolved"] >= 2

    def test_resolver_is_idempotent(self, tmp_path):
        from code_review_graph.rescript_resolver import (
            resolve_rescript_cross_module,
        )
        store, _ = self._build(tmp_path)
        second = resolve_rescript_cross_module(store)
        # Second run should find nothing new — all already resolved.
        assert second["calls_resolved"] == 0
        assert second["imports_resolved"] == 0

class TestNixParsing:
    """Flake-aware Nix parser — see the Nix language-support epic."""

    def setup_method(self):
        self.parser = CodeParser()
        # Parse the flake-shaped fixture as if its basename were ``flake.nix``
        # so the ``inputs.*.url`` branch of _extract_nix_constructs fires.
        flake_bytes = (FIXTURES / "sample.nix").read_bytes()
        self.flake_path = FIXTURES / "flake.nix"
        self.flake_nodes, self.flake_edges = self.parser.parse_bytes(
            self.flake_path, flake_bytes,
        )
        # The non-flake fixture retains its actual path; it's used to verify
        # the flake-input branch does *not* fire on non-flake files.
        module_path = FIXTURES / "sample_module.nix"
        self.module_nodes, self.module_edges = self.parser.parse_file(module_path)

    def test_detects_language(self):
        assert self.parser.detect_language(Path("flake.nix")) == "nix"
        assert self.parser.detect_language(Path("modules/foo.nix")) == "nix"

    def test_nodes_have_nix_language(self):
        for n in self.flake_nodes:
            assert n.language == "nix"
        for n in self.module_nodes:
            assert n.language == "nix"

    def test_top_level_bindings_become_functions(self):
        funcs = {n.name for n in self.flake_nodes if n.kind == "Function"}
        # Top-level bindings from sample.nix (flake-shaped).
        assert "description" in funcs
        assert "inputs" in funcs
        assert "outputs" in funcs
        # Nested bindings flattened to dotted names.
        assert "packages.default" in funcs
        assert "devShells.default" in funcs

    def test_flake_inputs_produce_import_edges(self):
        targets = {
            e.target for e in self.flake_edges if e.kind == "IMPORTS_FROM"
        }
        assert "github:NixOS/nixpkgs/nixos-unstable" in targets
        assert "github:numtide/flake-utils" in targets

    def test_import_and_callpackage_produce_import_edges(self):
        targets = {
            e.target for e in self.flake_edges if e.kind == "IMPORTS_FROM"
        }
        # callPackage ./default.nix and import ./shell.nix. Relative paths
        # are resolved against the caller's directory when possible; since
        # neither file exists alongside the fixture, the raw relative
        # path is preserved.
        assert "./default.nix" in targets
        assert "./shell.nix" in targets

    def test_non_flake_file_has_no_input_edges(self):
        # ``sample_module.nix`` is not named ``flake.nix``, so the
        # inputs.*.url branch must not fire — no github:-prefixed targets.
        targets = [
            e.target for e in self.module_edges if e.kind == "IMPORTS_FROM"
        ]
        assert not any(t.startswith("github:") for t in targets)
        # The import ./foo.nix inside the `let` body still produces an edge.
        assert any("foo.nix" in t for t in targets)

    def test_contains_edges_wire_file_to_top_level_bindings(self):
        file_path = self.flake_path.as_posix()
        contains_targets = {
            e.target for e in self.flake_edges
            if e.kind == "CONTAINS" and e.source == file_path
        }
        # Each top-level binding should be CONTAINS-linked from the file.
        for name in ("description", "inputs", "outputs"):
            qualified = f"{file_path}::{name}"
            assert qualified in contains_targets, (
                f"missing CONTAINS edge for {qualified}"
            )


class TestSpringDIParsing:
    """Tests for Spring DI annotation detection and INJECTS edge generation."""

    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "SpringDI.java")

    def test_detects_spring_stereotype_on_repository(self):
        classes = {n.name: n for n in self.nodes if n.kind == "Class"}
        assert "JpaOrderRepository" in classes
        assert classes["JpaOrderRepository"].extra.get("spring_stereotype") == "Repository"

    def test_detects_spring_stereotype_on_service(self):
        classes = {n.name: n for n in self.nodes if n.kind == "Class"}
        assert "NotificationService" in classes
        assert classes["NotificationService"].extra.get("spring_stereotype") == "Service"
        assert "OrderService" in classes
        assert classes["OrderService"].extra.get("spring_stereotype") == "Service"

    def test_detects_spring_stereotype_on_configuration(self):
        classes = {n.name: n for n in self.nodes if n.kind == "Class"}
        assert "AppConfig" in classes
        assert classes["AppConfig"].extra.get("spring_stereotype") == "Configuration"

    def test_no_stereotype_on_plain_interface(self):
        classes = {n.name: n for n in self.nodes if n.kind == "Class"}
        assert "OrderRepository" in classes
        assert "spring_stereotype" not in classes["OrderRepository"].extra

    def test_spring_annotations_list_stored(self):
        classes = {n.name: n for n in self.nodes if n.kind == "Class"}
        annotations = classes["OrderService"].extra.get("spring_annotations", [])
        assert "Service" in annotations
        assert "RequiredArgsConstructor" in annotations

    def test_autowired_field_injection_edge(self):
        injects = [e for e in self.edges if e.kind == "INJECTS"]
        # NotificationService has @Autowired OrderRepository field
        field_edges = [e for e in injects if e.extra.get("injection_type") == "field"]
        targets = {e.target for e in field_edges}
        assert "OrderRepository" in targets

    def test_autowired_field_source_is_class(self):
        injects = [e for e in self.edges if e.kind == "INJECTS"
                   and e.extra.get("injection_type") == "field"]
        sources = {e.source for e in injects}
        assert any("NotificationService" in s for s in sources)

    def test_lombok_required_args_constructor_injection(self):
        injects = [e for e in self.edges if e.kind == "INJECTS"]
        lombok_edges = [e for e in injects
                        if e.extra.get("injection_type") == "constructor_lombok"]
        targets = {e.target for e in lombok_edges}
        # OrderService has two final injected fields
        assert "OrderRepository" in targets
        assert "NotificationService" in targets

    def test_static_final_field_not_injected(self):
        """static final String TAG should NOT produce an INJECTS edge."""
        injects = [e for e in self.edges if e.kind == "INJECTS"]
        targets = {e.target for e in injects}
        assert "String" not in targets

    def test_explicit_autowired_constructor_injection(self):
        injects = [e for e in self.edges if e.kind == "INJECTS"]
        ctor_edges = [e for e in injects
                      if e.extra.get("injection_type") == "constructor"]
        targets = {e.target for e in ctor_edges}
        # AuditLogger has @Autowired constructor with OrderRepository param
        assert "OrderRepository" in targets

    def test_autowired_constructor_source_is_class(self):
        injects = [e for e in self.edges if e.kind == "INJECTS"
                   and e.extra.get("injection_type") == "constructor"]
        sources = {e.source for e in injects}
        assert any("AuditLogger" in s for s in sources)

    def test_total_injects_edge_count(self):
        """Sanity check: total INJECTS edges matches known injection points."""
        injects = [e for e in self.edges if e.kind == "INJECTS"]
        # NotificationService: 1 field
        # OrderService: 2 lombok (orderRepository + notificationService)
        # AuditLogger: 1 constructor
        assert len(injects) >= 4

    def test_field_name_stored_in_injects_extra(self):
        """INJECTS edges must carry extra.field_name for the resolver."""
        injects = [e for e in self.edges if e.kind == "INJECTS"]
        names = {e.extra.get("field_name") for e in injects}
        # @Autowired field in NotificationService
        assert "orderRepository" in names
        # @RequiredArgsConstructor final fields in OrderService
        assert "orderRepository" in names
        assert "notificationService" in names
        # @Autowired constructor param in AuditLogger
        assert "orderRepository" in names

    def test_java_method_call_target_is_method_not_receiver(self):
        """Java receiver.method() must emit CALLS with method as target, not receiver."""
        calls = [e for e in self.edges if e.kind == "CALLS"]
        targets = {e.target for e in calls}
        # placeOrder calls orderRepository.save() — target must end in "save"
        # (possibly qualified to "::OrderRepository.save" if same-file resolution kicks in)
        assert any("save" in t for t in targets), f"expected 'save' in targets, got {targets}"
        # receiver variable names must NOT appear as CALLS targets
        assert "orderRepository" not in targets
        assert "notificationService" not in targets

    def test_java_receiver_stored_in_calls_extra(self):
        """CALLS edges for Java method calls must carry extra.receiver."""
        calls = [e for e in self.edges if e.kind == "CALLS" and e.extra.get("receiver")]
        receivers = {e.extra["receiver"] for e in calls}
        assert "orderRepository" in receivers or "notificationService" in receivers


class TestSpringDIResolver:
    """Integration tests for the Spring DI post-build resolver."""

    def _build(self, tmp_path):
        """Build a mini Spring repo and run the resolver."""
        pkg = tmp_path / "src/main/java/com/example"
        pkg.mkdir(parents=True)

        (pkg / "OrderRepository.java").write_text(
            "package com.example;\n"
            "public interface OrderRepository {\n"
            "    void save(Order o);\n"
            "}\n"
        )
        (pkg / "JpaOrderRepository.java").write_text(
            "package com.example;\n"
            "import org.springframework.stereotype.Repository;\n"
            "@Repository\n"
            "public class JpaOrderRepository implements OrderRepository {\n"
            "    public void save(Order o) {}\n"
            "}\n"
        )
        (pkg / "OrderService.java").write_text(
            "package com.example;\n"
            "import org.springframework.stereotype.Service;\n"
            "import lombok.RequiredArgsConstructor;\n"
            "@Service\n"
            "@RequiredArgsConstructor\n"
            "public class OrderService {\n"
            "    private final OrderRepository orderRepository;\n"
            "    public void place(Order o) {\n"
            "        orderRepository.save(o);\n"
            "    }\n"
            "}\n"
        )

        from code_review_graph.graph import GraphStore
        from code_review_graph.incremental import full_build
        from code_review_graph.postprocessing import run_post_processing

        store = GraphStore(str(tmp_path / "graph.db"))
        result = full_build(tmp_path, store)
        run_post_processing(store)
        return store, result

    def test_resolver_runs_and_reports(self, tmp_path):
        _, result = self._build(tmp_path)
        stats = result.get("spring_resolution")
        assert stats is not None
        assert stats["files_indexed"] > 0

    def test_calls_resolved_through_field(self, tmp_path):
        store, result = self._build(tmp_path)
        stats = result.get("spring_resolution", {})
        assert stats.get("calls_resolved", 0) >= 1

    def test_resolved_target_includes_method_name(self, tmp_path):
        store, _ = self._build(tmp_path)
        cur = store._conn.cursor()
        rows = cur.execute(
            "SELECT target_qualified FROM edges WHERE kind='CALLS' "
            "AND extra LIKE '%spring_resolved%'"
        ).fetchall()
        assert rows, "Expected at least one spring-resolved CALLS edge"
        for (target,) in rows:
            assert "." in target or "::" in target, (
                f"Resolved target should contain type.method or ::, got: {target!r}"
            )


class TestTemporalParsing:
    """Tests for Temporal @WorkflowInterface / @ActivityInterface detection."""

    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "TemporalWorkflow.java")

    def test_workflow_interface_gets_temporal_role(self):
        classes = {n.name: n for n in self.nodes if n.kind == "Class"}
        assert "OrderWorkflow" in classes
        assert classes["OrderWorkflow"].extra.get("temporal_role") == "workflow_interface"

    def test_activity_interface_gets_temporal_role(self):
        classes = {n.name: n for n in self.nodes if n.kind == "Class"}
        assert "PaymentActivity" in classes
        assert classes["PaymentActivity"].extra.get("temporal_role") == "activity_interface"
        assert "ShippingActivity" in classes
        assert classes["ShippingActivity"].extra.get("temporal_role") == "activity_interface"

    def test_impl_class_has_no_temporal_role(self):
        classes = {n.name: n for n in self.nodes if n.kind == "Class"}
        assert "OrderWorkflowImpl" in classes
        assert "temporal_role" not in classes["OrderWorkflowImpl"].extra

    def test_temporal_stub_edges_emitted_for_activity_fields(self):
        stubs = [e for e in self.edges if e.kind == "TEMPORAL_STUB"]
        targets = {e.target for e in stubs}
        assert "PaymentActivity" in targets
        assert "ShippingActivity" in targets

    def test_temporal_stub_field_name_stored(self):
        stubs = [e for e in self.edges if e.kind == "TEMPORAL_STUB"]
        field_names = {e.extra.get("field_name") for e in stubs}
        assert "paymentActivity" in field_names
        assert "shippingActivity" in field_names

    def test_static_field_not_in_temporal_stubs(self):
        stubs = [e for e in self.edges if e.kind == "TEMPORAL_STUB"]
        field_names = {e.extra.get("field_name") for e in stubs}
        assert "TAG" not in field_names

    def test_temporal_stub_source_is_workflow_impl(self):
        stubs = [e for e in self.edges if e.kind == "TEMPORAL_STUB"]
        sources = {e.source for e in stubs}
        assert any("OrderWorkflowImpl" in s for s in sources)

    def test_workflow_method_annotation_stored_on_method(self):
        interface_methods = [
            n for n in self.nodes if n.kind == "Function" and n.parent_name == "OrderWorkflow"
        ]
        names = {n.name: n for n in interface_methods}
        assert "processOrder" in names
        assert names["processOrder"].extra.get("temporal_role") == "workflowmethod"

    def test_signal_method_annotation_stored(self):
        interface_methods = [
            n for n in self.nodes if n.kind == "Function" and n.parent_name == "OrderWorkflow"
        ]
        names = {n.name: n for n in interface_methods}
        assert "cancelOrder" in names
        assert names["cancelOrder"].extra.get("temporal_role") == "signalmethod"

    def test_activity_method_annotation_stored(self):
        activity_methods = [
            n for n in self.nodes if n.kind == "Function" and n.parent_name == "PaymentActivity"
        ]
        names = {n.name: n for n in activity_methods}
        assert "chargeCard" in names
        assert names["chargeCard"].extra.get("temporal_role") == "activitymethod"


class TestTemporalResolver:
    """Integration tests for the Temporal post-build call resolver."""

    def _build(self, tmp_path):
        pkg = tmp_path / "src/main/java/com/example"
        pkg.mkdir(parents=True)

        (pkg / "PaymentActivity.java").write_text(
            "package com.example;\n"
            "import io.temporal.activity.ActivityInterface;\n"
            "import io.temporal.activity.ActivityMethod;\n"
            "@ActivityInterface\n"
            "public interface PaymentActivity {\n"
            "    @ActivityMethod\n"
            "    boolean charge(String orderId);\n"
            "}\n"
        )
        (pkg / "PaymentActivityImpl.java").write_text(
            "package com.example;\n"
            "public class PaymentActivityImpl implements PaymentActivity {\n"
            "    public boolean charge(String orderId) { return true; }\n"
            "}\n"
        )
        (pkg / "OrderWorkflowImpl.java").write_text(
            "package com.example;\n"
            "public class OrderWorkflowImpl {\n"
            "    private PaymentActivity paymentActivity;\n"
            "    public String process(String id) {\n"
            "        return paymentActivity.charge(id) ? \"OK\" : \"FAIL\";\n"
            "    }\n"
            "}\n"
        )

        from code_review_graph.graph import GraphStore
        from code_review_graph.incremental import full_build

        store = GraphStore(str(tmp_path / "graph.db"))
        result = full_build(tmp_path, store)
        return store, result

    def test_temporal_resolver_runs_and_reports(self, tmp_path):
        _, result = self._build(tmp_path)
        stats = result.get("temporal_resolution")
        assert stats is not None
        assert stats["files_indexed"] > 0

    def test_calls_resolved_through_activity_stub(self, tmp_path):
        _, result = self._build(tmp_path)
        stats = result.get("temporal_resolution", {})
        assert stats.get("calls_resolved", 0) >= 1

    def test_resolved_target_is_fully_qualified(self, tmp_path):
        store, _ = self._build(tmp_path)
        rows = store._conn.execute(
            "SELECT target_qualified FROM edges WHERE kind='CALLS' "
            "AND extra LIKE '%temporal_resolved%'"
        ).fetchall()
        assert rows, "Expected at least one temporal-resolved CALLS edge"
        for (target,) in rows:
            assert "." in target or "::" in target, (
                f"Resolved target should be qualified, got: {target!r}"
            )

    def test_resolved_target_is_concrete_impl_not_interface(self, tmp_path):
        # paymentActivity.charge(...) has a single implementor, so it must
        # resolve to PaymentActivityImpl.charge, not the interface method
        # PaymentActivity.charge. Regression: implementors was keyed by the
        # bare interface name but looked up by the qualified name, so the
        # unique-implementor branch was dead and every stub call resolved to
        # the interface.
        store, _ = self._build(tmp_path)
        rows = store._conn.execute(
            "SELECT target_qualified FROM edges WHERE kind='CALLS' "
            "AND extra LIKE '%temporal_resolved%'"
        ).fetchall()
        targets = [t for (t,) in rows]
        assert targets, "Expected at least one temporal-resolved CALLS edge"
        assert any(t.endswith("PaymentActivityImpl.charge") for t in targets), (
            f"Expected resolution to the concrete impl, got: {targets!r}"
        )
        assert not any(t.endswith("PaymentActivity.charge") for t in targets), (
            f"Should not resolve to the interface method, got: {targets!r}"
        )


class TestKafkaParsing:
    """Tests for Kafka CONSUMES / PRODUCES edge detection."""

    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "KafkaPatterns.java")

    def test_kafka_listener_annotation_emits_consumes_edge(self):
        consumes = [e for e in self.edges if e.kind == "CONSUMES"]
        targets = {e.target for e in consumes}
        assert "kafka:order-events" in targets

    def test_kafka_listener_multiple_topics(self):
        consumes = [e for e in self.edges if e.kind == "CONSUMES"]
        targets = {e.target for e in consumes}
        assert "kafka:order-dlq" in targets
        assert "kafka:order-retry" in targets

    def test_kafka_listener_topic_in_extra(self):
        consumes = [e for e in self.edges if e.kind == "CONSUMES"
                    and e.target == "kafka:order-events"]
        assert consumes
        assert consumes[0].extra.get("topic") == "order-events"

    def test_kafka_template_field_emits_produces_edge(self):
        produces = [e for e in self.edges if e.kind == "PRODUCES"]
        sources = {e.source for e in produces}
        assert any("NotificationProducer" in s for s in sources)

    def test_kafka_receiver_field_emits_consumes_edge(self):
        consumes = [e for e in self.edges if e.kind == "CONSUMES"]
        sources = {e.source for e in consumes}
        assert any("ReactiveOrderConsumer" in s for s in sources)

    def test_kafka_receiver_message_type_stored(self):
        consumes = [e for e in self.edges if e.kind == "CONSUMES"
                    and "ReactiveOrderConsumer" in e.source]
        assert consumes
        assert consumes[0].extra.get("message_type") == "OrderEvent"

    def test_kafka_operations_field_emits_produces_edge(self):
        produces = [e for e in self.edges if e.kind == "PRODUCES"]
        sources = {e.source for e in produces}
        assert any("ReactiveOrderConsumer" in s for s in sources)

    def test_static_field_not_in_kafka_edges(self):
        all_kafka = [e for e in self.edges if e.kind in ("CONSUMES", "PRODUCES")]
        field_names = {e.extra.get("field_name") for e in all_kafka}
        assert "TOPIC" not in field_names

    def test_no_kafka_edges_for_plain_class(self):
        # OrderEvent (plain class, no Kafka) should not appear as a source
        kafka = [e for e in self.edges if e.kind in ("CONSUMES", "PRODUCES")]
        bare_sources = {e.source.split("::")[-1].split(".")[0] for e in kafka}
        assert "OrderEvent" not in bare_sources


# ---------------------------------------------------------------------------
# Verilog / SystemVerilog
# ---------------------------------------------------------------------------


def _has_verilog_parser():
    try:
        import tree_sitter_language_pack as tslp
        tslp.get_parser("verilog")
        return True
    except (LookupError, ImportError):
        return False


@pytest.mark.skipif(not _has_verilog_parser(), reason="verilog tree-sitter grammar not installed")
class TestVerilogParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample.sv")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("top.sv")) == "verilog"
        assert self.parser.detect_language(Path("pkg.svh")) == "verilog"
        assert self.parser.detect_language(Path("cpu.v")) == "verilog"
        assert self.parser.detect_language(Path("header.vh")) == "verilog"

    def test_finds_modules(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "FIFOController" in names
        assert "Adder" in names

    def test_finds_interfaces(self):
        classes = [n for n in self.nodes if n.kind == "Class"]
        names = {c.name for c in classes}
        assert "BusIf" in names

    def test_finds_tasks(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "do_write" in names

    def test_finds_functions_in_module(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "is_full" in names

    def test_task_and_function_parent_is_module(self):
        funcs = {f.name: f for f in self.nodes if f.kind == "Function"}
        assert funcs["do_write"].parent_name == "FIFOController"
        assert funcs["is_full"].parent_name == "FIFOController"

    def test_finds_package_imports(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = {e.target for e in imports}
        assert "utils_pkg" in targets
        assert "arith_pkg" in targets

    def test_module_instantiation_creates_call_edge(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        targets = {e.target for e in calls}
        assert any("Adder" in t for t in targets)

    def test_module_instantiation_caller_is_enclosing_module(self):
        # module_instantiation CALLS must be attributed to the containing
        # module, not a function — Verilog-specific fallback in _extract_calls.
        calls = [e for e in self.edges if e.kind == "CALLS"]
        adder_calls = [e for e in calls if "Adder" in e.target]
        assert adder_calls, "Expected a CALLS edge for Adder instantiation"
        assert any("FIFOController" in e.source for e in adder_calls)

    def test_file_node_language(self):
        file_nodes = [n for n in self.nodes if n.kind == "File"]
        assert len(file_nodes) == 1
        assert file_nodes[0].language == "verilog"

class TestSQLParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample.sql")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("schema.sql")) == "sql"

    def test_file_node(self):
        file_nodes = [n for n in self.nodes if n.kind == "File"]
        assert len(file_nodes) == 1
        assert file_nodes[0].language == "sql"

    def test_finds_tables(self):
        tables = [n for n in self.nodes if n.kind == "Class" and n.extra.get("sql_kind") == "table"]
        names = {t.name for t in tables}
        assert "users" in names
        assert "orders" in names

    def test_finds_view(self):
        views = [n for n in self.nodes if n.kind == "Class" and n.extra.get("sql_kind") == "view"]
        names = {v.name for v in views}
        assert "active_orders" in names

    def test_finds_function(self):
        funcs = [
            n for n in self.nodes
            if n.kind == "Function" and n.extra.get("sql_kind") == "function"
        ]
        names = {f.name for f in funcs}
        assert "get_user_total" in names

    def test_finds_procedure(self):
        procs = [
            n for n in self.nodes
            if n.kind == "Function" and n.extra.get("sql_kind") == "procedure"
        ]
        names = {p.name for p in procs}
        assert "archive_old_orders" in names

    def test_contains_edges(self):
        contains = [e for e in self.edges if e.kind == "CONTAINS"]
        targets = {e.target.split("::")[-1] for e in contains}
        assert "users" in targets
        assert "orders" in targets
        assert "active_orders" in targets
        assert "get_user_total" in targets
        assert "archive_old_orders" in targets

    def test_table_reference_edges(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = {e.target for e in imports}
        # active_orders view and archive procedure both reference orders/users
        assert "orders" in targets or "users" in targets

    def test_multiple_if_not_exists_creates_keep_distinct_references(self):
        _, edges = self.parser.parse_bytes(
            Path("idempotent_schema.sql"),
            b"CREATE TABLE IF NOT EXISTS customers (id INT);\n"
            b"CREATE TABLE IF NOT EXISTS invoices (id INT);\n",
        )
        targets = [e.target for e in edges if e.kind == "IMPORTS_FROM"]
        assert targets == ["customers", "invoices"]


class TestZigParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.fixture = FIXTURES / "sample_zig.zig"
        self.nodes, self.edges = self.parser.parse_file(self.fixture)

    def test_detects_language(self):
        assert self.parser.detect_language(Path("main.zig")) == "zig"

    def test_finds_top_level_functions(self):
        funcs = {
            n.name for n in self.nodes
            if n.kind == "Function" and n.parent_name is None
        }
        assert {"main", "helper"} <= funcs

    def test_finds_struct_methods(self):
        methods = {
            n.name for n in self.nodes
            if n.kind == "Function" and n.parent_name == "Point"
        }
        assert {"init", "distance"} <= methods

    def test_finds_struct_enum_union_classes(self):
        classes = {
            n.name: n.extra.get("zig_kind") for n in self.nodes
            if n.kind == "Class"
        }
        assert classes.get("Point") == "struct"
        assert classes.get("Color") == "enum"
        assert classes.get("Shape") == "union"

    def test_finds_imports(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = {e.target for e in imports}
        # std stays unresolved (no relative .zig path); util resolves to
        # the absolute fixture path.
        assert "std" in targets
        assert any(
            t.endswith("sample_zig_util.zig") and t != "./sample_zig_util.zig"
            for t in targets
        )

    def test_finds_calls(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        # Bare callees (std.debug.print, expect, util.noop) keep their final
        # identifier as the target; same-file helper resolves to the
        # qualified name via _resolve_call_targets.
        bare_targets = {e.target.split("::")[-1] for e in calls}
        assert "print" in bare_targets
        assert "expect" in bare_targets
        assert "helper" in bare_targets

    def test_builtin_calls_emitted(self):
        # @intCast inside Point.distance should produce a CALLS edge
        # whose target is the builtin name (with the leading @).
        targets = {e.target for e in self.edges if e.kind == "CALLS"}
        assert "@intCast" in targets

    def test_at_import_is_not_a_call(self):
        # @import is modelled as IMPORTS_FROM only — never as CALLS, so
        # it doesn't pollute the call graph.
        targets = {e.target for e in self.edges if e.kind == "CALLS"}
        assert "@import" not in targets

    def test_test_block_creates_test_node(self):
        tests = [n for n in self.nodes if n.kind == "Test"]
        assert len(tests) == 1
        assert tests[0].name.startswith("test:helper increments@L")
        assert tests[0].is_test is True

    def test_in_source_test_emits_tested_by_outside_test_path(self):
        path = Path("src/math.zig")
        nodes, edges = self.parser.parse_bytes(
            path,
            b"fn increment(x: i32) i32 { return x + 1; }\n"
            b'test "increment" { try expect(increment(1) == 2); }\n',
        )

        file_node = next(n for n in nodes if n.kind == "File")
        test_node = next(n for n in nodes if n.kind == "Test")
        function_node = next(
            n for n in nodes if n.kind == "Function" and n.name == "increment"
        )
        test_qname = self.parser._qualify(
            test_node.name, test_node.file_path, test_node.parent_name,
        )
        function_qname = self.parser._qualify(
            function_node.name, function_node.file_path, function_node.parent_name,
        )

        assert file_node.is_test is False
        assert any(
            edge.kind == "CALLS"
            and edge.source == test_qname
            and edge.target == function_qname
            for edge in edges
        )
        assert any(
            edge.kind == "TESTED_BY"
            and edge.source == function_qname
            and edge.target == test_qname
            for edge in edges
        )

    def test_calls_inside_methods_have_qualified_source(self):
        # Point.distance calls helper(...) — the source should be the
        # qualified Point.distance name, not the bare file path.
        sources = {
            e.source.split("::")[-1] for e in self.edges
            if e.kind == "CALLS"
        }
        assert "Point.distance" in sources

    def test_nodes_have_zig_language(self):
        for node in self.nodes:
            assert node.language == "zig"

class TestHCLParsing:
    """HCL / Terraform parser — closes #199."""

    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(FIXTURES / "sample.tf")

    def test_detects_language(self):
        assert self.parser.detect_language(Path("main.tf")) == "hcl"
        assert self.parser.detect_language(Path("config.hcl")) == "hcl"

    def test_nodes_have_hcl_language(self):
        for n in self.nodes:
            assert n.language == "hcl"

    def test_file_node(self):
        file_nodes = [n for n in self.nodes if n.kind == "File"]
        assert len(file_nodes) == 1
        assert file_nodes[0].name.endswith("sample.tf")

    def test_finds_resources(self):
        classes = {n.name for n in self.nodes if n.kind == "Class"}
        assert "resource.aws_vpc.main" in classes
        assert "resource.aws_instance.web" in classes
        assert "resource.aws_subnet.main" in classes

    def test_finds_data_sources(self):
        classes = {n.name for n in self.nodes if n.kind == "Class"}
        assert "data.aws_ami.ubuntu" in classes

    def test_finds_modules(self):
        classes = {n.name for n in self.nodes if n.kind == "Class"}
        assert "module.security" in classes

    def test_finds_variables(self):
        funcs = {n.name for n in self.nodes if n.kind == "Function"}
        assert "var.region" in funcs
        assert "var.instance_type" in funcs

    def test_finds_outputs(self):
        funcs = {n.name for n in self.nodes if n.kind == "Function"}
        assert "output.instance_ip" in funcs
        assert "output.vpc_id" in funcs

    def test_finds_locals(self):
        funcs = {n.name for n in self.nodes if n.kind == "Function"}
        assert "local.name_prefix" in funcs
        assert "local.full_name" in funcs

    def test_finds_provider(self):
        funcs = {n.name for n in self.nodes if n.kind == "Function"}
        assert "provider.aws" in funcs

    def test_hcl_type_extra_metadata(self):
        by_name = {n.name: n for n in self.nodes if n.kind != "File"}
        assert by_name["resource.aws_vpc.main"].extra["hcl_type"] == "resource"
        assert by_name["data.aws_ami.ubuntu"].extra["hcl_type"] == "data"
        assert by_name["module.security"].extra["hcl_type"] == "module"
        assert by_name["var.region"].extra["hcl_type"] == "variable"
        assert by_name["output.instance_ip"].extra["hcl_type"] == "output"
        assert by_name["local.name_prefix"].extra["hcl_type"] == "local"
        assert by_name["provider.aws"].extra["hcl_type"] == "provider"

    def test_module_source_creates_import_edge(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        targets = [e.target for e in imports]
        assert any("modules/security" in t for t in targets)

    def test_contains_edges(self):
        contains = [e for e in self.edges if e.kind == "CONTAINS"]
        targets = {e.target for e in contains}
        # All non-File nodes should be contained by the file
        for n in self.nodes:
            if n.kind != "File":
                qn = f"{n.file_path}::{n.name}"
                assert qn in targets, f"missing CONTAINS for {n.name}"

    def test_resource_references_variable(self):
        """resource.aws_instance.web references var.instance_type."""
        refs = [
            e for e in self.edges
            if e.kind == "REFERENCES"
            and "resource.aws_instance.web" in e.source
        ]
        targets = {e.target for e in refs}
        assert any("var.instance_type" in t for t in targets)

    def test_resource_references_other_resource(self):
        """resource.aws_instance.web references resource.aws_subnet.main."""
        refs = [
            e for e in self.edges
            if e.kind == "REFERENCES"
            and "resource.aws_instance.web" in e.source
        ]
        targets = {e.target for e in refs}
        assert any("resource.aws_subnet.main" in t for t in targets)

    def test_resource_references_data_source(self):
        """resource.aws_instance.web references data.aws_ami.ubuntu."""
        refs = [
            e for e in self.edges
            if e.kind == "REFERENCES"
            and "resource.aws_instance.web" in e.source
        ]
        targets = {e.target for e in refs}
        assert any("data.aws_ami.ubuntu" in t for t in targets)

    def test_output_references_resource(self):
        """output.instance_ip references resource.aws_instance.web."""
        refs = [
            e for e in self.edges
            if e.kind == "REFERENCES"
            and "output.instance_ip" in e.source
        ]
        targets = {e.target for e in refs}
        assert any("resource.aws_instance.web" in t for t in targets)

    def test_module_references_resource(self):
        """module.security references resource.aws_vpc.main."""
        refs = [
            e for e in self.edges
            if e.kind == "REFERENCES"
            and "module.security" in e.source
        ]
        targets = {e.target for e in refs}
        assert any("resource.aws_vpc.main" in t for t in targets)

    def test_provider_references_variable(self):
        """provider.aws references var.region."""
        refs = [
            e for e in self.edges
            if e.kind == "REFERENCES"
            and "provider.aws" in e.source
        ]
        targets = {e.target for e in refs}
        assert any("var.region" in t for t in targets)

    def test_terraform_block_skipped(self):
        """terraform {} block should not produce any nodes."""
        names = {n.name for n in self.nodes if n.kind != "File"}
        assert not any(name.startswith("terraform") for name in names)

    def test_resource_references_local(self):
        """resource.aws_vpc.main references local.full_name."""
        refs = [
            e for e in self.edges
            if e.kind == "REFERENCES"
            and "resource.aws_vpc.main" in e.source
        ]
        targets = {e.target for e in refs}
        assert any("local.full_name" in t for t in targets)

    # ------------------------------------------------------------------
    # Variable references inside function call arguments
    # ------------------------------------------------------------------

    def test_count_with_function_extracts_var_ref(self):
        """length(var.subnet_ids) in count — var.subnet_ids must be extracted."""
        refs = [
            e for e in self.edges
            if e.kind == "REFERENCES"
            and "resource.aws_instance.fleet" in e.source
        ]
        targets = {e.target for e in refs}
        assert any("var.subnet_ids" in t for t in targets), (
            f"Expected var.subnet_ids in refs from fleet; got {targets}"
        )

    # ------------------------------------------------------------------
    # Block-local meta-argument iterators must not produce REFERENCES edges
    # ------------------------------------------------------------------

    def test_each_value_produces_no_spurious_edge(self):
        """each.value.id should not produce any REFERENCES edge."""
        each_edges = [
            e for e in self.edges
            if e.kind == "REFERENCES" and "each" in e.target
        ]
        assert each_edges == [], (
            f"Spurious 'each' REFERENCES edges: {[e.target for e in each_edges]}"
        )

    def test_count_index_produces_no_spurious_edge(self):
        """count.index should not produce any REFERENCES edge."""
        count_edges = [
            e for e in self.edges
            if e.kind == "REFERENCES" and "count" in e.target
        ]
        assert count_edges == [], (
            f"Spurious 'count' REFERENCES edges: {[e.target for e in count_edges]}"
        )

    def test_path_module_produces_no_edge(self):
        """path.module must not produce a REFERENCES edge."""
        path_edges = [
            e for e in self.edges
            if e.kind == "REFERENCES" and "path" in e.target
        ]
        assert path_edges == [], (
            f"Spurious 'path' REFERENCES edges: {[e.target for e in path_edges]}"
        )

    def test_terraform_workspace_produces_no_edge(self):
        """terraform.workspace must not produce a REFERENCES edge."""
        tf_edges = [
            e for e in self.edges
            if e.kind == "REFERENCES"
            and e.target.rsplit("::", 1)[-1].startswith("terraform")
        ]
        assert tf_edges == [], (
            f"Spurious 'terraform' REFERENCES edges: {[e.target for e in tf_edges]}"
        )

    # ------------------------------------------------------------------
    # Resource-to-resource for_each chaining
    # ------------------------------------------------------------------

    def test_for_each_resource_chaining(self):
        """for_each = aws_vpc.main emits REFERENCES to resource.aws_vpc.main."""
        refs = [
            e for e in self.edges
            if e.kind == "REFERENCES"
            and "resource.aws_internet_gateway.gw" in e.source
        ]
        targets = {e.target for e in refs}
        assert any("resource.aws_vpc.main" in t for t in targets), (
            f"Expected resource.aws_vpc.main in refs from gw; got {targets}"
        )

    # ------------------------------------------------------------------
    # Variable references inside template string interpolations
    # ------------------------------------------------------------------

    def test_template_interpolation_extracts_var_ref(self):
        """\"${var.region}-static-assets\" must produce a REFERENCES edge to var.region."""
        refs = [
            e for e in self.edges
            if e.kind == "REFERENCES"
            and "resource.aws_s3_bucket.static" in e.source
        ]
        targets = {e.target for e in refs}
        assert any("var.region" in t for t in targets), (
            f"Expected var.region in refs from static bucket; got {targets}"
        )

    # ------------------------------------------------------------------
    # Nested block and dynamic block references
    # ------------------------------------------------------------------

    def test_lifecycle_replace_triggered_by(self):
        """lifecycle { replace_triggered_by = [...] } must emit REFERENCES edges."""
        refs = [
            e for e in self.edges
            if e.kind == "REFERENCES"
            and "resource.aws_autoscaling_group.web" in e.source
        ]
        targets = {e.target for e in refs}
        assert any("resource.aws_launch_template.web" in t for t in targets), (
            f"Expected resource.aws_launch_template.web in refs from asg.web; got {targets}"
        )

    def test_dynamic_block_for_each_ref(self):
        """dynamic block: for_each = var.ingress_rules must produce REFERENCES edge."""
        refs = [
            e for e in self.edges
            if e.kind == "REFERENCES"
            and "resource.aws_security_group.main" in e.source
        ]
        targets = {e.target for e in refs}
        assert any("var.ingress_rules" in t for t in targets), (
            f"Expected var.ingress_rules in refs from sg.main; got {targets}"
        )

    # ------------------------------------------------------------------
    # Dynamic block iterator scope
    # ------------------------------------------------------------------

    def test_dynamic_block_iterator_no_spurious_edge(self):
        """Iterator variables from dynamic blocks must not produce REFERENCES edges.

        Covers: ingress (existing fixture), setting (default iterator),
        srv (custom iterator=), origin_group and origin (nested dynamic).
        """
        iterator_names = ("ingress", "setting", "srv", "origin_group", "origin")
        spurious = [
            e for e in self.edges
            if e.kind == "REFERENCES"
            and any(f"resource.{name}." in e.target for name in iterator_names)
        ]
        assert spurious == [], (
            f"Spurious iterator REFERENCES edges: {[e.target for e in spurious]}"
        )

    def test_dynamic_block_default_iterator_for_each_extracted(self):
        """for_each = var.settings inside dynamic block must produce a REFERENCES edge."""
        refs = [
            e for e in self.edges
            if e.kind == "REFERENCES"
            and "resource.aws_elastic_beanstalk_environment.tfenvtest" in e.source
        ]
        targets = {e.target for e in refs}
        assert any("var.settings" in t for t in targets), (
            f"Expected var.settings in refs from tfenvtest; got {targets}"
        )

    def test_dynamic_block_resource_ref_alongside_iterator(self):
        """Non-iterator attribute refs must still be extracted from the same block.

        aws_elastic_beanstalk_environment.tfenvtest references both
        var.settings (via for_each) and aws_elastic_beanstalk_application.tftest
        (via application = <resource>.name) while also containing a 'setting'
        iterator.  Both real refs must survive.
        """
        refs = [
            e for e in self.edges
            if e.kind == "REFERENCES"
            and "resource.aws_elastic_beanstalk_environment.tfenvtest" in e.source
        ]
        targets = {e.target for e in refs}
        assert any("resource.aws_elastic_beanstalk_application.tftest" in t for t in targets), (
            f"Expected aws_elastic_beanstalk_application.tftest ref; got {targets}"
        )

    def test_dynamic_block_custom_iterator_for_each_extracted(self):
        """for_each = var.server_list with iterator = srv must still extract var.server_list."""
        refs = [
            e for e in self.edges
            if e.kind == "REFERENCES"
            and "resource.aws_lb_listener_rule.hosts" in e.source
        ]
        targets = {e.target for e in refs}
        assert any("var.server_list" in t for t in targets), (
            f"Expected var.server_list in refs from aws_lb_listener_rule.hosts; got {targets}"
        )

    def test_nested_dynamic_outer_for_each_extracted(self):
        """Outer dynamic for_each = var.load_balancer_origin_groups must be extracted."""
        refs = [
            e for e in self.edges
            if e.kind == "REFERENCES"
            and "resource.aws_cloudfront_distribution.cdn" in e.source
        ]
        targets = {e.target for e in refs}
        assert any("var.load_balancer_origin_groups" in t for t in targets), (
            f"Expected var.load_balancer_origin_groups in refs from cdn; got {targets}"
        )

    def test_nested_dynamic_inner_iterator_refs_suppressed(self):
        """Inner dynamic for_each = origin_group.value.origins must produce NO edge.

        origin_group is an iterator variable from the outer dynamic block;
        treating it as a resource type would emit a spurious
        resource.origin_group.value edge.
        """
        spurious = [
            e for e in self.edges
            if e.kind == "REFERENCES"
            and "resource.origin_group." in e.target
        ]
        assert spurious == [], (
            f"Spurious origin_group REFERENCES edges: {[e.target for e in spurious]}"
        )


# ---------------------------------------------------------------------------
# Ansible YAML parsing tests
# ---------------------------------------------------------------------------

try:
    import yaml as _yaml_check  # noqa: F401
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

_ANSIBLE_SKIP = pytest.mark.skipif(not _YAML_AVAILABLE, reason="pyyaml not installed")

_PLAYBOOK = FIXTURES / "playbooks" / "sample_ansible_playbook.yml"
_TASKS_FILE = FIXTURES / "tasks" / "sample_ansible_tasks.yml"
_META_FILE = FIXTURES / "roles" / "myrole" / "meta" / "main.yml"


@_ANSIBLE_SKIP
class TestAnsiblePlaybookParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(_PLAYBOOK)

    def test_detects_language_ansible_paths(self):
        p = self.parser
        assert p.detect_language(Path("playbooks/site.yml")) == "ansible"
        assert p.detect_language(Path("roles/web/tasks/main.yml")) == "ansible"
        assert p.detect_language(Path("handlers/main.yml")) == "ansible"
        assert p.detect_language(Path("config/settings.yml")) == "yaml"

    def test_file_node_created(self):
        file_nodes = [n for n in self.nodes if n.kind == "File"]
        assert len(file_nodes) == 1
        assert file_nodes[0].language == "ansible"

    def test_finds_plays_as_class_nodes(self):
        play_names = {n.name for n in self.nodes if n.kind == "Class"}
        assert "Configure web servers" in play_names
        assert "Configure database servers" in play_names

    def test_plays_have_ansible_kind_extra(self):
        plays = [n for n in self.nodes if n.kind == "Class"]
        assert plays, "expected at least one play"
        for p in plays:
            assert p.extra.get("ansible_kind") == "play"

    def test_import_playbook_produces_imports_from(self):
        targets = {e.target for e in self.edges if e.kind == "IMPORTS_FROM"}
        assert "base-setup.yml" in targets

    def test_pre_task_extracted(self):
        func_names = {n.name for n in self.nodes if n.kind == "Function"}
        assert "Verify connectivity" in func_names

    def test_post_task_extracted(self):
        func_names = {n.name for n in self.nodes if n.kind == "Function"}
        assert "Smoke test" in func_names

    def test_finds_tasks_as_function_nodes(self):
        func_names = {n.name for n in self.nodes if n.kind == "Function"}
        assert "Install packages" in func_names
        assert "Deploy config" in func_names
        assert "Run deploy tasks" in func_names

    def test_fqcn_module_stored_in_extra(self):
        task = next(
            n for n in self.nodes
            if n.kind == "Function" and n.name == "Verify connectivity"
        )
        assert task.extra.get("ansible_module") == "ansible.builtin.wait_for_connection"

    def test_finds_handlers(self):
        handlers = [
            n for n in self.nodes
            if n.kind == "Function" and n.extra.get("ansible_kind") == "handler"
        ]
        handler_names = {h.name for h in handlers}
        assert "restart app" in handler_names
        assert "restart db" in handler_names

    def test_handler_listen_stored(self):
        handler = next(
            n for n in self.nodes
            if n.kind == "Function" and n.name == "restart app"
        )
        assert handler.extra.get("ansible_listen") == "app restarted"

    def test_notify_scalar_produces_calls(self):
        calls = {e.target for e in self.edges if e.kind == "CALLS"}
        assert any(target.endswith("::Configure web servers.restart app") for target in calls)

    def test_notify_list_produces_multiple_calls(self):
        calls = {e.target for e in self.edges if e.kind == "CALLS"}
        assert any(target.endswith("::Configure database servers.restart db") for target in calls)
        assert any(target.endswith("::Configure database servers.run migrations") for target in calls)

    def test_include_tasks_imports_from(self):
        targets = {e.target for e in self.edges if e.kind == "IMPORTS_FROM"}
        assert "deploy.yml" in targets

    def test_import_role_imports_from(self):
        targets = {e.target for e in self.edges if e.kind == "IMPORTS_FROM"}
        assert "security" in targets

    def test_roles_list_imports_from(self):
        targets = {e.target for e in self.edges if e.kind == "IMPORTS_FROM"}
        assert "common" in targets
        assert "nginx" in targets

    def test_vars_files_imports_from(self):
        targets = {e.target for e in self.edges if e.kind == "IMPORTS_FROM"}
        assert "vars/common.yml" in targets

    def test_block_tasks_extracted(self):
        func_names = {n.name for n in self.nodes if n.kind == "Function"}
        assert "Run migration script" in func_names
        assert "Verify migration" in func_names

    def test_rescue_tasks_extracted(self):
        func_names = {n.name for n in self.nodes if n.kind == "Function"}
        assert "Log migration failure" in func_names

    def test_block_tasks_parented_to_play(self):
        block_task = next(
            n for n in self.nodes
            if n.kind == "Function" and n.name == "Run migration script"
        )
        assert block_task.parent_name == "Configure web servers"

    def test_file_contains_plays(self):
        file_path_str = str(_PLAYBOOK)
        file_contains = {e.target for e in self.edges
                         if e.kind == "CONTAINS" and e.source == file_path_str}
        assert any("Configure web servers" in t for t in file_contains)

    def test_line_numbers_positive(self):
        for n in self.nodes:
            assert n.line_start > 0, f"{n.name} has line_start={n.line_start}"
            assert n.line_end >= n.line_start, f"{n.name} has bad line range"

    def test_all_nodes_language_ansible(self):
        for n in self.nodes:
            assert n.language == "ansible", f"{n.name} has language={n.language!r}"


@_ANSIBLE_SKIP
class TestAnsibleTasksParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(_TASKS_FILE)

    def test_file_language_ansible(self):
        file_nodes = [n for n in self.nodes if n.kind == "File"]
        assert file_nodes[0].language == "ansible"

    def test_named_tasks_found(self):
        func_names = {n.name for n in self.nodes if n.kind == "Function"}
        assert "Create app user" in func_names
        assert "Clone repository" in func_names
        assert "Install requirements" in func_names

    def test_nameless_task_fallback_name(self):
        func_names = {n.name for n in self.nodes if n.kind == "Function"}
        fallbacks = [n for n in func_names if "@line" in n and "package" in n.lower()]
        assert fallbacks, "expected a fallback-named task for the nameless package task"

    def test_loop_key_not_misidentified_as_module(self):
        func_names = {n.name for n in self.nodes if n.kind == "Function"}
        assert not any(n.startswith("loop@") or n.startswith("with_") for n in func_names)

    def test_fqcn_include_role_imports_from(self):
        targets = {e.target for e in self.edges if e.kind == "IMPORTS_FROM"}
        assert "shared_config" in targets

    def test_import_tasks_imports_from(self):
        targets = {e.target for e in self.edges if e.kind == "IMPORTS_FROM"}
        assert "deploy_steps.yml" in targets

    def test_include_vars_imports_from(self):
        targets = {e.target for e in self.edges if e.kind == "IMPORTS_FROM"}
        assert "env_vars.yml" in targets

    def test_file_contains_tasks(self):
        file_path_str = str(_TASKS_FILE)
        sources = {e.source for e in self.edges if e.kind == "CONTAINS"}
        assert file_path_str in sources

    def test_tasks_have_no_parent_play(self):
        for n in self.nodes:
            if n.kind == "Function":
                assert n.parent_name is None, f"{n.name} should have no parent_play"


@_ANSIBLE_SKIP
class TestAnsibleMetaParsing:
    def setup_method(self):
        self.parser = CodeParser()
        self.nodes, self.edges = self.parser.parse_file(_META_FILE)

    def test_file_language_ansible(self):
        file_nodes = [n for n in self.nodes if n.kind == "File"]
        assert file_nodes[0].language == "ansible"

    def test_depends_on_bare_string(self):
        dep_targets = {e.target for e in self.edges if e.kind == "DEPENDS_ON"}
        assert "common" in dep_targets

    def test_depends_on_role_key(self):
        dep_targets = {e.target for e in self.edges if e.kind == "DEPENDS_ON"}
        assert "nginx" in dep_targets

    def test_depends_on_name_key_collections(self):
        dep_targets = {e.target for e in self.edges if e.kind == "DEPENDS_ON"}
        assert "security.hardening" in dep_targets
