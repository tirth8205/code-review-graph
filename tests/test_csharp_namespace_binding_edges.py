"""Edge cases for C# namespace identity and lexical call binding (PR #964, issue #946).

Every case drives ``full_build`` / ``incremental_update`` (the paths the CLI and
MCP tools use) on a scratch tree and reads the persisted graph back. Cases that
only guard behaviour the change must not break are marked as such.
"""

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update
from code_review_graph.parser import CodeParser, EdgeInfo
from code_review_graph.scoped_resolver import resolve_scoped_calls


def _write(root: Path, files: dict[str, str]) -> None:
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


def _store(root: Path) -> GraphStore:
    return GraphStore(root / ".code-review-graph" / "graph.db")


def _calls(store: GraphStore, root: Path) -> list[tuple[str, str]]:
    prefix = f"{root}/"
    return sorted(
        (row[0].replace(prefix, ""), row[1].replace(prefix, ""))
        for row in store._conn.execute(
            "SELECT source_qualified, target_qualified FROM edges WHERE kind = 'CALLS'",
        )
    )


def _names(store: GraphStore, name: str, root: Path) -> list[str]:
    prefix = f"{root}/"
    return sorted(
        row[0].replace(prefix, "")
        for row in store._conn.execute(
            "SELECT qualified_name FROM nodes WHERE name = ?", (name,),
        )
    )


def test_same_leaf_nested_types_keep_distinct_identities_and_bind_own_members(tmp_path):
    # The C# analogue of #999/#1087: same-leaf types in one file must not collapse.
    _write(tmp_path, {"Both.cs": (
        "namespace X { class Dup { public void M() {} } }\n"
        "namespace Y { class Dup { public void M() {} } }\n"
        "class Alpha { class Same { void M() { N(); } void N() {} } }\n"
        "class Beta { class Same { void M() { N(); } void N() {} } }\n"
    )})
    with _store(tmp_path) as store:
        full_build(tmp_path, store)
        assert _names(store, "M", tmp_path) == [
            "Both.cs::Alpha.Same.M", "Both.cs::Beta.Same.M",
            "Both.cs::X.Dup.M", "Both.cs::Y.Dup.M",
        ]
        assert _calls(store, tmp_path) == [
            ("Both.cs::Alpha.Same.M", "Both.cs::Alpha.Same.N"),
            ("Both.cs::Beta.Same.M", "Both.cs::Beta.Same.N"),
        ]


def test_type_kinds_top_level_statements_and_members_bind_through_usings(tmp_path):
    _write(tmp_path, {
        "Types.cs": "namespace A { public struct S { public static void Run() {} } "
                    "public record R { public static void Run() {} } "
                    "public interface I { void Run(); } }",
        "Caller.cs": "using A; namespace App { class C { "
                     "void Go(I i) { S.Run(); R.Run(); i.Run(); } } }",
        "Program.cs": "using A;\nS.Run();\n",
        "Empty.cs": "",
    })
    with _store(tmp_path) as store:
        result = full_build(tmp_path, store)
        assert result["errors"] == []
        assert _calls(store, tmp_path) == [
            ("Caller.cs::App.C.Go", "Types.cs::A.I.Run"),
            ("Caller.cs::App.C.Go", "Types.cs::A.R.Run"),
            ("Caller.cs::App.C.Go", "Types.cs::A.S.Run"),
            ("Program.cs", "Types.cs::A.S.Run"),
        ]


def test_unicode_identifiers_and_paths_with_spaces_bind(tmp_path):
    _write(tmp_path, {
        "My Proj é/My Proj.csproj": "<Project />",
        "My Proj é/Imports.cs": "global using Café;",
        "My Proj é/Café.cs": "namespace Café { public class Ünï { public static void Rün() {} } }",
        "My Proj é/Caller.cs": "class C { void Go() { Ünï.Rün(); } }",
    })
    with _store(tmp_path) as store:
        full_build(tmp_path, store)
        assert _calls(store, tmp_path) == [
            ("My Proj é/Caller.cs::C.Go", "My Proj é/Café.cs::Café.Ünï.Rün"),
        ]


def test_nearest_csproj_owns_global_usings(tmp_path):
    _write(tmp_path, {
        "Root.csproj": "<Project />",
        "Imports.cs": "global using Other;",
        "Other.cs": "namespace Other; public class Service { public static void Run() {} }",
        "RootCaller.cs": "class D { void Go() { Service.Run(); } }",
        "Tests/Tests.csproj": "<Project />",
        "Tests/Caller.cs": "class C { void Go() { Service.Run(); } }",
    })
    with _store(tmp_path) as store:
        full_build(tmp_path, store)
        assert _calls(store, tmp_path) == [
            ("RootCaller.cs::D.Go", "Other.cs::Other.Service.Run"),
            # The nested project does not inherit the root project's global using.
            ("Tests/Caller.cs::C.Go", "Service::Run"),
        ]


