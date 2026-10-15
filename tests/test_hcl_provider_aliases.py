"""Aliased providers retain identities and context-sensitive references (#985)."""

from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser

SOURCE = b'''
provider "aws" { region = "us-east-1" }
provider "aws" {
  alias = "west"
  region = "us-west-2"
}
provider "aws" { alias = "eu" }
resource "aws_instance" "w" {
  provider = aws.west
  tags = { ref = aws.west.id }
}
module "child" {
  source = "./child"
  providers = { aws = aws, aws.west = aws.eu }
}
'''


def parse():
    return CodeParser().parse_bytes(Path('main.tf'), SOURCE)


def test_provider_names_include_alias():
    nodes, _ = parse()
    names = [n.name for n in nodes if n.extra.get('hcl_type') == 'provider']
    assert names == ['provider.aws', 'provider.aws.west', 'provider.aws.eu']


def test_resource_provider_reference_has_provider_namespace():
    _, edges = parse()
    targets = {e.target for e in edges if e.kind == 'REFERENCES'
               and e.source == 'main.tf::resource.aws_instance.w'}
    assert targets == {'main.tf::provider.aws.west', 'main.tf::resource.aws.west'}


def test_module_provider_map_reads_values_not_child_provider_keys():
    _, edges = parse()
    targets = {e.target for e in edges if e.kind == 'REFERENCES'
               and e.source == 'main.tf::module.child'}
    assert targets == {'main.tf::provider.aws', 'main.tf::provider.aws.eu'}


def test_storing_provider_nodes_does_not_overwrite_aliases(tmp_path):
    nodes, edges = parse()
    with GraphStore(tmp_path / 'graph.db') as store:
        store.store_file_nodes_edges('main.tf', nodes, edges)
        providers = [n for n in store.get_nodes_by_file('main.tf')
                     if n.name.startswith('provider.')]
        assert len(providers) == 3
        assert len({n.line_start for n in providers}) == 3


def test_nested_attribute_named_provider_is_an_ordinary_resource_reference():
    _, edges = CodeParser().parse_bytes(Path('main.tf'), b'''
resource "example" "x" {
  config { provider = aws.west.id }
}
''')
    assert {e.target for e in edges if e.kind == 'REFERENCES'} == {
        'main.tf::resource.aws.west'
    }


@pytest.mark.parametrize("alias", ["", ".fs"])
def test_local_provider_does_not_resolve_to_a_local_value(alias):
    nodes, edges = CodeParser().parse_bytes(Path("main.tf"), f'''
provider "local" {{}}
provider "local" {{ alias = "fs" }}
locals {{ fs = "ordinary local" }}
resource "local_file" "x" {{
  provider = local{alias}
  content = local.fs
}}
module "child" {{
  source = "./child"
  providers = {{ local = local, local.other = local.fs }}
}}
'''.encode())
    resource_refs = {e.target for e in edges if e.kind == "REFERENCES"
                     and e.source == "main.tf::resource.local_file.x"}
    assert resource_refs == {f"main.tf::provider.local{alias}", "main.tf::local.fs"}
    module_refs = {e.target for e in edges if e.kind == "REFERENCES"
                   and e.source == "main.tf::module.child"}
    assert module_refs == {"main.tf::provider.local", "main.tf::provider.local.fs"}


def test_indexed_provider_preserves_iterator_references():
    _, edges = CodeParser().parse_bytes(Path("main.tf"), b'''
resource "aws_instance" "x" { provider = aws.by_region[each.key] }
''')
    assert {e.target for e in edges if e.kind == "REFERENCES"} == {
        "main.tf::provider.aws.by_region"
    }
