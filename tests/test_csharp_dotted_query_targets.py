"""Type.Member query targets must find namespaced C# symbols (PR #964)."""

from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.tools.query import query_graph


def _write(root: Path, files: dict[str, str]) -> None:
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "C# symbols now carry the namespace, so a Type.Member target "
        "no longer matches exactly and the Java guard blocks the fallback (#964)"
    ),
)
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
