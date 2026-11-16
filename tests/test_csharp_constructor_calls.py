"""C# ``new T(...)`` produces a CALLS edge to the type it constructs."""

import json
from pathlib import Path

from code_review_graph.parser import CodeParser
from tests.test_csharp_namespaces import _build, _calls

MODELS = (
    "namespace App.Models { "
    "public class Order { public Order() {} public Order(int id) {} } "
    "public class Plain {} "
    "public class Box<T> {} }"
)


def _kinds(call) -> str:
    return json.loads(call["extra"])["csharp_call_kind"]


def _targets(store, caller: str, root: Path) -> list[str]:
    return [
        call["target_qualified"].replace(str(root) + "/", "")
        for call in _calls(store, caller)
        if _kinds(call) == "constructor"
    ]


def test_parse_emits_one_constructor_call_per_creation():
    src = b"""namespace App {
  public class Order { public Order() {} public Order(int id) {} }
  public class Svc {
    public Order Make() { var o = new Order(); return new Order(1); }
  }
}"""
    _, edges = CodeParser().parse_bytes(Path("S.cs"), src)
    calls = [e for e in edges if e.kind == "CALLS"]
    assert [e.source for e in calls] == ["S.cs::App.Svc.Make"] * 2
    assert [e.line for e in calls] == [4, 4]
    assert {e.extra["csharp_call_kind"] for e in calls} == {"constructor"}
    assert {e.extra["receiver_scope"] for e in calls} == {"Order"}


def test_creation_forms_resolve_across_files(tmp_path):
    with _build(tmp_path, {
        "Models/Order.cs": MODELS,
        "Svc.cs": (
            "using App.Models; "
            "namespace App.Services { public class Svc { public void Make() { "
            "var a = new Order(); var b = new Order(1); var c = new Order { }; "
            "var d = new App.Models.Order(); var e = new Plain(); "
            "var f = new Box<int>(); } } }"
        ),
    }) as store:
        order = "Models/Order.cs::App.Models.Order"
        assert _targets(store, "App.Services.Svc.Make", tmp_path) == [
            order, order, order, order,
            "Models/Order.cs::App.Models.Plain",
            "Models/Order.cs::App.Models.Box",
        ]
        for call in _calls(store, "App.Services.Svc.Make"):
            assert json.loads(call["extra"])["scoped_resolved"] is True


def test_the_namespace_in_scope_picks_the_type(tmp_path):
    with _build(tmp_path, {
        "A.cs": "namespace A { public class Widget {} }",
        "B.cs": "namespace B { public class Widget {} }",
        "UsesA.cs": "using A; namespace X { class C { void Go() { new Widget(); } } }",
        "UsesB.cs": "using B; namespace Y { class C { void Go() { new Widget(); } } }",
    }) as store:
        assert _targets(store, "X.C.Go", tmp_path) == ["A.cs::A.Widget"]
        assert _targets(store, "Y.C.Go", tmp_path) == ["B.cs::B.Widget"]


def test_an_ambiguous_type_stays_unresolved(tmp_path):
    with _build(tmp_path, {
        "A.cs": "namespace A { public class Widget {} }",
        "B.cs": "namespace B { public class Widget {} }",
        "Both.cs": "using A; using B; class C { void Go() { new Widget(); } }",
    }) as store:
        (call,) = _calls(store, "C.Go")
        assert call["target_qualified"] == "Widget::Widget"


def test_unknown_external_type_invents_no_node(tmp_path):
    with _build(tmp_path, {
        "Svc.cs": (
            "using System.Collections.Generic; "
            "class Svc { void Make() { var l = new List<int>(); "
            "throw new System.InvalidOperationException(); } }"
        ),
    }) as store:
        calls = _calls(store, "Svc.Make")
        assert len(calls) == 2
        # The target is only the raw spelling: no node exists for it, and the
        # pass did not claim a resolution it cannot make.
        for call in calls:
            assert not json.loads(call["extra"]).get("scoped_resolved")
            assert store.get_node(call["target_qualified"]) is None
        names = {
            row[0] for row in store._conn.execute(
                "SELECT name FROM nodes WHERE kind != 'File'",
            )
        }
        assert names == {"Svc", "Make"}


def test_a_test_that_constructs_a_type_is_linked_to_it(tmp_path):
    with _build(tmp_path, {
        "Svc.cs": "namespace App.Services { public class Svc { public void Make() {} } }",
        "SvcTests.cs": (
            "using App.Services; namespace T { public class SvcTests { "
            "[Xunit.Fact] public void TestMake() { var s = new Svc(); s.Make(); } } }"
        ),
    }) as store:
        svc = f"{tmp_path / 'Svc.cs'}::App.Services.Svc"
        test = f"{tmp_path / 'SvcTests.cs'}::T.SvcTests.TestMake"
        mirrors = store._conn.execute(
            "SELECT source_qualified, target_qualified FROM edges "
            "WHERE kind = 'TESTED_BY' ORDER BY id",
        ).fetchall()
        assert [tuple(row) for row in mirrors] == [(svc, test), (f"{svc}.Make", test)]


def test_target_typed_new_is_not_guessed():
    # ``new()`` has no type of its own; it is left out rather than inferred.
    source = b"class C { void Go() { C x = new(); } }"
    _, edges = CodeParser().parse_bytes(Path("T.cs"), source)
    assert [e for e in edges if e.kind == "CALLS"] == []
