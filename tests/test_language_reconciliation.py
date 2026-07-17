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
