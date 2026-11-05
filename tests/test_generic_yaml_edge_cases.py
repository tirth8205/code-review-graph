"""Edge cases for value-free generic YAML indexing.

Pins the behaviour introduced by PR #1092 (a port of #816) for issue #436:
generic ``.yml``/``.yaml`` files become ``YamlPath`` nodes while the
specialised Ansible, Spring and dbt handling keeps working, values never
reach the database, and change detection maps diffs to the right paths.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path, PureWindowsPath

import pytest

import code_review_graph.parser as parser_module
from code_review_graph.changes import analyze_changes, map_changes_to_nodes
from code_review_graph.cli import build_parser
from code_review_graph.graph import GraphStore, node_to_dict
from code_review_graph.incremental import (
    DEFAULT_IGNORE_PATTERNS,
    _should_ignore,
    full_build,
    get_db_path,
    incremental_update,
)
from code_review_graph.parser import CodeParser
from code_review_graph.tools.query import query_graph, semantic_search_nodes
from code_review_graph.tools.review import detect_changes_func

_DANGLING_SQL = (
    "SELECT COUNT(*) FROM edges e "
    "LEFT JOIN nodes s ON s.qualified_name = e.source_qualified "
    "LEFT JOIN nodes t ON t.qualified_name = e.target_qualified "
    "WHERE e.kind IN ('CONTAINS', 'REFERENCES') "
    "AND (s.id IS NULL OR t.id IS NULL)"
)


def _paths(nodes):
    return [node for node in nodes if node.kind == "YamlPath"]


def _names(nodes):
    return [node.name for node in _paths(nodes)]


def _endpoints_exist(nodes, edges) -> bool:
    """Every CONTAINS/REFERENCES endpoint is a node this parse emitted."""
    store_names = set()
    for node in nodes:
        if node.kind == "File":
            store_names.add(node.file_path)
        else:
            store_names.add(f"{node.file_path}::{node.identity_name or node.name}")
    return all(
        edge.source in store_names and edge.target in store_names
        for edge in edges
        if edge.kind in ("CONTAINS", "REFERENCES")
    )


def _git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


# ---------------------------------------------------------------------------
# Empty, missing and degenerate documents
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [b"", b"# only a comment\n", b"\n\n"],
    ids=["empty", "comment-only", "blank-lines"],
)
def test_empty_yaml_yields_only_a_file_node(tmp_path: Path, source: bytes) -> None:
    nodes, edges = CodeParser().parse_bytes(tmp_path / "empty.yml", source)

    assert [node.kind for node in nodes] == ["File"]
    assert edges == []
    assert nodes[0].extra["yaml_document_count"] == 0
    assert nodes[0].extra["values_indexed"] is False
    assert nodes[0].extra["yaml_truncated"] is False


def test_explicit_empty_document_is_a_null_root(tmp_path: Path) -> None:
    nodes, edges = CodeParser().parse_bytes(tmp_path / "marker.yml", b"---\n")

    assert [(n.name, n.extra["value_type"]) for n in _paths(nodes)] == [("$", "null")]
    assert _endpoints_exist(nodes, edges)


def test_scalar_null_and_sequence_roots_are_typed(tmp_path: Path) -> None:
    parser = CodeParser()
    scalar_nodes, _ = parser.parse_bytes(tmp_path / "scalar.yml", b"just-a-secret-string\n")
    null_nodes, _ = parser.parse_bytes(tmp_path / "null.yml", b"~\n")
    seq_nodes, seq_edges = parser.parse_bytes(
        tmp_path / "seq.yml", b"- alpha\n- 2\n- beta\n",
    )

    assert [(n.name, n.extra["value_type"]) for n in _paths(scalar_nodes)] == [("$", "str")]
    assert [(n.name, n.extra["value_type"]) for n in _paths(null_nodes)] == [("$", "null")]
    item = next(n for n in _paths(seq_nodes) if n.name == "$[*]")
    assert item.extra["occurrence_count"] == 3
    assert item.extra["value_types"] == ["int", "str"]
    assert item.extra["value_type"] == "mixed"
    assert _endpoints_exist(seq_nodes, seq_edges)
    assert "just-a-secret-string" not in json.dumps([n.extra for n in scalar_nodes])


def test_multi_document_paths_are_scoped_per_document(tmp_path: Path) -> None:
    source = b"kind: A\nspec: {x: 1}\n---\n---\nkind: B\nspec: {y: 2}\n"
    nodes, edges = CodeParser().parse_bytes(tmp_path / "multi.yml", source)
    roots = [n for n in _paths(nodes) if n.name == "$"]

    assert nodes[0].extra["yaml_document_count"] == 3
    assert sorted(n.extra["document_index"] for n in roots) == [0, 1, 2]
    assert next(
        n for n in roots if n.extra["document_index"] == 1
    ).extra["value_type"] == "null"
    assert len({n.identity_name for n in _paths(nodes)}) == len(_paths(nodes))
    assert {n.identity_name for n in _paths(nodes) if n.name == "$.spec.y"} == {
        "yaml:2:$.spec.y",
    }
    assert _endpoints_exist(nodes, edges)


def test_reused_anchor_names_across_documents_keep_their_own_targets(
    tmp_path: Path,
) -> None:
    source = b"base: &b {x: 1}\ncopy: *b\n---\nbase: &b {y: 2}\nother: 1\ncopy: *b\n"
    nodes, edges = CodeParser().parse_bytes(tmp_path / "anchors.yml", source)
    references = sorted(
        (edge.line, edge.target) for edge in edges if edge.kind == "REFERENCES"
    )

    assert [line for line, _ in references] == [2, 6]
    assert "::yaml:0:anchor:" in references[0][1]
    assert "::yaml:1:anchor:" in references[1][1]
    assert _endpoints_exist(nodes, edges)


# ---------------------------------------------------------------------------
# Name collisions, unicode, encodings
# ---------------------------------------------------------------------------


def test_path_syntax_in_keys_never_collides_with_structure(tmp_path: Path) -> None:
    source = (
        'a:\n  b: nested\n"a.b": dotted\n"[*]": brackets\n"$": dollar\n'
        '"quote\\"key": q\n"with space": s\nlist: [1, 2]\n'
    ).encode()
    nodes, edges = CodeParser().parse_bytes(tmp_path / "keys.yml", source)
    names = _names(nodes)

    assert "$.a.b" in names
    assert '$["a.b"]' in names
    assert '$["[*]"]' in names
    assert '$["$"]' in names
    assert '$["quote\\"key"]' in names
    assert '$["with space"]' in names
    assert "$.list[*]" in names
    assert len(names) == len(set(names))
    assert len({n.identity_name for n in _paths(nodes)}) == len(names)
    assert _endpoints_exist(nodes, edges)


def test_unicode_keys_bom_crlf_and_invalid_utf8_parse_consistently(
    tmp_path: Path,
) -> None:
    parser = CodeParser()
    lf = "clé:\n  ключ: 1\n  列表: [1, 2]\nend: x\n".encode()
    lf_nodes, _ = parser.parse_bytes(tmp_path / "lf.yml", lf)
    crlf_nodes, _ = parser.parse_bytes(
        tmp_path / "crlf.yml", b"\xef\xbb\xbf" + lf.replace(b"\n", b"\r\n"),
    )

    def shape(nodes):
        return [(n.name, n.line_start, n.line_end) for n in _paths(nodes)]

    assert "$.clé.ключ" not in _names(lf_nodes)  # non-ASCII keys are quoted
    assert '$["clé"]["ключ"]' in _names(lf_nodes)
    assert shape(crlf_nodes) == shape(lf_nodes)

    broken_nodes, broken_edges = parser.parse_bytes(
        tmp_path / "bad.yml", b"k\xff\xfe: 1\nok: 2\n",
    )
    assert "$.ok" in _names(broken_nodes)
    assert _endpoints_exist(broken_nodes, broken_edges)


def test_windows_paths_keep_forward_slash_identities(tmp_path: Path) -> None:
    windows_path = PureWindowsPath(r"C:\repo\config dir\app.yml")
    nodes, edges = CodeParser().parse_bytes(
        windows_path, b"base: &b {x: 1}\nservice:\n  <<: *b\n  port: 80\n",
    )

    assert {node.file_path for node in nodes} == {"C:/repo/config dir/app.yml"}
    assert all("\\" not in edge.source + edge.target for edge in edges)
    assert _endpoints_exist(nodes, edges)

    with GraphStore(tmp_path / "graph.db") as store:
        store.store_file_nodes_edges("C:/repo/config dir/app.yml", nodes, edges, "h")
        assert store._conn.execute(_DANGLING_SQL).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Bounds: bombs, depth, truncation, determinism, missing dependency
# ---------------------------------------------------------------------------


def test_alias_bomb_stays_linear(tmp_path: Path) -> None:
    lines = ['a0: &a0 ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]']
    for level in range(1, 10):
        lines.append(f"a{level}: &a{level} [{', '.join([f'*a{level - 1}'] * 9)}]")
    nodes, edges = CodeParser().parse_bytes(
        tmp_path / "bomb.yml", ("\n".join(lines) + "\n").encode(),
    )

    # 9 levels of 9 aliases would expand to 9**10 scalars; aliases stay edges.
    assert len(_paths(nodes)) < 200
    assert sum(edge.kind == "REFERENCES" for edge in edges) == 81
    assert _endpoints_exist(nodes, edges)


def test_alias_bomb_with_unemittable_anchors_stays_bounded(tmp_path: Path) -> None:
    long_key = "k" * 600  # anchors under over-long paths are never emitted
    lines = [f'"{long_key}0": &a0 ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]']
    for level in range(1, 10):
        lines.append(f'"{long_key}{level}": &a{level} [{", ".join([f"*a{level - 1}"] * 9)}]')
    lines.append("use: *a9")
    nodes, edges = CodeParser().parse_bytes(
        tmp_path / "bomb-long.yml", ("\n".join(lines) + "\n").encode(),
    )

    assert nodes[0].extra["yaml_truncated"] is True
    assert len(_paths(nodes)) < 200
    assert _endpoints_exist(nodes, edges)


def test_depth_limit_truncates_without_dangling_edges(tmp_path: Path) -> None:
    depth = 150
    source = "root:\n" + "".join(f"{'  ' * d}c:\n" for d in range(1, depth))
    nodes, edges = CodeParser().parse_bytes(tmp_path / "deep.yml", source.encode())

    assert nodes[0].extra["yaml_truncated"] is True
    assert 0 < len(_paths(nodes)) <= CodeParser._MAX_YAML_DEPTH + 1
    assert _endpoints_exist(nodes, edges)


def test_node_limit_with_aliases_never_leaves_dangling_edges(tmp_path: Path) -> None:
    parser = CodeParser()
    parser._MAX_YAML_NODES = 5
    source = b"base: &b {x: 1, y: 2}\na: *b\nb: *b\nc: {d: 1, e: 2}\nf: *b\n"
    nodes, edges = parser.parse_bytes(tmp_path / "limited.yml", source)

    assert nodes[0].extra["yaml_truncated"] is True
    assert len(_paths(nodes)) == 5
    assert _endpoints_exist(nodes, edges)


def test_duplicate_group_summaries_are_capped(tmp_path: Path) -> None:
    parser = CodeParser()
    parser._MAX_YAML_DUPLICATE_GROUP_SAMPLES = 1
    source = b"- {k: 1, k: 2}\n- {k: 3, k: 4}\n- {j: 1, j: 2}\n"
    nodes, _ = parser.parse_bytes(tmp_path / "dups.yml", source)
    k_path = next(n for n in _paths(nodes) if n.name == "$[*].k")

    assert nodes[0].extra["yaml_duplicate_key_count"] == 3
    assert len(nodes[0].extra["yaml_duplicate_keys"]) == 1
    assert k_path.extra["duplicate_group_count"] == 2
    assert k_path.extra["duplicate_groups_truncated"] is True
    assert len(k_path.extra["duplicate_group_samples"]) == 1


def test_parse_is_deterministic(tmp_path: Path) -> None:
    source = (
        b"defaults: &d {a: 1}\nitems:\n  - {<<: *d, b: 2}\n  - {<<: *d, c: 3}\n"
        b"dup: 1\ndup: 2\n---\nother: [1, {x: 1}]\n"
    )

    def snapshot():
        nodes, edges = CodeParser().parse_bytes(tmp_path / "det.yml", source)
        return json.dumps(
            [[n.kind, n.name, n.identity_name, n.line_start, n.line_end, n.extra]
             for n in nodes]
            + [[e.kind, e.source, e.target, e.line, e.extra] for e in edges],
            sort_keys=True,
        )

    assert snapshot() == snapshot()


def test_missing_pyyaml_degrades_to_no_nodes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(parser_module, "_yaml", None)

    assert CodeParser().parse_bytes(tmp_path / "x.yml", b"a: 1\n") == ([], [])
    assert CodeParser().parse_bytes(tmp_path / "application.yml", b"a: 1\n") == ([], [])


def test_oversized_file_is_skipped_but_limit_is_inclusive(tmp_path: Path) -> None:
    parser = CodeParser()
    source = b"key: value\n"
    parser._MAX_GENERIC_YAML_BYTES = len(source)
    assert "$.key" in _names(parser.parse_bytes(tmp_path / "fits.yml", source)[0])

    parser._MAX_GENERIC_YAML_BYTES = len(source) - 1
    assert parser.parse_bytes(tmp_path / "over.yml", source) == ([], [])


# ---------------------------------------------------------------------------
# Specialised YAML handling keeps precedence (Ansible, Spring, dbt)
# ---------------------------------------------------------------------------


def test_ansible_vars_and_role_metadata_keep_ansible_precedence(tmp_path: Path) -> None:
    parser = CodeParser()
    for relative in (
        "group_vars/all.yml",
        "roles/web/vars/main.yml",
        "roles/web/defaults/main.yml",
        "roles/web/meta/main.yml",
    ):
        nodes, _ = parser.parse_bytes(tmp_path / relative, b"feature:\n  enabled: true\n")
        assert nodes, relative
        assert not _paths(nodes), relative
        assert nodes[0].language == "ansible", relative


def test_ansible_named_files_without_ansible_content_fall_back(tmp_path: Path) -> None:
    parser = CodeParser()
    for relative in ("deploy.yml", "site.yaml", "playbooks/notes.yml", "roles/x/handlers/a.yml"):
        nodes, edges = parser.parse_bytes(tmp_path / relative, b"build:\n  steps: [a]\n")
        assert "$.build.steps" in _names(nodes), relative
        assert not any(node.language == "ansible" for node in nodes), relative
        assert _endpoints_exist(nodes, edges), relative


def test_spring_profile_files_choose_one_parser(tmp_path: Path) -> None:
    parser = CodeParser()
    spring_nodes, _ = parser.parse_bytes(
        tmp_path / "application-prod.yaml", b"server:\n  port: 8443\n",
    )
    compose_nodes, _ = parser.parse_bytes(
        tmp_path / "application-docker.yml",
        b"version: '3'\nservices:\n  db:\n    image: postgres\n",
    )
    broken_nodes = parser.parse_bytes(tmp_path / "application.yml", b"a: [oops")

    assert any(node.kind == "ConfigProperty" for node in spring_nodes)
    assert not _paths(spring_nodes)
    assert "$.services.db.image" in _names(compose_nodes)
    assert not any(node.kind == "ConfigProperty" for node in compose_nodes)
    assert broken_nodes == ([], [])


def test_dbt_project_yaml_is_indexed_without_changing_dbt_models(tmp_path: Path) -> None:
    (tmp_path / "dbt_project.yml").write_text(
        "name: shop\nconfig-version: 2\nmodel-paths: [models]\n", encoding="utf-8",
    )
    models = tmp_path / "models"
    models.mkdir()
    (models / "customers.sql").write_text("select 1 as id\n", encoding="utf-8")
    (models / "orders.sql").write_text(
        "select * from {{ ref('customers') }}\n", encoding="utf-8",
    )
    (models / "schema.yml").write_text(
        "version: 2\nmodels:\n  - name: orders\n    columns:\n      - name: id\n",
        encoding="utf-8",
    )

    with GraphStore(tmp_path / "graph.db") as store:
        result = full_build(tmp_path, store)
        model_nodes = {
            node.name: node.extra.get("sql_kind")
            for node in store.get_nodes_by_kind(["Class"])
        }
        orders_imports = {
            edge.target_qualified
            for edge in store.get_edges_by_source(str(models / "orders.sql"))
            if edge.kind == "IMPORTS_FROM"
        }
        project_paths = {
            node.name for node in store.get_nodes_by_file(str(tmp_path / "dbt_project.yml"))
        }
        schema_paths = {
            node.name for node in store.get_nodes_by_file(str(models / "schema.yml"))
        }
        dangling = store._conn.execute(_DANGLING_SQL).fetchone()[0]

    assert result["errors"] == []
    assert model_nodes == {"customers": "dbt_model", "orders": "dbt_model"}
    assert "customers" in orders_imports
    assert "$.model-paths" in project_paths
    assert "$.models[*].columns[*].name" in schema_paths
    assert dangling == 0


# ---------------------------------------------------------------------------
# Persistence: values, paths with spaces/unicode, incremental vs full
# ---------------------------------------------------------------------------


def test_values_never_reach_the_database_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "secrets.yml").write_text(
        "db:\n"
        "  password: hunter2-value\n"
        "  token: !vault tagged-value\n"
        "anchors: &a {inner: anchored-value}\n"
        "merged:\n  <<: *a\n"
        "list: [listed-value, 42]\n"
        "block: |\n  block-scalar-value\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "graph.db"
    with GraphStore(db_path) as store:
        full_build(repo, store)
        names = {node.name for node in store.get_nodes_by_file(str(repo / "secrets.yml"))}
        store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    raw = b"".join(
        path.read_bytes() for path in tmp_path.glob("graph.db*") if path.is_file()
    )
    assert {"$.db.password", "$.db.token", "$.block"} <= names
    for value in (
        b"hunter2-value", b"tagged-value", b"anchored-value",
        b"listed-value", b"block-scalar-value",
    ):
        assert value not in raw, value


def test_spaces_and_unicode_in_file_paths_round_trip(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    config_dir = repo / "config dir" / "ünï"
    config_dir.mkdir(parents=True)
    config = config_dir / "app settings.yml"
    config.write_text("retries: 3\nservices: [{name: api}]\n", encoding="utf-8")

    db_path = get_db_path(repo)
    with GraphStore(db_path) as store:
        result = full_build(repo, store)
        dangling = store._conn.execute(_DANGLING_SQL).fetchone()[0]

    assert result["errors"] == []
    assert dangling == 0
    root = query_graph("children_of", str(config), repo_root=str(repo))
    assert [node["name"] for node in root["results"]] == ["$"]
    children = query_graph(
        "children_of", f"{config.as_posix()}::yaml:0:$", repo_root=str(repo),
    )
    assert {node["name"] for node in children["results"]} == {"$.retries", "$.services"}
    found = semantic_search_nodes("retries", kind="YamlPath", repo_root=str(repo))
    assert [node["name"] for node in found["results"]] == ["$.retries"]


def test_incremental_update_matches_full_build_and_drops_deleted_yaml(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "keep.yml").write_text("a: &x {b: 1}\nc: *x\n", encoding="utf-8")
    (repo / "gone.yaml").write_text("old:\n  key: 1\n", encoding="utf-8")
    (repo / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    _commit_all(repo, "initial")

    def snapshot(store):
        nodes = store._conn.execute(
            "SELECT kind, qualified_name, line_start, line_end, extra FROM nodes "
            "WHERE language = 'yaml' ORDER BY qualified_name"
        ).fetchall()
        edges = store._conn.execute(
            "SELECT kind, source_qualified, target_qualified, line FROM edges "
            "WHERE kind IN ('CONTAINS', 'REFERENCES') ORDER BY 1, 2, 3, 4"
        ).fetchall()
        return [tuple(row) for row in nodes], [tuple(row) for row in edges]

    with GraphStore(tmp_path / "incremental.db") as incremental:
        full_build(repo, incremental)
        first = snapshot(incremental)
        full_build(repo, incremental)
        assert snapshot(incremental) == first  # rebuilding is idempotent

        (repo / "keep.yml").write_text(
            "a: &x {b: 1}\nc: *x\nnew:\n  nested: true\n", encoding="utf-8",
        )
        (repo / "gone.yaml").unlink()
        _commit_all(repo, "edit yaml")
        incremental_update(repo, incremental)
        after_update = snapshot(incremental)
        dangling = incremental._conn.execute(_DANGLING_SQL).fetchone()[0]

    with GraphStore(tmp_path / "full.db") as full:
        full_build(repo, full)
        after_full = snapshot(full)

    assert dangling == 0
    assert after_update == after_full
    assert not any("gone.yaml" in row[1] for row in after_update[0])
    assert any(row[1].endswith("::yaml:0:$.new.nested") for row in after_update[0])


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------


def _store_yaml(store: GraphStore, path: Path, source: bytes) -> None:
    nodes, edges = CodeParser().parse_bytes(path, source)
    store.store_file_nodes_edges(str(path), nodes, edges, "h")


def _changed_paths(store: GraphStore, path: Path, ranges) -> set[str]:
    return {
        node.name
        for node in map_changes_to_nodes(store, {str(path): ranges})
        if node.kind == "YamlPath"
    }


def test_detect_changes_reports_yaml_paths_end_to_end(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / ".gitignore").write_text(".code-review-graph/\n", encoding="utf-8")
    config = repo / "settings.yml"
    config.write_text("server:\n  port: 80\n  host: local\nlog: info\n", encoding="utf-8")
    _commit_all(repo, "initial")
    with GraphStore(get_db_path(repo)) as store:
        full_build(repo, store)

    config.write_text("server:\n  port: 8080\n  host: local\nlog: info\n", encoding="utf-8")
    _commit_all(repo, "bump port")
    with GraphStore(get_db_path(repo)) as store:
        incremental_update(repo, store)

    standard = detect_changes_func(base="HEAD~1", repo_root=str(repo))
    minimal = detect_changes_func(
        base="HEAD~1", repo_root=str(repo), detail_level="minimal",
    )

    changed = {path["name"] for path in standard["changed_yaml_paths"]}
    assert changed == {"$", "$.server", "$.server.port"}
    assert standard["changed_functions"] == []
    assert standard["test_gaps"] == []
    assert minimal["changed_yaml_path_count"] == 3
    assert "3 changed YAML path(s)" in minimal["summary"]


def test_changed_yaml_path_cap_is_reported(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CRG_MAX_CHANGED_YAML_PATHS", "2")
    path = tmp_path / "config.yml"
    with GraphStore(tmp_path / "graph.db") as store:
        _store_yaml(store, path, b"a: 1\nb: 2\nc: 3\nd: 4\n")
        result = analyze_changes(
            store, changed_files=[str(path)], changed_ranges={str(path): [(1, 4)]},
        )

    assert len(result["changed_yaml_paths"]) == 2
    assert result["yaml_paths_truncated"] is True
    assert "CRG_MAX_CHANGED_YAML_PATHS" in result["summary"]


def test_truncated_line_samples_fall_back_to_the_node_span(tmp_path: Path) -> None:
    parser = CodeParser()
    parser._MAX_YAML_OCCURRENCE_SAMPLES = 1
    path = tmp_path / "items.yml"
    nodes, edges = parser.parse_bytes(
        path, b"items:\n  - name: a\n  - name: b\n  - name: c\n",
    )
    name_path = next(n for n in _paths(nodes) if n.name == "$.items[*].name")
    assert name_path.extra["lines_truncated"] is True
    assert name_path.extra["range_samples"] == [[2, 2]]

    with GraphStore(tmp_path / "graph.db") as store:
        store.store_file_nodes_edges(str(path), nodes, edges, "h")
        changed = _changed_paths(store, path, [(4, 4)])

    assert "$.items[*].name" in changed


@pytest.mark.xfail(
    strict=True,
    reason="_yaml_end_line ends block collections on the next sibling key's line",
)
def test_appending_a_sibling_key_does_not_flag_the_previous_block(
    tmp_path: Path,
) -> None:
    """Block collections end on their last content line, not the next key."""
    path = tmp_path / "pipeline.yml"
    source = b"build:\n  steps: [compile, test]\ndeploy:\n  region: eu\n"
    with GraphStore(tmp_path / "graph.db") as store:
        _store_yaml(store, path, source)
        appended = _changed_paths(store, path, [(3, 4)])
        renamed_key = _changed_paths(store, path, [(3, 3)])

    assert appended == {"$", "$.deploy", "$.deploy.region"}
    assert "$.build" not in renamed_key


def test_unchanged_sibling_scalar_is_not_reported(tmp_path: Path) -> None:
    path = tmp_path / "flat.yml"
    with GraphStore(tmp_path / "graph.db") as store:
        _store_yaml(store, path, b"a: 1\nb: 2\nc: 3\n")
        changed = _changed_paths(store, path, [(2, 2)])

    assert changed == {"$", "$.b"}


# ---------------------------------------------------------------------------
# Public surface: serialisation, CLI, ignore patterns
# ---------------------------------------------------------------------------


def test_node_to_dict_tolerates_malformed_yaml_metadata(tmp_path: Path) -> None:
    path = tmp_path / "x.yml"
    with GraphStore(tmp_path / "graph.db") as store:
        nodes, edges = CodeParser().parse_bytes(path, b"a: 1\n")
        target = next(node for node in nodes if node.name == "$.a")
        target.extra.update({
            "schema_path": 7,
            "occurrence_count": True,
            "value_types": ["str", 3, "\x00evil"],
            "line_samples": ["1", 2, False],
            "is_alias": "yes",
        })
        store.store_file_nodes_edges(str(path), nodes, edges, "h")
        stored = {
            "File" if node.kind == "File" else node.name: node
            for node in store.get_nodes_by_file(str(path))
        }
        python_file = tmp_path / "plain.py"
        py_nodes, py_edges = CodeParser().parse_bytes(python_file, b"def f():\n    pass\n")
        store.store_file_nodes_edges(str(python_file), py_nodes, py_edges, "h")
        plain_file = next(
            node for node in store.get_nodes_by_file(str(python_file))
            if node.kind == "File"
        )

    yaml_dict = node_to_dict(stored["$.a"])["yaml"]
    assert "schema_path" not in yaml_dict
    assert "occurrence_count" not in yaml_dict
    assert "is_alias" not in yaml_dict
    assert yaml_dict["value_types"] == ["str", "evil"]
    assert yaml_dict["line_samples"] == [2]
    assert node_to_dict(stored["File"])["yaml"]["values_indexed"] is False
    assert "yaml" not in node_to_dict(plain_file)


def test_cli_search_accepts_yaml_path_kind() -> None:
    args = build_parser().parse_args(["search", "retries", "--kind", "YamlPath"])

    assert args.kind == "YamlPath"


_NESTED_PNPM_XFAIL = pytest.mark.xfail(
    strict=True,
    reason="'pnpm-lock.yaml' ignore pattern matches only the repository root",
)


@pytest.mark.parametrize(
    "relative",
    [
        "pnpm-lock.yaml",
        pytest.param("packages/web/pnpm-lock.yaml", marks=_NESTED_PNPM_XFAIL),
        pytest.param("docs/site/pnpm-lock.yaml", marks=_NESTED_PNPM_XFAIL),
    ],
)
def test_pnpm_lockfiles_are_ignored_at_any_depth(relative: str) -> None:
    assert _should_ignore(relative, DEFAULT_IGNORE_PATTERNS)
