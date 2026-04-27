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
        by_name = {f.name: f for f in funcs}
        assert "FindByID" in by_name
        assert "Save" in by_name
        assert by_name["FindByID"].parent_name == "InMemoryRepo"
        assert by_name["Save"].parent_name == "InMemoryRepo"
        # Free functions should still have no parent.
        assert by_name["NewInMemoryRepo"].parent_name is None
        assert by_name["CreateUser"].parent_name is None

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
        assert imports[0].target == str((auth / "User.java").resolve())

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
        assert imports[0].target == str((pkg / "Helper.java").resolve())

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
        assert "QueryUtils::fetchRecords" in targets
        assert "EncounterService::create" in targets
        assert any(t.endswith("__construct") for t in run_queries_targets)
        assert any(t.endswith("factory") for t in run_queries_targets)

        # Global namespaced calls should normalize to a stable name
        assert "dirname" in target_names


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
