import json
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser
from code_review_graph.tools.query import query_graph

YAML_SOURCE = b"""
spring:
  datasource:
    url: jdbc:postgresql://localhost/orders
    password: yaml-super-secret
  kafka:
    bootstrap-servers:
      - broker-one:9092
      - broker-two:9092
---
app:
  api-token: second-super-secret
"""

PROPERTIES_SOURCE = b"""
# Spring profile configuration
payment.gateway.url=https://pay.example.test
payment.api-token:properties-super-secret
spring.datasource.password = another-secret
"""

JAVA_SOURCE = b"""
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "spring.kafka")
class KafkaSettings {
    @Value("${payment.gateway.url}")
    String gateway;

    @Value("${payment.api-token:do-not-store-this-default}")
    String token;
}
"""


def test_only_conventional_spring_files_are_classified(tmp_path: Path) -> None:
    parser = CodeParser()

    assert parser.detect_language(tmp_path / "application.yml") == "spring_config"
    assert parser.detect_language(tmp_path / "application-prod.yaml") == "spring_config"
    assert parser.detect_language(tmp_path / "application.properties") == "spring_config"
    assert parser.detect_language(tmp_path / "app.properties") is None
    assert parser.detect_language(tmp_path / "workflow.yml") == "yaml"

    assert parser.parse_bytes(tmp_path / "workflow.yml", YAML_SOURCE) == ([], [])
    assert parser.parse_bytes(tmp_path / "app.properties", PROPERTIES_SOURCE) == ([], [])


def test_confirmed_ansible_path_keeps_ansible_precedence(tmp_path: Path) -> None:
    path = tmp_path / "roles" / "demo" / "tasks" / "application.yml"
    source = b"- name: install package\n  ansible.builtin.package:\n    name: curl\n"

    nodes, _ = CodeParser().parse_bytes(path, source)

    assert nodes
    assert {node.language for node in nodes} == {"ansible"}
    assert not any(node.kind == "ConfigProperty" for node in nodes)


def test_non_spring_application_yaml_is_not_indexed_as_config(tmp_path: Path) -> None:
    parser = CodeParser()
    github_actions = b"name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
    kubernetes = b"apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\n"

    assert parser.parse_bytes(tmp_path / "application.yml", github_actions) == ([], [])
    assert parser.parse_bytes(tmp_path / "application.yaml", kubernetes) == ([], [])


def test_ansible_content_wins_even_without_ansible_path(tmp_path: Path) -> None:
    source = b"- name: install package\n  hosts: all\n  tasks:\n    - debug:\n        msg: ready\n"

    nodes, _ = CodeParser().parse_bytes(tmp_path / "application.yml", source)

    assert nodes
    assert {node.language for node in nodes} == {"ansible"}
    assert not any(node.kind == "ConfigProperty" for node in nodes)


def test_yaml_config_indexes_keys_without_values(tmp_path: Path) -> None:
    path = tmp_path / "application.yml"
    nodes, edges = CodeParser().parse_bytes(path, YAML_SOURCE)
    properties = [node for node in nodes if node.kind == "ConfigProperty"]

    assert edges == []
    assert any(node.kind == "File" for node in nodes)
    assert {node.name for node in properties} == {
        "spring.datasource.url",
        "spring.datasource.password",
        "spring.kafka.bootstrapServers[0]",
        "spring.kafka.bootstrapServers[1]",
        "app.apiToken",
    }
    serialized_metadata = json.dumps([node.extra for node in properties])
    assert "yaml-super-secret" not in serialized_metadata
    assert "second-super-secret" not in serialized_metadata
    assert "jdbc:postgresql" not in serialized_metadata
    assert all("config_value" not in node.extra for node in properties)


def test_properties_config_indexes_keys_without_values(tmp_path: Path) -> None:
    path = tmp_path / "application-prod.properties"
    nodes, edges = CodeParser().parse_bytes(path, PROPERTIES_SOURCE)
    properties = [node for node in nodes if node.kind == "ConfigProperty"]

    assert edges == []
    assert {node.name for node in properties} == {
        "payment.gateway.url",
        "payment.apiToken",
        "spring.datasource.password",
    }
    serialized_metadata = json.dumps([node.extra for node in properties])
    assert "properties-super-secret" not in serialized_metadata
    assert "another-secret" not in serialized_metadata
    assert "https://" not in serialized_metadata
    assert all("config_value" not in node.extra for node in properties)


def test_java_config_annotations_emit_key_only_dependencies(tmp_path: Path) -> None:
    path = tmp_path / "KafkaSettings.java"
    _, edges = CodeParser().parse_bytes(path, JAVA_SOURCE)
    config_edges = [edge for edge in edges if edge.kind == "DEPENDS_ON_CONFIG"]

    assert {edge.target for edge in config_edges} == {
        "config:spring.kafka.*",
        "config:payment.gateway.url",
        "config:payment.apiToken",
    }
    assert {edge.source for edge in config_edges} == {f"{path}::KafkaSettings"}
    serialized_metadata = json.dumps([edge.extra for edge in config_edges])
    assert "do-not-store-this-default" not in serialized_metadata


def test_consumers_query_matches_direct_and_prefix_dependencies(tmp_path: Path) -> None:
    yaml_path = tmp_path / "application.yml"
    profile_path = tmp_path / "application-prod.yml"
    java_path = tmp_path / "KafkaSettings.java"
    yaml_nodes, yaml_edges = CodeParser().parse_bytes(yaml_path, YAML_SOURCE)
    profile_nodes, profile_edges = CodeParser().parse_bytes(profile_path, YAML_SOURCE)
    java_nodes, java_edges = CodeParser().parse_bytes(java_path, JAVA_SOURCE)
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()
    with GraphStore(graph_dir / "graph.db") as store:
        store.store_file_nodes_edges(str(yaml_path), yaml_nodes, yaml_edges, "yaml")
        store.store_file_nodes_edges(
            str(profile_path),
            profile_nodes,
            profile_edges,
            "profile",
        )
        store.store_file_nodes_edges(str(java_path), java_nodes, java_edges, "java")

    direct = query_graph("consumers_of", "payment.gateway.url", repo_root=str(tmp_path))
    assert direct["status"] == "ok"
    assert [result["name"] for result in direct["results"]] == ["KafkaSettings"]

    prefix = query_graph(
        "consumers_of",
        "spring.kafka.bootstrap-servers[0]",
        repo_root=str(tmp_path),
    )
    assert prefix["status"] == "ok"
    assert [result["name"] for result in prefix["results"]] == ["KafkaSettings"]
    assert {edge["kind"] for edge in prefix["edges"]} == {"DEPENDS_ON_CONFIG"}
