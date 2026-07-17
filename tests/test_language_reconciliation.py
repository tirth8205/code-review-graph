"""Regression tests for reconciled community language contributions."""

from __future__ import annotations

from pathlib import Path

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
