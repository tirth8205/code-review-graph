"""Regression tests for reconciled community language contributions."""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser


def _parse(path: Path, source: str):
    path.write_text(source, encoding="utf-8")
    return CodeParser().parse_file(path)


class TestVBNetReconciliation:
    def test_namespaces_generics_and_multiline_signatures_are_scoped(self, tmp_path):
        path = tmp_path / "Services.vb"
        nodes, _ = _parse(
            path,
            """
Namespace Alpha.Tools
    Public Class Worker(Of T)
        Public Function Convert(Of TResult)(
            ByVal value As T,
            Optional enabled As Boolean = True
        ) As TResult
        End Function
    End Class
End Namespace

Namespace Beta.Tools
    Public Class Worker
    End Class
End Namespace
""".lstrip(),
        )

        assert CodeParser().detect_language(Path("Program.vb")) == "vbnet"
        classes = {
            (node.name, node.parent_name): node
            for node in nodes
            if node.kind == "Class" and node.extra.get("vbnet_kind") == "class"
        }
        assert ("Worker", "Alpha.Tools") in classes
        assert ("Worker", "Beta.Tools") in classes

        convert = next(node for node in nodes if node.name == "Convert")
        assert convert.parent_name == "Alpha.Tools.Worker"
        assert convert.params is not None and "value As T" in convert.params
        assert convert.params is not None and "enabled As Boolean" in convert.params
        assert convert.return_type == "TResult"
        assert convert.extra["vbnet_type_parameters"] == ["TResult"]

    def test_relationships_and_calls_resolve_case_insensitively_to_graph_nodes(
        self, tmp_path,
    ):
        path = tmp_path / "Repository.vb"
        nodes, edges = _parse(
            path,
            """
Namespace Acme
    Public Interface IRepository
        Sub Save(value As Integer)
    End Interface

    Public Class BaseRepository
    End Class

    Public Class Repository
        Inherits BaseRepository
        Implements IRepository

        Public Property Current As Integer
            Get
                Me.Helper(Current)
                Return Current
            End Get
        End Property

        Public Sub Save(value As Integer) Implements IRepository.Save
            hElPeR(value)
        End Sub

        Private Sub Helper(value As Integer)
        End Sub
    End Class
End Namespace
""".lstrip(),
        )

        store = GraphStore(":memory:")
        store.store_file_nodes_edges(str(path), nodes, edges)

        repository_qn = f"{path}::Acme.Repository"
        base_qn = f"{path}::Acme.BaseRepository"
        interface_qn = f"{path}::Acme.IRepository"
        helper_qn = f"{path}::Acme.Repository.Helper"
        property_qn = f"{path}::Acme.Repository.Current"
        save_qn = f"{path}::Acme.Repository.Save"

        assert store.get_node(repository_qn) is not None
        assert store.get_node(base_qn) is not None
        assert store.get_node(interface_qn) is not None
        assert store.get_node(helper_qn) is not None

        repository_edges = store.get_edges_by_source(repository_qn)
        assert any(edge.kind == "INHERITS" and edge.target_qualified == base_qn
                   for edge in repository_edges)
        assert any(edge.kind == "IMPLEMENTS" and edge.target_qualified == interface_qn
                   for edge in repository_edges)

        assert any(
            edge.kind == "CALLS" and edge.target_qualified == helper_qn
            for edge in store.get_edges_by_source(property_qn)
        )
        assert any(
            edge.kind == "CALLS" and edge.target_qualified == helper_qn
            for edge in store.get_edges_by_source(save_qn)
        )
        store.close()

    def test_overloads_share_one_stable_graph_symbol(self, tmp_path):
        path = tmp_path / "Overloads.vb"
        nodes, edges = _parse(
            path,
            """
Public Class Writer
    Public Overloads Sub Save(value As Integer)
    End Sub

    Public Overloads Sub Save(value As String)
    End Sub
End Class
""".lstrip(),
        )

        saves = [node for node in nodes if node.name == "Save"]
        assert len(saves) == 1
        assert saves[0].extra["vbnet_overloads"] == [
            "value As Integer",
            "value As String",
        ]

        store = GraphStore(":memory:")
        store.store_file_nodes_edges(str(path), nodes, edges)
        assert store.get_node(f"{path}::Writer.Save") is not None
        store.close()