def test_same_type_in_two_projects_never_binds_the_other_copy(tmp_path):
    # Guard for the documented gap: stays unresolved rather than guessing.
    _write(tmp_path, {
        "P1/P1.csproj": "<Project />",
        "P1/Svc.cs": "namespace Shared; public class Service { public static void Run() {} }",
        "P1/Caller.cs": "using Shared; class C { void Go() { Service.Run(); } }",
        "P2/P2.csproj": "<Project />",
        "P2/Svc.cs": "namespace Shared; public class Service { public static void Run() {} }",
    })
    with _store(tmp_path) as store:
        full_build(tmp_path, store)
        (call,) = _calls(store, tmp_path)
        assert not call[1].startswith("P2/")


def test_php_and_csharp_scoped_resolution_share_one_pass(tmp_path):
    _write(tmp_path, {
        "svc.php": "<?php\nclass Svc { public static function run() {} }\n",
        "caller.php": "<?php\nclass Caller { public function go() { Svc::run(); } }\n",
        "Other.cs": "namespace Other; public class Service { public static void Run() {} }",
        "Caller.cs": "using Other; class C { void Go() { Service.Run(); } }",
    })
    with _store(tmp_path) as store:
        result = full_build(tmp_path, store)
        assert result["scoped_resolution"] == {"files_indexed": 4, "calls_resolved": 2}
        assert _calls(store, tmp_path) == [
            ("Caller.cs::C.Go", "Other.cs::Other.Service.Run"),
            ("caller.php::Caller.go", "svc.php::Svc.run"),
        ]


def test_resolver_tolerates_empty_store_missing_root_and_malformed_extra(tmp_path):
    with _store(tmp_path) as store:
        assert resolve_scoped_calls(store) == {"files_indexed": 0, "calls_resolved": 0}
    _write(tmp_path, {
        "Other.cs": "namespace Other; public class Service { public static void Run() {} }",
        "Caller.cs": "using Other; class C { "
                     "void Go() { Service.Run(); } void Hop() { Service.Run(); } }",
    })
    with _store(tmp_path) as store:
        full_build(tmp_path, store)
        hop = store._conn.execute(
            "SELECT id FROM edges WHERE kind = 'CALLS' AND source_qualified LIKE '%::C.Hop'",
        ).fetchone()[0]
        store._conn.execute("UPDATE edges SET extra = '{not json' WHERE id = ?", (hop,))
        store._conn.execute(
            "UPDATE edges SET target_qualified = 'Service::Run' WHERE kind = 'CALLS'",
        )
        store.commit()
        # No repo root: ownership falls back to the files' common directory.
        resolve_scoped_calls(store)
        calls = dict(_calls(store, tmp_path))
        assert calls["Caller.cs::C.Go"] == "Other.cs::Other.Service.Run"
        assert calls["Caller.cs::C.Hop"] == "Service::Run"  # unreadable evidence is skipped


def test_upsert_edge_keeps_calls_with_distinct_source_offsets(tmp_path):
    def edge(offset):
        extra = {} if offset is None else {"source_offset": offset}
        return EdgeInfo(
            kind="CALLS", source="f::C.Go", target="S::Run", file_path="f", line=1, extra=extra,
        )

    with _store(tmp_path) as store:
        for offset in (10, 20, 20):
            store.upsert_edge(edge(offset))
        store.commit()
        rows = store._conn.execute("SELECT extra FROM edges").fetchall()
        assert sorted(json.loads(r[0])["source_offset"] for r in rows) == [10, 20]
        # Guard: without an offset, one site still collapses to one row.
        store._conn.execute("DELETE FROM edges")
        store.upsert_edge(edge(None))
        store.upsert_edge(edge(None))
        store.commit()
        assert store._conn.execute("SELECT count(*) FROM edges").fetchone()[0] == 1


def test_full_build_is_idempotent_and_noop_update_does_not_reparse(tmp_path):
    _write(tmp_path, {
        "Imports.cs": "global using Other;",
        "Other.cs": "namespace Other; public class Service { public static void Run() {} }",
        "Caller.cs": "namespace App; class C { void Go() { Service.Run(); } }",
    })
    with _store(tmp_path) as store:
        full_build(tmp_path, store)
        first = _calls(store, tmp_path)
        assert first == [("Caller.cs::App.C.Go", "Other.cs::Other.Service.Run")]
        full_build(tmp_path, store)
        assert _calls(store, tmp_path) == first
        assert store.get_metadata("csharp_identity_pending_files") == "[]"
        result = incremental_update(tmp_path, store, changed_files=[])
        assert result["files_updated"] == 0
        assert "identity_rebuild" not in result
        assert _calls(store, tmp_path) == first


