"""NodeNext TypeScript extensions remain visible through complete builds (#874)."""

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.tools.query import query_graph
from code_review_graph.tools.review import get_review_context


@pytest.mark.parametrize("specifier,extension", [(".mjs", ".mts"), (".cjs", ".cts")])
@pytest.mark.parametrize("targets", ["native", "native_and_legacy", "legacy"])
def test_full_build_indexes_node_typescript_imports_and_call_context(
    tmp_path,
    specifier,
    extension,
    targets,
):
    caller = tmp_path / "caller.ts"
    caller.write_text(
        f"import {{ calculate }} from './dependency{specifier}';\n"
        "export function run(): number { return calculate(7); }\n"
    )
    target = tmp_path / ("dependency.ts" if targets == "legacy" else f"dependency{extension}")
    target.write_text("export function calculate(value: number): number { return value + 1; }\n")
    if targets == "native_and_legacy":
        (tmp_path / "dependency.ts").write_text(
            "export function calculate(value: number): number { return value - 1; }\n"
        )
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()
    with GraphStore(graph_dir / "graph.db") as store:
        built = full_build(tmp_path, store)
        assert built["errors"] == []
        assert store.get_node(f"{target.as_posix()}::calculate") is not None

    root = str(tmp_path)
    importers = query_graph("importers_of", str(target), repo_root=root)
    assert importers["status"] == "ok"
    assert {node["file"] for node in importers["results"]} == {caller.as_posix()}

    callers = query_graph("callers_of", f"{target.as_posix()}::calculate", repo_root=root)
    assert callers["status"] == "ok"
    assert {node["qualified_name"] for node in callers["results"]} == {f"{caller.as_posix()}::run"}

    callees = query_graph("callees_of", f"{caller.as_posix()}::run", repo_root=root)
    assert callees["status"] == "ok"
    assert {node["qualified_name"] for node in callees["results"]} == {
        f"{target.as_posix()}::calculate"
    }

    review = get_review_context(changed_files=[target.name], repo_root=root)
    assert review["status"] == "ok"
    context = review["context"]
    assert any(node["name"] == "calculate" for node in context["graph"]["changed_nodes"])
    assert any(node["name"] == "run" for node in context["graph"]["impacted_nodes"])
    assert target.name in context["source_snippets"]