def _has_verilog_parser() -> bool:
    try:
        import tree_sitter_language_pack as tslp

        tslp.get_parser("verilog")
    except (ImportError, LookupError):
        return False
    return True


@pytest.mark.skipif(
    not _has_verilog_parser(), reason="verilog tree-sitter grammar not installed",
)
class TestSystemVerilogReconciliation:
    def test_module_signals_are_indexed_but_function_locals_are_not(self, tmp_path):
        path = tmp_path / "signals.sv"
        nodes, _ = _parse(
            path,
            """
module Signals(
    input logic clk,
    output logic ready
);
    logic shared_signal;

    function automatic logic first(input logic value);
        logic duplicate_local;
        first = duplicate_local;
    endfunction

    function automatic logic second(input logic value);
        logic duplicate_local;
        second = duplicate_local;
    endfunction
endmodule
""".lstrip(),
        )

        signals = {
            (node.name, node.parent_name): node
            for node in nodes
            if node.extra.get("verilog_kind")
        }
        assert ("clk", "Signals") in signals
        assert ("ready", "Signals") in signals
        assert ("shared_signal", "Signals") in signals
        assert not any(name == "duplicate_local" for name, _ in signals)

    def test_packages_typedefs_modports_and_verification_constructs(self, tmp_path):
        path = tmp_path / "constructs.sv"
        nodes, _ = _parse(
            path,
            """
package types_pkg;
    typedef enum logic {IDLE, RUNNING} state_t;
endpackage

interface BusIf(input logic clk);
    logic data;
    modport Producer(output data);
    sequence ready_sequence;
        data;
    endsequence
    property valid_property;
        @(posedge clk) data;
    endproperty
endinterface
""".lstrip(),
        )

        classes = {node.name for node in nodes if node.kind == "Class"}
        assert "types_pkg" in classes
        constructs = {
            (node.name, node.extra.get("verilog_kind"))
            for node in nodes
            if node.extra.get("verilog_kind")
        }
        assert ("state_t", "typedef") in constructs
        assert ("Producer", "modport") in constructs
        assert ("ready_sequence", "sequence") in constructs
        assert ("valid_property", "property") in constructs

    def test_named_port_references_keep_only_local_signal_roots(self, tmp_path):
        path = tmp_path / "connections.sv"
        nodes, edges = _parse(
            path,
            """
module Child(input logic data);
endmodule

module Top;
    logic local_signal;
    logic bus;
    Child #() direct(.data(local_signal));
    Child #() member(.data(bus.member));
endmodule
""".lstrip(),
        )

        targets = {
            edge.target
            for edge in edges
            if edge.kind == "REFERENCES" and edge.source.endswith("::Top")
        }
        assert targets == {
            f"{path}::Top.local_signal",
            f"{path}::Top.bus",
        }
        assert all(not target.endswith(".member") for target in targets)

    def test_signal_nodes_are_excluded_from_function_analyses(self, tmp_path):
        from code_review_graph.flows import detect_entry_points
        from code_review_graph.refactor import find_dead_code

        path = tmp_path / "analysis.sv"
        nodes, edges = _parse(
            path,
            """
module Analysis(input logic clk);
    logic value;
endmodule
""".lstrip(),
        )
        store = GraphStore(":memory:")
        store.store_file_nodes_edges(str(path), nodes, edges)

        stats = store.get_stats()
        assert stats.nodes_by_kind.get("Signal") == 2
        dead_names = {item["name"] for item in find_dead_code(store)}
        assert dead_names.isdisjoint({"clk", "value"})
        assert all(
            not node.extra.get("verilog_kind")
            for node in detect_entry_points(store)
        )
        impact = store.get_impact_radius([str(path)])
        assert all(
            not node.extra.get("verilog_kind")
            for node in impact["impacted_nodes"]
        )
        store.close()