def test_graph_from_a_release_without_identity_version_is_reparsed(tmp_path):
    files = {
        "Other.cs": "namespace Other; public class Service { public static void Run() {} }",
        "Caller.cs": "using Other; namespace App; class C { void Go() { Service.Run(); } }",
    }
    _write(tmp_path, files)
    parser = CodeParser(tmp_path)
    with _store(tmp_path) as store:
        # Released graphs store namespace-free identities and no version key.
        for name, namespace in (("Other.cs", "Other"), ("Caller.cs", "App")):
            path = tmp_path / name
            nodes, edges = parser.parse_file(path)
            for node in nodes:
                node.parent_name = (
                    (node.parent_name or "").removeprefix(namespace).lstrip(".") or None
                )
                node.extra.pop("csharp_namespace", None)
            for edge in edges:
                edge.source = edge.source.replace(f"::{namespace}.", "::")
                edge.extra.pop("csharp_raw_target", None)
            store.store_file_nodes_edges(
                str(path), nodes, edges, hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        store.commit()
        assert store.get_metadata("csharp_identity_version") is None
        assert _names(store, "Run", tmp_path) == ["Other.cs::Service.Run"]
        result = incremental_update(tmp_path, store, changed_files=[])
        assert result.get("identity_rebuild") is True
        assert _names(store, "Run", tmp_path) == ["Other.cs::Other.Service.Run"]
        assert _calls(store, tmp_path) == [
            ("Caller.cs::App.C.Go", "Other.cs::Other.Service.Run"),
        ]
        # The upgrade is one-shot: the next update has nothing to reparse.
        again = incremental_update(tmp_path, store, changed_files=[])
        assert again["files_updated"] == 0 and "identity_rebuild" not in again


def test_pending_failed_file_deleted_before_retry_clears_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    _write(tmp_path, {
        "Bad.cs": "namespace Bad; class C { void Run() {} }",
        "Good.cs": "namespace Good; class C { void Run() {} }",
    })
    original = CodeParser.parse_bytes

    def fail_bad(self, path, source):
        if path.name == "Bad.cs":
            raise ValueError("parse failed")
        return original(self, path, source)

    with _store(tmp_path) as store:
        with patch.object(CodeParser, "parse_bytes", fail_bad):
            full_build(tmp_path, store)
        assert json.loads(store.get_metadata("csharp_identity_pending_files")) == ["Bad.cs"]
        (tmp_path / "Bad.cs").unlink()
        result = incremental_update(tmp_path, store, changed_files=[])
        assert result["errors"] == []
        assert store.get_metadata("csharp_identity_pending_files") == "[]"
        assert _names(store, "Run", tmp_path) == ["Good.cs::Good.C.Run"]


def test_stale_reconciliation_restores_raw_reference_for_recreation(tmp_path):
    source = "namespace Other; public class Service { public static void Run() {} }"
    _write(tmp_path, {
        "Other.cs": source,
        "Caller.cs": "using Other; class C { void Go() { Service.Run(); } }",
    })
    with _store(tmp_path) as store:
        full_build(tmp_path, store)
        (tmp_path / "Other.cs").unlink()
        # The deletion is not reported as a change; stale reconciliation finds it.
        result = incremental_update(tmp_path, store, changed_files=[])
        assert result["stale_files_removed"] == 1
        (call,) = store._conn.execute("SELECT * FROM edges WHERE kind = 'CALLS'").fetchall()
        assert call["target_qualified"] == "Service::Run"
        assert "unresolved_targets" in json.loads(call["extra"])
        (tmp_path / "Other.cs").write_text(source, encoding="utf-8")
        incremental_update(tmp_path, store, changed_files=["Other.cs"])
        assert _calls(store, tmp_path) == [("Caller.cs::C.Go", "Other.cs::Other.Service.Run")]


def test_many_callers_rebind_together_when_a_global_alias_changes(tmp_path):
    count = 400
    methods = " ".join(f"void M{i}() {{ S.Run(); }}" for i in range(count))
    _write(tmp_path, {
        "Imports.cs": "global using S = A.Service;",
        "Callers.cs": f"class Callers {{ {methods} }}",
        "A.cs": "namespace A; class Service { public static void Run() {} }",
        "B.cs": "namespace B; class Service { public static void Run() {} }",
    })
    with _store(tmp_path) as store:
        full_build(tmp_path, store)
        targets = {target for _, target in _calls(store, tmp_path)}
        assert targets == {"A.cs::A.Service.Run"}
        assert len(_calls(store, tmp_path)) == count
        (tmp_path / "Imports.cs").write_text("global using S = B.Service;", encoding="utf-8")
        incremental_update(tmp_path, store, changed_files=["Imports.cs"])
        calls = _calls(store, tmp_path)
        assert len(calls) == count
        assert {target for _, target in calls} == {"B.cs::B.Service.Run"}
        before = store._conn.execute("SELECT * FROM edges ORDER BY id").fetchall()
        resolve_scoped_calls(store, tmp_path)
        after = store._conn.execute("SELECT * FROM edges ORDER BY id").fetchall()
        assert [tuple(r) for r in before] == [tuple(r) for r in after]
