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

    def test_finds_imports(self):
        imports = [e for e in self.edges if e.kind == "IMPORTS_FROM"]
        assert len(imports) >= 2

    def test_finds_inheritance(self):
        inherits = [e for e in self.edges if e.kind == "INHERITS"]
        # InMemoryRepo implements UserRepository
        assert len(inherits) >= 1

    def test_finds_calls(self):
        calls = [e for e in self.edges if e.kind == "CALLS"]
        assert len(calls) >= 3


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
        assert "User" in names or "InMemoryRepo" in names

    def test_finds_functions(self):
        funcs = [n for n in self.nodes if n.kind == "Function"]
        names = {f.name for f in funcs}
        assert "createUser" in names or "findById" in names or "save" in names


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
