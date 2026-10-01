"""Aliased providers retain identities and context-sensitive references (#985)."""

from pathlib import Path

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
