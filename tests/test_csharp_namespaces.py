"""Namespace identity and binding regressions for #946, through persisted graphs."""

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import CSHARP_IDENTITY_VERSION, incremental_update
from code_review_graph.parser import CodeParser
from code_review_graph.scoped_resolver import resolve_scoped_calls


def _build(root: Path, files: dict[str, str]) -> GraphStore:
    store = GraphStore(root / ".code-review-graph" / "graph.db")
    parser = CodeParser(root)
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        if path.suffix == ".cs":
            nodes, edges = parser.parse_file(path)
            store.store_file_nodes_edges(
                str(path), nodes, edges, hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    store.set_metadata("csharp_identity_version", CSHARP_IDENTITY_VERSION)
    resolve_scoped_calls(store, root)
    store.resolve_bare_call_targets()
    store.resolve_bare_tested_by_sources()
    return store


def _calls(store: GraphStore, caller: str):
    return store._conn.execute(
        "SELECT * FROM edges WHERE kind = 'CALLS' AND source_qualified LIKE ? ORDER BY line, id",
        (f"%::{caller}",),
    ).fetchall()


@pytest.mark.parametrize(("source", "caller", "expected"), [
    (
        "namespace Other { class App { public class Report { public class ExportHandler "
        "{ public static void Run() {} } } } } "
        "namespace Consumer { class C { void Go() { App.Report.ExportHandler.Run(); } } }",
        "Consumer.C.Go", None,
    ),
    (
        "namespace Other { class Service { public static void Run() {} } } "
        "namespace Consumer { class C { void Go() { Service.Run(); } } }",
        "Consumer.C.Go", None,
    ),
    (
        "namespace Other { class Service { public static void Run() {} } } "
        "namespace Consumer { class C { void Go() { Other.Service.Run(); } } }",
        "Consumer.C.Go", "Other.Service.Run",
    ),
    (
        "namespace Other { class Service { public static void Run() {} } } "
        "namespace A { using Other; class C { void Go() { Service.Run(); } } } "
        "namespace B { class C { void Go() { Service.Run(); } } }",
        "B.C.Go", None,
    ),
    (
        "namespace Other { class Service { public static void Run() {} } } "
        "namespace A { using Other; class First {} } "
        "namespace A { class C { void Go() { Service.Run(); } } }",
        "A.C.Go", None,
    ),
    (
        "namespace Other { class Service { public static void Run() {} } } "
        "namespace A { using Other; namespace B { class C { void Go() { Service.Run(); } } } }",
        "A.B.C.Go", "Other.Service.Run",
    ),
    (
        "namespace A { class Service { public static void Run() {} } } "
        "namespace B { class Service {} } "
        "namespace Consumer { using A; using B; class C { void Go() { Service.Run(); } } }",
        "Consumer.C.Go", None,
    ),
    (
        "namespace A.B { class Service { public static void Run() {} } } "
        "namespace Consumer { using A; class C { void Go() { B.Service.Run(); } } }",
        "Consumer.C.Go", None,
    ),
    (
        "namespace A { class Service { public static void Run() {} } } "
        "namespace Consumer { using Alias = global::A; "
        "class C { void Go() { Alias.Service.Run(); } } }",
        "Consumer.C.Go", "A.Service.Run",
    ),
    (
        "namespace A { class Service { public static void Run() {} } } "
        "namespace Consumer { class A {} class C { void Go() { A.Service.Run(); } } }",
        "Consumer.C.Go", None,
    ),
    (
        "namespace A { class Service { public static void Run() {} } } "
        "namespace Consumer { class A {} class C { void Go() { global::A.Service.Run(); } } }",
        "Consumer.C.Go", "A.Service.Run",
    ),
    (
        "namespace A { class Outer { public class Service { public static void Run() {} } "
        "class C { public class Service {} void Go() { Service.Run(); } } } }",
        "A.Outer.C.Go", None,
    ),
    (
        "namespace A { class Outer { public class D { public class E { "
        "public static void Run() {} } } class C { void Go() { D.E.Run(); } } } } "
        "class D { public class E { public static void Run() {} } }",
        "A.Outer.C.Go", "A.Outer.D.E.Run",
    ),
    (
        "namespace A { class C { public void Run() {} void Go() { Run(); } } } "
        "namespace B { class C { public void Run() {} } }",
        "A.C.Go", "A.C.Run",
    ),
    (
        "namespace @A { namespace /* trivia */ B { "
        "class @Service { public static void @Run() {} } } } "
        "namespace Consumer { using /* trivia */ Alias = A.B; "
        "class C { void Go() { Alias.Service.Run(); } } }",
        "Consumer.C.Go", "A.B.Service.Run",
    ),
    (
        "using Alias = App; namespace App { class Service { public static void Run() {} } } "
        "class C { void Go() { Alias::Service.Run(); } }",
        "C.Go", "App.Service.Run",
    ),
    (
        "namespace App { class Outer { static void Run() {} "
        "class Inner { void Go() { Run(); } } } }",
        "App.Outer.Inner.Go", "App.Outer.Run",
    ),
    (
        "using Alias = App; class Alias {} "
        "namespace App { class Service { public static void Run() {} } } "
        "class C { void Go(int Alias) { Alias::Service.Run(); } }",
        "C.Go", "App.Service.Run",
    ),
    (
        "using Alias = App.Container; namespace App { class Container { "
        "public class Service { public static void Run() {} } } } "
        "class C { void Go() { Alias::Service.Run(); } }",
        "C.Go", None,
    ),
    (
        "namespace App { class Service { public static void Run() {} } } "
        "namespace N { using Alias = App; class C {} } "
        "namespace N { class C { void Go() { Alias::Service.Run(); } } }",
        "N.C.Go", None,
    ),
    (
        "using Alias = App; namespace App { class Service { public void Run() {} } } "
        "class C { void Go(Alias::Service value) { value.Run(); } }",
        "C.Go", "App.Service.Run",
    ),
    (
        "namespace App { class Outer { static void Run() {} "
        "class Inner { void Go() { this.Run(); } } } }",
        "App.Outer.Inner.Go", None,
    ),
    (
        "namespace App { class Outer { void Run() {} "
        "class Inner { void Go() { Run(); } } } }",
        "App.Outer.Inner.Go", None,
    ),
    (
        "namespace App { class Outer { static void Run() {} "
        "class Inner { void Run() {} void Go() { Run(); } } } }",
        "App.Outer.Inner.Go", "App.Outer.Inner.Run",
    ),
    (
        "namespace App { class Outer { static void Run() {} "
        "class Inner { void Go(System.Action Run) { Run(); } } } }",
        "App.Outer.Inner.Go", None,
    ),
    (
        "namespace A { class Box<T> { public void Run() {} } "
        "class C { void Go(Box<int> value) { value.Run(); } } }",
        "A.C.Go", "A.Box.Run",
    ),
    (
        "namespace A { class Pair<K, V> { public void Run() {} } "
        "class C { void Go(Pair<string, int> value) { value.Run(); } } }",
        "A.C.Go", "A.Pair.Run",
    ),
    (
        "namespace A { class Box<T> { public void Run() {} } class C { "
        "void Go(Box<System.Collections.Generic.List<int>> value) { value.Run(); } } }",
        "A.C.Go", "A.Box.Run",
    ),
    (
        "namespace A { class Pair<K, V> { public void Run() {} } "
        "class C { void Go(Pair<int> value) { value.Run(); } } }",
        "A.C.Go", None,
    ),
    (
        "namespace A { class Outer<T> { public class Inner { public void Run() {} } } "
        "class C { void Go(Outer<int>.Inner value) { value.Run(); } } }",
        "A.C.Go", None,
    ),
])
def test_namespace_binding(source, caller, expected, tmp_path):
    with _build(tmp_path, {"Case.cs": source}) as store:
        calls = _calls(store, caller)
        assert len(calls) == 1
        extra = json.loads(calls[0]["extra"])
        if expected:
            assert calls[0]["target_qualified"] == f"{tmp_path / 'Case.cs'}::{expected}"
            assert "unresolved_targets" not in extra
        else:
            assert calls[0]["target_qualified"] == extra["csharp_raw_target"]
            assert "unresolved_targets" in extra


@pytest.mark.parametrize("declaration", [
    "System.Action Run; void Go() { Run(); }",
    "System.Action Run { get; set; } void Go() { Run(); }",
    "void Go() { System.Action Run = () => {}; Run(); }",
    "void Go() { void Run() {} Run(); }",
    "void Go() { Run(); void Run() {} }",
])
def test_enclosing_static_lookup_preserves_callable_shadowing(tmp_path, declaration):
    with _build(tmp_path, {"Case.cs":
        "namespace App { class Outer { static void Run() {} class Inner { "
        + declaration + " } } }"
    }) as store:
        call, = _calls(store, "App.Outer.Inner.Go")
        assert call["target_qualified"] == "Run"
        assert "unresolved_targets" in json.loads(call["extra"])


def test_same_file_identities_and_same_line_calls_survive_storage(tmp_path):
    with _build(tmp_path, {"Case.cs": """
namespace A { class Outer { public class Inner { public static void Run() {} } } }
namespace B { class Outer { public class Inner { public static void Run() {} } } }
class Caller { void Go() { A.Outer.Inner.Run(); B.Outer.Inner.Run(); } }
"""}) as store:
        calls = _calls(store, "Caller.Go")
        assert {row["target_qualified"].split("::")[1] for row in calls} == {
            "A.Outer.Inner.Run", "B.Outer.Inner.Run",
        }
        nodes = store.get_nodes_by_file(str(tmp_path / "Case.cs"))
        assert len([node for node in nodes if node.name == "Run"]) == 2
        contains = store._conn.execute(
            "SELECT source_qualified, target_qualified FROM edges WHERE kind = 'CONTAINS'",
        ).fetchall()
        assert (f"{tmp_path / 'Case.cs'}::A.Outer", f"{tmp_path / 'Case.cs'}::A.Outer.Inner") in [
            tuple(row) for row in contains
        ]


@pytest.mark.parametrize(("declaration", "expected_file", "expected_name"), [
    ("class C { static int Value = Service.Run(); }", "App.cs", "App.Service.Run"),
    ("class C { static int Value { get; } = Service.Run(); }", "App.cs", "App.Service.Run"),
    (
        "class Outer { public class Service { public static int Run() => 3; } "
        "class C { static int Value = Service.Run(); } }",
        "Caller.cs", "App.Outer.Service.Run",
    ),
    (
        "class C { static int Init() => 3; static int Value = Init(); }",
        "Caller.cs", "App.C.Init",
    ),
])
def test_initializers_keep_lexical_type_context(
    tmp_path, declaration, expected_file, expected_name,
):
    with _build(tmp_path, {
        "Global.cs": "public class Service { public static int Run() => 1; }",
        "App.cs": "namespace App; public class Service { public static int Run() => 2; }",
        "Caller.cs": "namespace App; " + declaration,
    }) as store:
        calls = store._conn.execute(
            "SELECT * FROM edges WHERE kind = 'CALLS' AND source_qualified = ?",
            (str(tmp_path / "Caller.cs"),),
        ).fetchall()
        assert len(calls) == 1
        assert calls[0]["target_qualified"] == f"{tmp_path / expected_file}::{expected_name}"


@pytest.mark.parametrize("type_name", ["Service?", "global::Other.Service?", "Service<int>?"])
def test_nullable_receivers_keep_raw_spelling_and_generic_boundaries(tmp_path, type_name):
    with _build(tmp_path, {
        "Service.cs": "namespace Other; public class Service { public void Run() {} }",
        "Generic.cs": "namespace Other; public class Service<T> { public void Run() {} }",
        "Caller.cs": (
            "#nullable enable\nusing Other; namespace App; class C { "
            f"{type_name} field; void Go({type_name} value) {{ {type_name} local = value; "
            "value?.Run(); field.Run(); local?.Run(); } }"
        ),
    }) as store:
        calls = _calls(store, "App.C.Go")
        assert len(calls) == 3
        for call in calls:
            extra = json.loads(call["extra"])
            assert extra["receiver_type"] == type_name
            assert extra["csharp_raw_target"] == f"{type_name}::Run"
            expected = "Generic.cs" if "<" in type_name else "Service.cs"
            assert call["target_qualified"] == f"{tmp_path / expected}::Other.Service.Run"
            assert "unresolved_targets" not in extra


@pytest.mark.parametrize("separator", [" ", "\n"])
def test_typed_receiver_evidence_and_test_mirrors_follow_each_call(tmp_path, separator):
    with _build(tmp_path, {
        "A.cs": "namespace A; public class Service { public void Run() {} }",
        "B.cs": "namespace B; public class Service { public void Run() {} }",
        "CallerTests.cs": (
            "// π makes byte offsets differ from character offsets.\n"
            "class CallerTests { void TestRun() { "
            "{ A.Service s = new A.Service(); s.Run(); }" + separator
            + "{ B.Service s = new B.Service(); s.Run(); } } }"
        ),
    }) as store:
        calls = [
            call for call in _calls(store, "CallerTests.TestRun")
            if json.loads(call["extra"])["csharp_call_kind"] != "constructor"
        ]
        expected = [f"{tmp_path / (ns + '.cs')}::{ns}.Service.Run" for ns in ("A", "B")]
        assert [call["target_qualified"] for call in calls] == expected
        mirrors = store._conn.execute(
            "SELECT source_qualified FROM edges WHERE kind = 'TESTED_BY' "
            "AND source_qualified LIKE '%.Run' ORDER BY id",
        ).fetchall()
        assert [row[0] for row in mirrors] == expected


def test_repeated_usings_on_one_line_retain_both_namespace_bodies(tmp_path):
    with _build(tmp_path, {"Case.cs":
        "namespace Other { class S { public static void Run() {} } } "
        "namespace A { using Other; class C { void Go() { S.Run(); } } } "
        "namespace B { using Other; class C { void Go() { S.Run(); } } }"
    }) as store:
        for caller in ("A.C.Go", "B.C.Go"):
            assert _calls(store, caller)[0]["target_qualified"].endswith("::Other.S.Run")


@pytest.mark.parametrize("inside", [False, True])
def test_using_before_and_after_file_scoped_namespace_has_distinct_lookup(tmp_path, inside):
    caller = "namespace App; using Other;" if inside else "using Other; namespace App;"
    with _build(tmp_path, {
        "Caller.cs": caller + " class C { void Go() { S.Run(); } }",
        "Types.cs": "namespace Other { class S { public static void Run() {} } } "
                    "namespace App.Other { class S { public static void Run() {} } }",
    }) as store:
        namespace = "App.Other" if inside else "Other"
        assert _calls(store, "App.C.Go")[0]["target_qualified"].endswith(f"::{namespace}.S.Run")


@pytest.mark.parametrize("nested", [False, True])
def test_partial_type_methods_keep_their_declaring_file(tmp_path, nested):
    declaration = "class Inner { void Go() { Run(); } }" if nested else "void Go() { Run(); }"
    modifier = "static " if nested else ""
    with _build(tmp_path, {
        "First.cs": "namespace A; partial class C { " + declaration + " }",
        "Second.cs": "namespace A; partial class C { public " + modifier + "void Run() {} }",
        "Caller.cs": "using A; class Consumer { void Go(C value) { "
                     + ("C" if nested else "value") + ".Run(); } }",
    }) as store:
        for caller in ("A.C.Inner.Go" if nested else "A.C.Go", "Consumer.Go"):
            assert _calls(store, caller)[0]["target_qualified"] == (
                f"{tmp_path / 'Second.cs'}::A.C.Run"
            )


@pytest.mark.parametrize("parameters", ["<T>", "<T, U>"])
@pytest.mark.parametrize("split", [False, True])
def test_generic_partial_lookup_preserves_declaring_arity(tmp_path, parameters, split):
    method = "public void Run() {}"
    files = {
        "First.cs": (
            f"namespace App; partial class Box{parameters} {{ "
            "void Go() { Run(); this.Run(); } " + ("" if split else method) + " }"
        ),
    }
    if split:
        other_parameters = "<T, U>" if parameters == "<T>" else "<T>"
        files.update({
            "Second.cs": f"namespace App; partial class Box{parameters} {{ {method} }}",
            "Plain.cs": f"namespace App; partial class Box {{ {method} }}",
            "OtherArity.cs": f"namespace App; partial class Box{other_parameters} {{ {method} }}",
        })
    with _build(tmp_path, files) as store:
        calls = _calls(store, "App.Box.Go")
        expected_file = "Second.cs" if split else "First.cs"
        assert len(calls) == 2
        assert all(
            call["target_qualified"] == f"{tmp_path / expected_file}::App.Box.Run"
            for call in calls
        )


def test_nested_caller_uses_generic_partial_enclosing_type(tmp_path):
    with _build(tmp_path, {
        "First.cs": "namespace App; partial class Box<T> { "
                    "class Inner { void Go() { Run(); } } }",
        "Second.cs": "namespace App; partial class Box<T> { public static void Run() {} }",
        "Plain.cs": "namespace App; partial class Box { public static void Run() {} }",
    }) as store:
        call, = _calls(store, "App.Box.Inner.Go")
        assert call["target_qualified"] == f"{tmp_path / 'Second.cs'}::App.Box.Run"


def test_generic_reference_is_not_erased_to_a_non_generic_declaration(tmp_path):
    with _build(tmp_path, {
        "Plain.cs": "namespace A; class I { public void Run() {} }",
        "Generic.cs": "namespace A; class I<T> { public void Run() {} }",
        "Caller.cs": "using A; class C { void Go(I<int> value) { value.Run(); } }",
    }) as store:
        call = _calls(store, "C.Go")[0]
        # Arity selects the declaration the reference names; the separate
        # non-generic ``I`` stays out of reach, and the spelling is retained.
        assert call["target_qualified"] == f"{tmp_path / 'Generic.cs'}::A.I.Run"
        assert json.loads(call["extra"])["receiver_scope"] == "I<int>"


def test_bare_type_receiver_does_not_select_generic_declaration(tmp_path):
    with _build(tmp_path, {
        "Generic.cs": "namespace A; class I<T> { public static void Run() {} }",
        "Caller.cs": "using A; class C { void Go() { I.Run(); } }",
    }) as store:
        assert _calls(store, "C.Go")[0]["target_qualified"] == "I::Run"


def test_this_does_not_bind_an_enclosing_types_instance_method(tmp_path):
    with _build(tmp_path, {"Case.cs":
        "namespace A { class Outer { public void Run() {} "
        "class Inner { void Go() { this.Run(); } } } }"
    }) as store:
        assert _calls(store, "A.Outer.Inner.Go")[0]["target_qualified"] == "this::Run"


@pytest.mark.parametrize("using", ["using Other;", "global using Other;"])
def test_file_scoped_namespace_and_typed_receiver(tmp_path, using):
    with _build(tmp_path, {
        "Imports.cs": "global using Other;" if using.startswith("global") else "",
        "Caller.cs": (
            (using if not using.startswith("global") else "")
            + "namespace Consumer; class C { "
            "void Go(global::Other.Service value) { value.Run(); } }"
        ),
        "Other.cs": "namespace Other; class Service { public void Run() {} }",
    }) as store:
        assert _calls(store, "Consumer.C.Go")[0]["target_qualified"] == (
            f"{tmp_path / 'Other.cs'}::Other.Service.Run"
        )


def test_global_usings_are_project_scoped_and_rebound_on_update(tmp_path):
    with _build(tmp_path, {
        "One/One.csproj": "<Project />",
        "Two/Two.csproj": "<Project />",
        "One/Imports.cs": "global using Other;",
        "One/Caller.cs": "namespace One; class C { void Go() { Service.Run(); } }",
        "Two/Caller.cs": "namespace Two; class C { void Go() { Service.Run(); } }",
        "One/Other.cs": "namespace Other; class Service { public static void Run() {} }",
    }) as store:
        assert _calls(store, "One.C.Go")[0]["target_qualified"].endswith("::Other.Service.Run")
        assert _calls(store, "Two.C.Go")[0]["target_qualified"] == "Service::Run"
        (tmp_path / "One/Imports.cs").write_text("// removed", encoding="utf-8")
        incremental_update(tmp_path, store, changed_files=["One/Imports.cs"])
        assert _calls(store, "One.C.Go")[0]["target_qualified"] == "Service::Run"
        (tmp_path / "One/Imports.cs").write_text("global using Other;", encoding="utf-8")
        incremental_update(tmp_path, store, changed_files=["One/Imports.cs"])
        assert _calls(store, "One.C.Go")[0]["target_qualified"].endswith("::Other.Service.Run")


@pytest.mark.parametrize("postprocess", ["full", "minimal", "none"])
def test_rebinding_refreshes_flows_for_unchanged_callers(tmp_path, postprocess):
    from code_review_graph.flows import get_flows, store_flows, trace_flows
    from code_review_graph.tools.build import _run_postprocess, build_or_update_graph

    with _build(tmp_path, {
        "Imports.cs": "global using S = A.Service;",
        "Caller.cs": "class Caller { void Go() { S.Run(); } }",
        "A.cs": "namespace A; class Service { public static void Run() { Finish(); } "
                "static void Finish() {} }",
        "B.cs": "namespace B; class Service { public static void Run() { Finish(); } "
                "static void Finish() {} }",
    }) as store:
        store_flows(store, trace_flows(store))
        assert len(get_flows(store)) == 2
        for directive in (
            "global using S = B.Service;", "// removed", "global using S = A.Service;",
        ):
            (tmp_path / "Imports.cs").write_text(directive, encoding="utf-8")
            result = incremental_update(tmp_path, store, changed_files=["Imports.cs"])
            assert result["files_updated"] == 1
            if postprocess == "full" and directive == "global using S = B.Service;":
                previous_flows = get_flows(store)
                with pytest.raises(KeyError):
                    # Failed replacement must preserve flows and pending invalidation.
                    store_flows(store, [{}])
                assert get_flows(store) == previous_flows
            _run_postprocess(store, result, postprocess, changed_files=result["changed_files"])
            if postprocess != "full":
                # A later full postprocess must consume invalidation even with no source changes.
                with patch("code_review_graph.incremental.get_changed_files", return_value=[]):
                    build_or_update_graph(repo_root=str(tmp_path), base="HEAD", postprocess="full")
            assert {
                flow["entry_point_id"]: flow["path"] for flow in get_flows(store)
            } == {
                flow["entry_point_id"]: flow["path"] for flow in trace_flows(store)
            }

        (tmp_path / "A.cs").unlink()
        result = incremental_update(tmp_path, store, changed_files=["A.cs"])
        _run_postprocess(store, result, "full", changed_files=result["changed_files"])
        assert {
            flow["entry_point_id"]: flow["path"] for flow in get_flows(store)
        } == {
            flow["entry_point_id"]: flow["path"] for flow in trace_flows(store)
        }


def test_global_using_and_tested_by_survive_repeated_passes(tmp_path):
    with _build(tmp_path, {
        "Imports.cs": "global using Other;",
        "CaseTests.cs": "class CaseTests { void TestRun() { Service.Run(); } }",
        "Other.cs": "namespace Other; class Service { public static void Run() {} }",
    }) as store:
        first = [tuple(r) for r in store._conn.execute("SELECT * FROM edges ORDER BY id")]
        assert resolve_scoped_calls(store, tmp_path)["calls_resolved"] == 0
        assert first == [tuple(r) for r in store._conn.execute("SELECT * FROM edges ORDER BY id")]
        mirror = store._conn.execute("SELECT * FROM edges WHERE kind = 'TESTED_BY'").fetchone()
        assert mirror["source_qualified"].endswith("::Other.Service.Run")
        (tmp_path / "Imports.cs").write_text("// removed", encoding="utf-8")
        incremental_update(tmp_path, store, changed_files=["Imports.cs"])
        store.resolve_bare_call_targets()
        store.resolve_bare_tested_by_sources()
        mirror = store._conn.execute("SELECT * FROM edges WHERE kind = 'TESTED_BY'").fetchone()
        assert mirror["source_qualified"] == "Service::Run"
        assert "unresolved_targets" in json.loads(mirror["extra"])


@pytest.mark.parametrize("ambiguous_project", [False, True])
@pytest.mark.parametrize("qualified", [False, True])
def test_global_alias_requires_known_shared_project_ownership(
    tmp_path, ambiguous_project, qualified,
):
    alias_target = "Other" if qualified else "Other.Service"
    receiver = "Alias::Service" if qualified else "Alias"
    files = {
        "One.csproj": "<Project />",
        "Imports.cs": f"global using Alias = {alias_target};",
        "Caller.cs": "class C { void Go() { " + receiver + ".Run(); } }",
        "Service.cs": "namespace Other; class Service { public static void Run() {} }",
    }
    if ambiguous_project:
        files["Two.csproj"] = "<Project />"
    with _build(tmp_path, files) as store:
        target = _calls(store, "C.Go")[0]["target_qualified"]
        if ambiguous_project:
            assert target == f"{receiver}::Run"
        else:
            assert target.endswith("::Other.Service.Run")


@pytest.mark.parametrize("stored_version", ["1", "2", "3", "4"])
def test_upgrade_retries_only_failed_files_and_bypasses_unchanged_hash(tmp_path, stored_version):
    with _build(tmp_path, {
        "Good.cs": "namespace Good; class C { static int Run() => 1; static int Value = Run(); }",
        "Bad.cs": "namespace Bad; class C { public void Run() {} }",
        "untouched.py": "def f(): pass",
    }) as store:
        store.set_metadata("csharp_identity_version", stored_version)
        # Seed old identities or call context while retaining the current hash.
        for name in ("Good", "Bad"):
            path = tmp_path / f"{name}.cs"
            nodes, edges = CodeParser().parse_file(path)
            for node in nodes:
                node.extra.pop("csharp_static", None)
            if stored_version == "1":
                for node in nodes:
                    node.parent_name = (
                        (node.parent_name or "").removeprefix(name).lstrip(".") or None
                    )
                    node.extra.pop("csharp_namespace", None)
            for edge in edges:
                if stored_version == "1":
                    edge.source = edge.source.replace(f"::{name}.", "::")
                    edge.target = edge.target.replace(f"::{name}.", "::")
                edge.extra.pop("csharp_containing_type", None)
            store.store_file_nodes_edges(
                str(path), nodes, edges, hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        parser = CodeParser.parse_bytes
        attempts = []

        def fail_bad(self, path, source):
            attempts.append(path.name)
            if path.name == "Bad.cs":
                raise ValueError("persistent parser failure")
            return parser(self, path, source)

        with patch.object(CodeParser, "parse_bytes", fail_bad):
            upgraded = incremental_update(tmp_path, store, changed_files=[])
            assert upgraded["identity_rebuild"]
            assert set(attempts) == {"Good.cs", "Bad.cs"}
            run = store.get_node(f"{tmp_path / 'Good.cs'}::Good.C.Run")
            assert run is not None and run.extra["csharp_static"]
            assert store.get_node(f"{tmp_path / 'Good.cs'}::C.Run") is None
            call = store._conn.execute("SELECT * FROM edges WHERE kind = 'CALLS'").fetchone()
            assert call["target_qualified"] == f"{tmp_path / 'Good.cs'}::Good.C.Run"
            assert json.loads(call["extra"])["csharp_containing_type"] == "Good.C"
            attempts.clear()
            retried = incremental_update(tmp_path, store, changed_files=[])
            assert attempts == ["Bad.cs"]
            assert len(retried["errors"]) == 1
        recovered = incremental_update(tmp_path, store, changed_files=[])
        assert recovered["files_updated"] == 1
        assert store.get_metadata("csharp_identity_pending_files") == "[]"
        assert store.get_node(f"{tmp_path / 'Bad.cs'}::Bad.C.Run") is not None
        assert store.get_node(f"{tmp_path / 'Bad.cs'}::C.Run") is None


def test_deleted_callee_keeps_raw_reference_for_recreation(tmp_path):
    with _build(tmp_path, {
        "Caller.cs": "using Other; class C { void Go() { Service.Run(); } }",
        "Other.cs": "namespace Other; class Service { public static void Run() {} }",
    }) as store:
        source = (tmp_path / "Other.cs").read_text()
        (tmp_path / "Other.cs").unlink()
        incremental_update(tmp_path, store, changed_files=["Other.cs"])
        assert _calls(store, "C.Go")[0]["target_qualified"] == "Service::Run"
        (tmp_path / "Other.cs").write_text(source, encoding="utf-8")
        incremental_update(tmp_path, store, changed_files=["Other.cs"])
        assert _calls(store, "C.Go")[0]["target_qualified"].endswith("::Other.Service.Run")
