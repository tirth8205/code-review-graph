"""Type.Member query targets must find namespaced C# symbols (PR #964)."""

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.tools.query import query_graph


def _write(root: Path, files: dict[str, str]) -> None:
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


def test_dotted_type_member_target_finds_namespaced_csharp_symbol(tmp_path):
    # ``Type.Member`` is the natural way to name a C# method, and query_graph
    # resolves exact qualified tails for C# (#934). A type declared inside a
    # namespace must still be addressable that way, not reported as missing.
    (tmp_path / ".git").mkdir()
    _write(tmp_path, {
        "Service.cs": "namespace Lib { public class Service { public static void Run() {} } }\n",
        "App.cs": (
            "using Lib;\n"
            "namespace App { class Caller { void Go() { Service.Run(); } } }\n"
        ),
    })
    with GraphStore(tmp_path / ".code-review-graph" / "graph.db") as store:
        full_build(tmp_path, store)

    result = query_graph("callers_of", "Service.Run", str(tmp_path))

    assert result["status"] == "ok", result.get("summary")
    assert [row["name"] for row in result["results"]] == ["Go"]


def test_dotted_target_shared_by_two_namespaces_is_ambiguous(tmp_path):
    # The namespace-tail fallback must not pick one of two same-named types.
    (tmp_path / ".git").mkdir()
    _write(tmp_path, {
        "A.cs": "namespace A { public class Service { public static void Run() {} } }\n",
        "B.cs": "namespace B { public class Service { public static void Run() {} } }\n",
    })
    with GraphStore(tmp_path / ".code-review-graph" / "graph.db") as store:
        full_build(tmp_path, store)

    result = query_graph("callers_of", "Service.Run", str(tmp_path))

    assert result["status"] == "ambiguous"
    assert {c["qualified_name"].rsplit("::", 1)[-1] for c in result["candidates"]} == {
        "A.Service.Run", "B.Service.Run",
    }


def test_dotted_target_does_not_match_a_longer_type_name(tmp_path):
    # ``Service.Run`` must not match ``MyService.Run``: the tail is whole segments.
    (tmp_path / ".git").mkdir()
    _write(tmp_path, {
        "A.cs": "namespace A { public class MyService { public static void Run() {} } }\n",
    })
    with GraphStore(tmp_path / ".code-review-graph" / "graph.db") as store:
        full_build(tmp_path, store)

    assert query_graph("callers_of", "Service.Run", str(tmp_path))["status"] == "not_found"
