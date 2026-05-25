"""Terraform/HCL parser and module-scope resolution tests."""

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import CodeParser


def test_for_expression_references_are_emitted_for_local_binding() -> None:
    path = Path("infra/locals.tf")
    source = b"""\
variable "items" {}
variable "enabled" {}

locals {
  selected = [
    for item in var.items : item.name
    if var.enabled
  ]
}
"""

    nodes, edges = CodeParser().parse_bytes(path, source)

    assert any(node.name == "local.selected" for node in nodes)
    local_qn = "infra/locals.tf::local.selected"
    targets = {
        edge.target
        for edge in edges
        if edge.kind == "REFERENCES" and edge.source == local_qn
    }
    assert targets == {
        "infra/locals.tf::var.items",
        "infra/locals.tf::var.enabled",
    }


def test_full_build_resolves_terraform_module_scope_and_local_source(
    tmp_path: Path,
) -> None:
    (tmp_path / "variables.tf").write_text(
        """\
variable "region" {}
variable "names" {}

locals {
  tags = { region = var.region }
}
""",
        encoding="utf-8",
    )
    (tmp_path / "main.tf").write_text(
        """\
resource "example_server" "web" {
  for_each = { for name in var.names : name => local.tags }
}

module "network" {
  source = "./modules/network"
}
""",
        encoding="utf-8",
    )
    module_dir = tmp_path / "modules" / "network"
    module_dir.mkdir(parents=True)
    (module_dir / "main.tf").write_text(
        'resource "example_network" "main" {}\n',
        encoding="utf-8",
    )

    store = GraphStore(tmp_path / ".code-review-graph" / "graph.db")
    try:
        result = full_build(tmp_path, store)

        resource_qn = f"{tmp_path / 'main.tf'}::resource.example_server.web"
        reference_targets = {
            edge.target_qualified
            for edge in store.get_edges_by_source(resource_qn)
            if edge.kind == "REFERENCES"
        }
        assert reference_targets == {
            f"{tmp_path / 'variables.tf'}::var.names",
            f"{tmp_path / 'variables.tf'}::local.tags",
        }

        local_qn = f"{tmp_path / 'variables.tf'}::local.tags"
        assert {
            edge.target_qualified
            for edge in store.get_edges_by_source(local_qn)
            if edge.kind == "REFERENCES"
        } == {f"{tmp_path / 'variables.tf'}::var.region"}

        module_imports = [
            edge
            for edge in store.get_edges_by_source(str(tmp_path / "main.tf"))
            if edge.kind == "IMPORTS_FROM"
        ]
        assert len(module_imports) == 1
        assert module_imports[0].target_qualified == str(module_dir / "main.tf")
        assert result["hcl_resolution"]["references_resolved"] == 2
        assert result["hcl_resolution"]["imports_resolved"] == 1
    finally:
        store.close()


def test_non_terraform_hcl_is_recognized_without_inventing_nodes() -> None:
    parser = CodeParser()
    nodes, edges = parser.parse_bytes(
        Path("jobs/example.hcl"),
        b'job "example" { datacenters = ["dc1"] }\n',
    )

    assert len(nodes) == 1
    assert nodes[0].kind == "File"
    assert nodes[0].language == "hcl"
    assert edges == []