class TestRustReconciliation:
    def test_traits_and_multiple_impl_blocks_keep_one_concrete_type(self, tmp_path):
        path = tmp_path / "lib.rs"
        nodes, edges = _parse(
            path,
            """
pub trait Repository {
    fn save(&self);
}

pub struct MemoryRepository;

impl MemoryRepository {
    pub fn new() -> Self { Self }
    pub fn duplicate() -> Self { Self::new() }
}

impl Repository for MemoryRepository {
    fn save(&self) {}
}

impl MemoryRepository {
    pub fn clear(&mut self) {}
}
""".lstrip(),
        )

        concrete = [
            node for node in nodes
            if node.kind == "Class" and node.name == "MemoryRepository"
        ]
        assert len(concrete) == 1
        assert concrete[0].line_start == 5
        assert any(node.kind == "Class" and node.name == "Repository" for node in nodes)
        methods = {
            (node.name, node.parent_name)
            for node in nodes
            if node.kind == "Function"
        }
        assert ("new", "MemoryRepository") in methods
        assert ("duplicate", "MemoryRepository") in methods
        assert ("save", "MemoryRepository") in methods
        assert ("clear", "MemoryRepository") in methods

        duplicate_qn = f"{path}::MemoryRepository.duplicate"
        new_qn = f"{path}::MemoryRepository.new"
        assert any(
            edge.kind == "CALLS"
            and edge.source == duplicate_qn
            and edge.target == new_qn
            for edge in edges
        )

        store = GraphStore(":memory:")
        store.store_file_nodes_edges(str(path), nodes, edges)
        concrete_qn = f"{path}::MemoryRepository"
        trait_qn = f"{path}::Repository"
        assert store.get_node(concrete_qn).line_start == 5
        assert any(
            edge.kind == "IMPLEMENTS" and edge.target_qualified == trait_qn
            for edge in store.get_edges_by_source(concrete_qn)
        )
        store.close()

    def test_alias_and_turbofish_calls_resolve_to_the_original_type(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "demo"\nversion = "0.1.0"\n',
            encoding="utf-8",
        )
        src = tmp_path / "src"
        src.mkdir()
        db = src / "db.rs"
        db.write_text(
            "pub struct Repository;\n"
            "impl Repository { pub fn new<T>() -> Self { Self } }\n",
            encoding="utf-8",
        )
        lib = src / "lib.rs"
        lib.write_text(
            "mod db;\n"
            "use crate::db::{Repository as Repo};\n"
            "pub fn build() {\n"
            "    Repo::new::<u8>();\n"
            "    crate::db::Repository::<u16>::new();\n"
            "}\n",
            encoding="utf-8",
        )

        parser = CodeParser(repo_root=tmp_path)
        db_nodes, db_edges = parser.parse_file(db)
        lib_nodes, lib_edges = parser.parse_file(lib)
        target = f"{db.resolve()}::Repository.new"
        calls = [edge for edge in lib_edges if edge.kind == "CALLS"]
        assert [edge.target for edge in calls].count(target) == 2
        assert all("::Repo.new" not in edge.target for edge in calls)

        store = GraphStore(":memory:")
        store.store_file_nodes_edges(str(db), db_nodes, db_edges)
        store.store_file_nodes_edges(str(lib), lib_nodes, lib_edges)
        assert store.get_node(target) is not None
        store.close()

    def test_self_super_and_crate_imports_resolve_to_module_files(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "demo"\nversion = "0.1.0"\n',
            encoding="utf-8",
        )
        src = tmp_path / "src"
        nested = src / "db" / "nested.rs"
        nested.parent.mkdir(parents=True)
        root = src / "lib.rs"
        parent = src / "db" / "mod.rs"
        root.write_text("pub fn root_function() {}\npub mod db;\n", encoding="utf-8")
        parent.write_text(
            "pub struct Repository;\npub mod nested;\n",
            encoding="utf-8",
        )
        nested.write_text(
            "use self::local_function;\n"
            "use super::Repository;\n"
            "use crate::root_function;\n"
            "pub fn local_function() {}\n",
            encoding="utf-8",
        )

        parser = CodeParser(repo_root=tmp_path)
        _, edges = parser.parse_file(nested)
        targets = {
            edge.target for edge in edges if edge.kind == "IMPORTS_FROM"
        }
        assert targets == {
            str(nested.resolve()),
            str(parent.resolve()),
            str(root.resolve()),
        }

    def test_workspace_dependency_alias_resolves_from_workspace_manifest(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text(
            "[workspace]\n"
            'members = ["crates/app", "crates/dep"]\n'
            'resolver = "2"\n'
            "[workspace.dependencies]\n"
            'renamed = { package = "dep-crate", path = "crates/dep" }\n',
            encoding="utf-8",
        )
        app = tmp_path / "crates" / "app"
        dep = tmp_path / "crates" / "dep"
        (app / "src").mkdir(parents=True)
        (dep / "src").mkdir(parents=True)
        (app / "Cargo.toml").write_text(
            "[package]\n"
            'name = "app"\nversion = "0.1.0"\n'
            "[dependencies]\nrenamed = { workspace = true }\n",
            encoding="utf-8",
        )
        (dep / "Cargo.toml").write_text(
            '[package]\nname = "dep-crate"\nversion = "0.1.0"\n',
            encoding="utf-8",
        )
        dep_lib = dep / "src" / "lib.rs"
        dep_lib.write_text("pub struct Helper;\n", encoding="utf-8")
        app_main = app / "src" / "main.rs"
        app_main.write_text("use renamed::Helper;\n", encoding="utf-8")

        _, edges = CodeParser(repo_root=tmp_path).parse_file(app_main)
        imports = [edge for edge in edges if edge.kind == "IMPORTS_FROM"]
        assert len(imports) == 1
        assert imports[0].target == str(dep_lib.resolve())

    def test_path_dependency_without_cargo_manifest_stays_unresolved(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text(
            "[package]\n"
            'name = "demo"\nversion = "0.1.0"\n'
            "[dependencies]\n"
            'fake = { path = "fake" }\n',
            encoding="utf-8",
        )
        src = tmp_path / "src"
        src.mkdir()
        lib = src / "lib.rs"
        lib.write_text("use fake::Helper;\n", encoding="utf-8")
        fake_src = tmp_path / "fake" / "src"
        fake_src.mkdir(parents=True)
        (fake_src / "lib.rs").write_text("pub struct Helper;\n", encoding="utf-8")

        _, edges = CodeParser(repo_root=tmp_path).parse_file(lib)
        imports = [edge for edge in edges if edge.kind == "IMPORTS_FROM"]
        assert len(imports) == 1
        assert imports[0].target == "fake::Helper"

    def test_full_and_incremental_builds_keep_resolved_rust_calls(
        self, tmp_path, monkeypatch,
    ):
        from code_review_graph.incremental import full_build, incremental_update

        monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
        (tmp_path / ".git").mkdir()
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "demo"\nversion = "0.1.0"\n',
            encoding="utf-8",
        )
        src = tmp_path / "src"
        src.mkdir()
        db = src / "db.rs"
        db.write_text(
            "pub struct Repository;\n"
            "impl Repository { pub fn new() -> Self { Self } }\n",
            encoding="utf-8",
        )
        lib = src / "lib.rs"
        lib.write_text(
            "mod db;\n"
            "use crate::db::Repository;\n"
            "pub fn build() { Repository::new(); }\n",
            encoding="utf-8",
        )
        target = f"{db.resolve()}::Repository.new"
        caller = f"{lib.resolve()}::build"

        store = GraphStore(":memory:")
        try:
            built = full_build(tmp_path, store)
            assert built["errors"] == []
            assert any(
                edge.kind == "CALLS" and edge.target_qualified == target
                for edge in store.get_edges_by_source(caller)
            )

            lib.write_text(
                "mod db;\n"
                "use crate::db::Repository;\n"
                "pub fn build() { Repository::new(); }\n"
                "pub fn build_again() { Repository::new(); }\n",
                encoding="utf-8",
            )
            updated = incremental_update(
                tmp_path, store, changed_files=["src/lib.rs"],
            )
            assert updated["errors"] == []
            assert any(
                edge.kind == "CALLS" and edge.target_qualified == target
                for edge in store.get_edges_by_source(
                    f"{lib.resolve()}::build_again",
                )
            )
        finally:
            store.close()


class TestPHPScopedCallReconciliation:
    def test_same_file_and_self_calls_resolve_without_cross_class_collisions(
        self, tmp_path,
    ):
        path = tmp_path / "Services.php"
        _, edges = _parse(
            path,
            """<?php
class FirstService {
    public static function run(): void {}
}

class SecondService {
    public static function run(): void {}

    public function dispatch(): void {
        self::run();
        FirstService::run();
    }
}
""",
        )

        targets = {
            edge.target
            for edge in edges
            if edge.kind == "CALLS"
            and edge.source == f"{path}::SecondService.dispatch"
        }
        assert targets == {
            f"{path}::SecondService.run",
            f"{path}::FirstService.run",
        }

    def test_import_alias_and_fully_qualified_calls_use_composer_evidence(
        self, tmp_path,
    ):
        (tmp_path / "composer.json").write_text(
            '{"autoload":{"psr-4":{"App\\\\":"app/"}}}',
            encoding="utf-8",
        )
        mailer = tmp_path / "app" / "Service" / "Mailer.php"
        mailer.parent.mkdir(parents=True)
        mailer.write_text(
            "<?php\nnamespace App\\Service;\n"
            "class Mailer { public static function send(): void {} }\n",
            encoding="utf-8",
        )
        caller = tmp_path / "app" / "Controller" / "SignupController.php"
        caller.parent.mkdir(parents=True)
        caller.write_text(
            "<?php\nnamespace App\\Controller;\n"
            "use App\\Service\\Mailer as Delivery;\n"
            "class SignupController {\n"
            "  public function register(): void {\n"
            "    Delivery::send();\n"
            "    \\App\\Service\\Mailer::send();\n"
            "  }\n"
            "}\n",
            encoding="utf-8",
        )

        _, edges = CodeParser(repo_root=tmp_path).parse_file(caller)
        target = f"{mailer.resolve()}::Mailer.send"
        calls = [
            edge for edge in edges
            if edge.kind == "CALLS" and edge.source.endswith("::SignupController.register")
        ]
        assert [edge.target for edge in calls].count(target) == 2
        assert {edge.extra["scoped_resolution"] for edge in calls} == {
            "import", "fully_qualified",
        }

    def test_global_cross_file_call_without_evidence_stays_unresolved(self, tmp_path):
        (tmp_path / "Mailer.php").write_text(
            "<?php class Mailer { public static function send(): void {} }\n",
            encoding="utf-8",
        )
        caller = tmp_path / "Caller.php"
        caller.write_text(
            "<?php function register(): void { Mailer::send(); }\n",
            encoding="utf-8",
        )
        _, edges = CodeParser(repo_root=tmp_path).parse_file(caller)

        calls = [edge for edge in edges if edge.kind == "CALLS"]
        assert len(calls) == 1
        assert calls[0].target == "Mailer::send"

    def test_incremental_update_reresolves_only_changed_php_file(
        self, tmp_path, monkeypatch,
    ):
        from code_review_graph.incremental import full_build, incremental_update

        monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
        (tmp_path / ".git").mkdir()
        (tmp_path / "composer.json").write_text(
            '{"autoload":{"psr-4":{"App\\\\":"app/"}}}',
            encoding="utf-8",
        )
        mailer = tmp_path / "app" / "Mailer.php"
        mailer.parent.mkdir()
        mailer.write_text(
            "<?php namespace App; "
            "class Mailer { public static function send(): void {} }\n",
            encoding="utf-8",
        )
        caller = tmp_path / "app" / "Signup.php"
        source = (
            "<?php namespace App; use App\\Mailer; "
            "function register(): void { Mailer::send(); }\n"
        )
        caller.write_text(source, encoding="utf-8")
        target = f"{mailer.resolve()}::Mailer.send"

        store = GraphStore(":memory:")
        try:
            assert full_build(tmp_path, store)["errors"] == []
            caller.write_text(source.replace("function", "\nfunction"), encoding="utf-8")
            result = incremental_update(
                tmp_path, store, changed_files=["app/Signup.php"],
            )
            assert result["errors"] == []
            assert any(
                edge.kind == "CALLS" and edge.target_qualified == target
                for edge in store.get_edges_by_source(f"{caller.resolve()}::register")
            )
        finally:
            store.close()
