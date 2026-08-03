import json
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import CodeParser
from code_review_graph.tools.query import query_graph

YAML_SOURCE = b"""
services:
  - id: api
    metadata:
      tier: frontend
    options:
      enabled: true
  - id: worker
    metadata:
      tier: backend
secret-token: never-index-this
"""


def _yaml_paths(nodes):
    return [node for node in nodes if node.kind == "YamlPath"]


def test_generic_yaml_indexes_nested_paths_without_values(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    nodes, edges = CodeParser().parse_bytes(path, YAML_SOURCE)
    yaml_paths = _yaml_paths(nodes)

    assert any(node.kind == "File" for node in nodes)
    assert {node.name for node in yaml_paths} >= {
        "$",
        "$.services",
        "$.services[*]",
        "$.services[*].id",
        "$.services[*].metadata.tier",
        "$.services[*].options.enabled",
        "$.secret-token",
    }
    tier = next(
        node for node in yaml_paths
        if node.name == "$.services[*].metadata.tier"
    )
    assert tier.extra["schema_path"] == "$.services[*].metadata.tier"
    assert tier.extra["value_type"] == "str"
    assert tier.extra["occurrence_count"] == 2
    assert tier.extra["line_samples"] == [5, 10]
    assert all(edge.kind == "CONTAINS" for edge in edges)

    serialized = json.dumps([
        [node.name, node.extra] for node in nodes
    ] + [
        [edge.source, edge.target, edge.extra] for edge in edges
    ])
    assert "never-index-this" not in serialized
    assert "frontend" not in serialized
    assert "worker" not in serialized
    assert all(node.extra.get("values_indexed") is False for node in nodes[:1])


def test_duplicate_keys_are_distinct_and_scoped_to_one_mapping(tmp_path: Path) -> None:
    source = b"""
record:
  label: first
  label: second
  nested:
    label: not-a-duplicate
---
record:
  label: another-document
"""
    path = tmp_path / "duplicate.yml"
    nodes, edges = CodeParser().parse_bytes(path, source)
    labels = [node for node in _yaml_paths(nodes) if node.name == "$.record.label"]

    assert len(labels) == 2
    first_document = [node for node in labels if node.extra["document_index"] == 0]
    assert len(first_document) == 1
    assert first_document[0].extra["occurrence_count"] == 2
    assert first_document[0].extra["duplicate_key_occurrence_count"] == 2
    assert first_document[0].extra["duplicate_group_count"] == 1
    assert first_document[0].extra["duplicate_key"] is True
    assert labels[-1].extra["duplicate_key"] is False
    assert all(
        node.extra["duplicate_key"] is False
        for node in _yaml_paths(nodes)
        if node.name == "$.record.nested.label"
    )

    file_node = nodes[0]
    assert file_node.extra["yaml_duplicate_key_count"] == 1
    assert file_node.extra["yaml_duplicate_keys"] == [{
        "document_index": 0,
        "path": "$.record.label",
        "example_path": "$.record.label",
        "occurrence_count": 2,
        "line_samples": [3, 4],
        "lines_truncated": False,
    }]

    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()
    with GraphStore(graph_dir / "graph.db") as store:
        store.store_file_nodes_edges(str(path), nodes, edges, "yaml")
        stored = [
            node for node in store.get_nodes_by_file(str(path))
            if node.name == "$.record.label"
        ]
        assert len(stored) == 2
        assert len({node.qualified_name for node in stored}) == 2


def test_typed_and_complex_mapping_keys_cannot_collide(tmp_path: Path) -> None:
    source = b"""
1: integer-key
"1": string-key
? [one, two]
: complex-key
"""
    nodes, edges = CodeParser().parse_bytes(tmp_path / "keys.yml", source)
    numeric_paths = [node for node in _yaml_paths(nodes) if node.name == '$["1"]']

    assert len(numeric_paths) == 1
    assert numeric_paths[0].extra["duplicate_key"] is False
    assert numeric_paths[0].extra["occurrence_count"] == 2
    assert len(numeric_paths[0].extra["key_tags"]) == 2
    assert nodes[0].extra["yaml_unsupported_key_count"] == 1
    assert len(edges) == 2  # File -> document root -> the supported aggregated path.


def test_alias_and_merge_emit_canonical_reference_edges(tmp_path: Path) -> None:
    source = b"""
defaults: &defaults
  tier: primary
service:
  <<: *defaults
  tier: secondary
"""
    path = tmp_path / "aliases.yml"
    nodes, edges = CodeParser().parse_bytes(path, source)
    yaml_paths = _yaml_paths(nodes)
    merge = next(node for node in yaml_paths if node.name == '$.service["<<"]')
    local_tier = next(node for node in yaml_paths if node.name == "$.service.tier")
    references = [edge for edge in edges if edge.kind == "REFERENCES"]
    anchor = next(
        node for node in yaml_paths
        if node.name == "$.defaults" and node.extra.get("anchor_definition")
    )

    assert len(references) == 1
    assert references[0].source.endswith(merge.identity_name)
    assert references[0].target.endswith(anchor.identity_name)
    assert references[0].extra == {"yaml_alias": True, "yaml_merge": True}
    assert local_tier.extra["duplicate_key"] is False


def test_root_and_sequence_aliases_keep_canonical_targets_and_lines(tmp_path: Path) -> None:
    root_nodes, root_edges = CodeParser().parse_bytes(
        tmp_path / "root.yml",
        b"&root\nname: app\nself: *root\n",
    )
    root_reference = next(edge for edge in root_edges if edge.kind == "REFERENCES")
    root_alias = next(node for node in _yaml_paths(root_nodes) if node.extra["is_alias"])

    root_anchor = next(
        node for node in _yaml_paths(root_nodes)
        if node.name == "$" and node.extra.get("anchor_definition")
    )
    assert root_reference.target.endswith(root_anchor.identity_name)
    assert root_reference.line == 3
    assert root_alias.name == "$.self"
    assert not any(node.name.startswith("$.self.") for node in _yaml_paths(root_nodes))

    sequence_nodes, sequence_edges = CodeParser().parse_bytes(
        tmp_path / "sequence.yml",
        b"refs:\n  - &item value\n  - *item\n",
    )
    sequence_alias = next(
        node for node in _yaml_paths(sequence_nodes) if node.extra["is_alias"]
    )
    sequence_reference = next(
        edge for edge in sequence_edges if edge.kind == "REFERENCES"
    )

    assert sequence_alias.name == "$.refs[1]"
    assert sequence_alias.line_start == 3
    assert sequence_reference.line == 3


def test_alias_lines_ignore_aliases_below_unsupported_complex_keys(
    tmp_path: Path,
) -> None:
    source = b"base: &base {x: one}\n? [*base]\n: ignored\ncopy: *base\n"
    nodes, edges = CodeParser().parse_bytes(tmp_path / "complex-key.yml", source)
    copy = next(node for node in _yaml_paths(nodes) if node.name == "$.copy")
    reference = next(edge for edge in edges if edge.kind == "REFERENCES")

    assert nodes[0].extra["yaml_unsupported_key_count"] == 1
    assert copy.extra["is_alias"] is True
    assert copy.line_start == 4
    assert reference.line == 4


def test_anchored_typed_keys_keep_distinct_reference_targets(tmp_path: Path) -> None:
    source = b'''1: &int-key {x: one}
"1": &str-key {y: two}
copy_int: *int-key
copy_str: *str-key
'''
    nodes, edges = CodeParser().parse_bytes(tmp_path / "typed-anchors.yml", source)
    definitions = [
        node for node in _yaml_paths(nodes)
        if node.name == '$["1"]' and node.extra.get("anchor_definition")
    ]
    references = [edge for edge in edges if edge.kind == "REFERENCES"]

    assert len(definitions) == 2
    assert len({node.identity_name for node in definitions}) == 2
    assert {tuple(node.extra["key_tags"]) for node in definitions} == {
        ("tag:yaml.org,2002:int",),
        ("tag:yaml.org,2002:str",),
    }
    assert len(references) == 2
    assert len({edge.target for edge in references}) == 2
    assert {edge.line for edge in references} == {3, 4}


def test_duplicate_anchored_keys_keep_distinct_reference_targets(tmp_path: Path) -> None:
    source = b'''item: &first {field: one}
item: &second {field: two}
copy_first: *first
copy_second: *second
'''
    nodes, edges = CodeParser().parse_bytes(tmp_path / "duplicate-anchors.yml", source)
    definitions = [
        node for node in _yaml_paths(nodes)
        if node.name == "$.item" and node.extra.get("anchor_definition")
    ]
    references = [edge for edge in edges if edge.kind == "REFERENCES"]

    assert len(definitions) == 2
    assert all(node.extra["duplicate_key"] is True for node in definitions)
    assert len({node.identity_name for node in definitions}) == 2
    assert len({edge.target for edge in references}) == 2


def test_merge_sequence_marks_every_alias_reference(tmp_path: Path) -> None:
    source = b"""
first: &first {field: primary}
second: &second {tier: one}
service:
  <<: [*first, *second]
"""
    _, edges = CodeParser().parse_bytes(tmp_path / "merge.yml", source)
    references = [edge for edge in edges if edge.kind == "REFERENCES"]

    assert len(references) == 2
    assert all(edge.extra["yaml_merge"] is True for edge in references)
    assert all(edge.line == 5 for edge in references)


def test_malformed_and_recursive_yaml_fail_safely(tmp_path: Path) -> None:
    parser = CodeParser()
    assert parser.parse_bytes(tmp_path / "broken.yml", b"a: [unterminated") == ([], [])

    nodes, _ = parser.parse_bytes(
        tmp_path / "recursive.yml",
        b"root: &root\n  self: *root\n",
    )
    assert len(nodes) < 10
    recursive = next(node for node in _yaml_paths(nodes) if node.name == "$.root.self")
    assert recursive.extra["is_alias"] is True


def test_deep_yaml_and_duplicate_metadata_are_bounded(tmp_path: Path) -> None:
    parser = CodeParser()
    deep = "root:\n" + "".join(
        f"{'  ' * depth}child:\n" for depth in range(1, 800)
    )
    assert parser.parse_bytes(tmp_path / "deep.yml", deep.encode()) == ([], [])

    parser._MAX_YAML_DUPLICATE_LINE_SAMPLES = 3
    duplicate_source = (
        b"root:\n  repeated: one\n  repeated: two\n"
        b"  repeated: three\n  repeated: four\n"
    )
    nodes, _ = parser.parse_bytes(tmp_path / "bounded.yml", duplicate_source)
    repeated = next(node for node in _yaml_paths(nodes) if node.name == "$.root.repeated")
    group = repeated.extra["duplicate_group_samples"][0]

    assert group["occurrence_count"] == 4
    assert group["line_samples"] == [2, 3, 4]
    assert group["lines_truncated"] is True


def test_yaml_node_limit_marks_file_as_truncated(tmp_path: Path) -> None:
    parser = CodeParser()
    parser._MAX_YAML_NODES = 2

    nodes, _ = parser.parse_bytes(
        tmp_path / "large.yml",
        b"one: 1\ntwo: 2\nthree: 3\n",
    )

    assert len(_yaml_paths(nodes)) == 2
    assert nodes[0].extra["yaml_truncated"] is True


def test_long_duplicate_paths_are_bounded_in_internal_metadata(tmp_path: Path) -> None:
    long_key = "x" * 600
    source = f'"{long_key}": one\n"{long_key}": two\n'.encode()

    nodes, _ = CodeParser().parse_bytes(tmp_path / "long-key.yml", source)
    duplicate = nodes[0].extra["yaml_duplicate_keys"][0]
    serialized = json.dumps([node.extra for node in nodes])

    assert nodes[0].extra["yaml_truncated"] is True
    assert duplicate["occurrence_count"] == 2
    assert len(duplicate["path"]) == CodeParser._MAX_YAML_PATH_LENGTH
    assert len(duplicate["example_path"]) == CodeParser._MAX_YAML_PATH_LENGTH
    assert long_key not in serialized


def test_spring_and_ansible_keep_precedence(tmp_path: Path) -> None:
    parser = CodeParser()
    spring_nodes, _ = parser.parse_bytes(
        tmp_path / "application.yml",
        b"spring:\n  datasource:\n    url: jdbc:test\n",
    )
    ansible_nodes, _ = parser.parse_bytes(
        tmp_path / "playbooks" / "site.yml",
        b"- name: run check\n  hosts: all\n  tasks:\n    - debug:\n        msg: ready\n",
    )

    assert any(node.kind == "ConfigProperty" for node in spring_nodes)
    assert not any(node.kind == "YamlPath" for node in spring_nodes)
    assert any(node.language == "ansible" for node in ansible_nodes)
    assert not any(node.kind == "YamlPath" for node in ansible_nodes)


def test_full_build_query_has_no_dangling_yaml_edges(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_bytes(YAML_SOURCE)
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()

    with GraphStore(graph_dir / "graph.db") as store:
        result = full_build(tmp_path, store)
        dangling = store._conn.execute(
            "SELECT COUNT(*) FROM edges e "
            "LEFT JOIN nodes s ON s.qualified_name = e.source_qualified "
            "LEFT JOIN nodes t ON t.qualified_name = e.target_qualified "
            "WHERE e.kind IN ('CONTAINS', 'REFERENCES') "
            "AND (s.id IS NULL OR t.id IS NULL)"
        ).fetchone()[0]

    assert result["errors"] == []
    assert result["files_parsed"] == 1
    assert dangling == 0

    children = query_graph("children_of", str(path), repo_root=str(tmp_path))
    assert children["status"] == "ok"
    assert {node["name"] for node in children["results"]} == {"$"}
    assert children["results"][0]["yaml"]["value_type"] == "mapping"

    root = query_graph(
        "children_of",
        f"{path.as_posix()}::yaml:0:$",
        repo_root=str(tmp_path),
    )
    assert {node["name"] for node in root["results"]} == {"$.services", "$.secret-token"}

    summary = query_graph("file_summary", str(path), repo_root=str(tmp_path))
    file_result = next(node for node in summary["results"] if node["kind"] == "File")
    services_result = next(
        node for node in summary["results"] if node["name"] == "$.services"
    )
    assert file_result["yaml"] == {
        "document_count": 1,
        "duplicate_key_count": 0,
        "unsupported_key_count": 0,
        "truncated": False,
        "values_indexed": False,
        "duplicate_keys": [],
    }
    assert services_result["yaml"]["schema_path"] == "$.services"
